"""group_bubbler.py —— 群聊定时主动冒泡

做什么：群里聊到一半没人说话了，过一会儿某个角色像真人一样自己冒个泡
        （接刚才的话题、想起点什么、或者 cue 一下群主），你回群时看到红点和新消息。

★ 成本控制（重点，默认很克制）：
  - 每个群每天最多冒泡 MAX_PER_DAY 次（默认 3）
  - 群里安静满 QUIET_MIN 分钟才考虑（默认 25 分钟），且离最后一条不超过 MAX_IDLE_H 小时
    （默认 6 小时——超过就说明话题早凉了，硬聊很尬）
  - 深夜不冒泡（默认 23:00~08:00）
  - 冒泡【不合成语音】：只出文字，省 Fish 额度；你想听时点"播放"会现场合成
  - 每次只让 1 个角色说 1 条气泡，绝不刷屏

★ 全部可用环境变量调：
  GROUP_BUBBLE=0        关掉这个功能
  BUBBLE_MAX_PER_DAY=3  每群每天上限
  BUBBLE_QUIET_MIN=25   安静多少分钟后才冒泡
  BUBBLE_MAX_IDLE_H=6   超过几小时就不冒了
  BUBBLE_CHECK_MIN=10   后台多久检查一次
"""
import os
import time
import random
import threading
from datetime import datetime, timedelta, timezone

from config import CN_TZ
from db import get_conn

ENABLED       = os.environ.get('GROUP_BUBBLE', '1') == '1'
MAX_PER_DAY   = int(os.environ.get('BUBBLE_MAX_PER_DAY', '3'))
QUIET_MIN     = int(os.environ.get('BUBBLE_QUIET_MIN', '25'))
MAX_IDLE_H    = int(os.environ.get('BUBBLE_MAX_IDLE_H', '6'))
CHECK_MIN     = int(os.environ.get('BUBBLE_CHECK_MIN', '10'))
QUIET_START   = 23   # 深夜静音时段（本地时间）
QUIET_END     = 8


def init_bubble_column():
    """给 group_messages 加个标记列，用来数今天冒了几次泡（幂等）。"""
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("ALTER TABLE group_messages ADD COLUMN IF NOT EXISTS is_proactive BOOLEAN DEFAULT FALSE")
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f'[bubble] 建列失败（不影响启动）：{e}')


def _all_groups():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, owner_user_id FROM groups')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{'id': r[0], 'owner': r[1]} for r in rows]


def _last_message_info(gid):
    """返回 (最后一条时间 utc, 最后说话的 sender_id, 最后一条 sender_type)。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT timestamp, sender_id, sender_type FROM group_messages
           WHERE group_id = %s ORDER BY id DESC LIMIT 1''', (gid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None, None, None
    return row[0], row[1], row[2]


def _today_bubble_count(gid):
    """今天这个群已经冒了几次泡。"""
    today_cn = datetime.now(CN_TZ).date()
    # 数据库存 UTC，用本地日 0 点换算成 UTC 边界
    start_cn = datetime(today_cn.year, today_cn.month, today_cn.day, tzinfo=CN_TZ)
    start_utc = start_cn.astimezone(timezone.utc).replace(tzinfo=None)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT COUNT(*) FROM group_messages
           WHERE group_id = %s AND is_proactive = TRUE AND timestamp >= %s''',
        (gid, start_utc))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


def _mark_proactive(gid, msg_id):
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('UPDATE group_messages SET is_proactive = TRUE WHERE id = %s', (msg_id,))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f'[bubble] 标记失败：{e}')


BUBBLE_SCENE = '''

【★ 主动冒泡场景——群里安静下来了】
刚才的对话停了一会儿，没人说话。你像真人玩手机那样，突然想起点什么，在群里【主动】说一句。
可以是：接着刚才的话题补一句想到的、想起件相关的小事、随口 cue 一下群主或某个成员、
或者干脆说件新的小事。选此刻最像"你"的一种。
硬性要求：
1. 只说【1 条】气泡，短一点（10~35 字），像随手打的一句话，不要长篇大论。
2. 不要重复刚才已经说过的话和已经给过的观点。
3. 不要说"我回来了""打断一下"这种旁白式开场，直接说内容。
4. 不要每次都 cue 群主问"在吗"——大部分时候是你自己想到什么就说什么。'''


def _try_bubble_one_group(g):
    """返回 True 表示这个群冒泡了。"""
    from route_group import (_get_group, _get_member_characters, _get_group_history,
                             _save_group_message, _generate_one_reply, _history_text)

    gid = g['id']
    owner = g['owner'] or 'default'

    last_ts, last_sender, last_type = _last_message_info(gid)
    if last_ts is None:
        return False   # 空群不冒泡

    now_utc = datetime.utcnow()
    idle_min = (now_utc - last_ts).total_seconds() / 60
    if idle_min < QUIET_MIN:
        return False                      # 还在聊，别插嘴
    if idle_min > MAX_IDLE_H * 60:
        return False                      # 话题早凉了，硬聊很尬

    if _today_bubble_count(gid) >= MAX_PER_DAY:
        return False                      # 今天配额用完

    members = _get_member_characters(gid)
    if not members:
        return False

    # 挑一个人：优先挑不是最后一个说话的（避免自言自语连发）
    candidates = [m for m in members if m['id'] != last_sender] or members
    speaker = random.choice(candidates)

    history = _get_group_history(gid, limit=12)
    if not history:
        return False

    # 用最近的对话做话题引子
    recent_user_text = ''
    for h in reversed(history):
        if h['sender_type'] == 'user':
            recent_user_text = h['zh'] or ''
            break

    reply = _generate_one_reply(
        gid, speaker, history,
        recent_user_text or '（群里刚才在闲聊）',
        members, user_id=owner,
        extra_scene=BUBBLE_SCENE,
    )
    if not reply:
        return False

    msgs = reply.get('messages', [])[:1]   # 只保留 1 条
    if not msgs:
        return False
    m = msgs[0]

    # ★ 冒泡不合成语音（省 Fish 额度）；用户想听时点"播放"会现场合成
    msg_id = _save_group_message(gid, 'character', speaker['id'],
                                 m.get('jp', ''), m.get('zh', ''),
                                 reply.get('emotion', '平静'))
    if msg_id:
        _mark_proactive(gid, msg_id)
    print(f'[bubble] 群{gid} · {speaker["name"]} 冒泡：{m.get("zh", "")[:30]}')
    return True


def _loop():
    print(f'[bubble] 定时冒泡已启动：每 {CHECK_MIN} 分钟检查一次 · '
          f'安静 {QUIET_MIN} 分钟后冒 · 每群每天最多 {MAX_PER_DAY} 次 · 深夜静音')
    while True:
        try:
            time.sleep(CHECK_MIN * 60)
            hour = datetime.now(CN_TZ).hour
            if hour >= QUIET_START or hour < QUIET_END:
                continue   # 深夜不打扰
            for g in _all_groups():
                try:
                    _try_bubble_one_group(g)
                except Exception as e:
                    print(f'[bubble] 群{g["id"]} 冒泡失败：{e}')
        except Exception as e:
            print(f'[bubble] 循环异常：{e}')


def start_bubbler():
    if not ENABLED:
        print('[bubble] 未启用（GROUP_BUBBLE=0）')
        return
    init_bubble_column()
    threading.Thread(target=_loop, daemon=True).start()
