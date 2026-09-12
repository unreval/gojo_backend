"""Offline, zero-model replay and parameter sweep for Cognitive Loop v1.1."""
import argparse
import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone

from cognitive_config import (
    COGNITIVE_COOLDOWN_SECONDS,
    COGNITIVE_DAILY_CYCLE_LIMIT,
    COGNITIVE_ESTIMATED_TOKENS_PER_SLOW_CYCLE,
    COGNITIVE_HIGH_WEIGHT_THRESHOLD,
    COGNITIVE_MAX_QUESTIONS_PER_CYCLE,
    COGNITIVE_MAX_TRIGGERS_PER_CYCLE,
    COGNITIVE_REACTIVATION_COSINE_THRESHOLD,
    COGNITIVE_REFLECTION_SUPPRESSION_SECONDS,
    REPLAY_COOLDOWN_MINUTES,
    REPLAY_HIGH_WEIGHT_THRESHOLDS,
    REPLAY_REACTIVATION_THRESHOLDS,
    TRIGGER_PRIORITIES,
)
from cognitive_reactivation import rank_dormant_questions
from relationship_config import CONFIDENCE_MULTIPLIER


def _as_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def _distribution(values):
    values = list(values)
    return {
        'mean': (sum(values) / len(values)) if values else 0.0,
        'p50': _percentile(values, 0.50),
        'p90': _percentile(values, 0.90),
        'p95': _percentile(values, 0.95),
        'p99': _percentile(values, 0.99),
        'max': max(values) if values else 0.0,
    }


def load_historical_records(*, user_id=None, character_id=None, conn=None):
    """Read real v4 provenance and interaction timing without model calls."""
    database = conn
    owns_connection = database is None
    if database is None:
        from db import get_conn
        database = get_conn()
    filters = []
    params = []
    if user_id:
        filters.append('user_id = %s')
        params.append(user_id)
    if character_id:
        filters.append('character_id = %s')
        params.append(character_id)
    where = (' WHERE ' + ' AND '.join(filters)) if filters else ''
    records = []
    cur = database.cursor()
    try:
        cur.execute(
            '''SELECT id, user_id, character_id, signal_type, confidence,
                      evidence_refs, note, timestamp
               FROM rel_provenance_log''' + where + '''
               ORDER BY timestamp, id''',
            tuple(params),
        )
        for row in cur.fetchall():
            records.append({
                'source_event_id': f'rel_provenance:{row[0]}',
                'user_id': row[1],
                'character_id': row[2],
                'source_event_type': 'relationship_v4_signal',
                'signal_type': row[3],
                'confidence': row[4],
                'evidence_refs': row[5],
                'brief': row[6] or '',
                'occurred_at': row[7],
            })

        cur.execute(
            '''SELECT id, user_id, character_id, direction, tone_category,
                      is_reciprocal, is_initiator, timestamp
               FROM rel_interaction_stats''' + where + '''
               ORDER BY timestamp, id''',
            tuple(params),
        )
        for row in cur.fetchall():
            records.append({
                'source_event_id': f'rel_interaction:{row[0]}',
                'user_id': row[1],
                'character_id': row[2],
                'source_event_type': 'interaction_timing',
                'direction': row[3],
                'tone_category': row[4],
                'is_reciprocal': row[5],
                'is_initiator': row[6],
                'occurred_at': row[7],
            })
    finally:
        cur.close()
        if owns_connection:
            database.close()
    records.sort(key=lambda item: _as_utc(item['occurred_at']))
    return records


def _trigger_classes(record, high_weight_threshold, reactivation_threshold):
    classes = list(record.get('trigger_classes') or [])
    if record.get('prediction_status') == 'violated':
        classes.append('prediction_error')
    similarity = record.get('reactivation_similarity')
    if similarity is not None and float(similarity) >= reactivation_threshold:
        classes.append('question_reactivation')
    if record.get('source_event_type') == 'scheduled_reflection':
        classes.append('scheduled_reflection')
    confidence = record.get('confidence')
    weight = CONFIDENCE_MULTIPLIER.get(confidence, 0.0)
    if (
        record.get('source_event_type') == 'relationship_v4_signal'
        and weight >= high_weight_threshold
    ):
        classes.append('high_weight_evidence')
    return classes


def run_replay(
    records,
    *,
    cooldown_minutes=COGNITIVE_COOLDOWN_SECONDS / 60,
    high_weight_threshold=COGNITIVE_HIGH_WEIGHT_THRESHOLD,
    reactivation_threshold=COGNITIVE_REACTIVATION_COSINE_THRESHOLD,
    daily_cycle_limit=COGNITIVE_DAILY_CYCLE_LIMIT,
    max_triggers_per_cycle=COGNITIVE_MAX_TRIGGERS_PER_CYCLE,
    estimated_tokens_per_cycle=COGNITIVE_ESTIMATED_TOKENS_PER_SLOW_CYCLE,
):
    """Replay deterministic scheduling. No embeddings or model APIs are called."""
    ordered = sorted(records, key=lambda item: _as_utc(item['occurred_at']))
    event_counts = Counter()
    trigger_counts = Counter()
    trigger_class_counts = Counter({name: 0 for name in TRIGGER_PRIORITIES})
    cycle_counts = Counter()
    daily_limit_hits = 0
    slow_cycle_opportunities = 0
    scheduled_total = 0
    scheduled_suppressed = 0
    events_per_cycle = []
    trigger_delays = []
    pending = defaultdict(list)
    last_cycle_at = {}

    for event_index, record in enumerate(ordered):
        occurred_at = _as_utc(record['occurred_at'])
        pair = (str(record['user_id']), str(record['character_id']))
        day_key = (pair[0], pair[1], occurred_at.date().isoformat())
        event_counts[day_key] += 1
        classes = _trigger_classes(
            record, high_weight_threshold, reactivation_threshold,
        )
        event_identity = record.get('source_event_id', f'replay:{event_index}')
        for trigger_class in classes:
            trigger_counts[day_key] += 1
            trigger_class_counts[trigger_class] += 1
            if trigger_class == 'scheduled_reflection':
                scheduled_total += 1
                previous = last_cycle_at.get(pair)
                if previous is not None and (
                    occurred_at - previous
                ).total_seconds() < COGNITIVE_REFLECTION_SUPPRESSION_SECONDS:
                    scheduled_suppressed += 1
                    continue
            pending[pair].append({
                'trigger_class': trigger_class,
                'occurred_at': occurred_at,
                'event_identity': event_identity,
            })

        if not pending[pair]:
            continue
        slow_cycle_opportunities += 1
        if cycle_counts[day_key] >= daily_cycle_limit:
            daily_limit_hits += 1
            pending[pair].clear()
            continue
        previous_cycle = last_cycle_at.get(pair)
        cooldown_ready = (
            previous_cycle is None
            or (occurred_at - previous_cycle).total_seconds()
            >= cooldown_minutes * 60
        )
        if not cooldown_ready:
            continue

        selected = pending[pair][:max_triggers_per_cycle]
        del pending[pair][:len(selected)]
        cycle_counts[day_key] += 1
        last_cycle_at[pair] = occurred_at
        events_per_cycle.append(len({item['event_identity'] for item in selected}))
        trigger_delays.extend(
            (occurred_at - item['occurred_at']).total_seconds()
            for item in selected
        )

    daily_keys = set(event_counts) | set(trigger_counts) | set(cycle_counts)
    event_distribution = _distribution(event_counts[key] for key in daily_keys)
    trigger_distribution = _distribution(trigger_counts[key] for key in daily_keys)
    cycle_distribution = _distribution(cycle_counts[key] for key in daily_keys)
    token_values = [
        cycle_counts[key] * estimated_tokens_per_cycle for key in daily_keys
    ]
    user_cycle_totals = Counter()
    user_active_days = defaultdict(set)
    for (user_id, _character_id, day), count in cycle_counts.items():
        user_cycle_totals[user_id] += count
        user_active_days[user_id].add(day)
    user_cycle_frequency = [
        total / max(1, len(user_active_days[user_id]))
        for user_id, total in user_cycle_totals.items()
    ]
    top_count = (
        max(1, math.ceil(len(user_cycle_frequency) * 0.01))
        if user_cycle_frequency else 0
    )
    burst_values = sorted(user_cycle_frequency, reverse=True)[:top_count]
    consumed_trigger_count = len(trigger_delays)
    cycle_total = sum(cycle_counts.values())

    return {
        'events_per_day_user': event_distribution,
        'trigger_occurrences_per_day_user': trigger_distribution,
        'trigger_occurrences_by_class': dict(sorted(trigger_class_counts.items())),
        'cycles_per_day_user': cycle_distribution,
        'trigger_to_cycle_aggregation_ratio': (
            consumed_trigger_count / cycle_total if cycle_total else 0.0
        ),
        'events_per_cycle': {
            'p50': _percentile(events_per_cycle, 0.50),
            'p90': _percentile(events_per_cycle, 0.90),
            'p95': _percentile(events_per_cycle, 0.95),
        },
        'trigger_to_cycle_delay_seconds': {
            'p50': _percentile(trigger_delays, 0.50),
            'p95': _percentile(trigger_delays, 0.95),
        },
        'daily_limit_hit_rate': (
            daily_limit_hits / slow_cycle_opportunities
            if slow_cycle_opportunities else 0.0
        ),
        'scheduled_suppression_rate': (
            scheduled_suppressed / scheduled_total if scheduled_total else 0.0
        ),
        'top_1_percent_burst_users_cycle_frequency': (
            sum(burst_values) / len(burst_values) if burst_values else 0.0
        ),
        'estimated_future_cognitive_tokens': {
            'p50': _percentile(token_values, 0.50),
            'p95': _percentile(token_values, 0.95),
            'p99': _percentile(token_values, 0.99),
            'burst': max(token_values) if token_values else 0.0,
        },
        'totals': {
            'events': sum(event_counts.values()),
            'trigger_occurrences': sum(trigger_counts.values()),
            'cycles': cycle_total,
            'daily_limit_hits': daily_limit_hits,
            'scheduled_suppressed': scheduled_suppressed,
        },
    }


def benchmark_question_reactivation(
    fixtures,
    *,
    threshold=COGNITIVE_REACTIVATION_COSINE_THRESHOLD,
    top_n=COGNITIVE_MAX_QUESTIONS_PER_CYCLE,
):
    """Evaluate human-labelled question fixtures without generating embeddings."""
    case_results = []
    for fixture in fixtures:
        ranked = rank_dormant_questions(
            fixture['evidence_embedding'],
            fixture['questions'],
            threshold=threshold,
            top_n=top_n,
        )
        predicted = {item['question_id'] for item in ranked}
        expected = set(fixture.get('expected_question_ids', []))
        true_positive = len(predicted & expected)
        case_results.append({
            'fixture_id': fixture.get('fixture_id'),
            'precision': true_positive / len(predicted) if predicted else 0.0,
            'recall': true_positive / len(expected) if expected else 0.0,
            'ranked_question_ids': [item['question_id'] for item in ranked],
        })
    return case_results


def run_parameter_sweep(records):
    """Return all 5 x 4 x 5 deterministic replay combinations."""
    results = []
    for cooldown_minutes in REPLAY_COOLDOWN_MINUTES:
        for high_weight_threshold in REPLAY_HIGH_WEIGHT_THRESHOLDS:
            for reactivation_threshold in REPLAY_REACTIVATION_THRESHOLDS:
                results.append({
                    'parameters': {
                        'cooldown_minutes': cooldown_minutes,
                        'high_weight_threshold': high_weight_threshold,
                        'reactivation_threshold': reactivation_threshold,
                    },
                    'metrics': run_replay(
                        records,
                        cooldown_minutes=cooldown_minutes,
                        high_weight_threshold=high_weight_threshold,
                        reactivation_threshold=reactivation_threshold,
                    ),
                })
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Offline Cognitive Loop v1.1 deterministic replay',
    )
    parser.add_argument('--user-id')
    parser.add_argument('--character-id')
    parser.add_argument('--sweep', action='store_true')
    args = parser.parse_args(argv)
    records = load_historical_records(
        user_id=args.user_id,
        character_id=args.character_id,
    )
    output = run_parameter_sweep(records) if args.sweep else run_replay(records)
    print(json.dumps(output, ensure_ascii=False, indent=2, default=str))


if __name__ == '__main__':
    main()
