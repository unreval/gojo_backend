"""Idempotent source-event ingress and deterministic Fast Loop maintenance."""
import json
import hashlib
import re
import unicodedata
from datetime import datetime, timezone

from cognitive_predictions import settle_pending_predictions
from cognitive_reactivation import reactivate_dormant_questions
from cognitive_triggers import (
    create_trigger_occurrence,
    high_weight_trigger_specs,
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


def _embed_v4_evidence(signals):
    """Reuse the existing embedding path; failure never blocks event ingress."""
    factual_briefs = [
        str(signal.get('brief', '')).strip()
        for signal in signals or []
        if isinstance(signal, dict) and str(signal.get('brief', '')).strip()
    ]
    if not factual_briefs:
        return None
    try:
        from memory_search import embed
        return embed('\n'.join(factual_briefs))
    except Exception:
        return None


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
    """Apply extracted explicit evidence to the EXISTING question lifecycle.

    Extraction identifies the witnessed answer/promise; this deterministic
    ingress never chooses a relationship answer or infers one from prose.
    """
    update_type = update.get('type') or update.get('kind')
    if update_type not in {'pending_answer', 'resolution'}:
        raise ValueError('question_update_type_invalid')
    content = str(update.get('content') or '').strip()
    question_text = str(update.get('question_text') or '').strip()
    question_key = str(update.get('question_key') or '').strip()
    if not content or not (question_key or question_text):
        raise ValueError('question_update_requires_content_and_question')
    if update_type == 'resolution' and update.get('value') in (None, ''):
        raise ValueError('question_resolution_requires_value')
    evidence_ids, evidence, source_id = validate_question_operation_evidence(update, canonical_events)
    event_time = _utc_now(occurred_at)
    database = conn
    owns_connection = database is None
    if owns_connection:
        from db import get_conn
        database = get_conn()
    cur = database.cursor()
    try:
        import raw_events
        from cognitive_queue import aggregate_pending_triggers, stable_advisory_lock_key

        cur.execute('SELECT pg_advisory_xact_lock(%s)',
                    (stable_advisory_lock_key(user_id, character_id),))
        if not raw_events.sources_are_active(evidence_ids, user_id, character_id, conn=database):
            database.rollback()
            return {'status': 'skipped_deleted'}
        cur.execute(
            '''SELECT id, question_key, question_text, status, metadata, source_event_refs
               FROM cognitive_questions
               WHERE user_id = %s AND character_id = %s
                 AND (question_key = %s OR question_text = %s)
               ORDER BY (question_key = %s) DESC, updated_at DESC
               LIMIT 1 FOR UPDATE''',
            (user_id, character_id, question_key, question_text, question_key),
        )
        previous = cur.fetchone()
        if previous:
            question_key = previous[1]
            question_text = previous[2]
        elif not question_text:
            raise ValueError('question_update_unknown_key_without_question_text')
        if not question_key:
            question_key = 'explicit.' + hashlib.sha256(question_text.encode('utf-8')).hexdigest()[:24]
        metadata = dict(_question_json(previous[4], {}) if previous else {})
        # A postponed answer is not evidence that a prior explicit yes/no vanished.
        status = question_transition_status(
            previous[3] if previous else None,
            'active' if update_type == 'pending_answer' else 'resolved',
            metadata=metadata, operation=update_type)
        if update_type == 'pending_answer' and status == 'resolved':
            database.commit()
            return {'status': 'already_resolved', 'question_id': previous[0],
                    'question_status': 'resolved'}
        event_id = record_source_event(
            database, user_id=user_id, character_id=character_id,
            source_event_type=f'question_{update_type}:{question_key}',
            source_event_id=source_id, source='memory_extraction', occurred_at=event_time,
            payload={'question_key': question_key, 'question_text': question_text,
                     'type': update_type, 'content': content, 'value': update.get('value'),
                     'evidence_event_ids': evidence_ids,
                     'canonical_turns': [{'event_id': e['event_id'], 'role': e['role'],
                                          'content': e.get('content', '')} for e in evidence]},
        )
        if event_id is None:
            database.commit()
            return {'status': 'duplicate', 'question_id': previous[0] if previous else None}
        refs = list(_question_json(previous[5], []) if previous else [])
        refs.append({'event_id': event_id, 'source': 'memory_extraction', 'source_id': source_id})
        history = list(metadata.get('lifecycle_history') or [])
        history.append({'previous_status': previous[3] if previous else None,
                        'previous_resolution': metadata.get('resolution'),
                        'previous_judgment': metadata.get('current_judgment'),
                        'event_id': event_id, 'type': update_type, 'at': event_time.isoformat()})
        metadata['lifecycle_history'] = history
        metadata['updated_by'] = 'explicit_question_evidence'
        if update_type == 'pending_answer':
            metadata['pending_answer'] = {'status': 'pending', 'content': content,
                                         'promised_at': event_time.isoformat(),
                                         'evidence_event_ids': evidence_ids}
        else:
            metadata['resolution'] = {'value': update['value'], 'content': content,
                                      'actor': update.get('actor', 'character'),
                                      'evidence_event_ids': evidence_ids,
                                      'resolved_at': event_time.isoformat()}
            if (update.get('novel') is True and update.get('kind') in {
                    'explicit_acceptance', 'explicit_rejection', 'explicit_correction',
                    'explicit_decision', 'relationship_resolution',
                    'answer_to_unresolved_question'}):
                metadata['resolution']['bond_delta'] = {
                    'kind': update['kind'], 'content': content, 'value': update['value'],
                    'actor': update.get('actor', 'character'),
                    'question_key': question_key, 'question_text': question_text,
                    'replaces': [item for item in update.get('replaces', []) if isinstance(item, str)],
                    'evidence_quote': update['evidence_quote'],
                    'evidence_event_ids': evidence_ids, 'novel': True,
                }
            metadata.pop('current_judgment', None)
            if metadata.get('pending_answer'):
                metadata['pending_answer'] = dict(metadata['pending_answer'], status='fulfilled')
        cur.execute(
            '''INSERT INTO cognitive_questions (
                   user_id, character_id, question_key, question_text, status,
                   metadata, source_event_refs, updated_at
               ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s)
               ON CONFLICT (user_id, character_id, question_key) DO UPDATE
               SET status = EXCLUDED.status, metadata = EXCLUDED.metadata,
                   source_event_refs = EXCLUDED.source_event_refs,
                   updated_at = EXCLUDED.updated_at
               RETURNING id''',
            (user_id, character_id, question_key, question_text, status,
             json.dumps(metadata, ensure_ascii=False), json.dumps(refs, ensure_ascii=False), event_time),
        )
        question_id = cur.fetchone()[0]
        if status == 'resolved':
            cur.execute(
                '''UPDATE cognitive_beliefs
                   SET metadata = metadata || %s::jsonb, updated_at = %s
                   WHERE user_id = %s AND character_id = %s AND status = 'active'
                     AND (metadata->>'question_key' = %s
                          OR committed_from_hypothesis_id IN (
                              SELECT id FROM cognitive_hypotheses
                              WHERE user_id = %s AND character_id = %s
                                AND question_id = %s))''',
                (json.dumps({'review_status': 'under_review',
                             'review_reason': 'question_explicitly_resolved',
                             'resolution_event_id': event_id}),
                 event_time, user_id, character_id, question_key,
                 user_id, character_id, question_id),
            )
            cur.execute(
                '''UPDATE cognitive_hypotheses SET status = 'archived', updated_at = %s
                   WHERE user_id = %s AND character_id = %s AND question_id = %s
                     AND status IN ('open', 'supported')''',
                (event_time, user_id, character_id, question_id),
            )
            cur.execute(
                '''UPDATE cognitive_predictions
                   SET status = 'expired', settled_at = %s, settled_by_event_id = %s,
                       last_error_code = 'question_explicitly_resolved'
                   WHERE user_id = %s AND character_id = %s AND question_id = %s
                     AND status = 'pending' ''',
                (event_time, event_id, user_id, character_id, question_id),
            )
            cur.execute(
                '''UPDATE cognitive_sticky_notes
                   SET status = 'completed', completed_at = %s, updated_at = %s
                   WHERE user_id = %s AND character_id = %s AND status = 'active'
                     AND (note_key = %s OR metadata->>'question_key' = %s)''',
                (event_time, event_time, user_id, character_id, question_key, question_key),
            )
        trigger_id = create_trigger_occurrence(
            database, event_id=event_id, user_id=user_id, character_id=character_id,
            trigger_class='question_reactivation', occurrence_key=f'question:{question_id}',
            payload={'question_id': question_id, 'question_key': question_key,
                     'reason': update_type, 'relation': 'explicit_evidence'},
        )
        database.commit()
        cycle = aggregate_pending_triggers(user_id, character_id, conn=database) if aggregate else None
        print('[cognitive_trace] '
              f'question_id={question_id} trigger=question_reactivation '
              f'cycle_id={(cycle or {}).get("cycle_id")} '
              f'status={"open" if status == "active" else status}', flush=True)
        return {'status': 'inserted', 'question_id': question_id,
                'question_key': question_key, 'question_status': status,
                'event_id': event_id, 'trigger_id': trigger_id, 'cycle': cycle}
    except Exception:
        database.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            database.close()


def ingest_v4_signals(
    *,
    user_id,
    character_id,
    source_event_id,
    signals,
    occurred_at=None,
    embedding_json=None,
    conn=None,
    aggregate=True,
):
    """Ingest the existing relationship v4 signal list without reshaping it."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    event_time = _utc_now(occurred_at)
    try:
        if source_event_id:
            import raw_events
            try:
                if not raw_events.sources_are_active(
                        [source_event_id], user_id, character_id, conn=database):
                    if owns_connection:
                        database.rollback()
                    return {
                        'status': 'skipped_deleted',
                        'event_id': None,
                        'trigger_ids': [],
                        'settled_predictions': [],
                        'reactivated_questions': [],
                        'cycle': None,
                    }
            except raw_events.SourceValidityError:
                if owns_connection:
                    database.rollback()
                return {
                    'status': 'failed_source_validity',
                    'event_id': None,
                    'trigger_ids': [],
                    'settled_predictions': [],
                    'reactivated_questions': [],
                    'cycle': None,
                }
        event_id = record_source_event(
            database,
            user_id=user_id,
            character_id=character_id,
            source_event_type='relationship_v4_signal',
            source_event_id=source_event_id,
            source='relationship_engine_v4',
            occurred_at=event_time,
            payload={'signals': signals},
            embedding_json=embedding_json,
        )
        if event_id is None:
            database.commit()
            return {
                'status': 'duplicate',
                'event_id': None,
                'trigger_ids': [],
                'settled_predictions': [],
                'reactivated_questions': [],
                'cycle': None,
            }

        if embedding_json is None:
            embedding_json = _embed_v4_evidence(signals)
        if embedding_json is not None:
            cur = database.cursor()
            try:
                cur.execute(
                    '''UPDATE cognitive_events SET embedding_json = %s
                       WHERE id = %s''',
                    (_embedding_text(embedding_json), event_id),
                )
            finally:
                cur.close()

        trigger_ids = []
        for spec in high_weight_trigger_specs(signals):
            trigger_id = create_trigger_occurrence(
                database,
                event_id=event_id,
                user_id=user_id,
                character_id=character_id,
                **spec,
            )
            if trigger_id is not None:
                trigger_ids.append(trigger_id)

        settled = settle_pending_predictions(
            database,
            user_id=user_id,
            character_id=character_id,
            event_id=event_id,
            occurred_at=event_time,
        )
        reactivated = reactivate_dormant_questions(
            database,
            user_id=user_id,
            character_id=character_id,
            event_id=event_id,
            evidence_embedding=embedding_json,
        )
        database.commit()

        cycle = None
        if aggregate:
            from cognitive_queue import aggregate_pending_triggers
            cycle = aggregate_pending_triggers(
                user_id, character_id, conn=database,
            )
        return {
            'status': 'inserted',
            'event_id': event_id,
            'trigger_ids': trigger_ids,
            'settled_predictions': settled,
            'reactivated_questions': reactivated,
            'cycle': cycle,
        }
    except Exception:
        database.rollback()
        raise
    finally:
        if owns_connection:
            database.close()
