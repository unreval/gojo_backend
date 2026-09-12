"""Central configuration for Cognitive Loop v1.1 deterministic maintenance."""
import os


def _int_env(name, default, minimum=0):
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _float_env(name, default, minimum=0.0, maximum=1.0):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, value))


COGNITIVE_COOLDOWN_SECONDS = _int_env('COGNITIVE_COOLDOWN_SECONDS', 30 * 60)
COGNITIVE_DAILY_CYCLE_LIMIT = _int_env('COGNITIVE_DAILY_CYCLE_LIMIT', 8, 1)
COGNITIVE_CLAIM_LEASE_SECONDS = _int_env('COGNITIVE_CLAIM_LEASE_SECONDS', 5 * 60, 1)
COGNITIVE_MAX_RETRY = _int_env('COGNITIVE_MAX_RETRY', 3, 1)
COGNITIVE_MAX_TRIGGERS_PER_CYCLE = _int_env(
    'COGNITIVE_MAX_TRIGGERS_PER_CYCLE', 20, 1,
)
COGNITIVE_MAX_QUESTIONS_PER_CYCLE = _int_env(
    'COGNITIVE_MAX_QUESTIONS_PER_CYCLE', 5, 1,
)
COGNITIVE_HIGH_WEIGHT_THRESHOLD = _float_env(
    'COGNITIVE_HIGH_WEIGHT_THRESHOLD', 0.8,
)
COGNITIVE_REACTIVATION_COSINE_THRESHOLD = _float_env(
    'COGNITIVE_REACTIVATION_COSINE_THRESHOLD', 0.75, -1.0, 1.0,
)
COGNITIVE_REFLECTION_SUPPRESSION_SECONDS = _int_env(
    'COGNITIVE_REFLECTION_SUPPRESSION_SECONDS', 6 * 60 * 60,
)
COGNITIVE_REFLECTION_INTERVAL_SECONDS = _int_env(
    'COGNITIVE_REFLECTION_INTERVAL_SECONDS', 24 * 60 * 60, 1,
)
COGNITIVE_ESTIMATED_TOKENS_PER_SLOW_CYCLE = _int_env(
    'COGNITIVE_ESTIMATED_TOKENS_PER_SLOW_CYCLE', 1200, 1,
)

TRIGGER_PRIORITIES = {
    'prediction_error': 400,
    'question_reactivation': 300,
    'high_weight_evidence': 200,
    'scheduled_reflection': 100,
}

PREDICTION_RESOLVER_WHITELIST = frozenset({
    'interaction_gap_seconds',
    'messages_since_prediction_created',
    'evidence_count_since_prediction_created',
})

PREDICTION_NUMERIC_OPERATORS = frozenset({'<', '<=', '>', '>='})
PREDICTION_BOOLEAN_OPERATORS = frozenset({'is_true', 'is_false'})

REPLAY_COOLDOWN_MINUTES = (5, 15, 30, 60, 120)
REPLAY_HIGH_WEIGHT_THRESHOLDS = (0.6, 0.7, 0.8, 0.9)
REPLAY_REACTIVATION_THRESHOLDS = (0.65, 0.70, 0.75, 0.80, 0.85)
