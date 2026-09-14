"""Central configuration for Cognitive Loop v1.1."""
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


def _bool_env(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    return raw.strip().lower() not in {'0', 'false', 'no', 'off'}


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
COGNITIVE_REFLECTION_SCAN_SECONDS = _int_env(
    'COGNITIVE_REFLECTION_SCAN_SECONDS', 5 * 60, 30,
)
COGNITIVE_REFLECTION_ACTIVE_DAYS = _int_env(
    'COGNITIVE_REFLECTION_ACTIVE_DAYS', 30, 1,
)
COGNITIVE_REFLECTION_SCAN_BATCH = _int_env(
    'COGNITIVE_REFLECTION_SCAN_BATCH', 200, 1,
)
COGNITIVE_ESTIMATED_TOKENS_PER_SLOW_CYCLE = _int_env(
    'COGNITIVE_ESTIMATED_TOKENS_PER_SLOW_CYCLE', 1200, 1,
)
COGNITIVE_WORKER_ENABLED = _bool_env('COGNITIVE_WORKER_ENABLED', True)
COGNITIVE_WORKER_MODEL = (
    os.environ.get('COGNITIVE_WORKER_MODEL')
    or os.environ.get('MODEL_MAIN')
    or 'claude-opus-4-6'
)
COGNITIVE_WORKER_MAX_TOKENS = _int_env(
    'COGNITIVE_WORKER_MAX_TOKENS', 1800, 256,
)
COGNITIVE_WORKER_MODEL_ATTEMPTS = _int_env(
    'COGNITIVE_WORKER_MODEL_ATTEMPTS', 2, 1,
)
COGNITIVE_WORKER_POLL_SECONDS = _int_env(
    'COGNITIVE_WORKER_POLL_SECONDS', 5, 1,
)
COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS = _int_env(
    'COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS', 20, 1,
)
COGNITIVE_MAX_BELIEFS_IN_CONTEXT = _int_env(
    'COGNITIVE_MAX_BELIEFS_IN_CONTEXT', 50, 1,
)
COGNITIVE_MAX_HYPOTHESES_IN_CONTEXT = _int_env(
    'COGNITIVE_MAX_HYPOTHESES_IN_CONTEXT', 30, 1,
)
COGNITIVE_MAX_PREDICTIONS_IN_CONTEXT = _int_env(
    'COGNITIVE_MAX_PREDICTIONS_IN_CONTEXT', 20, 1,
)
COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT = _int_env(
    'COGNITIVE_MAX_STICKY_NOTES_IN_CONTEXT', 8, 1,
)
COGNITIVE_MAX_DIARY_ENTRIES_IN_CONTEXT = _int_env(
    'COGNITIVE_MAX_DIARY_ENTRIES_IN_CONTEXT', 5, 1,
)
COGNITIVE_STICKY_NOTE_DEFAULT_TTL_SECONDS = _int_env(
    'COGNITIVE_STICKY_NOTE_DEFAULT_TTL_SECONDS', 3 * 24 * 60 * 60, 300,
)
COGNITIVE_STICKY_NOTE_MAX_TTL_SECONDS = _int_env(
    'COGNITIVE_STICKY_NOTE_MAX_TTL_SECONDS', 14 * 24 * 60 * 60, 300,
)

TRIGGER_PRIORITIES = {
    'prediction_error': 400,
    'prediction_confirmation': 350,
    'question_reactivation': 300,
    'high_weight_evidence': 200,
    'scheduled_reflection': 100,
}

PREDICTION_RESOLVER_WHITELIST = frozenset({
    'current_event_signal_outcome',
})

PREDICTION_NUMERIC_OPERATORS = frozenset({'<', '<=', '>', '>='})
PREDICTION_BOOLEAN_OPERATORS = frozenset({'is_true', 'is_false'})

REPLAY_COOLDOWN_MINUTES = (5, 15, 30, 60, 120)
REPLAY_HIGH_WEIGHT_THRESHOLDS = (0.6, 0.7, 0.8, 0.9)
REPLAY_REACTIVATION_THRESHOLDS = (0.65, 0.70, 0.75, 0.80, 0.85)
