"""schedule_engine.py —— 让角色自己排一天的行程

★ v4:接入真实店铺
  · 生成前先用 places_engine 搜 5 个真实店铺(Overpass API,免费)
  · 把店铺列表喂给 LLM,让它挑几家安排进日程
  · 生成完成后,把日程里用到的真实店铺自动写入 char_visited_places(地图打点)
  · 用 system prompt 框定为创意写作任务,避免 Claude 拒绝角色扮演
"""
from datetime import datetime
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


def _fetch_real_places(city='tokyo'):
    """调 places_engine 搜真实店铺。失败返回空列表(不阻断日程生成)。"""
    try:
        import places_engine
        places = places_engine.get_schedule_places(city, count=5)
        return places
    except Exception as e:
        print(f'[schedule] 搜真实店铺失败(不影响日程生成): {e}')
        return []


def _save_visited_places(character_id, user_id, items, real_places, target_date):
    """日程生成后,把用到的真实店铺写入探店记录(地图打点用)。"""
    if not real_places:
        return
    try:
        import db_visited_places
        # 把真实店名做个 set,检查日程 title/location 里有没有用到
        place_map = {p['name']: p for p in real_places}
        for item in items:
            text = (item.get('title', '') + ' ' + item.get('location', '')).strip()
            for name, place in place_map.items():
                if name in text:
                    review = item.get('note', '')
                    db_visited_places.add_visited(
                        character_id, user_id, place,
                        review=review, visit_date=target_date
                    )
                    print(f'[schedule] 📍 {character_id} 探店: {name}')
                    break  # 一个时段最多匹配一家店
    except Exception as e:
        print(f'[schedule] 保存探店记录失败(不影响日程): {e}')


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

    # ★ 偶尔去别的城市(15%)
    main_city = 'tokyo'
    city_note = ''
    if random.random() < 0.15:
        other = random.choice(['osaka', 'kyoto', 'yokohama', 'fukuoka', 'nagoya', 'sapporo', 'kobe'])
        city_names = {'osaka': '大阪', 'kyoto': '京都', 'yokohama': '横滨', 'fukuoka': '福冈',
                      'nagoya': '名古屋', 'sapporo': '北海道', 'kobe': '神户'}
        main_city = other
        city_note = f'\n★ 今天角色{random.choice(["出差去了", "临时跑去了", "心血来潮去了"])}{city_names.get(other, other)},日程安排在那边。\n'

    # ★ 搜真实店铺
    real_places = _fetch_real_places(main_city)
    places_block = ''
    if real_places:
        lines = []
        for p in real_places:
            lines.append(f'  · {p["name"]}({p["category_label"]}) — {p["address"] or p["city"]}')
        places_block = f'''
【今天可以安排去的真实店铺(从中挑 1-3 家放进日程,不必全用)】
{chr(10).join(lines)}
用到的店请在 title 或 location 里写上【完整店名】(必须和上面一字不差),这样系统才能在地图上标记。
没用到的就不写。也可以不用任何一家(去其他虚构地点)。
'''

    # ★ system prompt 框定为创意写作任务
    system_prompt = '''你是一个创意写作助手。你的任务是为一个虚拟陪伴 App 生成虚构角色的每日行程表。
这是 App 的一个功能模块:用户可以查看角色"今天在干什么"。
你需要根据角色设定,生成一份符合角色性格和背景的日程。
这是纯粹的创意写作/内容生成任务,不是角色扮演。
请直接输出 JSON 格式的日程数据,不需要任何解释或前言。'''

    prompt = f'''请为以下虚构角色生成 {target_date}（{weekday_cn}）的日程。

【角色资料】
角色名:{char_name}
{core_prompt}
{rhythm_block}
【当季关键词】{season}
{city_note}
{'今天是周末,安排可以更随性。' if is_weekend else '今天是工作日。'}
{places_block}

【日程写法要求】
1. 从起床到睡觉,排 8-12 个时间段。
2. 符合角色身份和性格。

3. title【极短】(5-15字),手机一行看完,细节放 note:
   ✅ "去Ivy Place吃brunch" ✅ "原宿买限量可颂"
   ✅ "溜去PARCO看快闪" ✅ "备课" ✅ "泡澡刷手机"
   ❌ 超过15字 = 失败。描述性的长句写进 note 不要写进 title。

4. location 要具体:
   ✅ "Blue Bottle 表参道" "涩谷PARCO" ❌ "某店" "外面"

5. note 是角色口吻的碎碎念,有趣/有画面:
   ✅ "排了40分钟结果踩雷了,下次不来" "拍照确实出片"
   ❌ "心情不错" ← 太空

6. 不全是好评!有时踩雷就吐槽。

7. can_reply 标注(能不能回手机消息):
   false = 走不开:上课、出任务、战斗、洗澡
   true = 能摸鱼:吃饭、逛街、探店、休息
   can_reply=false 一天最多 4 段,总共不超 4 小时。

8. 每天要不一样。

【输出:严格 JSON 一行,不要解释】
{{"schedule":[
  {{"start_time":"07:00","end_time":"07:45","title":"5-15字","location":"具体地点","note":"碎碎念","can_reply":true}},
  ...
]}}'''

    try:
        from ai_client import create_chat
        raw, _usage = create_chat(
            model=MODEL_CN_AUX,
            max_tokens=3000,
            system=system_prompt,
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

        # ★ 自动保存探店记录(地图打点)
        _save_visited_places(character_id, user_id, items, real_places, target_date)

        busy = [i for i in items if not i['can_reply']]
        print(f'[schedule] ✅ {char_name} {target_date} 共 {len(items)} 段,'
              f'其中走不开 {len(busy)} 段')
        return items

    except Exception as e:
        print(f'[schedule] {character_id} 生成出错: {e}')
        return None


# ═══════════════ _sanitize(不改) ═══════════════

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