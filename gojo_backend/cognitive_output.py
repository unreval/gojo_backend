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
    USER_FACING_STICKY_SOURCE,
)
from cognitive_predictions import validate_signal_prediction_contract
from cognitive_revision import (
    EVIDENCE_RELATIONS,
    EVIDENCE_STRENGTHS,
    FRAME_KINDS,
    apply_confidence_delta,
    append_revision_history,
    infer_hypothesis_relation,
    is_episodic_statement,
    is_global_personality_judgment,
    looks_like_belief_revision_sticky,
    normalize_scope,
    review_status_after_confidence,
    scope_exceeds_evidence,
)


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
BELIEF_OPTIONAL_FIELDS = frozenset({
    'evidence_relation', 'evidence_strength', 'scope',
    'independent_contexts', 'revision_reason', 'frame_kind',
})
HYPOTHESIS_OPTIONAL_FIELDS = frozenset({
    'scope', 'independent_contexts',
})
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


def _independent_contexts(value, field):
    if value is None:
        return []
    items = _array(value, field, 8)
    result = []
    for index, item in enumerate(items):
        text = _text(item, f'{field}_{index}', 80)
        if text not in result:
            result.append(text)
    return result


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
        extra = set(update) - expected - BELIEF_OPTIONAL_FIELDS
        if not expected.issubset(update) or extra:
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
        relation = update.get('evidence_relation')
        if relation is not None:
            relation = _text(
                relation, f'belief_update_{index}_evidence_relation', 24,
            )
            if relation not in EVIDENCE_RELATIONS:
                raise SlowLoopOutputError(
                    f'belief_update_{index}_evidence_relation_invalid',
                )
        strength = update.get('evidence_strength')
        if strength is not None:
            strength = _text(
                strength, f'belief_update_{index}_evidence_strength', 16,
            )
            if strength not in EVIDENCE_STRENGTHS:
                raise SlowLoopOutputError(
                    f'belief_update_{index}_evidence_strength_invalid',
                )
        frame_kind = update.get('frame_kind')
        if frame_kind is not None:
            frame_kind = _text(
                frame_kind, f'belief_update_{index}_frame_kind', 32,
            )
            if frame_kind not in FRAME_KINDS:
                raise SlowLoopOutputError(
                    f'belief_update_{index}_frame_kind_invalid',
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
            'evidence_relation': relation,
            'evidence_strength': strength or 'normal',
            'scope': normalize_scope(update.get('scope')),
            'independent_contexts': _independent_contexts(
                update.get('independent_contexts'),
                f'belief_update_{index}_independent_contexts',
            ),
            'revision_reason': _text(
                update.get('revision_reason') or '',
                f'belief_update_{index}_revision_reason',
                400,
                allow_empty=True,
            ),
            'frame_kind': frame_kind,
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
        extra = set(update) - required - HYPOTHESIS_OPTIONAL_FIELDS
        if not required.issubset(update) or extra:
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
            'scope': normalize_scope(update.get('scope')),
            'independent_contexts': _independent_contexts(
                update.get('independent_contexts'),
                f'hypothesis_update_{index}_independent_contexts',
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

    revision_relations = {
        item.get('evidence_relation')
        for item in belief_updates
        if item.get('evidence_relation') in {'contradiction', 'scope_limiter'}
    }
    for sticky in sticky_note_updates:
        if looks_like_belief_revision_sticky(sticky['content']) and not revision_relations:
            raise SlowLoopOutputError('sticky_note_cannot_replace_revision')

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


def _json_meta(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _load_keyed_map(cur, table, key_column, user_id, character_id, keys, columns):
    if not keys:
        return {}
    column_sql = ', '.join(columns)
    cur.execute(
        f'''SELECT {key_column}, {column_sql}
            FROM {table}
            WHERE user_id = %s AND character_id = %s
              AND {key_column} = ANY(%s)''',
        (user_id, character_id, list(keys)),
    )
    result = {}
    for row in cur.fetchall() or []:
        result[row[0]] = row[1:]
    return result


def _belief_commit_decision(update, source_refs, event_metadata, hypothesis_id,
                            existing=None):
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

    statement = update['statement']
    relation = update.get('evidence_relation')
    strength = update.get('evidence_strength') or 'normal'
    scope = normalize_scope(update.get('scope'), legacy=bool(existing))
    contexts = update.get('independent_contexts') or []
    proposed = float(update['confidence'])
    final_confidence = proposed
    review_status = 'stable'
    write_statement = statement

    if existing:
        old_confidence = float(existing.get('confidence') or 0)
        if relation not in EVIDENCE_RELATIONS:
            relation = None
            final_confidence = old_confidence
            write_statement = existing.get('statement') or statement
        else:
            if relation == 'irrelevant':
                final_confidence = old_confidence
            else:
                final_confidence = apply_confidence_delta(
                    old_confidence, relation, strength,
                )
            if relation in {'support', 'irrelevant'} or is_episodic_statement(statement):
                write_statement = existing.get('statement') or statement
            else:
                write_statement = statement
        was_committed = existing.get('status') == 'active' and old_confidence >= (
            COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE
        )
        review_status = review_status_after_confidence(
            final_confidence, was_committed=was_committed,
        )
    else:
        write_statement = statement

    base = {
        'belief_key': update['belief_key'],
        'from_hypothesis_key': update['from_hypothesis_key'],
        'confidence': final_confidence,
        'proposed_confidence': proposed,
        'independent_evidence_count': len(independent),
        'required_confidence': COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE,
        'required_independent_evidence_count': (
            COGNITIVE_BELIEF_COMMIT_MIN_INDEPENDENT_EVIDENCE
        ),
        'evidence_relation': relation,
        'scope': scope,
        'review_status': review_status,
        'statement': write_statement,
    }
    if hypothesis_id is None:
        return {**base, 'action': 'held', 'reason': 'missing_hypothesis'}
    if update['status'] == 'retracted':
        return {**base, 'action': 'retracted', 'reason': 'retraction_update'}
    if existing and relation not in EVIDENCE_RELATIONS:
        return {**base, 'action': 'held', 'reason': 'missing_evidence_relation'}
    if not existing and is_episodic_statement(statement):
        return {**base, 'action': 'held', 'reason': 'episodic_not_belief'}
    if not existing and is_global_personality_judgment(statement) and scope_exceeds_evidence(
        'general_tendency', contexts,
    ):
        return {**base, 'action': 'held', 'reason': 'scope_too_wide'}
    if not existing and scope_exceeds_evidence(scope, contexts):
        return {**base, 'action': 'held', 'reason': 'scope_too_wide'}
    if existing and relation == 'irrelevant':
        return {**base, 'action': 'held', 'reason': 'irrelevant_no_change'}
    if not existing and final_confidence < COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE:
        return {**base, 'action': 'held', 'reason': 'confidence_below_threshold'}
    required_independent = (
        1 if existing else COGNITIVE_BELIEF_COMMIT_MIN_INDEPENDENT_EVIDENCE
    )
    if len(independent) < required_independent:
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
    if existing and review_status == 'under_review':
        return {**base, 'action': 'under_review', 'reason': 'confidence_reopened'}
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

    existing_hypotheses = {}
    for key, row in _load_keyed_map(
        cur, 'cognitive_hypotheses', 'hypothesis_key',
        user_id, character_id,
        [item['hypothesis_key'] for item in output['hypothesis_updates']],
        ('statement', 'confidence', 'status', 'metadata'),
    ).items():
        existing_hypotheses[key] = {
            'statement': row[0],
            'confidence': float(row[1] or 0),
            'status': row[2],
            'metadata': _json_meta(row[3]),
        }

    hypothesis_ids = {}
    for update in output['hypothesis_updates']:
        question_id = resolve_question_id(update['question_key'])
        supporting_refs = full_refs(update['supporting_evidence_refs'])
        contradicting_refs = full_refs(update['contradicting_evidence_refs'])
        existing = existing_hypotheses.get(update['hypothesis_key'])
        if existing:
            relation = infer_hypothesis_relation(
                update['supporting_evidence_refs'],
                update['contradicting_evidence_refs'],
            )
            confidence = apply_confidence_delta(
                existing['confidence'], relation, 'normal',
            )
            hypo_meta = append_revision_history(
                existing['metadata'],
                old_statement=existing['statement'],
                old_confidence=existing['confidence'],
                new_statement=update['statement'],
                new_confidence=confidence,
                relation=relation,
                reason='slow_loop_hypothesis_revision',
                evidence_refs=supporting_refs + contradicting_refs,
                cycle_id=cycle_id,
            )
        else:
            confidence = update['confidence']
            hypo_meta = _json_meta(None)
        evidence_entry = [{
            'cycle_id': cycle_id,
            'confidence': confidence,
            'status': update['status'],
            'supporting_evidence_refs': supporting_refs,
            'contradicting_evidence_refs': contradicting_refs,
        }]
        hypo_meta.update({
            'updated_by': 'cognitive_slow_loop',
            'confidence_requires_current_evidence': True,
            'scope': update.get('scope') or 'unknown',
            'independent_contexts': update.get('independent_contexts') or [],
        })
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
                update['hypothesis_type'], confidence,
                json.dumps(supporting_refs, ensure_ascii=False),
                json.dumps(contradicting_refs, ensure_ascii=False),
                json.dumps(evidence_entry, ensure_ascii=False),
                json.dumps(hypo_meta, ensure_ascii=False),
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

    existing_beliefs = {}
    for key, row in _load_keyed_map(
        cur, 'cognitive_beliefs', 'belief_key',
        user_id, character_id,
        [item['belief_key'] for item in output['belief_updates']],
        ('statement', 'confidence', 'status', 'metadata'),
    ).items():
        existing_beliefs[key] = {
            'statement': row[0],
            'confidence': float(row[1] or 0),
            'status': row[2],
            'metadata': _json_meta(row[3]),
        }

    belief_commit_decisions = []
    for update in output['belief_updates']:
        hypothesis_id = resolve_hypothesis_id(update['from_hypothesis_key'])
        source_refs = full_refs(update['evidence_refs'])
        existing = existing_beliefs.get(update['belief_key'])
        decision = _belief_commit_decision(
            update, source_refs, event_metadata, hypothesis_id,
            existing=existing,
        )
        belief_commit_decisions.append(decision)
        if decision['action'] == 'held':
            continue
        statement = decision.get('statement') or update['statement']
        confidence = decision['confidence']
        meta = dict(existing['metadata']) if existing else {}
        if existing:
            meta = append_revision_history(
                meta,
                old_statement=existing['statement'],
                old_confidence=existing['confidence'],
                new_statement=statement,
                new_confidence=confidence,
                relation=decision.get('evidence_relation') or 'support',
                reason=update.get('revision_reason') or decision.get('reason'),
                evidence_refs=source_refs,
                cycle_id=cycle_id,
            )
        meta.update({
            'created_by': 'cognitive_slow_loop',
            'commit_gate': decision,
            'scope': decision.get('scope') or update.get('scope') or 'unknown',
            'review_status': decision.get('review_status') or 'stable',
            'independent_contexts': update.get('independent_contexts') or [],
            'frame_kind': update.get('frame_kind'),
        })
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
                statement, confidence, update['status'],
                update['belief_type'],
                json.dumps(source_refs, ensure_ascii=False),
                hypothesis_id, json.dumps(meta, ensure_ascii=False),
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
                   viewed = CASE
                       WHEN cognitive_sticky_notes.content IS DISTINCT FROM
                            EXCLUDED.content THEN FALSE
                       ELSE cognitive_sticky_notes.viewed
                   END,
                   viewed_at = CASE
                       WHEN cognitive_sticky_notes.content IS DISTINCT FROM
                            EXCLUDED.content THEN NULL
                       ELSE cognitive_sticky_notes.viewed_at
                   END,
                   user_hidden_at = CASE
                       WHEN cognitive_sticky_notes.content IS DISTINCT FROM
                            EXCLUDED.content THEN NULL
                       ELSE cognitive_sticky_notes.user_hidden_at
                   END,
                   user_visible = CASE
                       WHEN cognitive_sticky_notes.content IS DISTINCT FROM
                            EXCLUDED.content THEN TRUE
                       ELSE cognitive_sticky_notes.user_visible
                   END,
                   updated_at = EXCLUDED.updated_at''',
            (
                user_id, character_id, note['note_key'], note['content'],
                note['status'], USER_FACING_STICKY_SOURCE,
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
