"""Shadow-mode audit of deterministic classification decisions per turn.

Observes existing classifiers and writes only its own audit table.
Gated by SHADOW_AUDIT_ENABLED (default: False); never writes memory state.
"""
import hashlib
import json
import os
from datetime import datetime, timezone

from db import get_conn


def _bool_env(name, default=False):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in {'0', 'false', 'no', 'off'}


SHADOW_AUDIT_ENABLED = _bool_env('SHADOW_AUDIT_ENABLED')

SHADOW_AUDIT_DDL = (
    '''CREATE TABLE IF NOT EXISTS shadow_audit_log (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        source_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
        user_text_hash TEXT NOT NULL,
        text_length INTEGER NOT NULL DEFAULT 0,
        lifecycle_classification JSONB NOT NULL,
        auto_pin_detections JSONB NOT NULL DEFAULT '[]'::jsonb,
        would_write_sticky BOOLEAN NOT NULL DEFAULT FALSE,
        would_save_long_memory BOOLEAN NOT NULL DEFAULT FALSE,
        would_record_lifecycle BOOLEAN NOT NULL DEFAULT FALSE,
        would_auto_pin BOOLEAN NOT NULL DEFAULT FALSE,
        memory_kind TEXT,
        importance REAL,
        reason TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_shadow_audit_user
       ON shadow_audit_log (user_id, character_id, created_at DESC)''',
)


def init_shadow_audit_table(conn=None):
    """Explicitly install the audit table; safe to call repeatedly."""
    own = conn is None
    if own:
        conn = get_conn()
    cur = None
    try:
        cur = conn.cursor()
        for ddl in SHADOW_AUDIT_DDL:
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


def _text_hash(text):
    return hashlib.sha256((text or '').encode('utf-8')).hexdigest()


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def shadow_log_turn(user_id, character_id, user_text, *,
                    source_event_ids=(), now=None):
    """Return the audit row id, or None when disabled/empty.

    Failures propagate to the context-layer hook, which skips this observer.
    """
    if not SHADOW_AUDIT_ENABLED:
        return None

    now = now or datetime.now(timezone.utc)
    text = (user_text or '').strip()
    if not text:
        return None

    from memory_lifecycle import classify_memory_lifecycle
    from auto_pin import detect_auto_pins

    classification = classify_memory_lifecycle(text, now=now)
    pins = detect_auto_pins(text)
    # Pin drafts contain user text and matched spans; retain only decisions.
    pin_metadata = [
        {key: pin[key] for key in ('pin_type', 'topic', 'action') if key in pin}
        for pin in pins
    ]
    ids = [str(x) for x in (source_event_ids or ()) if str(x).strip()]
    row = {
        'user_id': user_id,
        'character_id': character_id,
        'source_event_ids': _json(ids),
        'user_text_hash': _text_hash(text),
        'text_length': len(text),
        'lifecycle_classification': _json(classification),
        'auto_pin_detections': _json(pin_metadata),
        'would_write_sticky': bool(classification.get('should_write_sticky')),
        'would_save_long_memory': bool(classification.get('should_save_long_memory')),
        'would_record_lifecycle': bool(classification.get('should_record_lifecycle')),
        'would_auto_pin': len(pins) > 0,
        'memory_kind': classification.get('memory_kind'),
        'importance': classification.get('importance'),
        'reason': classification.get('reason'),
        'created_at': now,
    }

    conn = get_conn()
    cur = None
    try:
        cur = conn.cursor()
        cur.execute(
            '''INSERT INTO shadow_audit_log (
                   user_id, character_id, source_event_ids,
                   user_text_hash, text_length,
                   lifecycle_classification, auto_pin_detections,
                   would_write_sticky, would_save_long_memory,
                   would_record_lifecycle, would_auto_pin,
                   memory_kind, importance, reason, created_at
               ) VALUES (
                   %(user_id)s, %(character_id)s, %(source_event_ids)s::jsonb,
                   %(user_text_hash)s, %(text_length)s,
                   %(lifecycle_classification)s::jsonb, %(auto_pin_detections)s::jsonb,
                   %(would_write_sticky)s, %(would_save_long_memory)s,
                   %(would_record_lifecycle)s, %(would_auto_pin)s,
                   %(memory_kind)s, %(importance)s, %(reason)s, %(created_at)s
               ) RETURNING id''',
            row,
        )
        row_id = cur.fetchone()
        conn.commit()
        return row_id[0] if row_id else None
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            if cur is not None:
                cur.close()
        finally:
            conn.close()
