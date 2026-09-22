"""Durable worker for chat generation side effects.

DB claim is the correctness source. Multiple Zeabur workers may race;
only the winner of claim_side_effect applies an effect.
Never reruns the main reply generation. The relationship effect may call its
own bounded observer.
"""
import threading
import time

import db_generation_receipt as receipt


IDLE_SECONDS = 2.0
ERROR_SLEEP_SECONDS = 2.0
MAX_ERROR_SLEEP_SECONDS = 8.0

_LOCK = threading.Lock()
_THREAD = None
_WAKE = threading.Event()
_STOP = threading.Event()


def notify_effects_pending():
    _WAKE.set()


def start_generation_side_effect_worker():
    """Start at most one scan thread in this process. Multi-instance safety is DB claim."""
    global _THREAD
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return _THREAD
        _STOP.clear()
        _THREAD = threading.Thread(
            target=_loop, name='generation-side-effects', daemon=True)
        _THREAD.start()
        print('[generation_side_effects] worker 已启动')
        return _THREAD


def stop_generation_side_effect_worker(timeout=1.0):
    """Test helper. Production process exit stops the daemon thread."""
    global _THREAD
    _STOP.set()
    _WAKE.set()
    thread = _THREAD
    if thread and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    with _LOCK:
        _THREAD = None


def _loop():
    error_sleep = ERROR_SLEEP_SECONDS
    while not _STOP.is_set():
        try:
            ran = process_due_side_effects(limit=8)
            error_sleep = ERROR_SLEEP_SECONDS
            if ran:
                continue
            _WAKE.wait(timeout=IDLE_SECONDS)
            _WAKE.clear()
        except Exception as e:
            print(f'[generation_side_effects] worker 出错：{e}')
            _STOP.wait(timeout=error_sleep)
            error_sleep = min(error_sleep * 2, MAX_ERROR_SLEEP_SECONDS)


def build_effect_ctx(row, extra_ctx=None):
    extra_ctx = dict(extra_ctx or {})
    payload = extra_ctx.get('payload')
    if payload is None:
        rec = receipt.get_generation(
            row['user_id'], row['character_id'],
            row['source_event_id'], row['endpoint'])
        payload = (rec or {}).get('response_json') or {}
    ctx = {
        'user_id': row['user_id'],
        'character_id': row['character_id'],
        'source_event_id': row['source_event_id'],
        'endpoint': row['endpoint'],
        'payload': payload,
        'user_text': extra_ctx.get('user_text') or payload.get('_user_text') or '',
        'full_jp': extra_ctx.get('full_jp') or ' '.join(
            str((m or {}).get('jp') or '') for m in (payload.get('messages') or [])),
        'msgs': extra_ctx.get('msgs') or payload.get('messages') or [],
        'effect_payload': row.get('payload_json') or extra_ctx.get('effect_payload') or {},
    }
    for key, value in extra_ctx.items():
        ctx.setdefault(key, value)
    ctx['effect_payload'] = row.get('payload_json') or extra_ctx.get('effect_payload') or {}
    return ctx


def process_one_side_effect(row, ctx=None, apply_fn=None):
    """Claim → apply → completed without rerunning main reply generation."""
    effect = row.get('effect')
    user_id = row['user_id']
    character_id = row['character_id']
    source_event_id = row['source_event_id']
    endpoint = row['endpoint']
    if row.get('status') == receipt.STATUS_COMPLETED:
        return False
    if receipt.CRASH_BEFORE_EFFECT == effect:
        raise RuntimeError(f'injected crash before {effect}')
    claim = receipt.claim_side_effect(
        user_id, character_id, source_event_id, endpoint, effect)
    if not claim.get('owned'):
        return False
    ctx = build_effect_ctx(row, extra_ctx=ctx)
    try:
        if apply_fn is not None:
            result = apply_fn(effect, ctx) or {}
        else:
            from generation_effects import apply_effect
            result = apply_effect(effect, ctx) or {}
        if receipt.CRASH_AFTER_EFFECT == effect:
            raise RuntimeError(f'injected crash after {effect}')
        receipt.complete_side_effect(
            user_id, character_id, source_event_id, endpoint, effect,
            claim.get('claim_token'), result_json=result)
        return True
    except Exception as e:
        injected = str(e).startswith('injected crash')
        if injected:
            raise
        receipt.fail_side_effect(
            user_id, character_id, source_event_id, endpoint, effect,
            claim.get('claim_token'), last_error=str(e))
        print(f'[{user_id}] side-effect {effect} failed:{e}')
        return False


def process_due_side_effects(limit=8, apply_fn=None, ctx=None):
    """Scan due ledger rows and apply claimed work. Used by tests and the worker loop."""
    ran = []
    for row in receipt.list_due_side_effects(limit=limit):
        if process_one_side_effect(row, ctx=ctx, apply_fn=apply_fn):
            ran.append(row.get('effect'))
    return ran


def process_due_for_receipt(user_id, character_id, source_event_id, endpoint,
                            ctx=None, skip=None, apply_fn=None, limit=16):
    skip = set(skip or ())
    ran = []
    rows = receipt.list_side_effects(user_id, character_id, source_event_id, endpoint)
    for row in rows:
        if len(ran) >= max(1, int(limit or 16)):
            break
        if row.get('effect') in skip:
            continue
        if process_one_side_effect(row, ctx=ctx, apply_fn=apply_fn):
            ran.append(row.get('effect'))
    return ran
