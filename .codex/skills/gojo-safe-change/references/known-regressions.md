# Gojo Known Regressions

本文件记录 Gojo 项目中已经实际发生过的重要回归或事故。

目的不是保存 Bug 日记。

目的为：

1. 防止同类问题再次出现；
2. 将严重事故逐步转化为永久 regression test；
3. 修改相关子系统时提醒 Agent 必须重新验证这些行为。

---

# Status Definition

## TESTED

当前仓库已经存在明确测试，
并确认测试覆盖此事故。

## NEEDS TEST

事故已经发生并已有修复，
但尚未确认存在足够的永久 regression test。

## OPEN

问题仍可能存在或修复尚未完整验收。

## HISTORICAL

旧事故，仅用于架构风险参考；
当前是否仍可复现尚未验证。

---

# REG-001 — Pure emoji / non-verbal reply protocol failure

## Area

Generation / Validator / TTS

## Historical failure

纯 emoji 或类似：

`😒`

`……`

可能被 generation protocol 当成异常输出，
或者进入不应该进入的语音处理路径。

## Required behavior

- 纯 emoji 可以作为合法可见回复；
- `……` 等非语言回复不能仅因为没有正文而被判生成失败；
- emoji / 非语言内容不应错误触发 TTS；
- generation validator 不应因为这类回复制造用户可见“回复失败”。

## Regression requirement

必须存在覆盖纯 emoji / 非语言回复的协议测试和 TTS 行为测试。

## Status

NEEDS TEST

---

# REG-002 — Generation protocol retry / envelope failure

## Area

Generation

## Historical failure

模型返回内容与 generation protocol / envelope 不完全一致时，
会直接导致回复失败，
而不是进行受控的协议纠正或重试。

## Required behavior

- protocol failure 与真实模型/API failure 必须区分；
- 重试不能改变 canonical conversation state；
- 失败重试不能制造重复 visible turn；
- validator 必须验证最终 envelope，而不是信任生成文本。

## Status

NEEDS TEST

---

# REG-003 — Character delete explicit failure leaves character blocked

## Area

Character hard delete

## Historical failure

永久删除请求明确失败，
例如明确 403 或确定已经回滚的 409 后，

前端仍可能保留：

- pending
- blocked

导致角色实际上没有被删除，
但也无法继续正常聊天。

## Required behavior

如果首次删除请求已经明确：

- 未执行；
- 被拒绝；
- 已回滚；

则原数据必须保留，
并允许安全取消失败尝试和恢复聊天。

## Status

NEEDS TEST

---

# REG-004 — Unknown delete request incorrectly unblocked by later failure

## Area

Character hard delete / failure semantics

## Historical failure

如果此前已有一次删除请求结果未知，

后续一次明确 403、
或查询返回 not_committed，

不能据此推断最早那个未知请求一定没有执行。

## Required behavior

KNOWN FAILURE
和
UNKNOWN RESULT

必须使用不同恢复规则。

结果未知的 destructive operation
必须继续保持保护状态，
直到 authoritative outcome 被确认。

Related invariant:

`INV-FAIL-001`

## Status

NEEDS TEST

---

# REG-005 — Audio cache path corruption blocks chat

## Area

Audio / TTS / Client cache

## Historical failure

音频缓存路径分块时，
曾可能切断 percent-encoded `%XX` 序列，

导致 Android 文件路径不可写。

同时局部 TTS/cache 异常曾向上冒泡，
使已经可以显示的文字回复也被表现成“连接失败”。

## Required behavior

- percent-encoded sequence 不得被错误拆断；
- 音频缓存失败不能阻止文字气泡显示；
- TTS 是附加能力，不是 visible text delivery 的必要条件；
- 音频目录清理不得制造并发写入破坏。

## Status

NEEDS TEST

---

# REG-006 — Startup process rewrites durable memory truth

## Area

Durable Memory / Persistence / Role View

## Historical failure

启动或迁移逻辑曾存在直接修改
`long_memory`
显示正文的风险，

例如为了角色视角转换直接 REPLACE 文本。

这会让展示迁移变成事实内容修改。

## Required behavior

启动、部署、worker restart
不得无条件重写 durable truth。

角色视角：

- 我
- 她
- user
- character

应该通过结构化 subject/reference 或 projection 处理，

而不是修改 canonical durable content。

Related invariants:

- `INV-ID-001`
- `INV-PERSIST-001`

## Status

NEEDS TEST

---

# REG-007 — Provider failure causes memory processing degradation

## Area

Memory jobs / Structured output / Provider failure

## Historical failure

structured-output provider 返回权限或 provider-level failure 时，

memory / rolling-summary jobs
可能无法正常产生预期结果，

并造成：

- candidate 存在；
- durable fact 没有正常产生；
- background job 持续失败或退化。

## Required behavior

Provider failure 必须：

- 明确记录；
- 不伪装成有效 memory result；
- 不破坏已经存在的 durable memory；
- fallback 不得获得新的事实 authority；
- read-only fallback 不能偷偷改写状态。

## Status

OPEN

---

# REG-008 — Derived memory mistaken for canonical truth

## Area

Rolling Summary / Episodic / Lifecycle Memory

## Historical risk

rolling summary、episode、sticky、diary、lifecycle
可能因为频繁进入 context，
被错误当作原始事实来源。

## Required behavior

这些内容始终是 derived state。

它们不能仅凭自己的文本内容：

- 建立 durable fact；
- 覆盖 Raw Event；
- 获得 relationship authority；
- 绕过 provenance。

Related invariants:

- `INV-STATE-002`
- `INV-EVID-002`

## Status

NEEDS TEST

---

# REG-009 — Schedule / online-state continuity regression

## Area

Schedule / Reply availability / Online state

## Historical issue

聊天持续进行时，
角色的在线/回复可用状态可能没有随真实互动及时调整，

或者 schedule 与实际聊天连续性发生偏离。

## Required behavior

Schedule authority 与聊天连续性需要保持明确边界。

任何 soft availability / online adjustment：

- 不得重写 canonical schedule history；
- 不得制造第二套 schedule truth；
- 必须能解释当前 availability 来自何处。

## Status

OPEN

---

# Regression Rule

任何任务如果修改到上述 Regression 的 Area：

1. 必须读取对应 REG；
2. 检查当前是否已有永久测试；
3. 如果已有测试，必须运行；
4. 如果事故已修复但没有测试，优先补 regression test；
5. 如果本次修改重新触发该事故，任务状态必须为 BLOCKED；
6. 不得因为“本次功能正常”而忽略历史 regression。

---

# Regression Index

| ID | Area | Status |
|---|---|---|
| REG-001 | Emoji / Generation / TTS | NEEDS TEST |
| REG-002 | Generation Protocol | NEEDS TEST |
| REG-003 | Character Delete Failure | NEEDS TEST |
| REG-004 | Unknown Delete Result | NEEDS TEST |
| REG-005 | Audio Cache / TTS | NEEDS TEST |
| REG-006 | Durable Memory Startup Rewrite | NEEDS TEST |
| REG-007 | Provider / Memory Jobs | OPEN |
| REG-008 | Derived Memory Authority | NEEDS TEST |
| REG-009 | Schedule / Online Continuity | OPEN |