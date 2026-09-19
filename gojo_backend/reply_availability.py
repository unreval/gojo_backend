"""Reply availability guard shared by text and image chat routes.

The schedule layer decides whether a character is free, soft-busy, or hard-busy.
This module turns that into the concrete per-message behavior:
  - free: seen + reply now
  - soft_busy: shared next_phone_check_at per activity; new messages only
    append pending inbox; consume check only when due; defer schedules the next one
  - hard_busy: persist pending context, no phone-check roll
  Runtime availability uses effective_busy_end / effective_reply_state:
  a long soft_busy block may stop blocking replies before its visual end_time.

The durable source of truth for pending busy-period messages is
char_phone_check (including event_meta / visual_summary for images).

Busy fallback is a delayed normal chat reply. It must not create
proactive_promise rows or use the proactive generator.
"""
import json


def _activity_state(activity):
    if not activity:
        return 'free'
    return activity.get('reply_state') or ('free' if activity.get('can_reply') else 'hard_busy')


def parse_pending_event_meta(event_meta):
    """event_meta is stored as newline-joined JSON objects."""
    items = []
    if isinstance(event_meta, list):
        return [item for item in event_meta if isinstance(item, dict)]
    if isinstance(event_meta, dict):
        return [event_meta]
    text = event_meta or ''
    if not str(text).strip():
        return items
    for line in str(text).split('\n'):
        line = line.strip()
        if not line:
            continue
        if line.startswith('{'):
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    items.append(parsed)
                    continue
            except Exception:
                pass
        items.append({'raw': line[:500]})
    return items


def format_pending_bundle_context(bundle):
    """INTERNAL CONTEXT for the normal chat pipeline. Not a final reply."""
    if not bundle:
        return ''
    title = bundle.get('activity_title') or bundle.get('title') or ''
    state = bundle.get('reply_state') or 'soft_busy'
    pending_text = (bundle.get('pending_text') or '').strip()
    count = bundle.get('pending_count') or 0
    metas = parse_pending_event_meta(bundle.get('event_meta'))
    visual_lines = []
    source_ids = []
    for meta in metas:
        sid = meta.get('source_event_id') or meta.get('event_id')
        if sid:
            source_ids.append(str(sid))
        kind = meta.get('kind') or ''
        visual = (meta.get('visual_summary') or '').strip()
        caption = (meta.get('caption') or meta.get('display_text') or '').strip()
        if kind in ('image', 'video') or visual:
            visual_lines.append(
                f'- kind={kind or "media"} caption={caption[:200]} '
                f'visual_summary={visual[:400]} source_event_id={sid or ""}'
            )
    first_id = bundle.get('first_source_event_id') or ''
    last_id = bundle.get('last_source_event_id') or ''
    if first_id:
        source_ids.insert(0, str(first_id))
    if last_id:
        source_ids.append(str(last_id))
    # preserve order, drop empties
    seen = set()
    ordered_ids = []
    for item in source_ids:
        if item and item not in seen:
            seen.add(item)
            ordered_ids.append(item)
    visual_block = '\n'.join(visual_lines)
    return (
        '【内部上下文——忙碌期间积压的消息，不是用户此刻新发的一条】\n'
        f'刚才角色在「{title}」({state})，当时没能及时回复。\n'
        f'积压 {count} 条。phone_check_id={bundle.get("id") or bundle.get("opportunity_id") or ""}。\n'
        f'source_event_ids={",".join(ordered_ids) or "(none)"}\n'
        f'【积压原文】\n{pending_text or "（无文字）"}\n'
        + (f'【积压图片/视频】\n{visual_block}\n' if visual_block else '')
        + '现在已经能回了。把这段积压当作刚刚一起看到的内容，用正常聊天回复：\n'
        '1. 这是延迟的正常回复，不是主动搭讪，不要当成 proactive。\n'
        '2. 不要逐条复读，也不要假装当时已经秒回。\n'
        '3. 输出格式仍是 1~3 个气泡的 messages[]，每条含 jp / zh。\n'
        '4. 图片/视频以 visual_summary 为准，不要编造没写过的画面。'
    )


def check_reply_availability(character_id, user_id, source_event_id='',
                             pending_text='', event_meta=None, now=None):
    """Return a decision dict for the current message.

    Callers should proceed with normal generation only when can_reply is true.
    When can_reply is false, this function has already persisted the pending
    phone-check opportunity. It does not create a proactive_promise.

    seen / can_reply are independent:
      · inbound busy 消息只进 inbox，本请求不生成
      · phone check 到点由 delayed_reply worker 认领后走正常 chat pipeline
    """
    from datetime import datetime, timezone
    import db_schedule
    try:
        from config import CN_TZ
    except Exception:
        CN_TZ = timezone.utc

    now = now or datetime.now(CN_TZ)
    activity = db_schedule.get_current_activity(character_id, user_id, now)
    if not activity or db_schedule.effective_reply_state(activity, now) == 'free':
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
    return decision
