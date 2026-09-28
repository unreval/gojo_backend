"""Daily schedule generation backed by one canonical schedule world.

Generation proposes planned events only. It cannot create a visited map point,
stable preference, or a state transition; those each have a separate durable
authority.
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


MAX_HARD_BUSY_SLOTS = 4
MAX_HARD_BUSY_MINUTES = 240
MIN_BUSY_PRIORITY = 4
SLEEP_REPLY_STATE = db_schedule.REPLY_FREE

_BUSY_PRIORITY = [
    (10, ('任务', '讨伐', '战斗', '出勤', '祓除', '交战', '出击')),
    (9,  ('上课', '授课', '教学', '讲课', '辅导')),
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
    """Low-salience seasonal variety, deliberately without fixed landmarks."""
    hints = {
        1: '年初整理、冬日室内活动、短途放松',
        2: '节日社交、早春散步、室内兴趣',
        3: '换季安排、春日户外、学期事务',
        4: '春季户外、朋友见面、工作节奏调整',
        5: '假期出行、自然散步、短期项目',
        6: '雨天室内活动、文书整理、轻松休息',
        7: '夏夜活动、避暑、短途任务',
        8: '暑期事务、夜间休闲、社交活动',
        9: '入秋整理、学习工作恢复、安静休闲',
        10: '秋季户外、文化活动、换季安排',
        11: '年末前事务、室内活动、自然散步',
        12: '年末整理、节日社交、室内放松',
    }
    return hints.get(month, '')


def _fetch_real_places(city='tokyo'):
    """★ v5:搜食物 + 景点 + 活动场地,不再只有吃的。"""
    try:
        import places_engine
        places = places_engine.get_schedule_places(city, count=7)
        return places
    except Exception as e:
        print(f'[schedule] 搜真实地点失败(不影响日程生成): {e}')
        return []


def _place_candidates_block(real_places):
    """Give the model resolver identities, not permission to invent map facts."""
    if not real_places:
        return ''
    lines = []
    for place in real_places:
        provider = place.get('provider')
        place_id = place.get('provider_place_id')
        if not provider or not place_id:
            continue
        lines.append(
            f'  · poi_ref="{provider}:{place_id}" '
            f'类别={place.get("verified_category") or place.get("category")} '
            f'名称={place.get("canonical_name") or place.get("name")} '
            f'区域={place.get("canonical_address") or place.get("address")}'
        )
    if not lines:
        return ''
    return (
        '\n【已验证 POI 候选（可选）】\n' + '\n'.join(lines) +
        '\n若选择其中一个，只在 planned_place_ref 写对应 poi_ref。'
        '不能把自己编的店名、地址或坐标当 POI；无法确认时 location 只写一般区域。\n'
    )


def _role_responsibility_block(character_id, user_id, target_date):
    try:
        from schedule_novelty import responsibility_weights
        history = db_schedule.get_recent_schedule_history(
            character_id, user_id, before_date=target_date, days=14)
        weights = responsibility_weights(history)
    except Exception:
        weights = {'teacher': 1.0, 'sorcerer': 1.0, 'clan_head': 1.0}
    return f'''【职责平衡（最近 14 天动态权重）】
- teacher={weights['teacher']}: 授课、学生指导、备课、教务。
- sorcerer={weights['sorcerer']}: 任务安排、巡逻、现场处置、汇报。
- clan_head={weights['clan_head']}: 家族文件、家族会议、人员/资源安排、对外交涉、必须出席的正式场合。
家主职责可以中频出现，但绝不做成每天固定模板；“家族事务”必须具体化，且按具体 phase 判断是否能回手机。'''


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

    # Only resolver-verified candidates can become exact planned POIs.
    real_places = _fetch_real_places(main_city)
    places_block = _place_candidates_block(real_places)
    responsibility_block = _role_responsibility_block(
        character_id, user_id, target_date)

    # ★ system prompt
    system_prompt = '''你是一个创意写作助手。你的任务是为一个虚拟陪伴 App 生成虚构角色的每日行程表。
这是 App 的一个功能模块:用户可以查看角色"今天在干什么"。
你需要根据角色设定,生成一份符合角色性格和背景的日程。
这是纯粹的创意写作/内容生成任务,不是角色扮演。
★ 所有内容必须用中文写(title、location、note 全部中文),不要用日文。
请直接输出 JSON 格式的日程数据,不需要任何解释或前言。'''

    prompt = f'''请为以下虚构角色生成 {target_date}（{weekday_cn}）的日程。

【角色资料】
角色名:{char_name}
{core_prompt}
{rhythm_block}
【低显著性季节方向】{season}
{city_note}
{'今天是周末,安排可以更随性。' if is_weekend else '今天是工作日。'}
{places_block}
{responsibility_block}

【日程写法要求】
1. 从起床到睡觉,排 8-12 个时间段。
2. 符合角色身份和性格。
3. ★★★ 所有内容(title / location / note)必须用【中文】写,不要用日文 ★★★

4. title【极短】(5-15字),手机一行看完,细节放 note:
   ✅ "备课" ✅ "家族资源核对" ✅ "夜间巡查" ✅ "泡澡刷手机"
   ❌ 超过15字 = 失败。描述性的长句写进 note 不要写进 title。

5. location 规则:
   - 选择候选 POI 时，写 planned_place_ref，不要把店名/地址/坐标当作自由文本事实。
   - 没有可靠 POI 时，location 只能是一般区域（如“学校办公室”“东京城区”“附近街区”），不能伪造具体店铺。

6. note 是角色口吻的碎碎念,有趣/有画面:
   ✅ "排了40分钟结果踩雷了,下次不来" "拍照确实出片" "人太多了差点被挤死"
   ❌ "心情不错" ← 太空

7. ★★ 不只是吃!角色是活人不是吃货!一天日程里应该有:
   · 至少 1 个非餐饮活动(散步/看展/运动/拜访/安静休息)
   · 可以有 2-3 个餐饮(不是每段都在吃)
   · 剩下的是工作/任务/训练/休息 等日常
   ★ 比例参考:吃 ≤ 3 段,景点/活动 1-2 段,工作/日常 4-6 段

8. 不全是好评!有时踩雷就吐槽。

9. reply_state 标注(手机消息状态):
   "free" = 能正常看手机并回复:吃饭、休息、逛街、探店、发呆
   "soft_busy" = 手上有事但可能瞄一眼手机:备课、开会、处理报告、通勤、排队、买东西、散步
   "hard_busy" = 真的不能看手机:上课、出任务、战斗、洗澡、驾驶
   开会/备课/写报告不是 hard_busy。hard_busy 一天最多 4 段,总共不超 4 小时。soft_busy 不算 hard_busy。

10. effective_busy_minutes（可选，仅 soft_busy）:
   日程块可以很长，但真正没法正常回复的时间可能很短。
   只有当你判断「真正集中处理、不太能回消息」的阶段明显短于整段日程时，才填写正整数分钟。
   例如 14:00-16:00 处理报告、预计15分钟写完 → "effective_busy_minutes": 15
   不知道、或整段都会分心忙 → null / 不填。
   禁止给所有 soft_busy 都填一个很短的数字。
   禁止用这个字段缩短 hard_busy。free 不需要这个字段。
   填写后不要改 start_time / end_time，视觉日程仍是原来的整段。

11. 每天要不一样。不要复用近两周的相同 POI、相同具体食物、相同风味主题或 note 句式。
    工作职责允许正常重复；严格去重只针对 flavor / 部分 leisure。

12. category 只能为 obligation / routine / social / leisure / flavor。
    fixedness 只能为 fixed / flexible / optional / flavor；固定职责不要随意取消。

13. 可选 phases 数组：仅当父活动里确实有连续且不同的步骤时填写。
    每个 phase 有 start_time/end_time/title/reply_state；当前 phase 而非父日程决定手机状态。

【输出:严格 JSON 一行,不要解释】
{{"schedule":[
  {{"start_time":"07:00","end_time":"07:45","title":"晨间整理","location":"住处","note":"碎碎念","category":"routine","fixedness":"flexible","reply_state":"free","effective_busy_minutes":null}},
  {{"start_time":"14:00","end_time":"16:00","title":"处理家族报告","location":"家族办公室","note":"先集中处理十五分钟","category":"obligation","fixedness":"flexible","reply_state":"soft_busy","effective_busy_minutes":15}},
  {{"start_time":"18:00","end_time":"19:00","title":"短暂休息","location":"东京城区","planned_place_ref":"nominatim:候选ID（仅当从候选选择时）","note":"不必每次都填","category":"leisure","fixedness":"optional","reply_state":"free"}},
  ...
]}}'''

    try:
        import anthropic
        from config import ANTHROPIC_KEY, MODEL_MAIN
        from ai_client import extract_text
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

        full_prompt = system_prompt + '\n\n' + prompt

        resp = client.messages.create(
            model=MODEL_MAIN,
            max_tokens=3000,
            messages=[{'role': 'user', 'content': full_prompt}],
        )
        raw = extract_text(resp).strip()
        if not raw:
            print(f'[schedule] {character_id} 生成返回空')
            return None

        from utils import extract_json
        parsed = extract_json(raw)
        if not parsed or not isinstance(parsed.get('schedule'), list):
            print(f'[schedule] {character_id} 解析失败: {raw[:200]}')
            return None

        try:
            history = db_schedule.get_recent_schedule_history(
                character_id, user_id, before_date=target_date, days=14)
        except Exception:
            history = []
        items = _sanitize(
            parsed['schedule'], character_id,
            poi_candidates=real_places, recent_history=history,
            target_date=target_date)
        if not items:
            print(f'[schedule] {character_id} 清洗后没有有效条目')
            return None

        persisted = db_schedule.save_canonical_schedule(
            character_id, user_id, target_date, items, force=force,
            provenance={
                'source': 'daily_schedule_generation',
                'stable_preference': False,
                'visited_on_generation': False,
            })

        busy = [i for i in items if i['reply_state'] != db_schedule.REPLY_FREE]
        hard_busy = [i for i in items if i['reply_state'] == db_schedule.REPLY_HARD_BUSY]
        food_cnt = sum(1 for i in items if any(k in i.get('title','') for k in ('吃','喝','咖啡','面','甜','brunch','午餐','晚餐','早餐')))
        act_cnt = sum(1 for i in items if any(k in i.get('title','') for k in ('逛','看','散步','打卡','参拜','花火','展','公园','温泉','泡')))
        print(f'[schedule] ✅ {char_name} {target_date} 共 {len(items)} 段,'
              f'忙碌 {len(busy)} 段(硬忙 {len(hard_busy)}), 吃≈{food_cnt} 活动≈{act_cnt}')
        return persisted

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


def _sanitize(raw_items, character_id=None, poi_candidates=None,
              recent_history=None, target_date=None):
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

    def _general_area(raw):
        value = (raw or '').strip()
        generic_words = ('学校', '办公室', '住处', '家', '城区', '街区', '附近', '校内', '校园')
        if value and any(word in value for word in generic_words):
            return value[:40]
        return '一般区域'

    def _resolved_place(item):
        reference = item.get('planned_place_ref') or item.get('planned_place')
        if not reference or not poi_candidates:
            return None
        try:
            import places_engine
            return places_engine.resolve_schedule_poi(reference, poi_candidates)
        except Exception:
            return None

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
        place = _resolved_place(it)
        if place:
            # A concrete display location can only come from this resolver.
            it['planned_place'] = place
            it['location'] = place.get('canonical_name') or place.get('name', '')
        else:
            it.pop('planned_place', None)
            it['location'] = _general_area(it.get('location'))
        it.pop('planned_place_ref', None)
        it['note'] = (it.get('note') or '').strip()
        # Category controls which novelty rules apply, so derive it from the
        # content instead of trusting a model label that could bypass checks.
        it['category'] = db_schedule._category_for_item(it)
        fixedness = str(it.get('fixedness') or '').strip().lower()
        it['fixedness'] = fixedness if fixedness in (
            'fixed', 'flexible', 'optional', 'flavor') else 'flexible'
        reply_state = db_schedule.normalize_reply_state(
            it.get('reply_state'), it.get('can_reply', True))

        is_sleep = any(k in title for k in SLEEP_KEYWORDS)
        if is_sleep:
            reply_state = SLEEP_REPLY_STATE
        else:
            try:
                from activity_phone import classify_activity_kind, PROFILES
                kind = classify_activity_kind(title)
                if kind and kind in PROFILES:
                    reply_state = PROFILES[kind].busy_state
            except Exception:
                pass

        it['reply_state'] = reply_state
        it['can_reply'] = db_schedule.can_reply_from_state(reply_state)

        if sleep_start and not is_sleep:
            if _in_sleep(it['start_time']) and _in_sleep(it['end_time']):
                continue
            if not _in_sleep(it['start_time']) and _in_sleep(it['end_time']):
                if it['end_time'] != want_start:
                    it['end_time'] = want_start

        ok.append(it)

    ok.sort(key=lambda x: x['start_time'])

    # A generated day is one canonical timeline. Keep the earlier proposed
    # item when a later one overlaps it instead of creating two active events.
    from schedule_contract import timeline_is_valid
    timeline = []
    sched_date = target_date or _now().date()
    for item in ok:
        if timeline_is_valid(timeline + [item], sched_date, CN_TZ):
            timeline.append(item)
        else:
            print(f'[schedule] dropped overlapping item: {item["title"]}')
    ok = timeline

    hard_busy = [it for it in ok
            if it.get('reply_state') == db_schedule.REPLY_HARD_BUSY
            and not any(k in it['title'] for k in SLEEP_KEYWORDS)]

    for it in list(hard_busy):
        if _busy_priority(it['title']) < MIN_BUSY_PRIORITY:
            it['reply_state'] = db_schedule.REPLY_SOFT_BUSY
            it['can_reply'] = False
            hard_busy.remove(it)

    hard_busy.sort(key=lambda it: (-_busy_priority(it['title']), -_dur(it)))

    kept_count = 0
    kept_minutes = 0
    keep_ids = set()
    for it in hard_busy:
        d = _dur(it)
        if kept_count >= MAX_HARD_BUSY_SLOTS or kept_minutes + d > MAX_HARD_BUSY_MINUTES:
            continue
        keep_ids.add(id(it))
        kept_count += 1
        kept_minutes += d

    for it in hard_busy:
        if id(it) not in keep_ids:
            it['reply_state'] = db_schedule.REPLY_SOFT_BUSY
            it['can_reply'] = False

    for it in ok:
        if it.get('reply_state') == db_schedule.REPLY_SOFT_BUSY:
            it['effective_busy_minutes'] = db_schedule.parse_effective_busy_minutes(
                it.get('effective_busy_minutes'))
        else:
            it['effective_busy_minutes'] = None

    # Strict novelty is intentionally narrow: repeated work obligations are
    # normal, while repeated discretionary POIs/flavors and copy are not.
    try:
        from schedule_novelty import validate_novelty
        seen_history = list(recent_history or [])
        for it in ok:
            decision = validate_novelty(
                it, seen_history, candidate_date=target_date)
            if decision.rejected:
                it.update({
                    'title': '机动安排',
                    'location': '一般区域',
                    'note': '',
                    'planned_place': None,
                    'category': 'leisure',
                    'fixedness': 'optional',
                })
                print(f'[schedule] novelty replaced: {decision.reason}')
            seen_history.append(dict(it))
    except Exception as exc:
        print(f'[schedule] novelty validation skipped: {exc}')
    return ok


def ensure_today(character_id, user_id):
    today = _now().date()
    if db_schedule.has_schedule(character_id, user_id, today):
        return False
    generate_daily_schedule(character_id, user_id, today)
    return True
