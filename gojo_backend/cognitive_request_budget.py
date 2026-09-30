"""Transport-only limits for the existing Slow Loop; no evidence/state writes."""
import copy
import json
import os


def _ceiling(name, default, minimum, maximum):
    try:
        value = int(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


# Byte admission is deliberately NOT advertised as an exact token estimate.
MAX_REQUEST_BYTES = _ceiling('COGNITIVE_WORKER_MAX_REQUEST_BYTES', 65536, 4096, 65536)
MAX_OUTPUT_TOKENS = 4096
MAX_MODEL_ATTEMPTS = 2
REQUEST_OVERHEAD_BYTES = 2048
OUTPUT_LIMITS = {
    'question_updates': 2,
    'belief_updates': 2,
    'hypothesis_updates': 2,
    'new_predictions': 1,
    'sticky_note_updates': 2,
    'diary_entries': 1,
}
OUTPUT_INSTRUCTION = '''
Transport budget: return only justified changes, not a full rewrite of prior state.
Return one compact JSON object, without indentation or commentary. Keep all
required root keys; use empty arrays for sections that need no change. At most
2 question_updates, 2 belief_updates, 2 hypothesis_updates, 1 new_prediction,
2 sticky_note_updates, and 1 diary_entry. Keep prose short; do not fill quotas.
Never invent evidence or a judgment to satisfy the budget. Historical evidence
revision logs omitted from this request remain in storage; their omission is
not evidence of absence, a retraction, or permission to resolve a question.
'''


class SlowLoopRequestBudgetError(ValueError):
    pass


_REDUNDANT_EVIDENCE_HISTORY_KEYS = frozenset({
    'cycle_id',
    'confidence',
    'status',
    'supporting_evidence_refs',
    'contradicting_evidence_refs',
})


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), default=str)


def _reference_ids(value):
    """Conservatively track both cognitive IDs and canonical source IDs."""
    if isinstance(value, dict):
        result = set()
        for key in ('event_id', 'source_event_id', 'source_id'):
            identifier = value.get(key)
            if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
                if str(identifier):
                    result.add((key, str(identifier)))
        for child in value.values():
            result.update(_reference_ids(child))
        return result
    if isinstance(value, list):
        result = set()
        for child in value:
            if isinstance(child, int) and not isinstance(child, bool):
                result.add(('event_id', str(child)))
            else:
                result.update(_reference_ids(child))
        return result
    return set()


def project_request_context(context):
    """Drop only redundant hypothesis revision logs from a detached request view.

    Keep every current event, source reference, opposing reference, question,
    resolution, and current-state field. Never truncate quotations or mutate the
    persisted context. If this lossless current-state view is still too large,
    the caller must refuse the API request rather than discard more evidence.
    """
    projected = copy.deepcopy(context)
    for hypothesis in projected.get('current_hypotheses', []):
        if not isinstance(hypothesis, dict):
            continue
        history = hypothesis.get('evidence')
        # Historical evidence may be the only reference in legacy rows. Do not
        # remove it unless both explicit reference channels exist as lists.
        if not (isinstance(history, list)
                and isinstance(hypothesis.get('supporting_evidence_refs'), list)
                and isinstance(hypothesis.get('contradicting_evidence_refs'), list)):
            continue
        visible_refs = _reference_ids([
            hypothesis['supporting_evidence_refs'],
            hypothesis['contradicting_evidence_refs'],
        ])
        retained_history = []
        omitted_count = 0
        for entry in history:
            if (isinstance(entry, dict)
                    and set(entry).issubset(_REDUNDANT_EVIDENCE_HISTORY_KEYS)
                    and _reference_ids(entry).issubset(visible_refs)):
                omitted_count += 1
            else:
                retained_history.append(entry)
        if omitted_count:
            if retained_history:
                hypothesis['evidence'] = retained_history
            else:
                hypothesis.pop('evidence', None)
            hypothesis['evidence_history_omitted_count'] = omitted_count
    return projected


def check_request_budget(system, messages, *, limit=None):
    ceiling = MAX_REQUEST_BYTES if limit is None else min(MAX_REQUEST_BYTES, int(limit))
    size = len(compact_json({'system': system, 'messages': messages}).encode('utf-8'))
    size += REQUEST_OVERHEAD_BYTES
    if size > ceiling:
        raise SlowLoopRequestBudgetError(
            f'request_budget_exceeded:bytes={size}:limit={ceiling}')
    return size


def validate_output_budget(output):
    for field, limit in OUTPUT_LIMITS.items():
        if len(output.get(field) or []) > limit:
            raise SlowLoopRequestBudgetError(f'output_budget_exceeded:{field}:limit={limit}')
    return output


def retry_output_budget(current):
    return min(MAX_OUTPUT_TOKENS, max(int(current) + 512, int(current) * 2))
