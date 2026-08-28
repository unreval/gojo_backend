"""schedule_share.py —— 日程驱动的主动分享

场景:他正在探店 / 买到限定甜品 / 翘班溜达 —— 这种时候真人会顺手发条消息。
不是"到点汇报",是"刚好遇到值得说的事"。

★ v-fix2:主动消息上限可被对话调整
  用户跟角色说"多发消息给我",角色同意后,
  promise_detector 会调高那个角色的每日上限(存数据库)。
  这里读 promise_detector.get_msg_limit() 拿个性化上限。
"""
import threading
import time
import random
from datetime import datetime, timedelta

from config import CN_TZ, MODEL_JP_AUX
import db_schedule
import proactive_msg

TARGET_USER = 'user_mofpiyd7442ia7'

# ── 节奏控制(默认值,可被对话调整) ──
TICK_SECONDS = 90 * 60
SHARE_CHANCE = 0.15
MAX_PER_DAY = 2             # ★ 默认值,实际用 _get_limit() 读个性化上限
MIN_GAP_HOURS = 3

_MIN_TICK_GAP_SEC = 30 * 60
_last_tick_at = {}

WORTH_SHARING = (
    '甜品', '甜点', '蛋糕', '和菓子', '大福', '限定', '探店', '店',
    '逛', '闲逛', '翘班', '偷懒', '溜', '发呆', '散步',
    '吃饭', '午餐', '晚餐', '便利店', '买',
)
NEVER_SHARE = ('睡', '就寝', '寝', '通勤', '起床', '洗漱', '开会', '会议', '文件', '事务')

_thread = None
_stop = False


def _now():
    return datetime.now(CN_TZ)


def _get_limit(character_id, user_id):
    """读个性化的每日主动消息上限。
    用户跟角色说"多发消息",角色同意后,promise_detector 会调高这个值。
    读不到就用默认值 MAX_PER_DAY。
    """
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
    from ai_client import create_chat
    from utils import extract_json

    char = get_character(character_id)
    if not char:
        return False
    char_name = char['name']
    voice_id = char.get('voice_id')
    now = _now()

    try:
        shorts = get_short_memory(user_id, 4, character_id)
        recent = '\n'.join(f'{"她" if r=="user" else "我"}：{c}' for r, c in shorts) \
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
        print(f'[life_share] 关系规则注入失败：{_e}')
        relation_rules = ''

    prompt = f'''你是{char_name}。现在是 {now.strftime("%H:%M")}。
{relation_rules}

【你正在做的事】
{activity["start_time"]}~{activity["end_time"]} {activity["title"]}
{f'地点：{activity["location"]}' if activity.get("location") else ''}
{f'你的想法：{activity["note"]}' if activity.get("note") else ''}

【你们最近聊过什么】
{recent}

【你们之间累计的事】
{bond_text}

【★ 你要判断：想不想跟她说这件事】

⚠️ 先想清楚一件事：**主动分享日常，是「在乎对方」才会做的事。**
没人会给不熟的人发"我现在在吃甜品"。会这么做，是因为
"遇到有意思的事，第一反应是想跟这个人说"。

所以判断顺序是：
1. 先按上面的关系规则，诚实判断【她对你来说到了什么程度】
2. 再问自己：**以这个关系，我会主动跟她说这件事吗？**

不同阶段的真实反应：
· 还很浅 / 只是认识 → **不会说**。你自己吃个甜品干嘛要报告给她。→ skip
· 开始有点在意她了 → 偶尔会说一句。有特别值得说的才说（买到限定的、翘班溜出来了），
  平平无奇的午餐不会特地讲。
· 已经很在乎她 → 遇到有意思的事第一反应就是想跟她说，
  哪怕只是"这家店不错"这种小事也会顺手发过去。

【会 skip 的情况】
- 关系还没到会分享生活的程度（**这是最常见的情况，别硬发**）
- 这件事对现在的你们来说没什么好说的
- 刚才才聊过，现在再发显得黏人
- 你此刻心情不想说话

【真的要说时】
- 【顺手一提】的语气，1 句就够，最多 2 句。别写成日记，别汇报行程。
- 不要问"你在干嘛"来找话题 —— 你是想说这件事，不是没话找话。
- 严禁"付き合ってやった"这种傲娇陪伴腔。
- 拿不准 → skip。漏发一次没损失，发得尴尬会破坏关系的真实感。

【输出格式（严格 JSON，一行）】
要发 → {{"jp":"日语","zh":"中文","emotion":"平静/调皮/自信/开心/温柔"}}
不发 → {{"skip": true, "reason": "简要原因"}}'''

    try:
        raw, _u = create_chat(
            model=MODEL_JP_AUX, max_tokens=500,
            messages=[{'role': 'user', 'content': prompt}],
        )
        parsed = extract_json((raw or '').strip())
        if not parsed:
            print(f'[life_share] {character_id} 解析失败：{(raw or "")[:100]}')
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
            print(f'[life_share] {character_id} 3h 内已发过相似开头,跳过复读。jp={jp[:30]}')
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
                data={'type': 'proactive', 'character_id': character_id,
                      'source': 'life_share'})
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

    # ★ 用个性化上限(可被对话调整),而不是硬编码的 MAX_PER_DAY
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

    print(f'[life_share] {character_id} 正在「{act["title"]}」,问问他想不想说 (limit={limit})')
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
    print(f'[life_share] 日程主动分享已启动'
          f'（默认上限 {MAX_PER_DAY} 条,可通过对话调整）')