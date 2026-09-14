"""Background Slow Loop worker for auditable cognitive consolidation."""
import json
import re
import threading
from datetime import datetime, timezone

from cognitive_config import (
    COGNITIVE_REFLECTION_SCAN_SECONDS,
    COGNITIVE_WORKER_ENABLED,
    COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS,
    COGNITIVE_WORKER_MAX_TOKENS,
    COGNITIVE_WORKER_MODEL,
    COGNITIVE_WORKER_MODEL_ATTEMPTS,
    COGNITIVE_WORKER_POLL_SECONDS,
    PREDICTION_RESOLVER_WHITELIST,
)
from cognitive_output import (
    SlowLoopOutputError,
    parse_slow_loop_output,
    validate_slow_loop_output,
)
from cognitive_queue import (
    aggregate_pending_triggers,
    build_reasoning_context,
    claim_next_cycle,
    commit_cycle_success,
    fail_cycle,
)
from cognitive_scheduler import enqueue_due_reflections


_THREAD = None
_STOP = threading.Event()
_LAST_REFLECTION_SCAN_AT = None


_SYSTEM_PROMPT = '''You are the Slow Loop consolidation worker for a fictional
character memory system. You receive only auditable facts, prior beliefs,
hypotheses, and deterministic prediction results.

Your job is to consolidate evidence. Do not write dialogue, choose a next
response, prescribe an emotion, modify relationship scores, or claim access to
hidden mental states. Distinguish direct evidence from hypotheses. A user saying
something about the character does not prove the character reciprocates it.

Return one JSON object and no markdown. It must contain these root keys:
cycle_summary, belief_updates, hypothesis_updates, new_predictions,
evidence_refs.
It may also contain optional root keys sticky_note_updates and diary_entries.

Schema:
{
  "cycle_summary": {
    "summary": "concise Chinese factual synthesis",
    "salient_change": "what changed from prior knowledge, or empty string",
    "uncertainty": "what remains unknown, or empty string",
    "confidence": "low|medium|high"
  },
  "belief_updates": [{
    "belief_key": "stable.lowercase.key",
    "statement": "durable factual belief in Chinese",
    "confidence": 0.0,
    "status": "active|retracted",
    "evidence_refs": [123]
  }],
  "hypothesis_updates": [{
    "hypothesis_key": "stable.lowercase.key",
    "statement": "testable interpretation in Chinese",
    "status": "open|supported|rejected|archived",
    "question_key": null,
    "evidence_refs": [123]
  }],
  "new_predictions": [{
    "prediction_key": "stable.lowercase.key",
    "resolver_name": "current_event_signal_outcome",
    "fulfillment_operator": ">=",
    "fulfillment_value": 1,
    "violation_operator": "<=",
    "violation_value": -1,
    "expires_in_seconds": 86400,
    "hypothesis_key": null,
    "metadata": {
      "description": "falsifiable expectation",
      "fulfillment_signals": [{
        "signal_type": "character_reciprocal",
        "actor": "character"
      }],
      "violation_signals": [{
        "signal_type": "character_stance_declared",
        "actor": "character",
        "attributes": {"stance_type": "retreat_boundary"}
      }]
    },
    "evidence_refs": [123]
  }],
  "evidence_refs": [{"event_id": 123, "reason": "why it supports output"}]
}

Optional sticky_note_updates schema:
[{
  "note_key": "stable.lowercase.key",
  "content": "short visible note for the character to remember soon",
  "status": "active|completed|expired|archived",
  "expires_in_seconds": 259200,
  "evidence_refs": [123]
}]

Sticky notes are lightweight short-term, visible/callable reminders. They are
not long-term memory, relationship state, or proof of affection. Use them for
near-future reminders, unresolved conversational threads, and small role-local
to-dos. Mark a note completed/expired only when current evidence supports that
lifecycle change. Every note must cite source events.

Optional diary_entries schema:
[{
  "diary_key": "stable.lowercase.key",
  "content": "first-person Chinese reflection grounded only in cited events",
  "reflection_kind": "event|periodic|repair|uncertainty",
  "evidence_refs": [123]
}]

Diary entries are cognitive output and later memory input only. They must not
modify relationship_model, rel_state, or relationship scores. They are not
evidence for themselves; do not use prior diary wording to prove a new belief.
Write only when cited events justify a reflective record.

Every referenced event_id must exist in the supplied context. Historical IDs
may be reused only when they already appear in a prior belief, hypothesis, or
prediction evidence_refs. Every update, prediction, sticky note, and diary
entry must cite at least one event declared in top-level evidence_refs. A scheduled_reflection event is a
clock tick, not factual evidence by itself. Use empty update arrays when the
evidence does not justify a change. Never invent IDs.

The only prediction resolver is current_event_signal_outcome. It checks the
next extracted relationship signals against declarative selectors. Use exactly
fulfillment >= 1 and violation <= -1. Each selector must contain signal_type
and actor; confidence and a subset of attributes are optional. Both selector
lists must be non-empty. Predict an observable future signal, not a hidden
feeling, message count, elapsed time, relationship score, or final romantic
outcome. Omit predictions that cannot be represented this way.

When settled_predictions are present, use their linked hypothesis_key,
description, selectors, status, and the settling event as a feedback signal.
A fulfillment may support a hypothesis and a violation may weaken or reject
it, but one observation is not automatically conclusive. Preserve uncertainty
and cite the actual event, not the prediction record, as evidence.'''


def _utc_now():
    return datetime.now(timezone.utc)


def _serialize_context(context):
    return json.dumps(context, ensure_ascii=False, indent=2, default=str)


def _event_ids(context):
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

    visit(context)
    return result


def _worker_error_code(exc):
    message = re.sub(r'[^a-zA-Z0-9_.:-]+', '_', str(exc)).strip('_')
    kind = exc.__class__.__name__.lower()
    return f'slow_loop_{kind}:{message or "failed"}'[:180]


def _load_pairs_requiring_maintenance(now=None):
    from db import get_conn

    current_time = now or _utc_now()
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT DISTINCT user_id, character_id
               FROM cognitive_event_triggers
               WHERE status = 'pending'
                  OR (status = 'claimed' AND claim_expires_at <= %s)
               ORDER BY user_id, character_id
               LIMIT 200''',
            (current_time,),
        )
        return cur.fetchall()
    finally:
        cur.close()
        conn.close()


def maintain_pending_cycles(now=None):
    """Recover expired leases and aggregate ready trigger pairs."""
    results = []
    for user_id, character_id in _load_pairs_requiring_maintenance(now=now):
        try:
            result = aggregate_pending_triggers(
                user_id, character_id, now=now,
            )
            results.append({
                'user_id': user_id,
                'character_id': character_id,
                'result': result,
            })
        except Exception as exc:
            print(
                f'[cognitive_worker] maintenance failed for '
                f'{user_id}/{character_id}: {exc}',
                flush=True,
            )
    return results


def maintain_scheduled_reflections(now=None):
    """Periodically create idempotent reflection events for active pairs."""
    global _LAST_REFLECTION_SCAN_AT
    current_time = now or _utc_now()
    if (
        _LAST_REFLECTION_SCAN_AT is not None
        and (current_time - _LAST_REFLECTION_SCAN_AT).total_seconds()
        < COGNITIVE_REFLECTION_SCAN_SECONDS
    ):
        return []
    results = enqueue_due_reflections(scheduled_for=current_time)
    _LAST_REFLECTION_SCAN_AT = current_time
    return results


def generate_cycle_output(context, *, create_chat_fn=None):
    """Call the model outside any database lock and validate its output."""
    if create_chat_fn is None:
        # Keep maintenance and test imports independent from production model
        # client dependencies until a cycle actually needs the model.
        from ai_client import create_chat as call_model
    else:
        call_model = create_chat_fn
    allowed_event_ids = _event_ids(context)
    messages = [{
        'role': 'user',
        'content': 'Consolidate this cognitive cycle:\n' + _serialize_context(context),
    }]
    last_error = None
    for attempt in range(COGNITIVE_WORKER_MODEL_ATTEMPTS):
        raw, usage = call_model(
            model=COGNITIVE_WORKER_MODEL,
            messages=messages,
            system=_SYSTEM_PROMPT,
            max_tokens=COGNITIVE_WORKER_MAX_TOKENS,
        )
        try:
            parsed = parse_slow_loop_output(raw)
            output = validate_slow_loop_output(
                parsed, allowed_event_ids=allowed_event_ids,
            )
            return output, usage
        except SlowLoopOutputError as exc:
            last_error = exc
            if attempt + 1 >= COGNITIVE_WORKER_MODEL_ATTEMPTS:
                break
            messages.extend([
                {'role': 'assistant', 'content': str(raw or '')[:12000]},
                {
                    'role': 'user',
                    'content': (
                        f'Validation failed: {exc}. Return a corrected JSON '
                        'object matching the schema exactly.'
                    ),
                },
            ])
    raise last_error or SlowLoopOutputError('model_output_validation_failed')


def run_worker_once(*, create_chat_fn=None, now=None):
    """Maintain the queue and process at most one cycle."""
    maintain_scheduled_reflections(now=now)
    maintain_pending_cycles(now=now)
    claimed = claim_next_cycle(now=now)
    if not claimed:
        return {'status': 'idle'}
    if claimed.get('status') != 'running':
        return claimed

    cycle_id = claimed['cycle_id']
    try:
        context = build_reasoning_context(cycle_id)
        output, usage = generate_cycle_output(
            context, create_chat_fn=create_chat_fn,
        )
        result = commit_cycle_success(
            cycle_id,
            reasoning_context=context,
            structured_output=output,
            worker_model=COGNITIVE_WORKER_MODEL,
            worker_usage=usage,
            now=now,
        )
        summary = output['cycle_summary']['summary'][:120]
        print(
            f'[cognitive_worker] cycle #{cycle_id} succeeded: {summary}',
            flush=True,
        )
        return result
    except Exception as exc:
        error_code = _worker_error_code(exc)
        try:
            result = fail_cycle(cycle_id, error_code, now=now)
        except Exception as fail_exc:
            print(
                f'[cognitive_worker] cycle #{cycle_id} failed and could not '
                f'release claims: {fail_exc}',
                flush=True,
            )
            raise
        print(
            f'[cognitive_worker] cycle #{cycle_id} failed: {error_code}',
            flush=True,
        )
        return result


def _loop():
    while not _STOP.is_set():
        wait_seconds = COGNITIVE_WORKER_POLL_SECONDS
        try:
            result = run_worker_once()
            if result.get('status') == 'succeeded':
                continue
        except Exception as exc:
            wait_seconds = COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS
            print(f'[cognitive_worker] loop error: {exc}', flush=True)
        _STOP.wait(wait_seconds)


def start_cognitive_worker():
    global _THREAD
    if not COGNITIVE_WORKER_ENABLED:
        print('[cognitive_worker] disabled by COGNITIVE_WORKER_ENABLED', flush=True)
        return None
    if _THREAD and _THREAD.is_alive():
        return _THREAD
    _STOP.clear()
    _THREAD = threading.Thread(
        target=_loop,
        name='cognitive-slow-loop',
        daemon=True,
    )
    _THREAD.start()
    print(
        '[cognitive_worker] started '
        f'(model={COGNITIVE_WORKER_MODEL}, '
        f'resolvers={sorted(PREDICTION_RESOLVER_WHITELIST)})',
        flush=True,
    )
    return _THREAD


def is_cognitive_worker_running():
    return bool(_THREAD and _THREAD.is_alive())


def stop_cognitive_worker(timeout=5):
    global _THREAD
    _STOP.set()
    if _THREAD and _THREAD.is_alive():
        _THREAD.join(timeout=timeout)
    _THREAD = None
