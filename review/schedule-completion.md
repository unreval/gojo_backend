# Today's timed schedule insert: completion report

Date: 2026-10-24 (Asia/Singapore).
Branch: `feat/schedule-insert-at-time-20261024`.
Base: `a091646ef172a9be5accabe97d3edd5c6ea87475` (local `origin/main`).
Worktree: `C:/Users/张三/Documents/GojoAssistant-live/.worktrees/schedule-insert-at-time-20261024`.

## Status

Implementation and verification complete. **No new regression**. Overall review
status: **NEEDS REVIEW / YELLOW**, because the full repository suite retains its
measured baseline failures and the automated report retains semantic risk.
Independent production-code review: PASS, no outstanding blocking finding.
No commit, push, merge, deployment, production DB operation, or paid API test.

## Root cause

The insert normalizer discarded clock fields; the canonical insert helper always
used the current minute; and commit rejected insert whenever any active event
existed. Both prompts omitted a future start time. Independent review also
identified the strict generation transport schema as a necessary part of the
fix: its event object disallowed undeclared properties, so schema-enabled models
could not emit start_time even after the prompt and database changes.

## Canonical authority

Authority Map domain 9 (Schedule / Phone Check / Proactive):
`schedule_contract.normalize_action_intent` remains the explicit intent contract.
`db_schedule.commit_schedule_transition` remains the conversation-to-schedule
writer, using the existing canonical event/phase insertion, transition ledger,
reflow, and phone-check reconciliation. No intent is inferred from prose.

## Files changed

Production:

- `gojo_backend/schedule_contract.py`: optional validated event.start_time;
  reject invalid clocks and explicit unsupported date fields entirely.
- `gojo_backend/db_schedule.py`: local-today insertion, future/active-overlap
  checks, reflow from the requested start, and future-plan prompt instructions.
- `gojo_backend/prompt.py`: timed insert example, today/future/omission rules,
  and continued free availability for companionship.
- `gojo_backend/generation_contract.py`: one optional string transport property
  for start_time; domain semantics remain in schedule_contract.

Tests:

- `tests/test_schedule_insert_time.py` (new): 28 pure contract cases.
- `tests/test_schedule_reflow_transition.py`: 11 new real SQL cases.
- `tests/test_insert_intent_prompt.py`: update example and add timing rules case.
- `tests/test_schedule_future_prompt.py`: assert actual world prompt guidance.
- `tests/test_generation_temporal_hotfix.py`: provider schema/round-trip case.

Docs/config:

- No product configuration change. `review/schedule-*` contains the change
  capsule, runner, completion, full comparison, governance evidence, and patch.
- `review/insert-time-*` contains original reproduction and interim test evidence.
- `review/latest.md` is the generated governance review entry point; its generic
  test-UNVERIFIED statement describes that generator, which does not run tests.
  Actual executed results are recorded here and in the linked JSON/log files.

## Behavior

Before: a structured insert could only start at submission, lost an explicit
clock, and was rejected during any active event.

After: event.start_time accepts ASCII H:MM or HH:MM (two minute digits), normalized
to HH:MM. It denotes today in `_canonical_tz()` with seconds/microseconds zero.
The clock must be future. Active overlap uses half-open actual bounds where
present, falling back to planned bounds. Invalid values, including present null,
reject the whole intent. Explicit sched_date/date/start_at on insert or event
also reject the whole intent. Missing start_time preserves the previous normalized
shape, active-event rejection, minute-truncated writer time, and after=now reflow.

Verified incident: at 15:30 with an active 15:00-16:00 event, insert 21:00-22:00
as free. The old planned 21:00-04:00 event and phase move to 22:00-05:00; the active
event and planned 18:00 dinner remain unchanged. Stored transition metadata keeps
the requested clock and actual reflow IDs/minutes. A later fixed event returns
fixed_future_event_conflict and restores event, phase, transition and phone-check
tables completely. Past/equal start and active overlap return the new explicit
reasons insert_start_not_future and insert_overlaps_active_event.

## Architecture impact

| Area | Impact |
|---|---|
| Canonical schedule authority | Unchanged; existing writer and stores |
| Relationship authority | Unchanged |
| Durable memory authority | Unchanged |
| Provenance | Unchanged; generated prose does not gain evidence authority |
| Database schema/migration | Unchanged; none added |
| API contract | Internal schedule intent/generation envelope adds optional event.start_time and two commit rejection reasons; HTTP endpoints unchanged |
| Worker lifecycle | Unchanged |
| Identity semantics | Unchanged |
| Truth/commit gates | Unchanged; failed commit cannot authorize the claimed change |
| Promise / schedule_revision | Unchanged |

## Tests actually executed

All commands use `C:/Users/张三/AppData/Local/GojoAssistant-test-env/python311/python.exe`
(abbreviated `$Py`) with `-B`, from this worktree. Database/generation groups use
offline network and real-psycopg2 guards, dummy keys, and local disposable PGlite.

| Actual command/group | Actual result |
|---|---|
| Direct local pytest of tests/test_schedule_insert_time.py before the normalizer change | Expected red: 26 failed, 2 passed |
| Same direct contract group after the normalizer change | 28 passed |
| `$Py -B review/run_memory_validation.py targeted --label insert-time-reproduction --path tests/test_schedule_reflow_transition.py::test_timed_insert_reserves_nine_pm_during_active_event_and_reflows_overnight` | Expected red before DB fix: 1 failed, 0 errors/skips |
| `$Py -B review/run_memory_validation.py targeted --label insert-time-targeted` with explicit paths schedule_insert_time, schedule_reflow_transition, insert_intent_prompt | Interim: 51 passed, 2 failed due to the new DATE serialization assertion; fixed by selecting sched_date::text, no product change |
| `$Py -B review/run_schedule_validation.py targeted --label insert-time-date-check --path tests/test_schedule_reflow_transition.py::test_timed_insert_anchors_to_local_today_and_preserves_overnight_end` | 2 passed, 0 failures/errors/skips |
| `$Py -B review/run_schedule_validation.py targeted --label schedule-targeted` with the 13 explicit paths listed below | 317 passed, 239 passed subtests, 0 failures/errors/skips |
| `$Py -B review/run_schedule_validation.py targeted --label schedule-schema-reproduction --path tests/test_generation_temporal_hotfix.py::GenerationEnvelopeTransportTests::test_provider_schema_allows_optional_timed_insert_and_round_trips_it` | Expected red before schema fix: 1 failed, 0 errors/skips |
| `$Py -B review/run_schedule_validation.py targeted --label schedule-generation-regression --path tests/test_generation_temporal_hotfix.py --path tests/test_generation_receipt.py --path tests/test_nonverbal_tts.py` | 112 passed, 220 passed subtests, 0 failures/errors/skips |
| `$Py -B review/run_schedule_validation.py full --baseline a091646 --label schedule-baseline-full` | 1755 collected; 1701 clean passed nodes, 54 failed nodes / 55 failure records, 0 errors/skips |
| `$Py -B review/run_schedule_validation.py full --label schedule-final-full` | 1796 collected; 1742 clean passed nodes, same 54 failed nodes / 55 failure records, 0 errors/skips |

The 13 expanded targeted paths were test_schedule_insert_time,
test_schedule_reflow_transition, test_insert_intent_prompt,
test_schedule_world_contract, test_schedule_revision, test_schedule_future_prompt,
test_schedule_future_prompt_integration, test_schedule_change_claim,
test_schedule_reply_state, test_read_receipt, test_delayed_reply,
test_structured_output_architecture, and test_chat_commit_gate (all `tests/*.py`).
The exact argv, individual nodes and raw results are retained in each JSON/log.

Raw final pytest summary: 55 failed, 1743 passed, 1196 subtests passed in 372.21s.
The clean-node counts exclude one parent call that pytest reports as passed even
though its two subtests failed. The task sheet's 32 failures does not match this
measured baseline. No failures were hidden or reclassified as passing.

`schedule-full-comparison.json` verifies all 41 added nodes passed, all 1701 old
clean passes remained passing, no collected nodes disappeared, and there were no
new failures, errors, skips or changed exception/assertion features. Its Markdown
companion lists all 54 old failing nodes individually. Baseline is a clean Git
archive with read-only history for existing git-show fixtures.

## Regression protection

The new permanent SQL incident test covers the reported missing reservation.
Additional permanent cases protect malformed input, backwards compatibility,
active actual/planned half-open bounds, rollback, midnight crossing, local-day
anchoring, prompt rendering and strict generation transport.

REG-009 owner: this backend repository. Static coverage remains PARTIAL; existing
schedule world, reply state, read receipt and delayed reply tests actually passed
in the targeted group. The uncovered continuity-adjustment boundary was not
modified. REG-001/REG-002 direct generation, nonverbal TTS, receipt and chat commit
gate tests actually passed in the two final targeted groups.

## Final diff and governance

Semantic risk: YELLOW. Classifier: YELLOW. Scope/diff audit: PASS for
schedule/proactive and generation. Generated review: NEEDS REVIEW / YELLOW,
with no deterministic scope violation. Final output is retained in
`schedule-governance-results.json` and individual gate logs.

Independent production review found and closed the missing transport property;
no blocking finding remains. `git diff --check` passed. The exact nine-file code
patch in `schedule-final-diff.patch` passed `git apply --check --reverse`.
Manual final diff review found no new writer/store, schema migration, authority
change, silent invalid-clock downgrade, altered guard, or unrelated refactor.

## Remaining risks and limits

- Full repository tests remain red with the same 54 pre-existing failed nodes;
  see the per-node comparison for exact failures.
- Reflow shifts whole planned events starting at/after the insertion start.
  It does not split a planned event that started before and spans the anchor.
  A later fixed event still rejects insertion, even when distant.
- Cross-day insert, proactive promise detection/delivery, and revision policy
  were explicitly excluded. A booked schedule interval alone does not create
  a proactive promise.
- Validation used disposable PGlite and mocked providers. No live provider,
  production PostgreSQL, deployment or backfill was tested/performed.

## Git state

- Task branch: `feat/schedule-insert-at-time-20261024`, HEAD remains a091646.
- Worktree has 4 modified production files, 4 modified test files, 1 new test
  file, and task review evidence. Changes are unstaged and uncommitted.
- Original IDE worktree remains `feat/time-cognition-projection-20261014` at
  b22841e with its original eight modified files and existing untracked content.
- Local main remains 6105646; local origin/main remains a091646.
- No commit, push, merge main, force push, branch deletion or deployment.
- No production database operation, backfill, or paid-model/API workload.
