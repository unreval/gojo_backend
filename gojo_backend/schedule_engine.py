"""schedule_engine.py —— 让角色自己排一天的行程

★ v2:日程风格大改
  旧版:title="去咖啡厅" location="涩谷" note="点了杯拿铁" ← 像打卡清单
  新版:title="跑去表参道那家新开的 % Arabica 排了20分钟队,
              就为了他家的西班牙拿铁限定版" ← 像真人的一天

  · 地点用东京/京都的真实店铺和地标(LLM 知道这些)
  · 偶尔去别的城市(大阪/名古屋/北海道)出差或旅行
  · 限定品/新品/当季特色 让日程更有时令感
  · 不全是好评,有时踩雷就吐槽
  · title 本身就是叙事,不是"做什么"三个字
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


# ── 忙碌配额配置(不变) ──
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


# ── 季节/月份 → 时令关键词(让日程有时令感) ──
def _seasonal_hints(month: int) -> str:
    hints = {
        1:  '正月初詣、福袋、冬季限定草莓甜品、热巧克力、温泉',
        2:  '情人节巧克力、草莓季、梅花、冬季清仓',
        3:  '樱花季开始、春季限定抹茶、毕业季',
        4:  '满开樱花、花见、春季新品、新学期',
        5:  '黄金周、新绿、抹茶新茶、初夏限定',
        6:  '梅雨季、紫阳花、夏季限定刨冰、水果挞',
        7:  '夏祭、花火大会、刨冰、西瓜、冷面',
        8:  '盂兰盆节、花火、夏季限定冰品、海边、啤酒花园',
        9:  '秋季栗子甜品、月见、秋刀鱼、葡萄',
        10: '万圣节限定、红叶开始、秋季新品、栗子蒙布朗',
        11: '红叶季、秋季限定、感恩节、热饮回归',
        12: '圣诞限定、年末、冬季草莓、热红酒、illumination',
    }
    return hints.get(month, '')


# ── 偶尔去别的城市 ──
def _pick_city_context() -> str:
    """大部分时间在东京/京都,偶尔(15%)去别的城市"""
    if random.random() < 0.15:
        city = random.choice(['大阪', '名古屋', '北海道', '福冈', '横滨', '�的�的', '神户'])
        if city == '镇仙':
            city = '�的仙台'
        return f'\n★ 今天你{random.choice(["出差去了", "临时跑去了", "心血来潮去了", "被叫去了"])}{city},日程安排在那边。\n'
    return ''


def generate_daily_schedule(character_id, user_id, target_date=None, force=False):
    """给某个角色生成某天的日程。返回条目列表或 None。"""
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
    city_context = _pick_city_context()

    prompt = f'''你是{char_name}。请安排你自己 {target_date}（{weekday_cn}）这一天的行程。

【你是谁】
{core_prompt}
{rhythm_block}
【当季关键词】{season}
{city_context}
{'今天是周末,可以更随性。' if is_weekend else '今天是工作日,该有的正事要有,但也别排太满。'}

═══════════════════════════════════════
★ 【最重要的改变:写法】
═══════════════════════════════════════

title 不是"做什么"这种干巴巴的三个字 —— 它是【你今天这段时间的生活叙事】。
要写得像你在跟朋友描述你今天干了什么,一句话就能让人看到画面:

❌ 错误示范(太死):
  title:"去咖啡厅" location:"涩谷" note:"点了拿铁"
  title:"午餐" location:"食堂" note:"吃了拉面"
  title:"逛街" location:"表参道" note:"买了衣服"

✅ 正确示范(有画面、有情绪、有细节):
  title:"跑去原宿那家排队排到马路上的限量版爆浆流心可颂,等了快半小时"
  title:"在衣帽间挑今天穿的高定私服,最后选了那件25万的白衬衫"
  title:"瞬移到京都高专把交流会奖品全换成发光惨叫鸡"
  title:"经过表参道的 Blue Bottle 顺手买了杯手冲,坐在路边看人"
  title:"在校长室喝茶并故意用宝宝用语恶心乐�的岩寺"
  title:"去涩谷 PARCO 看新开的潮牌快闪店,看了一圈什么都没买"
  title:"溜去新宿那家深夜拉面,点了特浓豚骨加叉烧加量"

关键:
· 提到的店、地标、食物要是【东京/京都真实存在的】或者至少是合理的
  (Blue Bottle / % Arabica / Cremia / HARBS / 一兰拉面 / bills / PARCO / 109 / 
   表参道 / 原宿竹下通 / 涩谷 / 新宿 / 代官山 / 中目黑 / 清水寺 / 锦市场 等等)
· 当季有什么限定品/新品,可以让你"去排队/去尝鲜/去打卡"
· 【不是每次都好评!】有时排半天队发现踩雷了,就在 note 里吐槽
  ("排了40分钟,拿到手发现没有网图那么夸张,下次不来了")
· title 一段话20-60字,别太短(≥15字),别太长(≤80字)

═══════════════════════════════════════

note 是你的【内心碎碎念】,用你自己的语气:
  ✅ "哎这笔账目看着很有趣呢~" "排队也是品尝美食的重要一环哦"
     "大家看到一定会很惊喜的吧!" "其实没那么好喝,但拍照确实出片"
     "明明是工作日人怎么这么多" "又买多了,衣柜要炸了"
  ❌ "今天天气不错" "心情很好" ← 太空洞

location 是【具体地点】:
  ✅ "原宿甜品店" "Blue Bottle 表参道" "京都高专校长室" "涩谷 PARCO 5F"
  ❌ "某店" "外面" "城市" ← 太模糊

【其他要求】
1. 从起床到睡觉,排 8-12 个时间段,覆盖一整天。
2. 每一段要符合你的身份和性格 —— 是"{char_name}这个人今天会怎么过"。
3. 【每天要不一样】:今天和昨天不是同一天,别复制粘贴。
   今天可能探店、可能出任务、可能翘班、可能被叫去开会、可能纯摸鱼。
4. can_reply 判断:
   · false = 真的走不开:上课、出任务、战斗、洗澡、正式会议
   · true = 能摸鱼回消息:吃饭、逛街、探店、发呆、休息、通勤
5. can_reply=false 的时段一天最多 4 段、总共不超过 4 小时。
6. 睡觉也排上(can_reply 看你设定)。

【时间格式】"HH:MM" 24小时制,前后要连上,不要留空档。

【严格 JSON 一行,不要任何解释】
{{"schedule":[
  {{"start_time":"07:00","end_time":"07:45","title":"一段生活叙事(15-80字)","location":"具体地点","note":"你的碎碎念","can_reply":true}},
  ...
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


# ════════════════════════════════════════════════
#  以下是 _sanitize 和其他辅助函数(不改,照搬原版)
# ════════════════════════════════════════════════

SLEEP_KEYWORDS = ('睡', '就寝', '寝る', '休息', '入睡')


def _dur(item):
    """计算一个时段的分钟数。"""
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
    """清洗 LLM 输出:校验时间格式、强制忙碌上限。"""
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

        # 睡觉关键词
        is_sleep = any(k in title for k in SLEEP_KEYWORDS)
        if is_sleep:
            it['can_reply'] = SLEEP_CAN_REPLY

        # 保护睡眠时段
        if sleep_start and not is_sleep:
            if _in_sleep(it['start_time']) and _in_sleep(it['end_time']):
                print(f'[schedule] 「{title}」完全在睡眠时间内,跳过')
                continue
            if not _in_sleep(it['start_time']) and _in_sleep(it['end_time']):
                if it['end_time'] != want_start:
                    print(f'[schedule] 「{title}」压到睡眠时间,'
                          f'结束时间 {it["end_time"]} → {want_start}')
                    it['end_time'] = want_start

        ok.append(it)

    ok.sort(key=lambda x: x['start_time'])

    # ── 忙碌配额:按优先级排序 ──
    busy = [it for it in ok
            if not it['can_reply']
            and not any(k in it['title'] for k in SLEEP_KEYWORDS)]

    for it in list(busy):
        if _busy_priority(it['title']) < MIN_BUSY_PRIORITY:
            print(f'[schedule] 「{it["title"]}」不足以让人失联,改成可回复')
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
    """确保今天有日程,没有就生成。"""
    today = _now().date()
    if db_schedule.has_schedule(character_id, user_id, today):
        return False
    generate_daily_schedule(character_id, user_id, today)
    return True