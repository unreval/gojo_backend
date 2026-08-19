"""便利贴吐槽表 —— AI 在聊天时"心里 OS / 内心吐槽"的记录

产生方式:
  每轮 /chat/text 回完之后,route_chat 起个后台线程调 grumble_engine,
  Haiku 快速判断这一轮有没有值得【心里嘀咕一句】的东西 —— 有就写一条,没就 skip。
  这些内容【不显示在对话里】,只在便利贴页面显示。

设计参考:
  · 结构照抄 db_diary 那套(建表 + 独立 CRUD 函数),不动 db.py。
  · 频率控制走"每日上限"(见 count_grumbles_since),避免刷屏和烧钱。
  · 用户能"撕掉"(delete),但不能"贴新的"(不提供 add) —— 便利贴是 AI 单方面产出的。
"""
from db import get_conn


# ────────────────────────── 建表 ──────────────────────────

def init_grumble_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS char_grumble (
        id SERIAL PRIMARY KEY,
        character_id TEXT NOT NULL DEFAULT 'gojo',
        user_id TEXT NOT NULL DEFAULT 'default',
        content TEXT NOT NULL,
        emotion TEXT DEFAULT '平静',
        trigger_snippet TEXT DEFAULT '',     -- 用户当时那句话的前 80 字,便利贴上显示"关于:xxx"
        viewed BOOLEAN DEFAULT FALSE,        -- 用户看过没(首页红点用)
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_grumble_user_time
                   ON char_grumble (user_id, character_id, created_at DESC)''')
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 便利贴吐槽表已就绪：char_grumble')


# ────────────────────────── 写入 ──────────────────────────

def add_grumble(character_id, user_id, content, emotion='平静', trigger_snippet=''):
    """AI 决定要嘀咕时,由 grumble_engine 调用。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO char_grumble (character_id, user_id, content, emotion, trigger_snippet)
           VALUES (%s, %s, %s, %s, %s) RETURNING id, created_at''',
        (character_id, user_id, content, emotion, (trigger_snippet or '')[:200])
    )
    new_id, created_at = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return new_id, created_at


# ────────────────────────── 读取 ──────────────────────────

def list_grumbles(user_id, character_id=None, limit=100, offset=0):
    """按时间倒序列出便利贴。character_id=None 就全部角色混着来。"""
    conn = get_conn()
    cur = conn.cursor()
    if character_id:
        cur.execute(
            '''SELECT id, character_id, content, emotion, trigger_snippet, viewed, created_at
               FROM char_grumble
               WHERE user_id = %s AND character_id = %s
               ORDER BY created_at DESC LIMIT %s OFFSET %s''',
            (user_id, character_id, limit, offset)
        )
    else:
        cur.execute(
            '''SELECT id, character_id, content, emotion, trigger_snippet, viewed, created_at
               FROM char_grumble
               WHERE user_id = %s
               ORDER BY created_at DESC LIMIT %s OFFSET %s''',
            (user_id, limit, offset)
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0],
        'character_id': r[1],
        'content': r[2],
        'emotion': r[3] or '平静',
        'trigger_snippet': r[4] or '',
        'viewed': bool(r[5]),
        'created_at': str(r[6]) if r[6] else None,
    } for r in rows]


def count_unviewed(user_id, character_id=None):
    """首页红点用:未看过的条数。"""
    conn = get_conn()
    cur = conn.cursor()
    if character_id:
        cur.execute(
            'SELECT COUNT(*) FROM char_grumble WHERE user_id=%s AND character_id=%s AND viewed=FALSE',
            (user_id, character_id)
        )
    else:
        cur.execute(
            'SELECT COUNT(*) FROM char_grumble WHERE user_id=%s AND viewed=FALSE',
            (user_id,)
        )
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return int(n or 0)


def count_grumbles_since(character_id, user_id, since_dt):
    """频率控制用:某个时间点之后写了几条(每日上限判断)。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT COUNT(*) FROM char_grumble
           WHERE character_id=%s AND user_id=%s AND created_at >= %s''',
        (character_id, user_id, since_dt)
    )
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return int(n or 0)


# ────────────────────────── 更新 / 删除 ──────────────────────────

def mark_all_viewed(user_id, character_id=None):
    """打开便利贴页就一次性标全部为已看。character_id=None 就标该用户全部。"""
    conn = get_conn()
    cur = conn.cursor()
    if character_id:
        cur.execute(
            'UPDATE char_grumble SET viewed=TRUE WHERE user_id=%s AND character_id=%s AND viewed=FALSE',
            (user_id, character_id)
        )
    else:
        cur.execute(
            'UPDATE char_grumble SET viewed=TRUE WHERE user_id=%s AND viewed=FALSE',
            (user_id,)
        )
    n = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return n


def delete_grumble(grumble_id, user_id):
    """用户手动撕掉某张便利贴。带 user_id 是防串号。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'DELETE FROM char_grumble WHERE id=%s AND user_id=%s',
        (grumble_id, user_id)
    )
    n = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return n > 0
