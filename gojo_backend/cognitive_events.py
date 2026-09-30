"""Idempotent source-event ingress and deterministic Fast Loop maintenance."""
import json
import hashlib
import re
import unicodedata
from datetime import datetime, timezone

from cognitive_triggers import (
    create_trigger_occurrence,
)
from relationship_semantics import (
    ROMANTIC_LABEL_KEY,
    is_nonrelationship_generated_source,
    normalize_romantic_label,
)


def _utc_now(value=None):
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        return result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _embedding_text(embedding):
    if embedding is None or isinstance(embedding, str):
        return embedding
    return json.dumps(embedding)


def record_source_event(
    conn,
    *,
    user_id,
    character_id,
    source_event_type,
    source_event_id,
    source,
    occurred_at,
    payload=None,
    embedding_json=None,
):
    """Return the new event id, or None when this source event already exists."""
    if not all((user_id, character_id, source_event_type, source_event_id, source)):
        raise ValueError('source event identity fields must be non-empty')
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO cognitive_events (
                   user_id, character_id, source_event_type, source_event_id,
                   source, occurred_at, payload, embedding_json
               ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
               ON CONFLICT (
                   user_id, character_id, source_event_type, source_event_id
               ) DO NOTHING
               RETURNING id''',
            (
                user_id, character_id, source_event_type, source_event_id,
                source, _utc_now(occurred_at),
                json.dumps(payload or {}, ensure_ascii=False),
                _embedding_text(embedding_json),
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def question_extraction_state(user_id, character_id, *, conn=None):
    """Bounded existing identities for extraction, not a second reasoning path."""
    database = conn
    owns_connection = database is None
    if owns_connection:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT question_key, question_text, status, metadata
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
                 AND status IN ('active', 'dormant', 'resolved')
               ORDER BY (status IN ('active', 'dormant')) DESC,
                        updated_at DESC, id DESC
               LIMIT 16''',
            (user_id, character_id),
        )
        return [
            {'question_key': row[0], 'question_text': row[1],
             'status': row[2], 'metadata': _question_json(row[3], {})}
            for row in cur.fetchall()
        ]
    finally:
        cur.close()
        if owns_connection:
            database.close()


def _question_json(value, fallback):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return fallback
    return value if value is not None else fallback


def _validated_question_evidence(update, canonical_events):
    """Only quoted, cited canonical turns may change a question's lifecycle."""
    ids = update.get('evidence_event_ids')
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        raise ValueError('question_update_requires_evidence_event_ids')
    ids = list(dict.fromkeys(ids))
    by_id = {str(event.get('event_id')): event for event in canonical_events or []
             if isinstance(event, dict)}
    if any(event_id not in by_id for event_id in ids):
        raise ValueError('question_update_evidence_not_canonical')
    selected = [by_id[event_id] for event_id in ids]
    user_ids = {str(event['event_id']) for event in selected if event.get('role') == 'user'}
    if not user_ids:
        raise ValueError('question_update_requires_user_turn')
    quote = str(update.get('evidence_quote') or '').strip()
    if not quote or not any(quote in str(event.get('content') or '') for event in selected):
        raise ValueError('question_update_quote_not_in_evidence')
    for event in selected:
        if event.get('role') == 'assistant':
            extra = _question_json(event.get('extra'), {})
            meta = _question_json(event.get('metadata'), {})
            reply_to = (event.get('reply_to_event_id') or extra.get('reply_to_event_id')
                        or meta.get('reply_to_event_id'))
            if reply_to not in user_ids:
                raise ValueError('question_update_assistant_not_reply_to_evidence')
        elif event.get('role') != 'user':
            raise ValueError('question_update_evidence_role_invalid')
    return ids, selected, next(event_id for event_id in ids if event_id in user_ids)


def question_transition_status(previous_status, requested_status, *, metadata=None,
                               operation='update'):
    """Single gate for both writers. No public reopen protocol exists yet."""
    if operation not in {'update', 'pending_answer', 'resolution'}:
        raise ValueError('question_transition_operation_unsupported')
    if requested_status not in {'active', 'dormant', 'resolved', 'archived'}:
        raise ValueError('question_transition_status_invalid')
    metadata = metadata or {}
    if previous_status == 'resolved' or metadata.get('resolution') is not None:
        return 'resolved'
    # A completed answer takes precedence over an older promise to answer.
    if requested_status == 'resolved':
        return 'resolved'
    if (operation == 'pending_answer'
            or (metadata.get('pending_answer') or {}).get('status') == 'pending'):
        return 'active'
    return requested_status


QUESTION_DELTA_KINDS = frozenset({
    'explicit_acceptance', 'explicit_rejection', 'explicit_correction',
    'explicit_decision', 'explicit_promise', 'relationship_resolution',
    'answer_to_unresolved_question',
})
_LITERAL_POLARITY = {
    'yes': 'yes', 'no': 'no', 'はい': 'yes', 'いいえ': 'no',
    'i accept': 'yes', 'i agree': 'yes', 'i do not accept': 'no',
    "i don't accept": 'no', 'i disagree': 'no',
    '接受': 'yes', '不接受': 'no', '是': 'yes', '不是': 'no',
    '要': 'yes', '不要': 'no', '愿意': 'yes', '不愿意': 'no',
    '可以': 'yes', '不可以': 'no', '同意': 'yes', '不同意': 'no',
}


def _answer_polarity(text, *, subject='我'):
    """Closed grammar of explicit answer fragments, not a sentiment parser.

    Every fragment must be an unambiguous assertion of the same polarity.
    Quoted/reported, conditional, mixed and otherwise unknown prose fails closed.
    """
    parts = [part.strip() for part in re.split(r'[,，。.!！;；\n]+',
             unicodedata.normalize('NFKC', str(text or '')).casefold()) if part.strip()]
    values = set()
    for part in parts:
        value = _LITERAL_POLARITY.get(part)
        if value is None:
            compact = re.sub(r'\s+', '', part)
            match = re.fullmatch(
                rf'(?:{re.escape(subject)})?(?:明确|确实)?(?:说了)?'
                r'(不接受|接受|不是|是|不要|要|不愿意|愿意|不可以|可以|不同意|同意|yes|no)'
                r'(.*)', compact)
            if not match:
                return None
            token, tail = match.groups()
            if tail:
                if token in {'是', '不是'}:
                    if tail != '的':
                        return None
                elif (not re.match(r'^(你|她|我|这|那|被)', tail)
                      or re.search(r'不|没|未|否|别|无|如果|除非|但是|可是|也许|可能|才怪|反话|玩笑|只要|假如|拒绝|吗|呢|[?？]', tail)):
                    return None
            value = _LITERAL_POLARITY[token]
        values.add(value)
    return next(iter(values)) if len(values) == 1 else None


def validate_question_operation_evidence(update, canonical_events):
    """Shared operation/actor/linkage/polarity gate for extraction and ingress."""
    ids, selected, source_id = _validated_question_evidence(update, canonical_events)
    kind = update.get('kind')
    operation = update.get('type') or ('pending_answer' if kind == 'explicit_promise' else 'resolution')
    if (operation not in {'pending_answer', 'resolution'}
            or (kind is not None and kind not in QUESTION_DELTA_KINDS)
            or not (update.get('question_key') or update.get('question_text'))
            or not update.get('content')):
        raise ValueError('question_operation_schema_invalid')
    actor = update.get('actor', 'character')
    if actor not in {'character', 'user'}:
        raise ValueError('question_operation_actor_invalid')
    role = 'assistant' if actor == 'character' else 'user'
    quote = str(update.get('evidence_quote') or '').strip()
    speakers = [e for e in selected if e.get('role') == role and quote in str(e.get('content') or '')]
    if not speakers:
        raise ValueError('question_operation_actor_evidence_mismatch')
    value = str(update.get('value') or '').strip().casefold()
    if operation == 'pending_answer':
        if kind not in (None, 'explicit_promise') or value:
            raise ValueError('pending_answer_cannot_contain_resolution')
        # Only an actually witnessed future-answer promise, not a quoted keyword.
        def promise(text):
            return (re.search(r'明天|稍后|之后|待会|晚点|以后|tomorrow|later|明日|後で|あとで|後ほど', text, re.I)
                    and re.search(r'回答|答复|答案|answer|答え|返事', text, re.I)
                    and not re.search(r'不|没|别|如果|也许|可能|ない|ません|もし|かも|[?？]|\b(?:not|never|if)\b', text, re.I))
        if (not promise(quote) or not promise(str(update['content']))
                or not any(quote.strip('。.!！') == str(e['content']).strip().strip('。.!！')
                           for e in speakers)):
            raise ValueError('pending_answer_evidence_ambiguous')
    elif value in {'yes', 'no'}:
        expected_kind = 'explicit_acceptance' if value == 'yes' else 'explicit_rejection'
        if (kind not in (None, expected_kind, 'explicit_decision', 'explicit_correction',
                         'relationship_resolution', 'answer_to_unresolved_question')
                or _answer_polarity(quote) != value
                or _answer_polarity(update['content'], subject='她' if actor == 'user' else '我') != value
                or not all(_answer_polarity(e['content']) == value for e in speakers)):
            raise ValueError('question_answer_polarity_mismatch_or_ambiguous')
    elif (kind not in {'explicit_decision', 'explicit_correction'} or not value
          or value != quote.casefold() or str(update['content']).strip() != quote
          or not all(str(e['content']).strip() == quote for e in speakers)):
        raise ValueError('question_resolution_requires_explicit_answer')
    return ids, selected, source_id


def ingest_question_update(
    *, user_id, character_id, update, canonical_events, occurred_at=None,
    conn=None, aggregate=True,
):
    """Compatibility ingress: ignore extracted judgments, reload canonical text."""
    results = [ingest_canonical_turn(user_id=user_id, character_id=character_id,
                source_event_id=e['event_id'], conn=conn, aggregate=aggregate)
               for e in canonical_events if e.get('role') == 'user' and e.get('event_id')]
    return results[-1] if results else {'status': 'pending_canonical_source'}


def ingest_v4_signals(*, user_id, character_id, source_event_id, signals,
                      occurred_at=None, embedding_json=None, conn=None, aggregate=True):
    """Legacy bridge: model labels/embeddings never become evidence authority."""
    return ingest_canonical_turn(user_id=user_id, character_id=character_id,
                                 source_event_id=source_event_id, conn=conn, aggregate=aggregate)


def ingest_canonical_turn(*, user_id, character_id, source_event_id, conn=None, aggregate=True,
                          source_chat_id=None, allow_assistant=False):
    """Use the existing Evidence Store / trigger queue, without any model call."""
    from raw_events import get_active_events_by_ids, cognitive_source_matches_scope
    from cognitive_revision import parse_cognitive_evidence, evidence_question_key
    from cognitive_queue import aggregate_pending_triggers, stable_advisory_lock_key

    group_candidate = (str(source_chat_id or '').startswith('group:')
                       and str(source_event_id or '').startswith(str(source_chat_id) + ':message:'))
    if not source_event_id or (is_nonrelationship_generated_source(source_event_id)
                               and not group_candidate and not allow_assistant):
        return {'status': 'pending_canonical_source'}
    database = conn
    owns_connection = database is None
    if owns_connection:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        cur.execute('SELECT pg_advisory_xact_lock(%s)',
                    (stable_advisory_lock_key(user_id, character_id),))
        chat_id = source_chat_id or character_id
        events = get_active_events_by_ids(user_id, chat_id, [source_event_id], conn=database, lock=True)
        allowed_roles = {'user', 'assistant'} if allow_assistant else {'user'}
        if len(events) != 1 or events[0]['role'] not in allowed_roles:
            return {'status': 'pending_canonical_source'}
        raw = events[0]
        if (raw['role'] == 'user' and is_nonrelationship_generated_source(source_event_id)
                and not group_candidate):
            return {'status': 'pending_canonical_source'}
        if not cognitive_source_matches_scope(raw, character_id, chat_id):
            return {'status': 'pending_canonical_source'}
        parsed = (parse_cognitive_evidence(raw['content'], user_id, character_id)
                  if raw['role'] == 'user' else {'operation': 'quote'})
        qkey = evidence_question_key(parsed, source_event_id)
        event_id = record_source_event(database, user_id=user_id, character_id=character_id,
            source_event_type='canonical_user_turn' if raw['role'] == 'user' else 'canonical_assistant_turn',
            source_event_id=source_event_id,
            source='deterministic_evidence_policy_v1', occurred_at=raw['timestamp'],
            payload={'content': raw['content'], 'source_chat_id': chat_id, **parsed})
        if event_id is None:
            # A duplicate source is not proof of a recovered historical judgment.
            # Report only a matching canonical decision from a committed cycle.
            cur.execute("""SELECT e.adjudication, e.source, e.payload->>'content', c.status
                           FROM cognitive_events e
                           LEFT JOIN cognitive_cycles c
                             ON c.id::text=e.adjudication->>'processed_by_cycle_id'
                            AND c.user_id=e.user_id AND c.character_id=e.character_id
                           WHERE e.user_id=%s AND e.character_id=%s
                             AND e.source_event_id=%s AND e.source_event_type=%s""",
                        (user_id, character_id, source_event_id,
                         'canonical_user_turn' if raw['role'] == 'user' else 'canonical_assistant_turn'))
            row = cur.fetchone()
            decision = _question_json(row[0], {}) if row else {}
            reason = 'historical_adjudication_missing'
            if row and row[1] == 'deterministic_evidence_policy_v1' and row[2] == raw['content']:
                if decision.get('status') == 'superseded':
                    reason = 'historical_adjudication_superseded'
                elif decision.get('status') == 'applied' and row[3] == 'succeeded':
                    reason = 'canonical_judgment_already_committed'
            if owns_connection:
                database.commit()
            return {'status': 'duplicate', 'event_id': None, 'reason': reason}
        trigger = create_trigger_occurrence(database, event_id=event_id, user_id=user_id,
            character_id=character_id, trigger_class='question_reactivation',
            occurrence_key=qkey, payload={'question_key': qkey, 'reason': 'new_canonical_evidence'})
        if owns_connection:
            database.commit()
        cycle = aggregate_pending_triggers(user_id, character_id, conn=database) if aggregate else None
        return {'status': 'inserted', 'event_id': event_id, 'trigger_id': trigger,
                'question_key': qkey, 'cycle': cycle}
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()
