"""Canonical Raw Event layer (L0).

Source of truth is the existing `chat_log` table, not a second ledger.
short_memory is a recent-view / compatibility cache and must not be treated
as an independent fact database.

This module currently holds several L0 concerns in one file on purpose:
canonical CRUD, annotations, processor state, provenance links, derived
idempotency, deletion invalidation, and keyed short_memory backfill.
Do not split unless a concrete maintainability failure appears; function
correctness outranks file hygiene.

Technical dedup only (same event_id / client_msg_id).
Semantic merge belongs to episodic / derived memory, not this module.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from assistant_turn import collapse_assistant_logical_turns, infer_assistant_identity
from db import get_conn

PROCESSOR_MEMORY_EXTRACTOR = 'memory_extractor'
PROCESSOR_MEMORY_EXTRACTOR_VERSION = 'v1'
PROCESSOR_VISION = 'vision_summary'
PROCESSOR_VISION_VERSION = 'v1'
PROCESSOR_RELATIONSHIP = 'relationship_v4'
PROCESSOR_RELATIONSHIP_VERSION = 'v1'

RAW_EVENT_DDL = (
    '''ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS event_id TEXT''',
    '''ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active' ''',
    '''ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ''',
    '''ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS reply_to_event_id TEXT''',
    '''UPDATE chat_log SET event_id = client_msg_id
       WHERE (event_id IS NULL OR event_id = '')
         AND client_msg_id IS NOT NULL AND client_msg_id <> '' ''',
    '''UPDATE chat_log SET status = 'active' WHERE status IS NULL OR status = '' ''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS idx_chatlog_event_id
       ON chat_log (user_id, chat_id, event_id)
       WHERE event_id IS NOT NULL AND event_id <> '' ''',
    '''CREATE INDEX IF NOT EXISTS idx_chatlog_active_time
       ON chat_log (user_id, chat_id, created_at DESC)
       WHERE COALESCE(status, 'active') = 'active' ''',
    '''CREATE TABLE IF NOT EXISTS event_annotations (
        event_id TEXT NOT NULL,
        annotation_type TEXT NOT NULL,
        processor_version TEXT NOT NULL,
        payload TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (event_id, annotation_type, processor_version)
    )''',
    '''CREATE TABLE IF NOT EXISTS event_processing_state (
        event_id TEXT NOT NULL,
        processor_type TEXT NOT NULL,
        processor_version TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        result_ref TEXT,
        retry_count INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        consumed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (event_id, processor_type, processor_version)
    )''',
    '''CREATE TABLE IF NOT EXISTS memory_source_events (
        id BIGSERIAL PRIMARY KEY,
        memory_type TEXT NOT NULL,
        memory_id INTEGER NOT NULL,
        source_event_id TEXT NOT NULL,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (memory_type, memory_id, source_event_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_memory_source_event
       ON memory_source_events (source_event_id)''',
    '''CREATE TABLE IF NOT EXISTS derived_memory_idempotency (
        processor_type TEXT NOT NULL,
        processor_version TEXT NOT NULL,
        source_key TEXT NOT NULL,
        memory_type TEXT,
        memory_id INTEGER,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (processor_type, processor_version, source_key)
    )''',
    '''ALTER TABLE memory_lifecycle_items
       ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ''',
    '''ALTER TABLE memory_lifecycle_items
       ADD COLUMN IF NOT EXISTS salience REAL DEFAULT 0.5''',
    '''ALTER TABLE memory_lifecycle_items
       ADD COLUMN IF NOT EXISTS strength REAL DEFAULT 0.5''',
    '''ALTER TABLE memory_lifecycle_items
       ADD COLUMN IF NOT EXISTS decay_state TEXT DEFAULT 'active' ''',
    '''ALTER TABLE bond_memory
       ADD COLUMN IF NOT EXISTS recall_status TEXT DEFAULT 'active' ''',
)


def init_raw_event_layer():
    conn = get_conn()
    cur = conn.cursor()
    try:
        for ddl in RAW_EVENT_DDL:
            cur.execute(ddl)
        conn.commit()
        backfill_raw_events_from_short_memory(conn)
        conn.commit()
        print('[raw_events] chat_log ledger / provenance tables ready')
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def _borrow_conn(conn=None):
    if conn is not None:
        return conn, False
    return get_conn(), True


def _normalize_event_id(value):
    text = str(value).strip() if value else ''
    return text or None


def _chat_role(role):
    role = (role or 'user').strip()
    if role in ('assistant', 'gojo'):
        return 'gojo'
    return 'user'


def _prompt_role(role):
    return 'user' if (role or '') == 'user' else 'assistant'


def _merge_extra(existing, extra):
    data = {}
    if existing:
        try:
            parsed = json.loads(existing)
            if isinstance(parsed, dict):
                data.update(parsed)
        except Exception:
            pass
    if extra:
        data.update(extra)
    if not data:
        return ''
    return json.dumps(data, ensure_ascii=False)[:6000]


def derivation_source_key(source_event_ids):
    ids = sorted({
        str(item).strip() for item in (source_event_ids or [])
        if str(item).strip()
    })
    if not ids:
        return None
    if len(ids) == 1:
        return ids[0]
    return hashlib.sha256('|'.join(ids).encode('utf-8')).hexdigest()


def append_raw_event(
    user_id,
    character_id,
    *,
    event_id=None,
    role='user',
    content='',
    content_type='text',
    metadata=None,
    reply_to_event_id=None,
    occurred_at=None,
    subtitle='',
    emotion='',
    has_audio=False,
):
    """Insert one canonical raw event. Technical retry with the same event_id is a no-op.

    Never overwrites raw_content with a vision summary.
    Never semantic-dedups different event_ids.
    """
    user_id = (user_id or '').strip()
    character_id = (character_id or '').strip()
    if not user_id or not character_id:
        return {'inserted': False, 'event_id': None, 'reason': 'missing_identity'}

    client_msg_id = _normalize_event_id(event_id)
    extra = dict(metadata or {})
    extra = infer_assistant_identity(role, client_msg_id, extra)
    if reply_to_event_id:
        extra.setdefault('reply_to_event_id', reply_to_event_id)
    extra.setdefault('content_type', content_type)
    kind = content_type if content_type in ('text', 'image', 'video', 'call_log', 'system') else 'text'
    if content_type == 'voice':
        kind = 'text'
        extra['channel'] = 'voice_stream'
    extra_text = json.dumps(extra, ensure_ascii=False)[:6000] if extra else ''

    msg = {
        'client_msg_id': client_msg_id or '',
        'role': _chat_role(role),
        'text': (content or '')[:4000],
        'subtitle': (subtitle or '')[:4000],
        'emotion': (emotion or '')[:20],
        'kind': kind[:20],
        'extra': extra_text,
        'has_audio': bool(has_audio),
        'reply_to_event_id': _normalize_event_id(reply_to_event_id) or '',
    }
    if occurred_at:
        if hasattr(occurred_at, 'isoformat'):
            msg['ts'] = occurred_at.isoformat()
        else:
            msg['ts'] = str(occurred_at)
    from db_chatlog import append_messages
    written = append_messages(user_id, character_id, [msg])
    return {
        'inserted': bool(written),
        'event_id': client_msg_id,
        'reason': None if written else 'duplicate_or_deleted',
    }


class SourceValidityError(RuntimeError):
    """Source Raw Event activity could not be confirmed. unknown != active."""


def is_raw_event_deleted(event_id, user_id=None, character_id=None, conn=None):
    event_id = _normalize_event_id(event_id)
    if not event_id:
        return False
    database, owned = _borrow_conn(conn)
    cur = database.cursor()
    try:
        if user_id and character_id:
            cur.execute(
                '''SELECT 1 FROM chat_log_tombstone
                   WHERE user_id=%s AND chat_id=%s AND client_msg_id=%s''',
                (user_id, character_id, event_id))
            if cur.fetchone():
                return True
            cur.execute(
                '''SELECT 1 FROM chat_log
                   WHERE user_id=%s AND chat_id=%s
                     AND (event_id=%s OR client_msg_id=%s)
                     AND COALESCE(status, 'active') = 'deleted'
                   LIMIT 1''',
                (user_id, character_id, event_id, event_id))
            return bool(cur.fetchone())
        cur.execute(
            '''SELECT 1 FROM chat_log_tombstone WHERE client_msg_id=%s LIMIT 1''',
            (event_id,))
        if cur.fetchone():
            return True
        cur.execute(
            '''SELECT 1 FROM chat_log
               WHERE (event_id=%s OR client_msg_id=%s)
                 AND COALESCE(status, 'active') = 'deleted'
               LIMIT 1''',
            (event_id, event_id))
        return bool(cur.fetchone())
    except SourceValidityError:
        raise
    except Exception as e:
        raise SourceValidityError(str(e) or 'source validity check failed') from e
    finally:
        cur.close()
        if owned:
            database.close()


def sources_are_active(event_ids, user_id=None, character_id=None, conn=None):
    """True if every required source is active.

    False = at least one source is deleted / tombstoned.
    Raises SourceValidityError if the check cannot be completed.
    Empty event_ids means no required source (True).
    """
    ids = [
        _normalize_event_id(item) for item in (event_ids or [])
        if _normalize_event_id(item)
    ]
    if not ids:
        return True
    for event_id in ids:
        if is_raw_event_deleted(event_id, user_id, character_id, conn=conn):
            return False
    return True


def get_active_events_by_ids(user_id, character_id, event_ids, conn=None):
    """Return active canonical chat_log events for the requested ids.

    This is a provenance read, not a second ledger. Derived-memory callers use
    it to prefer the source committed to chat_log over copied request payloads.
    """
    ids = []
    for item in event_ids or []:
        event_id = _normalize_event_id(item)
        if event_id and event_id not in ids:
            ids.append(event_id)
    if not ids:
        return []

    database, owned = _borrow_conn(conn)
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT COALESCE(NULLIF(event_id, ''), client_msg_id), role, text, kind, extra,
                      created_at, subtitle
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(status, 'active') = 'active'
                 AND COALESCE(NULLIF(event_id, ''), client_msg_id) = ANY(%s)''',
            (user_id, character_id, ids),
        )
        rows = cur.fetchall()
    except SourceValidityError:
        raise
    except Exception as e:
        if owned:
            try:
                database.rollback()
            except Exception:
                pass
        raise SourceValidityError(str(e) or 'canonical source lookup failed') from e
    finally:
        cur.close()
        if owned:
            database.close()

    by_id = {}
    for event_id, role, text, kind, extra, ts, subtitle in rows:
        meta = {}
        if extra:
            try:
                parsed = json.loads(extra)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                pass
        by_id[event_id] = {
            'event_id': event_id or '',
            'role': _prompt_role(role),
            'content': text or '',
            'kind': kind or 'text',
            'metadata': meta,
            'timestamp': ts,
            'subtitle': subtitle or '',
        }
    return [by_id[event_id] for event_id in ids if event_id in by_id]


def any_source_inactive(event_ids, user_id=None, character_id=None, conn=None):
    """True if any required Raw Event is missing-as-deleted / tombstoned.

    Raises SourceValidityError when activity cannot be confirmed.
    """
    return not sources_are_active(
        event_ids, user_id, character_id, conn=conn)

_MAX_PREVIOUS_USER_EVIDENCE_EVENTS = 2
_MAX_PREVIOUS_USER_EVIDENCE_HOURS = 24


def get_previous_active_user_events(user_id, character_id, event_id, n=2,
                                    hours=24, conn=None):
    """Return the bounded canonical user evidence immediately before one event.

    This is deliberately scoped to the same existing ``(user_id, chat_id)``
    ledger partition. The primary event is the temporal anchor: prior rows
    must be active user rows whose ``(created_at, id)`` sorts strictly before
    it, within the bounded evidence window. No session representation is
    introduced here because chat_log has no session/thread column.
    """
    event_id = _normalize_event_id(event_id)
    if not event_id:
        return []
    n = max(1, min(int(n or _MAX_PREVIOUS_USER_EVIDENCE_EVENTS),
                   _MAX_PREVIOUS_USER_EVIDENCE_EVENTS))
    hours = max(1, min(int(hours or _MAX_PREVIOUS_USER_EVIDENCE_HOURS),
                       _MAX_PREVIOUS_USER_EVIDENCE_HOURS))

    database, owned = _borrow_conn(conn)
    cur = database.cursor()
    try:
        cur.execute(
            '''WITH anchor AS (
                   SELECT id, created_at
                   FROM chat_log
                   WHERE user_id=%s AND chat_id=%s
                     AND COALESCE(status, 'active') = 'active'
                     AND role='user'
                     AND COALESCE(NULLIF(event_id, ''), client_msg_id)=%s
                   ORDER BY created_at DESC, id DESC
                   LIMIT 1
               )
               SELECT COALESCE(NULLIF(prior.event_id, ''), prior.client_msg_id),
                      prior.role, prior.text, prior.kind, prior.extra,
                      prior.created_at, prior.subtitle
               FROM chat_log AS prior
               CROSS JOIN anchor
               WHERE prior.user_id=%s AND prior.chat_id=%s
                 AND COALESCE(prior.status, 'active') = 'active'
                 AND prior.role='user'
                 AND prior.created_at >= anchor.created_at
                     - (%s * INTERVAL '1 hour')
                 AND (prior.created_at, prior.id) < (anchor.created_at, anchor.id)
               ORDER BY prior.created_at DESC, prior.id DESC
               LIMIT %s''',
            (user_id, character_id, event_id, user_id, character_id, hours, n),
        )
        rows = cur.fetchall()
    except SourceValidityError:
        raise
    except Exception as e:
        if owned:
            try:
                database.rollback()
            except Exception:
                pass
        raise SourceValidityError(
            str(e) or 'previous canonical evidence lookup failed') from e
    finally:
        cur.close()
        if owned:
            database.close()

    out = []
    for prior_id, role, text, kind, extra, ts, subtitle in reversed(rows):
        meta = {}
        if extra:
            try:
                parsed = json.loads(extra)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                pass
        out.append({
            'event_id': prior_id or '',
            'role': _prompt_role(role),
            'content': text or '',
            'kind': kind or 'text',
            'metadata': meta,
            'timestamp': ts,
            'subtitle': subtitle or '',
        })
    return out



def deleted_event_ids(user_id, character_id):
    """Return tombstoned / deleted Raw Event ids.

    Raises SourceValidityError if the lookup cannot be completed.
    unknown != active: callers must not treat a failed lookup as "nothing deleted".
    """
    conn = None
    cur = None
    ids = set()
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT client_msg_id FROM chat_log_tombstone
               WHERE user_id=%s AND chat_id=%s''',
            (user_id, character_id))
        ids.update(row[0] for row in cur.fetchall() if row[0])
        cur.execute(
            '''SELECT COALESCE(event_id, client_msg_id)
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(status, 'active') = 'deleted' ''',
            (user_id, character_id))
        ids.update(row[0] for row in cur.fetchall() if row[0])
        return ids
    except SourceValidityError:
        raise
    except Exception as e:
        raise SourceValidityError(str(e) or 'deleted-event lookup failed') from e
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def get_recent_events(user_id, character_id, n=40, hours=24):
    """Active raw events for recent context. Deleted events are excluded.

    Raises SourceValidityError if the active-set query cannot be completed.
    """
    n = max(1, min(int(n or 40), 40))
    hours = max(1, int(hours or 24))
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT COALESCE(event_id, client_msg_id), role, text, kind, extra,
                      created_at, subtitle
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(status, 'active') = 'active'
                 AND created_at >= NOW() - (%s * INTERVAL '1 hour')
               ORDER BY created_at DESC, id DESC
               LIMIT %s''',
            (user_id, character_id, hours, n))
        rows = cur.fetchall()
    except SourceValidityError:
        raise
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise SourceValidityError(str(e) or 'recent-event lookup failed') from e
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()
    out = []
    for event_id, role, text, kind, extra, ts, subtitle in reversed(rows):
        meta = {}
        if extra:
            try:
                parsed = json.loads(extra)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                meta = {}
        out.append({
            'event_id': event_id or '',
            'role': _prompt_role(role),
            'content': text or '',
            'kind': kind or 'text',
            'metadata': meta,
            'timestamp': ts,
            'subtitle': subtitle or '',
        })
    return collapse_assistant_logical_turns(
        out, turn_facts=_assistant_turn_facts(user_id, character_id))


def _assistant_turn_facts(user_id, character_id):
    try:
        import db_chatlog
        return db_chatlog.assistant_turn_facts(user_id, character_id)
    except Exception:
        return {}


def list_events_for_local_day(user_id, character_id, day_start, *, limit=80):
    """Active chat_log events for one local calendar day. Newest last."""
    from datetime import timedelta

    if not user_id or not character_id or day_start is None:
        return []
    limit = max(1, min(int(limit or 80), 120))
    day_end = day_start + timedelta(days=1)
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT COALESCE(event_id, client_msg_id), role, text, kind, extra,
                      created_at, subtitle
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(status, 'active') = 'active'
                 AND created_at >= %s AND created_at < %s
               ORDER BY created_at ASC, id ASC
               LIMIT %s''',
            (user_id, character_id, day_start, day_end, limit))
        rows = cur.fetchall()
    except Exception as e:
        print(f'[raw_events] list_events_for_local_day failed:{e}')
        return []
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()
    out = []
    for event_id, role, text, kind, extra, ts, subtitle in rows:
        meta = {}
        if extra:
            try:
                parsed = json.loads(extra)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                meta = {}
        out.append({
            'event_id': event_id or '',
            'role': _prompt_role(role),
            'content': (text or '')[:240],
            'kind': kind or 'text',
            'metadata': meta,
            'timestamp': ts,
            'subtitle': subtitle or '',
        })
    return collapse_assistant_logical_turns(
        out, turn_facts=_assistant_turn_facts(user_id, character_id))


def get_hot_candidate_events(user_id, character_id, n=240, hours=72):
    """Bounded active-event fetch for adaptive hot context.

    Separate from get_recent_events so the compatibility 40-cap stays intact.
    Raises SourceValidityError if the active-set query cannot be completed.
    """
    n = max(1, min(int(n or 240), 240))
    hours = max(1, int(hours or 72))
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT COALESCE(chat_log.event_id, chat_log.client_msg_id),
                      role, text, kind, extra, created_at, subtitle,
                      COALESCE(reply_to_event_id, '')
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(status, 'active') = 'active'
                 AND created_at >= NOW() - (%s * INTERVAL '1 hour')
               ORDER BY created_at DESC, id DESC
               LIMIT %s''',
            (user_id, character_id, hours, n))
        rows = cur.fetchall()
    except SourceValidityError:
        raise
    except Exception as e:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        raise SourceValidityError(str(e) or 'hot-candidate lookup failed') from e
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()
    out = []
    for event_id, role, text, kind, extra, ts, subtitle, reply_to in reversed(rows):
        meta = {}
        if extra:
            try:
                parsed = json.loads(extra)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                meta = {}
        out.append({
            'event_id': event_id or '',
            'role': _prompt_role(role),
            'content': text or '',
            'kind': kind or 'text',
            'metadata': meta,
            'timestamp': ts,
            'subtitle': subtitle or '',
            'reply_to_event_id': reply_to or '',
        })
    return collapse_assistant_logical_turns(
        out, turn_facts=_assistant_turn_facts(user_id, character_id))


def list_events_for_assistant_turn(user_id, character_id, assistant_turn_id):
    """All active segments that share one assistant_turn_id, oldest first."""
    turn_id = str(assistant_turn_id or '').strip()
    if not user_id or not character_id or not turn_id:
        return []
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT event_id, client_msg_id, role, text, extra, created_at
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(status, 'active') = 'active'
               ORDER BY created_at ASC, id ASC''',
            (user_id, character_id))
        rows = cur.fetchall()
    except Exception:
        rows = []
    finally:
        cur.close()
        conn.close()
    out = []
    for event_id, client_msg_id, role, text, extra, ts in rows:
        meta = {}
        if extra:
            try:
                parsed = json.loads(extra)
                if isinstance(parsed, dict):
                    meta = parsed
            except Exception:
                meta = {}
        identity = infer_assistant_identity(
            role, event_id or client_msg_id, meta)
        if identity.get('assistant_turn_id') != turn_id:
            continue
        out.append({
            'event_id': event_id or client_msg_id or '',
            'assistant_turn_id': turn_id,
            'segment_index': int(identity.get('segment_index') or 0),
            'role': _prompt_role(role),
            'content': text or '',
            'metadata': identity,
            'timestamp': ts,
        })
    out.sort(key=lambda item: (item['segment_index'], str(item.get('event_id'))))
    return out


def attach_event_annotation(event_id, annotation_type, payload,
                            processor_version=PROCESSOR_VISION_VERSION):
    """Derived annotation. Same processor version is idempotent and never overwrites raw text."""
    event_id = _normalize_event_id(event_id)
    if not event_id or not payload:
        return False
    payload_text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    conn = get_conn()
    cur = conn.cursor()
    inserted = False
    try:
        cur.execute(
            '''INSERT INTO event_annotations
               (event_id, annotation_type, processor_version, payload)
               VALUES (%s, %s, %s, %s)
               ON CONFLICT (event_id, annotation_type, processor_version)
               DO NOTHING
               RETURNING event_id''',
            (event_id, annotation_type, processor_version, payload_text[:4000]))
        inserted = bool(cur.fetchone())
        if inserted and annotation_type in ('vision_summary', 'visual_summary'):
            cur.execute(
                '''SELECT extra FROM chat_log
                   WHERE event_id=%s OR client_msg_id=%s
                   ORDER BY id DESC LIMIT 1''',
                (event_id, event_id))
            row = cur.fetchone()
            extra = _merge_extra(row[0] if row else '', {'visual_summary': payload_text[:800]})
            cur.execute(
                '''UPDATE chat_log
                   SET extra=%s
                   WHERE (event_id=%s OR client_msg_id=%s)
                     AND COALESCE(status, 'active') = 'active' ''',
                (extra, event_id, event_id))
        conn.commit()
        return inserted
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def claim_processor(event_id, processor_type, processor_version):
    """Per-event processor state.

    Returns 'already_succeeded' | 'claimed' | 'claim_failed'.
    claim failed != permission to process.
    """
    event_id = _normalize_event_id(event_id)
    if not event_id:
        return 'claimed'
    conn = None
    cur = None
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            '''SELECT status FROM event_processing_state
               WHERE event_id=%s AND processor_type=%s AND processor_version=%s''',
            (event_id, processor_type, processor_version))
        row = cur.fetchone()
        if row and row[0] == 'succeeded':
            conn.commit()
            return 'already_succeeded'
        cur.execute(
            '''INSERT INTO event_processing_state
               (event_id, processor_type, processor_version, status, retry_count)
               VALUES (%s, %s, %s, 'processing', 0)
               ON CONFLICT (event_id, processor_type, processor_version)
               DO UPDATE SET
                 status = 'processing',
                 retry_count = event_processing_state.retry_count + 1,
                 updated_at = CURRENT_TIMESTAMP''',
            (event_id, processor_type, processor_version))
        conn.commit()
        return 'claimed'
    except Exception:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return 'claim_failed'
    finally:
        if cur is not None:
            cur.close()
        if conn is not None:
            conn.close()


def finish_processor(event_id, processor_type, processor_version, status,
                     result_ref=None, last_error=None, conn=None):
    event_id = _normalize_event_id(event_id)
    if not event_id:
        return
    database, owned = _borrow_conn(conn)
    cur = database.cursor()
    try:
        consumed = datetime.now(timezone.utc) if status == 'succeeded' else None
        cur.execute(
            '''INSERT INTO event_processing_state
               (event_id, processor_type, processor_version, status,
                result_ref, last_error, consumed_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (event_id, processor_type, processor_version)
               DO UPDATE SET
                 status = EXCLUDED.status,
                 result_ref = EXCLUDED.result_ref,
                 last_error = EXCLUDED.last_error,
                 consumed_at = EXCLUDED.consumed_at,
                 updated_at = CURRENT_TIMESTAMP''',
            (event_id, processor_type, processor_version, status,
             result_ref, last_error, consumed))
        if owned:
            database.commit()
    except Exception:
        if owned:
            database.rollback()
        else:
            raise
    finally:
        cur.close()
        if owned:
            database.close()


def already_derived(processor_type, processor_version, source_key):
    if not source_key:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT 1 FROM derived_memory_idempotency
               WHERE processor_type=%s AND processor_version=%s AND source_key=%s''',
            (processor_type, processor_version, source_key))
        return bool(cur.fetchone())
    except Exception:
        return False
    finally:
        cur.close()
        conn.close()


def record_derived(processor_type, processor_version, source_key,
                   memory_type=None, memory_id=None):
    if not source_key:
        return False
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO derived_memory_idempotency
               (processor_type, processor_version, source_key, memory_type, memory_id)
               VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (processor_type, processor_version, source_key)
               DO NOTHING''',
            (processor_type, processor_version, source_key, memory_type, memory_id))
        conn.commit()
        return cur.rowcount > 0
    except Exception:
        conn.rollback()
        return False
    finally:
        cur.close()
        conn.close()


def link_memory_sources(memory_type, memory_id, source_event_ids):
    ids = [
        str(item).strip() for item in (source_event_ids or [])
        if str(item).strip()
    ]
    if not memory_type or not memory_id or not ids:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    n = 0
    try:
        for event_id in ids:
            cur.execute(
                '''INSERT INTO memory_source_events
                   (memory_type, memory_id, source_event_id)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (memory_type, memory_id, source_event_id)
                   DO NOTHING''',
                (memory_type, memory_id, event_id))
            n += cur.rowcount
        conn.commit()
    except Exception:
        conn.rollback()
        n = 0
    finally:
        cur.close()
        conn.close()
    return n


def _ref_event_ids(refs):
    out = []
    if isinstance(refs, str):
        try:
            refs = json.loads(refs)
        except Exception:
            refs = []
    for item in refs or []:
        if isinstance(item, dict):
            sid = item.get('source_id') or item.get('event_id') or item.get('source_event_id')
            if sid:
                out.append(str(sid).replace('memory_job:', '').replace('raw_event:', ''))
        elif item:
            out.append(str(item))
    return out


def invalidate_memories_for_deleted_event(event_id, user_id=None, character_id=None):
    """If a derived memory loses all remaining active sources, mark it stale."""
    event_id = _normalize_event_id(event_id)
    if not event_id:
        return {'invalidated': 0, 'unlinked': 0}
    conn = get_conn()
    cur = conn.cursor()
    invalidated = 0
    unlinked = 0
    try:
        cur.execute(
            '''SELECT memory_type, memory_id FROM memory_source_events
               WHERE source_event_id=%s''',
            (event_id,))
        mapped = list(cur.fetchall())
        cur.execute(
            '''DELETE FROM memory_source_events WHERE source_event_id=%s''',
            (event_id,))
        unlinked = cur.rowcount

        for memory_type, memory_id in mapped:
            cur.execute(
                '''SELECT COUNT(*) FROM memory_source_events
                   WHERE memory_type=%s AND memory_id=%s''',
                (memory_type, memory_id))
            remaining = cur.fetchone()[0]
            if remaining:
                continue
            if memory_type == 'long_memory':
                cur.execute(
                    '''UPDATE long_memory
                       SET recall_status='deleted'
                       WHERE id=%s AND COALESCE(recall_status, 'active') = 'active' ''',
                    (memory_id,))
                invalidated += cur.rowcount
            elif memory_type == 'lifecycle':
                cur.execute(
                    '''UPDATE memory_lifecycle_items
                       SET status='archived', decay_state='dormant',
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s AND status IN ('active', 'reactivated')''',
                    (memory_id,))
                invalidated += cur.rowcount
            elif memory_type == 'bond_memory':
                cur.execute(
                    '''UPDATE bond_memory
                       SET recall_status='deleted'
                       WHERE id=%s''',
                    (memory_id,))
                invalidated += cur.rowcount

        cur.execute(
            '''SELECT id, source_event_refs FROM long_memory
               WHERE COALESCE(recall_status, 'active') = 'active' ''')
        for row_id, refs in cur.fetchall():
            ids = _ref_event_ids(refs)
            if event_id not in ids:
                continue
            leftover = [item for item in ids if item != event_id and not is_raw_event_deleted(item)]
            if leftover:
                cur.execute(
                    '''UPDATE long_memory SET source_event_refs=%s::jsonb WHERE id=%s''',
                    (json.dumps([{'source_id': item} for item in leftover], ensure_ascii=False), row_id))
            else:
                cur.execute(
                    '''UPDATE long_memory SET recall_status='deleted' WHERE id=%s''',
                    (row_id,))
                invalidated += cur.rowcount
        conn.commit()
        try:
            from context_layer import reconcile_summaries_for_deleted
            if user_id and character_id:
                reconcile_summaries_for_deleted(user_id, character_id, [event_id])
        except Exception as hook_exc:
            print(f'[raw_events] summary reconcile skipped:{hook_exc}')
        try:
            from episodic_index import reconcile_deleted_sources
            if user_id and character_id:
                reconcile_deleted_sources(user_id, character_id, [event_id])
        except Exception as hook_exc:
            print(f'[raw_events] episode reconcile skipped:{hook_exc}')
    except Exception as e:
        conn.rollback()
        print(f'[raw_events] invalidate derived failed:{e}')
    finally:
        cur.close()
        conn.close()
    return {'invalidated': invalidated, 'unlinked': unlinked}


def hard_purge_raw_event(event_id, user_id, character_id):
    """Physical delete + cascade. Not wired to UI this phase."""
    event_id = _normalize_event_id(event_id)
    if not event_id or not user_id or not character_id:
        return 0
    invalidate_memories_for_deleted_event(event_id, user_id, character_id)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''DELETE FROM event_annotations
               WHERE event_id=%s''',
            (event_id,))
        cur.execute(
            '''DELETE FROM event_processing_state
               WHERE event_id=%s''',
            (event_id,))
        cur.execute(
            '''DELETE FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND (event_id=%s OR client_msg_id=%s)''',
            (user_id, character_id, event_id, event_id))
        n = cur.rowcount
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def backfill_raw_events_from_short_memory(conn=None):
    """Idempotent copy of keyed short_memory rows into chat_log. Never deletes old rows."""
    own = conn is None
    if own:
        conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''
            INSERT INTO chat_log
                (user_id, chat_id, client_msg_id, event_id, role, text,
                 kind, extra, created_at, status)
            SELECT
                sm.user_id,
                sm.character_id,
                sm.source_event_id,
                sm.source_event_id,
                CASE WHEN sm.role = 'user' THEN 'user' ELSE 'gojo' END,
                LEFT(COALESCE(sm.content, ''), 4000),
                'text',
                LEFT(COALESCE(sm.event_meta, ''), 6000),
                COALESCE(sm.timestamp, CURRENT_TIMESTAMP),
                'active'
            FROM short_memory sm
            WHERE sm.source_event_id IS NOT NULL
              AND sm.source_event_id <> ''
              AND NOT EXISTS (
                  SELECT 1 FROM chat_log_tombstone ts
                  WHERE ts.user_id = sm.user_id
                    AND ts.chat_id = sm.character_id
                    AND ts.client_msg_id = sm.source_event_id
              )
            ON CONFLICT DO NOTHING
            '''
        )
        keyed = cur.rowcount
        # Unkeyed short_memory rows have no stable event_id. Inventing sm-{id}
        # would create a second fact next to frontend chat_log bubbles.
        # Leave them in the compatibility cache until they can be keyed.
        if own:
            conn.commit()
        if keyed:
            print(f'[raw_events] backfill chat_log from short_memory keyed={keyed}')
        return keyed
    except Exception as e:
        if own:
            conn.rollback()
        print(f'[raw_events] backfill skipped:{e}')
        return 0
    finally:
        cur.close()
        if own:
            conn.close()
