"""Deterministic, per-source-event read receipts for one-to-one chat."""
from datetime import datetime, timezone

from db import get_conn
from reply_availability import parse_pending_event_meta


MAX_SOURCE_EVENT_ID_LENGTH = 120


def source_event_ids_from_claim(bundle):
    """Recover only exact IDs present in the immutable phone-check snapshot."""
    ids = []
    for meta in parse_pending_event_meta((bundle or {}).get('event_meta')):
        source_id = meta.get('source_event_id') or meta.get('event_id')
        if source_id:
            ids.append(str(source_id).strip())

    first = str((bundle or {}).get('first_source_event_id') or '').strip()
    last = str((bundle or {}).get('last_source_event_id') or '').strip()
    if first and first not in ids:
        ids.insert(0, first)
    if last and last not in ids:
        ids.append(last)

    ordered = list(dict.fromkeys(source_id for source_id in ids if source_id))
    pending_count = int((bundle or {}).get('pending_count') or 0)
    if len(ordered) < pending_count:
        print(f'[read_receipt] phone_check_id={(bundle or {}).get("id")} '
              f'known_ids={len(ordered)} pending_count={pending_count} '
              'legacy_snapshot_missing_ids')
    return ordered


def init_read_receipt_table():
    conn = get_conn()
    cur = conn.cursor()
    try:
        # Serialize first-time DDL across concurrently starting app instances.
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))',
                    ('chat_read_receipt_schema_init',))
        cur.execute('''CREATE TABLE IF NOT EXISTS chat_read_receipt (
            user_id TEXT NOT NULL,
            character_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            seen_at TIMESTAMPTZ NOT NULL,
            seen_via TEXT NOT NULL CHECK (seen_via IN ('immediate', 'phone_check')),
            phone_check_id BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (user_id, character_id, source_event_id)
        )''')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def mark_source_events_seen_tx(cur, user_id, character_id, source_event_ids,
                               seen_at, seen_via, phone_check_id=None):
    """Insert first-seen facts on the caller's transaction and cursor."""
    if not user_id or not character_id or seen_via not in ('immediate', 'phone_check'):
        raise ValueError('invalid read receipt scope or source')
    if not isinstance(seen_at, datetime) or seen_at.tzinfo is None:
        raise ValueError('seen_at must be timezone-aware')
    ids = list(dict.fromkeys(str(item).strip() for item in source_event_ids or ()))
    if any(not item or len(item) > MAX_SOURCE_EVENT_ID_LENGTH for item in ids):
        raise ValueError('invalid source_event_id')
    created = 0
    for source_id in ids:
        cur.execute('''INSERT INTO chat_read_receipt
                       (user_id, character_id, source_event_id, seen_at,
                        seen_via, phone_check_id)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (user_id, character_id, source_event_id)
                       DO NOTHING RETURNING source_event_id''',
                    (user_id, character_id, source_id, seen_at,
                     seen_via, phone_check_id))
        inserted = bool(cur.fetchone())
        created += inserted
        print(f'[read_receipt] source_event_id={source_id} '
              f'character_id={character_id} seen_via={seen_via} '
              f'phone_check_id={phone_check_id} '
              f'result={"created" if inserted else "already_exists"}')
    return created


def mark_immediate_seen(user_id, character_id, source_event_id):
    conn = get_conn()
    cur = conn.cursor()
    try:
        created = mark_source_events_seen_tx(
            cur, user_id, character_id, [source_event_id],
            datetime.now(timezone.utc), 'immediate')
        conn.commit()
        return created
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


def query_read_receipts(user_id, character_id, source_event_ids):
    if not source_event_ids:
        return []
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute('''SELECT source_event_id, seen_at, seen_via, phone_check_id
                       FROM chat_read_receipt
                       WHERE user_id=%s AND character_id=%s
                         AND source_event_id=ANY(%s)''',
                    (user_id, character_id, list(source_event_ids)))
        rows = cur.fetchall()
        by_id = {
            row[0]: {
                'source_event_id': row[0],
                'seen_at': row[1].isoformat(),
                'seen_via': row[2],
                'phone_check_id': row[3],
            }
            for row in rows
        }
        return [by_id[source_id] for source_id in source_event_ids
                if source_id in by_id]
    finally:
        cur.close()
        conn.close()
