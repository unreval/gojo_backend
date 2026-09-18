"""羁绊记忆建表（记忆四层化·第二三层）

设计成独立函数，在 gojo_server.py 启动时单独调用一次：
    from db_bond import init_bond_table
    init_bond_table()
不改动 db.py 里现有的 init_db()，互不干扰。

一张表，两种 kind：
  kind='between' —— "我们之间的事"：她和这个角色的共同经历/约定/互动
  kind='told'    —— "她告诉过我的事"：她告诉这个角色的、关于角色本人或其世界的信息
                    （包括剧透未来剧情。角色记得"她说过"，信不信由人设决定）
"""
from db import get_conn

BOND_EXPIRES_AT_DDL = (
    "ALTER TABLE bond_memory ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ"
)


def init_bond_table():
    """Generic schema only. No incident-specific data repairs."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS bond_memory (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'between',
        content TEXT NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_bond_memory_uid_cid
                   ON bond_memory (user_id, character_id, kind)''')
    cur.execute("ALTER TABLE bond_memory ADD COLUMN IF NOT EXISTS recall_status TEXT DEFAULT 'active'")
    cur.execute(BOND_EXPIRES_AT_DDL)
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 羁绊记忆表已就绪：bond_memory')
