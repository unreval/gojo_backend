"""Deterministic prediction settlement with a static resolver whitelist."""
import json
import operator
from datetime import datetime, timezone

from cognitive_config import (
    PREDICTION_NUMERIC_OPERATORS,
    PREDICTION_RESOLVER_WHITELIST,
)
from cognitive_triggers import create_trigger_occurrence


_NUMERIC_OPERATORS = {
    '<': operator.lt,
    '<=': operator.le,
    '>': operator.gt,
    '>=': operator.ge,
}


def _as_utc(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _compare(value, operation, target):
    if operation not in PREDICTION_NUMERIC_OPERATORS:
        raise ValueError(f'unsupported numeric operator: {operation}')
    return _NUMERIC_OPERATORS[operation](float(value), float(target))


def validate_prediction_rule(resolver_name, operation, target):
    if resolver_name not in PREDICTION_RESOLVER_WHITELIST:
        raise ValueError(f'unsupported prediction resolver: {resolver_name}')
    if operation not in PREDICTION_NUMERIC_OPERATORS:
        raise ValueError(f'unsupported numeric operator: {operation}')
    return resolver_name, operation, float(target)


def evaluate_prediction(prediction, resolver_values, now=None):
    """Evaluate a prediction from already resolved, verifiable numeric facts."""
    reference_time = _as_utc(now or datetime.now(timezone.utc))
    expires_at = _as_utc(prediction.get('expires_at'))
    if expires_at is not None and expires_at <= reference_time:
        return {'status': 'expired', 'observed_value': None}

    resolver_name = prediction['resolver_name']
    if resolver_name not in PREDICTION_RESOLVER_WHITELIST:
        raise ValueError(f'unsupported prediction resolver: {resolver_name}')
    value = resolver_values.get(resolver_name)
    if value is None:
        return {'status': 'pending', 'observed_value': None}

    if _compare(
        value,
        prediction['fulfillment_operator'],
        prediction['fulfillment_value'],
    ):
        return {'status': 'fulfilled', 'observed_value': float(value)}

    violation_operator = prediction.get('violation_operator')
    violation_value = prediction.get('violation_value')
    if violation_operator is not None and violation_value is not None:
        if _compare(value, violation_operator, violation_value):
            return {'status': 'violated', 'observed_value': float(value)}

    return {'status': 'pending', 'observed_value': float(value)}


def _resolve_interaction_gap_seconds(cur, prediction, event_id, occurred_at):
    cur.execute(
        '''SELECT occurred_at
           FROM cognitive_events
           WHERE user_id = %s AND character_id = %s
             AND id <> %s
             AND source_event_type <> 'scheduled_reflection'
             AND occurred_at <= %s
           ORDER BY occurred_at DESC, id DESC
           LIMIT 1''',
        (
            prediction['user_id'], prediction['character_id'],
            event_id, occurred_at,
        ),
    )
    row = cur.fetchone()
    if not row:
        return None
    previous_at = _as_utc(row[0])
    current_at = _as_utc(occurred_at)
    return max(0.0, (current_at - previous_at).total_seconds())


def _resolve_messages_since_prediction_created(cur, prediction, event_id, occurred_at):
    cur.execute(
        '''SELECT COUNT(*)
           FROM cognitive_events
           WHERE user_id = %s AND character_id = %s
             AND source_event_type = 'relationship_v4_signal'
             AND created_at > %s AND occurred_at <= %s''',
        (
            prediction['user_id'], prediction['character_id'],
            prediction['created_at'], occurred_at,
        ),
    )
    return int(cur.fetchone()[0])


def _resolve_evidence_count_since_prediction_created(
    cur, prediction, event_id, occurred_at,
):
    cur.execute(
        '''SELECT COALESCE(SUM(
               CASE WHEN jsonb_typeof(payload -> 'signals') = 'array'
                    THEN jsonb_array_length(payload -> 'signals')
                    ELSE 0 END
           ), 0)
           FROM cognitive_events
           WHERE user_id = %s AND character_id = %s
             AND source_event_type = 'relationship_v4_signal'
             AND created_at > %s AND occurred_at <= %s''',
        (
            prediction['user_id'], prediction['character_id'],
            prediction['created_at'], occurred_at,
        ),
    )
    return int(cur.fetchone()[0])


_RESOLVERS = {
    'interaction_gap_seconds': _resolve_interaction_gap_seconds,
    'messages_since_prediction_created': _resolve_messages_since_prediction_created,
    'evidence_count_since_prediction_created': _resolve_evidence_count_since_prediction_created,
}


def registered_resolvers():
    return tuple(sorted(_RESOLVERS))


def create_prediction(
    *,
    user_id,
    character_id,
    prediction_key,
    resolver_name,
    fulfillment_operator,
    fulfillment_value,
    violation_operator=None,
    violation_value=None,
    expires_at=None,
    question_id=None,
    hypothesis_id=None,
    created_by_cycle_id=None,
    metadata=None,
    conn=None,
):
    validate_prediction_rule(
        resolver_name, fulfillment_operator, fulfillment_value,
    )
    if (violation_operator is None) != (violation_value is None):
        raise ValueError('violation operator and value must be provided together')
    if violation_operator is not None:
        validate_prediction_rule(
            resolver_name, violation_operator, violation_value,
        )

    owns_connection = conn is None
    if conn is None:
        from db import get_conn
        conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO cognitive_predictions (
                   user_id, character_id, question_id, hypothesis_id,
                   prediction_key, resolver_name,
                   fulfillment_operator, fulfillment_value,
                   violation_operator, violation_value, expires_at,
                   created_by_cycle_id, metadata
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s::jsonb)
               ON CONFLICT (user_id, character_id, prediction_key) DO NOTHING
               RETURNING id''',
            (
                user_id, character_id, question_id, hypothesis_id,
                prediction_key, resolver_name,
                fulfillment_operator, float(fulfillment_value),
                violation_operator,
                float(violation_value) if violation_value is not None else None,
                expires_at, created_by_cycle_id,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        row = cur.fetchone()
        if owns_connection:
            conn.commit()
        return row[0] if row else None
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            conn.close()


def settle_pending_predictions(
    conn, *, user_id, character_id, event_id, occurred_at,
):
    """Settle pending predictions and stop at a prediction_error occurrence."""
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, user_id, character_id, resolver_name,
                      fulfillment_operator, fulfillment_value,
                      violation_operator, violation_value,
                      expires_at, created_at
               FROM cognitive_predictions
               WHERE user_id = %s AND character_id = %s AND status = 'pending'
               ORDER BY created_at, id
               FOR UPDATE''',
            (user_id, character_id),
        )
        rows = cur.fetchall()
        columns = [item[0] for item in cur.description]
        predictions = [dict(zip(columns, row)) for row in rows]
        settled = []
        for prediction in predictions:
            resolver_name = prediction['resolver_name']
            resolver = _RESOLVERS.get(resolver_name)
            if resolver is None:
                cur.execute(
                    '''UPDATE cognitive_predictions
                       SET last_error_code = 'resolver_not_whitelisted'
                       WHERE id = %s''',
                    (prediction['id'],),
                )
                continue
            value = resolver(cur, prediction, event_id, occurred_at)
            result = evaluate_prediction(
                prediction, {resolver_name: value}, now=occurred_at,
            )
            status = result['status']
            observed = result['observed_value']
            if status == 'pending':
                cur.execute(
                    '''UPDATE cognitive_predictions
                       SET observed_value = %s, last_error_code = NULL
                       WHERE id = %s''',
                    (observed, prediction['id']),
                )
                continue

            cur.execute(
                '''UPDATE cognitive_predictions
                   SET status = %s, observed_value = %s,
                       settled_at = %s, settled_by_event_id = %s,
                       last_error_code = NULL
                   WHERE id = %s''',
                (status, observed, occurred_at, event_id, prediction['id']),
            )
            item = {
                'prediction_id': prediction['id'],
                'resolver_name': resolver_name,
                'status': status,
                'observed_value': observed,
            }
            settled.append(item)
            if status == 'violated':
                create_trigger_occurrence(
                    conn,
                    event_id=event_id,
                    user_id=user_id,
                    character_id=character_id,
                    trigger_class='prediction_error',
                    occurrence_key=f'prediction:{prediction["id"]}',
                    payload=item,
                )
        return settled
    finally:
        cur.close()
