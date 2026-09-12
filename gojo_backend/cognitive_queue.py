"""Per-pair trigger aggregation and cognitive-cycle lifecycle."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from cognitive_config import (
    COGNITIVE_CLAIM_LEASE_SECONDS,
    COGNITIVE_COOLDOWN_SECONDS,
    COGNITIVE_DAILY_CYCLE_LIMIT,
    COGNITIVE_MAX_QUESTIONS_PER_CYCLE,
    COGNITIVE_MAX_RETRY,
    COGNITIVE_MAX_TRIGGERS_PER_CYCLE,
)


def stable_advisory_lock_key(user_id, character_id):
    raw = f'{user_id}\x00{character_id}'.encode('utf-8')
    digest = hashlib.sha256(raw).digest()[:8]
    return int.from_bytes(digest, byteorder='big', signed=True)


def retry_status(attempt_count, max_retry=None):
    limit = COGNITIVE_MAX_RETRY if max_retry is None else int(max_retry)
    return 'dead_letter' if int(attempt_count) >= limit else 'pending'


def successful_output_version(input_state_version):
    return int(input_state_version) + 1


def failed_output_version(input_state_version):
    return None


def _utc_now(now=None):
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json_value(value, default):
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return default
    return value


def _get_connection(conn):
    if conn is not None:
        return conn, False
    from db import get_conn
    return get_conn(), True


def _lock_pair(cur, user_id, character_id):
    cur.execute(
        'SELECT pg_advisory_xact_lock(%s)',
        (stable_advisory_lock_key(user_id, character_id),),
    )


def _recover_expired_claims(cur, user_id, character_id, now):
    cur.execute(
        '''SELECT id, claimed_by_cycle_id, attempt_count
           FROM cognitive_event_triggers
           WHERE user_id = %s AND character_id = %s
             AND status = 'claimed' AND claim_expires_at <= %s
           ORDER BY id''',
        (user_id, character_id, now),
    )
    expired = cur.fetchall()
    cycle_ids = sorted({row[1] for row in expired if row[1] is not None})
    for cycle_id in cycle_ids:
        cur.execute(
            '''UPDATE cognitive_cycles
               SET status = 'failed', output_state_version = NULL,
                   failure_code = 'claim_lease_expired', completed_at = %s
               WHERE id = %s AND status IN ('queued', 'running')''',
            (now, cycle_id),
        )
    for trigger_id, _cycle_id, attempt_count in expired:
        status = retry_status(attempt_count)
        cur.execute(
            '''UPDATE cognitive_event_triggers
               SET status = %s,
                   claimed_by_cycle_id = NULL, claimed_at = NULL,
                   claim_expires_at = NULL,
                   consumed_cycle_id = NULL, consumed_at = NULL,
                   last_error_code = 'claim_lease_expired'
               WHERE id = %s''',
            (status, trigger_id),
        )
    return {'recovered': len(expired), 'failed_cycle_ids': cycle_ids}


def recover_expired_claims(user_id, character_id, *, conn=None, now=None):
    database, owns_connection = _get_connection(conn)
    current_time = _utc_now(now)
    cur = database.cursor()
    try:
        _lock_pair(cur, user_id, character_id)
        result = _recover_expired_claims(
            cur, user_id, character_id, current_time,
        )
        database.commit()
        return result
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def aggregate_pending_triggers(user_id, character_id, *, conn=None, now=None):
    """Claim pending occurrences into one queued cycle under a short xact lock."""
    database, owns_connection = _get_connection(conn)
    current_time = _utc_now(now)
    lease_expires = current_time + timedelta(
        seconds=COGNITIVE_CLAIM_LEASE_SECONDS,
    )
    cur = database.cursor()
    try:
        _lock_pair(cur, user_id, character_id)
        recovery = _recover_expired_claims(
            cur, user_id, character_id, current_time,
        )

        cur.execute(
            '''SELECT id FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
                 AND status IN ('queued', 'running')
               LIMIT 1''',
            (user_id, character_id),
        )
        active = cur.fetchone()
        if active:
            database.commit()
            return {
                'status': 'active_cycle_exists',
                'cycle_id': active[0],
                'recovery': recovery,
            }

        day_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        cur.execute(
            '''SELECT COUNT(*) FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
                 AND status = 'succeeded' AND completed_at >= %s
                 AND completed_at < %s''',
            (
                user_id, character_id, day_start,
                day_start + timedelta(days=1),
            ),
        )
        successful_today = int(cur.fetchone()[0])
        if successful_today >= COGNITIVE_DAILY_CYCLE_LIMIT:
            cur.execute(
                '''UPDATE cognitive_event_triggers
                   SET status = 'suppressed',
                       slow_cycle_suppressed_reason = 'daily_limit',
                       suppressed_at = %s
                   WHERE user_id = %s AND character_id = %s
                     AND status = 'pending' ''',
                (current_time, user_id, character_id),
            )
            suppressed = cur.rowcount
            database.commit()
            return {
                'status': 'daily_limit',
                'suppressed_count': suppressed,
                'recovery': recovery,
            }

        cur.execute(
            '''SELECT completed_at FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
                 AND status = 'succeeded'
               ORDER BY completed_at DESC NULLS LAST LIMIT 1''',
            (user_id, character_id),
        )
        latest_success = cur.fetchone()
        if latest_success and latest_success[0] is not None:
            cooldown_until = latest_success[0] + timedelta(
                seconds=COGNITIVE_COOLDOWN_SECONDS,
            )
            if cooldown_until > current_time:
                database.commit()
                return {
                    'status': 'cooldown',
                    'cooldown_until': cooldown_until,
                    'recovery': recovery,
                }

        cur.execute(
            '''SELECT id, trigger_class, priority, event_id, created_at
               FROM cognitive_event_triggers
               WHERE user_id = %s AND character_id = %s
                 AND status = 'pending'
                 AND trigger_class = 'question_reactivation'
               ORDER BY priority DESC, created_at, id
               FOR UPDATE SKIP LOCKED
               LIMIT %s''',
            (
                user_id, character_id,
                COGNITIVE_MAX_QUESTIONS_PER_CYCLE,
            ),
        )
        question_triggers = cur.fetchall()
        cur.execute(
            '''SELECT id, trigger_class, priority, event_id, created_at
               FROM cognitive_event_triggers
               WHERE user_id = %s AND character_id = %s
                 AND status = 'pending'
                 AND trigger_class <> 'question_reactivation'
               ORDER BY priority DESC, created_at, id
               FOR UPDATE SKIP LOCKED
               LIMIT %s''',
            (user_id, character_id, COGNITIVE_MAX_TRIGGERS_PER_CYCLE),
        )
        other_triggers = cur.fetchall()
        selected = sorted(
            question_triggers + other_triggers,
            key=lambda row: (-row[2], row[4], row[0]),
        )[:COGNITIVE_MAX_TRIGGERS_PER_CYCLE]
        if not selected:
            database.commit()
            return {'status': 'empty', 'recovery': recovery}

        cur.execute(
            '''SELECT COALESCE(MAX(output_state_version), 0)
               FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
                 AND status = 'succeeded' ''',
            (user_id, character_id),
        )
        input_version = int(cur.fetchone()[0])
        primary_class = selected[0][1]
        cur.execute(
            '''INSERT INTO cognitive_cycles (
                   user_id, character_id, status, primary_trigger_class,
                   input_state_version, queued_at
               ) VALUES (%s, %s, 'queued', %s, %s, %s)
               RETURNING id''',
            (
                user_id, character_id, primary_class,
                input_version, current_time,
            ),
        )
        cycle_id = cur.fetchone()[0]

        trigger_ids = []
        for position, (
            trigger_id, _trigger_class, _priority, _event_id, _created_at,
        ) in enumerate(selected):
            trigger_ids.append(trigger_id)
            cur.execute(
                '''UPDATE cognitive_event_triggers
                   SET status = 'claimed', claimed_by_cycle_id = %s,
                       claimed_at = %s, claim_expires_at = %s,
                       attempt_count = attempt_count + 1,
                       last_error_code = NULL
                   WHERE id = %s AND status = 'pending' ''',
                (cycle_id, current_time, lease_expires, trigger_id),
            )
            cur.execute(
                '''INSERT INTO cognitive_cycle_trigger_events (
                       cycle_id, trigger_event_id, is_primary, position
                   ) VALUES (%s, %s, %s, %s)''',
                (cycle_id, trigger_id, position == 0, position),
            )

        database.commit()
        return {
            'status': 'queued',
            'cycle_id': cycle_id,
            'input_state_version': input_version,
            'primary_trigger_class': primary_class,
            'trigger_ids': trigger_ids,
            'recovery': recovery,
        }
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def mark_cycle_running(cycle_id, *, conn=None, now=None):
    database, owns_connection = _get_connection(conn)
    current_time = _utc_now(now)
    cur = database.cursor()
    try:
        cur.execute(
            '''UPDATE cognitive_cycles
               SET status = 'running', started_at = %s
               WHERE id = %s AND status = 'queued'
               RETURNING id''',
            (current_time, cycle_id),
        )
        row = cur.fetchone()
        database.commit()
        return bool(row)
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def commit_cycle_success(cycle_id, *, reasoning_context=None, conn=None, now=None):
    database, owns_connection = _get_connection(conn)
    current_time = _utc_now(now)
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT user_id, character_id
               FROM cognitive_cycles WHERE id = %s''',
            (cycle_id,),
        )
        pair = cur.fetchone()
        if not pair:
            raise ValueError(f'cycle not found: {cycle_id}')
        user_id, character_id = pair
        _lock_pair(cur, user_id, character_id)
        cur.execute(
            '''SELECT status, input_state_version
               FROM cognitive_cycles WHERE id = %s FOR UPDATE''',
            (cycle_id,),
        )
        status, input_version = cur.fetchone()
        if status not in ('queued', 'running'):
            raise ValueError(f'cycle cannot succeed from status: {status}')
        cur.execute(
            '''SELECT COUNT(*), COUNT(*) FILTER (
                   WHERE trigger.status = 'claimed'
                     AND trigger.claimed_by_cycle_id = %s
                     AND trigger.claim_expires_at > %s
               )
               FROM cognitive_cycle_trigger_events AS link
               JOIN cognitive_event_triggers AS trigger
                 ON trigger.id = link.trigger_event_id
               WHERE link.cycle_id = %s''',
            (cycle_id, current_time, cycle_id),
        )
        trigger_count, valid_claim_count = cur.fetchone()
        if trigger_count == 0 or valid_claim_count != trigger_count:
            raise ValueError('cycle claim is missing or expired')
        output_version = successful_output_version(input_version)
        context_json = (
            json.dumps(reasoning_context, ensure_ascii=False)
            if reasoning_context is not None
            else None
        )
        cur.execute(
            '''UPDATE cognitive_cycles
               SET status = 'succeeded', output_state_version = %s,
                   reasoning_context = COALESCE(%s::jsonb, reasoning_context),
                   failure_code = NULL, completed_at = %s
               WHERE id = %s''',
            (output_version, context_json, current_time, cycle_id),
        )
        cur.execute(
            '''UPDATE cognitive_event_triggers AS trigger
               SET status = 'consumed', consumed_cycle_id = %s,
                   consumed_at = %s, claimed_by_cycle_id = NULL,
                   claimed_at = NULL, claim_expires_at = NULL,
                   last_error_code = NULL
               FROM cognitive_cycle_trigger_events AS link
               WHERE link.cycle_id = %s
                 AND link.trigger_event_id = trigger.id
                 AND trigger.status = 'claimed' ''',
            (cycle_id, current_time, cycle_id),
        )
        day_start = current_time.replace(hour=0, minute=0, second=0, microsecond=0)
        cur.execute(
            '''SELECT COUNT(*) FROM cognitive_cycles
               WHERE user_id = %s AND character_id = %s
                 AND status = 'succeeded' AND completed_at >= %s
                 AND completed_at < %s''',
            (
                user_id, character_id, day_start,
                day_start + timedelta(days=1),
            ),
        )
        if int(cur.fetchone()[0]) >= COGNITIVE_DAILY_CYCLE_LIMIT:
            cur.execute(
                '''UPDATE cognitive_event_triggers
                   SET status = 'suppressed',
                       slow_cycle_suppressed_reason = 'daily_limit',
                       suppressed_at = %s
                   WHERE user_id = %s AND character_id = %s
                     AND status = 'pending' ''',
                (current_time, user_id, character_id),
            )
        database.commit()
        return {'status': 'succeeded', 'output_state_version': output_version}
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def fail_cycle(cycle_id, error_code, *, conn=None, now=None):
    database, owns_connection = _get_connection(conn)
    current_time = _utc_now(now)
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT user_id, character_id
               FROM cognitive_cycles WHERE id = %s''',
            (cycle_id,),
        )
        pair = cur.fetchone()
        if not pair:
            raise ValueError(f'cycle not found: {cycle_id}')
        user_id, character_id = pair
        _lock_pair(cur, user_id, character_id)
        cur.execute(
            '''SELECT status FROM cognitive_cycles WHERE id = %s FOR UPDATE''',
            (cycle_id,),
        )
        status = cur.fetchone()[0]
        if status not in ('queued', 'running'):
            raise ValueError(f'cycle cannot fail from status: {status}')
        cur.execute(
            '''UPDATE cognitive_cycles
               SET status = 'failed', output_state_version = NULL,
                   failure_code = %s, completed_at = %s
               WHERE id = %s''',
            (error_code, current_time, cycle_id),
        )
        cur.execute(
            '''SELECT trigger.id, trigger.attempt_count
               FROM cognitive_event_triggers AS trigger
               JOIN cognitive_cycle_trigger_events AS link
                 ON link.trigger_event_id = trigger.id
               WHERE link.cycle_id = %s AND trigger.status = 'claimed'
               FOR UPDATE''',
            (cycle_id,),
        )
        released = []
        for trigger_id, attempt_count in cur.fetchall():
            next_status = retry_status(attempt_count)
            cur.execute(
                '''UPDATE cognitive_event_triggers
                   SET status = %s, claimed_by_cycle_id = NULL,
                       claimed_at = NULL, claim_expires_at = NULL,
                       consumed_cycle_id = NULL, consumed_at = NULL,
                       last_error_code = %s
                   WHERE id = %s''',
                (next_status, error_code, trigger_id),
            )
            released.append({'trigger_id': trigger_id, 'status': next_status})
        database.commit()
        return {
            'status': 'failed',
            'output_state_version': failed_output_version(0),
            'triggers': released,
        }
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def build_reasoning_context(cycle_id, *, conn=None):
    """Build factual context for a future Slow Loop without invoking a model."""
    database, owns_connection = _get_connection(conn)
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT user_id, character_id, input_state_version,
                      primary_trigger_class, queued_at
               FROM cognitive_cycles WHERE id = %s''',
            (cycle_id,),
        )
        cycle = cur.fetchone()
        if not cycle:
            raise ValueError(f'cycle not found: {cycle_id}')
        user_id, character_id, input_version, primary_class, queued_at = cycle

        cur.execute(
            '''SELECT trigger.id, trigger.trigger_class, trigger.priority,
                      trigger.payload, trigger.created_at,
                      link.is_primary, link.position,
                      event.id, event.source_event_type, event.source_event_id,
                      event.source, event.occurred_at, event.payload
               FROM cognitive_cycle_trigger_events AS link
               JOIN cognitive_event_triggers AS trigger
                 ON trigger.id = link.trigger_event_id
               JOIN cognitive_events AS event ON event.id = trigger.event_id
               WHERE link.cycle_id = %s
               ORDER BY link.position''',
            (cycle_id,),
        )
        rows = cur.fetchall()
        triggers = []
        events_by_id = {}
        event_ids = []
        reactivated_questions = []
        for row in rows:
            (
                trigger_id, trigger_class, priority, trigger_payload,
                trigger_created_at, is_primary, position,
                event_id, source_event_type, source_event_id,
                source, occurred_at, event_payload,
            ) = row
            payload = _json_value(trigger_payload, {})
            triggers.append({
                'trigger_id': trigger_id,
                'trigger_class': trigger_class,
                'priority': priority,
                'is_primary': bool(is_primary),
                'position': position,
                'event_id': event_id,
                'payload': payload,
                'created_at': trigger_created_at,
            })
            if event_id not in events_by_id:
                event_ids.append(event_id)
                events_by_id[event_id] = {
                    'event_id': event_id,
                    'source_event_type': source_event_type,
                    'source_event_id': source_event_id,
                    'source': source,
                    'occurred_at': occurred_at,
                    'payload': _json_value(event_payload, {}),
                }
            if trigger_class == 'question_reactivation':
                reactivated_questions.append({
                    'question_id': payload.get('question_id'),
                    'similarity': payload.get('similarity'),
                    'relation': 'possibly_related',
                })

        settled_predictions = []
        if event_ids:
            cur.execute(
                '''SELECT id, prediction_key, status, resolver_name,
                          observed_value, settled_by_event_id, settled_at
                   FROM cognitive_predictions
                   WHERE settled_by_event_id = ANY(%s)
                   ORDER BY settled_at, id''',
                (event_ids,),
            )
            settled_predictions = [
                {
                    'prediction_id': row[0],
                    'prediction_key': row[1],
                    'status': row[2],
                    'resolver_name': row[3],
                    'observed_value': row[4],
                    'settled_by_event_id': row[5],
                    'settled_at': row[6],
                }
                for row in cur.fetchall()
            ]

        if reactivated_questions:
            question_ids = [
                item['question_id'] for item in reactivated_questions
                if item['question_id'] is not None
            ]
            if question_ids:
                cur.execute(
                    '''SELECT id, question_key, question_text
                       FROM cognitive_questions WHERE id = ANY(%s)''',
                    (question_ids,),
                )
                question_facts = {
                    row[0]: {'question_key': row[1], 'question_text': row[2]}
                    for row in cur.fetchall()
                }
                for item in reactivated_questions:
                    item.update(question_facts.get(item['question_id'], {}))

        event_times = [item['occurred_at'] for item in events_by_id.values()]
        return {
            'cycle_id': cycle_id,
            'input_state_version': input_version,
            'events': [events_by_id[event_id] for event_id in event_ids],
            'triggers': triggers,
            'settled_predictions': settled_predictions,
            'reactivated_questions': reactivated_questions,
            'temporal': {
                'queued_at': queued_at,
                'first_event_at': min(event_times) if event_times else None,
                'last_event_at': max(event_times) if event_times else None,
            },
            'pair': {'user_id': user_id, 'character_id': character_id},
            'primary_trigger_class': primary_class,
        }
    finally:
        cur.close()
        if owns_connection:
            database.close()
