"""migrate_promise_identity.py —— 承诺身份去重

手动执行一次。不要在 gojo_server 启动时调用。

在 gojo_backend 目录下:
    python migrate_promise_identity.py

顺序必须是先清掉已兑现和重复的活跃行，再建立唯一索引。
索引若提前建立，存量重复会让 CREATE UNIQUE INDEX 失败。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db import get_conn
import db_promise


def _count_update(cur, sql):
    cur.execute(sql)
    return len(cur.fetchall())


def migrate_promise_identity():
    conn = get_conn()
    cur = conn.cursor()
    try:
        # 1) 历史已兑现的 once 退出活跃集合
        fired = _count_update(cur, '''
            UPDATE proactive_promise SET is_active = FALSE
             WHERE trigger_kind = 'once' AND is_fired = TRUE AND is_active = TRUE
            RETURNING id''')
        print(f'[migrate] once fired deactivated: {fired}')

        # 2) 未触发的重复行只保留 id 最小的一条
        once_dups = _count_update(cur, '''
            UPDATE proactive_promise p SET is_active = FALSE, last_error = 'dedup_migration'
             WHERE p.trigger_kind = 'once' AND p.is_fired = FALSE AND p.is_active = TRUE
               AND EXISTS (SELECT 1 FROM proactive_promise q
                            WHERE q.trigger_kind = 'once' AND q.is_fired = FALSE
                              AND q.is_active = TRUE
                              AND q.user_id = p.user_id AND q.character_id = p.character_id
                              AND q.trigger_at = p.trigger_at AND q.id < p.id)
            RETURNING id''')
        print(f'[migrate] once duplicates deactivated: {once_dups}')

        # 3) daily 同理按 trigger_time 去重
        daily_dups = _count_update(cur, '''
            UPDATE proactive_promise p SET is_active = FALSE, last_error = 'dedup_migration'
             WHERE p.trigger_kind = 'daily' AND p.is_active = TRUE
               AND EXISTS (SELECT 1 FROM proactive_promise q
                            WHERE q.trigger_kind = 'daily' AND q.is_active = TRUE
                              AND q.user_id = p.user_id AND q.character_id = p.character_id
                              AND q.trigger_time = p.trigger_time AND q.id < p.id)
            RETURNING id''')
        print(f'[migrate] daily duplicates deactivated: {daily_dups}')
        conn.commit()
    except Exception as e:
        print(f'[migrate] promise identity cleanup skipped: {e}')
        conn.rollback()
        cur.close()
        conn.close()
        return

    # 4) 最后才建唯一索引。1–3 已提交，索引失败不会把去重回滚掉。
    try:
        cur.execute('SAVEPOINT promise_identity_idx')
        db_promise.ensure_promise_identity_indexes(cur)
        cur.execute('RELEASE SAVEPOINT promise_identity_idx')
        conn.commit()
        print('[migrate] identity indexes ready')
    except Exception as e:
        print(f'[migrate] identity index skipped: {e}')
        try:
            cur.execute('ROLLBACK TO SAVEPOINT promise_identity_idx')
            conn.commit()
        except Exception:
            conn.rollback()
    finally:
        cur.close()
        conn.close()


if __name__ == '__main__':
    migrate_promise_identity()
