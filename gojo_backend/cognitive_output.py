"""Validation and transactional persistence for Slow Loop model output."""
import json
import re
from datetime import timedelta

from cognitive_config import (
    PREDICTION_NUMERIC_OPERATORS,
    PREDICTION_RESOLVER_WHITELIST,
)


ROOT_FIELDS = frozenset({
    'cycle_summary',
    'belief_updates',
    'hypothesis_updates',
    'new_predictions',
    'evidence_refs',
})
SUMMARY_FIELDS = frozenset({
    'summary', 'salient_change', 'uncertainty', 'confidence',
})
BELIEF_STATUSES = frozenset({'active', 'retracted'})
HYPOTHESIS_STATUSES = frozenset({'open', 'supported', 'rejected', 'archived'})
CONFIDENCE_LABELS = frozenset({'low', 'medium', 'high'})
KEY_RE = re.compile(r'^[a-z0-9][a-z0-9._:-]{0,127}$')
MAX_OUTPUT_ITEMS = 20
MAX_PREDICTIONS = 10
MIN_PREDICTION_TTL_SECONDS = 300
MAX_PREDICTION_TTL_SECONDS = 30 * 24 * 60 * 60


class SlowLoopOutputError(ValueError):
    pass


def parse_slow_loop_output(raw):
    """Parse the first complete JSON object without retaining model reasoning."""
    text = str(raw or '').strip()
    if text.startswith('```'):
        first_newline = text.find('\n')
        text = text[first_newline + 1:] if first_newline >= 0 else ''
        if text.rstrip().endswith('```'):
            text = text.rstrip()[:-3]
    start = text.find('{')
    if start < 0:
        raise SlowLoopOutputError('model_output_missing_json')
    try:
        value, _end = json.JSONDecoder().raw_decode(text[start:])
    except (TypeError, ValueError) as exc:
        raise SlowLoopOutputError('model_output_invalid_json') from exc
    if not isinstance(value, dict):
        raise SlowLoopOutputError('model_output_root_not_object')
    return value


def _object(value, field):
    if not isinstance(value, dict):
        raise SlowLoopOutputError(f'{field}_must_be_object')
    return value


def _array(value, field, maximum):
    if not isinstance(value, list):
        raise SlowLoopOutputError(f'{field}_must_be_array')
    if len(value) > maximum:
        raise SlowLoopOutputError(f'{field}_too_many_items')
    return value


def _text(value, field, maximum, *, allow_empty=False):
    if not isinstance(value, str):
        raise SlowLoopOutputError(f'{field}_must_be_string')
    result = value.strip()
    if not result and not allow_empty:
        raise SlowLoopOutputError(f'{field}_must_not_be_empty')
    if len(result) > maximum:
        raise SlowLoopOutputError(f'{field}_too_long')
    return result


def _key(value, field):
    result = _text(value, field, 128)
    if not KEY_RE.fullmatch(result):
        raise SlowLoopOutputError(f'{field}_invalid')
    return result


def _confidence(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SlowLoopOutputError(f'{field}_must_be_number')
    result = float(value)
    if result < 0.0 or result > 1.0:
        raise SlowLoopOutputError(f'{field}_out_of_range')
    return result


def _event_id(value, field, allowed_event_ids):
    if isinstance(value, bool) or not isinstance(value, int):
        raise SlowLoopOutputError(f'{field}_must_be_integer')
    result = int(value)
    if result not in allowed_event_ids:
        raise SlowLoopOutputError(f'{field}_not_in_cycle')
    return result


def _update_refs(value, field, allowed_event_ids, declared_event_ids):
    refs = _array(value, field, MAX_OUTPUT_ITEMS)
    result = []
    for index, item in enumerate(refs):
        event_id = _event_id(
            item, f'{field}_{index}', allowed_event_ids,
        )
        if event_id not in declared_event_ids:
            raise SlowLoopOutputError(f'{field}_{index}_not_declared')
        if event_id not in result:
            result.append(event_id)
    if not result:
        raise SlowLoopOutputError(f'{field}_must_not_be_empty')
    return result


def validate_slow_loop_output(value, *, allowed_event_ids):
    """Return a normalized output or reject any ungrounded/model-invented field."""
    root = _object(value, 'root')
    missing = ROOT_FIELDS - set(root)
    extra = set(root) - ROOT_FIELDS
    if missing:
        raise SlowLoopOutputError(
            'missing_root_fields:' + ','.join(sorted(missing)),
        )
    if extra:
        raise SlowLoopOutputError(
            'unexpected_root_fields:' + ','.join(sorted(extra)),
        )
    allowed_ids = {int(item) for item in allowed_event_ids}

    summary = _object(root['cycle_summary'], 'cycle_summary')
    if set(summary) != SUMMARY_FIELDS:
        raise SlowLoopOutputError('cycle_summary_fields_invalid')
    normalized_summary = {
        'summary': _text(summary['summary'], 'summary', 1200),
        'salient_change': _text(
            summary['salient_change'], 'salient_change', 600, allow_empty=True,
        ),
        'uncertainty': _text(
            summary['uncertainty'], 'uncertainty', 600, allow_empty=True,
        ),
        'confidence': _text(summary['confidence'], 'summary_confidence', 16),
    }
    if normalized_summary['confidence'] not in CONFIDENCE_LABELS:
        raise SlowLoopOutputError('summary_confidence_invalid')

    evidence_refs = []
    declared_ids = set()
    for index, item in enumerate(
        _array(root['evidence_refs'], 'evidence_refs', MAX_OUTPUT_ITEMS)
    ):
        ref = _object(item, f'evidence_ref_{index}')
        if set(ref) != {'event_id', 'reason'}:
            raise SlowLoopOutputError(f'evidence_ref_{index}_fields_invalid')
        event_id = _event_id(
            ref['event_id'], f'evidence_ref_{index}', allowed_ids,
        )
        if event_id in declared_ids:
            raise SlowLoopOutputError(f'evidence_ref_{index}_duplicate')
        declared_ids.add(event_id)
        evidence_refs.append({
            'event_id': event_id,
            'reason': _text(ref['reason'], f'evidence_ref_{index}_reason', 400),
        })
    if allowed_ids and not evidence_refs:
        raise SlowLoopOutputError('evidence_refs_must_not_be_empty')

    belief_updates = []
    belief_keys = set()
    for index, item in enumerate(
        _array(root['belief_updates'], 'belief_updates', MAX_OUTPUT_ITEMS)
    ):
        update = _object(item, f'belief_update_{index}')
        expected = {
            'belief_key', 'statement', 'confidence', 'status', 'evidence_refs',
        }
        if set(update) != expected:
            raise SlowLoopOutputError(f'belief_update_{index}_fields_invalid')
        key = _key(update['belief_key'], f'belief_update_{index}_key')
        if key in belief_keys:
            raise SlowLoopOutputError(f'belief_update_{index}_duplicate_key')
        belief_keys.add(key)
        status = _text(update['status'], f'belief_update_{index}_status', 16)
        if status not in BELIEF_STATUSES:
            raise SlowLoopOutputError(f'belief_update_{index}_status_invalid')
        belief_updates.append({
            'belief_key': key,
            'statement': _text(
                update['statement'], f'belief_update_{index}_statement', 1000,
            ),
            'confidence': _confidence(
                update['confidence'], f'belief_update_{index}_confidence',
            ),
            'status': status,
            'evidence_refs': _update_refs(
                update['evidence_refs'],
                f'belief_update_{index}_evidence_refs',
                allowed_ids, declared_ids,
            ),
        })

    hypothesis_updates = []
    hypothesis_keys = set()
    for index, item in enumerate(
        _array(root['hypothesis_updates'], 'hypothesis_updates', MAX_OUTPUT_ITEMS)
    ):
        update = _object(item, f'hypothesis_update_{index}')
        required = {
            'hypothesis_key', 'statement', 'status', 'evidence_refs',
        }
        optional = {'question_key'}
        if not required.issubset(update) or set(update) - required - optional:
            raise SlowLoopOutputError(f'hypothesis_update_{index}_fields_invalid')
        key = _key(
            update['hypothesis_key'], f'hypothesis_update_{index}_key',
        )
        if key in hypothesis_keys:
            raise SlowLoopOutputError(f'hypothesis_update_{index}_duplicate_key')
        hypothesis_keys.add(key)
        status = _text(update['status'], f'hypothesis_update_{index}_status', 16)
        if status not in HYPOTHESIS_STATUSES:
            raise SlowLoopOutputError(f'hypothesis_update_{index}_status_invalid')
        question_key = update.get('question_key')
        hypothesis_updates.append({
            'hypothesis_key': key,
            'statement': _text(
                update['statement'], f'hypothesis_update_{index}_statement', 1200,
            ),
            'status': status,
            'question_key': (
                _key(question_key, f'hypothesis_update_{index}_question_key')
                if question_key is not None else None
            ),
            'evidence_refs': _update_refs(
                update['evidence_refs'],
                f'hypothesis_update_{index}_evidence_refs',
                allowed_ids, declared_ids,
            ),
        })

    new_predictions = []
    prediction_keys = set()
    for index, item in enumerate(
        _array(root['new_predictions'], 'new_predictions', MAX_PREDICTIONS)
    ):
        prediction = _object(item, f'new_prediction_{index}')
        required = {
            'prediction_key', 'resolver_name', 'fulfillment_operator',
            'fulfillment_value', 'expires_in_seconds', 'evidence_refs',
        }
        optional = {
            'violation_operator', 'violation_value', 'hypothesis_key', 'metadata',
        }
        if not required.issubset(prediction) or set(prediction) - required - optional:
            raise SlowLoopOutputError(f'new_prediction_{index}_fields_invalid')
        key = _key(prediction['prediction_key'], f'new_prediction_{index}_key')
        if key in prediction_keys:
            raise SlowLoopOutputError(f'new_prediction_{index}_duplicate_key')
        prediction_keys.add(key)
        resolver = _text(
            prediction['resolver_name'], f'new_prediction_{index}_resolver', 80,
        )
        if resolver not in PREDICTION_RESOLVER_WHITELIST:
            raise SlowLoopOutputError(f'new_prediction_{index}_resolver_invalid')
        fulfillment_operator = _text(
            prediction['fulfillment_operator'],
            f'new_prediction_{index}_fulfillment_operator', 8,
        )
        if fulfillment_operator not in PREDICTION_NUMERIC_OPERATORS:
            raise SlowLoopOutputError(
                f'new_prediction_{index}_fulfillment_operator_invalid',
            )
        fulfillment_value = prediction['fulfillment_value']
        if isinstance(fulfillment_value, bool) or not isinstance(
            fulfillment_value, (int, float)
        ):
            raise SlowLoopOutputError(
                f'new_prediction_{index}_fulfillment_value_invalid',
            )
        violation_operator = prediction.get('violation_operator')
        violation_value = prediction.get('violation_value')
        if (violation_operator is None) != (violation_value is None):
            raise SlowLoopOutputError(f'new_prediction_{index}_violation_pair')
        if violation_operator is not None:
            violation_operator = _text(
                violation_operator,
                f'new_prediction_{index}_violation_operator', 8,
            )
            if violation_operator not in PREDICTION_NUMERIC_OPERATORS:
                raise SlowLoopOutputError(
                    f'new_prediction_{index}_violation_operator_invalid',
                )
            if isinstance(violation_value, bool) or not isinstance(
                violation_value, (int, float)
            ):
                raise SlowLoopOutputError(
                    f'new_prediction_{index}_violation_value_invalid',
                )
            violation_value = float(violation_value)
        ttl = prediction['expires_in_seconds']
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise SlowLoopOutputError(f'new_prediction_{index}_ttl_invalid')
        if ttl < MIN_PREDICTION_TTL_SECONDS or ttl > MAX_PREDICTION_TTL_SECONDS:
            raise SlowLoopOutputError(f'new_prediction_{index}_ttl_out_of_range')
        hypothesis_key = prediction.get('hypothesis_key')
        metadata = prediction.get('metadata', {})
        if not isinstance(metadata, dict):
            raise SlowLoopOutputError(f'new_prediction_{index}_metadata_invalid')
        if len(json.dumps(metadata, ensure_ascii=False)) > 4000:
            raise SlowLoopOutputError(f'new_prediction_{index}_metadata_too_large')
        new_predictions.append({
            'prediction_key': key,
            'resolver_name': resolver,
            'fulfillment_operator': fulfillment_operator,
            'fulfillment_value': float(fulfillment_value),
            'violation_operator': violation_operator,
            'violation_value': violation_value,
            'expires_in_seconds': ttl,
            'hypothesis_key': (
                _key(hypothesis_key, f'new_prediction_{index}_hypothesis_key')
                if hypothesis_key is not None else None
            ),
            'metadata': metadata,
            'evidence_refs': _update_refs(
                prediction['evidence_refs'],
                f'new_prediction_{index}_evidence_refs',
                allowed_ids, declared_ids,
            ),
        })

    return {
        'cycle_summary': normalized_summary,
        'belief_updates': belief_updates,
        'hypothesis_updates': hypothesis_updates,
        'new_predictions': new_predictions,
        'evidence_refs': evidence_refs,
    }


def persist_slow_loop_output(
    cur, *, cycle_id, user_id, character_id, output, now,
):
    """Apply durable beliefs, hypotheses, and predictions in the cycle txn."""
    refs_by_id = {
        item['event_id']: item for item in output['evidence_refs']
    }

    def full_refs(event_ids):
        return [refs_by_id[event_id] for event_id in event_ids]

    for update in output['belief_updates']:
        cur.execute(
            '''INSERT INTO cognitive_beliefs (
                   user_id, character_id, belief_key, statement, confidence,
                   status, evidence_refs, created_by_cycle_id,
                   updated_by_cycle_id, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
               ON CONFLICT (user_id, character_id, belief_key) DO UPDATE
               SET statement = EXCLUDED.statement,
                   confidence = EXCLUDED.confidence,
                   status = EXCLUDED.status,
                   evidence_refs = EXCLUDED.evidence_refs,
                   updated_by_cycle_id = EXCLUDED.updated_by_cycle_id,
                   updated_at = EXCLUDED.updated_at''',
            (
                user_id, character_id, update['belief_key'],
                update['statement'], update['confidence'], update['status'],
                json.dumps(full_refs(update['evidence_refs']), ensure_ascii=False),
                cycle_id, cycle_id, now,
            ),
        )

    hypothesis_ids = {}
    for update in output['hypothesis_updates']:
        question_id = None
        if update['question_key']:
            cur.execute(
                '''SELECT id FROM cognitive_questions
                   WHERE user_id = %s AND character_id = %s
                     AND question_key = %s''',
                (user_id, character_id, update['question_key']),
            )
            row = cur.fetchone()
            question_id = row[0] if row else None
        evidence_entry = [{
            'cycle_id': cycle_id,
            'evidence_refs': full_refs(update['evidence_refs']),
        }]
        cur.execute(
            '''INSERT INTO cognitive_hypotheses (
                   user_id, character_id, question_id, hypothesis_key,
                   statement, status, evidence, created_by_cycle_id,
                   updated_by_cycle_id, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
               ON CONFLICT (user_id, character_id, hypothesis_key) DO UPDATE
               SET question_id = COALESCE(
                       EXCLUDED.question_id, cognitive_hypotheses.question_id),
                   statement = EXCLUDED.statement,
                   status = EXCLUDED.status,
                   evidence = cognitive_hypotheses.evidence || EXCLUDED.evidence,
                   updated_by_cycle_id = EXCLUDED.updated_by_cycle_id,
                   updated_at = EXCLUDED.updated_at
               RETURNING id''',
            (
                user_id, character_id, question_id,
                update['hypothesis_key'], update['statement'], update['status'],
                json.dumps(evidence_entry, ensure_ascii=False),
                cycle_id, cycle_id, now,
            ),
        )
        hypothesis_ids[update['hypothesis_key']] = cur.fetchone()[0]

    for prediction in output['new_predictions']:
        hypothesis_id = None
        hypothesis_key = prediction['hypothesis_key']
        if hypothesis_key:
            hypothesis_id = hypothesis_ids.get(hypothesis_key)
            if hypothesis_id is None:
                cur.execute(
                    '''SELECT id FROM cognitive_hypotheses
                       WHERE user_id = %s AND character_id = %s
                         AND hypothesis_key = %s''',
                    (user_id, character_id, hypothesis_key),
                )
                row = cur.fetchone()
                hypothesis_id = row[0] if row else None
        metadata = dict(prediction['metadata'])
        metadata.update({
            'created_by': 'cognitive_slow_loop',
            'evidence_refs': full_refs(prediction['evidence_refs']),
        })
        cur.execute(
            '''INSERT INTO cognitive_predictions (
                   user_id, character_id, hypothesis_id, prediction_key,
                   resolver_name, fulfillment_operator, fulfillment_value,
                   violation_operator, violation_value, expires_at,
                   created_by_cycle_id, metadata
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s::jsonb)
               ON CONFLICT (user_id, character_id, prediction_key) DO NOTHING''',
            (
                user_id, character_id, hypothesis_id,
                prediction['prediction_key'], prediction['resolver_name'],
                prediction['fulfillment_operator'],
                prediction['fulfillment_value'],
                prediction['violation_operator'], prediction['violation_value'],
                now + timedelta(seconds=prediction['expires_in_seconds']),
                cycle_id, json.dumps(metadata, ensure_ascii=False),
            ),
        )
