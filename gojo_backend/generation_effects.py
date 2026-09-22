"""Apply durable chat generation side effects after receipt complete.

Never reruns the main reply generation. relationship_update owns its bounded
observer call; replay/repair uses the same side-effect functions.
"""
from datetime import datetime


ENDPOINT_CHAT_TEXT = 'chat_text'
ENDPOINT_CHAT_IMAGE = 'chat_image'

# Must not block TTS / HTTP. Durable worker still repairs them.
SYNC_SKIP_EFFECTS = (
    'assistant_short_memory',
    'record_turn',
    'private_extraction',
    'behavior_evidence',
    'relationship_update',
    'promise_detector',
)
CLIENT_COUPLED_EFFECTS = ('reminder', 'cancel_reminder')


class RetryableRelationshipObserverError(RuntimeError):
    """Signals observer failed; the durable side-effect row should be retried."""


def assistant_turn_id_for(endpoint, source_event_id):
    source = str(source_event_id or '').strip()
    if endpoint == ENDPOINT_CHAT_IMAGE:
        return f'image_reply:{source}'
    return f'chat_reply:{source}'


def occurrence_key_for(endpoint, source_event_id, effect):
    return f'{endpoint}:{source_event_id}:{effect}'


def resolve_cancel_targets(ctx):
    """Read-only freeze of task ids to cancel. Never deletes."""
    payload = _payload(ctx)
    cancel = (
        payload.get('_cancel_reminder')
        or payload.get('cancel_reminder')
        or ctx.get('cancel_reminder')
        or {}
    )
    if not isinstance(cancel, dict):
        cancel = {}
    keyword = (cancel.get('keyword') or '').strip()
    latest = bool(cancel.get('latest', False))
    user_id = ctx.get('user_id')
    ids = []
    frozen = []
    try:
        if keyword:
            find_open = _ctx_fn(
                ctx, 'find_open_tasks_by_keyword',
                'tasks', 'find_open_tasks_by_keyword')
            rows = find_open(user_id, keyword, latest_only=True) or []
        elif latest:
            find_latest = _ctx_fn(
                ctx, 'find_latest_open_task', 'tasks', 'find_latest_open_task')
            rows = find_latest(user_id) or []
        else:
            rows = []
        for row in rows:
            if not row or row[0] is None:
                continue
            task_id = int(row[0])
            notification_id = row[1] if len(row) > 1 else None
            ids.append(task_id)
            frozen.append({
                'task_id': task_id,
                'notification_id': notification_id,
            })
    except Exception as e:
        print(f'[{user_id}] resolve_cancel_targets skipped:{e}')
    return {
        'target_task_ids': ids,
        'target_tasks': frozen,
        'keyword': keyword,
        'latest': latest,
    }


def build_effect_payloads(endpoint, payload, ctx=None):
    from db_generation_receipt import needed_effects
    ctx = dict(ctx or {})
    ctx.setdefault('payload', payload)
    effects = needed_effects(endpoint, payload, ctx)
    payloads = {}
    if 'cancel_reminder' in effects:
        payloads['cancel_reminder'] = resolve_cancel_targets(ctx)
    if 'reminder' in effects:
        rem = payload.get('reminder') or {}
        payloads['reminder'] = {
            'date': rem.get('date'),
            'time': rem.get('time'),
            'content': rem.get('content'),
            'notification': rem.get('notification'),
        }
    if 'proactive_promise' in effects:
        payloads['proactive_promise'] = (
            payload.get('_proactive_promise')
            or payload.get('proactive_promise')
            or ctx.get('proactive_promise')
            or {}
        )
    return effects, payloads


def commit_and_run_effects(user_id, character_id, source_event_id, endpoint,
                           claim_token, resp, ctx=None):
    from db_generation_receipt import after_generation_commit, complete_generation
    ctx = dict(ctx or {})
    ctx.setdefault('user_id', user_id)
    ctx.setdefault('character_id', character_id)
    ctx.setdefault('source_event_id', source_event_id)
    ctx.setdefault('endpoint', endpoint)
    ctx.setdefault('payload', resp)
    effects, payloads = build_effect_payloads(endpoint, resp, ctx)
    completed = complete_generation(
        user_id, character_id, source_event_id, endpoint,
        claim_token, resp, effects=effects, effect_payloads=payloads)
    ran = after_generation_commit(
        user_id, character_id, source_event_id, endpoint,
        ctx=ctx, skip=SYNC_SKIP_EFFECTS)
    return {
        'completed': completed,
        'effects': effects,
        'client_effects': [
            effect for effect in effects if effect in CLIENT_COUPLED_EFFECTS
        ],
        'ran': ran,
    }


def repair_completed_generation(user_id, character_id, source_event_id,
                                endpoint, payload, extra_ctx=None):
    from db_generation_receipt import (
        after_generation_commit, ensure_side_effect_row, get_generation,
        needed_effects,
    )
    ctx = dict(extra_ctx or {})
    payload = payload or {}
    ctx.setdefault('user_id', user_id)
    ctx.setdefault('character_id', character_id)
    ctx.setdefault('source_event_id', source_event_id)
    ctx.setdefault('endpoint', endpoint)
    ctx.setdefault('payload', payload)
    ctx.setdefault(
        'user_text',
        payload.get('_user_text') or payload.get('user_text') or ctx.get('user_text') or '')
    if not ctx.get('full_jp'):
        ctx['full_jp'] = ' '.join(
            str((m or {}).get('jp') or '') for m in (payload.get('messages') or []))
    rec = get_generation(user_id, character_id, source_event_id, endpoint)
    if not rec or rec.get('status') != 'completed':
        return payload
    stored = rec.get('response_json') or payload
    effects, payloads = build_effect_payloads(endpoint, stored, ctx)
    for effect in effects or needed_effects(endpoint, payload, ctx):
        ensure_side_effect_row(
            user_id, character_id, source_event_id, endpoint, effect,
            payload=payloads.get(effect))
    after_generation_commit(
        user_id, character_id, source_event_id, endpoint,
        ctx=ctx, skip=SYNC_SKIP_EFFECTS)
    return payload


def client_coupled_effects(endpoint, payload, ctx=None):
    from db_generation_receipt import needed_effects
    return [
        effect for effect in needed_effects(endpoint, payload or {}, ctx or {})
        if effect in CLIENT_COUPLED_EFFECTS
    ]


def _client_effect_completed(effect, row):
    if not row or row.get('status') != 'completed':
        return False
    result = row.get('result_json') or {}
    if not isinstance(result, dict):
        return False
    if effect == 'reminder':
        return result.get('task_id') is not None
    if effect == 'cancel_reminder':
        return isinstance(result.get('cancelled_tasks'), list)
    return True


def pending_client_effects(user_id, character_id, source_event_id, endpoint,
                           required_effects):
    required = [
        effect for effect in (required_effects or [])
        if effect in CLIENT_COUPLED_EFFECTS
    ]
    if not required:
        return []
    from db_generation_receipt import list_side_effects
    rows = {
        row.get('effect'): row
        for row in list_side_effects(user_id, character_id, source_event_id, endpoint)
    }
    return [
        effect for effect in required
        if not _client_effect_completed(effect, rows.get(effect))
    ]


def client_effects_pending_response(source_event_id, pending_effects):
    from db_generation_receipt import in_progress_body
    pending = list(pending_effects or [])
    body = in_progress_body(source_event_id)
    body['client_effects_pending'] = pending
    if 'reminder' in pending:
        body['reminder_pending'] = True
    if 'cancel_reminder' in pending:
        body['cancel_reminder_pending'] = True
    return body


def apply_effect(effect, ctx):
    dispatch = {
        'assistant_short_memory': apply_assistant_short_memory,
        'record_turn': apply_record_turn,
        'private_extraction': apply_private_extraction,
        'behavior_evidence': apply_behavior_evidence,
        'promise_detector': apply_promise_detector,
        'relationship_update': apply_relationship_update,
        'reminder': apply_reminder,
        'cancel_reminder': apply_cancel_reminder,
        'proactive_promise': apply_proactive_promise,
    }
    fn = dispatch.get(effect)
    if fn:
        return fn(ctx) or {}
    return {}


def _ctx_fn(ctx, name, module_name, attr):
    fn = (ctx or {}).get(name)
    if fn is not None:
        return fn
    module = __import__(module_name, fromlist=[attr])
    return getattr(module, attr)


def _payload(ctx):
    return (ctx or {}).get('payload') or {}


def _effect_payload(ctx):
    extra = (ctx or {}).get('effect_payload')
    return extra if isinstance(extra, dict) else {}


def apply_assistant_short_memory(ctx):
    user_id = ctx['user_id']
    character_id = ctx['character_id']
    source_event_id = ctx['source_event_id']
    endpoint = ctx.get('endpoint') or ENDPOINT_CHAT_TEXT
    full_jp = ctx.get('full_jp') or ' '.join(
        str((m or {}).get('jp') or '') for m in (_payload(ctx).get('messages') or []))
    if not full_jp:
        return {}
    turn_id = assistant_turn_id_for(endpoint, source_event_id)
    save_short_memory = _ctx_fn(ctx, 'save_short_memory', 'user_memory', 'save_short_memory')
    save_short_memory(
        user_id, 'assistant', full_jp, character_id,
        source_event_id=turn_id,
        metadata={
            'turn_aggregate': True,
            'assistant_turn_id': turn_id,
        })
    event_meta = ctx.get('event_meta') or _payload(ctx).get('event_meta')
    if event_meta:
        try:
            attach = _ctx_fn(
                ctx, 'attach_short_memory_event_meta',
                'user_memory', 'attach_short_memory_event_meta')
            attach(user_id, character_id, source_event_id, event_meta)
        except Exception as e:
            print(f'[{user_id}] short_memory event_meta attach skipped:{e}')
    return {}


def apply_record_turn(ctx):
    record_turn = _ctx_fn(ctx, 'record_turn', 'temporal_awareness', 'record_turn')
    endpoint = ctx.get('endpoint') or ENDPOINT_CHAT_TEXT
    source = ctx.get('turn_source')
    if not source:
        source = 'chat_video' if ctx.get('is_video') else (
            'chat_image' if endpoint == ENDPOINT_CHAT_IMAGE else 'chat_text')
    record_turn(
        ctx['user_id'], ctx['character_id'], source=source,
        prior_snapshot=ctx.get('temporal_snapshot'),
        source_event_id=ctx.get('source_event_id'),
    )
    return {}


def apply_private_extraction(ctx):
    user_text = str(ctx.get('user_text') or _payload(ctx).get('_user_text') or '').strip()
    if not user_text:
        return {}
    full_jp = ctx.get('full_jp') or ' '.join(
        str((m or {}).get('jp') or '') for m in (_payload(ctx).get('messages') or []))
    enqueue = _ctx_fn(
        ctx, 'enqueue_private_extraction',
        'memory_jobs', 'enqueue_private_extraction')
    enqueue(
        ctx['user_id'], user_text, full_jp, ctx['character_id'],
        temporal_context=ctx.get('temporal_snapshot'),
        source_event_id=ctx.get('source_event_id'),
    )
    return {}


def apply_behavior_evidence(ctx):
    try:
        record_reply_cycle = _ctx_fn(
            ctx, 'record_reply_cycle',
            'behavior_evidence', 'record_reply_cycle')
        from datetime import timezone as _tz
        availability = ctx.get('availability') or {}
        full_jp = ctx.get('full_jp') or ''
        record_reply_cycle(
            ctx['user_id'], ctx['character_id'],
            user_event_id=ctx.get('source_event_id'),
            user_at=(ctx.get('temporal_snapshot') or {}).get('now_utc'),
            seen_at=availability.get('seen_at'),
            replied_at=datetime.now(_tz.utc),
            message_length=len(full_jp or ''),
            busy_state=availability.get('reply_state') or 'free',
            interaction_mode='text',
            phone_check_id=availability.get('opportunity_id'),
        )
    except Exception as e:
        print(f'[{ctx.get("user_id")}] behavior observation skipped:{e}')
    return {}


def apply_promise_detector(ctx):
    payload = _payload(ctx)
    msgs = ctx.get('msgs') or payload.get('messages') or []
    reply_zh = ctx.get('reply_zh') or ' '.join(
        str((m or {}).get('zh') or '') for m in msgs if (m or {}).get('zh'))
    if not reply_zh:
        return {}
    detect = ctx.get('detect_and_save')
    if detect is None:
        import promise_detector
        detect = promise_detector.detect_and_save
    detect(
        ctx['character_id'], ctx['user_id'],
        ctx.get('user_text') or payload.get('_user_text') or '',
        reply_zh,
        occurrence_key=occurrence_key_for(
            ctx.get('endpoint') or ENDPOINT_CHAT_TEXT,
            ctx.get('source_event_id'),
            'promise_detector'),
    )
    return {}


def _relationship_effect_result(result):
    if not isinstance(result, dict):
        return {}
    observer_error = result.get('observer_error')
    if observer_error:
        # Keep the durable queue error useful without copying model output or
        # arbitrary provider text into chat_generation_side_effect.last_error.
        error_code = str(observer_error).split(':', 1)[0][:80]
        raise RetryableRelationshipObserverError(
            f'relationship_observer_failed:{error_code}')
    return {
        'signals_extracted': int(result.get('signals_extracted') or 0),
        'signals_applied': int(result.get('signals_applied') or 0),
        'skipped': result.get('skipped'),
    }


def apply_relationship_update(ctx):
    fn = ctx.get('relationship_fn')
    if fn is not None:
        return _relationship_effect_result(fn())
    user_text = ctx.get('user_text') or _payload(ctx).get('_user_text') or ''
    full_jp = ctx.get('full_jp') or ' '.join(
        str((m or {}).get('jp') or '') for m in (_payload(ctx).get('messages') or []))
    char = ctx.get('char') or {}
    core_snippet = (char.get('core_prompt') or '')[:300]
    pack = ctx.get('pack')
    short_memories = ctx.get('short_memories') or []
    if pack is not None and getattr(pack, 'messages', None):
        recent_ctx = [
            {'role': m.get('role'), 'content': m.get('content')}
            for m in list(pack.messages)[-6:]
        ]
    else:
        recent_ctx = [{'role': r, 'content': c} for r, c in list(short_memories)[-6:]]
    from relationship_engine import process_turn
    result = process_turn(
        user_id=ctx['user_id'],
        character_id=ctx['character_id'],
        user_message=user_text,
        character_reply=full_jp,
        character_core_snippet=core_snippet,
        recent_context=recent_ctx,
        temporal_context=ctx.get('temporal_snapshot'),
        source_event_id=ctx.get('source_event_id'),
    )
    return _relationship_effect_result(result)


def apply_reminder(ctx):
    payload = _payload(ctx)
    reminder_data = _effect_payload(ctx) or payload.get('reminder')
    if not reminder_data:
        reminder_data = payload.get('reminder')
    if not reminder_data:
        return {}
    user_id = ctx['user_id']
    occ = occurrence_key_for(
        ctx.get('endpoint') or ENDPOINT_CHAT_TEXT,
        ctx.get('source_event_id'),
        'reminder')
    find_duplicate_task = _ctx_fn(ctx, 'find_duplicate_task', 'tasks', 'find_duplicate_task')
    find_similar_task = _ctx_fn(ctx, 'find_similar_task', 'task_dedup', 'find_similar_task')
    existing = find_duplicate_task(
        user_id,
        reminder_data.get('content'),
        reminder_data.get('date'),
        reminder_data.get('time'),
    )
    similar = None
    if not existing:
        similar = find_similar_task(
            user_id,
            reminder_data.get('content'),
            reminder_data.get('date'),
            reminder_data.get('time'),
        )
    if existing or similar:
        if existing:
            task_id, _ = existing
        else:
            task_id, _notif, same_title = similar
            print(f'[{user_id}] 同时段已有相近提醒「{same_title}」，跳过新建：{reminder_data.get("content")}')
        print(f'[{user_id}] 提醒已存在 task_id={task_id}，跳过新建')
        return {'task_id': task_id, 'duplicate': True}
    task_id = _insert_task_once(
        user_id,
        reminder_data.get('content'),
        reminder_data.get('date'),
        reminder_data.get('time'),
        occ,
    )
    if task_id is not None:
        print(f'[{user_id}] 提醒已保存 task_id={task_id}')
        return {'task_id': task_id, 'duplicate': False}
    return {}


def _insert_task_once(user_id, title, due_date, due_time, occurrence_key):
    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO tasks (
                   user_id, title, category, due_date, due_time,
                   reminder_minutes, occurrence_key)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (occurrence_key)
               WHERE occurrence_key IS NOT NULL AND occurrence_key <> ''
               DO NOTHING
               RETURNING id''',
            (user_id, title, '个人', due_date, due_time, 0, occurrence_key))
        row = cur.fetchone()
        if not row:
            cur.execute(
                'SELECT id FROM tasks WHERE occurrence_key=%s',
                (occurrence_key,))
            row = cur.fetchone()
        conn.commit()
        return row[0] if row else None
    finally:
        cur.close()
        conn.close()


def apply_cancel_reminder(ctx):
    frozen = _effect_payload(ctx)
    target_tasks = {}
    for item in frozen.get('target_tasks') or []:
        try:
            task_id = int(item.get('task_id'))
        except Exception:
            continue
        target_tasks[task_id] = item.get('notification_id')
    if 'target_task_ids' not in frozen:
        ids = list(target_tasks.keys())
    else:
        ids = [
            int(tid) for tid in list(frozen.get('target_task_ids') or [])
            if tid is not None
        ]
    user_id = ctx['user_id']
    delete_tasks_by_ids = _ctx_fn(
        ctx, 'delete_tasks_by_ids', 'tasks', 'delete_tasks_by_ids')
    cancelled = []
    deleted = delete_tasks_by_ids(user_id, ids) or []
    deleted_ids = set()
    for task_id, notif_id in deleted:
        task_id = int(task_id)
        deleted_ids.add(task_id)
        if notif_id is None:
            notif_id = target_tasks.get(task_id)
        cancelled.append({'task_id': task_id, 'notification_id': notif_id})
        print(f'[{user_id}] cancel_reminder deleted task id={task_id}')
    for task_id in ids:
        task_id = int(task_id)
        if task_id in deleted_ids or task_id not in target_tasks:
            continue
        cancelled.append({
            'task_id': task_id,
            'notification_id': target_tasks.get(task_id),
        })
    return {
        'target_task_ids': ids,
        'cancelled_tasks': cancelled,
    }


def apply_proactive_promise(ctx):
    payload = _payload(ctx)
    pp = _effect_payload(ctx) or (
        payload.get('_proactive_promise')
        or payload.get('proactive_promise')
        or ctx.get('proactive_promise')
    )
    if not pp:
        return {}
    user_id = ctx['user_id']
    character_id = ctx['character_id']
    occ = occurrence_key_for(
        ctx.get('endpoint') or ENDPOINT_CHAT_TEXT,
        ctx.get('source_event_id'),
        'proactive_promise')
    user_text = str(ctx.get('user_text') or payload.get('_user_text') or '')[:200]
    import db_promise
    from datetime import datetime as _dt
    kind = pp.get('trigger_kind')
    context_ = (pp.get('context') or '').strip()
    add_promise = ctx.get('add_promise') or db_promise.add_promise
    pid = None
    saved = {}
    if kind == 'once' and pp.get('trigger_at') and context_:
        trigger_at = pp['trigger_at']
        if isinstance(trigger_at, str):
            trigger_at = _dt.strptime(trigger_at, '%Y-%m-%d %H:%M')
        pid = add_promise(
            character_id=character_id, user_id=user_id,
            trigger_kind='once', trigger_at=trigger_at,
            context=context_, origin_text=user_text,
            occurrence_key=occ,
        )
        saved = {
            'promise_id': pid, 'id': pid, 'kind': 'once',
            'trigger_at': pp['trigger_at'], 'context': context_,
        }
    elif kind == 'daily' and pp.get('trigger_time') and context_:
        pid = add_promise(
            character_id=character_id, user_id=user_id,
            trigger_kind='daily', trigger_time=pp['trigger_time'],
            context=context_, origin_text=user_text,
            occurrence_key=occ,
        )
        saved = {
            'promise_id': pid, 'id': pid, 'kind': 'daily',
            'trigger_time': pp['trigger_time'], 'context': context_,
        }
    else:
        print(f'[{user_id}] proactive_promise 字段不全,跳过:{pp}')
        return {}
    if pid:
        print(f'[{user_id}] 记下承诺 #{pid} {kind}: {context_}')
    return saved
