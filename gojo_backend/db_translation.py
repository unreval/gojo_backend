"""Display-only subtitle restoration or manual entry for a committed bubble.

Restore reads only the exact event's completed delivery; manual entry takes
Chinese text from the caller. Neither path generates a reply or invokes a
model. User-provided text is labeled and stays out of factual evidence.
"""
import json

from db import get_conn
from generation_contract import translation_missing
from utils import classify_reply_content, reply_message_rejection_reason


_ENDPOINT_PREFIX = {
    'chat_text': 'chat_reply',
    'chat_image': 'image_reply',
    'delayed_reply': 'delayed_reply',
}


def _valid_manual_translation(original_jp, zh):
    if (not isinstance(zh, str) or not zh.strip() or len(zh) > 4000
            or not any('\u3400' <= ch <= '\u9fff' for ch in zh)):
        return False
    return reply_message_rejection_reason({'jp': original_jp, 'zh': zh}) is None


def repair_subtitle(user_id, character_id, source_event_id, endpoint,
                    event_id, original_jp, zh):
    """Manually fill one bubble, preferring its existing successful subtitle."""
    return _write_subtitle(user_id, character_id, source_event_id, endpoint,
                           event_id, original_jp, zh)


def restore_subtitle(user_id, character_id, source_event_id, endpoint,
                     event_id, original_jp):
    """Restore a successful subtitle for the exact event and Japanese text."""
    return _write_subtitle(user_id, character_id, source_event_id, endpoint,
                           event_id, original_jp, None)


def _write_subtitle(user_id, character_id, source_event_id, endpoint,
                    event_id, original_jp, requested_zh):
    fields = (user_id, character_id, source_event_id, event_id, original_jp)
    if (not all(isinstance(value, str) and value.strip() for value in fields)
            or endpoint not in _ENDPOINT_PREFIX
            or classify_reply_content(original_jp) != 'text'):
        return {'status': 'invalid_translation'}
    user_id, character_id = user_id.strip(), character_id.strip()
    source_event_id, event_id = source_event_id.strip(), event_id.strip()
    if not event_id.startswith(f'{_ENDPOINT_PREFIX[endpoint]}:{source_event_id}:'):
        return {'status': 'identity_mismatch'}

    conn = get_conn()
    cur = conn.cursor()
    committed = False
    try:
        payload = None
        message = None
        proactive_id = None
        if endpoint == 'delayed_reply':
            cur.execute(
                '''SELECT id, jp, zh, translation_source FROM proactive_msg
                   WHERE user_id=%s AND character_id=%s
                     AND kind='delayed_reply' AND event_id=%s FOR UPDATE''',
                (user_id, character_id, event_id))
            proactive_rows = cur.fetchall()
            if len(proactive_rows) != 1:
                return {'status': 'delivery_unavailable'}
            proactive_id, saved_jp, stored_zh, saved_source = proactive_rows[0]
            if saved_jp != original_jp:
                return {'status': 'identity_mismatch'}
        else:
            cur.execute(
                '''SELECT status, response_json FROM chat_generation_receipt
                   WHERE user_id=%s AND character_id=%s AND source_event_id=%s
                     AND endpoint=%s FOR UPDATE''',
                (user_id, character_id, source_event_id, endpoint))
            receipt = cur.fetchone()
            if not receipt or receipt[0] != 'completed':
                return {'status': 'receipt_unavailable'}
            payload = receipt[1]
            if isinstance(payload, str):
                payload = json.loads(payload)
            if not isinstance(payload, dict):
                return {'status': 'receipt_unavailable'}
            messages = payload.get('messages')
            matches = [msg for msg in messages or []
                       if isinstance(msg, dict) and msg.get('event_id') == event_id]
            if len(matches) != 1 or matches[0].get('jp') != original_jp:
                return {'status': 'identity_mismatch'}
            message = matches[0]
            stored_zh = str(message.get('zh') or '')
            saved_source = message.get('translation_source')
        cur.execute(
            '''SELECT id, role, text, subtitle, extra,
                      COALESCE(status, 'active'),
                      EXISTS(SELECT 1 FROM chat_log_tombstone t
                             WHERE t.user_id=chat_log.user_id
                               AND t.chat_id=chat_log.chat_id
                               AND t.client_msg_id=chat_log.client_msg_id)
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(NULLIF(event_id, ''), client_msg_id)=%s
               FOR UPDATE''',
            (user_id, character_id, event_id))
        row = cur.fetchone()
        if not row:
            return {'status': 'message_not_synced'}
        row_id, role, text, subtitle, extra, status, tombstoned = row
        if status != 'active' or tombstoned:
            return {'status': 'deleted_rejected'}
        if role not in ('gojo', 'assistant') or text != original_jp:
            return {'status': 'identity_mismatch'}
        if stored_zh:
            zh = stored_zh
            provenance = saved_source or (
                'delivery' if endpoint == 'delayed_reply' else 'receipt')
        elif requested_zh is None:
            return {'status': 'translation_unavailable'}
        elif _valid_manual_translation(original_jp, requested_zh):
            zh = requested_zh.strip()
            provenance = 'user_supplied'
        else:
            return {'status': 'invalid_translation'}
        if subtitle and subtitle != zh:
            return {'status': 'translation_conflict'}
        try:
            metadata = json.loads(extra) if extra else {}
        except (TypeError, ValueError):
            return {'status': 'invalid_existing_metadata'}
        if not isinstance(metadata, dict):
            return {'status': 'invalid_existing_metadata'}
        if stored_zh and subtitle:
            return {'status': 'already_identical', 'event_id': event_id,
                    'zh': zh, 'translation_source': provenance,
                    'translation_missing': False}

        if not subtitle or not stored_zh:
            metadata['translation_source'] = provenance
            cur.execute(
                '''UPDATE chat_log SET subtitle=%s, extra=%s
                   WHERE id=%s AND user_id=%s AND chat_id=%s
                     AND COALESCE(status, 'active')='active'
                     AND text=%s AND COALESCE(subtitle, '') IN ('', %s)''',
                (zh, json.dumps(metadata, ensure_ascii=False), row_id,
                 user_id, character_id, original_jp, zh))
            if cur.rowcount != 1:
                return {'status': 'translation_conflict'}
        if not stored_zh:
            if endpoint == 'delayed_reply':
                cur.execute(
                    '''UPDATE proactive_msg SET zh=%s, translation_source=%s
                       WHERE id=%s AND user_id=%s AND character_id=%s
                         AND event_id=%s AND jp=%s AND COALESCE(zh, '')='' ''',
                    (zh, provenance, proactive_id, user_id, character_id,
                     event_id, original_jp))
                if cur.rowcount != 1:
                    return {'status': 'translation_conflict'}
            else:
                message['zh'] = zh
                message['translation_source'] = provenance
                payload['translation_missing'] = any(
                    translation_missing(msg) for msg in messages)
                cur.execute(
                    '''UPDATE chat_generation_receipt SET response_json=%s::jsonb
                       WHERE user_id=%s AND character_id=%s AND source_event_id=%s
                         AND endpoint=%s AND status='completed' ''',
                    (json.dumps(payload, ensure_ascii=False), user_id,
                     character_id, source_event_id, endpoint))
                if cur.rowcount != 1:
                    return {'status': 'receipt_unavailable'}
        conn.commit()
        committed = True
        return {'status': 'repaired', 'event_id': event_id, 'zh': zh,
                'translation_source': provenance,
                'translation_missing': any(
                    translation_missing(msg) for msg in payload.get('messages') or [])
                if payload is not None else False}
    finally:
        if not committed:
            conn.rollback()
        cur.close()
        conn.close()
