"""Validation and transactional persistence for Slow Loop model output."""
import json
import re
from datetime import timedelta

from cognitive_config import (
    COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE,
    COGNITIVE_BELIEF_COMMIT_MIN_INDEPENDENT_EVIDENCE,
    COGNITIVE_STICKY_NOTE_DEFAULT_TTL_SECONDS,
    COGNITIVE_STICKY_NOTE_MAX_TTL_SECONDS,
    PREDICTION_NUMERIC_OPERATORS,
    PREDICTION_RESOLVER_WHITELIST,
)
from cognitive_predictions import validate_signal_prediction_contract


ROOT_FIELDS = frozenset({
    'cycle_summary',
    'question_updates',
    'belief_updates',
    'hypothesis_updates',
    'new_predictions',
    'evidence_refs',
    'reflection_note',
})
OPTIONAL_ROOT_FIELDS = frozenset({
    'sticky_note_updates',
    'diary_entries',
})
SUMMARY_FIELDS = frozenset({
    'summary', 'salient_change', 'uncertainty', 'confidence',
})
BELIEF_STATUSES = frozenset({'active', 'retracted'})
HYPOTHESIS_STATUSES = frozenset({'open', 'supported', 'rejected', 'archived'})
QUESTION_STATUSES = frozenset({'active', 'dormant', 'resolved', 'archived'})
HYPOTHESIS_TYPES = frozenset({
    'self_model', 'relationship', 'user_model', 'interaction_pattern',
})
BELIEF_TYPES = frozenset({
    'general', 'self_model', 'relationship_observation',
    'user_model', 'interaction_pattern',
})
STICKY_NOTE_STATUSES = frozenset({'active', 'completed', 'expired', 'archived'})
DIARY_REFLECTION_KINDS = frozenset({'event', 'periodic', 'repair', 'uncertainty'})
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


def _update_refs(
    value,
    field,
    allowed_event_ids,
    declared_event_ids,
    *,
    allow_empty=False,
):
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
    if not result and not allow_empty:
        raise SlowLoopOutputError(f'{field}_must_not_be_empty')
    return result


def _require_current_ref(refs, field, current_event_ids):
    if current_event_ids and not any(event_id in current_event_ids for event_id in refs):
        raise SlowLoopOutputError(f'{field}_must_reference_current_evidence')


def validate_slow_loop_output(value, *, allowed_event_ids, current_event_ids=None):
    """Return a normalized output or reject any ungrounded/model-invented field."""
    root = _object(value, 'root')
    missing = ROOT_FIELDS - set(root)
    extra = set(root) - ROOT_FIELDS - OPTIONAL_ROOT_FIELDS
    if missing:
        raise SlowLoopOutputError(
            'missing_root_fields:' + ','.join(sorted(missing)),
        )
    if extra:
        raise SlowLoopOutputError(
            'unexpected_root_fields:' + ','.join(sorted(extra)),
        )
    allowed_ids = {int(item) for item in allowed_event_ids}
    current_ids = (
        {int(item) for item in current_event_ids}
        if current_event_ids is not None else set()
    )

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

    question_updates = []
    question_keys = set()
    for index, item in enumerate(
        _array(root['question_updates'], 'question_updates', MAX_OUTPUT_ITEMS)
    ):
        update = _object(item, f'question_update_{index}')
        expected = {
            'question_key', 'question_text', 'status', 'evidence_refs',
        }
        if set(update) != expected:
            raise SlowLoopOutputError(f'question_update_{index}_fields_invalid')
        key = _key(update['question_key'], f'question_update_{index}_key')
        if key in question_keys:
            raise SlowLoopOutputError(f'question_update_{index}_duplicate_key')
        question_keys.add(key)
        status = _text(update['status'], f'question_update_{index}_status', 16)
        if status not in QUESTION_STATUSES:
            raise SlowLoopOutputError(f'question_update_{index}_status_invalid')
        refs = _update_refs(
            update['evidence_refs'],
            f'question_update_{index}_evidence_refs',
            allowed_ids, declared_ids,
        )
        _require_current_ref(
            refs, f'question_update_{index}_evidence_refs', current_ids,
        )
        question_updates.append({
            'question_key': key,
            'question_text': _text(
                update['question_text'],
                f'question_update_{index}_question_text',
                800,
            ),
            'status': status,
            'evidence_refs': refs,
        })

    belief_updates = []
    belief_keys = set()
    for index, item in enumerate(
        _array(root['belief_updates'], 'belief_updates', MAX_OUTPUT_ITEMS)
    ):
        update = _object(item, f'belief_update_{index}')
        expected = {
            'belief_key', 'statement', 'confidence', 'status', 'belief_type',
            'from_hypothesis_key', 'evidence_refs',
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
        belief_type = _text(
            update['belief_type'], f'belief_update_{index}_belief_type', 32,
        )
        if belief_type not in BELIEF_TYPES:
            raise SlowLoopOutputError(f'belief_update_{index}_belief_type_invalid')
        from_hypothesis_key = _key(
            update['from_hypothesis_key'],
            f'belief_update_{index}_from_hypothesis_key',
        )
        refs = _update_refs(
            update['evidence_refs'],
            f'belief_update_{index}_evidence_refs',
            allowed_ids, declared_ids,
        )
        _require_current_ref(
            refs, f'belief_update_{index}_evidence_refs', current_ids,
        )
        belief_updates.append({
            'belief_key': key,
            'statement': _text(
                update['statement'], f'belief_update_{index}_statement', 1000,
            ),
            'confidence': _confidence(
                update['confidence'], f'belief_update_{index}_confidence',
            ),
            'status': status,
            'belief_type': belief_type,
            'from_hypothesis_key': from_hypothesis_key,
            'evidence_refs': refs,
        })

    hypothesis_updates = []
    hypothesis_keys = set()
    for index, item in enumerate(
        _array(root['hypothesis_updates'], 'hypothesis_updates', MAX_OUTPUT_ITEMS)
    ):
        update = _object(item, f'hypothesis_update_{index}')
        required = {
            'hypothesis_key', 'statement', 'hypothesis_type', 'confidence',
            'status', 'question_key', 'supporting_evidence_refs',
            'contradicting_evidence_refs',
        }
        if set(update) != required:
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
        hypothesis_type = _text(
            update['hypothesis_type'],
            f'hypothesis_update_{index}_hypothesis_type',
            32,
        )
        if hypothesis_type not in HYPOTHESIS_TYPES:
            raise SlowLoopOutputError(
                f'hypothesis_update_{index}_hypothesis_type_invalid',
            )
        question_key = _key(
            update['question_key'], f'hypothesis_update_{index}_question_key',
        )
        supporting_refs = _update_refs(
            update['supporting_evidence_refs'],
            f'hypothesis_update_{index}_supporting_evidence_refs',
            allowed_ids, declared_ids, allow_empty=True,
        )
        contradicting_refs = _update_refs(
            update['contradicting_evidence_refs'],
            f'hypothesis_update_{index}_contradicting_evidence_refs',
            allowed_ids, declared_ids, allow_empty=True,
        )
        combined_refs = supporting_refs + [
            event_id for event_id in contradicting_refs
            if event_id not in supporting_refs
        ]
        if not combined_refs:
            raise SlowLoopOutputError(
                f'hypothesis_update_{index}_evidence_refs_must_not_be_empty',
            )
        _require_current_ref(
            combined_refs, f'hypothesis_update_{index}_evidence_refs',
            current_ids,
        )
        hypothesis_updates.append({
            'hypothesis_key': key,
            'statement': _text(
                update['statement'], f'hypothesis_update_{index}_statement', 1200,
            ),
            'hypothesis_type': hypothesis_type,
            'confidence': _confidence(
                update['confidence'],
                f'hypothesis_update_{index}_confidence',
            ),
            'status': status,
            'question_key': question_key,
            'supporting_evidence_refs': supporting_refs,
            'contradicting_evidence_refs': contradicting_refs,
        })

    new_predictions = []
    prediction_keys = set()
    for index, item in enumerate(
        _array(root['new_predictions'], 'new_predictions', MAX_PREDICTIONS)
    ):
        prediction = _object(item, f'new_prediction_{index}')
        required = {
            'prediction_key', 'resolver_name', 'fulfillment_operator',
            'fulfillment_value', 'expires_in_seconds', 'question_key',
            'hypothesis_key', 'evidence_refs',
        }
        optional = {
            'violation_operator', 'violation_value', 'metadata',
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
        question_key = _key(
            prediction['question_key'], f'new_prediction_{index}_question_key',
        )
        hypothesis_key = _key(
            prediction['hypothesis_key'],
            f'new_prediction_{index}_hypothesis_key',
        )
        metadata = prediction.get('metadata', {})
        if not isinstance(metadata, dict):
            raise SlowLoopOutputError(f'new_prediction_{index}_metadata_invalid')
        if len(json.dumps(metadata, ensure_ascii=False)) > 4000:
            raise SlowLoopOutputError(f'new_prediction_{index}_metadata_too_large')
        try:
            metadata = validate_signal_prediction_contract(
                resolver,
                fulfillment_operator,
                fulfillment_value,
                violation_operator,
                violation_value,
                metadata,
            )
        except (TypeError, ValueError) as exc:
            raise SlowLoopOutputError(
                f'new_prediction_{index}_{exc}',
            ) from exc
        refs = _update_refs(
            prediction['evidence_refs'],
            f'new_prediction_{index}_evidence_refs',
            allowed_ids, declared_ids,
        )
        _require_current_ref(
            refs, f'new_prediction_{index}_evidence_refs', current_ids,
        )
        new_predictions.append({
            'prediction_key': key,
            'resolver_name': resolver,
            'fulfillment_operator': fulfillment_operator,
            'fulfillment_value': float(fulfillment_value),
            'violation_operator': violation_operator,
            'violation_value': violation_value,
            'expires_in_seconds': ttl,
            'question_key': question_key,
            'hypothesis_key': hypothesis_key,
            'metadata': metadata,
            'evidence_refs': refs,
        })

    sticky_note_updates = []
    sticky_keys = set()
    for index, item in enumerate(
        _array(root.get('sticky_note_updates', []),
               'sticky_note_updates', MAX_OUTPUT_ITEMS)
    ):
        update = _object(item, f'sticky_note_update_{index}')
        required = {'note_key', 'content', 'status', 'evidence_refs'}
        optional = {'expires_in_seconds'}
        if not required.issubset(update) or set(update) - required - optional:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_fields_invalid',
            )
        key = _key(update['note_key'], f'sticky_note_update_{index}_key')
        if key in sticky_keys:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_duplicate_key',
            )
        sticky_keys.add(key)
        status = _text(update['status'], f'sticky_note_update_{index}_status', 16)
        if status not in STICKY_NOTE_STATUSES:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_status_invalid',
            )
        ttl = update.get('expires_in_seconds')
        if ttl is None and status == 'active':
            ttl = COGNITIVE_STICKY_NOTE_DEFAULT_TTL_SECONDS
        if ttl is not None:
            if isinstance(ttl, bool) or not isinstance(ttl, int):
                raise SlowLoopOutputError(
                    f'sticky_note_update_{index}_ttl_invalid',
                )
            if ttl < 300 or ttl > COGNITIVE_STICKY_NOTE_MAX_TTL_SECONDS:
                raise SlowLoopOutputError(
                    f'sticky_note_update_{index}_ttl_out_of_range',
                )
        refs = _update_refs(
            update['evidence_refs'],
            f'sticky_note_update_{index}_evidence_refs',
            allowed_ids, declared_ids,
        )
        _require_current_ref(
            refs, f'sticky_note_update_{index}_evidence_refs', current_ids,
        )
        sticky_note_updates.append({
            'note_key': key,
            'content': _text(
                update['content'],
                f'sticky_note_update_{index}_content',
                500,
            ),
            'status': status,
            'expires_in_seconds': ttl,
            'evidence_refs': refs,
        })

    diary_entries = []
    diary_keys = set()
    for index, item in enumerate(
        _array(root.get('diary_entries', []), 'diary_entries', MAX_OUTPUT_ITEMS)
    ):
        entry = _object(item, f'diary_entry_{index}')
        required = {'diary_key', 'content', 'reflection_kind', 'evidence_refs'}
        if set(entry) != required:
            raise SlowLoopOutputError(f'diary_entry_{index}_fields_invalid')
        key = _key(entry['diary_key'], f'diary_entry_{index}_key')
        if key in diary_keys:
            raise SlowLoopOutputError(f'diary_entry_{index}_duplicate_key')
        diary_keys.add(key)
        reflection_kind = _text(
            entry['reflection_kind'], f'diary_entry_{index}_kind', 24,
        )
        if reflection_kind not in DIARY_REFLECTION_KINDS:
            raise SlowLoopOutputError(f'diary_entry_{index}_kind_invalid')
        refs = _update_refs(
            entry['evidence_refs'],
            f'diary_entry_{index}_evidence_refs',
            allowed_ids, declared_ids,
        )
        _require_current_ref(
            refs, f'diary_entry_{index}_evidence_refs', current_ids,
        )
        diary_entries.append({
            'diary_key': key,
            'content': _text(
                entry['content'], f'diary_entry_{index}_content', 1200,
            ),
            'reflection_kind': reflection_kind,
            'evidence_refs': refs,
        })

    note = _object(root['reflection_note'], 'reflection_note')
    if set(note) != {'content', 'evidence_refs'}:
        raise SlowLoopOutputError('reflection_note_fields_invalid')
    reflection_content = _text(
        note['content'], 'reflection_note_content', 800, allow_empty=True,
    )
    reflection_refs = _update_refs(
        note['evidence_refs'],
        'reflection_note_evidence_refs',
        allowed_ids,
        declared_ids,
        allow_empty=not bool(reflection_content),
    )
    if reflection_content:
        _require_current_ref(
            reflection_refs, 'reflection_note_evidence_refs', current_ids,
        )
    elif reflection_refs:
        raise SlowLoopOutputError('empty_reflection_note_must_not_cite_evidence')

    return {
        'cycle_summary': normalized_summary,
        'question_updates': question_updates,
        'belief_updates': belief_updates,
        'hypothesis_updates': hypothesis_updates,
        'new_predictions': new_predictions,
        'evidence_refs': evidence_refs,
        'reflection_note': {
            'content': reflection_content,
            'evidence_refs': reflection_refs,
        },
        'sticky_note_updates': sticky_note_updates,
        'diary_entries': diary_entries,
    }


def _load_event_metadata(cur, user_id, character_id, event_ids):
    if not event_ids:
        return {}
    cur.execute(
        '''SELECT id, source_event_type, source_event_id, source, payload
           FROM cognitive_events
           WHERE user_id = %s AND character_id = %s AND id = ANY(%s)''',
        (user_id, character_id, sorted(event_ids)),
    )
    result = {}
    for row in cur.fetchall():
        payload = row[4]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                payload = {}
        result[int(row[0])] = {
            'source_event_type': row[1],
            'source_event_id': row[2],
            'source': row[3],
            'payload': payload if isinstance(payload, dict) else {},
        }
    return result


def _event_evidence_category(metadata):
    payload = metadata.get('payload') if isinstance(metadata, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return payload.get('evidence_category') or metadata.get('source_event_type')


def _belief_commit_decision(update, source_refs, event_metadata, hypothesis_id):
    independent = set()
    categories = []
    for ref in source_refs:
        event_id = ref['event_id']
        metadata = event_metadata.get(event_id, {})
        if metadata:
            independent.add(
                f'{metadata.get("source_event_type")}:{metadata.get("source_event_id")}'
            )
        else:
            independent.add(f'event:{event_id}')
        categories.append(_event_evidence_category(metadata))

    base = {
        'belief_key': update['belief_key'],
        'from_hypothesis_key': update['from_hypothesis_key'],
        'confidence': update['confidence'],
        'independent_evidence_count': len(independent),
        'required_confidence': COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE,
        'required_independent_evidence_count': (
            COGNITIVE_BELIEF_COMMIT_MIN_INDEPENDENT_EVIDENCE
        ),
    }
    if hypothesis_id is None:
        return {**base, 'action': 'held', 'reason': 'missing_hypothesis'}
    if update['status'] == 'retracted':
        return {**base, 'action': 'retracted', 'reason': 'retraction_update'}
    if update['confidence'] < COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE:
        return {**base, 'action': 'held', 'reason': 'confidence_below_threshold'}
    if len(independent) < COGNITIVE_BELIEF_COMMIT_MIN_INDEPENDENT_EVIDENCE:
        return {
            **base,
            'action': 'held',
            'reason': 'insufficient_independent_evidence',
        }
    if categories and all(category == 'character_self_claim' for category in categories):
        return {
            **base,
            'action': 'held',
            'reason': 'character_self_claim_only',
        }
    return {**base, 'action': 'committed', 'reason': 'commit_gate_passed'}


def persist_slow_loop_output(
    cur, *, cycle_id, user_id, character_id, output, now,
):
    """Apply durable questions, hypotheses, predictions, and gated beliefs."""
    refs_by_id = {
        item['event_id']: item for item in output['evidence_refs']
    }

    def full_refs(event_ids):
        return [refs_by_id[event_id] for event_id in event_ids]

    all_referenced_event_ids = {
        int(item['event_id']) for item in output['evidence_refs']
    }
    event_metadata = _load_event_metadata(
        cur, user_id, character_id, all_referenced_event_ids,
    )

    question_ids = {}
    for update in output['question_updates']:
        source_refs = full_refs(update['evidence_refs'])
        metadata = {
            'updated_by': 'cognitive_slow_loop',
            'lifecycle_separate_from_predictions': True,
        }
        cur.execute(
            '''INSERT INTO cognitive_questions (
                   user_id, character_id, question_key, question_text,
                   status, metadata, source_event_refs, created_by_cycle_id,
                   updated_by_cycle_id, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                         %s, %s, %s)
               ON CONFLICT (user_id, character_id, question_key) DO UPDATE
               SET question_text = EXCLUDED.question_text,
                   status = EXCLUDED.status,
                   metadata = EXCLUDED.metadata,
                   source_event_refs = EXCLUDED.source_event_refs,
                   updated_by_cycle_id = EXCLUDED.updated_by_cycle_id,
                   updated_at = EXCLUDED.updated_at
               RETURNING id''',
            (
                user_id, character_id, update['question_key'],
                update['question_text'], update['status'],
                json.dumps(metadata, ensure_ascii=False),
                json.dumps(source_refs, ensure_ascii=False),
                cycle_id, cycle_id, now,
            ),
        )
        question_ids[update['question_key']] = cur.fetchone()[0]

    def resolve_question_id(question_key):
        if question_key in question_ids:
            return question_ids[question_key]
        cur.execute(
            '''SELECT id FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
                 AND question_key = %s''',
            (user_id, character_id, question_key),
        )
        row = cur.fetchone()
        question_id = row[0] if row else None
        if question_id is not None:
            question_ids[question_key] = question_id
        return question_id

    hypothesis_ids = {}
    for update in output['hypothesis_updates']:
        question_id = resolve_question_id(update['question_key'])
        supporting_refs = full_refs(update['supporting_evidence_refs'])
        contradicting_refs = full_refs(update['contradicting_evidence_refs'])
        evidence_entry = [{
            'cycle_id': cycle_id,
            'confidence': update['confidence'],
            'status': update['status'],
            'supporting_evidence_refs': supporting_refs,
            'contradicting_evidence_refs': contradicting_refs,
        }]
        metadata = {
            'updated_by': 'cognitive_slow_loop',
            'confidence_requires_current_evidence': True,
        }
        cur.execute(
            '''INSERT INTO cognitive_hypotheses (
                   user_id, character_id, question_id, hypothesis_key,
                   statement, status, hypothesis_type, confidence,
                   supporting_evidence_refs, contradicting_evidence_refs,
                   evidence, metadata, created_by_cycle_id,
                   updated_by_cycle_id, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                         %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
                         %s, %s, %s)
               ON CONFLICT (user_id, character_id, hypothesis_key) DO UPDATE
               SET question_id = COALESCE(
                       EXCLUDED.question_id, cognitive_hypotheses.question_id),
                   statement = EXCLUDED.statement,
                   status = EXCLUDED.status,
                   hypothesis_type = EXCLUDED.hypothesis_type,
                   confidence = EXCLUDED.confidence,
                   supporting_evidence_refs =
                       cognitive_hypotheses.supporting_evidence_refs
                       || EXCLUDED.supporting_evidence_refs,
                   contradicting_evidence_refs =
                       cognitive_hypotheses.contradicting_evidence_refs
                       || EXCLUDED.contradicting_evidence_refs,
                   evidence = cognitive_hypotheses.evidence || EXCLUDED.evidence,
                   metadata = EXCLUDED.metadata,
                   updated_by_cycle_id = EXCLUDED.updated_by_cycle_id,
                   updated_at = EXCLUDED.updated_at
               RETURNING id''',
            (
                user_id, character_id, question_id,
                update['hypothesis_key'], update['statement'], update['status'],
                update['hypothesis_type'], update['confidence'],
                json.dumps(supporting_refs, ensure_ascii=False),
                json.dumps(contradicting_refs, ensure_ascii=False),
                json.dumps(evidence_entry, ensure_ascii=False),
                json.dumps(metadata, ensure_ascii=False),
                cycle_id, cycle_id, now,
            ),
        )
        hypothesis_ids[update['hypothesis_key']] = cur.fetchone()[0]

    def resolve_hypothesis_id(hypothesis_key):
        if hypothesis_key in hypothesis_ids:
            return hypothesis_ids[hypothesis_key]
        cur.execute(
            '''SELECT id FROM cognitive_hypotheses
               WHERE user_id = %s AND character_id = %s
                 AND hypothesis_key = %s''',
            (user_id, character_id, hypothesis_key),
        )
        row = cur.fetchone()
        hypothesis_id = row[0] if row else None
        if hypothesis_id is not None:
            hypothesis_ids[hypothesis_key] = hypothesis_id
        return hypothesis_id

    belief_commit_decisions = []
    for update in output['belief_updates']:
        hypothesis_id = resolve_hypothesis_id(update['from_hypothesis_key'])
        source_refs = full_refs(update['evidence_refs'])
        decision = _belief_commit_decision(
            update, source_refs, event_metadata, hypothesis_id,
        )
        belief_commit_decisions.append(decision)
        if decision['action'] == 'held':
            continue
        metadata = {
            'created_by': 'cognitive_slow_loop',
            'commit_gate': decision,
        }
        cur.execute(
            '''INSERT INTO cognitive_beliefs (
                   user_id, character_id, belief_key, statement, confidence,
                   status, belief_type, evidence_refs,
                   committed_from_hypothesis_id, metadata,
                   created_by_cycle_id, updated_by_cycle_id, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                         %s, %s::jsonb, %s, %s, %s)
               ON CONFLICT (user_id, character_id, belief_key) DO UPDATE
               SET statement = EXCLUDED.statement,
                   confidence = EXCLUDED.confidence,
                   status = EXCLUDED.status,
                   belief_type = EXCLUDED.belief_type,
                   evidence_refs = EXCLUDED.evidence_refs,
                   committed_from_hypothesis_id =
                       EXCLUDED.committed_from_hypothesis_id,
                   metadata = EXCLUDED.metadata,
                   updated_by_cycle_id = EXCLUDED.updated_by_cycle_id,
                   updated_at = EXCLUDED.updated_at''',
            (
                user_id, character_id, update['belief_key'],
                update['statement'], update['confidence'], update['status'],
                update['belief_type'],
                json.dumps(source_refs, ensure_ascii=False),
                hypothesis_id, json.dumps(metadata, ensure_ascii=False),
                cycle_id, cycle_id, now,
            ),
        )

    for prediction in output['new_predictions']:
        question_id = resolve_question_id(prediction['question_key'])
        hypothesis_id = resolve_hypothesis_id(prediction['hypothesis_key'])
        metadata = dict(prediction['metadata'])
        metadata.update({
            'created_by': 'cognitive_slow_loop',
            'question_key': prediction['question_key'],
            'hypothesis_key': prediction['hypothesis_key'],
            'evidence_refs': full_refs(prediction['evidence_refs']),
        })
        cur.execute(
            '''INSERT INTO cognitive_predictions (
                   user_id, character_id, question_id, hypothesis_id,
                   prediction_key, resolver_name, fulfillment_operator,
                   fulfillment_value, violation_operator, violation_value,
                   expires_at, created_by_cycle_id, metadata
               ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                         %s, %s::jsonb)
               ON CONFLICT (user_id, character_id, prediction_key) DO NOTHING''',
            (
                user_id, character_id, question_id, hypothesis_id,
                prediction['prediction_key'], prediction['resolver_name'],
                prediction['fulfillment_operator'],
                prediction['fulfillment_value'],
                prediction['violation_operator'], prediction['violation_value'],
                now + timedelta(seconds=prediction['expires_in_seconds']),
                cycle_id, json.dumps(metadata, ensure_ascii=False),
            ),
        )

    for note in output.get('sticky_note_updates', []):
        source_refs = full_refs(note['evidence_refs'])
        expires_at = (
            now + timedelta(seconds=note['expires_in_seconds'])
            if note.get('expires_in_seconds') and note['status'] == 'active'
            else None
        )
        completed_at = now if note['status'] == 'completed' else None
        cur.execute(
            '''INSERT INTO cognitive_sticky_notes (
                   user_id, character_id, note_key, content, status, source,
                   source_event_refs, created_by_cycle_id,
                   updated_by_cycle_id, expires_at, completed_at, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s)
               ON CONFLICT (user_id, character_id, note_key) DO UPDATE
               SET content = EXCLUDED.content,
                   status = EXCLUDED.status,
                   source = EXCLUDED.source,
                   source_event_refs = EXCLUDED.source_event_refs,
                   updated_by_cycle_id = EXCLUDED.updated_by_cycle_id,
                   expires_at = EXCLUDED.expires_at,
                   completed_at = COALESCE(
                       EXCLUDED.completed_at,
                       cognitive_sticky_notes.completed_at),
                   updated_at = EXCLUDED.updated_at''',
            (
                user_id, character_id, note['note_key'], note['content'],
                note['status'], 'cognitive_slow_loop',
                json.dumps(source_refs, ensure_ascii=False),
                cycle_id, cycle_id, expires_at, completed_at, now,
            ),
        )

    for entry in output.get('diary_entries', []):
        source_refs = full_refs(entry['evidence_refs'])
        cur.execute(
            '''INSERT INTO cognitive_diary_entries (
                   user_id, character_id, diary_key, content, reflection_kind,
                   source, source_event_refs, created_by_cycle_id, occurred_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
               ON CONFLICT (user_id, character_id, diary_key) DO NOTHING''',
            (
                user_id, character_id, entry['diary_key'], entry['content'],
                entry['reflection_kind'], 'cognitive_slow_loop',
                json.dumps(source_refs, ensure_ascii=False), cycle_id, now,
            ),
        )

    return belief_commit_decisions
