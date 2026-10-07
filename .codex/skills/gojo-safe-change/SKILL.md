---
name: gojo-safe-change
description: >
  Gojo Backend 所有非简单代码诊断、Bug 修复、功能修改、重构、
  测试和代码审查必须使用的安全修改流程。
---

# Gojo Safe Change

本 Skill 的目标不是“尽快让报错消失”。

目标是：

1. 找到真正根因；
2. 只修改必要范围；
3. 不制造第二套权威状态；
4. 不顺手修改无关模块；
5. 用测试证明没有引入新的回归；
6. 让用户可以清楚审核修改内容。


## Authority Map

开始 Diagnosis 时，如果任务涉及已有架构领域，
必须读取：

`references/authority-map.md`

不要仅根据文件名、README、prompt 或旧报告猜测 authority。

如果当前代码与 Authority Map 不一致：

1. 以当前代码调查结果为准；
2. 标记 Authority Map 可能过期；
3. 在实施前报告差异；
4. 不得在 authority 不清楚时建立新的 state path。

## Known Regressions


如果任务涉及历史事故或修改到已有 Regression Area，
必须读取：

`references/known-regressions.md`

如果当前任务与某个 REG 匹配：

1. 在 Change Capsule 中列出 REG 编号；
2. 查找该 REG 是否已有永久 regression test；
3. 不得仅依赖旧修复说明判断它已经受到测试保护。

同时读取：

`references/regression-coverage.md`

必须先确认 Regression 的 owner repository。

如果 Regression 属于其它 repository：

不得因为当前仓库找不到测试，
就直接创建测试或判定整个项目没有覆盖。

报告中必须区分：

- static coverage；
- tests actually executed；
- owner repository。

---

# Phase 1：只调查，不修改

开始任务后，首先进入 Diagnosis。

此阶段：

- 可以阅读代码；
- 可以搜索调用关系；
- 可以检查 Git；
- 可以阅读测试；
- 可以运行不会产生生产副作用的诊断命令；

但是：

**不得修改生产代码。**

必须先回答：

## 1. 问题是什么

明确实际观察到的现象。

不要把推测当成事实。

区分：

- 已观察事实；
- 从代码得出的结论；
- 尚未验证的假设。

## 2. 执行路径是什么

找到真实执行链，例如：

用户请求
→ API
→ canonical event
→ evidence
→ validator
→ state
→ context
→ generation

或者实际涉及的其它链路。

## 3. Canonical Authority 是谁

必须确认：

这个行为最终由哪个模块、数据库表、状态机或服务负责。

如果已经存在权威实现：

**禁止另外建立一套平行实现。**

## 4. Root Cause 是什么

没有足够证据确认根因时：

继续调查。

不得因为“这个地方看起来可疑”就直接修改。

---

# Phase 2：生成 Change Capsule

开始修改前，必须先输出一个简短的 Change Capsule。

格式：

## Problem

当前问题。

## Root Cause

已经确认的根因。

## Canonical Authority

必须指出它与 `references/authority-map.md`
中的哪个 Domain 对应。

如果不对应任何已有 Domain，
必须说明为什么这是新的 authority，而不是旧 authority 的重复实现。
哪个模块是这件事情的权威来源。

## Allowed Scope

允许修改哪些模块和文件类型。

## Protected Invariants

哪些东西绝对不能因为本次修复发生变化。

## Required Regression Test

本次 Bug 修好以后，必须留下什么永久回归测试。

## Risk Level

GREEN / YELLOW / RED。

---

# 风险等级

## GREEN

满足以下特征时通常属于 GREEN：

- 只影响一个子系统；
- 不改变 canonical authority；
- 不改变数据库 schema；
- 不改变 public API contract；
- 不改变 worker 生命周期；
- 不改变 identity semantics；
- 不涉及生产环境；
- 不涉及永久删除；
- 修改范围较小。

GREEN：

可以继续实施，不需要用户逐步骤批准。

仍然必须执行测试和最终 Diff 审计。

---

## YELLOW

以下情况至少为 YELLOW：

- 同时影响两个或以上主要子系统；
- 修改 shared utility；
- 修改 cache semantics；
- 修改 background worker 行为；
- 修改 API 行为；
- 修改超过约 5 个生产代码文件；
- 实际修改范围明显超过原始问题；
- 修复过程中发现必须扩大 scope。

YELLOW：

完成 Diagnosis 和 Change Capsule 后，

如果用户原始请求没有明确授权扩大后的范围：

**停止实施，向用户报告原因。**

不要擅自扩大修改。

---

## RED

以下情况属于 RED：

- canonical authority 改变；
- relationship authority 改变；
- durable-memory authority 改变；
- provenance 语义改变；
- canonical raw event 语义改变；
- 数据库 schema / migration；
- 生产数据库操作；
- production backfill；
- 永久角色/账号删除机制；
- authentication / authorization；
- production worker 重要行为；
- merge main；
- deploy；
- force push。

RED：

**必须获得用户明确批准以后才能执行。**

---

# Phase 3：最小修改

确认 Change Capsule 后才能修改代码。

规则：

1. 一次只解决已经确认的根因。
2. 不顺手重构无关代码。
3. 不顺手修其它发现的问题。
4. 新发现的问题记录为 follow-up。
5. 优先修改现有 canonical path。
6. 禁止建立第二套实现绕过旧系统。
7. 一个逻辑单元修改完成后立即测试。

如果修改过程中发现：

实际需要修改的范围比 Change Capsule 大很多：

**立即停止。**

返回 Phase 2。

重新评估影响面。

---

# Phase 4：分层测试

不得一上来只跑完整测试然后宣布成功。

测试顺序：

## Layer 1 — Reproduction

证明原始问题确实可以复现。

## Layer 2 — Targeted Test

运行直接覆盖修改模块的测试。

## Layer 3 — Regression Test

如果 `references/known-regressions.md`
中存在对应 REG，
Completion Report 必须报告该 REG 的实际测试覆盖状态。

必须增加能够永久覆盖本次事故的测试。

严重 Bug 修过一次以后：

**不能只修代码而不留下回归测试。**

## Layer 4 — Integration Test

验证受影响链路整体仍然正常。

## Layer 5 — Architecture Invariants

运行项目架构不变量测试。

## Layer 6 — Broader Regression

前面通过以后，再根据影响面决定是否运行更大的测试集。

---

# 测试真实性规则

严格区分：

- 测试文件存在；
- 测试代码预计会通过；
- 测试实际执行并通过。

只有第三种才能写：

PASS。

如果测试因为环境、凭证、服务或依赖无法运行：

必须写：

UNVERIFIED

并说明原因。

不得写成通过。

---

# Phase 5：Diff Audit

# Automated Governance Gates

所有实际代码修改任务都必须运行仓库治理脚本。

这些脚本是 gojo-safe-change 的强制组成部分，
不需要用户每次另外提醒。

## Gate 1 — Change Classification

完成 Change Capsule 后，并在最终交付前再次运行：

`python ".codex/skills/gojo-safe-change/scripts/classify_change.py"`

该脚本提供 deterministic minimum-risk classification。

规则：

- 脚本只能提供最低风险；
- semantic review 可以提高风险；
- semantic review 不得因为脚本输出 GREEN 而降低已经确定的 YELLOW / RED。

如果脚本检测出的风险高于当前 Change Capsule：

必须采用更高风险等级。

---

## Gate 2 — Scope / Diff Audit

实现完成后必须运行：

`python ".codex/skills/gojo-safe-change/scripts/audit_diff.py" --allow-domain <DOMAIN>`

`<DOMAIN>` 必须来自已经批准的 Change Capsule。

可使用的 domain 名称包括：

- `raw-event/evidence`
- `relationship`
- `memory`
- `cognitive-loop`
- `generation`
- `schedule/proactive`
- `identity/role`
- `audio/tts`
- `workflow-governance`

如果 Change Capsule 明确允许多个 Domain，
则重复提供参数，例如：

`--allow-domain generation --allow-domain raw-event/evidence`

结果处理：

### PASS

可以继续 Completion Review。

### NEEDS_REVIEW

不得自动声称 SAFE。

必须检查敏感路径或新增行为，
并根据 semantic review 决定是否继续。

### BLOCKED

不得完成任务。

实际 Diff 已经超出批准范围。

必须：

1. 停止；
2. 报告超范围文件；
3. 返回 Change Capsule；
4. 获得需要的扩大范围批准后才能继续。

---

## Gate 3 — Review Report

完成 Diff Audit 后运行：

`python ".codex/skills/gojo-safe-change/scripts/build_review_report.py" --allow-domain <DOMAIN>`

使用与 Gate 2 相同的 approved domains。

默认报告：

`review/latest.md`

该报告是用户的快速审核入口。

不得把自动报告中的：

`UNVERIFIED`

改写成：

`PASS`

除非相关测试确实已经执行并成功。

---

## Governance Gate Rule

最终 Completion Report 必须同时报告：

- semantic risk；
- classifier result；
- diff audit result；
- review report status；
- actual executed tests。

如果这些结果互相冲突：

采用更保守的结果。

例如：

semantic risk = YELLOW
classifier = GREEN

最终风险仍然是：

YELLOW。

修改完成以后必须检查最终 Git Diff。

重点检查：

- 是否出现预期外文件；
- 是否增加新的数据库写路径；
- 是否增加新的 state store；
- 是否删除 validator / gate；
- 是否增加静默 fallback；
- 是否 swallow exception；
- 是否改变 API contract；
- 是否改变 schema；
- 是否改变 identity key；
- 是否增加 background behavior；
- 是否出现与本次 Bug 无关的重构；
- 是否改变 canonical authority。

如果 Diff 超出 Change Capsule：

不能直接交付。

必须调查原因。

---

# Gojo Architecture Invariants
详细的不变量定义与编号见：

`references/architecture-invariants.md`

当任务涉及 memory、cognition、relationship、evidence、
provenance、identity、persistence 或 generation authority 时，
必须读取该 reference。

以下规则必须长期保持：

1. 不建立第二套 relationship brain。
2. 不建立第二套 relationship authority。
3. 不建立第二套 durable-memory authority。
4. Generator 输出本身不是 cognitive evidence。
5. Durable write 必须拥有有效 provenance。
6. Fast Loop 的权威判断不得依赖 LLM。
7. Slow Loop 不得绕过 evidence pipeline。
8. Slow Loop 不得直接修改 relationship authoritative state。
9. Display text 不得作为 canonical identity key。
10. 已存在 canonical store 时，不建立平行 authoritative store。

---

# 旧问题处理原则

如果调查过程中发现另一个 Bug：

不要顺手修复。

记录：

FOLLOW-UP ISSUE

包括：

- 现象；
- 涉及模块；
- 严重程度；
- 是否阻塞当前修复。

除非它直接阻止当前问题的正确修复，否则留到独立任务处理。

---

# Baseline Failure

如果开始修改以前测试本身已经失败：

记录为：

PRE-EXISTING FAILURE

不要把它伪装成本次修改导致。

反过来：

如果修改以后新增失败：

视为：

NEW REGRESSION

必须停止完成流程并调查。

---

# Phase 6：Completion Report

完成后不要输出长篇流水账。

必须使用下面结构：

## Status

SAFE / NEEDS REVIEW / BLOCKED

## Risk

GREEN / YELLOW / RED

## Root Cause

一句到几句说明真正原因。

## Canonical Authority

本次涉及的权威模块。

## Files Changed

分别列出：

- Production
- Tests
- Docs / Config

## Behavior

Before:

修改前行为。

After:

修改后行为。

## Architecture Impact

明确写：

- Canonical authority: changed / unchanged
- Relationship authority: changed / unchanged
- Durable memory authority: changed / unchanged
- Provenance: changed / unchanged
- Database schema: changed / unchanged
- API contract: changed / unchanged
- Worker lifecycle: changed / unchanged
- Identity semantics: changed / unchanged

## Tests

列出实际执行命令和真实结果。

例如：

Targeted:
18 passed

Architecture:
34 passed

Regression:
91 passed

## Regression Protection

列出本次新增的永久回归测试。

## Remaining Risks

仍然存在的风险。

## Git State

必须明确：

- current branch
- commit created or not
- pushed or not
- main changed or unchanged
- deployed or not

---

# Git Safety

除非用户明确授权：

不得：

- merge main；
- force push；
- deploy；
- 修改生产数据库；
- production backfill；
- 删除分支；
- 丢弃无关本地修改；
- 为测试调用付费模型/API。

未经明确要求：

不要 commit。

未经明确要求：

不要 push。