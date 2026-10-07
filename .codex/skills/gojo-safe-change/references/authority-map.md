# Gojo Backend Authority Map

本文件记录当前代码中已经确认的 canonical authority、
派生状态和已知 authority 风险。

它不是产品需求文档。

修改代码前，如果任务涉及对应 Domain，
必须先核对这里记录的 authority 是否仍与代码一致。

如果代码与本文冲突：

以当前实现调查为准，并更新本文。

---

# 1. Canonical Raw Event

## Authority

PostgreSQL:

`chat_log`

主要入口：

- `raw_events.py`
- `db_chatlog.py`
- `route_chatlog.py`

## Meaning

`chat_log` 是当前 canonical raw-event ledger。

Raw Event 是否仍有效由：

- event identity
- active state
- tombstone / deletion state

共同决定。

## LLM authority

NONE

## Derived

以下不是 Raw Event authority：

- `short_memory`
- processing metadata
- turn aggregation
- annotations

## Confidence

HIGH

---

# 2. Evidence / Provenance

## Authority

事实来源有效性：

`chat_log`

Cognitive evidence / adjudication：

`cognitive_events`

相关实现：

- `cognitive_events.py`
- `cognitive_revision.py`
- `memory_authority.py`
- `cognitive_db.py`
- `raw_events.py`

## Provenance links

`memory_source_events`

只负责：

memory → source event

链接。

它不是独立事实源。

## LLM authority

NONE

## Important rule

Derived evidence 不能脱离有效 canonical source
独立获得事实 authority。

## Confidence

HIGH

---

# 3. Relationship

Relationship 当前不是一个单字段 authority，
而是明确分层。

## Numeric relationship state

Authority:

`rel_state`

负责数值型关系账本。

## Declared stance

Authority:

`rel_declared_stance`

负责带来源的明确表态。

## Semantic relationship cognition

Authority:

Cognitive:

- questions
- beliefs
- hypotheses

负责窄语义认知结论。

## Important rule

以上是不同职责范围。

不得因为它们同时存在就创建新的统一
“relationship brain”。

不得从数值状态直接推导未经证据确认的关系语义。

## Derived

- relationship labels
- legacy/debug projections
- offline short-term continuity state

都不是新的 relationship authority。

## Current ambiguity

旧 relationship mutator 仍存在于代码中。

当前扫描未发现它们构成正在运行的第二套
relationship semantic authority。

需要持续检查生产调用关系。

## LLM authority

NONE in current authoritative path

## Confidence

MEDIUM

---

# 4. Durable Memory

## Authority

事实是否成立最终依赖：

- valid Raw Event
- Cognitive adjudication
- required stable belief
- provenance

## Projection stores

- `long_memory`
- `bond_memory`

在 canonical path 中属于可召回投影，
不是独立事实裁决者。

主要实现：

- `memory_authority.py`
- `cognitive_revision.py`
- `user_memory.py`
- `smart_recall.py`
- `route_memory.py`

## Legacy write warning

仍存在多个物理写入入口。

Legacy/generated rows 可以物理存在，
但不应自动获得 authoritative recall 权限。

任何修改都必须检查：

`authoritative_memory_sql`

及 recall authority gate 是否仍生效。

## LLM authority

NONE for authoritative facts

## Confidence

MEDIUM

---

# 5. Episodic / Rolling Summary / Lifecycle

## Canonical source

Raw Event:

`chat_log`

## Derived stores

包括：

- rolling summary
- episodic memory index
- lifecycle memory
- diary
- sticky note
- embeddings
- retrieval text

这些均不得替代 Raw Event 或 Cognitive truth。

## Rolling Summary

LLM authority:

CANDIDATE ONLY

摘要可以参与 context，
但不是事实 authority。

## Known ambiguity

`cognitive_diary_entries`
与
`char_diary`

存在 recall 来源重叠。

当前没有证据证明它们形成事实 authority 冲突。

## Confidence

MEDIUM

---

# 6. Fast Loop

## Authority

Canonical Raw Event ingress
+
Cognitive trigger enqueue

主要实现：

- `raw_events.py`
- `memory_jobs.py`
- `cognitive_events.py`
- `cognitive_queue.py`

## LLM authority

NONE

## Important rule

Fast Loop 不允许 LLM 成为 authoritative judgment source。

## Confidence

HIGH

---

# 7. Slow Loop

## Current authority

主要执行链：

`cognitive_worker`
→ `cognitive_revision.apply_rule_evidence`
→ Cognitive state

主要实现：

- `cognitive_worker.py`
- `cognitive_queue.py`
- `cognitive_revision.py`
- `cognitive_output.py`
- `cognitive_reader.py`

## Current behavior

当前 worker 使用 deterministic processing。

当前扫描确认：

`model_calls: 0`

旧 structured-output parsing helper 仍存在，
但不属于当前 Slow Loop worker 主路径。

## LLM authority

NONE

## Important rule

Slow Loop：

- 不得绕过 evidence pipeline；
- 不得直接改 relationship authority；
- 不得让外部 LLM judgment 直接成为 persistent truth。

## Confidence

HIGH

---

# 8. Generation

## Responsibilities

Generation 负责：

- response generation
- expression
- structured candidate intent
- generation receipt
- side-effect request

## Authority boundary

可见 assistant text：

EXPRESSION ONLY

Structured business intent：

CANDIDATE ONLY

最终业务状态必须由对应 Domain writer
重新验证和提交。

主要实现：

- `route_chat.py`
- `generation_contract.py`
- `generation_effects.py`
- `db_generation_receipt.py`
- `raw_events.py`

## Important rule

Assistant text 可以成为：

“角色确实说过这句话”

的 quoted utterance 记录。

但不能因此证明：

“这句话描述的外部事实是真的”。

## Confidence

MEDIUM

---

# 9. Schedule / Phone Check / Proactive

## Schedule authority

- `char_schedule`
- `char_schedule_phase`

决定计划事件、阶段和 world availability。

## Phone-check authority

`char_phone_check`

负责：

- occurrence
- claim
- completion lifecycle

## Proactive message

`proactive_msg`

负责主动消息状态。

## Proactive promise

`proactive_promise`

负责主动承诺。

主要实现：

- `db_schedule.py`
- `schedule_engine.py`
- `schedule_transition.py`
- `phone_check_occurrence.py`
- `reply_availability.py`
- `proactive_msg.py`
- `db_promise.py`
- `promise_detector.py`

## LLM authority

Schedule / promise structured output:

CANDIDATE ONLY

Proactive message text:

EXPRESSION ONLY

## AUTHORITY CONFLICT — AC-001

当前代码扫描发现：

同一回复可能同时：

1. 产生结构化 `proactive_promise`
2. 正文命中 `promise_detector`

两条 effect path 使用不同 occurrence key，
但最终都可能写入：

`proactive_promise`

因此存在重复/竞争写入风险。

当前阶段：

KNOWN ISSUE

不要在其它 Bug Fix 中顺手修改。

需要独立 Diagnosis 和 regression test 后再修。

## Confidence

Schedule / phone ownership:

HIGH

Promise multi-write semantics:

MEDIUM

---

# 10. Identity / Role

## Canonical identity

依赖结构化 ID：

- `user_id`
- `character_id`
- `chat_id`
- event ID
- group/message ID
- typed subject refs

语义主体示例：

- `user:<id>`
- `character:<id>`

主要实现：

- `characters.py`
- `raw_events.py`
- `assistant_turn.py`
- `cognitive_revision.py`
- `role_view.py`

## Not authority

以下只是 display projection：

- nickname
- “用户”
- “她”
- “我”
- role-view text

不得用 display text 作为 canonical identity key。

## Known boundary

当前后端扫描不足以确认外部 authentication identity
的最终 canonical authority。

不要对此做未经验证的推断。

## LLM authority

NONE

## Confidence

MEDIUM

---

# Known Authority Risks

## AC-001 — Proactive promise competing writers

Status:

OPEN / NOT FIXED

Risk:

Two generation-effect paths may persist the same logical promise
using different occurrence keys.

Do not fix as an unrelated side effect of another task.

---

# Quick Reference

| Domain | Authority | LLM Authority | Confidence |
|---|---|---|---|
| Raw Event | `chat_log` | NONE | HIGH |
| Evidence | Raw Event + Cognitive adjudication | NONE | HIGH |
| Relationship numeric | `rel_state` | NONE | MEDIUM |
| Relationship semantics | Cognitive state | NONE | MEDIUM |
| Durable memory | Cognitive authority + gated projection | NONE | MEDIUM |
| Episodic / summary | Derived from Raw Event | CANDIDATE ONLY | MEDIUM |
| Fast Loop | deterministic ingress | NONE | HIGH |
| Slow Loop | deterministic Cognitive revision | NONE | HIGH |
| Generation | domain candidate / expression | CANDIDATE / EXPRESSION | MEDIUM |
| Schedule | schedule event + phase | CANDIDATE ONLY | HIGH |
| Phone Check | phone occurrence state machine | NONE | HIGH |
| Identity | structured IDs / typed refs | NONE | MEDIUM |