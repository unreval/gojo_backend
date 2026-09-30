# Deterministic cognitive evidence loop — canonical memory authority

Base main: `8e7797098c12101146bb1aa600a461a029ffb4e3`.
This iteration extends `93e0fa7afd8eb2af09bd0a1affe49704aa208b30` on
`codex/deterministic-cognitive-loop`. It does not deploy or enable a production
worker. `COGNITIVE_WORKER_ENABLED` remains false in code and `.env.example`.

## One authority path

The existing flow remains:

`chat_log -> cognitive_events -> cognitive_event_triggers -> cognitive_cycles
-> Validator -> cognitive_revision -> existing cognitive tables -> readers`

Private extraction, group extraction, question-update compatibility ingress
and `relationship_engine.process_turn` submit canonical source IDs to this
same path. Copied transcripts, model labels, model confidence and extracted
question keys do not decide an issue's identity, judgment or prediction.

Group messages, their raw event and a durable memory job are saved in the
same transaction, before generation. A user message is preserved even when
no character replies. Raw group metadata records the owner, audience and
explicit recipient; character messages record the actual speaker.
Unaddressed literal user facts use the existing `shared` bucket. A claim
about “you”, a boundary, refusal or commitment without an explicit group
recipient remains pending. Group clear/delete also retires the raw sources.

The worker constructs an evidence worklist without a model call. Persistence
reloads and locks the actual source, checks role/owner/chat/audience/text,
reparses the literal clause and commits judgments and memory projections in
one transaction. Retries cannot apply the same event twice.

The old implementation below the mandatory return in
`persist_slow_loop_output` has been deleted. It rejects external
question/belief/hypothesis/prediction/sticky/diary candidates and opt-outs
from deterministic persistence. Read-only schema validators remain for
compatibility; they contain no alternate judgment writer.

`relationship_engine` retains only the compatible turn-result shape and
canonical ingress. The old signal router, care/flirt/reciprocal handlers,
numeric updates and reappraisal writer have been removed. The pure historical
signal aggregation function lives in `relationship_legacy_readonly`, with no
production callers or database access. The old model-scored
`relationship_backfill` is now a read-only canonical-memory inspection CLI;
its scoring prompt, estimation and state-writing functions are removed.

## Long/bond/told provenance

`memory_authority.py` provides the projection writer and shared reader gates.
The existing long/bond tables gain `authority`, `authority_event_id` and
`authority_belief_key`, plus an event-unique projection index.
The DDL runs through the existing database initialization. It has been tested
on disposable PostgreSQL, not applied to production during this task.

All pre-existing rows and generic/model-created rows default to
`generated_recollection`. They are retained, but never automatically promoted
because their text looks plausible or has an attached source ID.
`save_long_memory` / `save_bond_memory` cannot select canonical authority.
Legacy merge/resolve/invalidate helpers cannot modify canonical projections.

An authoritative read requires all of:

- A canonical authority marker and active, unexpired memory row.
- An applied canonical event in the same user/character scope.
- A currently active raw source whose ID, owner, chat, role and text match.
- Exact agreement between projected content/table and the event adjudication.
- For user reports, an active stable belief with the same key and statement.
- For assistant speech, a witnessed utterance only, with no belief key.

Assistant speech is explicitly rendered as “角色实际说过 … 仅为话语记录，不证明话中内容或关系”.
It does not establish a user fact, a realized event, a relationship or a belief.
The group extractor no longer calls an LLM or stores model-proposed
`user_fact`, `told` or `char_bonds`.

The common gates cover the direct getters, vector candidate selection,
two-level recall, prompt assembly, context packs, diary generation and the
getters used by proactive/schedule sharing. Prompt assembly revalidates
candidate IDs **and their content**, including cached packs; an authority
badge alone is insufficient. Source-check failure excludes factual entries.
Prompt labels describe witnessed, scoped self-reports, never independently
verified real-world facts. Subjective diary/lifecycle recollections retain
distinct non-factual expression labels and cannot become cognitive evidence.

Correction marks old projections superseded. Retracted beliefs, changed or
deleted sources and late generic writes cannot restore them. Cognitive
prompt readers also recheck sources for typed literal beliefs, including
read compatibility with the prior deterministic commit before memory
projection metadata existed. This does not migrate untyped model beliefs.

## Supported literal language

This is a bounded compositional grammar, not general language understanding.
It accepts complete literal clauses; quoted values extend the vocabulary
without treating trailing prose as evidence.

| Capability | Example |
| --- | --- |
| Preference | `我喜欢咖啡。`, `我不喜欢「浓茶」。` |
| Explicit identity/fact | `我的职业是「教师」。`, `我的「宠物名字」是「小白」。` |
| Self-reported state | `我的状态是「很累」。` |
| Explicit refusal/acceptance | `我拒绝「深夜来电」。`, `我接受「邮件通知」。` |
| Explicit boundary | `不要「讨论体重」。`, `可以「用昵称称呼」。` |
| Exact date/range | `在2026-09-01至2026-09-30，我的居住地是「上海」。` |
| Commitment | `我承诺在2026-10-02前完成「寄明信片」。` |
| Matching fulfillment report | `我已兑现截至2026-10-02的承诺「寄明信片」。` |
| Scoped correction | `更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。` |

If multiple old sources match, a correction must specify the canonical ID:
`更正事件「old-source-id」：「我讨厌咖啡」不对，应为「我喜欢咖啡」。`

Supported unquoted preference objects remain 咖啡 / 茶 / 甜食 / 辣食 / 独处 /
聊天 / 你. Attribute names 名字 / 职业 / 居住地 / 状态 can be unquoted; other
attribute names and arbitrary values must be delimited.
Date ranges must be valid calendar dates and non-reversed. Correction requires
the same source chat, subject, predicate, object and exact time scope.
An undated clause has scope `unspecified`, not “always”.

Every committed report is qualified as a witnessed self-report. Fixed weight
0.8 measures the policy's attestation of the report, not a calibrated estimate
of hidden feelings. Repetition never increases it. Conflicting reports stay
under review pending a scoped correction; repeating the conflict cannot
silently resolve it. State memory projections have a 48-hour recall expiry
and retain their original report date; they do not assert the state persists.

Sarcasm, flirtation, implicit intent, mixed clauses, contextual yes/no,
ambiguous recipients, unsupported Japanese/free-form paraphrases and
unmatched corrections remain pending. This is not a keyword sentiment
classifier. No LLM label is used to fill these gaps.

## Revision and prediction lifecycle

The existing revision policy preserves old text/evidence and history while
retracting effective beliefs, following transitive dependencies, reopening
dependent questions, rejecting affected hypotheses and invalidating
prediction/action bases. Sticky actions are archived. Cognitive summaries
and derived context summaries/episodes with invalidated sources leave current
readers; the existing late-write checks remain.

The existing `explicit_report_outcome` resolver handles two bounded forecasts:

- Preference: whether the next explicit report in the same scope agrees.
- Commitment: whether a matching explicit fulfillment report arrives by the
  stated deadline. The date ends at the following midnight in UTC+08:00.

Repeated commitment text is neither fulfillment nor breach. Fulfillment
requires a prior commitment with the same task, date and source chat.
The earlier commitment remains historical; its active bond projection is
completed. A fulfillment report does not independently prove a real-world
action. Without matching evidence, the forecast can expire; the program does
not invent a violation from silence.

Source events are processed once. Idle time creates no synthetic reflection
cycle; scheduling checks due predictions only. Old model cooldown/spend
limits do not suppress new canonical evidence.

## Offline verification, 2026-09-30

Tests use disposable PGlite PostgreSQL via child-process standard input/output.
External network and real psycopg2 connections are blocked by
`tests/run_offline.py`. Cognitive model/embedding entry points are fail-on-call
mocks with zero-call assertions. No paid model or production service was used.

| Run | Tests | Failure records | Error records | Skips / xfails |
| --- | ---: | ---: | ---: | ---: |
| Original deterministic acceptance | 26 | 0 | 0 | 0 / 0 |
| New memory authority and literal-policy acceptance | 26 | 0 | 0 | 0 / 0 |
| Migrated diary/episodic reader modules, separate run | 52 | 0 | 0 | 0 / 0 |
| Related memory/relationship/context/diary regressions | 331 | 7 | 64 | 0 / 0 |
| Full suite | 933 | 95 | 60 | 0 / 0 |

Failure/error counts are records: a subtest can add more than one. The full
suite is **not green**. Every pre-existing test method in changed test files
was retained (267 methods checked); no test file was deleted and no new
skip/xfail was introduced. Related/full counts differ partly because old
tests replace the global db module during discovery, producing different
fixture failures when modules are run separately.

The remaining 155 full-regression records were classified by failing
location and original intent:

- 92 retain old model-call, model-authority, direct persistence, old return
  field or unconditional-reflection contracts.
- 32 concern unimplemented broader product semantics: multi-turn conventions,
  contextual yes/no, free-form/bilingual extraction and independent additive
  memory deltas. Several fail first at an obsolete fixture, so their full
  behavior has not been verified.
- 31 require migration of database/source/queue fixtures to the canonical
  transactional ingress. These are unresolved integration checks, not proof
  that all remaining product behavior is correct.

The delivered regression classification links every record to its full stack
trace. Partial test migration is not a claim of production readiness.

To reproduce with the optional local PGlite package:

```powershell
$env:COGNITIVE_WORKER_ENABLED='false'
$env:COGNITIVE_TEST_PGLITE='path/to/node_modules/@electric-sql/pglite'
python tests/run_offline.py --repo . --pattern test_cognitive_deterministic.py --pattern test_memory_authority.py --report acceptance.json
python tests/run_offline.py --repo . --full --report regression.json
```

The optional runtime used here is `@electric-sql/pglite@0.3.14`. The tests
exercise real PostgreSQL SQL, rollback and durable state changes, but do not
load-test independent concurrent database connections.

## Remaining work

- Restore broader multi-turn, bilingual and implicit semantics through
  explicit program rules and provenance, without restoring a model judge.
- Complete migration of old fixtures/contracts and inspect remaining
  behavior failures until the whole regression suite is green.
- Decide an evidence-backed migration strategy for historical model memories
  and untyped beliefs; no automatic text-based promotion was added.
- Review production schema migration and concurrent contention separately.
  This branch has not been deployed or merged, and production worker remains
  outside this task's execution scope.

## Source-first ingress and reply parsing follow-up

This follow-up starts from `cfb57c091482300bd934ebdc097158da4fc70ab8` on
`codex/deterministic-cognitive-loop`.

Private user ingress now follows this order:

1. Resolve a stable source event id.
2. Commit the canonical `chat_log` source and one `canonical_turn`
   `memory_jobs` row in the same transaction, under the existing pair lock.
3. Write the idempotent `short_memory` compatibility cache.
4. Generate the visible assistant response.

Source/job failure rolls back before cache or generation. Cache failure keeps
the durable source and job. Duplicate identities must match the existing active
source; retries cannot replace content or resurrect deleted sources. Provider
or parse failure leaves user evidence available to the deterministic worker
and creates no assistant source.

A successful receipt schedules the repairable assistant source/cache effect.
That source write owns the assistant's evidence job. New receipts no longer
schedule `private_extraction` and `relationship_update` for the same user
event. Existing named handlers remain for historical receipt/job repair and
still reload canonical evidence. Projection occurs only in the deterministic
cycle; assistant evidence records the actual utterance without proving its
claims.

The existing single `_generate_or_none` accepts safe raw speech on the current
attempt. Nonverbal content (`🥺`, `🥺...`, `...`, `……`) preserves both
language fields exactly and skips translation and punctuation normalization.
Structured responses retain the existing validation and truth guards. Empty,
broken JSON, internal/state output and provider failures can retry; a
deterministic truth-guard rejection can also retry. Accepted trace records add
`acceptance_mode=structured|plaintext|nonverbal`. Fallback carries no state or
machine intents, and does not schedule promise detection. TTS behavior is
unchanged.

Rolling summaries use the shared `structured_output.parse_structured_output`
with exactly the six existing fields, all required strings. Multiple distinct
roots and malformed/schema-invalid output fail closed. Jobs retain the safe
parse error code and log id, kind and attempt without raw text. Empty derived
summaries fail as `empty_summary`. The existing source-current gate remains;
it now also verifies all referenced raw events are active before a late
summary/episode can become current.

New acceptance runs use the same disposable PostgreSQL and offline guard as
the baseline above. SQL fixture migrations preserve unresolved positive
product expectations. Remaining old-contract and capability failures are
reported separately; this follow-up does not restore broader semantics by
calling a model judge.

Follow-up verification:

| Run | Tests | Failure records | Error records | Skips / xfails |
| --- | ---: | ---: | ---: | ---: |
| Deterministic hard acceptance | 52 | 0 | 0 | 0 / 0 |
| Migrated diary/episodic readers | 52 | 0 | 0 | 0 / 0 |
| Targeted generation/source/summary/authority regression | 413 | 0 | 0 | 0 / 0 |
| Full suite after fixture migration | 956 | 45 | 62 | 0 / 0 |

At the pre-closeout snapshot, the 107 remaining records comprised 79 old
model-authority contracts, 27 capability gaps and one historical-recovery
fixture migration. Compared with the 155-record baseline, 48 records have
cleared. Identity/subtest comparison confirms 98 persistent records, nine
failure-to-error transitions, zero error-to-failure transitions and zero newly
failing records. The error count is 60 - 7 resolved errors + 9 prior failures
now reaching result-contract errors = 62; aggregate counts alone are not proof. Of the original 31 fixture records,
26 cleared, four now reach unmet positive behavior expectations, and one still
needs a valid historical-recovery setup. Passing negative semantic checks does
not imply the corresponding positive capability is implemented.

All 386 pre-existing test methods in changed test files remain, including five
renames for the intentional-silence contract. The method bodies for all 29
distinct tests representing the original 32 capability records are unchanged.
No new skip/xfail was added. The optional PGlite adapter only adds PostgreSQL
parameter typing for standalone `IS NULL` placeholders; authority queries,
locks, commits and rollbacks still execute as SQL.


## Bounded canonical recovery and receipt closeout

The remaining historical recovery fixture now constructs real canonical user
evidence, deterministic adjudication and a completed receipt. Replaying a legacy
model duplicate candidate preserves exactly the same rows and projections.
Legacy candidates without sources do not become judgments; missing or invalid
historical adjudication reports a reason. Deleted/retracted sources and
superseded judgments cannot be revived. Source read errors propagate as errors.
Compatibility ingress success means evidence ingress, never recovered authority.

The three receipt windows are covered by real PGlite SQL in
`tests/test_receipt_windows.py`: no receipt commit keeps only user evidence;
an ASGI connection reset after commit replays the stored reply without a second
Generator call; a crash after the assistant effect's durable writes but before
effect completion repairs the same effect without duplicating raw/job/projection.
`completed` means server persistence, not client receipt or a user read.

The 79 legacy records retain their assertions and are mapped individually in
`COGNITIVE_CONTRACT_COVERAGE.json`, including the actual replacement tests and
unimplemented positive behavior. The 27 capability records remain explicit in
`COGNITIVE_CAPABILITY_GAPS.json`; their test method bodies are unchanged by this
closeout. A passing safety boundary does not establish the absent capability.

All SQL acceptance uses PGlite 0.3.14, one database and one borrowed connection
per fixture. Existing receipt owner/two-worker/heartbeat tests use Python
threads and a process-local locked store. Independent PostgreSQL connections
racing on source/job/receipt/effect boundaries remain unaccepted before release.
No mock or single-connection result is a concurrent PostgreSQL acceptance claim.
