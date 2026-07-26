"""主动消息数据层：存"角色主动发来、用户还没读"的消息 + 取未读接口。

用途：
  - 任务汇报（现在做）：五条按约定时间主动发一条任务报备。
  - 以后的主动消息（问候/想念等）：同一张表，kind 区分。
  前端拉 /proactive/pending 取未读；真推送下一轮接。

一张表：proactive_msg
  kind: 'report'（任务汇报）/ 'greeting' / 'miss' 等，方便以后扩展。
"""
from datetime import datetime
from config import CN_TZ
from db import get_conn


def init_proactive_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS proactive_msg (
        id SERIAL PRIMARY KEY,
        character_id TEXT NOT NULL DEFAULT 'gojo',
        user_id TEXT NOT NULL DEFAULT 'default',
        kind TEXT NOT NULL DEFAULT 'report',
        jp TEXT NOT NULL,
        zh TEXT DEFAULT '',
        emotion TEXT DEFAULT '平静',
        audio_b64 TEXT DEFAULT '',
        is_read BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    cur.close()
    conn.close()
    print('[proactive] 主动消息表已就绪')


def add_proactive_msg(character_id, user_id, kind, jp, zh='', emotion='平静', audio_b64='', created_at=None):
    conn = get_conn()
    cur = conn.cursor()
    if created_at is not None:
        cur.execute(
            '''INSERT INTO proactive_msg (character_id, user_id, kind, jp, zh, emotion, audio_b64, created_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id, created_at''',
            (character_id, user_id, kind, jp, zh, emotion, audio_b64, created_at)
        )
    else:
        cur.execute(
            '''INSERT INTO proactive_msg (character_id, user_id, kind, jp, zh, emotion, audio_b64)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id, created_at''',
            (character_id, user_id, kind, jp, zh, emotion, audio_b64)
        )
    new_id, ts = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return new_id, ts


def get_pending(user_id, character_id=None):
    """取未读的主动消息（按时间正序，先发的先显示）。"""
    conn = get_conn()
    cur = conn.cursor()
    if character_id:
        cur.execute(
            '''SELECT id, character_id, kind, jp, zh, emotion, audio_b64, created_at
               FROM proactive_msg
               WHERE user_id=%s AND character_id=%s AND is_read=FALSE
               ORDER BY created_at ASC''',
            (user_id, character_id)
        )
    else:
        cur.execute(
            '''SELECT id, character_id, kind, jp, zh, emotion, audio_b64, created_at
               FROM proactive_msg
               WHERE user_id=%s AND is_read=FALSE
               ORDER BY created_at ASC''',
            (user_id,)
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0], 'character_id': r[1], 'kind': r[2], 'jp': r[3], 'zh': r[4],
        'emotion': r[5], 'audio_b64': r[6], 'created_at': str(r[7]) if r[7] else None,
    } for r in rows]


def mark_read(msg_ids):
    if not msg_ids:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE proactive_msg SET is_read=TRUE WHERE id = ANY(%s)', (list(msg_ids),))
    conn.commit()
    cur.close()
    conn.close()


def count_reports_since(character_id, user_id, since_dt):
    """since_dt 之后发过几条任务汇报（防同一天重复发）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "SELECT COUNT(*) FROM proactive_msg WHERE character_id=%s AND user_id=%s AND kind='report' AND created_at >= %s",
        (character_id, user_id, since_dt)
    )
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n
