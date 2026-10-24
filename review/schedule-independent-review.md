# Schedule timed insert — independent review

Review scope: the current diff against `origin/main` (`a091646`), limited to today's explicit insert clock and its contract, transaction, prompt, and regression coverage. This reviewer authored the additional SQL regressions in `test_schedule_reflow_transition.py`; review of those tests is therefore a self-check. The production diff was reviewed independently.

## Finding corrected and verified

**P1 — The strict generation schema forbade the new clock — CLOSED.**

The first reviewed diff left `gojo_backend/generation_contract.py`'s `schedule_action_intent.event` with `additionalProperties=False` and no `start_time` property. Since `route_chat._create_json` passes that schema through `output_config.format`, schema-enabled generation could not emit the new prompt example. The new transport regression actually reproduced the defect before correction: `review/schedule-schema-reproduction-tests.log` records **1 failed** with `KeyError: 'start_time'` on the schema sent to the mocked provider.

The final product diff adds only optional `start_time: _STRING` to that existing event schema. The new `GenerationEnvelopeTransportTests.test_provider_schema_allows_optional_timed_insert_and_round_trips_it` inspects the exact schema passed to `_create_json`'s mocked provider, verifies the clock is optional and allowed, preserves the additional-property restriction, and round-trips it into `normalize_action_intent`. Semantic validation remains with the schedule contract and the existing commit gate remains unchanged. The narrow generation transport impact is documented in `review/schedule-insert-change-capsule.md`.

This reviewer read the final schema/test diff and the actual run artifacts: the generation regression group completed with **112 passed, 220 passed subtests, 0 failed, 0 errors, 0 skipped** (`review/schedule-generation-regression-tests.json`). P1 is closed.

## Production checks without other findings

- The contract accepts only ASCII 24-hour clocks with one or two hour digits and two minute digits, canonicalizes `9:00` to `09:00`, rejects present invalid clocks including `None`, and leaves the previous no-clock normalized shape intact. Input dictionaries are not mutated. Explicit `sched_date`, `date`, and `start_at` fields on the insert or event are rejected, preventing unsupported dates from silently becoming an immediate insert.
- Both validation and writing anchor an explicit clock to `now.astimezone(_canonical_tz())`, with seconds and microseconds cleared. The writer continues through `_insert_canonical_event_tx`; existing `event_bounds` handles overnight ends. No second writer or state store was added.
- The timed branch rejects `start <= now` and checks all active events, without a schedule-date filter that would miss a previous day's overnight event. `COALESCE(actual_start_at, planned_start_at) < end` and `COALESCE(actual_end_at, planned_end_at) > start` implement half-open overlap semantics using actual bounds where present.
- The no-clock branch still calls `_has_active_event_tx` and returns the same rejection. Its writer still floors `now` to a minute, while reflow still uses the original `now` rather than the floored time.
- Timed reflow uses the inserted start as its anchor, preserves duration and stored reflow metadata, and retains the existing fixed-event rejection plus transaction rollback. Event/phase mutations and transition creation remain in the existing transaction.
- The rendered insert example and world prompt both explain today's future clock, omission for immediate insertion, and companionship remaining `free`. The truth/claim guards and generated-intent commit gate are unchanged.
- No schema migration, promise change, schedule revision change, identity change, relationship/memory authority change, provenance promotion, paid workload, or production access was introduced.

## Regression evidence

- Original failure was actually reproduced before the production fix: `review/insert-time-reproduction-tests.log` records the new 15:30-active / 21:00-insert regression failing against the original implementation.
- Added real SQL coverage exercises the 21:00–04:00 event moving to 22:00–05:00, event and phase revisions, unaffected active and intermediate plans, stored clock/reflow metadata, past/equal starts, actual/planned active overlap and exact boundary acceptance, fixed-conflict rollback across all four affected tables, legacy active-event rejection, midnight crossing, and UTC/local day anchoring.
- The first targeted run recorded 51 passes and two failures caused solely by the test's assumption about PGlite's JSON serialization of a DATE (`2026-10-24T00:00:00.000Z`). The assertion query was corrected to read `sched_date::text`. This reviewer then actually ran `review/run_schedule_validation.py targeted --label insert-time-date-check --path tests/test_schedule_reflow_transition.py::test_timed_insert_anchors_to_local_today_and_preserves_overnight_end` using the local Python 3.11 environment and offline guards: **2 passed, 0 failed, 0 skipped**. Evidence: `review/insert-time-date-check-tests.json` and `.log`.
- The primary agent's subsequent expanded schedule run was actually executed and its JSON artifact inspected during this review: **317 passed, 239 passed subtests, 0 failed, 0 errors, 0 skipped** (`review/schedule-targeted-tests.json`). This group includes the corrected DATE assertions, schedule/phase/reflow tests, future prompts, claim guards, reply availability, REG-009 direct coverage, structured-output architecture, and chat commit gate.
- `git diff --check` was actually executed successfully during review. This review did not run the full suite. Full regression comparison and final governance gates remain the primary agent's responsibility.
- REG-009 belongs to `gojo_backend` and has PARTIAL static coverage. Existing direct tests include schedule world, reply state, read receipt, and delayed reply. This change does not modify the still-uncovered continuity-adjustment implementation.

## Deliberate remaining limits

The existing reflow moves only planned events whose `planned_start_at >= insertion_start`; it does not split a planned event that began before that anchor, and any later fixed event causes rejection. These are the requested reuse boundaries. Cross-day booking, proactive promise detection/delivery, and generation of a full day around previously booked promises remain outside this task.

Independent code-review status: **PASS; P1 closed.** No remaining blocking production defect was identified in the final requested implementation. Semantic risk remains **YELLOW** for the authorized schedule contract and narrow generation transport change. Full-suite baseline comparison and final governance gates remain required before task completion; this report does not claim those pending checks passed.
