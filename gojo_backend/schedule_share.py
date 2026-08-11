"""schedule_share.py —— 日程驱动的主动分享

场景:他正在探店 / 买到限定甜品 / 翘班溜达 —— 这种时候真人会顺手发条消息。
不是"到点汇报",是"刚好遇到值得说的事"。

★ 三道闸门,缺一不可:
  1. 【关系够深】陌生人凭什么给你汇报行程。关系浅时这个功能整个不启动。
  2. 【活动值得分享】开会/写文件/通勤没什么好说的;探店/限定甜品/翘班才有。
  3. 【频率克制】一天最多 2 条、间隔至少 3 小时、每次还要掷骰子。
     真人不会每小时都给你发生活播报。

★ 最终决定权在 LLM:它拿到当前活动和关系状态后可以选择 skip。
  跟 proactive_scheduler 一样,宁可不发也不要发得尴尬。

独立线程,不依赖 diary_scheduler,只需要在 gojo_server.py 加一行 start_schedule_share()。
"""
import threading
import time
import random
from datetime import datetime, timedelta

from config import CN_TZ, MODEL_JP_AUX
import db_schedule
import proactive_msg

TARGET_USER = 'user_mofpiyd7442ia7'

# ── 节奏控制 ──
# ★ 这些只是【防刷屏的技术上限】,不是目标值。
#   真正决定发不发的是角色自己 —— 关系浅他会一直 skip,
#   关系深了才越来越愿意说。频率曲线由他的判断自然产生,不靠数值卡。
TICK_SECONDS = 45 * 60      # 每 45 分钟看一眼
SHARE_CHANCE = 0.5          # 掷骰子(不是每次遇到有趣的事都非说不可)
MAX_PER_DAY = 4             # 硬上限,防止极端情况刷屏
MIN_GAP_HOURS = 2           # 两条之间至少隔多久

# ── 什么活动值得分享 ──
# 有这些关键词才可能发 —— 开会、写文件、通勤没什么好说的。
WORTH_SHARING = (
    '甜品', '甜点', '蛋糕', '和菓子', '大福', '限定', '探店', '店',
    '逛', '闲逛', '翘班', '偷懒', '溜', '发呆', '散步',
    '吃饭', '午餐', '晚餐', '便利店', '买',
)
# 明确不发的
NEVER_SHARE = ('睡', '就寝', '寝', '通勤', '起床', '洗漱', '开会', '会议', '文件', '事务')

_thread = None
_stop = False


def _now():
    return datetime.now(CN_TZ)


def _has_any_history(character_id, user_id):
    """最低理智检查:至少得聊过。

    ★ 这里【故意】不设"认识几天""几条记忆"的门槛 ——
      那是机械判断,和"他想不想跟你分享"是两回事。
      真正的判断交给他自己(见 _generate_share 的 prompt),
      关系浅他会一直 skip,关系深了自然越说越多。
    """
    try:
        from user_memory import get_bond_memories
        bonds = get_bond_memories(user_id, character_id, kind='between', limit=3)
        return len(bonds) > 0
    except Exception:
        return False


def _worth_sharing(activity):
    """这个活动值不值得主动说一句。"""
    if not activity or not activity.get('can_reply'):
        return False        # 走不开的时候本来就不该发消息
    title = activity.get('title', '')
    if any(k in title for k in NEVER_SHARE):
        return False
    return any(k in title for k in WORTH_SHARING)


def _sent_today(character_id, user_id):
    """今天已经主动分享过几条 + 最后一条是什么时候。"""
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
    """让角色就当前这件事说一句。返回 True 表示真的发了。"""
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

    # ★ 用和聊天时【同一套】关系判断规则 —— 不然这里判出来的亲疏
    #   会和他在聊天里表现的不一致(之前日记就踩过这个坑)
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

    act = db_schedule.get_current_activity(character_id, TARGET_USER, now)
    if not _worth_sharing(act):
        return

    if not _has_any_history(character_id, TARGET_USER):
        return          # 完全没聊过,连判断的必要都没有

    sent, last = _sent_today(character_id, TARGET_USER)
    if sent >= MAX_PER_DAY:
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

    print(f'[life_share] {character_id} 正在「{act["title"]}」,问问他想不想说')
    _generate_share(character_id, TARGET_USER, act)


def _loop():
    global _stop
    time.sleep(120)     # 启动后等其它初始化跑完
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
          f'（发不发由角色自己按关系判断，技术上限一天 {MAX_PER_DAY} 条）')