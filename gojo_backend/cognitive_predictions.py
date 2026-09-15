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

_USER_SIGNAL_TYPES = frozenset({
    'small_care', 'genuine_care', 'self_disclosure', 'flirt_signal',
    'positive_reciprocal', 'explicit_rejection', 'ambiguous_response',
    'promise_kept', 'promise_broken', 'boundary_hit',
    'boundary_respected', 'repair_attempt', 'offensive_content',
})
_CHARACTER_SIGNAL_TYPES = frozenset({
    'character_stance_declared', 'character_boundary_stated',
    'character_reciprocal',
})
_SIGNAL_TYPES = _USER_SIGNAL_TYPES | _CHARACTER_SIGNAL_TYPES
_SIGNAL_ACTORS = frozenset({'user', 'character'})
_SIGNAL_CONFIDENCE = frozenset({'low', 'medium', 'high'})
_SELECTOR_FIELDS = frozenset({
    'signal_type', 'actor', 'confidence', 'attributes',
})
_MAX_SIGNAL_SELECTORS = 8
PREDICTION_STANCE_TYPES = frozenset({
    'care_admission', 'promise', 'relationship_confirm',
    'retreat_boundary', 'boundary_stated',
})
_SIGNAL_ATTRIBUTE_FIELDS = {
    'self_disclosure': frozenset({'depth'}),
    'flirt_signal': frozenset({'explicit'}),
    'boundary_hit': frozenset({'topic_hint', 'severity', 'intentional'}),
    'boundary_respected': frozenset({'topic_hint'}),
    'repair_attempt': frozenset({
        'acknowledgment', 'responsibility', 'corrective_action',
    }),
    'offensive_content': frozenset({'target'}),
    'character_stance_declared': frozenset({'stance_type'}),
    'character_boundary_stated': frozenset({'topic_hint'}),
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


def _json_value(value, fallback):
    if value is None:
        return fallback
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback
    return value


def _normalize_selector(value, field):
    if not isinstance(value, dict):
        raise ValueError(f'{field}_must_be_object')
    if set(value) - _SELECTOR_FIELDS:
        raise ValueError(f'{field}_fields_invalid')
    signal_type = str(value.get('signal_type') or '').strip()
    actor = str(value.get('actor') or '').strip()
    if signal_type not in _SIGNAL_TYPES:
        raise ValueError(f'{field}_signal_type_invalid')
    if actor not in _SIGNAL_ACTORS:
        raise ValueError(f'{field}_actor_invalid')
    expected_actor = (
        'user' if signal_type in _USER_SIGNAL_TYPES else 'character'
    )
    if actor != expected_actor:
        raise ValueError(f'{field}_actor_signal_mismatch')
    result = {'signal_type': signal_type, 'actor': actor}
    confidence = value.get('confidence')
    if confidence is not None:
        if confidence not in _SIGNAL_CONFIDENCE:
            raise ValueError(f'{field}_confidence_invalid')
        result['confidence'] = confidence
    attributes = value.get('attributes')
    if attributes is not None:
        if not isinstance(attributes, dict) or len(attributes) > 8:
            raise ValueError(f'{field}_attributes_invalid')
        allowed_attribute_fields = _SIGNAL_ATTRIBUTE_FIELDS.get(
            signal_type, frozenset(),
        )
        if set(attributes) - allowed_attribute_fields:
            raise ValueError(f'{field}_attribute_not_observable')
        clean_attributes = {}
        for key, expected in attributes.items():
            if not isinstance(key, str) or not key or len(key) > 64:
                raise ValueError(f'{field}_attribute_key_invalid')
            if expected is not None and not isinstance(
                expected, (str, bool, int, float)
            ):
                raise ValueError(f'{field}_attribute_value_invalid')
            if isinstance(expected, str) and len(expected) > 120:
                raise ValueError(f'{field}_attribute_value_too_long')
            clean_attributes[key] = expected
        if signal_type == 'self_disclosure' and 'depth' in clean_attributes:
            if clean_attributes.get('depth') not in {'outer', 'middle', 'core'}:
                raise ValueError(f'{field}_depth_invalid')
        if signal_type == 'flirt_signal' and 'explicit' in clean_attributes:
            if not isinstance(clean_attributes.get('explicit'), bool):
                raise ValueError(f'{field}_explicit_invalid')
        if signal_type == 'boundary_hit':
            severity = clean_attributes.get('severity')
            intentional = clean_attributes.get('intentional')
            if severity is not None and severity not in {'low', 'medium', 'high'}:
                raise ValueError(f'{field}_severity_invalid')
            if intentional is not None and intentional not in {
                'yes', 'no', 'unclear',
            }:
                raise ValueError(f'{field}_intentional_invalid')
        if signal_type == 'repair_attempt':
            for key in ('acknowledgment', 'responsibility', 'corrective_action'):
                if key in clean_attributes and not isinstance(
                    clean_attributes[key], bool,
                ):
                    raise ValueError(f'{field}_{key}_invalid')
        if signal_type == 'offensive_content' and 'target' in clean_attributes:
            if clean_attributes.get('target') not in {
                'character', 'third_party',
            }:
                raise ValueError(f'{field}_target_invalid')
        if (
            signal_type == 'character_stance_declared'
            and 'stance_type' in clean_attributes
        ):
            if clean_attributes.get('stance_type') not in PREDICTION_STANCE_TYPES:
                raise ValueError(f'{field}_stance_type_invalid')
        result['attributes'] = clean_attributes
    return result


def normalize_signal_prediction_metadata(metadata):
    """Validate the declarative signal selectors used by semantic predictions."""
    value = _json_value(metadata, {})
    if not isinstance(value, dict):
        raise ValueError('prediction_metadata_must_be_object')
    description = str(value.get('description') or '').strip()
    if not description or len(description) > 400:
        raise ValueError('prediction_description_invalid')
    result = {'description': description}
    for field in ('fulfillment_signals', 'violation_signals'):
        selectors = value.get(field)
        if not isinstance(selectors, list) or not selectors:
            raise ValueError(f'prediction_{field}_must_not_be_empty')
        if len(selectors) > _MAX_SIGNAL_SELECTORS:
            raise ValueError(f'prediction_{field}_too_many_items')
        result[field] = [
            _normalize_selector(item, f'{field}_{index}')
            for index, item in enumerate(selectors)
        ]
    return result


def validate_signal_prediction_contract(
    resolver_name,
    fulfillment_operator,
    fulfillment_value,
    violation_operator,
    violation_value,
    metadata,
):
    if resolver_name != 'current_event_signal_outcome':
        raise ValueError('semantic_prediction_resolver_required')
    if fulfillment_operator != '>=' or float(fulfillment_value) != 1.0:
        raise ValueError('semantic_prediction_fulfillment_rule_invalid')
    if violation_operator != '<=' or float(violation_value) != -1.0:
        raise ValueError('semantic_prediction_violation_rule_invalid')
    return normalize_signal_prediction_metadata(metadata)


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


def _selector_matches(signal, selector):
    if not isinstance(signal, dict):
        return False
    if signal.get('signal_type') != selector['signal_type']:
        return False
    if signal.get('actor') != selector['actor']:
        return False
    if selector.get('confidence') is not None:
        if signal.get('confidence') != selector['confidence']:
            return False
    actual_attributes = signal.get('attributes')
    if not isinstance(actual_attributes, dict):
        actual_attributes = {}
    return all(
        actual_attributes.get(key) == expected
        for key, expected in selector.get('attributes', {}).items()
    )


def _resolve_current_event_signal_outcome(
    cur, prediction, event_id, occurred_at,
):
    cur.execute(
        '''SELECT payload
           FROM cognitive_events
           WHERE id = %s AND user_id = %s AND character_id = %s''',
        (
            event_id, prediction['user_id'], prediction['character_id'],
        ),
    )
    row = cur.fetchone()
    if not row:
        return None
    payload = _json_value(row[0], {})
    signals = payload.get('signals', []) if isinstance(payload, dict) else []
    metadata = normalize_signal_prediction_metadata(prediction.get('metadata'))
    if any(
        _selector_matches(signal, selector)
        for signal in signals
        for selector in metadata['violation_signals']
    ):
        return -1.0
    if any(
        _selector_matches(signal, selector)
        for signal in signals
        for selector in metadata['fulfillment_signals']
    ):
        return 1.0
    return 0.0


_RESOLVERS = {
    'current_event_signal_outcome': _resolve_current_event_signal_outcome,
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
    normalized_metadata = validate_signal_prediction_contract(
        resolver_name,
        fulfillment_operator,
        fulfillment_value,
        violation_operator,
        violation_value,
        metadata,
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
                json.dumps(normalized_metadata, ensure_ascii=False),
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
                      expires_at, created_at, metadata
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
            expires_at = _as_utc(prediction.get('expires_at'))
            if expires_at is not None and expires_at <= _as_utc(occurred_at):
                result = evaluate_prediction(prediction, {}, now=occurred_at)
            else:
                try:
                    value = resolver(cur, prediction, event_id, occurred_at)
                except ValueError as exc:
                    cur.execute(
                        '''UPDATE cognitive_predictions
                           SET last_error_code = %s WHERE id = %s''',
                        (str(exc)[:180], prediction['id']),
                    )
                    continue
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
            elif status == 'fulfilled':
                create_trigger_occurrence(
                    conn,
                    event_id=event_id,
                    user_id=user_id,
                    character_id=character_id,
                    trigger_class='prediction_confirmation',
                    occurrence_key=f'prediction:{prediction["id"]}',
                    payload=item,
                )
        return settled
    finally:
        cur.close()
