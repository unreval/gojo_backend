# Schedule insert at a time: change capsule

Date: 2026-10-24 (Asia/Singapore)
Branch: `feat/schedule-insert-at-time-20261024`
Baseline: `a091646` (local `origin/main` at worktree creation).
Worktree: `.worktrees/schedule-insert-at-time-20261024`.

## Problem and root cause

The structured insert contract discarded clock fields, the canonical insert
helper always used the current minute, and commit rejected every insert while
any event was active. A reservation for later today could not reach the canonical
schedule. Both generation and future-plan prompts lacked this capability.

## Canonical authority

Authority Map domain 9, Schedule / Phone Check / Proactive:
`schedule_contract.normalize_action_intent` validates explicit proposals;
`db_schedule.commit_schedule_transition` remains the conversation-to-schedule
writer, using the existing `char_schedule`, `char_schedule_phase`, transition
ledger, and phone-check reconciliation. The LLM proposes; commit validates.

## Allowed scope and impact review

Production: `schedule_contract.py`, `db_schedule.py`, the schedule action
section of `prompt.py`, and the matching optional field in
`generation_contract.py`. Tests: direct contract, SQL reflow, prompt contracts,
and the actual Provider request schema and response round trip.
Validation evidence and this report live under `review/`.

Governance domains: `schedule/proactive` and `generation` (prompt wording only).
The structured intent contract gains optional `event.start_time`; commit gains
two explicit rejection reasons. This affects the already-authorized schedule
behavior. No promise detector, proactive worker, schedule_revision, generation
commit gate, database schema, or new persistence path is involved.

Independent review found that the strict generation envelope schema disallows
undeclared event properties. Without the matching optional `start_time` field,
schema-enabled models cannot produce the requested action. Impact was reviewed
before extending implementation to that fourth production file: add only a
string transport property, retain semantic validation in schedule_contract,
and preserve all generation gates, envelope requirements and other actions.
This is a necessary part of carrying the requested new intent field through the
existing transport path, within the already-reviewed generation domain.

## Protected invariants

- Explicit structured intent only; no inference from dialogue.
- Invalid start times and explicit `sched_date`, `date`, or `start_at` on insert
  are rejected entirely, including those fields on the outer intent object.
- Today is determined in `_canonical_tz()`; an explicit clock must be future.
- Missing start_time preserves the old shape, active rejection, minute-truncated
  insertion time, and `after=now` reflow anchor.
- Existing fixed-event reflow refusal and transaction rollback remain intact.
- No new authoritative store, authority owner, identity semantics, schema, or
  worker behavior. Relationship, durable memory, and provenance are unchanged.
- `change_claim_conflict` and successful-commit requirements remain unchanged.

## Required permanent regression tests

- At 15:30 during an active event, reserve 21:00-22:00 with reply_state free;
  shift the planned 21:00-04:00 event and its phase to 22:00-05:00, preserve the
  active event and earlier planned dinner, and retain reflow ledger metadata.
- Validate H:MM/HH:MM, reject invalid/nonstring values and cross-day fields,
  preserve the full old normalized output, and prove normalization is idempotent.
- Refuse past/equal start and active actual/planned half-open overlap.
- Refuse a fixed future event with all four schedule tables unchanged.
- Preserve old active rejection, local-day anchoring, overnight end, and rendered
  prompt examples and timing instructions.

## Historical regression

REG-009 owner: this repository (`gojo_backend`); static coverage is PARTIAL.
Existing tests: schedule_world_contract, read_receipt, schedule_reply_state,
delayed_reply. Run these along with schedule/reflow/change-claim/prompt tests.
The missing end-to-end continuity-adjustment coverage is outside this change;
no new availability authority or history-rewriting behavior is introduced.

## Risk

Semantic risk: YELLOW for the explicitly authorized structured contract and
commit behavior extension. Initial classifier: GREEN (no diff yet); this does
not lower semantic risk. Final gates and executed tests are recorded separately.

## Verification plan

1. Run contract tests and the permanent SQL incident test against old behavior.
2. Run direct contract, reflow, and rendered insert-prompt tests.
3. Run schedule integration, truth-claim, architecture, and REG-009 tests.
4. Compare full-suite results against a clean archive of a091646, including
   failure identities, signatures, errors, skips, and lost passes.
5. Inspect final diff; run classifier, scope audit, and review report generator.

## Deliberate limits

Reuse existing reflow: shift whole planned events at/after the insertion start.
Do not split placeholders. Cross-day reservation and proactive promise detection
require separate work. The existing reflow selection does not handle a planned
event that starts before the anchor and spans it; this task retains that policy.
