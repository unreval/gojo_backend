"""Per-pair trigger aggregation and cognitive-cycle lifecycle."""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from cognitive_config import (
    COGNITIVE_CLAIM_LEASE_SECONDS,
    COGNITIVE_COOLDOWN_SECONDS,
    COGNITIVE_DAILY_CYCLE_LIMIT,
    COGNITIVE_MAX_BELIEFS_IN_CONTEXT,
    COGNITIVE_MAX_HYPOTHESES_IN_CONTEXT,
    COGNITIVE_MAX_PREDICTIONS_IN_CONTEXT,
    COGNITIVE_MAX_QUESTIONS_PER_CYCLE,
    COGNITIVE_MAX_QUESTIONS_IN_CONTEXT,
    COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT,
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


def _json_datetime(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'Object of type {type(value).__name__} is not JSON serializable')


def _read_behavior_anomalies(user_id, character_id, limit=6):
    """Read-only. Recalling anomalies must not reinforce them."""
    try:
        from behavior_evidence import recall_anomalies_without_reinforcement
        rows = recall_anomalies_without_reinforcement(
            user_id, character_id, limit=limit) or []
        out = []
        for row in rows:
            out.append({
                'observation_type': row.get('observation_type'),
                'direction': row.get('direction'),
                'magnitude': row.get('magnitude'),
                'confidence': row.get('confidence'),
                'busy_state': row.get('busy_state'),
                'interaction_mode': row.get('interaction_mode'),
                'observation_id': row.get('observation_id'),
                'note': 'measurable deviation only; not a relationship delta',
            })
        return out
    except Exception:
        return []


def _context_evidence_ids(context):
    result = set()

    def visit(value):
        if isinstance(value, dict):
            event_id = value.get('event_id')
            if isinstance(event_id, int) and not isinstance(event_id, bool):
                result.add(int(event_id))
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(context or {})
    return result


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


def claim_next_cycle(*, conn=None, now=None):
    """Claim one queued cycle across worker replicas and refresh its lease."""
    database, owns_connection = _get_connection(conn)
    current_time = _utc_now(now)
    lease_expires = current_time + timedelta(
        seconds=COGNITIVE_CLAIM_LEASE_SECONDS,
    )
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT id, user_id, character_id
               FROM cognitive_cycles
               WHERE status = 'queued'
               ORDER BY queued_at, id
               FOR UPDATE SKIP LOCKED
               LIMIT 1''',
        )
        row = cur.fetchone()
        if not row:
            database.commit()
            return None
        cycle_id, user_id, character_id = row
        cur.execute(
            '''UPDATE cognitive_event_triggers
               SET claimed_at = %s, claim_expires_at = %s
               WHERE claimed_by_cycle_id = %s AND status = 'claimed' ''',
            (current_time, lease_expires, cycle_id),
        )
        if cur.rowcount <= 0:
            cur.execute(
                '''UPDATE cognitive_cycles
                   SET status = 'failed', failure_code = 'missing_claimed_triggers',
                       output_state_version = NULL, completed_at = %s
                   WHERE id = %s''',
                (current_time, cycle_id),
            )
            database.commit()
            return {'status': 'invalid', 'cycle_id': cycle_id}
        cur.execute(
            '''UPDATE cognitive_cycles
               SET status = 'running', started_at = %s, failure_code = NULL
               WHERE id = %s AND status = 'queued' ''',
            (current_time, cycle_id),
        )
        database.commit()
        return {
            'status': 'running',
            'cycle_id': cycle_id,
            'user_id': user_id,
            'character_id': character_id,
            'claim_expires_at': lease_expires,
        }
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def commit_cycle_success(
    cycle_id,
    *,
    reasoning_context=None,
    structured_output=None,
    worker_model=None,
    worker_usage=None,
    conn=None,
    now=None,
):
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
        normalized_output = None
        belief_commit_decisions = None
        if structured_output is not None:
            cur.execute(
                '''SELECT DISTINCT trigger.event_id
                   FROM cognitive_cycle_trigger_events AS link
                   JOIN cognitive_event_triggers AS trigger
                     ON trigger.id = link.trigger_event_id
                   WHERE link.cycle_id = %s''',
                (cycle_id,),
            )
            current_event_ids = {int(row[0]) for row in cur.fetchall()}
            allowed_event_ids = set(current_event_ids)
            historical_candidates = (
                _context_evidence_ids(reasoning_context) - allowed_event_ids
            )
            if historical_candidates:
                cur.execute(
                    '''SELECT id FROM cognitive_events
                       WHERE user_id = %s AND character_id = %s
                         AND id = ANY(%s)''',
                    (
                        user_id,
                        character_id,
                        sorted(historical_candidates),
                    ),
                )
                allowed_event_ids.update(int(row[0]) for row in cur.fetchall())
            from cognitive_output import (
                persist_slow_loop_output,
                validate_slow_loop_output,
            )
            normalized_output = validate_slow_loop_output(
                structured_output,
                allowed_event_ids=allowed_event_ids,
                current_event_ids=current_event_ids,
            )
            belief_commit_decisions = persist_slow_loop_output(
                cur,
                cycle_id=cycle_id,
                user_id=user_id,
                character_id=character_id,
                output=normalized_output,
                now=current_time,
            )
        context_json = (
            json.dumps(
                reasoning_context, ensure_ascii=False, default=_json_datetime,
            )
            if reasoning_context is not None
            else None
        )
        output_json = {
            key: (
                json.dumps(normalized_output[key], ensure_ascii=False)
                if normalized_output is not None else None
            )
            for key in (
                'cycle_summary', 'question_updates', 'belief_updates',
                'hypothesis_updates', 'new_predictions', 'evidence_refs',
                'reflection_note',
                'sticky_note_updates', 'diary_entries',
            )
        }
        belief_commit_decisions_json = (
            json.dumps(belief_commit_decisions or [], ensure_ascii=False)
            if normalized_output is not None else None
        )
        usage_json = (
            json.dumps(worker_usage, ensure_ascii=False)
            if worker_usage is not None else None
        )
        cur.execute(
            '''UPDATE cognitive_cycles
               SET status = 'succeeded', output_state_version = %s,
                   reasoning_context = COALESCE(%s::jsonb, reasoning_context),
                   cycle_summary = COALESCE(%s::jsonb, cycle_summary),
                   question_updates = COALESCE(%s::jsonb, question_updates),
                   belief_updates = COALESCE(%s::jsonb, belief_updates),
                   belief_commit_decisions =
                       COALESCE(%s::jsonb, belief_commit_decisions),
                   hypothesis_updates = COALESCE(%s::jsonb, hypothesis_updates),
                   new_predictions = COALESCE(%s::jsonb, new_predictions),
                   evidence_refs = COALESCE(%s::jsonb, evidence_refs),
                   reflection_note = COALESCE(%s::jsonb, reflection_note),
                   sticky_note_updates = COALESCE(%s::jsonb, sticky_note_updates),
                   diary_entries = COALESCE(%s::jsonb, diary_entries),
                   worker_model = COALESCE(%s, worker_model),
                   worker_usage = COALESCE(%s::jsonb, worker_usage),
                   failure_code = NULL, completed_at = %s
               WHERE id = %s''',
            (
                output_version, context_json,
                output_json['cycle_summary'], output_json['question_updates'],
                output_json['belief_updates'], belief_commit_decisions_json,
                output_json['hypothesis_updates'], output_json['new_predictions'],
                output_json['evidence_refs'],
                output_json['reflection_note'],
                output_json['sticky_note_updates'], output_json['diary_entries'],
                worker_model, usage_json,
                current_time, cycle_id,
            ),
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
        return {
            'status': 'succeeded',
            'output_state_version': output_version,
            'output': (
                {**normalized_output,
                 'belief_commit_decisions': belief_commit_decisions or []}
                if normalized_output is not None else None
            ),
        }
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
    """Build factual context for the Slow Loop without invoking a model."""
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
                '''SELECT prediction.id, prediction.prediction_key,
                          prediction.status, prediction.resolver_name,
                          prediction.observed_value,
                          prediction.settled_by_event_id,
                          prediction.settled_at, prediction.metadata,
                          hypothesis.hypothesis_key
                   FROM cognitive_predictions AS prediction
                   LEFT JOIN cognitive_hypotheses AS hypothesis
                     ON hypothesis.id = prediction.hypothesis_id
                   WHERE prediction.settled_by_event_id = ANY(%s)
                   ORDER BY prediction.settled_at, prediction.id''',
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
                    'metadata': _json_value(row[7], {}),
                    'hypothesis_key': row[8],
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

        cur.execute(
            '''SELECT question_key, question_text, status,
                      source_event_refs, updated_at
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
                 AND status IN ('active', 'dormant')
               ORDER BY updated_at DESC, id DESC
               LIMIT %s''',
            (user_id, character_id, COGNITIVE_MAX_QUESTIONS_IN_CONTEXT),
        )
        current_questions = [
            {
                'question_key': row[0],
                'question_text': row[1],
                'status': row[2],
                'source_event_refs': _json_value(row[3], []),
                'updated_at': row[4],
                'lifecycle': 'separate_from_prediction_status',
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT belief_key, statement, confidence, status,
                      evidence_refs, updated_at
               FROM cognitive_beliefs
               WHERE user_id = %s AND character_id = %s
                 AND status = 'active'
               ORDER BY updated_at DESC, id DESC
               LIMIT %s''',
            (user_id, character_id, COGNITIVE_MAX_BELIEFS_IN_CONTEXT),
        )
        current_beliefs = [
            {
                'belief_key': row[0],
                'statement': row[1],
                'confidence': row[2],
                'status': row[3],
                'evidence_refs': _json_value(row[4], []),
                'updated_at': row[5],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT hypothesis_key, statement, status, hypothesis_type,
                      confidence, supporting_evidence_refs,
                      contradicting_evidence_refs, evidence, updated_at
               FROM cognitive_hypotheses
               WHERE user_id = %s AND character_id = %s
                 AND status <> 'archived'
               ORDER BY updated_at DESC, id DESC
               LIMIT %s''',
            (user_id, character_id, COGNITIVE_MAX_HYPOTHESES_IN_CONTEXT),
        )
        current_hypotheses = [
            {
                'hypothesis_key': row[0],
                'statement': row[1],
                'status': row[2],
                'hypothesis_type': row[3],
                'confidence': row[4],
                'supporting_evidence_refs': _json_value(row[5], []),
                'contradicting_evidence_refs': _json_value(row[6], []),
                'evidence': _json_value(row[7], []),
                'updated_at': row[8],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT prediction.prediction_key, prediction.resolver_name,
                      prediction.fulfillment_operator,
                      prediction.fulfillment_value,
                      prediction.violation_operator,
                      prediction.violation_value,
                      prediction.expires_at, prediction.observed_value,
                      prediction.metadata, prediction.created_at,
                      question.question_key, hypothesis.hypothesis_key
               FROM cognitive_predictions AS prediction
               LEFT JOIN cognitive_questions AS question
                 ON question.id = prediction.question_id
               LEFT JOIN cognitive_hypotheses AS hypothesis
                 ON hypothesis.id = prediction.hypothesis_id
               WHERE prediction.user_id = %s AND prediction.character_id = %s
                 AND prediction.status = 'pending'
               ORDER BY prediction.created_at DESC, prediction.id DESC
               LIMIT %s''',
            (user_id, character_id, COGNITIVE_MAX_PREDICTIONS_IN_CONTEXT),
        )
        pending_predictions = [
            {
                'prediction_key': row[0],
                'resolver_name': row[1],
                'fulfillment_operator': row[2],
                'fulfillment_value': row[3],
                'violation_operator': row[4],
                'violation_value': row[5],
                'expires_at': row[6],
                'observed_value': row[7],
                'metadata': _json_value(row[8], {}),
                'created_at': row[9],
                'question_key': row[10],
                'hypothesis_key': row[11],
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT note_key, content, status, source_event_refs,
                      expires_at, updated_at
               FROM cognitive_sticky_notes
               WHERE user_id = %s AND character_id = %s
                 AND status = 'active'
                 AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
               ORDER BY updated_at DESC, id DESC
               LIMIT %s''',
            (
                user_id, character_id,
                COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT,
            ),
        )
        current_sticky_notes = [
            {
                'note_key': row[0],
                'content': row[1],
                'status': row[2],
                'source_event_refs': _json_value(row[3], []),
                'expires_at': row[4],
                'updated_at': row[5],
                'lifecycle': 'short_term_visible_note_not_relationship_evidence',
            }
            for row in cur.fetchall()
        ]

        cur.execute(
            '''SELECT diary_key, content, source_event_refs, occurred_at
               FROM cognitive_diary_entries
               WHERE user_id = %s AND character_id = %s
               ORDER BY occurred_at DESC, id DESC
               LIMIT 12''',
            (user_id, character_id),
        )
        prior_diary_entries = [
            {
                'diary_key': row[0],
                'content': row[1],
                'source_event_refs': _json_value(row[2], []),
                'occurred_at': row[3],
                'note': 'already written; do not rewrite the same evidence/topic',
            }
            for row in cur.fetchall()
        ]

        event_times = [item['occurred_at'] for item in events_by_id.values()]
        return {
            'cycle_id': cycle_id,
            'input_state_version': input_version,
            'events': [events_by_id[event_id] for event_id in event_ids],
            'triggers': triggers,
            'settled_predictions': settled_predictions,
            'reactivated_questions': reactivated_questions,
            'current_questions': current_questions,
            'current_beliefs': current_beliefs,
            'current_hypotheses': current_hypotheses,
            'pending_predictions': pending_predictions,
            'current_sticky_notes': current_sticky_notes,
            'prior_diary_entries': prior_diary_entries,
            'temporal': {
                'queued_at': queued_at,
                'first_event_at': min(event_times) if event_times else None,
                'last_event_at': max(event_times) if event_times else None,
            },
            'pair': {'user_id': user_id, 'character_id': character_id},
            'primary_trigger_class': primary_class,
            'behavior_anomalies': _read_behavior_anomalies(user_id, character_id),
        }
    finally:
        cur.close()
        if owns_connection:
            database.close()
