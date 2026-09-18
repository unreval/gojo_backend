"""Delayed busy reply: phone_check inbox → normal chat generator.

Not a second LLM brain. Not proactive_scheduler. Delivery uses the existing
proactive_msg inbox so the frontend can show 1~3 bubbles, but generation is
the same /chat/text pipeline (build context, OUTPUT_SPEC, commit gate).
"""
import threading
import time
from datetime import datetime, timezone

from reply_availability import format_pending_bundle_context, parse_pending_event_meta


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
    ids = []
    for meta in parse_pending_event_meta((bundle or {}).get('event_meta')):
        sid = meta.get('source_event_id') or meta.get('event_id')
        if sid:
            ids.append(str(sid).strip())
    for key in ('first_source_event_id', 'last_source_event_id'):
        value = str((bundle or {}).get(key) or '').strip()
        if value:
            ids.append(value)
    seen = set()
    ordered = []
    for item in ids:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


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


def _persist_pending_user_messages(bundle):
    from user_memory import save_user_short_memory_once

    user_id = bundle.get('user_id')
    character_id = bundle.get('character_id')
    metas = parse_pending_event_meta(bundle.get('event_meta'))
    pending_lines = [
        line for line in str(bundle.get('pending_text') or '').split('\n')
        if line.strip()
    ]
    for index, meta in enumerate(metas):
        text = (
            meta.get('caption')
            or meta.get('display_text')
            or meta.get('text')
            or (pending_lines[index] if index < len(pending_lines) else '')
        )
        event_id = meta.get('source_event_id') or meta.get('event_id')
        if not text and not event_id:
            continue
        save_user_short_memory_once(
            user_id, text or '(pending)', character_id,
            source_event_id=event_id,
            event_meta=meta,
        )
    if not metas:
        first_id = bundle.get('first_source_event_id') or None
        last_id = bundle.get('last_source_event_id') or None
        ids = []
        if first_id:
            ids.append(first_id)
        if last_id and last_id != first_id:
            ids.append(last_id)
        for index, line in enumerate(pending_lines):
            event_id = ids[index] if index < len(ids) else (
                ids[-1] if ids else None)
            save_user_short_memory_once(
                user_id, line, character_id, source_event_id=event_id)


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
    from memory_jobs import enqueue_private_extraction
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

    pending_text = (bundle.get('pending_text') or '').strip()
    pending_ids = _pending_source_event_ids(bundle)
    extra_suffix = '\n\n' + format_pending_bundle_context(bundle)
    trigger = (
        '【系统内部】你现在有空看手机了。请根据 INTERNAL CONTEXT 里忙碌期间'
        '积压的消息，用正常聊天回复。这不是用户此刻新发的一条，也不是主动搭讪。'
    )
    temporal_snapshot = get_temporal_snapshot(user_id, character_id)
    pack, messages = helpers._turn_context(
        user_id, character_id, '', profile='text',
        current_event_id=pending_ids or None)
    messages = helpers._history_plus_current(messages, trigger)
    recall_query = pending_text or trigger
    system_blocks = build_system_blocks(
        user_id, character_id, recall_query, extra_suffix=extra_suffix,
        temporal_snapshot=temporal_snapshot, context_pack=pack)

    from config import MODEL_MAIN
    result, committed_state = helpers._generate_or_none(
        MODEL_MAIN, 1500, system_blocks, messages,
        attempts=3,
        log_tag=f'delayed:{user_id}][{character_id}',
        cache_tag=f'chat:{character_id}',
        salvage=True,
    )
    emotion, msgs = helpers._finalize_committed(result)
    if msgs is None:
        return {'ok': False, 'reason': 'generation_failed'}

    helpers._commit_offline_state(user_id, character_id, committed_state)
    full_jp = ' '.join(m['jp'] for m in msgs)
    voice_id = char.get('voice_id')
    for msg in msgs:
        try:
            msg['audio_b64'] = tts_to_b64(msg['jp'], emotion, voice_id) or ''
        except Exception as exc:
            print(f'[delayed_reply] TTS skipped: {exc}')
            msg['audio_b64'] = ''

    _persist_pending_user_messages(bundle)
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
        )
    record_turn(
        user_id, character_id, source='chat_delayed',
        prior_snapshot=temporal_snapshot,
    )
    enqueue_private_extraction(
        user_id, pending_text, full_jp, character_id,
        temporal_context=temporal_snapshot,
        source_event_id=bundle.get('last_source_event_id') or None,
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

    helpers._start_relationship_update(
        user_id, character_id, pending_text, full_jp,
        char, short_memories, temporal_snapshot,
        bundle.get('last_source_event_id'), pack=pack)
    _push_delayed_reply(user_id, character_id, char.get('name') or character_id, msgs)
    update_chat_days(user_id)
    return {
        'ok': True,
        'emotion': emotion,
        'messages': msgs,
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
                try:
                    db_schedule.resolve_phone_check(oid)
                    resolved = True
                except Exception as exc:
                    print(f'[delayed_reply] resolve #{oid} fallback failed: {exc}')
            results.append({
                'id': oid, 'action': 'replied', 'ok': True,
                'resolved': resolved,
            })
        else:
            try:
                db_schedule.abort_delayed_reply(oid, claimed['claim_token'])
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
