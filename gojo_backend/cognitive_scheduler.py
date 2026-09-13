"""Idempotent scheduled-reflection ingress; it never creates a cycle."""
from datetime import datetime, timedelta, timezone

from cognitive_config import (
    COGNITIVE_REFLECTION_ACTIVE_DAYS,
    COGNITIVE_REFLECTION_INTERVAL_SECONDS,
    COGNITIVE_REFLECTION_SCAN_BATCH,
    COGNITIVE_REFLECTION_SUPPRESSION_SECONDS,
)
from cognitive_events import record_source_event
from cognitive_predictions import settle_pending_predictions
from cognitive_triggers import create_trigger_occurrence


def _as_utc(value=None):
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def reflection_bucket(value=None, interval_seconds=None):
    current = _as_utc(value)
    interval = int(interval_seconds or COGNITIVE_REFLECTION_INTERVAL_SECONDS)
    epoch_seconds = int(current.timestamp())
    bucket_seconds = epoch_seconds - (epoch_seconds % interval)
    return datetime.fromtimestamp(bucket_seconds, tz=timezone.utc)


def scheduled_source_event_id(user_id, character_id, scheduled_for=None):
    bucket = reflection_bucket(scheduled_for)
    return f'reflection:{bucket.isoformat()}:{user_id}:{character_id}'


def enqueue_scheduled_reflection(
    user_id,
    character_id,
    *,
    scheduled_for=None,
    conn=None,
):
    """Write one source event and one occurrence, with no queue inspection."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    event_time = _as_utc(scheduled_for)
    source_event_id = scheduled_source_event_id(
        user_id, character_id, event_time,
    )
    try:
        event_id = record_source_event(
            database,
            user_id=user_id,
            character_id=character_id,
            source_event_type='scheduled_reflection',
            source_event_id=source_event_id,
            source='cognitive_scheduler',
            occurred_at=event_time,
            payload={'scheduled_for': event_time.isoformat()},
        )
        if event_id is None:
            database.commit()
            return {'status': 'duplicate', 'event_id': None, 'trigger_id': None}

        settled_predictions = settle_pending_predictions(
            database,
            user_id=user_id,
            character_id=character_id,
            event_id=event_id,
            occurred_at=event_time,
        )

        cur = database.cursor()
        try:
            suppression_floor = event_time - timedelta(
                seconds=COGNITIVE_REFLECTION_SUPPRESSION_SECONDS,
            )
            cur.execute(
                '''SELECT id FROM cognitive_cycles
                   WHERE user_id = %s AND character_id = %s
                     AND status = 'succeeded'
                     AND completed_at >= %s AND completed_at <= %s
                   ORDER BY completed_at DESC LIMIT 1''',
                (
                    user_id, character_id, suppression_floor, event_time,
                ),
            )
            recent_success = cur.fetchone()
        finally:
            cur.close()

        if recent_success:
            trigger_id = create_trigger_occurrence(
                database,
                event_id=event_id,
                user_id=user_id,
                character_id=character_id,
                trigger_class='scheduled_reflection',
                occurrence_key='scheduled',
                payload={'scheduled_for': event_time.isoformat()},
                status='suppressed',
                suppressed_reason='recent_success',
                suppressed_at=event_time,
            )
            status = 'suppressed'
        else:
            trigger_id = create_trigger_occurrence(
                database,
                event_id=event_id,
                user_id=user_id,
                character_id=character_id,
                trigger_class='scheduled_reflection',
                occurrence_key='scheduled',
                payload={'scheduled_for': event_time.isoformat()},
            )
            status = 'pending'
        database.commit()
        return {
            'status': status,
            'event_id': event_id,
            'trigger_id': trigger_id,
            'settled_predictions': settled_predictions,
        }
    except Exception:
        database.rollback()
        raise
    finally:
        if owns_connection:
            database.close()


def enqueue_due_reflections(*, scheduled_for=None, conn=None):
    """Discover recently active pairs and enqueue their current time bucket."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    event_time = _as_utc(scheduled_for)
    activity_floor = event_time - timedelta(
        days=COGNITIVE_REFLECTION_ACTIVE_DAYS,
    )
    try:
        cur = database.cursor()
        try:
            cur.execute(
                '''SELECT user_id, character_id,
                          MAX(occurred_at) AS last_event_at
                   FROM cognitive_events
                   WHERE source_event_type <> 'scheduled_reflection'
                     AND occurred_at >= %s AND occurred_at <= %s
                   GROUP BY user_id, character_id
                   ORDER BY last_event_at DESC
                   LIMIT %s''',
                (activity_floor, event_time, COGNITIVE_REFLECTION_SCAN_BATCH),
            )
            pairs = [(row[0], row[1]) for row in cur.fetchall()]
        finally:
            cur.close()

        results = []
        for user_id, character_id in pairs:
            try:
                result = enqueue_scheduled_reflection(
                    user_id,
                    character_id,
                    scheduled_for=event_time,
                    conn=database,
                )
            except Exception as exc:
                result = {'status': 'failed', 'error': str(exc)[:180]}
                print(
                    '[cognitive_scheduler] reflection enqueue failed for '
                    f'{user_id}/{character_id}: {exc}',
                    flush=True,
                )
            results.append({
                'user_id': user_id,
                'character_id': character_id,
                'result': result,
            })
        return results
    finally:
        if owns_connection:
            database.close()
