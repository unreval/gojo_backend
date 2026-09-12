"""PostgreSQL schema for Cognitive Loop v1.1."""


COGNITIVE_DDL = (
    '''CREATE TABLE IF NOT EXISTS cognitive_events (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        source_event_type TEXT NOT NULL,
        source_event_id TEXT NOT NULL,
        source TEXT NOT NULL,
        occurred_at TIMESTAMPTZ NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        embedding_json TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, character_id, source_event_type, source_event_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_cognitive_events_pair_time
       ON cognitive_events (user_id, character_id, occurred_at DESC)''',
    '''CREATE TABLE IF NOT EXISTS cognitive_cycles (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
        primary_trigger_class TEXT NOT NULL,
        input_state_version BIGINT NOT NULL DEFAULT 0,
        output_state_version BIGINT,
        reasoning_context JSONB,
        failure_code TEXT,
        queued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        started_at TIMESTAMPTZ,
        completed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        CONSTRAINT ck_cognitive_cycle_output_version CHECK (
            (status = 'succeeded'
             AND output_state_version = input_state_version + 1)
            OR (status <> 'succeeded' AND output_state_version IS NULL)
        )
    )''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS uq_cognitive_cycles_one_active_pair
       ON cognitive_cycles (user_id, character_id)
       WHERE status IN ('queued', 'running')''',
    '''CREATE INDEX IF NOT EXISTS idx_cognitive_cycles_pair_completed
       ON cognitive_cycles (user_id, character_id, completed_at DESC)''',
    '''CREATE TABLE IF NOT EXISTS cognitive_event_triggers (
        id BIGSERIAL PRIMARY KEY,
        event_id BIGINT NOT NULL REFERENCES cognitive_events(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        trigger_class TEXT NOT NULL CHECK (trigger_class IN (
            'prediction_error', 'question_reactivation',
            'high_weight_evidence', 'scheduled_reflection'
        )),
        occurrence_key TEXT NOT NULL DEFAULT 'default',
        priority INTEGER NOT NULL,
        payload JSONB NOT NULL DEFAULT '{}'::jsonb,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN (
            'pending', 'claimed', 'consumed', 'suppressed', 'dead_letter'
        )),
        claimed_by_cycle_id BIGINT REFERENCES cognitive_cycles(id),
        claimed_at TIMESTAMPTZ,
        claim_expires_at TIMESTAMPTZ,
        consumed_cycle_id BIGINT REFERENCES cognitive_cycles(id),
        consumed_at TIMESTAMPTZ,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        last_error_code TEXT,
        slow_cycle_suppressed_reason TEXT,
        suppressed_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (event_id, trigger_class, occurrence_key),
        CONSTRAINT ck_cognitive_trigger_claim_fields CHECK (
            (status = 'claimed'
             AND claimed_by_cycle_id IS NOT NULL
             AND claimed_at IS NOT NULL
             AND claim_expires_at IS NOT NULL)
            OR (status <> 'claimed'
                AND claimed_by_cycle_id IS NULL
                AND claimed_at IS NULL
                AND claim_expires_at IS NULL)
        ),
        CONSTRAINT ck_cognitive_trigger_consumed_fields CHECK (
            (status = 'consumed'
             AND consumed_cycle_id IS NOT NULL
             AND consumed_at IS NOT NULL)
            OR (status <> 'consumed'
                AND consumed_cycle_id IS NULL
                AND consumed_at IS NULL)
        )
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_cognitive_triggers_pending_pair
       ON cognitive_event_triggers
       (user_id, character_id, status, priority DESC, created_at)''',
    '''CREATE INDEX IF NOT EXISTS idx_cognitive_triggers_lease
       ON cognitive_event_triggers (status, claim_expires_at)
       WHERE status = 'claimed' ''',
    '''CREATE TABLE IF NOT EXISTS cognitive_cycle_trigger_events (
        cycle_id BIGINT NOT NULL REFERENCES cognitive_cycles(id) ON DELETE CASCADE,
        trigger_event_id BIGINT NOT NULL
            REFERENCES cognitive_event_triggers(id) ON DELETE RESTRICT,
        is_primary BOOLEAN NOT NULL DEFAULT FALSE,
        position INTEGER NOT NULL DEFAULT 0,
        added_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (cycle_id, trigger_event_id)
    )''',
    '''CREATE UNIQUE INDEX IF NOT EXISTS uq_cognitive_cycle_one_primary
       ON cognitive_cycle_trigger_events (cycle_id)
       WHERE is_primary''',
    '''CREATE TABLE IF NOT EXISTS cognitive_questions (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        question_key TEXT NOT NULL,
        question_text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'dormant'
            CHECK (status IN ('active', 'dormant', 'resolved', 'archived')),
        embedding_json TEXT,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, character_id, question_key)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_cognitive_questions_dormant
       ON cognitive_questions (user_id, character_id, status)
       WHERE status = 'dormant' ''',
    '''CREATE TABLE IF NOT EXISTS cognitive_hypotheses (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        question_id BIGINT REFERENCES cognitive_questions(id) ON DELETE SET NULL,
        hypothesis_key TEXT NOT NULL,
        statement TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open'
            CHECK (status IN ('open', 'supported', 'rejected', 'archived')),
        evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
        created_by_cycle_id BIGINT REFERENCES cognitive_cycles(id),
        updated_by_cycle_id BIGINT REFERENCES cognitive_cycles(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, character_id, hypothesis_key)
    )''',
    '''CREATE TABLE IF NOT EXISTS cognitive_predictions (
        id BIGSERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        question_id BIGINT REFERENCES cognitive_questions(id) ON DELETE SET NULL,
        hypothesis_id BIGINT REFERENCES cognitive_hypotheses(id) ON DELETE SET NULL,
        prediction_key TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'fulfilled', 'violated', 'expired')),
        resolver_name TEXT NOT NULL,
        fulfillment_operator TEXT NOT NULL,
        fulfillment_value DOUBLE PRECISION NOT NULL,
        violation_operator TEXT,
        violation_value DOUBLE PRECISION,
        observed_value DOUBLE PRECISION,
        expires_at TIMESTAMPTZ,
        settled_at TIMESTAMPTZ,
        settled_by_event_id BIGINT REFERENCES cognitive_events(id) ON DELETE SET NULL,
        created_by_cycle_id BIGINT REFERENCES cognitive_cycles(id),
        last_error_code TEXT,
        metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, character_id, prediction_key)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_cognitive_predictions_pending
       ON cognitive_predictions (user_id, character_id, status, created_at)
       WHERE status = 'pending' ''',
)


def ddl_statements():
    return COGNITIVE_DDL


def init_cognitive_tables(conn=None):
    """Initialize only deterministic storage; no worker or model is started."""
    owns_connection = conn is None
    if conn is None:
        from db import get_conn
        conn = get_conn()
    cur = conn.cursor()
    try:
        for statement in COGNITIVE_DDL:
            cur.execute(statement)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            conn.close()
    print('[init] Cognitive Loop v1.1 deterministic tables ready')
