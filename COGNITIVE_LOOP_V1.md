# Cognitive Loop v1.1

The Cognitive Loop combines verifiable deterministic maintenance with a
background Slow Loop consolidation worker. The worker creates auditable beliefs,
hypotheses, and falsifiable predictions. It does not decide the character's next
reply or directly change relationship state.

## Data Flow

```text
source event
    -> cognitive_events
    -> deterministic maintenance
       - prediction settlement
       - dormant-question cosine ranking
       - v4 signal confidence classification
    -> cognitive_event_triggers
    -> per-user/character aggregation and claim
    -> cognitive_cycles
    -> Slow Loop model consolidation
       - cycle_summary
       - belief_updates
       - hypothesis_updates
       - new_predictions
       - evidence_refs
    -> atomic validation, persistence, and trigger consumption
```

Scheduled reflection writes an idempotent source event and a normal trigger
occurrence. It does not inspect the pending queue, merge work, claim work, or
create a cycle. All trigger classes meet at the same queue transaction.

## Invariants

- **CLV1-INV-001:** Cognitive Loop never writes `rel_state`, any relationship
  model, or relationship dimensions such as warmth, trust, attachment,
  passion, or friction.
- **CLV1-INV-002:** The deterministic Fast Loop makes no LLM calls. Only
  `cognitive_worker.py` may invoke the model for Slow Loop consolidation.
- **CLV1-INV-003:** Cognitive output cannot recursively create another
  cognitive source event.
- **CLV1-INV-004:** Cognitive ingress reuses the relationship v4 signal schema
  (`signal_type`, `actor`, `confidence`, `brief`, `attributes`) and its
  `CONFIDENCE_MULTIPLIER`; there is no second evidence schema.
- **CLV1-INV-005:** A source event is idempotent by `(user_id, character_id,
  source_event_type, source_event_id)`. A duplicate creates no side effects.
- **CLV1-INV-006:** Claiming is not consuming. A claimed trigger remains
  unconsumed while its cycle is queued or running.
- **CLV1-INV-007:** Only a successfully committed cognitive cycle advances the
  state version from `input_state_version` to `input_state_version + 1`.
- **CLV1-INV-008:** The PostgreSQL advisory lock protects only the short
  recovery, limit check, aggregation, and claim transaction. No model call is
  permitted while it is held.
- **CLV1-INV-009:** A PostgreSQL partial unique index permits at most one
  `queued` or `running` cycle for each `(user_id, character_id)` pair.
- **CLV1-INV-010:** Scheduled reflection uses the unified source-event and
  trigger ingress and never creates a cycle itself.
- **CLV1-INV-011:** The primary trigger records the highest-priority cause;
  every selected secondary trigger remains attached to the cycle.
- **CLV1-INV-012:** Prediction settlement is deterministic and can only emit a
  `prediction_error` occurrence for a verified violation. It does not infer an
  emotion, attitude, or response policy.
- **CLV1-INV-013:** At the daily Slow Loop limit, pending triggers are
  suppressed with reason `daily_limit` so they do not become a next-day
  backlog.
- **CLV1-INV-014:** Question reactivation means only "possibly related". It
  does not resolve a question, validate an answer, or modify question status.
- **CLV1-INV-015:** Embeddings are stored as JSON in `TEXT` and cosine
  similarity is computed in-process; pgvector is not required.
- **CLV1-INV-016:** Prediction fields are resolved only by the static
  whitelist: `interaction_gap_seconds`,
  `messages_since_prediction_created`, and
  `evidence_count_since_prediction_created`. Dynamic imports, arbitrary field
  paths, `eval`, and `exec` are prohibited.
- **CLV1-INV-017:** One source event may produce multiple trigger occurrences,
  including multiple occurrences of the same class with distinct keys.
- **CLV1-INV-018:** One cycle may aggregate multiple trigger occurrences and
  multiple trigger classes through `cognitive_cycle_trigger_events`.
- **CLV1-INV-019:** Daily limits and cooldowns govern only creation of a new
  Slow cycle. Event writes, prediction settlement, cosine ranking, trigger
  detection, and lease recovery continue independently.
- **CLV1-INV-020:** `consumed_cycle_id` and `consumed_at` are written only in
  the successful cycle commit transaction. Failure and lease expiry leave both
  fields `NULL` and either retry or dead-letter the trigger.
- **CLV1-INV-021:** The model call runs after the cycle claim transaction has
  committed. No database or advisory lock is held during network I/O.
- **CLV1-INV-022:** The worker receives only structured cycle events, trigger
  facts, current beliefs, hypotheses, and pending predictions. It does not load
  or send complete chat logs.
- **CLV1-INV-023:** Model output must contain exactly `cycle_summary`,
  `belief_updates`, `hypothesis_updates`, `new_predictions`, and
  `evidence_refs`. Hidden reasoning or surrounding prose is not persisted.
- **CLV1-INV-024:** Every update must cite an event attached to the claimed
  cycle. Invented, undeclared, or out-of-cycle evidence IDs reject the whole
  output.
- **CLV1-INV-025:** Durable cognitive writes, the cycle output version, and
  trigger consumption commit in one transaction or all roll back together.

## Lifecycle

Aggregation uses a stable advisory-lock key for each user/character pair. In a
single short transaction it recovers expired leases, checks the one-active
constraint and Slow Loop limits, selects pending triggers with `FOR UPDATE SKIP
LOCKED`, creates a queued cycle, claims all selected occurrences, and records
their primary/secondary joins.

The Slow Loop worker is enabled by default. Each pass recovers expired leases,
aggregates pending trigger pairs, claims one queued cycle with `FOR UPDATE SKIP
LOCKED`, commits the claim, builds factual context, and calls the configured
model outside the transaction. Model output is parsed and strictly validated;
one correction attempt is allowed by default.

On success, structured cycle output, durable beliefs, hypotheses, predictions,
the output version, and all claimed trigger occurrences are committed in one
transaction. On failure or lease expiry, the output version remains `NULL`;
triggers return to `pending` until `COGNITIVE_MAX_RETRY`, then become
`dead_letter`. Cooldown leaves triggers pending for later aggregation. Daily
limit suppression is terminal for those occurrences. A one-time startup
migration requeues pre-worker dead letters caused only by expired claims.

## Structured Output

`cognitive_cycles` stores the complete validated result in five JSONB columns:

- `cycle_summary`: factual synthesis, salient change, uncertainty, and a
  low/medium/high confidence label.
- `belief_updates`: durable keyed beliefs with numeric confidence, status, and
  evidence references. Current values are upserted into `cognitive_beliefs`.
- `hypothesis_updates`: keyed, testable interpretations with lifecycle status
  and evidence history. Current values are upserted into
  `cognitive_hypotheses`.
- `new_predictions`: keyed predictions restricted to the deterministic resolver
  and operator whitelist. They are inserted into `cognitive_predictions`.
- `evidence_refs`: event IDs from this cycle plus a concise explanation of why
  each event supports the output.

Predictions are never free-form executable instructions. Unknown fields,
resolvers, operators, status values, oversized content, invalid TTLs, duplicate
keys, and ungrounded references reject the output before any conclusion is
written.

## Operation

The server starts the worker automatically after cognitive tables are ready.
Set `COGNITIVE_WORKER_ENABLED=false` only to disable it. Useful settings are:

- `COGNITIVE_WORKER_MODEL` (defaults to `MODEL_MAIN`)
- `COGNITIVE_WORKER_MAX_TOKENS` (default `1800`)
- `COGNITIVE_WORKER_MODEL_ATTEMPTS` (default `2`)
- `COGNITIVE_WORKER_POLL_SECONDS` (default `5`)
- `COGNITIVE_WORKER_ERROR_BACKOFF_SECONDS` (default `20`)

The health response includes `cognitive_worker: true|false`. To inspect saved
conclusions without exposing the full reasoning context, run this inside the
backend service directory:

```bash
python3 cognitive_inspect.py --user-id USER_ID --character-id gojo --limit 5
```

## Prediction Rules

Predictions use numeric `<`, `<=`, `>`, and `>=` operators. Boolean operators
are reserved as `is_true` and `is_false`, but no Boolean field is registered in
v1.1. Resolvers read only cognitive event counts and timestamps. They never
read relationship state or relationship-model output.

## Replay

`cognitive_replay.py` reads existing `rel_provenance_log` and
`rel_interaction_stats` facts, accepts synthetic prediction fixtures and
human-labelled reactivation fixtures, and performs no model or embedding API
calls. Its sweep is the Cartesian product of five cooldowns, four high-weight
thresholds, and five cosine thresholds: exactly 100 combinations.
