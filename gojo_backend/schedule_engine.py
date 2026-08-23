"""schedule_engine.py —— 让角色自己排一天的行程

★ v3 修复:Claude 通过中转拒绝角色扮演
  根因:prompt 直接写"你是五条悟",触发 Claude 的 roleplay 拒绝。
  修法:用 system 参数框定为"虚构角色的日程安排 App 功能",
       user message 里只给角色设定和要求,不说"你是 XXX"。
"""
from datetime import datetime, timedelta
from config import CN_TZ, MODEL_CN_AUX
from characters import get_character
from characters_data._loader import load_core
from character_rhythm import get_rhythm_text, get_sleep_window
import db_schedule
import random


def _now():
    return datetime.now(CN_TZ)


MAX_BUSY_SLOTS = 4
MAX_BUSY_MINUTES = 240
MIN_BUSY_PRIORITY = 4
SLEEP_CAN_REPLY = True

_BUSY_PRIORITY = [
    (10, ('任务', '讨伐', '战斗', '出勤', '祓除', '交战', '出击')),
    (9,  ('上课', '授课', '教学', '讲课', '辅导', '训练')),
    (8,  ('会议', '开会', '谈判', '汇报', '高层')),
    (6,  ('洗澡', '泡澡', '沐浴')),
    (4,  ('开车', '驾驶')),
    (2,  ('起床', '洗漱', '换衣', '打扮', '通勤', '移动')),
]


def _busy_priority(title: str) -> int:
    for score, keywords in _BUSY_PRIORITY:
        if any(k in title for k in keywords):
            return score
    return 5


def _seasonal_hints(month: int) -> str:
    hints = {
        1: '正月初詣、福袋、冬季限定草莓甜品',
        2: '情人节巧克力、草莓季、梅花',
        3: '樱花季开始、春季限定抹茶',
        4: '满开樱花、花见、春季新品',
        5: '黄金周、新绿、抹茶新茶',
        6: '梅雨季、紫阳花、夏季限定刨冰',
        7: '夏祭、花火大会、刨冰',
        8: '盂兰盆节、花火、夏季限定冰品、啤酒花园',
        9: '秋季栗子甜品、月见、秋刀鱼',
        10: '万圣节限定、红叶开始、栗子蒙布朗',
        11: '红叶季、秋季限定、热饮回归',
        12: '圣诞限定、年末、冬季草莓',
    }
    return hints.get(month, '')


def generate_daily_schedule(character_id, user_id, target_date=None, force=False):
    target_date = target_date or _now().date()

    if not force and db_schedule.has_schedule(character_id, user_id, target_date):
        return None

    char = get_character(character_id)
    if not char:
        print(f'[schedule] 角色 {character_id} 不存在')
        return None
    char_name = char['name']

    try:
        core = load_core(character_id)
        core_prompt = (core.get('core_prompt') or '')[:1500]
    except Exception:
        core_prompt = char.get('core_prompt', '')[:1500]

    weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][target_date.weekday()]
    is_weekend = target_date.weekday() >= 5

    rhythm = get_rhythm_text(character_id)
    rhythm_block = f'\n{rhythm}\n' if rhythm else ''

    season = _seasonal_hints(target_date.month)

    city_note = ''
    if random.random() < 0.15:
        city = random.choice(['大阪', '名古屋', '北海道', '福冈', '横滨', '仙台', '神户'])
        city_note = f'\n★ 今天这个角色{random.choice(["出差去了", "临时跑去了", "心血来潮去了"])}{city},日程安排在那边。\n'

    # ★★★ 关键修复:用 system 参数框定任务性质 ★★★
    system_prompt = f'''你是一个创意写作助手。你的任务是为一个虚拟陪伴 App 生成虚构角色的每日行程表。

这是 App 的一个功能模块:用户可以查看角色"今天在干什么"。
你需要根据角色设定,生成一份符合角色性格和背景的日程。

这是纯粹的创意写作/内容生成任务,不是角色扮演。
请直接输出 JSON 格式的日程数据,不需要任何解释或前言。'''

    # user message 里只给角色资料和格式要求,不说"你是 XXX"
    prompt = f'''请为以下虚构角色生成 {target_date}（{weekday_cn}）的日程。

【角色资料】
角色名:{char_name}
{core_prompt}
{rhythm_block}
【当季关键词】{season}
{city_note}
{'今天是周末,安排可以更随性。' if is_weekend else '今天是工作日,该有的正事要有。'}

【日程要求】
1. 从起床到睡觉,排 8-12 个时间段,覆盖一整天。
2. 每一段要符合这个角色的身份和性格。

3. title【极短】(5-15字),手机一行看完,细节放 note:
   ✅ "去Ivy Place吃brunch" ✅ "原宿买限量可颂"
   ✅ "溜去PARCO看快闪" ✅ "备课" ✅ "泡澡刷手机"
   ❌ 超过15字 = 失败。描述性的长句写进 note 不要写进 title。

4. location 要具体:
   ✅ "原宿甜品店" "Blue Bottle 表参道" "涩谷 PARCO 5F"
   ❌ "某店" "外面"

5. note 是角色口吻的碎碎念:
   ✅ "排队也是品尝美食的重要一环哦" "又买多了,衣柜要炸了"
   ❌ "心情不错" ← 太空

6. 提到的店铺、地标要是东京/京都真实存在的或合理的。
   不全是好评!有时踩雷就吐槽。

7. can_reply 标注(这段时间角色能不能回手机消息):
   false = 走不开:上课、出任务、战斗、洗澡、正式会议
   true = 能摸鱼:吃饭、逛街、探店、发呆、休息
   can_reply=false 的时段一天最多 4 段、总共不超过 4 小时。

8. 每天要不一样,不要套模板。

【时间格式】"HH:MM" 24小时制,前后要连上。

【输出格式:严格 JSON 一行,不要任何解释】
{{"schedule":[
  {{"start_time":"07:00","end_time":"07:45","title":"生活化叙事","location":"具体地点","note":"角色碎碎念","can_reply":true}},
  ...
]}}'''

    try:
        from ai_client import create_chat
        raw, _usage = create_chat(
            model=MODEL_CN_AUX,
            max_tokens=3000,
            system=system_prompt,       # ★ 用 system 参数
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


# ═══════════════ _sanitize 和辅助函数(不改) ═══════════════

SLEEP_KEYWORDS = ('睡', '就寝', '寝る', '休息', '入睡')


def _dur(item):
    try:
        sh, sm = map(int, item['start_time'].split(':'))
        eh, em = map(int, item['end_time'].split(':'))
        start = sh * 60 + sm
        end = eh * 60 + em
        if end <= start:
            end += 24 * 60
        return end - start
    except Exception:
        return 60


def _sanitize(raw_items, character_id=None):
    import re

    sleep_start, sleep_end = None, None
    try:
        sw = get_sleep_window(character_id) if character_id else None
        if sw:
            sleep_start, sleep_end = sw
    except Exception:
        pass

    def _in_sleep(time_str):
        if not sleep_start or not sleep_end:
            return False
        try:
            h, m = map(int, time_str.split(':'))
            t = h * 60 + m
            s = int(sleep_start.split(':')[0]) * 60 + int(sleep_start.split(':')[1])
            e = int(sleep_end.split(':')[0]) * 60 + int(sleep_end.split(':')[1])
            if s > e:
                return t >= s or t < e
            return s <= t < e
        except Exception:
            return False

    want_start = sleep_start

    ok = []
    for it in raw_items:
        st = (it.get('start_time') or '').strip()
        et = (it.get('end_time') or '').strip()
        title = (it.get('title') or '').strip()
        if not st or not et or not title:
            continue
        if not re.match(r'^\d{2}:\d{2}$', st) or not re.match(r'^\d{2}:\d{2}$', et):
            continue
        it['start_time'] = st
        it['end_time'] = et
        it['title'] = title
        it['location'] = (it.get('location') or '').strip()
        it['note'] = (it.get('note') or '').strip()
        it['can_reply'] = bool(it.get('can_reply', True))

        is_sleep = any(k in title for k in SLEEP_KEYWORDS)
        if is_sleep:
            it['can_reply'] = SLEEP_CAN_REPLY

        if sleep_start and not is_sleep:
            if _in_sleep(it['start_time']) and _in_sleep(it['end_time']):
                continue
            if not _in_sleep(it['start_time']) and _in_sleep(it['end_time']):
                if it['end_time'] != want_start:
                    it['end_time'] = want_start

        ok.append(it)

    ok.sort(key=lambda x: x['start_time'])

    busy = [it for it in ok
            if not it['can_reply']
            and not any(k in it['title'] for k in SLEEP_KEYWORDS)]

    for it in list(busy):
        if _busy_priority(it['title']) < MIN_BUSY_PRIORITY:
            it['can_reply'] = True
            busy.remove(it)

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
            it['can_reply'] = True

    return ok


def ensure_today(character_id, user_id):
    today = _now().date()
    if db_schedule.has_schedule(character_id, user_id, today):
        return False
    generate_daily_schedule(character_id, user_id, today)
    return True