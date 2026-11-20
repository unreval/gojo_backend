# Gojo Architecture Invariants

这些是不允许普通 Bug Fix 悄悄改变的系统级约束。

如果修复需要改变其中任何一条，
必须升级风险等级并重新做 Change Capsule。

---

## 1. Authority

### INV-AUTH-001 — Relationship authority

系统只能存在一套权威的 relationship state / relationship judgment 链。

禁止：

- 为修 Bug 新建第二套 relationship brain；
- 在 generation 层保存另一套关系判断；
- 用 display text 或 prompt 内容作为隐藏 relationship state。

---

### INV-AUTH-002 — Durable memory authority

Durable memory 必须通过统一的权威写入路径。

禁止：

- 新建平行 durable-memory store；
- 绕过现有 validation / provenance gate；
- 因为读取旧数据困难而复制一套新的 truth store。

---

### INV-AUTH-003 — Canonical event authority

Canonical raw event 是事实来源之一。

派生状态不得反过来伪造或替代原始事件。

---

## 2. Evidence and Provenance

### INV-EVID-001 — Generated text is not evidence

Assistant / generator 自己生成的文本：

不能仅因为被生成，
就成为 cognitive evidence。

---

### INV-EVID-002 — Durable writes require provenance

所有 durable write 必须能够追溯到有效 canonical source。

至少需要能够回答：

- 来源是什么；
- 来源是否仍 active；
- 哪个判断使用了该来源；
- 来源撤回时如何失效。

---

### INV-EVID-003 — Revoked evidence loses authority

如果 canonical source：

- 被撤回；
- 被删除；
- 被判定无效；
- 被 supersede；

则依赖它产生的 derived authorization
不能继续仅依靠这个 source 保持有效。

---

## 3. Cognitive Loop

### INV-COG-001 — Fast Loop has no LLM authority

Fast Loop 的权威判断不能依赖 LLM。

LLM 可以参与表达，
但不能成为 authoritative decision maker。

注：`thinking_fast` / `thinking_slow`（思维链）不是这里说的 Fast/Slow Loop，不写认知权威，见 INV-THINK-*。

---

### INV-COG-002 — Slow Loop uses evidence pipeline

Slow Loop 不得绕过：

canonical evidence
→ validation
→ deterministic policy / revision

直接写入权威认知状态。

注：`thinking_fast` / `thinking_slow`（思维链）不是这里说的 Fast/Slow Loop，不写认知权威，见 INV-THINK-*。

---

### INV-COG-003 — Slow Loop cannot directly mutate relationship authority

Slow Loop 可以：

- 产生候选分析；
- 产生问题；
- 触发重新评估；

但不能跳过正式 authority path，
直接改变 relationship authoritative state。

---

## 4. Identity

### INV-ID-001 — Display text is not canonical identity

以下内容不能作为 canonical identity key：

- 用户可见昵称；
- “用户”/“她”等显示文本；
- prompt 中的角色称呼；
- UI label。

Identity 必须依赖稳定的结构化 reference。

---

## 5. State Stores

### INV-STATE-001 — No parallel authoritative store

如果已有 canonical state/store：

优先扩展现有 authority。

禁止为了局部修复建立新的平行 authoritative store。

---

### INV-STATE-002 — Derived state must remain derived

cache、summary、projection、display model、generation context 等派生数据：

不能因为实现方便而升级为事实权威。

---

## 6. Restart and Persistence

### INV-PERSIST-001 — Restart must not rewrite truth

应用启动、worker 重启、部署重启：

不得无条件改写 canonical durable content。

初始化逻辑只能：

- 创建缺失结构；
- 补充安全默认值；
- 执行经过明确授权的 migration。

---

### INV-PERSIST-002 — Runtime cache is not durable truth

缓存丢失或重建不能改变真实 authoritative state。

---

## 7. Failure Semantics

### INV-FAIL-001 — Known failure and unknown result are different

明确失败：

例如请求确定未执行或已经回滚。

结果未知：

例如 timeout、connection loss、无法确认远端是否提交。

二者不得使用同一种状态恢复规则。

---

### INV-FAIL-002 — Fallback must not bypass safety gates

Fallback 可以恢复可用性，

但不得绕过：

- validation；
- provenance；
- authority；
- identity；
- destructive-operation protection。

---

## 8. Generation

### INV-GEN-001 — LLM is expression, not truth authority

Generation 层负责：

- 表达；
- 语言风格；
- response rendering。

Generation 输出不得直接决定：

- durable truth；
- relationship authority；
- canonical identity；
- evidence validity。

---

## 9. 表达层便利贴（view）

### INV-VIEW-001 — View notes are not authority

view 便利贴的内容由模型生成，不具权威。

不得写入 long_memory、bond_memory、cognitive_beliefs、cognitive_hypotheses，
也不得作为任何裁决、关系判断或记忆投影的证据。

---

### INV-VIEW-002 — One view note hangs on one live utterance

每张 view 便利贴必须且只能挂一条有效的用户原话。

原话被改或被删，便利贴即不可见。

---

### INV-VIEW-003 — View wording is the character's own thought

view 内容不得表述为「你说过」。

它只能是角色自己的想法。

---

### INV-VIEW-004 — View notes do not feed back

view 不回流。

G1–G9、候选投影、日记的事实部分不得读取它。

---

### INV-VIEW-005 — Chat does not read view notes unless switched on

聊天是否读取 view 便利贴由 STICKY_VIEW_IN_CHAT 决定，默认关闭。

---

### INV-THINK-001 — Thinking outputs are not authority

思维链的理解（thinking_understandings）和看法（character_views）由模型生成，不具权威。
不得写入 long_memory、bond_memory、cognitive_* 表、cognitive_sticky_notes，
不得作为 G1–G9、裁决、关系判断、记忆投影的证据。

---

### INV-THINK-002 — Citations come from the given candidates

理解引用的对话、经历、看法，必须是本次调用里给出的候选，由代码检查。
不在候选里的引用使这一条理解作废，不影响同批其他条。

---

### INV-THINK-003 — View evidence is counted by utterances

看法的独立证据按用户亲口说过、仍然有效的原话去重计数，不按理解计数。

---

### INV-THINK-004 — Views move one level at a time

看法强度一次最多变化一级。

---

### INV-THINK-005 — No transaction or lock during model calls

思维链调用模型期间不开数据库事务、不持有锁。

---

### INV-THINK-006 — Prompts carry no character name or prescribed reaction

思维链提示词里不出现任何角色名或角色 id（角色名只从角色配置注入），
不出现"遇到 X 要 Y"这类规定反应的话。

---

### INV-THINK-007 — Nothing reads thinking outputs unless switched on

聊天、日记、便利贴、记忆召回不读取理解和看法。以后要读，必须新增默认关闭的开关。

---

## Change Rule

如果某项修改需要改变这些 invariant：

1. 停止普通 Bug Fix；
2. 标记具体 INV 编号；
3. 风险至少升级为 YELLOW；
4. 涉及 authority / provenance / schema / production persistence 时升级为 RED；
5. 明确告诉用户为什么旧 invariant 必须改变；
6. 获得需要的批准后才能实施。