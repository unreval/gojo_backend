# Deterministic cognitive evidence loop — first bounded slice

Base: `8e7797098c12101146bb1aa600a461a029ffb4e3`. This change is for an
independent review branch. It does not deploy, enable a production worker,
or call a model. `COGNITIVE_WORKER_ENABLED` now defaults to `false` and is
explicitly false in `.env.example`.

## Authority and existing entry points

The existing flow remains:

`chat_log -> cognitive_events -> cognitive_event_triggers -> cognitive_cycles
-> Validator -> cognitive_revision -> existing cognitive tables -> reader`

`extract_and_save_memory`, `ingest_question_update`, `ingest_v4_signals`, and
`relationship_engine.process_turn` all use the same canonical evidence ingress.
Copied request text, assistant prose, extracted question keys, model `novel`
flags, model signal labels, confidence labels, and embeddings do not decide
an issue's identity, resolution, belief, or prediction outcome.

`generate_cycle_output` produces an evidence worklist without calling its
legacy `create_chat_fn` parameter. The commit transaction checks that the
worklist contains exactly its claimed current evidence and reloads the source
messages. `persist_slow_loop_output` rejects external judgment candidates and
rejects attempts to opt out of deterministic persistence. Its historical
implementation is retained below the mandatory return for now, but cannot be
selected by callers. Existing output validation and queue lease/retry rules
remain in place.

The ordinary private-memory LLM extraction and relationship observer main
paths are intentionally replaced, not moved elsewhere. Free-form fact/bond
extraction, automatic convention backfill and inferred relationship deltas
from those paths are therefore not supported by this first slice. Existing
records are preserved; they are not reclassified from keywords. Standalone
group-memory summarization, expression/diary/rolling-summary generators and
manual historical backfill helpers still exist. They are not read as evidence
by the new cognitive loop and cannot bypass its mandatory write gate. This
is not a claim that every generative feature in the application is model-free.

## Explicitly supported language and scope

The parser recognizes complete literal **self-reports**, not hidden feelings:

- `我喜欢咖啡。`, `我不喜欢茶。`, `我讨厌你。`
- Bare objects: 咖啡 / 茶 / 甜食 / 辣食 / 独处 / 聊天 / 你.
- Other objects must be delimited, e.g. `我喜欢「直白地沟通」。`.
- Optional exact date: `在2026-09-30，我喜欢咖啡。`.
- Explicit correction: `更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。`.
- If the old quote has multiple eligible sources, require its canonical id:
  `更正事件「old-source-id」：「我讨厌咖啡」不对，应为「我喜欢咖啡」。`.

The subject is bound to the canonical user. `你` is bound to that chat's
character. A correction must match the old source, full original clause,
subject, predicate, object, and exact time scope. An undated clause has the
explicit scope `unspecified`; it is never silently widened to all dates.
The original source must precede the correction and remain active. Raw source
rows are locked during application so deletion cannot race the state commit.

`我讨厌你才怪`, quoted/reported speech, questions, mixed clauses, implicit
intent, unsupported polarity, and malformed/ambiguous corrections become
pending issues. The parser does not claim to understand sarcasm. A bare
literal self-report is evidence only that the user reported it, and is stored
with that qualification. Confidence `0.8` is a fixed policy weight for this
literal attestation, not a calibrated probability of an internal feeling;
repeated reports never raise it. The legacy multi-source gate for inferred
durable beliefs is not repurposed to pretend these reports establish a
general personality or relationship truth.

Existing model-derived beliefs without a matching typed canonical scope are
not silently migrated or guessed from their prose. Corrections that cannot
identify an eligible source remain pending. Legacy data migration and broader
language coverage remain separate work, and are not represented as completed.

## Revision, dependencies and predictions

The old statement and evidence remain. The old event becomes `superseded`,
the old belief becomes `retracted`, and the question records its prior
judgment, correction source, and withdrawn beliefs. A replacement literal
report is stored separately.

Existing evidence references, `committed_from_hypothesis_id`, question links,
and metadata `basis_belief_keys` carry dependencies. Revision traverses belief
dependencies transitively, reopens dependent questions, rejects affected
hypotheses and invalidates affected prediction/action bases. Prediction
metadata preserves its prior outcome rather than rewriting history as if
the forecast had never existed. Sticky actions are archived, and cognitive
cycle/diary summaries are marked with `invalidated_by_event_id` and omitted
from current readers. Existing rolling summaries, pins and episode indexes
that cite the corrected source become superseded. Their storage paths share
the revision lock and source-validity check, so a late writer cannot make a
superseded source current again.

New predictions use the existing lifecycle and a narrow resolver:
`explicit_report_outcome`. The forecast is whether the next explicit report
in the same scope agrees with the last report. A later matching report can
fulfill or violate it; unrelated subjects, objects, dates, retrospective
corrections and model signal labels cannot. Expiration remains a real deadline
operation. No prediction settlement by itself proves a hidden state.

Each source event is processed once. The queue context contains current
events, not a bulk retrieval of prior beliefs, hypotheses, diaries or
embeddings. Idle time creates no event or cycle; the scheduler scans due
prediction deadlines only. Outcomes handled inside a cycle suppress a
redundant second pass over the same event. Old model spend cooldowns and daily
quotas cannot suppress or delay fresh canonical corrections.

## Offline acceptance and regression status

`tests/test_cognitive_deterministic.py` exercises the actual PostgreSQL SQL
through a disposable PGlite database, including canonical ingress, queue
claiming, commits, rollback/retry, correction chains, reader state, prediction
fulfillment/violation/expiration, source isolation, history preservation,
summary invalidation, late writes, and idle/repeated evidence.

The model and embedding entry points are replaced with fail-on-call test
stubs, and every guard is asserted to have zero calls. The offline runner
blocks external network connections and real psycopg2 connections. PGlite
runs over a local child process's standard input/output; no production
database, credentials, listener or worker is used. It verifies PostgreSQL
SQL/state behavior, but does not simulate multiple independent database
connections, so concurrent contention is not load-tested.

Install the optional test runtime in a scratch directory, then run from the
repository root in PowerShell:

```powershell
npm install --prefix work/pgtest --ignore-scripts --no-audit --no-fund @electric-sql/pglite@0.3.14
$env:COGNITIVE_WORKER_ENABLED='false'
$env:COGNITIVE_TEST_PGLITE=(Resolve-Path work/pgtest/node_modules/@electric-sql/pglite).Path
python tests/run_offline.py --repo . --pattern 'test_cognitive_deterministic.py'
python tests/run_offline.py --repo . --full
```

The second command deliberately retains **all existing regression tests**.
Tests that require the removed model calls, model-authored writes, extractor
prompts, automatic memory merges or unconditional reflection are still
present and fail under the new contract. They have not been deleted, skipped,
or converted to expected failures. The full suite must not be reported as
green. See the delivered execution logs for exact counts. Those test contracts
and the wider memory/relationship feature coverage still require review and
migration before a deployment decision.

Recorded execution on this branch: targeted acceptance **26 tests, 0 failures,
0 errors**; full unchanged regression **907 tests, 118 failure records,
46 error records** (subtests can contribute multiple failure records). The
new acceptance cases also have no failures in that full run. The full suite
is explicitly **not green**; this branch is a bounded implementation for
review, not a completion claim for the wider existing feature set.
