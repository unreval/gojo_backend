# Gojo Regression Coverage

本文件记录 `known-regressions.md` 中历史事故的当前测试覆盖状态。

注意：

`COVERED` 表示已经静态确认存在直接测试和相关断言。

它不表示本次审计实际运行了这些测试。

测试只有实际执行成功后才能报告为 PASS。

---

# Coverage Status

## COVERED

已经确认存在直接覆盖核心事故的测试。

## PARTIAL

存在相关测试，但仍缺少事故要求的一部分关键保证。

## NO TEST FOUND

在正确的 owner repository 中完成搜索后仍未找到直接测试。

## EXTERNAL REPO

该事故主要属于其它 Gojo repository。

当前 repository 的搜索结果不能用于判断整个项目是否有覆盖。

## UNKNOWN

目前证据不足。

---

# Repository Ownership

Gojo 是多仓库项目。

测试必须放在真正拥有该行为的 repository。

不要为了让当前仓库“看起来有测试”，
把客户端、后端或桌面职责的测试放错位置。

---

# REG-001 — Pure emoji / non-verbal reply

## Owner

gojo_backend

## Coverage

COVERED

## Direct tests

- `test_chat_commit_gate.py`
- `test_nonverbal_tts.py`

Confirmed behaviors include:

- pure/non-verbal visible reply acceptance;
- malformed protocol output is rejected;
- non-verbal response does not invoke TTS/provider;
- non-verbal content does not become inferred authority.

## Execution status

NOT RUN IN COVERAGE AUDIT

---

# REG-002 — Generation protocol / envelope

## Owner

gojo_backend

## Coverage

COVERED

## Direct tests

- `test_chat_commit_gate.py`
- `test_generation_temporal_hotfix.py`
- `test_generation_receipt.py`

Confirmed behaviors include:

- controlled protocol retry;
- failed generation does not commit assistant turn;
- retry does not duplicate source/user event;
- successful retry appends assistant exactly once;
- same source replay does not regenerate unnecessarily.

## Remaining boundary

Other generation endpoints have not yet been proven to use identical
envelope/retry semantics.

## Execution status

NOT RUN IN COVERAGE AUDIT

---

# REG-003 — Explicit character delete failure remains blocked

## Primary owner

gojo_simple / gojo_pub client deletion workflow

## Backend audit result

EXTERNAL REPO

No direct test was found in `gojo_backend`.

This must not be interpreted as project-wide `NO TEST FOUND`.

## Required external verification

Verify in the owner repository that tests cover:

- explicit 403;
- rolled-back 409;
- clearing failed pending state;
- clearing failed blocked state;
- original data remains intact;
- normal chat resumes.

---

# REG-004 — Unknown delete result incorrectly unblocked

## Primary owner

gojo_simple / gojo_pub deletion state machine

## Backend audit result

EXTERNAL REPO

## Required external verification

Verify owner-repository coverage for:

- prior unknown request;
- later explicit 403;
- later `not_committed`;
- later failure must not clear protection for earlier unknown result;
- restart preserves safe state.

Related invariant:

`INV-FAIL-001`

---

# REG-005 — Audio cache path corruption blocks visible chat

## Primary owner

gojo_simple client audio/cache layer

## Backend audit result

EXTERNAL REPO

Backend TTS tests are not sufficient to prove client cache correctness.

## Required external verification

Verify owner-repository tests for:

- `%XX` encoded sequence is never split incorrectly;
- cache write failure returns a non-fatal result;
- visible text remains available when audio cache fails;
- audio cleanup/write ordering is safe.

---

# REG-006 — Startup rewrites durable memory truth

## Owner

gojo_backend

## Coverage

COVERED

## Direct tests

- `test_memory_init_restart.py`
- `test_memory_projection_audit.py`
- `test_role_view.py`

Confirmed behaviors include:

- repeated initialization preserves canonical projection;
- process restart preserves raw/canonical content;
- old startup rewrite pattern is detectable;
- audit does not silently rewrite authority;
- role view is rendered rather than written into canonical truth.

## Execution status

NOT RUN IN COVERAGE AUDIT

---

# REG-007 — Provider failure degrades memory processing

## Owner

gojo_backend

## Coverage

PARTIAL

## Existing tests

- `test_provider_errors.py`
- `test_rolling_summary_structured.py`
- `test_memory_structured_output.py`

Existing coverage proves:

- 401/403 fail appropriately;
- transient provider errors retry;
- invalid summary does not project;
- invalid extraction does not receive authority.

## Missing coverage

Need an end-to-end failure test proving together that:

1. existing durable memory remains recallable;
2. failed provider result creates no authoritative write;
3. fallback does not alter fact authority;
4. background processing can later recover correctly.

## Execution status

NOT RUN IN COVERAGE AUDIT

---

# REG-008 — Derived memory becomes canonical truth

## Owner

gojo_backend

## Coverage

PARTIAL

## Existing tests

Examples include:

- `test_context_budget.py`
- `test_cognitive_deterministic.py`
- `test_episodic_index.py`
- `test_diary_recall.py`
- `test_grumble_cognitive_sticky.py`
- `test_memory_authority.py`

Existing coverage proves important individual boundaries for:

- rolling summaries;
- episodes;
- diary;
- sticky notes;
- canonical sources;
- relationship evidence.

## Missing coverage

No single system-level invariant currently proves that all derived-memory
classes are unable to independently create or overwrite:

- Raw Event truth;
- durable fact authority;
- relationship authority.

Candidate for architecture-invariant testing.

## Execution status

NOT RUN IN COVERAGE AUDIT

---

# REG-009 — Schedule / online continuity

## Owner

gojo_backend

## Coverage

PARTIAL

## Existing tests

- `test_schedule_world_contract.py`
- `test_read_receipt.py`
- `test_schedule_reply_state.py`
- `test_delayed_reply.py`

Existing coverage proves:

- recent reply can alter phone-check timing;
- current world state affects delayed-reply decisions;
- failed generation does not consume pending inbox state.

## Missing coverage

Need end-to-end proof that:

1. a real reply timestamp affects subsequent availability/check timing;
2. continuity adjustment does not rewrite canonical schedule history;
3. continuity does not create a second schedule/availability authority.

## Execution status

NOT RUN IN COVERAGE AUDIT

---

# Current Coverage Matrix

| REG | Owner | Coverage |
|---|---|---|
| REG-001 | gojo_backend | COVERED |
| REG-002 | gojo_backend | COVERED |
| REG-003 | gojo_simple / gojo_pub | EXTERNAL REPO |
| REG-004 | gojo_simple / gojo_pub | EXTERNAL REPO |
| REG-005 | gojo_simple | EXTERNAL REPO |
| REG-006 | gojo_backend | COVERED |
| REG-007 | gojo_backend | PARTIAL |
| REG-008 | gojo_backend | PARTIAL |
| REG-009 | gojo_backend | PARTIAL |

---

# Workflow Rule

When a change matches a REG:

1. read `known-regressions.md`;
2. read this coverage file;
3. identify the owner repository;
4. do not create tests in the wrong repository;
5. run existing direct regression tests when available;
6. report static coverage separately from executed test results;
7. if coverage is PARTIAL, determine whether the current change touches the uncovered boundary.