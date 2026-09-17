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
    -> read-only Cognitive Reader context for later chat turns
```

Scheduled reflection writes an idempotent source event and a normal trigger
occurrence. It does not inspect the pending queue, merge work, claim work, or
create a cycle. The worker scans recently active user/character pairs and calls
this ingress once per reflection bucket. All trigger classes meet at the same
queue transaction.

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
- **CLV1-INV-012:** Prediction settlement is deterministic. A verified
  violation emits `prediction_error`; a verified fulfillment emits
  `prediction_confirmation`. It does not infer an emotion, attitude, or
  response policy.
- **CLV1-INV-013:** At the daily Slow Loop limit, pending triggers are
  suppressed with reason `daily_limit` so they do not become a next-day
  backlog.
- **CLV1-INV-014:** Question reactivation means only "possibly related". It
  does not resolve a question, validate an answer, or modify question status.
- **CLV1-INV-015:** Embeddings are stored as JSON in `TEXT` and cosine
  similarity is computed in-process; pgvector is not required.
- **CLV1-INV-016:** Predictions are resolved only by the static
  `current_event_signal_outcome` resolver. It compares the next v4 signal event
  with validated declarative fulfillment and violation selectors. Dynamic
  imports, arbitrary field paths, `eval`, and `exec` are prohibited.
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
- **CLV1-INV-023:** Model output must contain `cycle_summary`,
  `question_updates`, `belief_updates`, `hypothesis_updates`,
  `new_predictions`, `evidence_refs`, and `reflection_note`. Hidden reasoning
  or surrounding prose is not persisted. `sticky_note_updates` and
  `diary_entries` are optional.
- **CLV1-INV-024:** Every update must cite either an event attached to the
  claimed cycle or an existing same-pair event already cited in the supplied
  belief/hypothesis/prediction context. Invented, undeclared, or cross-pair
  evidence IDs reject the whole output.
- **CLV1-INV-025:** Durable cognitive writes, the cycle output version, and
  trigger consumption commit in one transaction or all roll back together.
- **CLV1-INV-026:** The chat path reads only the latest summary, active beliefs,
  and open/supported hypotheses. It never injects hidden reasoning or pending
  predictions, and labels hypotheses as non-factual.

## Lifecycle

Aggregation uses a stable advisory-lock key for each user/character pair. In a
single short transaction it recovers expired leases, checks the one-active
constraint and Slow Loop limits, selects pending triggers with `FOR UPDATE SKIP
LOCKED`, creates a queued cycle, claims all selected occurrences, and records
their primary/secondary joins.

The Slow Loop worker is enabled by default. At a bounded interval it enqueues
idempotent scheduled reflections for recently active pairs. Each pass then
recovers expired leases, aggregates pending trigger pairs, claims one queued
cycle with `FOR UPDATE SKIP LOCKED`, commits the claim, builds factual context,
and calls the configured model outside the transaction. Model output is parsed
and strictly validated; one correction attempt is allowed by default.

On success, structured cycle output, durable beliefs, hypotheses, predictions,
the output version, and all claimed trigger occurrences are committed in one
transaction. On failure or lease expiry, the output version remains `NULL`;
triggers return to `pending` until `COGNITIVE_MAX_RETRY`, then become
`dead_letter`. Cooldown leaves triggers pending for later aggregation. Daily
limit suppression is terminal for those occurrences. A one-time startup
migration requeues pre-worker dead letters caused only by expired claims.

## Structured Output

`cognitive_cycles` stores the validated result in JSONB columns:

- `cycle_summary`: factual synthesis, salient change, uncertainty, and a
  low/medium/high confidence label.
- `question_updates`: unresolved questions with lifecycle status.
- `belief_updates`: durable keyed beliefs with numeric confidence, status, and
  evidence references. Current values are upserted into `cognitive_beliefs`.
- `hypothesis_updates`: keyed, testable interpretations with lifecycle status
  and evidence history. Current values are upserted into
  `cognitive_hypotheses`.
- `new_predictions`: keyed predictions restricted to semantic v4 signal
  selectors, exact fulfillment/violation rules, and bounded TTLs. They are
  inserted into `cognitive_predictions`.
- `evidence_refs`: current or previously registered same-pair event IDs plus a
  concise explanation of why each event supports the output.
- `reflection_note`: compact internal note for the next generator prompt. It
  is not shown on the user-facing 便利贴.
- optional `sticky_note_updates`: Slow Loop working notes persisted to
  `cognitive_sticky_notes` with `source='cognitive_slow_loop'`. This is the
  only user-facing 便利贴 source.
- optional `diary_entries`: first-person reflective records.

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
- `COGNITIVE_REFLECTION_SCAN_SECONDS` (default `300`)
- `COGNITIVE_REFLECTION_INTERVAL_SECONDS` (default `86400`)
- `COGNITIVE_REFLECTION_ACTIVE_DAYS` (default `30`)

The health response includes `cognitive_worker: true|false`. To inspect saved
conclusions without exposing the full reasoning context, run this inside the
backend service directory:

```bash
python3 cognitive_inspect.py --user-id USER_ID --character-id gojo --limit 5
```

## Prediction Rules

Every new prediction uses `current_event_signal_outcome`, fulfillment `>= 1`,
and violation `<= -1`. Metadata contains non-empty `fulfillment_signals` and
`violation_signals`; each selector names a v4 `signal_type`, an `actor`, and
optional confidence or attribute constraints. The resolver checks only the
next stored signal event. It never reads relationship state or
relationship-model output. A one-time startup migration expires old Slow Loop
predictions that used message, evidence-count, or elapsed-time proxies.

## Replay

`cognitive_replay.py` reads existing `rel_provenance_log` and
`rel_interaction_stats` facts, accepts synthetic prediction fixtures and
human-labelled reactivation fixtures, and performs no model or embedding API
calls. Its sweep is the Cartesian product of five cooldowns, four high-weight
thresholds, and five cosine thresholds: exactly 100 combinations.

## User-facing Sticky Notes

The 便利贴 UI is a presentation surface of the persistent cognitive system.
It is not a second per-turn roleplay pass.

```text
chat / events
    -> Cognitive Fast Loop (no LLM)
    -> trigger
    -> Cognitive Slow Loop
    -> sticky_note_updates
    -> cognitive_sticky_notes (source=cognitive_slow_loop)
    -> GET /grumbles
    -> UI
```

Rules:

- Fast Loop does not call an LLM and must not write user-facing stickies.
- Only the Slow Loop worker may generate `sticky_note_updates`.
- User-facing notes always carry provenance: `source`, `source_event_refs`,
  `created_by_cycle_id`, `updated_by_cycle_id`.
- `memory_lifecycle_fast_loop` stickies are internal memory cues (for example
  a near-term exam reminder). They stay in `cognitive_sticky_notes` for recall
  but are not listed by `/grumbles`.
- Stickies written before the user-facing cutoff are kept in place. A one-shot
  startup migration sets `user_visible=FALSE` on existing
  `source=cognitive_slow_loop` rows so old internal working notes are not
  shown on `/grumbles`. New Slow Loop content on the same `note_key` becomes
  visible again. Rows are not deleted.
- `viewed` / `viewed_at` are read-state. `status=completed` is semantic
  lifecycle completion. These are different fields; mark-viewed must not
  complete a note.
- User tear-off sets `user_hidden_at`. It does not set `status=completed`.

## Retired: grumble_engine

`grumble_engine` was a per-turn independent inner-monologue generator:

```text
/chat/text -> maybe_write_grumble -> MODEL_CN_AUX -> char_grumble -> /grumbles
```

Status: **RETIRED / LEGACY**.

- `char_grumble` is no longer a UI source and receives no new writes.
- Historical `char_grumble` rows are kept; they are not dropped in this
  migration and must not re-enter current mind output.
- Do not add another per-turn inner-monologue classifier or emotion LLM to
  keep the old 便利贴 colors. If emotion is needed later, it belongs in the
  Slow Loop output schema with validation and provenance.
