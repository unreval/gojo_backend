"""Delayed busy reply: phone_check inbox → normal chat generator.

Not a second LLM brain. Not proactive_scheduler. Delivery uses the existing
proactive_msg inbox so the frontend can show 1~3 bubbles, but generation is
the same /chat/text pipeline (build context, OUTPUT_SPEC, commit gate).
"""
import threading
import time
from datetime import datetime, timezone

from reply_availability import format_pending_bundle_context
from db_read_receipt import source_event_ids_from_claim


_thread = None
_stop = False
TICK_SECONDS = 30
_BUSY_FALLBACK_MARKER = 'phone_check_id='


def _now():
    try:
        from config import CN_TZ
        return datetime.now(CN_TZ)
    except Exception:
        return datetime.now(timezone.utc)


def is_busy_fallback_promise(promise):
    context = (promise or {}).get('context') or ''
    return _BUSY_FALLBACK_MARKER in context


def delivery_event_id(phone_check_id, index):
    return f'delayed_reply:{phone_check_id}:{int(index)}'


def _pending_source_event_ids(bundle):
    return source_event_ids_from_claim(bundle)


def assistant_already_committed(user_id, character_id, phone_check_id):
    """True if a previous crash already wrote the first delayed bubble."""
    if phone_check_id is None or not user_id or not character_id:
        return False
    event_id = delivery_event_id(phone_check_id, 0)
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                '''SELECT 1 FROM chat_log
                   WHERE user_id=%s AND chat_id=%s
                     AND COALESCE(event_id, client_msg_id)=%s
                   LIMIT 1''',
                (user_id, character_id, event_id),
            )
            if cur.fetchone():
                return True
            cur.execute(
                '''SELECT 1 FROM short_memory
                   WHERE user_id=%s AND character_id=%s
                     AND role='assistant' AND source_event_id=%s
                   LIMIT 1''',
                (user_id, character_id, event_id),
            )
            return bool(cur.fetchone())
        finally:
            cur.close()
            conn.close()
    except Exception as exc:
        print(f'[delayed_reply] delivery lookup skipped: {exc}')
        return False


def cancel_orphan_busy_promises(now=None):
    """Old architecture wrote wake-up promises. Do not generate from them."""
    try:
        import db_promise
        now = now or _now()
        cancelled = 0
        if hasattr(db_promise, 'deactivate_legacy_busy_fallbacks'):
            ids = db_promise.deactivate_legacy_busy_fallbacks(now) or []
            cancelled = len(ids)
        else:
            due = db_promise.get_due_promises(now)
            for promise in due:
                if not is_busy_fallback_promise(promise):
                    continue
                db_promise.mark_fired(promise['id'], now)
                cancelled += 1
        if cancelled:
            print(f'[delayed_reply] cancelled {cancelled} orphan busy promises')
        return cancelled
    except Exception as exc:
        print(f'[delayed_reply] orphan promise cancel skipped: {exc}')
        return 0


def _load_pending_events(bundle):
    """Pair pending IDs with their canonical rows, including explicit quotes."""
    from raw_events import SourceValidityError, get_active_events_by_ids
    from db_chatlog import resolve_reply_reference

    ids = _pending_source_event_ids(bundle)
    if not ids:
        raise SourceValidityError('pending_source_ids_missing')
    events = get_active_events_by_ids(
        bundle['user_id'], bundle['character_id'], ids)
    if len(events) != len(ids):
        raise SourceValidityError('pending_source_unavailable')
    for event in events:
        meta = event.get('metadata') or {}
        preview = meta.get('reply_to') or {}
        reference_id = (meta.get('reply_to_event_id')
                        or (preview.get('source_event_id') if isinstance(preview, dict) else None))
        if reference_id:
            target = resolve_reply_reference(
                bundle['user_id'], bundle['character_id'], reference_id)
            if target:
                event['verified_reply'] = target
            else:
                event['reply_unavailable'] = True
    return events


def _persist_pending_user_messages(bundle, events):
    from user_memory import save_user_short_memory_once

    user_id = bundle.get('user_id')
    character_id = bundle.get('character_id')
    for event in events:
        meta = dict(event.get('metadata') or {})
        if event.get('reply_unavailable'):
            for key in ('reply_to', 'reply_to_event_id', 'reply_to_source_event_id'):
                meta.pop(key, None)
        save_user_short_memory_once(
            user_id, event.get('content') or '', character_id,
            source_event_id=event.get('event_id'),
            event_meta=meta,
            reply_to_event_id=(event.get('verified_reply') or {}).get('source_event_id'),
        )


def _push_delayed_reply(user_id, character_id, char_name, msgs):
    try:
        import push_notify
        body = msgs[0].get('zh') or msgs[0].get('jp') or ''
        push_notify.push_to_user(
            user_id, title=char_name, body=body,
            data={
                'type': 'delayed_reply',
                'character_id': character_id,
            },
        )
    except Exception as exc:
        print(f'[delayed_reply] push skipped: {exc}')


def generate_delayed_chat_reply(bundle, *, helpers=None):
    """Read pending bundle → INTERNAL CONTEXT → normal chat generator → commit.

    Resolve is the caller's job after this returns ok=True.
    """
    import route_chat
    from characters import get_character
    from user_memory import (
        get_short_memory, SHORT_MEMORY_MAX,
        commit_visible_assistant_message, update_chat_days,
    )
    from temporal_awareness import get_temporal_snapshot, record_turn
    from prompt import build_system_blocks
    from tts import tts_to_b64
    import proactive_msg

    helpers = helpers or route_chat
    user_id = bundle['user_id']
    character_id = bundle['character_id']
    phone_check_id = bundle.get('id')
    char = get_character(character_id)
    if not char:
        return {'ok': False, 'reason': 'missing_character'}

    if assistant_already_committed(user_id, character_id, phone_check_id):
        print(f'[delayed_reply] #{phone_check_id} already committed, skip generate')
        return {
            'ok': True,
            'already_delivered': True,
            'messages': [],
            'full_jp': '',
            'turn_id': delivery_event_id(phone_check_id, 0),
        }

    try:
        pending_events = _load_pending_events(bundle)
    except Exception as exc:
        return {'ok': False, 'reason': str(exc) or 'pending_source_unavailable'}
    pending_text = ' '.join(event.get('content') or '' for event in pending_events)
    pending_ids = [event['event_id'] for event in pending_events]
    extra_suffix = '\n\n' + format_pending_bundle_context(bundle, pending_events)
    trigger = (
        '【系统内部】你现在有空看手机了。请根据 INTERNAL CONTEXT 里忙碌期间'
        '积压的消息，用正常聊天回复。这不是用户此刻新发的一条，也不是主动搭讪。'
    )
    temporal_snapshot = get_temporal_snapshot(user_id, character_id)
    snapshot_started = time.monotonic()
    pack, messages = helpers._turn_context(
        user_id, character_id, '', profile='text',
        current_event_id=pending_ids or None,
        temporal_snapshot=temporal_snapshot)
    messages = helpers._history_plus_current(messages, trigger)
    recall_query = pending_text or trigger
    system_blocks = build_system_blocks(
        user_id, character_id, recall_query, extra_suffix=extra_suffix,
        temporal_snapshot=temporal_snapshot, context_pack=pack,
        time_anchor_text='')

    def reject_reply(parsed):
        from temporal_awareness import find_reply_calendar_conflict
        reply_text = ' '.join(
            f'{msg.get("jp", "")} {msg.get("zh", "")}'
            for msg in parsed.get('messages') or [])
        conflict = find_reply_calendar_conflict(
            pending_text, reply_text,
            now_utc=temporal_snapshot.get('now_utc'))
        if conflict:
            system_blocks.append({'type': 'text', 'text': (
                f'上一候选违反当前时间快照，错误代码：{conflict}。'
                '请按本轮确定性时间事实修正，不改变历史消息时间。')})
            return conflict
        return route_chat._reject_schedule_candidate(
            character_id, user_id, parsed, system_blocks,
            now=temporal_snapshot.get('now_local'))

    from config import MODEL_MAIN
    result, committed_state = helpers._generate_or_none(
        MODEL_MAIN, 1500, system_blocks, messages,
        attempts=2,
        log_tag=f'delayed:{user_id}][{character_id}',
        cache_tag=f'chat:{character_id}',
        salvage=True,
        reject_fn=reject_reply,
    )
    # Delayed replies use the same truth guard/commit path as immediate text.
    # A phone-check may have completed an old phase while the model was running.
    if result:
        from schedule_transition import validate_generated_schedule_reply
        reason, _world = validate_generated_schedule_reply(
            character_id, user_id, result)
        if reason:
            return {'ok': False, 'reason': reason}
    emotion, msgs = helpers._finalize_committed(result)
    if msgs is None:
        return {'ok': False, 'reason': 'generation_failed'}

    try:
        current_pending_events = _load_pending_events(bundle)
    except Exception:
        return {'ok': False, 'reason': 'pending_source_unavailable'}
    if any(
            old.get('event_id') != new.get('event_id')
            or old.get('content') != new.get('content')
            or old.get('timestamp') != new.get('timestamp')
            or old.get('verified_reply') != new.get('verified_reply')
            or bool(old.get('reply_unavailable')) != bool(new.get('reply_unavailable'))
            for old, new in zip(pending_events, current_pending_events)):
        return {'ok': False, 'reason': 'pending_reference_changed'}

    from temporal_awareness import find_commit_clock_conflict
    clock_conflict = find_commit_clock_conflict(
        pending_text,
        ' '.join(f'{msg.get("jp", "")} {msg.get("zh", "")}'
                 for msg in result.get('messages') or []),
        temporal_snapshot, time.monotonic() - snapshot_started)
    if clock_conflict:
        return {'ok': False, 'reason': clock_conflict}

    from schedule_transition import commit_generated_schedule_intent
    transition = commit_generated_schedule_intent(
        character_id, user_id, result,
        source_event_id=bundle.get('last_source_event_id') or '')
    if not transition.get('ok'):
        return {'ok': False, 'reason': transition.get('reason') or 'schedule_transition_failed'}

    helpers._commit_offline_state(user_id, character_id, committed_state)
    full_jp = ' '.join(m['jp'] for m in msgs)
    voice_id = char.get('voice_id')
    for msg in msgs:
        try:
            msg['audio_b64'] = tts_to_b64(msg['jp'], emotion, voice_id) or ''
        except Exception as exc:
            print(f'[delayed_reply] TTS skipped: {exc}')
            msg['audio_b64'] = ''

    _persist_pending_user_messages(bundle, current_pending_events)
    short_memories = get_short_memory(user_id, SHORT_MEMORY_MAX, character_id)

    turn_id = delivery_event_id(phone_check_id, 0)
    for index, msg in enumerate(msgs):
        event_id = delivery_event_id(phone_check_id, index)
        commit_visible_assistant_message(
            user_id, msg.get('jp') or '', character_id,
            event_id=event_id,
            kind='delayed_reply',
            subtitle=msg.get('zh') or '',
            emotion=emotion,
            metadata={
                'proactive_kind': 'delayed_reply',
                'phone_check_id': phone_check_id,
                'assistant_turn_id': turn_id,
                'segment_index': index,
            },
        )
        proactive_msg.add_proactive_msg(
            character_id, user_id, 'delayed_reply',
            msg.get('jp') or '', msg.get('zh') or '',
            emotion, msg.get('audio_b64') or '',
            event_id=event_id,
            assistant_turn_id=turn_id,
            segment_index=index,
        )
    record_turn(
        user_id, character_id, source='chat_delayed',
        prior_snapshot=temporal_snapshot,
    )
    try:
        from behavior_evidence import record_reply_cycle
        record_reply_cycle(
            user_id, character_id,
            user_event_id=bundle.get('last_source_event_id'),
            user_at=(temporal_snapshot or {}).get('now_utc'),
            seen_at=bundle.get('seen_at'),
            replied_at=datetime.now(timezone.utc),
            message_length=len(full_jp or ''),
            busy_state=bundle.get('reply_state') or 'soft_busy',
            interaction_mode='delayed',
            phone_check_id=phone_check_id,
        )
    except Exception as exc:
        print(f'[{user_id}] delayed behavior observation skipped:{exc}')

    _push_delayed_reply(user_id, character_id, char.get('name') or character_id, msgs)
    update_chat_days(user_id)
    from generation_contract import translation_missing
    for msg in msgs:
        msg['translation_missing'] = translation_missing(msg)
    return {
        'ok': True,
        'emotion': emotion,
        'messages': msgs,
        'translation_missing': any(msg['translation_missing'] for msg in msgs),
        'full_jp': full_jp,
        'turn_id': turn_id,
    }


def process_due_phone_checks(now=None, *, generate_fn=None, evaluate_fn=None):
    """Claim due bundles, generate at most once, resolve only after commit."""
    import db_schedule

    now = now or _now()
    generate_fn = generate_fn or generate_delayed_chat_reply
    evaluate_fn = evaluate_fn or db_schedule.evaluate_due_phone_check
    results = []
    try:
        ids = db_schedule.iter_due_phone_checks(now)
    except Exception as exc:
        print(f'[delayed_reply] list due failed: {exc}')
        return results
    for oid in ids:
        try:
            decision = evaluate_fn(oid, now)
        except Exception as exc:
            print(f'[delayed_reply] evaluate #{oid} failed: {exc}')
            continue
        action = (decision or {}).get('action')
        claimed = (decision or {}).get('claimed')
        if action != 'reply' or not claimed:
            results.append({'id': oid, 'action': action or 'skip'})
            continue
        generated = None
        try:
            generated = generate_fn(claimed)
        except Exception as exc:
            print(f'[delayed_reply] generate #{oid} failed: {exc}')
            generated = {'ok': False, 'reason': str(exc)}
        if generated and generated.get('ok'):
            resolved = False
            for _attempt in range(3):
                try:
                    n = db_schedule.complete_delayed_reply(
                        oid, claimed['claim_token'], now)
                    if n:
                        resolved = True
                        break
                except Exception as exc:
                    print(f'[delayed_reply] resolve #{oid} retry failed: {exc}')
            if not resolved:
                # Never fall back to an unconditional resolve here.  A message
                # that arrived while this claim was generating belongs to a
                # successor occurrence; consuming the old row without its
                # claim watermark would silently lose that message.
                print(f'[delayed_reply] finish #{oid} did not win watermark CAS; successor remains pending')
            else:
                db_schedule.log_phone_check_action('consumed', claimed)
            results.append({
                'id': oid, 'action': 'replied', 'ok': True,
                'resolved': resolved,
            })
        else:
            db_schedule.log_phone_check_action('generation_failed', claimed)
            try:
                released = db_schedule.abort_delayed_reply(oid, claimed['claim_token'])
                if released:
                    db_schedule.log_phone_check_action('released', claimed)
            except Exception as exc:
                print(f'[delayed_reply] abort #{oid} failed: {exc}')
            results.append({
                'id': oid, 'action': 'failed',
                'ok': False,
                'reason': (generated or {}).get('reason'),
            })
    return results


def _loop():
    global _stop
    time.sleep(20)
    while not _stop:
        try:
            cancel_orphan_busy_promises()
            process_due_phone_checks()
        except Exception as exc:
            print(f'[delayed_reply] tick failed: {exc}')
        time.sleep(TICK_SECONDS)


def start_delayed_reply_worker():
    global _thread
    if _thread is not None:
        return
    cancel_orphan_busy_promises()
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print('[delayed_reply] phone-check delayed reply worker started')


def is_delayed_reply_worker_running():
    return _thread is not None and _thread.is_alive()
