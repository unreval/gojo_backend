"""migrate_two_level_recall.py —— 两级召回·数据库迁移

在 gojo_server.py 启动时调用一次：
    from migrate_two_level_recall import migrate_two_level
    migrate_two_level()

新增列：
  long_memory:
    mention_count  INTEGER DEFAULT 1    —— 被提起/确认的次数（越多越重要）
    last_mentioned TIMESTAMP            —— 上次被提起的时间（用于时间衰减）
    pinned         BOOLEAN DEFAULT FALSE —— 钉住的记忆，永远注入（用户手动设置）

  bond_memory:
    linked_fact_id INTEGER              —— 关联的 long_memory.id（两级召回的纽带）
"""
from db import get_conn


def migrate_two_level():
    conn = get_conn()
    cur = conn.cursor()
    try:
        # ── long_memory 新增列 ──
        cur.execute('ALTER TABLE long_memory ADD COLUMN IF NOT EXISTS mention_count INTEGER DEFAULT 1')
        cur.execute('ALTER TABLE long_memory ADD COLUMN IF NOT EXISTS last_mentioned TIMESTAMP')
        cur.execute('ALTER TABLE long_memory ADD COLUMN IF NOT EXISTS pinned BOOLEAN DEFAULT FALSE')

        # ── bond_memory 新增列 ──
        cur.execute('ALTER TABLE bond_memory ADD COLUMN IF NOT EXISTS linked_fact_id INTEGER')

        # ── 给 linked_fact_id 加索引（按 fact 拉关联 bond 时用）──
        cur.execute('''CREATE INDEX IF NOT EXISTS idx_bond_linked_fact
                       ON bond_memory (linked_fact_id) WHERE linked_fact_id IS NOT NULL''')

        conn.commit()
        print('[migrate] ✅ 两级召回迁移完成：mention_count / last_mentioned / pinned / linked_fact_id')
    except Exception as e:
        print(f'[migrate] 两级召回迁移跳过：{e}')
    finally:
        cur.close()
        conn.close()
