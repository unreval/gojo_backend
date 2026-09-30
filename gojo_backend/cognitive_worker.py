"""Event-driven cognitive worker. Decisions come only from program rules."""
import re
import threading
from datetime import datetime, timezone

from cognitive_config import (
    COGNITIVE_REFLECTION_SCAN_SECONDS, COGNITIVE_WORKER_ENABLED,
    COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS, COGNITIVE_WORKER_POLL_SECONDS,
    PREDICTION_RESOLVER_WHITELIST,
)
from cognitive_queue import (
    aggregate_pending_triggers, build_reasoning_context, claim_next_cycle,
    commit_cycle_success, fail_cycle,
)
from cognitive_scheduler import enqueue_due_reflections
from cognitive_output import validate_slow_loop_output
from cognitive_revision import deterministic_cycle_output

_THREAD = None
_STOP = threading.Event()
_LAST_REFLECTION_SCAN_AT = None


def _utc_now():
    return datetime.now(timezone.utc)


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
    """No model authority, including when a legacy caller supplies a client."""
    output = deterministic_cycle_output(context)
    ids = [r['event_id'] for r in output['evidence_refs']]
    return validate_slow_loop_output(output, allowed_event_ids=ids,
        current_event_ids=ids), {'model_calls': 0, 'input_tokens': 0, 'output_tokens': 0}


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
        for trigger in context.get('triggers', []):
            payload = trigger.get('payload') or {}
            if payload.get('reason') in {'pending_answer', 'resolution'}:
                print('[cognitive_trace] '
                      f'question_id={payload.get("question_id")} '
                      f'trigger=question_reactivation cycle_id={cycle_id} '
                      'status=processing', flush=True)
        output, usage = generate_cycle_output(
            context, create_chat_fn=create_chat_fn,
        )
        result = commit_cycle_success(
            cycle_id,
            reasoning_context=context,
            structured_output=output,
            worker_model='deterministic_evidence_policy_v1',
            worker_usage=usage,
            now=now,
        )
        summary = (result.get('output') or output)['cycle_summary']['summary'][:120]
        print(
            f'[cognitive_worker] cycle #{cycle_id} succeeded: {summary}',
            flush=True,
        )
        return result
    except Exception as exc:
        error_code = _worker_error_code(exc)
        try:
            result = fail_cycle(
                cycle_id, error_code, now=now,
                preserve_raw_evidence=True,
            )
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
            if result.get('status') == 'failed':
                wait_seconds = COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS
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
        '(policy=deterministic_evidence_policy_v1, '
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
