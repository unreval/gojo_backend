"""Write-only observation log for candidate-producer records.

This is not a durable-memory store. Recall, projection, cognition, prompts,
and relationship code must not read candidate_audit_log. Rows keep a
subjective interpretation that is allowed to be wrong. A failed write is
discarded and cannot change the producer result.
"""
import os
from datetime import datetime, timezone

CANDIDATE_AUDIT_DDL = (
    '''CREATE TABLE IF NOT EXISTS candidate_audit_log (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        batch_id TEXT,
        source_event_id TEXT,
        candidate_index INTEGER,
        quote TEXT,
        summary TEXT,
        interpretation TEXT,
        persistence TEXT,
        gate_accepted BOOLEAN NOT NULL DEFAULT FALSE,
        accepted BOOLEAN NOT NULL DEFAULT FALSE,
        reason TEXT,
        projection_status TEXT,
        projection_reason TEXT,
        module_version TEXT,
        shadow BOOLEAN NOT NULL DEFAULT TRUE,
        occurred_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_candidate_audit_user
       ON candidate_audit_log (user_id, character_id, created_at DESC)''',
    '''CREATE INDEX IF NOT EXISTS idx_candidate_audit_source
       ON candidate_audit_log (source_event_id)''',
)


def _bool_env(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in {'0', 'false', 'no', 'off'}


CANDIDATE_AUDIT_ENABLED = _bool_env('CANDIDATE_AUDIT_ENABLED', default=True)


def init_candidate_audit_table(conn=None):
    """Install the observation table. Safe to call repeatedly.

    A borrowed connection stays in the caller's transaction. An owned
    connection is committed and closed here.
    """
    own = conn is None
    if own:
        from db import get_conn
        conn = get_conn()
    cur = None
    try:
        cur = conn.cursor()
        for ddl in CANDIDATE_AUDIT_DDL:
            cur.execute(ddl)
        if own:
            conn.commit()
    except Exception:
        if own:
            conn.rollback()
        raise
    finally:
        try:
            if cur is not None:
                cur.close()
        finally:
            if own:
                conn.close()


def _text(value):
    return value if isinstance(value, str) else None


def _flag(value, default):
    return value if isinstance(value, bool) else default


def _index(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _timestamptz(value):
    """Keep only an aware datetime. Naive values are not given a timezone."""
    if (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None):
        return value
    return None


def log_candidate(record, *, batch_id=None, occurred_at=None,
                  module_version=None, shadow=None, created_at=None):
    """Insert one candidate observation on its own connection.

    Returns the new row id, or None when the write is disabled, skipped, or
    fails. Exceptions are logged and swallowed. occurred_at is the source
    event time; a missing or naive value stays NULL and is never replaced
    with the current time.
    """
    if not CANDIDATE_AUDIT_ENABLED:
        return None
    if not isinstance(record, dict) or record.get('kind') != 'candidate':
        return None
    user_id = record.get('user_id')
    character_id = record.get('character_id')
    if not isinstance(user_id, str) or not isinstance(character_id, str):
        return None

    from db import get_conn
    conn = None
    cur = None
    try:
        conn = get_conn()
        init_candidate_audit_table(conn)
        cur = conn.cursor()
        cur.execute(
            '''INSERT INTO candidate_audit_log (
                   user_id, character_id, batch_id, source_event_id,
                   candidate_index, quote, summary, interpretation, persistence,
                   gate_accepted, accepted, reason, projection_status,
                   projection_reason, module_version, shadow, occurred_at,
                   created_at
               ) VALUES (
                   %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                   %s, %s, %s
               ) RETURNING id''',
            (
                user_id,
                character_id,
                _text(batch_id if batch_id is not None else record.get('batch_id')),
                _text(record.get('source_event_id')),
                _index(record.get('candidate_index')),
                _text(record.get('quote')),
                _text(record.get('summary')),
                _text(record.get('interpretation')),
                _text(record.get('persistence')),
                _flag(record.get('gate_accepted'), False),
                _flag(record.get('accepted'), False),
                _text(record.get('reason')),
                _text(record.get('projection_status')),
                _text(record.get('projection_reason')),
                _text(module_version if module_version is not None
                      else record.get('module_version')),
                _flag(shadow if shadow is not None else record.get('shadow'), True),
                _timestamptz(occurred_at),
                _timestamptz(created_at) or datetime.now(timezone.utc),
            ),
        )
        row_id = cur.fetchone()
        conn.commit()
        return row_id[0] if row_id else None
    except Exception as exc:
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        print(f'[candidate_audit] write skipped:{exc}')
        return None
    finally:
        try:
            if cur is not None:
                cur.close()
        finally:
            if conn is not None:
                conn.close()
