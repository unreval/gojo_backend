"""群聊相关建表（第一步·骨架）

设计成独立函数，在 gojo_server.py 启动时单独调用一次：
    from db_group import init_group_tables
    init_group_tables()
不改动 db.py 里现有的 init_db()，互不干扰，最大限度避免碰坏现有表。

三张表：
  groups          —— 群本身（群名、头像、创建人）
  group_members   —— 群里有哪些成员（角色 character_id，或用户 'user'）
  group_messages  —— 群里的每条消息（谁发的、内容、时间）
"""
from db import get_conn


def init_group_tables():
    conn = get_conn()
    cur = conn.cursor()

    # ── 群 ──
    cur.execute('''CREATE TABLE IF NOT EXISTS groups (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        avatar_url TEXT,
        owner_user_id TEXT NOT NULL DEFAULT 'default',
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 群成员 ──
    # member_type: 'character' = 角色，'user' = 创建人本人
    # member_id  : 角色时存 character_id；用户时存 user_id
    cur.execute('''CREATE TABLE IF NOT EXISTS group_members (
        id SERIAL PRIMARY KEY,
        group_id INTEGER NOT NULL,
        member_type TEXT NOT NULL DEFAULT 'character',
        member_id TEXT NOT NULL,
        is_owner_role BOOLEAN DEFAULT FALSE,
        joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # ── 群消息 ──
    # sender_type: 'user' = 创建人发言，'character' = 角色发言
    # sender_id  : user_id 或 character_id
    # jp / zh    : 角色发言时存日语原文 + 中文；用户发言时 jp 留空、zh 存用户原话
    cur.execute('''CREATE TABLE IF NOT EXISTS group_messages (
        id SERIAL PRIMARY KEY,
        group_id INTEGER NOT NULL,
        sender_type TEXT NOT NULL,
        sender_id TEXT NOT NULL,
        jp TEXT DEFAULT '',
        zh TEXT DEFAULT '',
        emotion TEXT DEFAULT '平静',
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')

    # 查询群消息历史时按 group_id + 时间排序，建个索引
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_group_messages_gid_ts
                   ON group_messages (group_id, timestamp)''')

    conn.commit()
    cur.close()
    conn.close()
    print('[init] 群聊表已就绪：groups / group_members / group_messages')
