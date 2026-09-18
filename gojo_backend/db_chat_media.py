"""chat_media — durable image objects associated by source_event_id.

Canonical association is (user_id, chat_id, source_event_id, media_kind).
chat_log rows may be written earlier or later; GET /chatlog joins on
client_msg_id / event_id == source_event_id.

The database stores object_key only. Signed URLs are minted at read time.
"""
import hashlib
import re
import uuid

from db import get_conn

import media_storage


MIME_TO_EXT = {
    'image/jpeg': 'jpg',
    'image/jpg': 'jpg',
    'image/png': 'png',
    'image/gif': 'gif',
    'image/webp': 'webp',
}

SAFE_KEY_RE = re.compile(r'[^A-Za-z0-9._-]+')


class MediaPersistError(RuntimeError):
    pass


def _safe_key_part(value, fallback='unknown'):
    text = SAFE_KEY_RE.sub('_', str(value or '').strip())
    text = text.strip('._-')[:180]
    return text or fallback


def object_key_for(user_id, chat_id, source_event_id, mime_type='image/jpeg'):
    ext = MIME_TO_EXT.get((mime_type or '').lower(), 'jpg')
    return (
        f'chat-media/{_safe_key_part(user_id)}/'
        f'{_safe_key_part(chat_id)}/'
        f'{_safe_key_part(source_event_id)}/original.{ext}'
    )


def init_chat_media_table():
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute('''CREATE TABLE IF NOT EXISTS chat_media (
            id UUID PRIMARY KEY,
            user_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            media_kind TEXT NOT NULL DEFAULT 'image',
            object_key TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
            deleted_at TIMESTAMPTZ
        )''')
        cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_media_event
                       ON chat_media (user_id, chat_id, source_event_id, media_kind)''')
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print('[init] chat_media 表已就绪')


def _row_to_record(row):
    if not row:
        return None
    return {
        'id': str(row[0]),
        'user_id': row[1],
        'chat_id': row[2],
        'source_event_id': row[3],
        'media_kind': row[4] or 'image',
        'object_key': row[5],
        'mime_type': row[6],
        'size_bytes': int(row[7] or 0),
        'sha256': row[8],
        'created_at': row[9],
        'deleted_at': row[10] if len(row) > 10 else None,
    }


_SELECT_COLS = (
    'id, user_id, chat_id, source_event_id, media_kind, '
    'object_key, mime_type, size_bytes, sha256, created_at, deleted_at'
)


def _fetch_one(cur, user_id, chat_id, source_event_id, media_kind='image',
               active_only=True):
    sql = (
        f'SELECT {_SELECT_COLS} FROM chat_media '
        'WHERE user_id=%s AND chat_id=%s AND source_event_id=%s AND media_kind=%s'
    )
    params = [user_id, chat_id, source_event_id, media_kind]
    if active_only:
        sql += ' AND deleted_at IS NULL'
    cur.execute(sql, params)
    return _row_to_record(cur.fetchone())


def get_media_for_source_event(user_id, chat_id, source_event_id,
                               media_kind='image'):
    source_event_id = str(source_event_id or '').strip()
    if not user_id or not chat_id or not source_event_id:
        return None
    conn = get_conn()
    cur = conn.cursor()
    try:
        return _fetch_one(
            cur, user_id, chat_id, source_event_id, media_kind, active_only=True)
    finally:
        cur.close()
        conn.close()


def get_media_by_source_events(user_id, chat_id, source_event_ids,
                               media_kind='image'):
    ids = []
    seen = set()
    for item in source_event_ids or []:
        value = str(item or '').strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ids.append(value)
    if not user_id or not chat_id or not ids:
        return {}
    conn = get_conn()
    cur = conn.cursor()
    try:
        placeholders = ','.join(['%s'] * len(ids))
        cur.execute(
            f'''SELECT {_SELECT_COLS}
                FROM chat_media
                WHERE user_id=%s AND chat_id=%s AND media_kind=%s
                  AND source_event_id IN ({placeholders})
                  AND deleted_at IS NULL''',
            (user_id, chat_id, media_kind, *ids))
        out = {}
        for row in cur.fetchall():
            rec = _row_to_record(row)
            if rec:
                out[rec['source_event_id']] = rec
        return out
    finally:
        cur.close()
        conn.close()


def list_active_media(user_id, chat_id, media_kind=None):
    if not user_id or not chat_id:
        return []
    conn = get_conn()
    cur = conn.cursor()
    try:
        if media_kind:
            cur.execute(
                f'''SELECT {_SELECT_COLS}
                    FROM chat_media
                    WHERE user_id=%s AND chat_id=%s AND media_kind=%s
                      AND deleted_at IS NULL''',
                (user_id, chat_id, media_kind))
        else:
            cur.execute(
                f'''SELECT {_SELECT_COLS}
                    FROM chat_media
                    WHERE user_id=%s AND chat_id=%s
                      AND deleted_at IS NULL''',
                (user_id, chat_id))
        return [_row_to_record(row) for row in cur.fetchall() if row]
    finally:
        cur.close()
        conn.close()


def persist_image(user_id, chat_id, source_event_id, data_bytes,
                  mime_type='image/jpeg', media_kind='image'):
    """Idempotent persist. Same source_event_id reuses the existing object_key."""
    user_id = str(user_id or '').strip()
    chat_id = str(chat_id or '').strip()
    source_event_id = str(source_event_id or '').strip()
    media_kind = (media_kind or 'image').strip() or 'image'
    mime_type = mime_type or 'image/jpeg'
    if not user_id or not chat_id or not source_event_id:
        raise MediaPersistError('missing_association')
    if not isinstance(data_bytes, (bytes, bytearray)) or not data_bytes:
        raise MediaPersistError('empty_image_bytes')

    conn = get_conn()
    cur = conn.cursor()
    try:
        existing = _fetch_one(
            cur, user_id, chat_id, source_event_id, media_kind, active_only=True)
        if existing:
            return existing

        if not media_storage.is_configured():
            raise MediaPersistError('r2_not_configured')

        object_key = object_key_for(user_id, chat_id, source_event_id, mime_type)
        digest = hashlib.sha256(bytes(data_bytes)).hexdigest()
        size_bytes = len(data_bytes)
        media_id = str(uuid.uuid4())

        media_storage.put_bytes(object_key, bytes(data_bytes), mime_type)

        deleted = _fetch_one(
            cur, user_id, chat_id, source_event_id, media_kind, active_only=False)
        if deleted and deleted.get('deleted_at') is not None:
            cur.execute(
                '''UPDATE chat_media
                   SET deleted_at=NULL,
                       object_key=%s,
                       mime_type=%s,
                       size_bytes=%s,
                       sha256=%s
                   WHERE id=%s
                   RETURNING ''' + _SELECT_COLS,
                (object_key, mime_type, size_bytes, digest, deleted['id']))
            row = cur.fetchone()
            conn.commit()
            return _row_to_record(row)

        try:
            cur.execute(
                '''INSERT INTO chat_media
                     (id, user_id, chat_id, source_event_id, media_kind,
                      object_key, mime_type, size_bytes, sha256)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING ''' + _SELECT_COLS,
                (media_id, user_id, chat_id, source_event_id, media_kind,
                 object_key, mime_type, size_bytes, digest))
            row = cur.fetchone()
            conn.commit()
            if row:
                return _row_to_record(row)
        except Exception:
            conn.rollback()
            raced = _fetch_one(
                cur, user_id, chat_id, source_event_id, media_kind,
                active_only=True)
            if raced:
                return raced
            raise
        raced = _fetch_one(
            cur, user_id, chat_id, source_event_id, media_kind, active_only=True)
        if raced:
            return raced
        raise MediaPersistError('insert_failed')
    finally:
        cur.close()
        conn.close()


def soft_delete_media_for_event(user_id, chat_id, source_event_id,
                                media_kind=None):
    source_event_id = str(source_event_id or '').strip()
    if not user_id or not chat_id or not source_event_id:
        return []
    conn = get_conn()
    cur = conn.cursor()
    try:
        if media_kind:
            cur.execute(
                f'''UPDATE chat_media
                    SET deleted_at=CURRENT_TIMESTAMP
                    WHERE user_id=%s AND chat_id=%s AND source_event_id=%s
                      AND media_kind=%s AND deleted_at IS NULL
                    RETURNING {_SELECT_COLS}''',
                (user_id, chat_id, source_event_id, media_kind))
        else:
            cur.execute(
                f'''UPDATE chat_media
                    SET deleted_at=CURRENT_TIMESTAMP
                    WHERE user_id=%s AND chat_id=%s AND source_event_id=%s
                      AND deleted_at IS NULL
                    RETURNING {_SELECT_COLS}''',
                (user_id, chat_id, source_event_id))
        rows = [_row_to_record(row) for row in cur.fetchall() if row]
        conn.commit()
        return rows
    finally:
        cur.close()
        conn.close()


def soft_delete_media_for_chat(user_id, chat_id):
    if not user_id or not chat_id:
        return []
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            f'''UPDATE chat_media
                SET deleted_at=CURRENT_TIMESTAMP
                WHERE user_id=%s AND chat_id=%s AND deleted_at IS NULL
                RETURNING {_SELECT_COLS}''',
            (user_id, chat_id))
        rows = [_row_to_record(row) for row in cur.fetchall() if row]
        conn.commit()
        return rows
    finally:
        cur.close()
        conn.close()


def public_media(record):
    """API view with a fresh signed URL. Never write this URL to the database."""
    if not record:
        return None
    url = None
    try:
        url = media_storage.signed_get_url(record['object_key'])
    except Exception as e:
        print(f'[chat-media] signed url failed object_key={record.get("object_key")}:{e}')
    return {
        'id': str(record['id']),
        'kind': record.get('media_kind') or 'image',
        'url': url,
        'mime_type': record.get('mime_type') or 'image/jpeg',
    }
