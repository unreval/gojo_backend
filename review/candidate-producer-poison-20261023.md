# Candidate producer poison batch recovery

Date: 2026-10-23 (Asia/Singapore)
Implementation baseline: `811c0f0` (PR #35).

## Status and risk

NEEDS REVIEW / YELLOW. Scope audit PASS; independent source review found no
remaining blocking findings. The user authorized committing, pushing this fix
branch and opening a PR. Merge, deployment and shadow/live changes are separate.

## Problem and root cause

An unescaped quote in a model string invalidates the entire JSON array. Returning
the oldest eight sources to pending repeats the same failing batch and consumes
the global daily limit. Previously only truncation fixed the batch size at four,
and successful batches did not restore it. Decoder exceptions lost their position.

The initial isolation design also incorrectly used cumulative retry_count, which
includes historical batch failures. A count of 48 would skip after the first
actual solo parse failure. The final implementation uses an independent count.

## Behavior and canonical authority

- Prompt requests valid JSON strings, escaped ASCII quotes, Chinese quotation
  marks for quoted words and omission of an uncertain perspective field.
- Parse-stage invalid_json / invalid_batch_shape / incomplete_response /
  truncated_response reduce the saved size 8->4->2->1, with a floor of one.
- Only an actual single-source parse failure increments that source's
  solo_failures entry in the existing per-user event_processing_state.result_ref
  JSON. Historical retry_count remains audit-only and still increments normally.
- The third solo failure generates skip_ids and removes the count; SQL only
  checks set membership. A source starting at retry_count=48 therefore remains
  pending after the first solo failure, with total49/solo1.
- Success copies the solo map and removes this batch's source IDs. Successful
  saved batch sizes double to a maximum of eight: 1->2->4->8. Failed persistence
  cannot save an uncommitted success cleanup through the exception path.
- Skipped receipts and solo removal commit atomically. A content-free
  source_skipped audit follows confirmed commit; version-scoped queue filters
  permit other sources to progress and another processor version to reprocess.
- Parser diagnostics expose exception class and container-relative character
  offset, e.g. JSONDecodeError@35; non-positioned strict errors use ValueError@0.
  No raw response or exception message is added to telemetry. JSON is not repaired.

candidate_producer owns processing accounting in event_processing_state.
memory_candidate retains deterministic validation; cognitive adjudication and
memory_authority retain canonical projection. No second authority/store exists.

## Files and architecture impact

Production: candidate_producer.py and structured_output.py.
Tests: test_candidate_producer.py and test_structured_output.py.
Validation: register parser and parser architecture tests in run_memory_validation.py.

Canonical, relationship and durable-memory authority, provenance, schema, public
API, identity and worker lifecycle remain unchanged. Only requested retry policy
and safe parser diagnostic metadata change. G1-G9 and canonical writers remain
unchanged; 15 protected module/function comparisons against 811c0f0 confirmed it.

## Actual validation

All test commands used local Python 3.11 with the existing offline runner,
disposable PGlite and fake providers. External networking and real psycopg2 were
disabled; no paid model/API workload or production database was used.

| Runner invocation | Actual result |
|---|---|
| review/run_memory_validation.py targeted --path tests/test_candidate_producer.py --path tests/test_candidate_producer_projection.py --label solo-targeted | 71 passed, 95 subtests; no failures/errors/skips |
| review/run_memory_validation.py targeted --label solo-memory-suite | 328 passed, 372 subtests; no failures/errors/skips |
| review/run_memory_validation.py full --label solo-final-full | 1672 passed, 1127 subtests; 57 existing failure reports across 56 unique failed tests; no errors/skips |

Full-suite exit status is 1. Comparison against the preceding full run in the
same Git environment (1668 passed, 57 reports / 56 tests) found no new failure,
lost prior pass, changed failure-report count or changed normalized exception
signature. Four newly passing methods cover the solo correction. The original
811c0f0 archive run has six additional reports from two tests requiring Git
metadata; those environmental differences are not claimed as repair benefits.

Reproduction tests failed before their fixes: missing safe decoder details;
missing shrinking/isolation; total retry48 incorrectly skipped49; and successful
size1 incorrectly restored8. Final permanent coverage includes all four parse
codes in shadow/live modes, three actual solo failures, non-parse exclusion,
queue progress, version reprocessing, transaction/unknown-commit handling,
rollback-safe cleanup and zero-authority writes. All 50 memory_candidate gate
tests and four shared-parser architecture tests pass in the memory suite.

Governance: classify_change YELLOW; audit_diff PASS for memory, generation and
workflow-governance; generated review NEEDS REVIEW; git diff --check and final
patch reverse-apply checks passed. Raw execution logs, intermediate reports and
comparison JSON are retained in the local review workspace.

## Remaining limits

- Production recovery/24-hour metrics and real PostgreSQL replica concurrency or
  disconnect behavior remain unverified until separately authorized observation.
- The 57 existing full-suite failure reports are outside this fix.
- Success/skip removes normal solo entries. A source externally revoked after a
  solo failure may retain an ID/integer entry if it never enters another batch;
  extending purge/version protocols is outside the requested cleanup policy.
- Quota and cooldown are unchanged. After reset, subsequent failure cooldowns
  are 30s, 60s, then 1800s from the third failure. Approximately 90 minutes after
  three large-batch failures reaches the first solo attempt, not guaranteed success.
- Prompt constraints are probabilistic. Provider JSON schema support and
  shadow/live switching remain separate decisions.
