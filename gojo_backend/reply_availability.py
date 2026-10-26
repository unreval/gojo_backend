"""Reply availability guard shared by every inbound chat route.

The canonical schedule world returns the active event, active phase, and
availability together.  This module deliberately does not recalculate whether
the character can reply: it only turns that one decision into phone-check
inbox behavior.

The durable source of truth for pending busy-period messages is
char_phone_check (including event_meta / visual_summary for images).

Busy fallback is a delayed normal chat reply. It must not create
proactive_promise rows or use the proactive generator.
"""
import json
import re
from datetime import datetime, timedelta


def _activity_state(activity):
    if not activity:
        return 'free'
    return activity.get('reply_state') or ('free' if activity.get('can_reply') else 'hard_busy')


_CN_DIGITS = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}
# 只有明确要角色确认或到点开口的句子才提前看手机。
# 单纯提到一个钟点（“明天六点考试”）不改变看手机时间。
_REQUEST_CUES = (
    '记得', '记住', '别忘', '答应', '听到了吗', '听见了吗',
    '叫我', '叫醒', '喊我', '提醒我',
)
_CLOCK_RE = re.compile(
    r'(?:(?P<h>\d{1,2})\s*[:：]\s*(?P<m>\d{2})'
    r'|(?P<c>\d{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*点\s*(?P<half>半)?)'
)


def _cn_hour(token):
    token = (token or '').strip()
    if not token:
        return None
    if token.isdecimal():
        value = int(token)
        return value if 0 <= value <= 23 else None
    if any(char not in _CN_DIGITS for char in token):
        return None
    if '十' not in token:
        if len(token) != 1:
            return None
        return _CN_DIGITS[token]
    head, _, tail = token.partition('十')
    value = (_CN_DIGITS[head] if head else 1) * 10 + (_CN_DIGITS[tail] if tail else 0)
    return value if 0 <= value <= 23 else None


def time_request_deadline(text, now):
    """Future China-local clock the character must see before, or None.

    This only bounds when a soft-busy phone check may happen. It does not
    create a promise and it does not make the current request replyable.
    A clock that has already passed is not moved to the next day unless the
    text itself says 明天 or 后天.
    """
    text = text or ''
    if not any(cue in text for cue in _REQUEST_CUES):
        return None
    try:
        from config import CN_TZ
    except Exception:
        return None
    if now is None:
        now = datetime.now(CN_TZ)
    now_cn = now.astimezone(CN_TZ) if now.tzinfo else now.replace(tzinfo=CN_TZ)
    if '后天' in text:
        day_offset = 2
    elif '明天' in text or '明早' in text:
        day_offset = 1
    else:
        day_offset = 0
    plus_twelve = (
        ('下午' in text or '晚上' in text)
        and '早上' not in text and '上午' not in text
    )
    found = []
    for match in _CLOCK_RE.finditer(text):
        if match.group('h') is not None:
            hour = int(match.group('h'))
            minute = int(match.group('m'))
        else:
            hour = _cn_hour(match.group('c'))
            minute = 30 if match.group('half') else 0
        if hour is None or minute > 59:
            continue
        if plus_twelve and hour < 12:
            hour += 12
        if hour > 23:
            continue
        day = now_cn.date() + timedelta(days=day_offset)
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=CN_TZ)
        if candidate > now_cn:
            found.append(candidate)
    return min(found) if found else None


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


def _elapsed_since(sent_at, now):
    if sent_at.tzinfo is None and now.tzinfo is not None:
        sent_at = sent_at.replace(tzinfo=now.tzinfo)
    elif sent_at.tzinfo is not None and now.tzinfo is not None:
        sent_at = sent_at.astimezone(now.tzinfo)
    total = int((now - sent_at).total_seconds())
    if total < 0:
        return '发送时刻晚于当前时刻'
    minutes = total // 60
    if minutes < 1:
        return '不到 1 分钟'
    hours, mins = divmod(minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f'{days} 天 {hours} 小时 {mins} 分'
    if hours:
        return f'{hours} 小时 {mins} 分'
    return f'{mins} 分钟'


def format_pending_bundle_context(bundle, now=None):
    """INTERNAL CONTEXT for the normal chat pipeline. Not a final reply."""
    if not bundle:
        return ''
    title = bundle.get('activity_title') or bundle.get('title') or ''
    state = bundle.get('reply_state') or 'soft_busy'
    pending_text = (bundle.get('pending_text') or '').strip()
    prompt_events = bundle.get('pending_prompt_events')
    if prompt_events:
        lines = []
        for event in prompt_events:
            text = event['text']
            sent_at = event.get('timestamp')
            if isinstance(sent_at, datetime):
                text = f'[发送时间 {sent_at.isoformat(sep=" ", timespec="seconds")}] {text}'
            lines.append(text)
        pending_text = '\n'.join(lines).strip()
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
    try:
        from config import CN_TZ
    except Exception:
        from datetime import timezone
        CN_TZ = timezone.utc
    now = now or datetime.now(CN_TZ)
    now_cn = now.astimezone(CN_TZ) if now.tzinfo else now.replace(tzinfo=CN_TZ)
    last_sent = None
    if prompt_events:
        candidate = prompt_events[-1].get('timestamp')
        if isinstance(candidate, datetime):
            last_sent = candidate
    if last_sent is not None:
        shown = last_sent.astimezone(CN_TZ) if last_sent.tzinfo else last_sent
        elapsed_line = (
            f'最后一条积压消息发送于 {shown.strftime("%Y-%m-%d %H:%M")}，'
            f'距今 {_elapsed_since(last_sent, now_cn)}。'
        )
    else:
        elapsed_line = '最后一条积压消息没有单独的发送时间，不要把它当成刚刚发生。'
    return (
        '【内部上下文——忙碌期间积压的消息，不是用户此刻新发的一条】\n'
        f'刚才角色在「{title}」({state})，当时没能及时回复。\n'
        f'积压 {count} 条。phone_check_id={bundle.get("id") or bundle.get("opportunity_id") or ""}。\n'
        f'source_event_ids={",".join(ordered_ids) or "(none)"}\n'
        f'【积压原文】\n{pending_text or "（无文字）"}\n'
        + (f'【积压图片/视频】\n{visual_block}\n' if visual_block else '')
        + '现在可以回复了。先按真实经过的时间理解这些积压，再作正常聊天回复：\n'
        f'当前时刻：{now_cn.strftime("%Y-%m-%d %H:%M")}（中国时区）。\n'
        f'{elapsed_line}\n'
        '1. 这是延迟的正常回复，不是主动搭讪，不要当成 proactive。\n'
        '2. 不要逐条复读，也不要假装当时已经秒回。\n'
        '3. 输出格式仍是 1~3 个气泡的 messages[]，每条含 jp / zh。\n'
        '4. 图片/视频以 visual_summary 为准，不要编造没写过的画面。\n'
        '5. 积压里如果有带时间点的请求，先判断那个时间点相对现在是'
        '「还没到 / 正好是现在 / 已经过了」，再决定怎么说。'
        '已经过了就正面承认没做到，不要假装做过，也不要默认事情已经发生。'
    )


def check_reply_availability(character_id, user_id, source_event_id='',
                             pending_text='', event_meta=None, now=None):
    """Return a decision dict for the current message.

    Callers should proceed with normal generation only when can_reply is true.
    When can_reply is false, this function has already persisted the pending
    phone-check opportunity. It does not create a proactive_promise.
    A soft-busy message that asks the character to confirm a future clock
    may only pull next_phone_check_at earlier. It still does not reply now.

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
    world = db_schedule.get_current_world_state(character_id, user_id, now)
    activity = world.get('activity')
    availability = world.get('availability') or {}
    reply_state = availability.get('reply_state') or 'free'
    if not activity or availability.get('can_reply', reply_state == 'free'):
        return {
            'reply_state': 'free',
            'seen': True,
            'can_reply': True,
            'activity': activity,
            'world': world,
            'free_at': None,
            'opportunity_id': None,
            'source_event_id': source_event_id,
            'next_phone_check_at': None,
            'seen_at': None,
        }

    handle_before = None
    if reply_state == 'soft_busy':
        handle_before = time_request_deadline(pending_text or '', now)
    decision = db_schedule.decide_phone_check(
        character_id, user_id, now, activity,
        source_event_id=source_event_id,
        pending_text=pending_text,
        event_meta=event_meta,
        handle_before=handle_before,
    )
    decision['world'] = world
    decision['source_event_id'] = source_event_id
    decision['free_at'] = db_schedule.get_next_free_time(
        character_id, user_id, now, world=world) \
        or activity.get('end_time')
    return decision
