"""Idempotent source-event ingress and deterministic Fast Loop maintenance."""
import json
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
