"""Output schema compatibility validators and canonical-only persistence."""
import json
import re

from cognitive_config import (
    COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE,
    COGNITIVE_BELIEF_COMMIT_MIN_INDEPENDENT_EVIDENCE,
    PREDICTION_NUMERIC_OPERATORS,
    PREDICTION_RESOLVER_WHITELIST,
    USER_FACING_STICKY_SOURCE,
    get_sticky_note_ttl_config,
)
from cognitive_predictions import validate_signal_prediction_contract
from relationship_semantics import (
    ENGAGEMENT_STYLE_KEY,
    INTERNAL_CONFLICT_KEY,
    ROMANTIC_LABEL_KEY,
    ROMANTIC_LABEL_VALUES,
    ROMANTIC_OPENNESS_KEY,
)
from cognitive_revision import (
    EVIDENCE_RELATIONS,
    EVIDENCE_STRENGTHS,
    FRAME_KINDS,
    apply_confidence_delta,
    is_episodic_statement,
    is_global_personality_judgment,
    looks_like_belief_revision_sticky,
    normalize_scope,
    review_status_after_confidence,
    scope_exceeds_evidence,
)
from structured_output import parse_structured_output


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
_STICKY_AUDIT_MARKERS = (
    '本轮',
    '构成',
    '验证',
    '观察到',
    '需观察',
    '应关注',
    '关系状态',
    '预测',
    '证据',
    '互动周期',
    '下次回复前记得',
)
_STICKY_TAXONOMY_RE = re.compile(
    r'(?i)(?:self_disclosure|character_reciprocal|relationship_confirm)'
    r'|(?:character|user|boundary|observe)\.[a-z][a-z0-9._-]*'
)
_STICKY_NARRATOR_RE = re.compile(r'(?:^|[，。；！？\n])(?:用户|角色)')
_STICKY_SUMMARY_FLOW_RE = re.compile(
    r'(?:她|他|用户).{0,32}(?:说|表示|告诉|问|做了).{0,80}'
    r'(?:(?:我|角色).{0,32}(?:说|回答|回复|告诉)|后来|然后)'
)
STICKY_NOTE_EMOTIONS = frozenset({
    '平静', '调皮', '无奈', '得意', '嫌弃', '心动', '感慨',
    '嘲讽', '自嘲', '疑惑', '开心', '温柔', '愤怒', '悲伤',
    '认真', '警惕', '嘴硬', '弱情绪', '别扭', '在意', '烦躁',
    '松口气', '松了口气',
})
STICKY_NOTE_TONES = frozenset({
    '平静', '调皮', '无奈', '得意', '嫌弃', '心动', '感慨',
    '嘲讽', '自嘲', '疑惑', '开心', '温柔', '愤怒', '悲伤',
    '认真', '警惕', '嘴硬', '弱情绪', '别扭', '在意', '烦躁',
    '松口气', '松了口气',
})
STICKY_NOTE_EMOTION_TAGS = {
    '平静': '·',
    '调皮': 'hh',
    '无奈': '..',
    '得意': '哼',
    '嫌弃': 'tsk',
    '心动': '♡',
    '感慨': '...',
    '嘲讽': '呵',
    '自嘲': 'hah',
    '疑惑': '?',
    '开心': '!',
    '温柔': '♡',
    '愤怒': '!!',
    '悲伤': '..',
    '认真': '·',
    '警惕': '!',
    '嘴硬': '~',
    '弱情绪': '·',
    '别扭': '~',
    '在意': '♡',
    '烦躁': '!',
    '松口气': '...',
    '松了口气': '...',
}


def sticky_emotion_tag(emotion):
    return STICKY_NOTE_EMOTION_TAGS.get(emotion, '·')


class SlowLoopOutputError(ValueError):
    def __init__(self, message, *, details=None):
        super().__init__(message)
        self.code = message
        self.details = dict(details or {})


_TTL_MISSING = object()


def _ttl_type(value):
    if value is _TTL_MISSING:
        return 'missing'
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, int):
        return 'integer'
    if isinstance(value, float):
        return 'float'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, list):
        return 'array'
    return 'other'


def _sticky_ttl_details(index, status, source, value, config, category):
    details = {
        'error_category': category,
        'field_path': f'sticky_note_updates[{index}].expires_in_seconds',
        'update_index': index,
        'status': status,
        'ttl_source': source,
        'ttl_type': _ttl_type(value),
        'ttl_min': config['minimum'],
        'ttl_max': config['maximum'],
    }
    if (not isinstance(value, bool)
            and isinstance(value, (int, float))):
        details['ttl_value'] = value
    return details


def _normalize_sticky_ttl(update, index, status, config, diagnostics):
    supplied = 'expires_in_seconds' in update
    raw_ttl = update.get('expires_in_seconds') if supplied else _TTL_MISSING

    if status == 'active':
        if raw_ttl is _TTL_MISSING or raw_ttl is None:
            return config['default']
        if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int):
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_ttl_invalid',
                details=_sticky_ttl_details(
                    index, status, 'model', raw_ttl, config, 'ttl_invalid',
                ),
            )
        if raw_ttl < config['minimum'] or raw_ttl > config['maximum']:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_ttl_out_of_range',
                details=_sticky_ttl_details(
                    index, status, 'model', raw_ttl, config,
                    'ttl_out_of_range',
                ),
            )
        return raw_ttl

    if raw_ttl is _TTL_MISSING or raw_ttl is None:
        return None
    if isinstance(raw_ttl, bool) or not isinstance(raw_ttl, int):
        raise SlowLoopOutputError(
            f'sticky_note_update_{index}_ttl_invalid',
            details=_sticky_ttl_details(
                index, status, 'model', raw_ttl, config, 'ttl_invalid',
            ),
        )
    if raw_ttl == 0:
        diagnostics.append(_sticky_ttl_details(
            index, status, 'model', raw_ttl, config,
            'inactive_zero_normalized',
        ))
        return None
    if config['minimum'] <= raw_ttl <= config['maximum']:
        diagnostics.append(_sticky_ttl_details(
            index, status, 'model', raw_ttl, config, 'inactive_ttl_ignored',
        ))
        return None
    raise SlowLoopOutputError(
        f'sticky_note_update_{index}_ttl_out_of_range',
        details=_sticky_ttl_details(
            index, status, 'model', raw_ttl, config, 'ttl_out_of_range',
        ),
    )


def is_user_facing_sticky_content(text):
    """True when sticky content reads as the character's private note."""
    content = str(text or '').strip()
    if not content:
        return False
    if _STICKY_NARRATOR_RE.search(content):
        return False
    lowered = content.lower()
    for marker in _STICKY_AUDIT_MARKERS:
        needle = marker.lower() if marker.isascii() else marker
        haystack = lowered if marker.isascii() else content
        if needle in haystack:
            return False
    if _STICKY_TAXONOMY_RE.search(content):
        return False
    if _STICKY_SUMMARY_FLOW_RE.search(content):
        return False
    return True


def _slow_loop_parse_error(result):
    code = result.error_code or 'structured_output_failed'
    public_code = {
        'empty_response': 'model_output_missing_json',
        'no_top_level_json_object': 'model_output_missing_json',
        'incomplete_json': 'model_output_invalid_json',
        'invalid_json': 'model_output_invalid_json',
        'root_not_object': 'model_output_root_not_object',
        'multiple_distinct_json_objects': 'model_output_ambiguous_json',
        'truncated_response': 'model_output_truncated',
        'model_refused': 'model_output_refused',
    }.get(code)
    if code == 'schema_validation_failed':
        public_code = result.error_detail or 'model_output_schema_invalid'
    if not public_code:
        public_code = 'model_output_invalid_json'
    details = {'structured_output': result.telemetry()}
    if result.schema_details:
        details.update(result.schema_details)
    return SlowLoopOutputError(public_code, details=details)


def parse_slow_loop_output(raw=None, *, parse_result=None):
    """Use the canonical parser and never select one of several model roots."""
    result = parse_result or parse_structured_output(
        raw, schema_name='slow_loop_output')
    if not result.ok:
        raise _slow_loop_parse_error(result)
    return result.value


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


def _clip_label(value, field, maximum, *, allow_empty=True):
    if value is None:
        return ''
    if not isinstance(value, str):
        raise SlowLoopOutputError(f'{field}_must_be_string')
    result = value.strip()[:maximum]
    if not result and not allow_empty:
        raise SlowLoopOutputError(f'{field}_must_not_be_empty')
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


def _validate_relationship_semantic_contract(
        question_updates, belief_updates, hypothesis_updates):
    """Keep the narrow relationship vocabulary in the existing Cognitive path.

    This is a schema guard, not a scorer: normal evidence/reference validation
    above remains responsible for whether a belief or hypothesis is justified.
    """
    for update in question_updates:
        key = update['question_key']
        if key in {ENGAGEMENT_STYLE_KEY, ROMANTIC_OPENNESS_KEY, INTERNAL_CONFLICT_KEY}:
            raise SlowLoopOutputError('relationship_semantic_key_wrong_lifecycle')
        if key != ROMANTIC_LABEL_KEY:
            continue
        judgment = update.get('current_judgment')
        if judgment is not None and judgment['value'] not in ROMANTIC_LABEL_VALUES:
            raise SlowLoopOutputError('relationship_romantic_label_value_invalid')
        if update['status'] == 'resolved' and judgment is None:
            raise SlowLoopOutputError('relationship_romantic_label_resolution_requires_value')

    for update in belief_updates:
        key = update['belief_key']
        if key in {ROMANTIC_LABEL_KEY, ROMANTIC_OPENNESS_KEY}:
            raise SlowLoopOutputError('relationship_semantic_key_wrong_lifecycle')
        if key in {ENGAGEMENT_STYLE_KEY, INTERNAL_CONFLICT_KEY}:
            if update['belief_type'] != 'relationship_observation':
                raise SlowLoopOutputError('relationship_observation_belief_type_invalid')

    for update in hypothesis_updates:
        key = update['hypothesis_key']
        if key == ROMANTIC_LABEL_KEY:
            raise SlowLoopOutputError('relationship_semantic_key_wrong_lifecycle')
        if key in {ENGAGEMENT_STYLE_KEY, ROMANTIC_OPENNESS_KEY, INTERNAL_CONFLICT_KEY}:
            if update['hypothesis_type'] != 'relationship':
                raise SlowLoopOutputError('relationship_semantic_hypothesis_type_invalid')


def validate_slow_loop_output(value, *, allowed_event_ids, current_event_ids=None,
                              diagnostics=None):
    """Return a normalized output or reject any ungrounded/model-invented field."""
    sticky_ttl_config = get_sticky_note_ttl_config()
    ttl_diagnostics = diagnostics if diagnostics is not None else []
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
        if not expected.issubset(update) or set(update) - expected - {'current_judgment'}:
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
        if 'current_judgment' in update:
            judgment = update['current_judgment']
            if judgment is not None:
                judgment = _object(judgment, f'question_update_{index}_current_judgment')
                if set(judgment) != {'value', 'content', 'status'}:
                    raise SlowLoopOutputError(f'question_update_{index}_judgment_fields_invalid')
                if judgment['status'] not in {'current', 'committed'}:
                    raise SlowLoopOutputError(f'question_update_{index}_judgment_status_invalid')
                judgment = {
                    'value': _text(judgment['value'], f'question_update_{index}_judgment_value', 80),
                    'content': _text(judgment['content'], f'question_update_{index}_judgment_content', 600),
                    'status': judgment['status'],
                }
            question_updates[-1]['current_judgment'] = judgment

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

    _validate_relationship_semantic_contract(
        question_updates, belief_updates, hypothesis_updates,
    )

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
        optional = {
            'expires_in_seconds', 'emotion', 'trigger_snippet', 'tone', 'tag',
            'question_key',
        }
        if not required.issubset(update) or set(update) - required - optional:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_fields_invalid',
            )
        key = _key(update['note_key'], f'sticky_note_update_{index}_key')
        if key in sticky_keys:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_duplicate_key',
            )
        if key.startswith('memory_lifecycle.'):
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_reserved_namespace',
            )
        sticky_keys.add(key)
        status = _text(update['status'], f'sticky_note_update_{index}_status', 16)
        if status not in STICKY_NOTE_STATUSES:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_status_invalid',
            )
        ttl = _normalize_sticky_ttl(
            update, index, status, sticky_ttl_config, ttl_diagnostics,
        )
        refs = _update_refs(
            update['evidence_refs'],
            f'sticky_note_update_{index}_evidence_refs',
            allowed_ids, declared_ids,
        )
        _require_current_ref(
            refs, f'sticky_note_update_{index}_evidence_refs', current_ids,
        )
        content = _text(
            update['content'],
            f'sticky_note_update_{index}_content',
            180,
        )
        emotion = _clip_label(
            update.get('emotion'), f'sticky_note_update_{index}_emotion', 24,
        )
        if emotion and emotion not in STICKY_NOTE_EMOTIONS:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_emotion_invalid',
            )
        tone = _clip_label(
            update.get('tone'), f'sticky_note_update_{index}_tone', 24,
        )
        if tone and tone not in STICKY_NOTE_TONES:
            raise SlowLoopOutputError(
                f'sticky_note_update_{index}_tone_invalid',
            )
        trigger_snippet = _clip_label(
            update.get('trigger_snippet'),
            f'sticky_note_update_{index}_trigger_snippet',
            160,
        )
        tag = _clip_label(
            update.get('tag') or sticky_emotion_tag(emotion),
            f'sticky_note_update_{index}_tag',
            8,
        )
        sticky_note_updates.append({
            'note_key': key,
            'content': content,
            'emotion': emotion,
            'tone': tone,
            'trigger_snippet': trigger_snippet,
            'tag': tag,
            'status': status,
            'expires_in_seconds': ttl,
            'evidence_refs': refs,
            'question_key': (_key(update['question_key'],
                                  f'sticky_note_update_{index}_question_key')
                             if update.get('question_key') else None),
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

    visible_sticky_updates = []
    for sticky in sticky_note_updates:
        if (
            sticky['status'] == 'active'
            and not is_user_facing_sticky_content(sticky['content'])
        ):
            print(
                '[cognitive] dropped non-user-facing sticky '
                f"note_key={sticky['note_key']}"
            )
            continue
        visible_sticky_updates.append(sticky)
    sticky_note_updates = visible_sticky_updates

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




def _event_evidence_category(metadata):
    payload = metadata.get('payload') if isinstance(metadata, dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return payload.get('evidence_category') or metadata.get('source_event_type')






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


def _event_id_set(refs):
    ids = set()
    for ref in refs or []:
        if isinstance(ref, dict):
            value = ref.get('event_id')
            if value is None:
                value = ref.get('source_id')
            try:
                ids.add(int(value))
            except (TypeError, ValueError):
                text = str(value or '').strip()
                if text:
                    ids.add(text)
        else:
            try:
                ids.add(int(ref))
            except (TypeError, ValueError):
                text = str(ref or '').strip()
                if text:
                    ids.add(text)
    return ids


def _diary_entry_is_duplicate(cur, user_id, character_id, entry, source_refs):
    """Same diary_key or highly overlapping evidence must not write twice."""
    cur.execute(
        '''SELECT diary_key, source_event_refs
           FROM cognitive_diary_entries
           WHERE user_id = %s AND character_id = %s
           ORDER BY occurred_at DESC, id DESC
           LIMIT 40''',
        (user_id, character_id),
    )
    incoming_ids = _event_id_set(source_refs) or _event_id_set(entry.get('evidence_refs'))
    incoming_key = (entry.get('diary_key') or '').strip()
    for diary_key, refs in cur.fetchall():
        if incoming_key and diary_key == incoming_key:
            return True
        existing_ids = _event_id_set(_json_refs(refs))
        if not incoming_ids or not existing_ids:
            continue
        overlap = len(incoming_ids & existing_ids) / float(
            min(len(incoming_ids), len(existing_ids)))
        if overlap >= 0.6:
            return True
    return False


def _json_refs(value):
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def persist_slow_loop_output(
    cur, *, cycle_id, user_id, character_id, output, now, deterministic=True,
):
    """Apply durable questions, hypotheses, predictions, and gated beliefs."""
    if deterministic is not True:
        raise SlowLoopOutputError('model_judgment_persistence_disabled')
    from cognitive_revision import apply_rule_evidence
    if any(output.get(key) for key in (
            'question_updates', 'belief_updates', 'hypothesis_updates',
            'new_predictions', 'sticky_note_updates', 'diary_entries')):
        raise SlowLoopOutputError('external_judgment_candidates_not_authoritative')
    return apply_rule_evidence(cur, cycle_id=cycle_id, user_id=user_id,
                               character_id=character_id, output=output, now=now)
