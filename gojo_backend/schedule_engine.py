"""schedule_engine.py —— 让角色自己排一天的行程

每天生成一次,由 LLM 按角色的背景设定安排。
不是随机填格子 —— 是"这个人今天大概会怎么过"。

★ 关键:can_reply 由 LLM 逐条判断
    真的走不开(上课/出任务/洗澡/开会) → false
    能摸鱼(探店/逛街/查账/吃饭/发呆) → true

★ 约束:忙碌时段一天不超过 4 小时、不超过 4 段。
    全天都忙的话用户就没得聊了,那不是陪伴 App 该有的样子。
"""
from datetime import datetime, timedelta
from config import CN_TZ, MODEL_CN_AUX
from characters import get_character
from characters_data._loader import load_core
from character_rhythm import get_rhythm_text, get_sleep_window
import db_schedule


def _now():
    return datetime.now(CN_TZ)


# ── 忙碌配额配置 ──
MAX_BUSY_SLOTS = 4        # 一天最多几段走不开
MAX_BUSY_MINUTES = 240    # 走不开总时长上限(分钟),不含睡眠
SLEEP_CAN_REPLY = True    # ★ 睡觉时能不能回。默认 True ——
                          #   深夜常常正是用户想聊天的时候,睡死了 App 就废了。
                          #   当作他半夜醒着刷手机 / 睡得浅。

# 越"不可能回消息"的活动优先级越高,配额不够时优先保留它们。
# 之前是先到先得,结果"起床洗漱"占了配额、"咒灵讨伐"被放行,完全反了。
_BUSY_PRIORITY = [
    (10, ('任务', '讨伐', '战斗', '出勤', '祓除', '交战', '出击')),
    (9,  ('上课', '授课', '教学', '讲课', '辅导', '训练')),
    (8,  ('会议', '开会', '谈判', '汇报', '高层')),
    (6,  ('洗澡', '泡澡', '沐浴')),
    (4,  ('开车', '驾驶')),
    (2,  ('起床', '洗漱', '换衣', '打扮', '通勤', '移动')),
]


def _busy_priority(title: str) -> int:
    """这件事有多不可能回消息。数字越大越走不开。"""
    for score, keywords in _BUSY_PRIORITY:
        if any(k in title for k in keywords):
            return score
    return 5      # 没匹配上的给中等优先级


def generate_daily_schedule(character_id, user_id, target_date=None, force=False):
    """给某个角色生成某天的日程。返回条目列表或 None。

    force=False 时,当天已有日程就跳过(避免重复生成覆盖掉你手动改过的)。
    """
    target_date = target_date or _now().date()

    if not force and db_schedule.has_schedule(character_id, user_id, target_date):
        return None

    char = get_character(character_id)
    if not char:
        print(f'[schedule] 角色 {character_id} 不存在')
        return None
    char_name = char['name']

    # 拿角色的核心设定,让排程贴合人设
    try:
        core = load_core(character_id)
        core_prompt = (core.get('core_prompt') or '')[:1500]
    except Exception:
        core_prompt = char.get('core_prompt', '')[:1500]

    weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][target_date.weekday()]
    is_weekend = target_date.weekday() >= 5

    # ★ 官方设定的作息骨架。没有这个,LLM 会按普通上班族排(23点睡7点起),
    #   而五条公式书写的是 04:00 就寝、07:00 起床、一天睡 3 小时。
    rhythm = get_rhythm_text(character_id)
    rhythm_block = f'\n{rhythm}\n' if rhythm else ''

    prompt = f'''你是{char_name}。请安排你自己 {target_date}（{weekday_cn}）这一天的行程。

【你是谁】
{core_prompt}
{rhythm_block}
【要求】
1. 从早上起床到晚上睡觉,排 8-12 个时间段,覆盖一整天。
2. 每一段要符合【你的身份和性格】——不是通用打工人日程,
   是"{char_name}这个人今天会怎么过"。
   {'今天是周末,安排可以更随性。' if is_weekend else '今天是工作日,该有的正事要有。'}

★ 【每天要不一样】这一点很重要:
   不要每天都排一模一样的格子。真实的人今天和明天过得不同 ——
   · 今天可能一整天泡在任务里,明天可能一个任务都没有
   · 可能临时决定绕路去买某家限定甜品、去新开的店探店、逛街
   · 可能翘掉一节课去偷懒,也可能被临时叫去救场
   · 可能纯粹发呆一小时,什么正事都不干
   在作息骨架里【自由填内容】,今天就想一个"今天他会干嘛"的答案,
   别套模板。
3. 每段写清楚:开始时间、结束时间、做什么、在哪、以及一句你自己的碎碎念。
4. ★ 每段要标注 can_reply —— 这段时间你能不能回手机消息:
   · false（真的走不开）:上课、出任务、战斗、洗澡、正式会议、开车
   · true（能摸鱼）:吃饭、逛街、探店、查资料、发呆、休息、通勤(非自己开车)
5. ★ 重要限制:can_reply=false 的时段【一天最多 4 段、总共不超过 4 小时】。
   剩下的时间都要能回消息 —— 你不是全天失联的人。
6. 睡觉时段也要排(通常 can_reply=false),但别排太长。

【时间格式】必须是 "HH:MM" 24 小时制,前后时段要连得上,不要留空档也不要重叠。

【严格按这个 JSON 输出,只输出一行,不要任何解释】
{{"schedule":[
  {{"start_time":"07:00","end_time":"07:45","title":"做什么","location":"在哪","note":"一句碎碎念","can_reply":true}},
  {{"start_time":"07:45","end_time":"09:00","title":"...","location":"...","note":"...","can_reply":false}}
]}}'''

    try:
        from ai_client import create_chat
        raw, _usage = create_chat(
            model=MODEL_CN_AUX, max_tokens=3000,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = (raw or '').strip()
        if not raw:
            print(f'[schedule] {character_id} 生成返回空')
            return None

        from utils import extract_json
        parsed = extract_json(raw)
        if not parsed or not isinstance(parsed.get('schedule'), list):
            print(f'[schedule] {character_id} 解析失败: {raw[:200]}')
            return None

        items = _sanitize(parsed['schedule'], character_id)
        if not items:
            print(f'[schedule] {character_id} 清洗后没有有效条目')
            return None

        db_schedule.save_schedule(character_id, user_id, target_date, items)
        busy = [i for i in items if not i['can_reply']]
        print(f'[schedule] ✅ {char_name} {target_date} 共 {len(items)} 段,'
              f'其中走不开 {len(busy)} 段')
        return items

    except Exception as e:
        print(f'[schedule] {character_id} 生成出错: {e}')
        return None


def _sanitize(raw_items, character_id=None):
    """清洗 LLM 输出:校验时间格式、强制忙碌上限。

    LLM 经常会把一整天排满忙碌时段(它觉得这样"更真实"),
    但那样用户一天都聊不上天。这里硬性砍到 4 段 / 4 小时以内。
    """
    import re
    ok = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        st = str(it.get('start_time', '')).strip()
        et = str(it.get('end_time', '')).strip()
        title = str(it.get('title', '')).strip()
        if not re.fullmatch(r'\d{1,2}:\d{2}', st) or not re.fullmatch(r'\d{1,2}:\d{2}', et):
            continue
        if not title:
            continue
        # 补零成 HH:MM,保证字符串比较能当时间比较用
        st = f'{int(st.split(":")[0]):02d}:{st.split(":")[1]}'
        et = f'{int(et.split(":")[0]):02d}:{et.split(":")[1]}'
        ok.append({
            'start_time': st, 'end_time': et, 'title': title[:80],
            'location': str(it.get('location', ''))[:40],
            'note': str(it.get('note', ''))[:120],
            'can_reply': bool(it.get('can_reply', True)),
        })

    ok.sort(key=lambda x: x['start_time'])

    def _mins(hhmm):
        h, m = hhmm.split(':')
        return int(h) * 60 + int(m)

    def _dur(it):
        d = _mins(it['end_time']) - _mins(it['start_time'])
        return d + 24 * 60 if d < 0 else d      # 跨午夜

    # ── 睡眠特殊处理 ──
    # 深夜往往正是用户最想聊天的时候。如果角色"睡着了"一律不回,
    # App 在半夜就完全用不了了。
    # 所以睡眠时段【默认可以回】—— 当作他半夜醒着刷手机 / 睡得浅。
    # 想改成真的不回,把 SLEEP_CAN_REPLY 设成 False。
    SLEEP_KEYWORDS = ('睡', '就寝', '寝', '休息中', '入眠')
    for it in ok:
        if any(k in it['title'] for k in SLEEP_KEYWORDS):
            it['can_reply'] = SLEEP_CAN_REPLY

    # ── ★ 睡眠时长兜底 ──
    # 光靠 prompt 不保险:LLM 很容易按"正常人"排 23:00-07:00 睡 8 小时,
    # 但五条 canon 是 04:00-07:00 只睡 3 小时。
    # 这里按 character_rhythm 配置的窗口强制纠正。
    window = get_sleep_window(character_id) if character_id else None
    if window:
        want_start, want_end = window
        sleeps = [it for it in ok if any(k in it['title'] for k in SLEEP_KEYWORDS)]
        for it in sleeps:
            if it['start_time'] != want_start or it['end_time'] != want_end:
                print(f'[schedule] 睡眠时段纠正:'
                      f'{it["start_time"]}-{it["end_time"]} → {want_start}-{want_end}')
                it['start_time'] = want_start
                it['end_time'] = want_end
        # 被睡眠覆盖掉的时段要清掉,否则会和睡眠重叠
        if sleeps:
            def _in_sleep(t):
                # 跨午夜的窗口(04:00-07:00 不跨,23:00-07:00 跨)
                if want_start <= want_end:
                    return want_start <= t < want_end
                return t >= want_start or t < want_end
            ok = [it for it in ok
                  if any(k in it['title'] for k in SLEEP_KEYWORDS)
                  or not (_in_sleep(it['start_time']) and _in_sleep(it['end_time']))]

    ok.sort(key=lambda x: x['start_time'])

    # ── 忙碌配额:按"有多不可能回消息"排优先级,不是先到先得 ──
    # 之前是先到先得,结果"起床洗漱"这种占掉配额,
    # "咒灵讨伐"反而被挤出去变成能回 —— 完全本末倒置。
    busy = [it for it in ok
            if not it['can_reply']
            and not any(k in it['title'] for k in SLEEP_KEYWORDS)]
    busy.sort(key=lambda it: (-_busy_priority(it['title']), -_dur(it)))

    kept_count = 0
    kept_minutes = 0
    keep_ids = set()
    for it in busy:
        d = _dur(it)
        if kept_count >= MAX_BUSY_SLOTS or kept_minutes + d > MAX_BUSY_MINUTES:
            continue
        keep_ids.add(id(it))
        kept_count += 1
        kept_minutes += d

    for it in busy:
        if id(it) not in keep_ids:
            it['can_reply'] = True      # 没进配额的放行

    return ok


def ensure_today(character_id, user_id):
    """确保今天有日程,没有就生成。开 App 时调一次做兜底。"""
    today = _now().date()
    if db_schedule.has_schedule(character_id, user_id, today):
        return False
    generate_daily_schedule(character_id, user_id, today)
    return True