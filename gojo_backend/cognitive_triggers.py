"""Trigger occurrence creation and v4 signal classification."""
import json

from cognitive_config import COGNITIVE_HIGH_WEIGHT_THRESHOLD, TRIGGER_PRIORITIES


TRIGGER_CLASSES = frozenset(TRIGGER_PRIORITIES)
TRIGGER_STATUSES = frozenset({
    'pending', 'claimed', 'consumed', 'suppressed', 'dead_letter',
})


def trigger_priority(trigger_class):
    try:
        return TRIGGER_PRIORITIES[trigger_class]
    except KeyError as exc:
        raise ValueError(f'unsupported trigger class: {trigger_class}') from exc


def create_trigger_occurrence(
    conn,
    *,
    event_id,
    user_id,
    character_id,
    trigger_class,
    occurrence_key='default',
    payload=None,
    status='pending',
    suppressed_reason=None,
    suppressed_at=None,
):
    """Create one occurrence; an event may own many distinct occurrences."""
    if status not in TRIGGER_STATUSES:
        raise ValueError(f'unsupported trigger status: {status}')
    priority = trigger_priority(trigger_class)
    if status == 'suppressed' and not suppressed_reason:
        raise ValueError('suppressed triggers require a reason')
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO cognitive_event_triggers (
                   event_id, user_id, character_id, trigger_class,
                   occurrence_key, priority, payload, status,
                   slow_cycle_suppressed_reason, suppressed_at
               ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
               ON CONFLICT (event_id, trigger_class, occurrence_key) DO NOTHING
               RETURNING id''',
            (
                event_id, user_id, character_id, trigger_class,
                str(occurrence_key), priority,
                json.dumps(payload or {}, ensure_ascii=False), status,
                suppressed_reason, suppressed_at,
            ),
        )
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        cur.close()


def high_weight_trigger_specs(signals, threshold=None):
    """Classify the existing v4 signal dictionaries without inventing a schema."""
    from relationship_config import CONFIDENCE_MULTIPLIER

    cutoff = (
        COGNITIVE_HIGH_WEIGHT_THRESHOLD if threshold is None else float(threshold)
    )
    specs = []
    for index, signal in enumerate(signals or []):
        if not isinstance(signal, dict):
            continue
        weight = float(CONFIDENCE_MULTIPLIER.get(signal.get('confidence'), 0.0))
        if weight < cutoff:
            continue
        specs.append({
            'trigger_class': 'high_weight_evidence',
            'occurrence_key': f'signal:{index}',
            'payload': {
                'signal_index': index,
                'confidence_weight': weight,
                'signal': signal,
            },
        })
    return specs
