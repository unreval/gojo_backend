"""temporal_awareness.py —— 持久时间意识层

这层只记录客观时间线：第一次互动、上一次用户消息、上一次角色消息、
本轮距离上次互动隔了多久、最长断档等。

它不做"过了 X 天所以感情 +/-N"这种线性情绪/关系改动，只把真实经过时间
交给 prompt、记忆提取和关系 observer/reader 当上下文使用。
"""
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

from config import CN_TZ
from db import get_conn


def init_temporal_awareness_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS temporal_awareness (
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        first_interaction_at TIMESTAMP,
        last_user_message_at TIMESTAMP,
        last_assistant_message_at TIMESTAMP,
        last_interaction_at TIMESTAMP,
        previous_interaction_at TIMESTAMP,
        last_gap_seconds INTEGER DEFAULT 0,
        longest_gap_seconds INTEGER DEFAULT 0,
        interaction_count INTEGER DEFAULT 0,
        last_initiator TEXT DEFAULT 'unknown',
        last_source TEXT DEFAULT 'chat',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, character_id)
    )''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_temporal_awareness_lookup
                   ON temporal_awareness (user_id, character_id, updated_at DESC)''')
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 持久时间意识表已就绪：temporal_awareness')


def get_temporal_snapshot(user_id: str, character_id: str,
                          now_utc: Optional[datetime] = None) -> Dict:
    """读取当前轮开始前的客观时间线快照。

    调用点应该在保存本轮消息之前，这样 elapsed 表示"距离上一轮互动"。
    """
    now = _as_utc_naive(now_utc)
    row = _load_row(user_id, character_id) or _load_history_fallback(user_id, character_id)

    if not row or not row.get('last_interaction_at'):
        return {
            'has_history': False,
            'user_id': user_id,
            'character_id': character_id,
            'now_utc': now,
            'now_cn': _format_cn(now),
            **_derived_temporal_fields(now, None),
            'elapsed_seconds_since_last_interaction': None,
            'elapsed_label': '首次记录',
            'gap_bucket': 'first_contact',
            'interaction_count': 0,
        }

    last_interaction = _coerce_dt(row.get('last_interaction_at'))
    elapsed = _seconds_between(last_interaction, now)
    previous = _coerce_dt(row.get('previous_interaction_at'))
    first = _coerce_dt(row.get('first_interaction_at')) or last_interaction
    last_user = _coerce_dt(row.get('last_user_message_at'))
    last_assistant = _coerce_dt(row.get('last_assistant_message_at'))

    return {
        'has_history': True,
        'user_id': user_id,
        'character_id': character_id,
        'now_utc': now,
        'now_cn': _format_cn(now),
        **_derived_temporal_fields(now, last_interaction),
        'first_interaction_at': first,
        'first_interaction_cn': _format_cn(first) if first else None,
        'last_interaction_at': last_interaction,
        'last_interaction_cn': _format_cn(last_interaction),
        'previous_interaction_at': previous,
        'previous_interaction_cn': _format_cn(previous) if previous else None,
        'last_user_message_at': last_user,
        'last_user_message_cn': _format_cn(last_user) if last_user else None,
        'last_assistant_message_at': last_assistant,
        'last_assistant_message_cn': _format_cn(last_assistant) if last_assistant else None,
        'elapsed_seconds_since_last_interaction': elapsed,
        'elapsed_label': format_elapsed(elapsed),
        'gap_bucket': classify_gap(elapsed),
        'stored_last_gap_seconds': int(row.get('last_gap_seconds') or 0),
        'stored_last_gap_label': format_elapsed(row.get('last_gap_seconds') or 0),
        'longest_gap_seconds': int(row.get('longest_gap_seconds') or 0),
        'longest_gap_label': format_elapsed(row.get('longest_gap_seconds') or 0),
        'interaction_count': int(row.get('interaction_count') or 0),
        'last_initiator': row.get('last_initiator') or 'unknown',
        'last_source': row.get('last_source') or 'chat',
    }


def serialize_snapshot(snapshot: Optional[Dict]) -> Optional[Dict]:
    """把 snapshot 变成可 JSON 序列化的 dict，供 memory_jobs 持久化。"""
    if not snapshot:
        return None
    out = {}
    for key, value in snapshot.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        else:
            out[key] = value
    return out


def _derived_temporal_fields(now_utc: Optional[datetime],
                             previous_event: Optional[datetime]) -> Dict:
    """Facts the generator should not have to infer from raw timestamps."""
    now_local = _as_local(now_utc)
    previous_local = _as_local(previous_event) if previous_event else None
    current_phase = clock_phase(now_local)
    fields = {
        'current_timestamp': _iso_local(now_local),
        'current_timestamp_utc': _iso_utc(now_utc or now_local),
        'current_clock_phase': current_phase,
        'previous_relevant_event_kind': None,
        'previous_relevant_event_timestamp': None,
        'previous_relevant_event_timestamp_utc': None,
        'previous_relevant_event_cn': None,
        'previous_clock_phase': None,
        'clock_phase_transition': None,
        'calendar_relation': 'first_contact',
        'same_calendar_day': None,
        'cross_calendar_day': None,
        'semantic_gap_category': 'first_contact',
        'temporal_language_constraints': (
            '首次时间记录；不要凭空说刚才、好久不见或隔了多久。'
        ),
    }
    if not previous_local:
        return fields

    elapsed = _seconds_between(previous_event, now_utc)
    previous_phase = clock_phase(previous_local)
    same_day = previous_local.date() == now_local.date()
    fields.update({
        'previous_relevant_event_kind': 'last_interaction',
        'previous_relevant_event_timestamp': _iso_local(previous_local),
        'previous_relevant_event_timestamp_utc': _iso_utc(previous_event),
        'previous_relevant_event_cn': _format_cn(previous_event),
        'previous_clock_phase': previous_phase,
        'clock_phase_transition': f'{previous_phase} -> {current_phase}',
        'calendar_relation': (
            'same_calendar_day' if same_day else 'cross_calendar_day'
        ),
        'same_calendar_day': same_day,
        'cross_calendar_day': not same_day,
        'semantic_gap_category': semantic_gap_category(elapsed, same_day),
        'temporal_language_constraints': temporal_language_constraints(
            elapsed, same_day, previous_phase, current_phase,
        ),
    })
    return fields


def clock_phase(local_dt: Optional[datetime]) -> Optional[str]:
    if not local_dt:
        return None
    hour = local_dt.hour
    if hour < 5:
        return 'early_morning(凌晨)'
    if hour < 9:
        return 'morning(早上)'
    if hour < 12:
        return 'late_morning(上午)'
    if hour < 14:
        return 'noon(中午)'
    if hour < 18:
        return 'afternoon(下午)'
    if hour < 22:
        return 'evening(晚上)'
    return 'late_night(深夜)'


def semantic_gap_category(seconds, same_calendar_day=None) -> str:
    bucket = classify_gap(seconds)
    if bucket in ('first_contact', 'unknown', 'continuous', 'short_gap'):
        return bucket
    if same_calendar_day is True:
        if int(seconds or 0) >= 12 * 3600:
            return 'same_day_long_gap'
        return 'same_day_gap'
    if same_calendar_day is False and bucket == 'overnight':
        return 'cross_day_overnight'
    return bucket


def temporal_language_constraints(seconds, same_calendar_day=None,
                                  previous_phase=None,
                                  current_phase=None) -> str:
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return '时间差未知；不确定时不要使用刚才/さっき/just now。'
    if seconds < 10 * 60:
        return '连续对话；可以自然承接，但仍以 current_timestamp 为准。'
    if seconds < 2 * 3600:
        return '短间隔；可以接上旧话题，但不要夸张成隔了很久。'

    banned = '刚才 / さっき / just now / a moment ago'
    preferred = '上次、 earlier、刚才那会儿以外的具体时间表达'
    if same_calendar_day:
        preferred = '今天早些时候、今天凌晨/上午/下午、几个小时前'
    elif seconds < 36 * 3600:
        preferred = '昨天/今天、昨晚/今早、上次聊到时'
    phase_hint = ''
    if previous_phase and current_phase:
        phase_hint = f'；时段已经从 {previous_phase} 变成 {current_phase}'
    return (
        f'长间隔（约 {format_elapsed(seconds)}）{phase_hint}。'
        f'禁止把 previous_relevant_event 说成 {banned}；'
        f'应改用 {preferred}，或直接说约 {format_elapsed(seconds)} 前。'
    )


def build_calendar_grounding(now_utc: Optional[datetime] = None,
                             user_text: str = '') -> str:
    """Build deterministic local-calendar facts for the generation prompt."""
    current = _coerce_dt(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    if current is None:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    now_cn = current.astimezone(CN_TZ).replace(second=0, microsecond=0)

    today_morning = _local_anchor(now_cn, 8)
    today_noon = _local_anchor(now_cn, 12)
    today_evening = _local_anchor(now_cn, 18)
    tonight = _local_anchor(now_cn, 21)
    tomorrow_noon = today_noon + timedelta(days=1)
    next_noon = today_noon if now_cn <= today_noon else tomorrow_noon

    if now_cn <= today_noon:
        noon_rule = (
            f'今天 12:00 尚未过去；面向未来的“到中午/昼まで”指今天 '
            f'{today_noon.strftime("%Y-%m-%d 12:00")}。'
        )
    else:
        noon_rule = (
            f'今天 12:00 已经过去；面向未来的“到中午/昼まで”只能指下一次中午，'
            f'即明天 {tomorrow_noon.strftime("%Y-%m-%d 12:00")}。'
            '严禁说“今天中午还有几小时”。'
        )

    explicit_rule = ''
    normalized_text = str(user_text or '')
    tomorrow_noon_tokens = ('明天中午', '明日の昼', '明日のお昼', '明日昼')
    today_noon_tokens = ('今天中午', '今日の昼', '今日のお昼', '今日昼')
    if any(token in normalized_text for token in tomorrow_noon_tokens):
        explicit_rule = (
            f'\n- 本轮用户所说的“明天中午” = '
            f'{tomorrow_noon.strftime("%Y-%m-%d 12:00")}，'
            f'{_describe_delta(tomorrow_noon, now_cn)}；绝不是今天中午。'
        )
    elif any(token in normalized_text for token in today_noon_tokens):
        explicit_rule = (
            f'\n- 本轮用户所说的“今天中午” = '
            f'{today_noon.strftime("%Y-%m-%d 12:00")}，'
            f'{_describe_delta(today_noon, now_cn)}。'
        )

    return f'''【确定性日历锚点——后端已计算，不得凭感觉改写】
- 当前：{now_cn.strftime("%Y-%m-%d %H:%M")}（北京时间）
{_anchor_line('今天早上', today_morning, now_cn)}
{_anchor_line('今天中午', today_noon, now_cn)}
{_anchor_line('今天傍晚', today_evening, now_cn)}
{_anchor_line('今晚', tonight, now_cn)}
{_anchor_line('下一次中午', next_noon, now_cn)}{explicit_rule}

硬约束：
1. {noon_rule}
2. “今天/明天/昨天”必须先换成完整日期，再判断目标时刻在当前时刻之前还是之后。
3. 说“还有多久”只能用于未来；目标已过去必须说“已经过去”。
4. 只有“九点”而没有早上/晚上时，不得擅自把 09:00 和 21:00 互换；上下文不足就自然确认。'''


def find_reply_calendar_conflict(user_text: str, assistant_text: str,
                                 now_utc: Optional[datetime] = None) -> Optional[str]:
    """Return a stable code for an obvious named-time contradiction."""
    current = _coerce_dt(now_utc) if now_utc is not None else datetime.now(timezone.utc)
    if current is None:
        current = datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    now_cn = current.astimezone(CN_TZ).replace(second=0, microsecond=0)
    today_noon = _local_anchor(now_cn, 12)

    user = str(user_text or '')
    reply = str(assistant_text or '')
    tomorrow_noon_tokens = ('明天中午', '明日の昼', '明日のお昼', '明日昼')
    today_noon_tokens = ('今天中午', '今日の昼', '今日のお昼', '今日昼')
    today_noon_corrections = (
        '不是今天中午', '并非今天中午', '今天中午已经', '今天中午早就',
        '今日の昼じゃない', '今日の昼ではなく', '今日の昼じゃなく',
        '今日の昼はもう', '今日の昼ならもう',
    )

    if any(token in user for token in tomorrow_noon_tokens):
        says_today_noon = any(token in reply for token in today_noon_tokens)
        corrects_today_noon = any(token in reply for token in today_noon_corrections)
        if says_today_noon and not corrects_today_noon:
            return 'tomorrow_noon_rewritten_as_today'

    if now_cn > today_noon:
        future_noon_terms = ('昼まで', '中午再', '到中午', '等到中午', '忍到中午')
        future_cues = (
            '我慢すれば', '我慢でき', '我慢して', '待て', 'あと',
            '再忍', '等到', '还有',
        )
        says_future_noon = (
            any(token in reply for token in future_noon_terms)
            and any(token in reply for token in future_cues)
        )
        names_tomorrow = any(token in reply for token in tomorrow_noon_tokens)
        if says_future_noon and not names_tomorrow:
            return 'past_noon_used_as_future_deadline'

    return None


def _local_anchor(now_cn: datetime, hour: int, minute: int = 0) -> datetime:
    return now_cn.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _anchor_line(label: str, target: datetime, now_cn: datetime) -> str:
    return (
        f'- {label}：{target.strftime("%Y-%m-%d %H:%M")}，'
        f'{_describe_delta(target, now_cn)}'
    )


def _describe_delta(target: datetime, now_cn: datetime) -> str:
    delta_seconds = int((target - now_cn).total_seconds())
    if abs(delta_seconds) < 60:
        return '就是现在'
    if delta_seconds > 0:
        return f'还有{_format_clock_delta(delta_seconds)}'
    return f'已过去{_format_clock_delta(-delta_seconds)}'


def _format_clock_delta(seconds: int) -> str:
    total_minutes = max(1, int(round(seconds / 60)))
    days, remaining_minutes = divmod(total_minutes, 24 * 60)
    hours, minutes = divmod(remaining_minutes, 60)
    parts = []
    if days:
        parts.append(f'{days}天')
    if hours:
        parts.append(f'{hours}小时')
    if minutes:
        parts.append(f'{minutes}分钟')
    return ''.join(parts) or '不到1分钟'


def record_turn(user_id: str, character_id: str, source: str = 'chat',
                occurred_at: Optional[datetime] = None,
                has_user_message: bool = True,
                has_assistant_message: bool = True,
                prior_snapshot: Optional[Dict] = None) -> Optional[Dict]:
    """记录一次真实互动结束。

    has_user_message/has_assistant_message 用来区分普通聊天、忙碌只已读、
    主动消息等不同入口。失败时只打日志，不阻断主流程。
    """
    now = _as_utc_naive(occurred_at)
    last_initiator = _last_initiator(has_user_message, has_assistant_message)

    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''SELECT first_interaction_at, last_user_message_at,
                              last_assistant_message_at, last_interaction_at,
                              longest_gap_seconds, interaction_count
                       FROM temporal_awareness
                       WHERE user_id = %s AND character_id = %s''',
                    (user_id, character_id))
        row = cur.fetchone()

        if row:
            first, last_user, last_assistant, previous, longest, count = row
            previous = _coerce_dt(previous)
            gap = _seconds_between(previous, now) if previous else 0
            longest = max(int(longest or 0), gap)
            first = _coerce_dt(first) or previous or now
            next_user = now if has_user_message else last_user
            next_assistant = now if has_assistant_message else last_assistant

            cur.execute('''UPDATE temporal_awareness
                           SET first_interaction_at = %s,
                               last_user_message_at = %s,
                               last_assistant_message_at = %s,
                               previous_interaction_at = last_interaction_at,
                               last_interaction_at = %s,
                               last_gap_seconds = %s,
                               longest_gap_seconds = %s,
                               interaction_count = %s,
                               last_initiator = %s,
                               last_source = %s,
                               updated_at = CURRENT_TIMESTAMP
                           WHERE user_id = %s AND character_id = %s''',
                        (first, next_user, next_assistant, now, gap, longest,
                         int(count or 0) + 1, last_initiator, source,
                         user_id, character_id))
        else:
            history = _normalize_snapshot(prior_snapshot)
            if not history or not history.get('has_history'):
                history = _load_history_fallback(user_id, character_id)

            previous = _coerce_dt(history.get('last_interaction_at')) if history else None
            first = _coerce_dt(history.get('first_interaction_at')) if history else None
            last_user = _coerce_dt(history.get('last_user_message_at')) if history else None
            last_assistant = _coerce_dt(history.get('last_assistant_message_at')) if history else None
            gap = _seconds_between(previous, now) if previous else 0
            longest = max(int((history or {}).get('longest_gap_seconds') or 0), gap)
            count = int((history or {}).get('interaction_count') or 0) + 1
            first = first or previous or now
            next_user = now if has_user_message else last_user
            next_assistant = now if has_assistant_message else last_assistant

            cur.execute('''INSERT INTO temporal_awareness
                (user_id, character_id, first_interaction_at,
                 last_user_message_at, last_assistant_message_at,
                 last_interaction_at, previous_interaction_at,
                 last_gap_seconds, longest_gap_seconds, interaction_count,
                 last_initiator, last_source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                (user_id, character_id, first, next_user, next_assistant,
                 now, previous, gap, longest, count, last_initiator, source))

        conn.commit()
        cur.close()
        conn.close()
        return get_temporal_snapshot(user_id, character_id, now_utc=now)
    except Exception as e:
        print(f'[temporal] 记录失败 {user_id}/{character_id}: {e}')
        return None


def record_user_message(user_id: str, character_id: str, source: str = 'chat',
                        occurred_at: Optional[datetime] = None,
                        prior_snapshot: Optional[Dict] = None):
    return record_turn(user_id, character_id, source, occurred_at,
                       has_user_message=True, has_assistant_message=False,
                       prior_snapshot=prior_snapshot)


def record_assistant_message(user_id: str, character_id: str, source: str = 'proactive',
                             occurred_at: Optional[datetime] = None,
                             prior_snapshot: Optional[Dict] = None):
    return record_turn(user_id, character_id, source, occurred_at,
                       has_user_message=False, has_assistant_message=True,
                       prior_snapshot=prior_snapshot)


def build_prompt_context(user_id: str, character_id: str,
                         snapshot: Optional[Dict] = None) -> str:
    """给主生成 prompt 的持久时间上下文。"""
    snap = _normalize_snapshot(snapshot) or get_temporal_snapshot(user_id, character_id)
    if not snap.get('has_history'):
        return f'''

【TEMPORAL SNAPSHOT——生成前必须优先遵守】
- current_timestamp: {snap.get('current_timestamp') or snap.get('now_cn')}
- previous_relevant_event: none
- elapsed: first_contact
- calendar_relation: first_contact
- clock_phase_transition: none
- gap_category: first_contact
- temporal_language_constraints: {snap.get('temporal_language_constraints') or '首次时间记录；不要凭空说刚才、好久不见或隔了多久。'}

【真实经过时间——持久时间账本】
这是后端第一次记录到你和她在这个角色维度的互动。若上方长期记忆里已有旧事，以那些旧事为准；不要凭空断言你们"刚认识"或"很久没见"。

用法：
- 你能感知真实时间在流动，但不要把时间间隔当作情绪本身。
- 不因为"过了多久"自动变热、变冷、想念或生气；只有她的话、你们的记忆、约定和关系账本能决定这件事。'''

    elapsed = snap.get('elapsed_label') or '未知'
    last_cn = snap.get('last_interaction_cn') or '未知'
    first_cn = snap.get('first_interaction_cn') or '未知'
    last_user_cn = snap.get('last_user_message_cn')
    last_assistant_cn = snap.get('last_assistant_message_cn')
    count = snap.get('interaction_count') or 0
    longest = snap.get('longest_gap_label') or '未知'
    initiator = _actor_word(snap.get('last_initiator'))
    guidance = _gap_guidance(snap.get('gap_bucket'))

    user_line = f'\n- 她上次主动说话：{last_user_cn}' if last_user_cn else ''
    assistant_line = f'\n- 你上次发给她：{last_assistant_cn}' if last_assistant_cn else ''

    return f'''

【TEMPORAL SNAPSHOT——生成前必须优先遵守】
- current_timestamp: {snap.get('current_timestamp') or snap.get('now_cn')}
- previous_relevant_event:
  · kind: {snap.get('previous_relevant_event_kind') or 'last_interaction'}
  · timestamp: {snap.get('previous_relevant_event_timestamp') or last_cn}
  · human_label: {last_cn}
- elapsed: {elapsed}（{snap.get('elapsed_seconds_since_last_interaction')} seconds）
- calendar_relation: {snap.get('calendar_relation') or 'unknown'}; same_calendar_day={snap.get('same_calendar_day')}; cross_calendar_day={snap.get('cross_calendar_day')}
- clock_phase_transition: {snap.get('clock_phase_transition') or 'unknown'}
- gap_category: {snap.get('semantic_gap_category') or snap.get('gap_bucket')}
- temporal_language_constraints: {snap.get('temporal_language_constraints')}

【真实经过时间——持久时间账本】
- 现在：{snap.get('now_cn')}
- 上次有记录的互动：{last_cn}（约 {elapsed} 前，最后由{initiator}开口）{user_line}{assistant_line}
- 最早有记录的互动：{first_cn}；累计记录互动约 {count} 轮；最长断档约 {longest}

用法：
1. 这是真实经过时间，不是某条历史消息里的字面时间。你可以知道"刚刚还在聊"、"隔了几个小时"、"隔夜/隔了几天又回来"。
2. {guidance}
3. 时间间隔只提供语境：它可以影响你是否自然翻篇、是否提到"刚才/昨天/上次/好久没见"，也可以帮助理解等待、失约、重逢。若 temporal_language_constraints 禁用了"刚才/さっき/just now"，最终回复里绝不能出现同义表达。
4. 严禁把时间间隔直接换算成情绪或关系分数：不许因为过了 X 天就自动更爱/更冷/更生气。情绪看这一轮内容，关系看账本和真实事件。'''


def build_memory_context(snapshot: Optional[Dict]) -> str:
    """给记忆提取器看的本轮时间线。"""
    snap = _normalize_snapshot(snapshot)
    if not snap:
        return ''
    if not snap.get('has_history'):
        return f'''

【本轮真实时间线】
本轮发生时间：{snap.get('now_cn') or '未知'}。这是该角色维度的首次时间记录。

记忆规则：不要仅因为"首次记录"就生成关系或情绪记忆；只提取本轮对话里明确发生、明确告知、或对未来有持续意义的事实。'''

    return f'''

【本轮真实时间线】
本轮发生时间：{snap.get('now_cn') or '未知'}。
距离上一轮有记录互动：约 {snap.get('elapsed_label') or '未知'}（上次互动：{snap.get('last_interaction_cn') or '未知'}；timestamp={snap.get('previous_relevant_event_timestamp') or '未知'}）。
日历关系：{snap.get('calendar_relation') or '未知'}；时段变化：{snap.get('clock_phase_transition') or '未知'}；gap_category={snap.get('semantic_gap_category') or snap.get('gap_bucket') or '未知'}。
她上次主动说话：{snap.get('last_user_message_cn') or '暂无记录'}。
你上次发给她：{snap.get('last_assistant_message_cn') or '暂无记录'}。

记忆规则：
1. 这段时间差可用于理解"刚才/昨天/上次/隔了几天/好久没聊"等表达。
2. 如果这次对话明确围绕断档、等待、失约、重逢、持续状态展开，可以把对应事件写入记忆。
3. 不要单独提取"过了 X 小时/天"作为情绪结论；更不要写成"因为过了 X 天所以我更喜欢/更讨厌她"。'''


def build_relationship_context(snapshot: Optional[Dict]) -> str:
    """给关系 observer/reader 的客观时间线。"""
    snap = _normalize_snapshot(snapshot)
    if not snap:
        return ''
    if not snap.get('has_history'):
        return f'''

【客观时间线】
本轮时间：{snap.get('now_cn') or '未知'}。这是该角色维度的首次时间记录。
只把它当作时间事实，不要由此推断关系性质。'''

    return f'''

【客观时间线】
本轮时间：{snap.get('now_cn') or '未知'}。
距离上一轮有记录互动：约 {snap.get('elapsed_label') or '未知'}（上次：{snap.get('last_interaction_cn') or '未知'}；timestamp={snap.get('previous_relevant_event_timestamp') or '未知'}）。
日历关系：{snap.get('calendar_relation') or '未知'}；时段变化：{snap.get('clock_phase_transition') or '未知'}；gap_category={snap.get('semantic_gap_category') or snap.get('gap_bucket') or '未知'}。
她上次主动说话：{snap.get('last_user_message_cn') or '暂无记录'}；你上次发给她：{snap.get('last_assistant_message_cn') or '暂无记录'}。

使用边界：时间跨度可以帮助判断等待、守约/失约、久别后回来、连续陪伴等事件；但时间本身不是感情结论，不直接改变 warmth/trust/attachment/passion。'''


def format_elapsed(seconds) -> str:
    if seconds is None:
        return '未知'
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return '未知'
    seconds = max(0, seconds)
    if seconds < 60:
        return '刚刚'
    if seconds < 3600:
        return f'{max(1, round(seconds / 60))}分钟'
    if seconds < 86400:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        if minutes >= 10:
            return f'{hours}小时{minutes}分钟'
        return f'{hours}小时'
    if seconds < 7 * 86400:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f'{days}天{hours}小时' if hours else f'{days}天'
    if seconds < 30 * 86400:
        weeks = seconds // (7 * 86400)
        days = (seconds % (7 * 86400)) // 86400
        return f'{weeks}周{days}天' if days else f'{weeks}周'
    if seconds < 365 * 86400:
        months = seconds // (30 * 86400)
        days = (seconds % (30 * 86400)) // 86400
        return f'{months}个月{days}天' if days else f'{months}个月'
    years = seconds // (365 * 86400)
    months = (seconds % (365 * 86400)) // (30 * 86400)
    return f'{years}年{months}个月' if months else f'{years}年'


def classify_gap(seconds) -> str:
    if seconds is None:
        return 'first_contact'
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return 'unknown'
    if seconds < 10 * 60:
        return 'continuous'
    if seconds < 2 * 3600:
        return 'short_gap'
    if seconds < 12 * 3600:
        return 'same_day_gap'
    if seconds < 36 * 3600:
        return 'overnight'
    if seconds < 7 * 86400:
        return 'few_days'
    if seconds < 30 * 86400:
        return 'long_gap'
    return 'very_long_gap'


def _load_row(user_id: str, character_id: str) -> Optional[Dict]:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''SELECT first_interaction_at, last_user_message_at,
                              last_assistant_message_at, last_interaction_at,
                              previous_interaction_at, last_gap_seconds,
                              longest_gap_seconds, interaction_count,
                              last_initiator, last_source
                       FROM temporal_awareness
                       WHERE user_id = %s AND character_id = %s''',
                    (user_id, character_id))
        row = cur.fetchone()
        cur.close()
        conn.close()
    except Exception:
        return None
    if not row:
        return None
    return {
        'first_interaction_at': row[0],
        'last_user_message_at': row[1],
        'last_assistant_message_at': row[2],
        'last_interaction_at': row[3],
        'previous_interaction_at': row[4],
        'last_gap_seconds': row[5],
        'longest_gap_seconds': row[6],
        'interaction_count': row[7],
        'last_initiator': row[8],
        'last_source': row[9],
    }


def _load_history_fallback(user_id: str, character_id: str) -> Optional[Dict]:
    """新表还没积累时，从已有 short_memory/chat_log 尽量补一个只读快照。"""
    row = _load_short_memory_fallback(user_id, character_id)
    if row:
        return row
    return _load_chatlog_fallback(user_id, character_id)


def _load_short_memory_fallback(user_id: str, character_id: str) -> Optional[Dict]:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''SELECT MIN(timestamp), MAX(timestamp), COUNT(*)
                       FROM short_memory
                       WHERE user_id = %s AND character_id = %s''',
                    (user_id, character_id))
        first, last, count = cur.fetchone()
        if not last:
            cur.close()
            conn.close()
            return None
        cur.execute('''SELECT role, timestamp FROM short_memory
                       WHERE user_id = %s AND character_id = %s
                       ORDER BY timestamp DESC LIMIT 1''',
                    (user_id, character_id))
        last_role_row = cur.fetchone()
        cur.execute('''SELECT MAX(timestamp) FROM short_memory
                       WHERE user_id = %s AND character_id = %s AND role = 'user' ''',
                    (user_id, character_id))
        last_user = cur.fetchone()[0]
        cur.execute('''SELECT MAX(timestamp) FROM short_memory
                       WHERE user_id = %s AND character_id = %s AND role = 'assistant' ''',
                    (user_id, character_id))
        last_assistant = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {
            'first_interaction_at': first,
            'last_user_message_at': last_user,
            'last_assistant_message_at': last_assistant,
            'last_interaction_at': last,
            'previous_interaction_at': None,
            'last_gap_seconds': 0,
            'longest_gap_seconds': 0,
            'interaction_count': count or 0,
            'last_initiator': _role_to_initiator(last_role_row[0] if last_role_row else ''),
            'last_source': 'short_memory_fallback',
        }
    except Exception:
        return None


def _load_chatlog_fallback(user_id: str, character_id: str) -> Optional[Dict]:
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('''SELECT MIN(created_at), MAX(created_at), COUNT(*)
                       FROM chat_log
                       WHERE user_id = %s AND chat_id = %s''',
                    (user_id, character_id))
        first, last, count = cur.fetchone()
        if not last:
            cur.close()
            conn.close()
            return None
        cur.execute('''SELECT role, created_at FROM chat_log
                       WHERE user_id = %s AND chat_id = %s
                       ORDER BY created_at DESC LIMIT 1''',
                    (user_id, character_id))
        last_role_row = cur.fetchone()
        cur.execute('''SELECT MAX(created_at) FROM chat_log
                       WHERE user_id = %s AND chat_id = %s AND role = 'user' ''',
                    (user_id, character_id))
        last_user = cur.fetchone()[0]
        cur.execute('''SELECT MAX(created_at) FROM chat_log
                       WHERE user_id = %s AND chat_id = %s AND role = 'gojo' ''',
                    (user_id, character_id))
        last_assistant = cur.fetchone()[0]
        cur.close()
        conn.close()
        return {
            'first_interaction_at': first,
            'last_user_message_at': last_user,
            'last_assistant_message_at': last_assistant,
            'last_interaction_at': last,
            'previous_interaction_at': None,
            'last_gap_seconds': 0,
            'longest_gap_seconds': 0,
            'interaction_count': count or 0,
            'last_initiator': _role_to_initiator(last_role_row[0] if last_role_row else ''),
            'last_source': 'chat_log_fallback',
        }
    except Exception:
        return None


def _normalize_snapshot(snapshot: Optional[Dict]) -> Optional[Dict]:
    if not snapshot:
        return None
    snap = dict(snapshot)
    for key in (
        'now_utc', 'first_interaction_at', 'last_interaction_at',
        'previous_interaction_at', 'last_user_message_at',
        'last_assistant_message_at',
    ):
        if key in snap:
            snap[key] = _coerce_dt(snap.get(key))

    if snap.get('now_utc') and not snap.get('now_cn'):
        snap['now_cn'] = _format_cn(snap['now_utc'])
    for key, out_key in (
        ('first_interaction_at', 'first_interaction_cn'),
        ('last_interaction_at', 'last_interaction_cn'),
        ('previous_interaction_at', 'previous_interaction_cn'),
        ('last_user_message_at', 'last_user_message_cn'),
        ('last_assistant_message_at', 'last_assistant_message_cn'),
    ):
        if snap.get(key) and not snap.get(out_key):
            snap[out_key] = _format_cn(snap[key])

    elapsed = snap.get('elapsed_seconds_since_last_interaction')
    if elapsed is None and snap.get('has_history') and snap.get('now_utc') and snap.get('last_interaction_at'):
        elapsed = _seconds_between(snap['last_interaction_at'], snap['now_utc'])
        snap['elapsed_seconds_since_last_interaction'] = elapsed
    if elapsed is not None and not snap.get('elapsed_label'):
        snap['elapsed_label'] = format_elapsed(elapsed)
    if elapsed is not None and not snap.get('gap_bucket'):
        snap['gap_bucket'] = classify_gap(elapsed)
    if snap.get('longest_gap_seconds') is not None and not snap.get('longest_gap_label'):
        snap['longest_gap_label'] = format_elapsed(snap.get('longest_gap_seconds'))
    derived = _derived_temporal_fields(
        snap.get('now_utc'), snap.get('last_interaction_at'),
    )
    for key, value in derived.items():
        if snap.get(key) is None:
            snap[key] = value
    return snap


def _gap_guidance(bucket: str) -> str:
    mapping = {
        'continuous': '这是连续对话，接续刚才的话即可，但仍以当前时间为准。',
        'short_gap': '中间只隔了一小会儿，可以自然接上，不要说得像隔了很久。',
        'same_day_gap': '同一天隔了几个小时，旧话题可以接，但要知道不是刚刚发生。',
        'overnight': '中间可能隔了半天或一晚；问候、作息、昨天/今天的指代要按真实日期处理。',
        'few_days': '已经隔了几天，旧话题需要用过去式；她回来可以被你注意到，但别自动脑补原因。',
        'long_gap': '已经隔了一周以上，这是明显断档；可以意识到"有段时间没聊"，但别把沉默直接判成感情变化。',
        'very_long_gap': '已经隔了很久；除非她主动解释或旧约定相关，否则不要编造这段空白里发生了什么。',
    }
    return mapping.get(bucket, '把时间差当作客观背景使用，拿不准就自然确认。')


def _last_initiator(has_user_message: bool, has_assistant_message: bool) -> str:
    if has_assistant_message:
        return 'assistant'
    if has_user_message:
        return 'user'
    return 'system'


def _role_to_initiator(role: str) -> str:
    if role == 'user':
        return 'user'
    if role in ('assistant', 'gojo', 'character'):
        return 'assistant'
    return 'unknown'


def _actor_word(value: str) -> str:
    return {
        'user': '她',
        'assistant': '你',
        'system': '系统',
    }.get(value or '', '未知一方')


def _seconds_between(start: Optional[datetime], end: Optional[datetime]) -> int:
    if not start or not end:
        return 0
    start = _as_utc_naive(start)
    end = _as_utc_naive(end)
    return max(0, int((end - start).total_seconds()))


def _as_local(dt: Optional[datetime]) -> datetime:
    value = _coerce_dt(dt)
    if value is None:
        value = datetime.utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(CN_TZ).replace(second=0, microsecond=0)


def _as_aware_utc(dt: Optional[datetime]) -> datetime:
    value = _coerce_dt(dt)
    if value is None:
        value = datetime.utcnow()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(second=0, microsecond=0)


def _iso_local(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return _as_local(dt).isoformat()


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    return _as_aware_utc(dt).isoformat()


def _as_utc_naive(dt: Optional[datetime]) -> datetime:
    if dt is None:
        return datetime.utcnow()
    dt = _coerce_dt(dt)
    if dt is None:
        return datetime.utcnow()
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.replace(tzinfo=None)


def _coerce_dt(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            try:
                return datetime.strptime(text[:19], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                return None
    return None


def _format_cn(dt: Optional[datetime]) -> Optional[str]:
    if not dt:
        return None
    dt = _coerce_dt(dt)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    cn = dt.astimezone(CN_TZ)
    weekdays = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']
    return f'{cn.strftime("%Y-%m-%d %H:%M")}（{weekdays[cn.weekday()]}）'
