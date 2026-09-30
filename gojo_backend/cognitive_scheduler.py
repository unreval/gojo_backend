"""Idempotent scheduled-reflection ingress; it never creates a cycle."""
from datetime import datetime, timezone

from cognitive_config import (
    COGNITIVE_REFLECTION_INTERVAL_SECONDS,
    COGNITIVE_REFLECTION_SCAN_BATCH,
)
from cognitive_events import record_source_event
from cognitive_predictions import settle_pending_predictions
from cognitive_queue import stable_advisory_lock_key
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


def enqueue_scheduled_reflection(user_id, character_id, *, scheduled_for=None, conn=None):
    """Only a due deadline justifies a timer event; idle time is not evidence."""
    import hashlib
    database = conn
    owns_connection = database is None
    if owns_connection:
        from db import get_conn
        database = get_conn()
    now = _as_utc(scheduled_for)
    cur = database.cursor()
    try:
        cur.execute('SELECT pg_advisory_xact_lock(%s)',
                    (stable_advisory_lock_key(user_id, character_id),))
        cur.execute("""SELECT id FROM cognitive_predictions
                       WHERE user_id=%s AND character_id=%s AND status='pending'
                         AND expires_at<=%s ORDER BY id FOR UPDATE""", (user_id, character_id, now))
        due = [row[0] for row in cur.fetchall()]
        if not due:
            return {'status': 'idle', 'event_id': None, 'trigger_id': None}
        key = hashlib.sha256(','.join(map(str, due)).encode()).hexdigest()[:24]
        event_id = record_source_event(database, user_id=user_id, character_id=character_id,
            source_event_type='scheduled_reflection', source_event_id='deadline:' + key,
            source='cognitive_scheduler', occurred_at=now, payload={'due_prediction_ids': due})
        if event_id is None:
            return {'status': 'duplicate', 'event_id': None, 'trigger_id': None}
        settled = settle_pending_predictions(database, user_id=user_id, character_id=character_id,
                                              event_id=event_id, occurred_at=now)
        trigger_id = create_trigger_occurrence(database, event_id=event_id, user_id=user_id,
            character_id=character_id, trigger_class='scheduled_reflection',
            occurrence_key='deadline', payload={'due_prediction_ids': due})
        database.commit()
        return {'status': 'pending', 'event_id': event_id, 'trigger_id': trigger_id,
                'settled_predictions': settled}
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def enqueue_due_reflections(*, scheduled_for=None, conn=None):
    """Discover only pairs with due prediction deadlines."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    event_time = _as_utc(scheduled_for)
    try:
        cur = database.cursor()
        try:
            cur.execute(
                """SELECT user_id, character_id, MIN(expires_at)
                   FROM cognitive_predictions WHERE status='pending' AND expires_at<=%s
                   GROUP BY user_id, character_id ORDER BY MIN(expires_at) LIMIT %s""",
                (event_time, COGNITIVE_REFLECTION_SCAN_BATCH),
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
