"""Exactly-once generation receipts for /chat/text and /chat/image.

Correctness comes from PostgreSQL unique + atomic claim, not process locks.
Zeabur multi-worker retries of the same source_event_id share one receipt.

Side effects use a durable pending/processing/completed/failed ledger.
Receipt complete and pending side-effect rows commit in one transaction.
"""
import copy
import json
import threading
import time
import uuid

from db import get_conn


ENDPOINT_CHAT_TEXT = 'chat_text'
ENDPOINT_CHAT_IMAGE = 'chat_image'

STATUS_PROCESSING = 'processing'
STATUS_COMPLETED = 'completed'
STATUS_FAILED = 'failed'
STATUS_PENDING = 'pending'

DEFAULT_LEASE_SECONDS = 240
DEFAULT_WAIT_SECONDS = 12.0
DEFAULT_POLL_SECONDS = 0.3
DEFAULT_EFFECT_LEASE_SECONDS = 120
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 45
MAX_EFFECT_ATTEMPTS = 8

# Test-only crash injection. Production code never sets these.
CRASH_BEFORE_COMPLETE = False
CRASH_BEFORE_EFFECT = None
CRASH_AFTER_EFFECT = None


def assistant_turn_id_for(endpoint, source_event_id):
    source = str(source_event_id or '').strip()
    if endpoint == ENDPOINT_CHAT_IMAGE:
        return f'image_reply:{source}'
    return f'chat_reply:{source}'


def occurrence_key_for(endpoint, source_event_id, effect):
    return f'{endpoint}:{source_event_id}:{effect}'


def assign_source_event_id(raw):
    """Formal chat/text and chat/image always participate in receipts."""
    source = str(raw or '').strip()
    if source:
        return source, False
    assigned = 'legacy:' + uuid.uuid4().hex
    print(f'[chat:idempotency] legacy request without source_event_id; assigned {assigned}')
    return assigned, True


def stamp_assistant_messages(msgs, turn_id):
    stamped = []
    for index, raw in enumerate(msgs or []):
        item = dict(raw or {})
        item['event_id'] = f'{turn_id}:{index}'
        item['segment_index'] = index
        stamped.append(item)
    return stamped


def strip_audio(msgs):
    out = []
    for raw in msgs or []:
        item = dict(raw or {})
        item.pop('audio_b64', None)
        out.append(item)
    return out


def canonical_payload(body):
    """Persistable application response. Never stores audio_b64 or signed URLs."""
    payload = copy.deepcopy(body or {})
    payload['messages'] = strip_audio(payload.get('messages') or [])
    media = payload.get('media')
    if isinstance(media, dict):
        media = dict(media)
        media.pop('url', None)
        payload['media'] = media
    return payload


def needed_effects(endpoint, payload, ctx=None):
    """Effects required by a completed canonical payload. Failed receipts get none."""
    ctx = ctx or {}
    payload = payload or {}
    effects = ['assistant_short_memory', 'record_turn']
    user_text = str(
        ctx.get('user_text')
        or payload.get('_user_text')
        or payload.get('user_text')
        or ''
    ).strip()
    if user_text:
        effects.append('private_extraction')
    if endpoint in (ENDPOINT_CHAT_TEXT, ENDPOINT_CHAT_IMAGE):
        effects.append('behavior_evidence')
        effects.append('relationship_update')
        msgs = payload.get('messages') or []
        if any(str((m or {}).get('zh') or '').strip() for m in msgs):
            effects.append('promise_detector')
    if payload.get('reminder'):
        effects.append('reminder')
    if (payload.get('cancelled_tasks')
            or payload.get('cancel_reminder')
            or payload.get('_cancel_reminder')
            or payload.get('_cancel_targets')
            or ctx.get('cancel_reminder')):
        effects.append('cancel_reminder')
    if (payload.get('saved_promise')
            or payload.get('proactive_promise')
            or payload.get('_proactive_promise')
            or ctx.get('proactive_promise')):
        effects.append('proactive_promise')
    return effects


def init_generation_receipt_table():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute('''CREATE TABLE IF NOT EXISTS chat_generation_receipt (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            status TEXT NOT NULL,
            claim_token TEXT,
            claimed_at TIMESTAMPTZ,
            claim_expires_at TIMESTAMPTZ,
            response_json JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMPTZ,
            failed_at TIMESTAMPTZ,
            last_error TEXT,
            UNIQUE (user_id, character_id, source_event_id, endpoint)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS chat_generation_side_effect (
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            endpoint TEXT NOT NULL,
            effect TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            claim_token TEXT,
            claimed_at TIMESTAMPTZ,
            claim_expires_at TIMESTAMPTZ,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMPTZ,
            PRIMARY KEY (user_id, character_id, source_event_id, endpoint, effect)
        )''')
        cur.execute(
            "ALTER TABLE chat_generation_side_effect "
            "ADD COLUMN IF NOT EXISTS status TEXT")
        cur.execute(
            '''UPDATE chat_generation_side_effect
               SET status='completed',
                   completed_at=COALESCE(completed_at, created_at)
               WHERE status IS NULL''')
        for stmt in (
            "ALTER TABLE chat_generation_side_effect ADD COLUMN IF NOT EXISTS claim_token TEXT",
            "ALTER TABLE chat_generation_side_effect ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ",
            "ALTER TABLE chat_generation_side_effect ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ",
            "ALTER TABLE chat_generation_side_effect ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE chat_generation_side_effect ADD COLUMN IF NOT EXISTS last_error TEXT",
            "ALTER TABLE chat_generation_side_effect ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
            "ALTER TABLE chat_generation_side_effect ADD COLUMN IF NOT EXISTS payload_json JSONB",
            "ALTER TABLE chat_generation_side_effect ADD COLUMN IF NOT EXISTS result_json JSONB",
            "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS occurrence_key TEXT",
            "ALTER TABLE proactive_promise ADD COLUMN IF NOT EXISTS occurrence_key TEXT",
        ):
            try:
                cur.execute('SAVEPOINT receipt_alter')
                cur.execute(stmt)
                cur.execute('RELEASE SAVEPOINT receipt_alter')
            except Exception as e:
                print(f'[init] generation receipt alter skipped:{stmt}:{e}')
                cur.execute('ROLLBACK TO SAVEPOINT receipt_alter')
        cur.execute('''CREATE TABLE IF NOT EXISTS temporal_turn_once (
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, character_id, source_event_id)
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS task_cancel_occurrence (
            occurrence_key TEXT PRIMARY KEY,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
        )''')
        try:
            cur.execute('SAVEPOINT receipt_occ_idx')
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_occurrence_key "
                "ON tasks (occurrence_key) "
                "WHERE occurrence_key IS NOT NULL AND occurrence_key <> ''")
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_promise_occurrence_key "
                "ON proactive_promise (occurrence_key) "
                "WHERE occurrence_key IS NOT NULL AND occurrence_key <> ''")
            cur.execute('RELEASE SAVEPOINT receipt_occ_idx')
        except Exception as e:
            print(f'[init] occurrence unique index skipped:{e}')
            cur.execute('ROLLBACK TO SAVEPOINT receipt_occ_idx')
        cur.execute(
            '''CREATE INDEX IF NOT EXISTS idx_side_effect_due
               ON chat_generation_side_effect (status, claim_expires_at, created_at)''')
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print('[init] chat_generation_receipt 表已就绪')


def _row_to_receipt(row):
    if not row:
        return None
    response = row[3]
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except Exception:
            response = None
    return {
        'id': row[0],
        'status': row[1],
        'claim_token': row[2],
        'response_json': response,
        'claim_expires_at': row[4] if len(row) > 4 else None,
        'last_error': row[5] if len(row) > 5 else None,
    }


def get_generation(user_id, character_id, source_event_id, endpoint):
    source_event_id = str(source_event_id or '').strip()
    if not user_id or not character_id or not source_event_id or not endpoint:
        return None
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, status, claim_token, response_json,
                      claim_expires_at, last_error
               FROM chat_generation_receipt
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s''',
            (user_id, character_id, source_event_id, endpoint))
        return _row_to_receipt(cur.fetchone())
    finally:
        cur.close()
        conn.close()


def claim_generation(user_id, character_id, source_event_id, endpoint,
                     lease_seconds=DEFAULT_LEASE_SECONDS):
    """Atomic claim. Only the owner may call the LLM."""
    user_id = str(user_id or '').strip()
    character_id = str(character_id or '').strip()
    source_event_id = str(source_event_id or '').strip()
    endpoint = str(endpoint or '').strip()
    lease_seconds = max(1, int(lease_seconds or DEFAULT_LEASE_SECONDS))
    if not user_id or not character_id or not source_event_id or not endpoint:
        return {
            'owned': True,
            'status': STATUS_PROCESSING,
            'claim_token': None,
            'source_event_id': source_event_id or None,
            'skip_receipt': True,
        }
    token = str(uuid.uuid4())
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO chat_generation_receipt (
                   user_id, character_id, source_event_id, endpoint,
                   status, claim_token, claimed_at, claim_expires_at
               ) VALUES (
                   %s, %s, %s, %s,
                   'processing', %s, NOW(),
                   NOW() + (%s * INTERVAL '1 second')
               )
               ON CONFLICT (user_id, character_id, source_event_id, endpoint)
               DO UPDATE SET
                   status = 'processing',
                   claim_token = EXCLUDED.claim_token,
                   claimed_at = NOW(),
                   claim_expires_at = NOW() + (%s * INTERVAL '1 second'),
                   last_error = NULL,
                   failed_at = NULL
               WHERE chat_generation_receipt.status = 'failed'
                  OR (
                       chat_generation_receipt.status = 'processing'
                       AND chat_generation_receipt.claim_expires_at < NOW()
                  )
               RETURNING id, status, claim_token, response_json, claim_expires_at''',
            (user_id, character_id, source_event_id, endpoint,
             token, lease_seconds, lease_seconds))
        owned = cur.fetchone()
        conn.commit()
        if owned:
            return {
                'owned': True,
                'status': STATUS_PROCESSING,
                'claim_token': token,
                'source_event_id': source_event_id,
                'receipt': _row_to_receipt(owned),
            }
        current = None
        cur.execute(
            '''SELECT id, status, claim_token, response_json,
                      claim_expires_at, last_error
               FROM chat_generation_receipt
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s''',
            (user_id, character_id, source_event_id, endpoint))
        current = _row_to_receipt(cur.fetchone())
        return {
            'owned': False,
            'status': (current or {}).get('status') or STATUS_PROCESSING,
            'claim_token': None,
            'source_event_id': source_event_id,
            'receipt': current,
            'response_json': (current or {}).get('response_json'),
        }
    finally:
        cur.close()
        conn.close()


def renew_generation_lease(user_id, character_id, source_event_id, endpoint,
                           claim_token, lease_seconds=DEFAULT_LEASE_SECONDS):
    """Extend a processing generation lease. Token mismatch cannot renew."""
    if not claim_token:
        return False
    lease_seconds = max(1, int(lease_seconds or DEFAULT_LEASE_SECONDS))
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE chat_generation_receipt
               SET claim_expires_at = NOW() + (%s * INTERVAL '1 second')
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s
                 AND claim_token=%s
                 AND status='processing'
               RETURNING claim_expires_at''',
            (lease_seconds, user_id, character_id, source_event_id, endpoint,
             claim_token))
        row = cur.fetchone()
        conn.commit()
        return bool(row)
    finally:
        cur.close()
        conn.close()


class GenerationHeartbeat:
    """Background lease renewer for the generation owner. Stops on complete/fail/crash."""

    _live = []

    def __init__(self, user_id, character_id, source_event_id, endpoint, claim_token,
                 interval_seconds=None, lease_seconds=None):
        self.user_id = user_id
        self.character_id = character_id
        self.source_event_id = source_event_id
        self.endpoint = endpoint
        self.claim_token = claim_token
        self.interval_seconds = max(
            0.05, float(interval_seconds or DEFAULT_HEARTBEAT_INTERVAL_SECONDS))
        self.lease_seconds = lease_seconds or DEFAULT_LEASE_SECONDS
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if not self.claim_token or (self._thread and self._thread.is_alive()):
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name='generation-heartbeat', daemon=True)
        self._thread.start()
        GenerationHeartbeat._live.append(self)
        return self

    def _loop(self):
        while not self._stop.wait(self.interval_seconds):
            try:
                if not renew_generation_lease(
                        self.user_id, self.character_id, self.source_event_id,
                        self.endpoint, self.claim_token,
                        lease_seconds=self.lease_seconds):
                    return
            except Exception as e:
                print(f'[generation_receipt] heartbeat skipped:{e}')

    def stop(self):
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        try:
            GenerationHeartbeat._live.remove(self)
        except ValueError:
            pass

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        self.stop()
        return False


def stop_all_generation_heartbeats():
    for hb in list(GenerationHeartbeat._live):
        try:
            hb.stop()
        except Exception:
            pass


def _insert_pending_effects(cur, user_id, character_id, source_event_id,
                            endpoint, effects, payloads=None):
    payloads = payloads or {}
    for effect in effects or []:
        effect = str(effect or '').strip()
        if not effect:
            continue
        payload = payloads.get(effect)
        encoded = None
        if payload is not None:
            encoded = json.dumps(payload, ensure_ascii=False, default=str)
        cur.execute(
            '''INSERT INTO chat_generation_side_effect (
                   user_id, character_id, source_event_id, endpoint, effect,
                   status, payload_json
               ) VALUES (%s, %s, %s, %s, %s, 'pending', %s::jsonb)
               ON CONFLICT (user_id, character_id, source_event_id, endpoint, effect)
               DO UPDATE SET
                   payload_json = COALESCE(
                       chat_generation_side_effect.payload_json,
                       EXCLUDED.payload_json)''',
            (user_id, character_id, source_event_id, endpoint, effect, encoded))


def complete_generation(user_id, character_id, source_event_id, endpoint,
                        claim_token, response_json, effects=None,
                        effect_payloads=None):
    if CRASH_BEFORE_COMPLETE:
        raise RuntimeError('injected crash before complete_generation')
    if not claim_token:
        return False
    payload = canonical_payload(response_json)
    if effects is None:
        effects = needed_effects(endpoint, payload)
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE chat_generation_receipt
               SET status='completed',
                   response_json=%s::jsonb,
                   completed_at=NOW(),
                   last_error=NULL,
                   failed_at=NULL
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s
                 AND claim_token=%s
                 AND status='processing' ''',
            (encoded, user_id, character_id, source_event_id, endpoint, claim_token))
        n = cur.rowcount
        if n > 0:
            _insert_pending_effects(
                cur, user_id, character_id, source_event_id, endpoint, effects,
                payloads=effect_payloads)
        conn.commit()
        return n > 0
    finally:
        cur.close()
        conn.close()


def fail_generation(user_id, character_id, source_event_id, endpoint,
                    claim_token, last_error=None):
    if not claim_token:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE chat_generation_receipt
               SET status='failed',
                   failed_at=NOW(),
                   last_error=%s
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s
                 AND claim_token=%s
                 AND status='processing' ''',
            ((last_error or '')[:500], user_id, character_id,
             source_event_id, endpoint, claim_token))
        n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        cur.close()
        conn.close()


def release_generation(user_id, character_id, source_event_id, endpoint,
                       claim_token):
    """Drop a processing claim without completing (busy / early exit)."""
    if not claim_token:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''DELETE FROM chat_generation_receipt
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s
                 AND claim_token=%s
                 AND status='processing' ''',
            (user_id, character_id, source_event_id, endpoint, claim_token))
        n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        cur.close()
        conn.close()


def wait_for_completed(user_id, character_id, source_event_id, endpoint,
                       timeout_seconds=DEFAULT_WAIT_SECONDS,
                       poll_seconds=DEFAULT_POLL_SECONDS):
    deadline = time.monotonic() + max(0.2, float(timeout_seconds or 0))
    pause = max(0.05, float(poll_seconds or DEFAULT_POLL_SECONDS))
    last = None
    while time.monotonic() < deadline:
        last = get_generation(user_id, character_id, source_event_id, endpoint)
        status = (last or {}).get('status')
        if status == STATUS_COMPLETED:
            return last
        if status in (STATUS_FAILED, None):
            return last
        time.sleep(pause)
    return last


def _decode_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def list_side_effects(user_id, character_id, source_event_id, endpoint):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT effect, status, claim_token, last_error, attempt_count,
                      payload_json, result_json, claim_expires_at
               FROM chat_generation_side_effect
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s''',
            (user_id, character_id, source_event_id, endpoint))
        rows = cur.fetchall() or []
        return [
            {
                'effect': row[0],
                'status': row[1],
                'claim_token': row[2],
                'last_error': row[3],
                'attempt_count': row[4],
                'payload_json': _decode_json(row[5]) if len(row) > 5 else None,
                'result_json': _decode_json(row[6]) if len(row) > 6 else None,
                'claim_expires_at': row[7] if len(row) > 7 else None,
                'user_id': user_id,
                'character_id': character_id,
                'source_event_id': source_event_id,
                'endpoint': endpoint,
            }
            for row in rows
        ]
    finally:
        cur.close()
        conn.close()


def list_due_side_effects(limit=8, max_attempts=MAX_EFFECT_ATTEMPTS):
    limit = max(1, int(limit or 8))
    max_attempts = max(1, int(max_attempts or MAX_EFFECT_ATTEMPTS))
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT user_id, character_id, source_event_id, endpoint, effect,
                      payload_json, result_json, status, attempt_count
               FROM chat_generation_side_effect
               WHERE status = 'pending'
                  OR (
                       status = 'processing'
                       AND claim_expires_at < NOW()
                  )
                  OR (
                       status = 'failed'
                       AND attempt_count < %s
                       AND (
                           claim_expires_at IS NULL
                           OR claim_expires_at < NOW()
                       )
                  )
               ORDER BY created_at ASC
               LIMIT %s''',
            (max_attempts, limit))
        rows = cur.fetchall() or []
        return [
            {
                'user_id': row[0],
                'character_id': row[1],
                'source_event_id': row[2],
                'endpoint': row[3],
                'effect': row[4],
                'payload_json': _decode_json(row[5]),
                'result_json': _decode_json(row[6]),
                'status': row[7],
                'attempt_count': row[8],
            }
            for row in rows
        ]
    finally:
        cur.close()
        conn.close()


def ensure_side_effect_row(user_id, character_id, source_event_id, endpoint, effect,
                           payload=None):
    conn = get_conn()
    cur = conn.cursor()
    try:
        payloads = {effect: payload} if payload is not None else None
        _insert_pending_effects(
            cur, user_id, character_id, source_event_id, endpoint, [effect],
            payloads=payloads)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def claim_side_effect(user_id, character_id, source_event_id, endpoint, effect,
                      lease_seconds=DEFAULT_EFFECT_LEASE_SECONDS):
    """Atomic claim of a durable side-effect row. Completed rows are never re-owned."""
    effect = str(effect or '').strip()
    if not user_id or not character_id or not source_event_id or not endpoint or not effect:
        return {'owned': False, 'status': None}
    token = str(uuid.uuid4())
    lease_seconds = max(15, int(lease_seconds or DEFAULT_EFFECT_LEASE_SECONDS))
    conn = get_conn()
    cur = conn.cursor()
    try:
        _insert_pending_effects(
            cur, user_id, character_id, source_event_id, endpoint, [effect])
        cur.execute(
            '''UPDATE chat_generation_side_effect
               SET status='processing',
                   claim_token=%s,
                   claimed_at=NOW(),
                   claim_expires_at=NOW() + (%s * INTERVAL '1 second'),
                   attempt_count=attempt_count + 1,
                   last_error=NULL
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s AND effect=%s
                 AND (
                     status = 'pending'
                     OR (
                         status = 'failed'
                         AND (
                             claim_expires_at IS NULL
                             OR claim_expires_at < NOW()
                         )
                     )
                     OR (
                         status='processing'
                         AND claim_expires_at < NOW()
                     )
                 )
               RETURNING effect, status''',
            (token, lease_seconds, user_id, character_id,
             source_event_id, endpoint, effect))
        owned = cur.fetchone()
        conn.commit()
        if owned:
            return {
                'owned': True,
                'status': STATUS_PROCESSING,
                'claim_token': token,
                'effect': effect,
            }
        cur.execute(
            '''SELECT effect, status FROM chat_generation_side_effect
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s AND effect=%s''',
            (user_id, character_id, source_event_id, endpoint, effect))
        current = cur.fetchone()
        return {
            'owned': False,
            'status': current[1] if current else None,
            'effect': effect,
        }
    finally:
        cur.close()
        conn.close()


def complete_side_effect(user_id, character_id, source_event_id, endpoint,
                         effect, claim_token, result_json=None):
    if not claim_token:
        return False
    encoded = json.dumps(result_json or {}, ensure_ascii=False, default=str)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE chat_generation_side_effect
               SET status='completed',
                   completed_at=NOW(),
                   last_error=NULL,
                   result_json=%s::jsonb
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s AND effect=%s
                 AND claim_token=%s
                 AND status='processing' ''',
            (encoded, user_id, character_id, source_event_id, endpoint, effect,
             claim_token))
        n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        cur.close()
        conn.close()


def fail_side_effect(user_id, character_id, source_event_id, endpoint,
                     effect, claim_token, last_error=None):
    if not claim_token:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE chat_generation_side_effect
               SET status='failed',
                   last_error=%s,
                   claim_expires_at = NOW() + (
                       LEAST(30, POWER(2, GREATEST(attempt_count, 1)))
                       * INTERVAL '1 second'
                   )
               WHERE user_id=%s AND character_id=%s
                 AND source_event_id=%s AND endpoint=%s AND effect=%s
                 AND claim_token=%s
                 AND status='processing' ''',
            ((last_error or '')[:500], user_id, character_id,
             source_event_id, endpoint, effect, claim_token))
        n = cur.rowcount
        conn.commit()
        return n > 0
    finally:
        cur.close()
        conn.close()


def ensure_completed_generation_effects(user_id, character_id, source_event_id,
                                        endpoint, ctx=None, apply_fn=None):
    """Repair/run durable effects for a completed receipt. Never calls the LLM."""
    rec = get_generation(user_id, character_id, source_event_id, endpoint)
    if not rec or rec.get('status') != STATUS_COMPLETED:
        return []
    payload = rec.get('response_json') or {}
    ctx = dict(ctx or {})
    ctx.setdefault('user_id', user_id)
    ctx.setdefault('character_id', character_id)
    ctx.setdefault('source_event_id', source_event_id)
    ctx.setdefault('endpoint', endpoint)
    ctx.setdefault('payload', payload)
    required = needed_effects(endpoint, payload, ctx)
    ran = []
    for effect in required:
        ensure_side_effect_row(
            user_id, character_id, source_event_id, endpoint, effect)
        if CRASH_BEFORE_EFFECT == effect:
            raise RuntimeError(f'injected crash before {effect}')
        claim = claim_side_effect(
            user_id, character_id, source_event_id, endpoint, effect)
        if not claim.get('owned'):
            continue
        rows = {
            row['effect']: row
            for row in list_side_effects(
                user_id, character_id, source_event_id, endpoint)
        }
        ctx_one = dict(ctx)
        ctx_one['effect_payload'] = (rows.get(effect) or {}).get('payload_json')
        try:
            result = {}
            if apply_fn is not None:
                result = apply_fn(effect, ctx_one) or {}
            else:
                from generation_effects import apply_effect
                result = apply_effect(effect, ctx_one) or {}
            if CRASH_AFTER_EFFECT == effect:
                raise RuntimeError(f'injected crash after {effect}')
            complete_side_effect(
                user_id, character_id, source_event_id, endpoint, effect,
                claim.get('claim_token'), result_json=result)
            ran.append(effect)
        except Exception as e:
            injected_after = str(e).startswith('injected crash after')
            injected_before = str(e).startswith('injected crash before')
            if injected_after or injected_before:
                raise
            fail_side_effect(
                user_id, character_id, source_event_id, endpoint, effect,
                claim.get('claim_token'), last_error=str(e))
            print(f'[{user_id}] side-effect {effect} failed:{e}')
    return ran


def after_generation_commit(user_id, character_id, source_event_id, endpoint,
                            ctx=None, skip=None):
    """Best-effort kick after receipt complete. Durability does not depend on this."""
    try:
        from generation_side_effect_worker import (
            notify_effects_pending, process_due_for_receipt,
        )
        notify_effects_pending()
        return process_due_for_receipt(
            user_id, character_id, source_event_id, endpoint,
            ctx=ctx, skip=skip, limit=8)
    except Exception as e:
        if str(e).startswith('injected crash'):
            raise
        print(f'[generation_receipt] after_commit kick skipped:{e}')
        return []


def in_progress_body(source_event_id):
    return {
        'generation_in_progress': True,
        'source_event_id': source_event_id,
        'retryable': True,
    }


def resolve_generation(user_id, character_id, source_event_id, endpoint,
                       wait_seconds=None, poll_seconds=None, lease_seconds=None):
    wait_seconds = DEFAULT_WAIT_SECONDS if wait_seconds is None else wait_seconds
    poll_seconds = DEFAULT_POLL_SECONDS if poll_seconds is None else poll_seconds
    lease_seconds = DEFAULT_LEASE_SECONDS if lease_seconds is None else lease_seconds
    source_event_id = str(source_event_id or '').strip()
    if not source_event_id:
        source_event_id, _legacy = assign_source_event_id(source_event_id)
    claim = claim_generation(
        user_id, character_id, source_event_id, endpoint,
        lease_seconds=lease_seconds)
    if claim.get('owned'):
        return {
            'action': 'generate',
            'claim_token': claim.get('claim_token'),
            'source_event_id': source_event_id,
        }
    if claim.get('status') == STATUS_COMPLETED:
        return {
            'action': 'replay',
            'response': claim.get('response_json') or {},
            'source_event_id': source_event_id,
        }
    waited = wait_for_completed(
        user_id, character_id, source_event_id, endpoint,
        timeout_seconds=wait_seconds, poll_seconds=poll_seconds)
    waited_status = (waited or {}).get('status')
    if waited_status == STATUS_COMPLETED:
        return {
            'action': 'replay',
            'response': (waited or {}).get('response_json') or {},
            'source_event_id': source_event_id,
        }
    if waited_status == STATUS_FAILED:
        retry = claim_generation(
            user_id, character_id, source_event_id, endpoint,
            lease_seconds=lease_seconds)
        if retry.get('owned'):
            return {
                'action': 'generate',
                'claim_token': retry.get('claim_token'),
                'source_event_id': source_event_id,
            }
    return {
        'action': 'in_progress',
        'source_event_id': source_event_id,
    }


def merge_effect_result(body, effect, result):
    """Fold a completed side-effect result into a live response body."""
    body = body if isinstance(body, dict) else {}
    result = result or {}
    if not isinstance(result, dict):
        return body
    if effect == 'reminder':
        rem = dict(body.get('reminder') or {})
        if result.get('task_id') is not None:
            rem['task_id'] = result.get('task_id')
        if 'duplicate' in result:
            rem['duplicate'] = result.get('duplicate')
        if rem:
            body['reminder'] = rem
    elif effect == 'proactive_promise':
        saved = {
            'id': result.get('promise_id') if result.get('promise_id') is not None
            else result.get('id'),
            'kind': result.get('kind'),
            'trigger_at': result.get('trigger_at'),
            'trigger_time': result.get('trigger_time'),
            'context': result.get('context'),
        }
        body['saved_promise'] = {
            key: value for key, value in saved.items() if value is not None
        }
    elif effect == 'cancel_reminder':
        body['cancelled_tasks'] = result.get('cancelled_tasks') or []
    return body


def hydrate_completed_generation_response(
        user_id, character_id, source_event_id, endpoint, payload=None):
    """receipt.response_json + completed effect.result_json + media URL + TTS."""
    if payload is None:
        rec = get_generation(user_id, character_id, source_event_id, endpoint)
        body = copy.deepcopy((rec or {}).get('response_json') or {})
    else:
        body = copy.deepcopy(payload)
    try:
        for row in list_side_effects(
                user_id, character_id, source_event_id, endpoint):
            if row.get('status') != STATUS_COMPLETED:
                continue
            merge_effect_result(body, row.get('effect'), row.get('result_json'))
    except Exception as e:
        print(f'[generation_receipt] effect result merge skipped:{e}')
    return hydrate_replay(
        body,
        character_id=character_id,
        user_id=user_id,
        chat_id=character_id,
        source_event_id=source_event_id,
    )


def hydrate_replay(body, character_id, user_id=None, chat_id=None,
                   source_event_id=None):
    """Rebuild a live response from a stored canonical payload. TTS only, no LLM."""
    payload = copy.deepcopy(body or {})
    for key in list(payload.keys()):
        if str(key).startswith('_'):
            payload.pop(key, None)
    emotion = payload.get('emotion') or '平静'
    msgs = list(payload.get('messages') or [])
    try:
        from characters import get_character
        from tts import tts_to_b64
        char = get_character(character_id) or {}
        voice_id = char.get('voice_id')
        for item in msgs:
            jp = item.get('jp') or ''
            if jp:
                item['audio_b64'] = tts_to_b64(jp, emotion, voice_id)
    except Exception as e:
        print(f'[generation_receipt] replay TTS skipped:{e}')
    payload['messages'] = msgs
    media = payload.get('media')
    event_id = source_event_id or payload.get('source_event_id')
    if user_id and (chat_id or character_id) and event_id:
        try:
            from db_chat_media import get_media_for_source_event, public_media
            rec = get_media_for_source_event(
                user_id, chat_id or character_id, event_id)
            if rec:
                payload['media'] = public_media(rec)
            elif isinstance(media, dict):
                media = dict(media)
                media.pop('url', None)
                payload['media'] = media
        except Exception as e:
            print(f'[generation_receipt] replay media skipped:{e}')
    return payload
