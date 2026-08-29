"""schedule_share.py —— 日程驱动的主动分享

★ v-fix3: 改用 MODEL_MAIN + 创意写作框架 + 直接 Anthropic 客户端
  避免 tdyun Claude 拒绝角色扮演
"""
import threading
import time
import random
from datetime import datetime, timedelta

from config import CN_TZ, ANTHROPIC_KEY, MODEL_MAIN
import anthropic
import db_schedule
import proactive_msg

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

TARGET_USER = 'user_mofpiyd7442ia7'

TICK_SECONDS = 90 * 60
SHARE_CHANCE = 0.15
MAX_PER_DAY = 2
MIN_GAP_HOURS = 3

_MIN_TICK_GAP_SEC = 30 * 60
_last_tick_at = {}

WORTH_SHARING = (
    '甜品', '甜点', '蛋糕', '和菓子', '大福', '限定', '探店', '店',
    '逛', '闲逛', '翘班', '偷懒', '溜', '发呆', '散步',
    '吃饭', '午餐', '晚餐', '便利店', '买',
)
NEVER_SHARE = ('睡', '就寝', '寝', '通勤', '起床', '洗漱', '开会', '会议', '文件', '事务')

_SYSTEM = '''你是一个创意写作助手。你的任务是为一个虚拟陪伴 App 生成角色的日常分享消息。
角色在做一件有趣的事时,可能会顺手给用户发一条消息分享。
你需要根据角色性格和关系状态,决定发不发、发什么。
这是纯粹的创意写作任务,不是角色扮演。
日语写 jp,中文写 zh。直接输出 JSON,不要解释。'''

_thread = None
_stop = False


def _now():
    return datetime.now(CN_TZ)


def _get_limit(character_id, user_id):
    try:
        from promise_detector import get_msg_limit
        return get_msg_limit(character_id, user_id, default=MAX_PER_DAY)
    except Exception:
        return MAX_PER_DAY


def _has_any_history(character_id, user_id):
    try:
        from user_memory import get_bond_memories
        bonds = get_bond_memories(user_id, character_id, kind='between', limit=3)
        return len(bonds) > 0
    except Exception:
        return False


def _worth_sharing(activity):
    if not activity or not activity.get('can_reply'):
        return False
    title = activity.get('title', '')
    if any(k in title for k in NEVER_SHARE):
        return False
    return any(k in title for k in WORTH_SHARING)


def _sent_today(character_id, user_id):
    from db import get_conn
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """SELECT COUNT(*), MAX(created_at) FROM proactive_msg
               WHERE character_id=%s AND user_id=%s
                 AND kind='life_share' AND created_at >= %s""",
            (character_id, user_id, start))
        n, last = cur.fetchone()
    finally:
        cur.close()
        conn.close()
    return (n or 0), last


def _generate_share(character_id, user_id, activity):
    from characters import get_character
    from user_memory import get_short_memory, get_bond_memories, save_short_memory
    from utils import extract_json

    char = get_character(character_id)
    if not char:
        return False
    char_name = char['name']
    voice_id = char.get('voice_id')
    now = _now()

    try:
        shorts = get_short_memory(user_id, 4, character_id)
        recent = '\n'.join(f'{"她" if r=="user" else "角色"}：{c}' for r, c in shorts) \
                 if shorts else '（最近没聊）'
    except Exception:
        recent = ''
    try:
        bonds = get_bond_memories(user_id, character_id, kind='between', limit=12)
        bond_text = '\n'.join(f'- {b[1]}' for b in bonds) if bonds else '（还没什么共同的事）'
    except Exception:
        bonds, bond_text = [], ''

    try:
        from shared_relation_prompt import build_relation_rules
        from user_memory import get_first_interaction_days, get_long_memory
        _days = get_first_interaction_days(user_id, character_id)
        _facts = get_long_memory(user_id, character_id)
        relation_rules = build_relation_rules(_days, len(bonds), len(_facts))
    except Exception as _e:
        relation_rules = ''

    prompt = f'''请为以下虚构角色判断:要不要就当前正在做的事给用户发一条消息。

【角色】{char_name}
【当前时间】{now.strftime("%H:%M")}
{relation_rules}

【角色正在做的事】
{activity["start_time"]}~{activity["end_time"]} {activity["title"]}
{f'地点：{activity["location"]}' if activity.get("location") else ''}
{f'角色的想法：{activity["note"]}' if activity.get("note") else ''}

【最近对话】
{recent}

【关系背景】
{bond_text}

【判断规则】
主动分享日常是"在乎对方"才会做的事。判断顺序:
1. 按关系深浅判断角色对用户到了什么程度
2. 以这个关系,角色会主动说这件事吗?

不同阶段:
· 还很浅 → 不说。skip。
· 开始在意了 → 有特别值得说的才说(限定/翘班)
· 很在乎了 → 有意思的事第一反应就想说

大部分情况应该 skip。宁可不发也不要发得尴尬。
真的要说时:顺手一提的语气,1-2 句。不要问"你在干嘛",不要汇报行程。

【输出(JSON 一行)】
发 → {{"jp":"日语","zh":"中文","emotion":"平静/调皮/自信/开心/温柔"}}
不发 → {{"skip": true, "reason": "原因"}}'''

    try:
        resp = claude_client.messages.create(
            model=MODEL_MAIN,
            max_tokens=500,
            system=_SYSTEM,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = resp.content[0].text.strip() if resp.content else ''
        parsed = extract_json(raw)
        if not parsed:
            print(f'[life_share] {character_id} 解析失败：{raw[:100]}')
            return False
        if parsed.get('skip'):
            print(f'[life_share] {character_id} 决定不发：{parsed.get("reason", "")}')
            return False

        jp = (parsed.get('jp') or '').strip()
        zh = (parsed.get('zh') or '').strip()
        emotion = parsed.get('emotion', '平静')
        if not jp:
            return False

        if proactive_msg.has_similar_recent(user_id, character_id, jp, within_minutes=180):
            print(f'[life_share] {character_id} 3h 内已发过相似,跳过')
            return False

        audio_b64 = ''
        try:
            from tts import tts_to_b64
            audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
        except Exception as e:
            print(f'[life_share] TTS 出错：{e}')

        mid, _ts = proactive_msg.add_proactive_msg(
            character_id, user_id, 'life_share', jp, zh, emotion, audio_b64, created_at=now)
        print(f'[life_share] ✅ {char_name}（{activity["title"]}）→ #{mid}：{jp[:40]}')

        try:
            save_short_memory(user_id, 'assistant', jp, character_id)
        except Exception:
            pass

        try:
            import push_notify
            push_notify.push_to_user(
                user_id, title=char_name, body=zh or jp,
                data={'type': 'proactive', 'character_id': character_id, 'source': 'life_share'})
        except Exception as e:
            print(f'[life_share] 推送跳过：{e}')
        return True

    except Exception as e:
        print(f'[life_share] 生成出错：{e}')
        return False


def _tick_character(character_id):
    now = _now()

    now_ts = time.time()
    last_ts = _last_tick_at.get(character_id)
    if last_ts and (now_ts - last_ts) < _MIN_TICK_GAP_SEC:
        return
    _last_tick_at[character_id] = now_ts

    act = db_schedule.get_current_activity(character_id, TARGET_USER, now)
    if not _worth_sharing(act):
        return

    if not _has_any_history(character_id, TARGET_USER):
        return

    limit = _get_limit(character_id, TARGET_USER)

    sent, last = _sent_today(character_id, TARGET_USER)
    if sent >= limit:
        return
    if last:
        try:
            last_naive = last.replace(tzinfo=None)
            if (datetime.utcnow() - last_naive) < timedelta(hours=MIN_GAP_HOURS):
                return
        except Exception:
            pass

    if random.random() > SHARE_CHANCE:
        return

    print(f'[life_share] {character_id} 正在「{act["title"]}」,问问想不想说 (limit={limit})')
    _generate_share(character_id, TARGET_USER, act)


def _loop():
    global _stop
    time.sleep(120)
    while not _stop:
        try:
            from characters import list_characters
            for c in list_characters():
                try:
                    _tick_character(c['id'])
                except Exception as e:
                    print(f'[life_share] {c["id"]} tick 出错：{e}')
        except Exception as e:
            print(f'[life_share] tick 出错：{e}')
        jitter = random.randint(-600, 600)
        time.sleep(max(600, TICK_SECONDS + jitter))


def start_schedule_share():
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print(f'[life_share] 日程主动分享已启动（默认上限 {MAX_PER_DAY} 条,可通过对话调整）')