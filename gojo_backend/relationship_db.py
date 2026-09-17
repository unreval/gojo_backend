"""relationship_db.py —— 感情判断系统 v4 · 数据库表定义

设计成独立函数，仿 db_bond.py 的模式，在 gojo_server.py 启动时单独调一次：
    from relationship_db import init_relationship_tables
    init_relationship_tables()
不改动 db.py 里现有的 init_db()，互不干扰。

★ 六张表 + 一张 JSONB 内嵌字段（hypothesis 走 JSONB 不建表）：
  1. relationship_state    —— 主账本：W/F/I/Trust/Attachment/C/P + hypothesis JSONB
  2. provenance_log        —— 每次状态变化的证据链
  3. declared_stance       —— 已确认表态清单（防"反悔bug"）
  4. boundary_hits         —— 每个雷区被触碰的历史
  5. repair_log            —— 每次修复尝试及结果
  6. interaction_stats     —— 消息级别的轻量统计（Tone/Reciprocity/Pursue-Withdraw 用）
  7. rel_event_applications —— 关系 apply 的 exactly-once 身份
     (event_id, processor_type, processor_version)

命名前缀：所有表都以 `rel_` 开头，方便和现有 bond_memory / short_memory 等区分。
"""
import json
import threading
from contextlib import contextmanager

from db import get_conn

_tls = threading.local()


class _TxnConn:
    """psycopg2 connection proxy.

    Inside relationship_txn(), commit/close/rollback are no-ops so apply_* /
    provenance / boundary / repair writers join the same uncommitted unit.
    Outside a txn this is a normal connection.
    """

    def __init__(self, conn, owned):
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_owned', owned)

    def commit(self):
        if self._owned:
            self._conn.commit()

    def rollback(self):
        if self._owned:
            self._conn.rollback()

    def close(self):
        if self._owned:
            self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def work_get_conn():
    """Use this instead of db.get_conn() in relationship ledger writers."""
    shared = getattr(_tls, 'conn', None)
    if shared is not None:
        return _TxnConn(shared, owned=False)
    return _TxnConn(get_conn(), owned=True)


@contextmanager
def relationship_txn():
    """One DB transaction for relationship mutation + application row + finish."""
    if getattr(_tls, 'conn', None) is not None:
        yield _tls.conn
        return
    conn = get_conn()
    _tls.conn = conn
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        _tls.conn = None
        try:
            conn.close()
        except Exception:
            pass


def try_begin_application(event_id, processor_type, processor_version,
                          user_id=None, character_id=None, payload=None):
    """Insert unique application identity. True = this caller may mutate the ledger."""
    event_id = str(event_id or '').strip()
    processor_type = str(processor_type or '').strip()
    processor_version = str(processor_version or '').strip()
    if not event_id or not processor_type or not processor_version:
        return True
    conn, owned = (getattr(_tls, 'conn', None), False)
    if conn is None:
        conn = get_conn()
        owned = True
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO rel_event_applications
                   (event_id, processor_type, processor_version,
                    user_id, character_id, result_json)
               VALUES (%s, %s, %s, %s, %s, %s::jsonb)
               ON CONFLICT (event_id, processor_type, processor_version)
               DO NOTHING
               RETURNING event_id''',
            (event_id, processor_type, processor_version,
             user_id, character_id,
             json.dumps(payload if isinstance(payload, dict) else {},
                        ensure_ascii=False)),
        )
        won = bool(cur.fetchone())
        if owned:
            conn.commit()
        return won
    except Exception:
        if owned:
            try:
                conn.rollback()
            except Exception:
                pass
        raise
    finally:
        cur.close()
        if owned:
            conn.close()


def get_application_payload(event_id, processor_type, processor_version):
    event_id = str(event_id or '').strip()
    if not event_id:
        return None
    conn, owned = (getattr(_tls, 'conn', None), False)
    if conn is None:
        conn = get_conn()
        owned = True
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT result_json FROM rel_event_applications
               WHERE event_id=%s AND processor_type=%s AND processor_version=%s''',
            (event_id, processor_type, processor_version))
        row = cur.fetchone()
        if owned:
            conn.commit()
        if not row or row[0] is None:
            return None
        payload = row[0]
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return None
        return payload if isinstance(payload, dict) else None
    except Exception:
        if owned:
            try:
                conn.rollback()
            except Exception:
                pass
        return None
    finally:
        cur.close()
        if owned:
            conn.close()


def init_relationship_tables():
    conn = get_conn()
    cur = conn.cursor()

    # ── 1. 主账本 ─────────────────────────────────────────────
    # 每个 (user_id, character_id) 一行；hypothesis 用 JSONB 直接嵌入
    cur.execute('''CREATE TABLE IF NOT EXISTS rel_state (
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        warmth REAL DEFAULT 0,
        friction JSONB DEFAULT '{}'::jsonb,
        intimacy REAL DEFAULT 0,
        trust REAL DEFAULT 0,
        attachment REAL DEFAULT 0,
        commitment REAL DEFAULT 0,
        passion REAL DEFAULT 0,
        pending_passion INTEGER DEFAULT 0,
        pending_hypothesis JSONB DEFAULT '[]'::jsonb,
        banter_baseline TEXT DEFAULT 'reserved',
        last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, character_id))''')

    # ── 2. Provenance log ─────────────────────────────────────
    # 每次任何状态变化都记一条，供 debug 和 model integrity check
    cur.execute('''CREATE TABLE IF NOT EXISTS rel_provenance_log (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        state_field TEXT NOT NULL,
        value_before REAL,
        value_after REAL,
        delta REAL,
        signal_type TEXT,
        confidence TEXT,
        rule TEXT,
        evidence_refs JSONB DEFAULT '[]'::jsonb,
        note TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_rel_prov_user_char
                   ON rel_provenance_log (user_id, character_id, timestamp DESC)''')

    # ── 3. Declared Stance（已确认表态清单）─────────────────
    # 生成层的硬约束，只能被走完完整流程的负面事件正式废除
    cur.execute('''CREATE TABLE IF NOT EXISTS rel_declared_stance (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        stance_type TEXT NOT NULL,
        content TEXT NOT NULL,
        source_event_ref TEXT,
        status TEXT DEFAULT 'active',
        declared_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        revoked_at TIMESTAMP,
        revoke_reason TEXT)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_rel_stance_user_char
                   ON rel_declared_stance (user_id, character_id, status)''')

    # ── 4. Boundary Hits（雷区触碰历史）────────────────────
    # 记录每个 (角色, 用户, 话题) 被踩过几次、上次啥时候、是否已明确表态过
    cur.execute('''CREATE TABLE IF NOT EXISTS rel_boundary_hits (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        topic_id TEXT NOT NULL,
        hit_count INTEGER DEFAULT 1,
        known_by_user BOOLEAN DEFAULT FALSE,
        last_severity TEXT,
        last_intentional TEXT,
        last_hit_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        first_hit_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, character_id, topic_id))''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_rel_boundary_user_char
                   ON rel_boundary_hits (user_id, character_id)''')

    # ── 5. Repair Log（修复事件历史）───────────────────────
    cur.execute('''CREATE TABLE IF NOT EXISTS rel_repair_log (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        conflict_ref TEXT,
        acknowledgment BOOLEAN DEFAULT FALSE,
        responsibility BOOLEAN DEFAULT FALSE,
        corrective_action BOOLEAN DEFAULT FALSE,
        quality_score REAL,
        quality_tier TEXT,
        f_reduce_ratio REAL,
        trust_delta REAL,
        confidence TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_rel_repair_user_char
                   ON rel_repair_log (user_id, character_id, timestamp DESC)''')

    # ── 6. Interaction Stats（轻量消息级统计）─────────────
    # 用于 Tone / Reciprocity / Pursue-Withdraw 的滑动窗口计算
    # 每条用户消息 + 角色消息各写一行
    cur.execute('''CREATE TABLE IF NOT EXISTS rel_interaction_stats (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        direction TEXT NOT NULL,
        tone_category TEXT,
        is_reciprocal BOOLEAN,
        is_initiator BOOLEAN DEFAULT FALSE,
        session_id TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_rel_interaction_user_char
                   ON rel_interaction_stats (user_id, character_id, timestamp DESC)''')

    # ── 7. Exactly-once relationship application identity ─────
    # Ledger mutation is keyed by (event_id, processor_type, processor_version).
    # Retry of the same Raw Event must not apply another delta.
    cur.execute('''CREATE TABLE IF NOT EXISTS rel_event_applications (
        event_id TEXT NOT NULL,
        processor_type TEXT NOT NULL,
        processor_version TEXT NOT NULL,
        user_id TEXT,
        character_id TEXT,
        result_json JSONB DEFAULT '{}'::jsonb,
        applied_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (event_id, processor_type, processor_version))''')

    cur.execute('''CREATE TABLE IF NOT EXISTS rel_offline_character_state (
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, character_id))''')

    conn.commit()
    cur.close()
    conn.close()
    print('[init] 感情判断系统 v4 数据表已就绪：'
          'rel_state / rel_provenance_log / rel_declared_stance / '
          'rel_boundary_hits / rel_repair_log / rel_interaction_stats / '
          'rel_event_applications / rel_offline_character_state')


def ensure_state_row(user_id: str, character_id: str, banter_baseline: str = 'reserved'):
    """确保 (user_id, character_id) 在 rel_state 里有一行；没有就用默认值创建。
    调用方（engine / reader）在任何读写前先调这个函数，避免"读到空行"分歧。
    """
    conn = work_get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO rel_state (user_id, character_id, banter_baseline)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (user_id, character_id) DO NOTHING''',
                (user_id, character_id, banter_baseline))
    conn.commit()
    cur.close()
    conn.close()
