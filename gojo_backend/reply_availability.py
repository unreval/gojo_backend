"""Reply availability guard shared by text and image chat routes.

The schedule layer decides whether a character is free, soft-busy, or hard-busy.
This module turns that into the concrete per-message behavior:
  - free: seen + reply now
  - soft_busy: shared next_phone_check_at per activity; new messages only
    append pending inbox; consume check only when due; defer schedules the next one
  - hard_busy: persist pending context, no phone-check roll

Promise rows are only a wake-up fallback. The durable source of truth for
pending busy-period messages is char_phone_check (including event_meta /
visual_summary for images).
"""
from datetime import timedelta


def _activity_state(activity):
    if not activity:
        return 'free'
    return activity.get('reply_state') or ('free' if activity.get('can_reply') else 'hard_busy')


def _trigger_at_from_hhmm(now, hhmm):
    if not hhmm:
        return now + timedelta(minutes=10)
    hh, mm = hhmm.split(':')
    trigger_at = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if trigger_at <= now:
        trigger_at += timedelta(days=1)
    return trigger_at


def _fallback_context(activity, decision, user_text, event_meta=None):
    state = decision.get('reply_state') or _activity_state(activity)
    title = activity.get('title', '') if activity else ''
    seen = '看到了' if decision.get('seen') else '没看到'
    kind = (event_meta or {}).get('kind') if isinstance(event_meta, dict) else ''
    visual = ''
    if kind in ('image', 'video'):
        visual = f'\n这条 pending 消息包含{kind}，元数据:{event_meta}'
    return (
        f'刚才我在「{title}」({state})，当时手机状态是「{seen}但没及时回复」。'
        f'\n她当时说/发来:「{(user_text or "")[:500]}」。'
        f'{visual}'
        f'\nphone_check_id={decision.get("opportunity_id") or ""}，'
        f'source_event_id={decision.get("source_event_id") or ""}。'
        '现在如果已经能回了，把忙碌期间积攒的话头合并成一段自然回复；'
        '不要逐条复读，也不要假装当时已经秒回。'
    )


def ensure_busy_fallback(character_id, user_id, now, activity, decision,
                         user_text='', event_meta=None):
    if decision.get('can_reply'):
        return None
    if decision.get('fallback_promise_id'):
        return decision.get('fallback_promise_id')
    try:
        import db_schedule
        import db_promise

        free_at = db_schedule.get_next_free_time(character_id, user_id, now) \
            or activity.get('end_time')
        trigger_at = _trigger_at_from_hhmm(now, free_at)
        pid = db_promise.add_promise(
            character_id=character_id,
            user_id=user_id,
            trigger_kind='once',
            trigger_at=trigger_at,
            context=_fallback_context(activity, decision, user_text, event_meta),
            origin_text=(user_text or '')[:200],
        )
        try:
            db_schedule.attach_fallback_promise(decision.get('opportunity_id'), pid)
        except Exception:
            pass
        decision['fallback_promise_id'] = pid
        decision['free_at'] = free_at
        return pid
    except Exception as exc:
        print(f'[{user_id}] busy fallback promise skipped: {exc}')
        return None


def check_reply_availability(character_id, user_id, source_event_id='',
                             pending_text='', event_meta=None, now=None):
    """Return a decision dict for the current message.

    Callers should proceed with normal generation only when can_reply is true.
    When can_reply is false, this function has already persisted the pending
    phone-check opportunity and attempted to attach one fallback promise.

    seen / can_reply are independent:
      · phone check 到点 → 先写 seen_at，再决定 reply_now / defer
      · defer 时 seen=True 但 can_reply=False（看过但没空回）
      · check 未到点 → seen=False, can_reply=False
    """
    from datetime import datetime, timezone
    import db_schedule
    try:
        from config import CN_TZ
    except Exception:
        CN_TZ = timezone.utc

    now = now or datetime.now(CN_TZ)
    activity = db_schedule.get_current_activity(character_id, user_id, now)
    if not activity or _activity_state(activity) == 'free':
        return {
            'reply_state': 'free',
            'seen': True,
            'can_reply': True,
            'activity': activity,
            'free_at': None,
            'opportunity_id': None,
            'source_event_id': source_event_id,
            'next_phone_check_at': None,
            'seen_at': None,
        }

    decision = db_schedule.decide_phone_check(
        character_id, user_id, now, activity,
        source_event_id=source_event_id,
        pending_text=pending_text,
        event_meta=event_meta,
    )
    decision['source_event_id'] = source_event_id
    decision['free_at'] = db_schedule.get_next_free_time(character_id, user_id, now) \
        or activity.get('end_time')
    if not decision.get('can_reply'):
        ensure_busy_fallback(
            character_id, user_id, now, activity, decision,
            user_text=pending_text, event_meta=event_meta,
        )
    return decision
