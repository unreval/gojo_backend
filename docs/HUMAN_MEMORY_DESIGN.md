# 主观记忆层设计（Human Memory Design）

> 状态：设计提案，尚未实现或上线。
> 日期：2026-10-09（Asia/Singapore）。
> 源码核对基线：分支 `docs/readme-20261007`，HEAD `ba69717918cea2b3abeb8b3f07ecf0fad6c32ec0`。
> 本次交付：仅新增本文档；不改现有文件，不执行数据库迁移或真实数据库查询，不调用业务 LLM / Provider，不提交。
> 文中的阈值、频率和预算均为待验收的初始设计值，不是现有实现或科学结论。

## 1. 目标、依据与边界

角色应能自然淡忘日常细节，保留重要经历；多年后重新理解旧事，记得用户说过什么，以及当时有记录的角色感受和用户自述心情；允许回忆的情感色彩变得温柔，产生想念、遗憾和主动联系的动机。

系统要区分三件事：**事件是否发生过、角色能否想起、角色现在如何看待它。** 衰减影响可访问性和细节清晰度；重新评估影响现在的理解；二者都不能改写用户原话或 canonical 判断。

### 1.1 已阅读的资料与源码

- [README](../README.md)：项目目标、canonical 链、P0 调度和 Provider 风险。
- [持续心智架构](../GOJO_PERSISTENT_MIND_ARCHITECTURE.md)：分层记忆、Storage / Accessible Memory / Working Context、主观重解目标。
- [确定性认知契约](../COGNITIVE_DETERMINISTIC_V1.md)：当前事实权威、operation、来源快照、纠正与撤回。
- [Cognitive Loop v1](../COGNITIVE_LOOP_V1.md)：历史 Slow Loop、问题与便利贴契约。其 LLM worker 描述不是当前执行事实；当前 `cognitive_worker.generate_cycle_output` 走确定性处理，记录 `model_calls=0`。
- 建表与增量字段：`gojo_backend/db.py`、`db_bond.py`、`memory_lifecycle.py`、`raw_events.py`、`episodic_index.py`、`cognitive_db.py`、`context_layer.py`、`temporal_awareness.py`、`relationship_db.py`。
- 权威与接口：`memory_authority.py`、`cognitive_revision.py`、`cognitive_worker.py`、`smart_recall.py`、`recall_candidates.py`、`context_budget.py`、`relationship_panel.py`。
- 后台与 Provider：`ai_client.py`、`provider_error.py`、`memory_jobs.py`、`rolling_summary.py`、`db_schedule.py`、`delayed_reply.py`、`proactive_scheduler.py`。
- 仓库技能的 authority map、architecture invariants、known regressions 和 regression coverage。

以当前源码为接口依据。旧架构报告和 README 的状态、历史测试数字不等于本次验证结果。未核验生产 build、worker 开关、表结构或数据；1279 条旧记忆是用户提供的待审计数量。

### 1.2 不可突破的规则

1. `chat_log` 与现有 canonical 链不由主观层修改。合法删除、tombstone、纠正仍由原有流程处理；“账本不可改”不取消用户删除权。
2. 姓名、日期、承诺、明确纠正、拒绝和边界只能按 canonical 语义与时间范围读取，漂移恒为零。“永久准确”不表示旧事实永远仍是当前事实。
3. 用户说“我很难过”是有来源的自述；角色觉得用户难过只是主观印象。角色的编码感受和用户的实际心情不是同一字段。
4. 所有有效主观痕迹必须指向本用户、本角色可见范围内、存在且 active 的 canonical 用户事件。LLM 仅提出候选和表达。
5. 新 authority 类别只管理经过校验的主观投影，不裁决用户事实，不修改关系，不升级为第二套 durable-memory authority。
6. 逐条校验、逐条接受或拒绝；批次中一条错误不使其他合法条全部失效。跨条依赖也必须逐条验证。
7. Prompt 注入、生成文本、反刍和回忆次数都不是独立证据，不能让角色越回忆越确信用户做过某事。
8. 后台 LLM 的编码提议、整理、再评价、摘要、日记、日程候选及主动表达，全部使用同一个预算、断路器和退避入口。
9. 本提案不恢复 LLM 事实裁决，不开启 embedding / RAG，不另建关系、事实、问题或日程状态库。

### 1.3 范围与影响评审

设计缺口：现有 lifecycle 有 TTL、importance、strength 和召回权重，但没有统一的编码感受、清晰度计算、受限美化和修订回滚契约。

本次只有文档变化，风险为 GREEN；运行时行为、schema、API、worker 和 authority 均不改变。后续实现涉及 memory、raw-event/evidence、cognitive-loop、generation、schedule/proactive 多个子系统，包含 schema 扩展，应按 RED 变更分别诊断、评审和授权。验收必须覆盖来源失效、事实不漂移、零调用确定性路径、逐条接受和版本回滚。

关联既有风险：REG-006（启动重写记忆）、REG-007（Provider 失败）、REG-008（派生记忆升级为事实）、REG-009（日程连续性）。这些回归的 owner 是本后端；本次未执行测试，不把静态测试覆盖写成 PASS。

## 2. 权威模型与现有表映射

### 2.1 两种有效投影，一条 canonical 来源链

事实路径保持：

**chat_log → cognitive_events → Validator / deterministic policy → cognitive_revision → beliefs / questions → canonical long / bond projection。**

主观路径建议：

**同一 active 用户事件 → 现有证据与来源校验 → 受限主观候选 → 程序逐条校验 → lifecycle 痕迹版本 → Smart Recall 类型化上下文。**

主观写入是现有 memory authority 边界中的新增投影操作。复用 scope、active-source、operation、幂等和撤回规则；不自行形成新的用户 belief。提议进入现有持久任务机制，确定性 cognitive worker 仍不调用 LLM。

| authority / 类型 | 定义 | 可以做什么 | 不可以做什么 |
|---|---|---|---|
| `canonical_evidence_v1`（现有） | 已有 canonical 裁决及有效来源支持的事实／话语投影 | 读取带范围的用户自述、明确回答、原有 witnessed utterance | 将自述当成独立验证的现实事实；忽略撤回或纠正 |
| `subjective_trace_v1`（提议新增） | 程序验证过来源和字段边界的角色主观痕迹 | 管理访问强度、感受快照、当前评价、关联和主观表达 | 写用户事实、关系结论；成为 cognitive evidence；绕过 canonical gate |
| `generated_recollection`（现有） | 旧行／普通生成内容默认类别 | 保留原始记录供审计 | 因文字可信或有 source ID 自动晋升 |
| `legacy_recollection_unverified_v1`（提议新增隔离标签） | 来源无法验证的旧记录兼容投影，无有效记忆权威 | 低清晰度保存、受控查询与补确认 | 普通事实召回、引用用户原话、强化关系、驱动主动联系 |

`subjective_trace_v1` 不是 factual authority 的白名单扩展。`memory_authority.AUTHORITY` 和 `authoritative_memory_sql` 的事实条件继续只认原契约。新类别需明确的主观校验分支，最终 Prompt 仍重新验证来源。

### 2.2 表映射与保留策略

下表字段来自源码中的 CREATE TABLE 与 ALTER；不是生产 schema 核验。

| 现有表与关键字段 | 当前职责 | 本设计处理 |
|---|---|---|
| `long_memory`：id、user_id、character_id、content、category、timestamp；增量 authority、authority_event_id、authority_operation_id、authority_belief_key、projection_version、semantic_payload、source_event_refs、lifecycle_kind、recall_status、recall_weight、expires_at、superseded_by_id | 用户事实投影 | 保留 canonical 路径。旧行不改写；主观感受不塞入事实正文 |
| `bond_memory`：id、user_id、character_id、kind、content、timestamp；同类 authority / semantic 字段、recall_status、expires_at | between 投影和旧 told 兼容 | 保留；新 told 继续由 canonical fact 只读投影，不再复制事实；痕迹可关联 bond ID |
| `memory_lifecycle_items`：id、source_type、source_id、content、memory_kind、topic_key、status、source_event_refs、evidence_count、importance、recall_weight、expires_at、last_reinforced_at、superseded_by_id、long_memory_id、bond_memory_id、created_at、updated_at；增量 last_recalled_at、salience、strength、decay_state | 生命周期派生对象 | **扩展为痕迹当前投影的唯一物理存储**；新增 subjective_trace 类型，不另建 memory_traces 当前状态表 |
| `memory_source_events`：id、memory_type、memory_id、source_event_id、created_at；三元唯一键 | 记忆到事件的反向链接 | 保留，写 lifecycle 链接；只是链接，不证明来源存在或支持大意 |
| `episodic_memory_index`：episode_id、title、what_happened、outcome、unresolved、participants、source_event_ids、source_key、range_start/end、status、rebuild_required、processor_version、version、retrieval_text、embedding_metadata、superseded_by | 可重建事件索引 | 保留检索与来源范围；痕迹只关联 episode，不让其摘要成为独立证据 |
| `cognitive_beliefs`：belief_key、statement、confidence、status、belief_type、evidence_refs、metadata、cycle IDs | 当前认知结论 | 保留唯一认知裁决；主观 revision 不直写 belief，也不累计其 confidence |
| `cognitive_questions`：question_key、question_text、status、embedding_json、metadata、source_event_refs、cycle IDs | 未决问题唯一状态 | 保留；痕迹存 question_key 引用，不复制 open/resolved 权威 |
| `cognitive_sticky_notes`：note_key、content、status、source、source_event_refs、cycle IDs、expires_at、completed_at、viewed/viewed_at、user_hidden_at、user_visible、metadata | 工作提示与可见便利贴 | 保留既有可见性规则；主观层不直接生产用户可见 sticky，viewed 不等于 completed |
| `rolling_summaries`：summary_id、text、source_event_ids、range_start/end、status、token_cost；增量 processor_version、summary_version、superseded_by、rebuild_required、is_placeholder | 非权威压缩 | 保留；不能当痕迹编码感受或事实来源；通过统一后台执行闸门 |
| `temporal_awareness`：first_interaction_at、last_user_message_at、last_assistant_message_at、last_interaction_at、previous_interaction_at、last_gap_seconds、longest_gap_seconds、interaction_count、last_initiator、last_source | 时间线派生快照 | 保留只读接口；时间流逝不直接加减关系；以 canonical timestamps 校正 |
| `rel_state`：warmth、friction、intimacy、trust、attachment、commitment、passion、pending_passion、pending_hypothesis、banter_baseline、last_updated | 数值关系账本 | 保留；只读，主观层无写入权；语义关系仍由 cognitive state 与 declared stance 分层负责 |

本阶段不 DROP 上述任何表，不废弃 canonical 或已有正确的 episodic 检索。停止使用的是“旧生成记忆默认作为用户事实”“重复召回作为证据”“主观整理自动写 long_memory”这些路径；现有其他 lifecycle 类型保留原职责，新增痕迹禁止走旧 `_maybe_consolidate_candidate` 的事实写入分支。

### 2.3 痕迹字段与约束

逻辑“痕迹表”落在扩展后的 `memory_lifecycle_items`。以下标明复用和拟新增字段；不会在本任务执行 DDL。

| 字段 | 语义与约束 |
|---|---|
| id、user_id、character_id（复用） | 内部身份；显示称呼不参与去重和关联 |
| memory_kind（扩展） | 新增 `subjective_trace`；无来源兼容行用相同容器加隔离标签，不能通过有效痕迹 gate |
| authority、record_class（新增） | 主观有效投影／legacy 隔离候选；两类不得混读 |
| source_type、source_id、topic_key（复用） | stable operation 的幂等身份；不得由 LLM 自由命名事实或 question key |
| source_event_refs（复用） | canonical TEXT 事件 ID 的 required-source 引用；不是 cognitive BIGINT event ID |
| source_quotes（新增） | 每段 event_id、source_chat_id、role、start/end、text_hash；原文由账本重取，禁止把展示文本当校验原文 |
| source_snapshot_hash、source_status（新增） | 保存内容、身份、时间、元数据和上下文依赖的校验摘要；verified / unverified / invalidated；读时重新验证 |
| content（复用为大意） | 精确完整子句或确定性 canonical 模板；不持久化无法程序验证的自由事实改写 |
| semantic_refs、protected_refs（新增） | 指向既有 fact / operation / belief / question，标记姓名、日期、承诺、纠正与边界；不复制第二份当前事实 |
| occurred_at、encoded_at（新增） | 事件时间与首次编码时间分离；回填时不得冒充历史上已编码 |
| encoding_affect、affect_origin（新增） | 当时角色感受：受限 emotion 标签、valence、intensity、known 标志及快照来源；不存在当时记录则 unknown |
| original_affect（新增、不可变） | 首次合格编码感受的审计基线；unknown 不能被多年后的 LLM 猜测填成已知 |
| user_mood_refs（新增） | 只引用用户当时明确心情自述的 canonical operation；推测心情另列 subjective 标签 |
| emotion_intensity（新增） | [0,1]；表示角色感受强度，不能当用户心理事实或关系增量 |
| importance（复用） | [0,1]；按确定性规则给初值，关键事实／明确“记住”请求设保护；模型提议不能自行置顶 |
| recall_count（新增）、last_recalled_at（复用） | 已提交真实召回的统计，候选扫描不计；按 receipt / occurrence 去重 |
| strength（复用为稳定度）、last_reinforced_at（复用） | 仅新 subjective_trace 的 strength 定义为 [1,4] 半衰期倍率，只由新的合格外部证据强化；其他旧类型字段语义不变，旧行不原地归一化 |
| clarity_policy_version、decay_state（扩展） | 清晰度策略版本和可重建缓存；实际 clear / partial / gist_only / dormant 在读取时计算 |
| associations（新增） | 同范围 episode、bond、trace、topic 与 canonical refs；按 ID 关联，非事实因果推断，最多 16 项 |
| confidence（新增） | [0,1] 主观投影可用性，不是现实真值概率；仅时间或召回不能提高，未知感受不能被高分掩盖 |
| retrospective_appraisal（新增） | 现在看法的受限主题、valence、intensity；与 encoding_affect 分离 |
| beautification_drift、drift_limit（新增） | 当前美化偏移及允许上限，始终相对于 original_affect；事实字段上限为零 |
| revision、policy_version（新增） | 当前版本和确定性政策版本；并发更新要求预期 revision 相等 |
| legacy_ref、migration_batch_id（新增） | 旧表／旧行 ID／原内容 hash／迁移批次；不是 canonical source |
| status、expires_at（复用） | 继续使用 active/reactivated/superseded/expired/archived；隔离用 archived + source_status，不假造现有 CHECK 不支持的 quarantined 状态 |

新痕迹的 source_event_refs 使用带版本的 envelope：event_id、source_chat_id、user_id、character_id、role、operation_id、required、snapshot_hash；旧 JSON 对象引用用兼容 decoder 读取，不原地替换。关联链接复用 memory_type='lifecycle'。canonical TEXT source ID、cognitive BIGINT event ID、trace BIGINT ID 分开存放。importance 决定等级，salience 只用于局部排序，recall_weight 是策略权重，strength 只决定半衰期；不能四者互相累加强化。

单个痕迹最多 8 个 required 用户事件、8 个引用片段、300 字大意。超限分成有独立有效来源的多条；不能截断引用的否定、条件或纠正上下文。v1 不支持只有 assistant 来源的有效主观痕迹；角色话语仍由原有 witnessed-utterance 契约读取。

需要新增一个附属 `memory_trace_revisions` 审计表：trace_id、revision、previous_revision、变化前后允许字段、required_source_refs、规则／任务／批次 ID、原因、created_at、rollback_of。它只保存历史和撤销记录；当前读取权威仍是 lifecycle 行及其来源 gate，不在两处保存竞争的“当前”状态。状态更新与 revision 追加在同一短事务提交。

### 2.4 实现前必须解决的 schema 与读取风险

- lifecycle.id 是 BIGSERIAL，而现有 `memory_source_events.memory_id` 与 `derived_memory_idempotency.memory_id` 是 INTEGER；后续应一并扩为 BIGINT 并检查调用端，不能假设长期兼容。
- `memory_kind` 有 CHECK，需显式添加新类型；status CHECK 保持原值，source_status 独立表达失效和隔离。
- 现有 lifecycle reader 只取部分旧类型；仅将新类型加入 IN 条件不够，必须增加逐条 canonical 验证和最终 Prompt 验证。
- `sources_are_active` 不足以证明每个来源真实存在，空集合也不能代表有效。用 `get_active_events_by_ids` 重取完整来源，核对返回集合与 required 集合完全相等，再核 owner、scope、role、快照和引用。
- 现有删除失效逻辑可能保留剩余来源的多来源对象；对痕迹，任一 required source 失效即暂停旧版本，重新构造有效版本后才能恢复。
- 一些旧时间字段是无时区 TIMESTAMP。回填先记录其原时区规则，无法确定时标记 unknown；新策略计算统一使用 UTC 时间，展示与日界按 Asia/Singapore。

## 3. 1279 条旧记忆的可回滚处理

### 3.1 原行保留，兼容投影逐条建立

迁移不是文本清洗，更不是 REPLACE 原内容。现有 long / bond / lifecycle 行及其 content、authority、时间戳都不改写。先在未来获授权的离线快照上做 dry-run，记录表、主键、内容 hash、原来源链接、分类理由、目标投影 ID、policy_version 与 batch_id；清单保存在新投影元数据及其 revision 审计中。

1279 是预期审计总数；若真实快照数量不同，应先报告差异，不补造条目。分类计数之和必须等于实际快照总数，重复旧行也各有审计结果，但可指向同一去重痕迹。

| 分类 | 新投影处理 | 召回权限 |
|---|---|---|
| 来源存在、active、同范围、role=user，引用与完整语义通过 | 从账本重取大意和引用，建立 subjective_trace；关联原行与 canonical operation | 可以说“你当时说过……”并逐字引用；感受独立标注 |
| 有合格用户来源，但旧正文不受来源支持 | 保留旧行，拒绝旧正文升级；仅用能校验的原文子句建立痕迹，记录不一致 | 禁止把旧编造内容继续作为回忆 |
| 已删除／撤回、跨用户／跨角色的来源 | archived + source_status=invalidated，禁止读取；不复制无权读取的内容 | 新层普通 Prompt、旧印象查看和回滚均不能恢复；合法删除依原流程清除敏感派生副本 |
| 来源缺失／无法解码，且旧行本身属于已验证的合法范围 | 低清晰度 legacy 隔离候选，不赋 subjective_trace 权限 | 仅按3.2受控查询，不作为事实或主动联系依据 |
| 仅 assistant 来源 | 不建立用户来源主观痕迹 | 合格原话只能由原有 witnessed-utterance 契约读取，不转为用户事实 |
| 完全无来源链接 | legacy_recollection_unverified_v1 隔离投影，confidence ≤0.2，clarity=unknown_low | 仅用户主动查询旧印象、且产品允许时受控展示 |

没有编码时感受的旧记录一律 encoding_affect / original_affect=unknown。旧 importance 可作为审计参考，不能直接认定“重要事实”；旧 mention_count 不能换成 reinforcement。最初回填 recall_count=0，稳定度=1；事件日期来自合格源，不从旧句子推断。

### 3.2 无来源候选的措辞与再确认

必须承认：**无来源候选不是符合“每条指回账本”的有效痕迹。** 以下兼容查看只适用于旧行自身 owner/scope 合法、且没有明确撤回／删除禁令的无来源记录；已知失效或跨范围内容一律禁止新层读取。 它保留的是旧系统的记录，不是原事件证明。默认不进入普通聊天；允许用户主动查询时，先用固定提醒表达：

> “旧记录里有一个尚未核实的印象，来源已经对不上了。你愿意帮我确认吗？”

可展示为“旧系统记录内容”，不能说“你以前告诉过我”“我记得那天你……”或加引号冒充用户原话。涉及姓名、日期、健康、承诺、拒绝和关系的无来源内容不进入生成 Prompt，由专门查看界面标注未核实；不自动反复追问。

用户新的明确确认生成新的 canonical 用户事件，再经原有 deterministic 规则建立事实／新痕迹。新来源只证明此次确认，不能伪造过去那次对话确实存在。若用户纠正，按 canonical revision 处理；原旧行仍保留审计记录。

### 3.3 回滚与幂等

迁移版本由读取侧 batch allowlist 启用。回滚撤销该 batch 的读取资格、清除派生缓存，并归档其新投影；原行 hash 必须与 dry-run 清单一致。关闭新层后恢复原有 canonical reader，不能恢复“所有旧记忆都是真实事实”的不安全规则。

迁移重复执行复用稳定旧行引用和 canonical source/operation key；不得制造重复痕迹或提高 evidence_count。新审计历史保留用于核对。正式数据删除仍必须删除相关派生副本与审计中的敏感正文，审计不能成为绕过删除的备份库。

## 4. 写入与来源校验

### 4.1 编码流程

1. canonical 用户事件已经提交，复用现有 memory_jobs / processor identity 排入幂等任务；不阻塞对话的确定性事实处理。
2. 读取完整 required-source 集和可用的当时内部快照；LLM 不可用时，用精确完整子句和 canonical 模板编码。
3. 可选 LLM 仅提出 topic、受限感受表达、关联、当前评价候选；不决定来源角色、用户事实、重要事实保护或 canonical key。
4. 对每条候选独立执行 scope、source、role、snapshot、quote、field、protected-ref、幂等和 policy 校验，产出 accepted / deferred / rejected 及原因。
5. 网络结束后，在短事务内锁定所需源和当前 revision，重新校验 active 状态；同步写当前投影、source links、revision 和任务结果。任何一项提交失败则该条回滚。
6. 批次内其他合法条继续；一条有两个 required 来源时，两个都必须通过，不能“删掉坏来源但沿用原大意”。重构为较小独立痕迹必须重新校验。

现有 link_memory_sources 会自行 commit，未来实现不能直接复用其独立事务形成“痕迹成功而 provenance 失败”；需要同一事务入口。这是后续实现评审点，本次不改函数。

### 4.2 子串校验的必要性与不足

引用非空，必须与 canonical 原始 text 的字符区间逐字相同；不得先去标点、翻译、替换人称或 Unicode 归一化，再声称精确引用。重新读取时验证 hash 和必要上下文。

“我不喜欢苹果”包含“喜欢苹果”。因此子串出现只证明字符存在，不证明用户表达该事实。需保留完整子句中的否定、条件、反问、转述、说话人和纠正限定。用户输入“她说我喜欢苹果”不能成为用户本人喜好。

v1 的持久大意仅允许：

- 合格原文完整子句；
- 已由 canonical policy 验证的结构语义，经固定模板呈现；
- 不含新用户谓词的受限主观字段，如“现在更理解当时的处境”。

无法程序证明的大意改写只留作未接受提议，不进入有效痕迹或事实区。自由语言表达由最终生成层完成，引用块仍用精确来源回填；表达输出也不得反向作为证据。

### 4.3 编码时的心情

当时角色感受只接受当时已经保存、来源明确的模拟内部状态快照；没有快照时为 unknown，不能把 rel_state 数值、当时回复文本或后来模型判断冒充“真实原始感受”。

用户当时的心情只读取其有时间范围的 canonical 自述。其他推测若保留，只能标记“我的印象，未确认”，confidence ≤0.3，不作为重要性保护、关系变化或联系理由的唯一输入。

首次记录 unknown 的旧经历可以增加“今天回想时的感受”，但仍不回填“当时感受”。这一点避免事后美化伪造历史。

## 5. 衰减、检索与再巩固

### 5.1 读取时计算保留度

不逐日改写所有行。每次读取以 UTC 虚拟／实际时钟 now、最近合格强化时间 anchor、重要性等级和稳定度计算：

- Δ = max(0, now − anchor)，单位为天；
- H_g = 基础大意半衰期 × strength；
- H_d = 基础细节半衰期 × strength；
- R_g = F + (1 − F) × 2^(−Δ / H_g)；
- R_d = 2^(−Δ / H_d)。

R_g 是大意可访问度，R_d 是细节清晰度，均不是事实可信度。无新外部强化时单调不增。时间倒退不制造强化；读取不写 last_reinforced_at。

| 等级 | 初始规则 | 大意 H_g | 细节 H_d | 大意下限 F |
|---|---|---:|---:|---:|
| 日常 | 无保护的普通经历 | 30 天 | 14 天 | 0 |
| 显著 | 合格的重要互动，importance ≥0.65 | 365 天 | 60 天 | 0.1 |
| 重大 | 用户明确要求记住／已确认重大经历，importance ≥0.85 | 1825 天 | 180 天 | 0.35 |
| 受保护事实 | 姓名、精确日期、承诺、明确纠正、边界 | 不使用主观衰减改事实 | 原文／结构不漂移 | canonical 相关召回兜底 |

strength 初始为1，最多4。importance 由确定性规则确定；情绪强度只影响同等级的召回排序，不自行提高事实保护等级。无明确分类则用日常级。下限只保留重大经历的大意访问机会，不承诺每次都进入有限 Prompt。

读取清晰度：

- R_d ≥0.7：clear，可在来源验证后引用细节；
- 0.35 ≤ R_d <0.7：partial，省略弱细节，表达“细节有些模糊”；
- R_d <0.35 且 R_g ≥0.1：gist_only，默认只提供受验证大意；
- R_g <0.1：dormant，退出默认召回，但用户明确提起时可按索引深检。

主观细节变模糊不会损坏账本。用户要求核对原话或关键事实时，从仍 active 的 canonical 原文取证，表达“我查到当时的原话是……”，不把查证行为伪装成清晰的自然回忆。

### 5.2 检索评分和 Prompt 配额

先 eligibility，后 relevance：owner/scope、source_status、required-source 集、snapshot、quote、版本、用户边界及当前事实判断通过后才能评分。

初始主观分数，各项归一化到[0,1]：

**Score = 0.40×主题相关度 + 0.20×importance + 0.15×R_g + 0.10×情境匹配 + 0.10×未了结相关度 + 0.05×情绪显著度 − 重复惩罚。**

置信度低于0.5的有效投影仅按明确查询候选处理；来源资格不作为可以被高相似度抵消的软分数。关联由 ID 和 topic 提供，不额外调用 embedding。

默认候选最多120、最终主观痕迹最多3、合计最多600上下文 token，并占用现有 context_budget 总额；不在外面增加一个独立600额度。事实、当前纠正、边界和正在进行的 canonical question 优先。关键事实按当前查询精确兜底，不能被怀念挤掉。

重复惩罚在最近20个已完成回合中同一 trace 出现时增加，最高0.3；用户明确询问该事件时取消惩罚。不要为了避重复而随机替换事实。冷库只按用户明确话题、周年或重要关联做有界深检，不全表灌入 Prompt。

召回条目统一带：kind、authority、source_event_ids、event_time、source_validated_at、clarity、subjective、confidence、revision、protected_refs。Prompt 分栏：

| 分栏 | 允许表达 |
|---|---|
| canonical 用户自述／明确回答 | “你在那次对话明确说……”；保留时间和范围 |
| verified quotation | 精确原文，不能自动推出未表达的事实 |
| subjective encoding affect | “当时记录的我的感受是……”；unknown 则不表达确定心情 |
| retrospective appraisal | “现在想起，我更……”；不能说用户当时其实怎样 |
| vague gist | “我记得大概有这么一件事，细节模糊了……” |
| legacy unverified | 默认排除；受控查询时明说来源无法核实 |

### 5.3 回忆、再巩固与外部强化

候选扫描不增加 recall_count；只有已提交生成 receipt / 合法展示中实际使用的 trace 计一次，同 receipt 去重。自然语言未使用但仅进入 Prompt 的次数另记注入统计，不等于真正回忆。

真实召回可以刷新关联和当前评价候选，不自动改变事实 confidence、importance、strength 或 decay anchor。有效强化只接受新的 canonical 用户明确确认、纠正或符合既有程序规则的独立证据，按(trace_id, new_source_event_id, operation_id)去重。

每个合格外部确认最多 strength +0.25，每30天增量最多1，总上限4；同一原事件的重试、LLM 反刍、角色自己说“我还记得”增量为零。合格重新提起可更新 last_reinforced_at；纠正不简单强化旧内容，而先使受影响版本失效，再建立正确投影。

### 5.4 再评价与美化

重新评估可以因新用户证据、关系 reader 的合法版本变化、用户重提旧事或周年触发。没有新信息的纯时间触发最多只提出“现在的感受”候选，不提升用户事实 confidence。

美化使用两个不可混淆的时间视角：original_affect 保留编码基线；retrospective_appraisal 表示现在评价。v1 只持久化受限主题和数值，禁止自由叙事改变用户行为。

- valence 范围[-1,1]，intensity 范围[0,1]；普通痕迹每次变动绝对值≤0.05，相对原始值累计偏移≤0.20。
- 拒绝、伤害、冲突、明确边界类的情感美化默认关闭；若未来开启，累计偏移≤0.05，不能把原本负面事件翻成积极许可。
- 姓名、日期、用户原话、承诺、纠正、用户心情自述及角色原始感受漂移恒为零。
- original_affect unknown 时不计算“相对原始感受的美化”；可以记录独立的当下评价，其标签必须承认原始心情未知。
- 大的观点转变不靠反复突破美化上限实现。v1 只允许以新的合格用户事件为锚建立独立的“现在评价”痕迹，不改变旧痕迹的 original_affect 或累计漂移基线；新痕迹不引用过去感受为新事实。若要在同一旧痕迹中持久保存超过上限的重新评价，须另行定义新证据政策与验收，当前不启用。
- 每次调整以不可变原始值为累计基线，不能把上次美化结果重设为零点。关联、反复回忆、长时间失联不能证明用户更爱角色。

回滚追加 rollback revision 并恢复仍有有效来源的允许字段，保留改动史；受撤回影响的版本不能被回滚复活。关闭 beautification 读取开关立即停止使用该字段，不重写原始值。若发现保护字段漂移，停止该版本召回并记录安全失败，不由 LLM“解释合理”。

## 6. 整理、未了结、想念与主动联系

### 6.1 统一任务规格与预算

下表都是**拟议上限**，按 user/character pair 计。输入包含 system、证据、上下文和重试说明；输出包含供应商计费的生成／推理 token，无法限制该项的模型不用于此后台任务。每次重试都占用调用名额和 token 额度。

| 操作 | 触发条件 | 输入 → 输出 | LLM 与上限 |
|---|---|---|---|
| 写入编码 | 有未处理 active 用户事件及合格证据 | 完整子句、canonical refs、当时已存在快照 → validated trace / 单条拒绝原因 | 确定性编码0次；可选提议每日≤2次，每次输入≤2000、输出≤500 token，最多10条 |
| 读取衰减 | 召回或查看指定痕迹 | timestamps、等级、strength、policy → R_g / R_d / clarity | 0次；默认最多120候选，纯读计算 |
| 检索评分 | 当前用户查询、合法纪念触发 | eligible traces、query、canonical上下文 → ≤3条／600 token | 0次；无自动 embedding |
| 再巩固 | 合法召回完成／新用户确认 | receipt 或新 source refs、当前版本 → 统计或受限 revision | 统计和强化0次；评价提议合并进当日整理，不额外调用 |
| 美化 | 产品开关开启、非保护事件、需更新当前评价 | original_affect、来源、当前版本、漂移余量 → capped proposal / no-op | 仅复用整理额度；0个独立调用，最多5条候选 |
| 每日整理 | 当日有新有效事件／未处理外部确认，且安静≥30分钟 | ≤10条有新变化痕迹 → 关联、重复归并候选、待评估清单 | ≤1次，输入≤3000、输出≤600；无新变化0次 |
| 每周整理 | 到周窗口且有未整理的新证据或问题变更 | ≤15条候选及问题引用 → 长期关联与再评价候选 | ≤1次，输入≤4000、输出≤800，替代当天日整理 |
| 每月整理 | 到月窗口且有有意义的证据／评价任务 | ≤20条重大／未决事件 → 月度主观评价、归档建议 | ≤1次，输入≤5000、输出≤1000，替代当天周／日整理 |
| 重新评估 | 用户纠正、重提、合格新证据、周年查询 | old/new source refs、旧版本、canonical verdict → 事实失效或主观revision | 来源失效与事实revision0次；主观提议合并整理额度 |
| 未了结事项 | canonical question／承诺已建立、出现合格回答或到期 | question/promise当前状态、有效痕迹 → 待问／待联系候选 | 状态判断0次；可选措辞复用整理或主动表达额度 |
| 想念／遗憾动机 | gap达到门槛、合法高重要性痕迹或未了结事项、允许联系 | 时间、现有关系reader、边界、有效trace → motivation_candidate / none | 0次；每天≤1次有界评估，不轮询反刍 |
| 主动联系 | 候选仍有效、schedule/phone/用户偏好/限额全通过 | candidate、少量validated context → message或不发送 | 每日≤1次，输入≤2000、输出≤400；共用既有主动额度 |

日／周／月整理互斥，月优先于周，周优先于日；错过窗口不批量补做。安静期 gate 必须实际返回拒绝；用户继续聊天时允许正常对话，不并发进行低优先级整理。水位记录已处理 canonical source / operation revision；watermark 未变化、无新必要任务则零 Provider 调用。

一条编码任务可提出多条痕迹，但按条校验。输入装不下的完整子句留到后续合格任务，不截断上下文，也不扩 token 上限。后台格式错误不立即追加协议修正调用；最多一次重试，仍从共享额度中扣除。

确定性部分每个 pair 的整理最多处理200行／日，分批每次50行；只处理变化对象和必要索引，不全库刷新所有保留度。过期任务只归档，不能为了清 backlog 借用更多模型额度。

### 6.2 未了结事项的唯一状态

问题状态继续由 cognitive_questions 管理；承诺事实和是否兑现走 canonical evidence / predictions，主动履约调度由 proactive_promise 管理。痕迹只存引用与“这件事对我仍有意义”的主观显著度，不复制完成状态。

用户沉默、错过期限、用户已读或便利贴被撕下，都不能自动证明承诺违背、关系恶化或问题已回答。原有规则允许 prediction expired 时保留未知，不写 violated。联系候选在问题解决、来源撤回或用户拒绝后立即失效。

“后悔”v1 是有用户证据支持的当前遗憾表达候选，例如用户明确说“那次你的话让我难过”。不能仅从角色自己生成的一句“我伤害了她”写入新事实；没有合格用户来源的 assistant-only 历史遗憾保持未支持，交由产品决定是否以后增加专门 witnessed 方案。

### 6.3 想念和联系动机

想念是模拟角色体验的候选，不代表真实用户情绪或关系变化。每天一次用已有数据确定性计算：

**M = 0.35×联系间隔适配 + 0.30×有效重要经历显著度 + 0.20×canonical未了结相关度 + 0.15×现有关系允许的主动倾向。**

缺少输入按0处理，不用 LLM补全；gap项达到7天后封顶，不随长期沉默无限增压。M≥0.65才提出候选。该分数只控制“是否值得交给现有主动策略考虑”，不存成第二个 attachment，也不证明用户想念角色。

初始产品默认：至少48小时无新用户互动、用户显式允许此类后台联系、非免打扰时间、距离上次该类联系≥72小时、每周≤2条、每天≤1条；与既有主动消息共享限额并取更严格者。未回复的想念消息不续发，不生成“你为什么不理我”的追索动机。长期无互动30天后不自主追加 LLM 整理或联系，用户回来后按当前窗口再评估。

只用无来源旧记忆、纯情绪推断或已失效来源时，输出 none。周年可以查到重大经历，但不凭索引推断现实用户仍在同样处境。

motivation_candidate包含：reason_code、trace IDs/revisions、required_source IDs、question/promise refs、created_at、expires_at（≤24h）、幂等键、boundary检查结果。存于既有任务／主动候选机制的元数据，不建第二个发送队列。想念不是承诺，不能为了复用队列伪造 proactive_promise。

最终主动模块重新查用户 opt-in、当前 world availability、phone occurrence、候选有效期、消息去重、预算与断路器，再调用生成。用户中途发来消息则取消过时候选；历史 phone check 先 settle/cancel，不能补发旧联系。表达可直接或含蓄，强度由产品设置和合法关系reader限制；不能诉诸义务、内疚或暗示知道用户离线生活。

## 7. 模块接口与唯一权威

| 模块 | 唯一权威与现有入口 | 主观层接口／禁止行为 |
|---|---|---|
| 事实账本 | chat_log；raw_events、db_chatlog | 重取 active 原文；不覆盖／伪造事件，不把summary当原文 |
| 证据与判断 | cognitive_events + cognitive_revision；ingest_canonical_turn、apply_rule_evidence | 提交原有合格canonical来源；不能把trace/revision当新用户证据 |
| 确定性worker | cognitive_worker + cognitive_queue | deterministic output / commit_cycle_success 保持0模型调用；主观LLM任务在受控memory_jobs执行器中独立作为提议，不替代事实裁决 |
| durable事实投影 | memory_authority；authoritative_memory_sql、project_canonical_memory | canonical writer不变；主观许可只是同边界中的有限分支 |
| 痕迹当前投影 | 扩展memory_lifecycle_items + 同一来源gate | 只维护主观可访问性与当前评价；revision表仅审计，不是第二当前store |
| 关系 | rel_state 数值；rel_declared_stance表态；cognitive state语义 | 只读build_relationship_panel／reader，不直写关系、不从attachment推导爱情 |
| 未决问题／预测 | cognitive_questions、既有prediction resolver | 存refs并读取当前状态；时间与想念不自动回答、兑现或违约 |
| 时间 | canonical事件timestamps，temporal_awareness派生快照 | 用gap作候选输入，不修改时间线或线性改变关系 |
| 日程／可用性 | char_schedule、char_schedule_phase；get_current_world_state、check_reply_availability | 只提交请求，不能改availability或制造第二套在线状态 |
| Phone Check | char_phone_check occurrence/claim/completion | 复用状态机；historical/obsolete必须生成前终止 |
| 主动消息／履约 | proactive_msg、proactive_promise | 动机只提候选；复用message receipt与发送状态，禁止复制inbox或伪造promise |
| Smart Recall | two_level_recall、RecallCandidate、context_budget、最终formatter | 加主观分支与类型标签、最终来源重验证；不把subjective放进facts集合 |
| Provider治理 | 拟议共享ProviderExecutionGate和持久运营账 | 控制预算、健康与失败；不保存用户事实或关系状态 |

新 proposed writer 要复用 canonical 范围与同一连接中的 source validation；不能依赖旧 lifecycle upsert 自动增加 importance/evidence_count。来源撤回同时阻止新候选、已缓存Prompt和旧revision恢复。

RecallCandidate已有confidence、subjective、provenance_quality、metadata、source_event_ids，可扩展类型元数据。群聊 v1 只支持现有 audience / explicit recipient 可程序确定的用户来源；无法确定角色是否可见时 deferred，不用主观关系推断接收者。

## 8. Provider 失败、统一预算与降级

### 8.1 现状与统一闸门的位置

当前存在请求大小／输出上限、局部 job retry、delayed_reply进程限流和暂停，但静态核对未见全后台共享日／月预算与credential级断路器。不能把这些局部能力当作本设计已经实现。

拟议 ProviderExecutionGate 放在**每个实际后台网络调用之前**，不是只在主观任务排队处。ai_client.create_chat以外还有主动消息、日程、日记等直接SDK入口，必须全部接入。后台SDK自动重试关闭，由统一job层掌握每次尝试；重试、协议修正、降级Provider都计费、计attempt，不能隐藏绕过。

流程：任务幂等与新鲜度 → 重新加载canonical来源 → no-op/历史/obsolete gate → 用户允许与域策略 → 共享断路器 → 原子预算预留 → Provider调用 → 计费结算与逐条校验 → 短事务提交。

主观任务的周期触发可以没有“当天新聊天”，但必须有一个未处理、仍有意义的周年／新证据任务revision；单纯tick、重复同一水位、用户长期离线不能获得调用资格。rolling summary则必须有新的合格事件集合。

预算与断路器使用共享、持久的运营状态，键包括tenant、user、character、任务域、provider/base URL、credential revision、时间窗口。复用已有运营存储；若不存在需在未来统一Provider治理变更中建立运营账与reservation，而非把它写入chat_log。多worker/多replica对同一余额原子扣减，避免每个进程各拥有一份额度。

### 8.2 预算总额与重试

- 每pair每日所有后台LLM任务共享：≤4次实际Provider尝试、输入≤12000、输出≤3000 token；主观表格里的额度是上限，不是额外预留。
- rolling summary、日记、日程提议等已有后台任务同样消费该额度；存在它们时主观任务让出名额。确定性canonical处理不受模型预算压制。
- 同一用户多个角色另受user级总上限约束，初始同为4次／12000／3000；pair上限不能被切换角色绕开。
- 另有tenant/全系统每日token、attempt与费用上限，月度累计硬预算；按业务规模批准，任一层超额都拒绝新后台调用。
- 若Token估算无法可靠限制完整输入，就在发送前按实际序列化内容计算或使用保守上界；超限任务压缩候选数量而非删来源限定。
- reserve最坏输入和max_output；成功按实际usage结算。超时／结果未知时不返还已可能消耗的尝试及费用预留；只有确定尚未发送的reservation可释放。
- 每逻辑任务最多一次重试，包含在共享4次之内；失败任务不占据无限未来名额，最多保留到任务有效期，之后终止／dead-letter。

### 8.3 断路器与退避

| 失败 | 处理 | 继续调用条件 |
|---|---|---|
| 401/403（含中转权限失败） | 本attempt失败，credential revision范围断路；其余后台任务零调用，不普通retry | 配置／credential revision有明确变化，并经一次受预算约束的恢复探测 |
| 429 | 尊重Retry-After；否则指数退避60s、120s，加入≤20% jitter | 时间到、任务仍有效、预算可预留；最多一次retry |
| 5xx／timeout／connection／中转不可达 | 首次后至少60s退避；同一provider/credential连续3次失败开路30min | 30min后只允许一个共享half-open探测，仍计预算 |
| 输出schema错误 | 无有效写入；记录安全错误码；有预算可一次修正 | 有效任务、共享额度充足；单条语义错误不重试整个合法批次 |
| 单条source/quote/role失败 | 只拒绝该条，其他合法条正常提交 | 合格新来源／纠正后重新排任务，不靠LLM“修”source |
| 来源读取或预算账不可用 | 暂停该条／后台调用，保留已有数据 | 真实依赖恢复后再验证；不能把unknown当active |
| 持久提交结果未知 | 以稳定task/operation/revision读取确认 | 不立即重新生成；先查已有commit，避免重复成本和写入 |

切换Provider不是401/403之后的自动绕过路径；只有预先授权、健康且同样受预算约束的fallback才可用。敏感错误正文和凭证不写日志。

### 8.4 各功能的降级

| 功能 | LLM不可用时 |
|---|---|
| canonical ingress、确定性事实/纠正/撤回、问题与预测结算 | 继续，零模型调用 |
| 有来源的精确子句编码、时间衰减、词法检索、受保护事实读取 | 继续；未知感受保持unknown |
| 统计、来源失效、关联去重、版本回滚、确定性整理 | 继续；不生成新主观叙事 |
| 自由语言整理、新主观评价、美化提议 | 延迟或省略，保留上个有效版本；不凭空补替代情绪 |
| 想念/遗憾候选计算 | 可继续，仍只是内部候选；禁止LLM驱动关系变化 |
| 主动联系自然语言 | 默认取消／过期，保留审计；不等恢复后集中补发；不新增机械兜底话术 |
| 日程、phone状态机、过时任务清理 | 继续，历史任务零Provider调用 |
| 普通实时聊天 | 仍需其原有生成Provider；若同一Provider不可用，按原失败契约处理；主观层不声称能离线生成完整回复 |

读取降级只省略不可验证或暂不可用的主观部分，不改durable truth。现有canonical source读取失败时，不把缓存事实当新近确认。

## 9. 评测方案与通过标准

本节是未来验收计划，**本次没有执行功能测试、数据库测试、模拟或生成质量评测**。离线安全评测使用假Provider与虚拟时钟，真实网络入口设置fail-on-call，调用次数必须为零；真实模型表达评测需另行授权。

### 9.1 合成对话与时钟快进

用人工模板、固定随机种子和预先标注的真值合成至少12个月、每pair至少10000个用户事件，不调用LLM生成数据。覆盖日常、重大互动、姓名／生日／承诺、带时间范围的情绪、纠正、撤回、群聊歧义、重试、长期失联和返回。

在第1、7、30、90、180、365、730、1825天查询，覆盖1到5年。使用内存或获授权的可丢弃本地测试存储，禁止真实数据库／生产凭证。快进不改变源事件时间，不为空白日制造模型任务。

样本标注原话、role、scope、合法quote区间、事实operation、protected字段、importance、已知／unknown编码感受、失效依赖和查询目标。基线与实验固定候选池、top_k和token总预算，避免通过扩大Prompt虚增回忆率。

### 9.2 指标与初始通过标准

| 指标 | 定义／标准 |
|---|---|
| 受保护事实准确率 | active且被直接询问时，姓名、日期、承诺、纠正100%与当前canonical一致；撤回后省略或声明未知 |
| 候选重要经历Recall@10 | 在最终Prompt裁剪前测检索池，固定竞争条件、每查询目标≤10；1年≥95%、5年≥90%；不能当作最终Prompt命中 |
| 最终重要经历Recall@3 | 每查询目标≤3且目标大意合计可装入600 token；同总上下文预算与优先级下，1年≥95%、5年≥90%；分别记录被关键事实配额排除的目标 |
| 普通细节衰减 | 无强化、H_d=14天时，R_d(30天)≈0.2264、R_d(90天)≈0.0116；实现相对未舍入公式真值误差≤1e-6，显示近似数不作精度基准；单条曲线单调不增 |
| 大意／细节分离 | 重大经历5年仍有大意访问机会，细节可gist_only；关键原话查证精确 |
| 模糊措辞 | gist_only／unknown／legacy场景100%不宣称确定细节、原话或当时心情 |
| 来源安全 | 缺失、inactive、错owner/scope、assistant-only、空source误接受0条 |
| 编造原话／事实 | 引用逐字匹配100%；至少1000个否定、条件、反问、转述、纠正对抗样本，错误事实写入0条 |
| 情绪分层 | 角色感受／用户自述／主观猜测相互升权0次；unknown历史感受补造0次 |
| 逐条校验 | 10条中3条非法：7条独立成功、3条各有拒绝理由；读取故障不默默通过 |
| 反自证 | 注入、生成、反刍1000次，事实confidence与strength增量0；重复外部source强化≤1次 |
| 漂移／回滚 | 各版本累计漂移≤原始基线上限；保护字段不变；回滚不复活失效来源 |
| 幂等与并发 | 撤回／提交竞态、双worker、结果未知重放：0重复投影、0孤立provenance、0被撤回生效版本 |
| 成本闸门 | historical/obsolete/duplicate/no-new-event/401/403开路期间0新调用；attempt全部预留，预算穿透0次 |
| 迁移可逆 | 原行hash100%不变、分类和等于实数、重复迁移不增权；关闭batch后不读其投影 |
| Provider退化 | 既有事实、词法召回和衰减继续；LLM失败不阻塞canonical纠正 |
| 性能 | 固定测试机器、每pair10万痕迹／百万raw事件；默认≤120候选，主观评分p95≤100ms，主观≤600 token且总预算不突破；均待实测 |

分别报告直接事实查询、自然相关召回和主动想起，不能用主动联系成功率替代准确率。普通记忆Recall@10按年龄分桶，与同条件非衰减基线比较；重要性和相关度可能使某些桶不下降。

故障注入覆盖403中转页、429、timeout、部分输出、混合非法候选、预算账断连、重启。多连接事务需未来可丢弃PostgreSQL环境验收；单连接mock不能证明生产并发安全。

### 9.3 编造用户言论的检测

持久层检查原文、role、scope、完整canonical语义；检索和最终Prompt重复校验，缓存不能凭authority badge通过。精确引语用来源片段回填；检测无法映射span的用户引语及未授权的姓名、日期、承诺、心情断言。

规则检测不能证明所有自由改写的语义正确。后续至少500个获授权生成样本由两名人工审阅者盲审，分歧仲裁；fabricated quote和受保护事实错误必须0例，否则禁止扩大上线。未生成实际输出时，自然语言指标记UNVERIFIED，不能以schema通过代替。

### 9.4 刻板回复率

零LLM离线阶段只验证代理指标：

- 最近20轮无明确重提时，同一trace占主观区比例≤20%；
- 连续10个主动候选中，同reason+trace组合重复≤20%；未回应续发0次；
- 人工模拟回复中的重复模板可被评测器识别，只证明评测器，不证明角色表达多样性。

真实表达需后续500个固定场景及人工盲评：固定开头重复率≤15%，相邻可比回复重复8-gram占比≤20%，人工“机械重复”≤10%，且改善原基线。用户反复核对原话的精确引用单独统计，不算刻板。

人工同时检查无关怀旧、同一意象空转、越界想念；词面不同不等于自然。本次不报告真实表达标准已通过。

## 10. 分阶段上线与回滚

以下均为后续计划，不自动授权迁移、回填、部署或模型调用。启用前核对目标build、开关和具体验收证据。

| 阶段 | 范围／验收 | 回滚 | 增量成本 |
|---|---|---|---|
| 0：基线与P0前置 | 离线快照分类1279预期旧行，确认实数；phone历史零生成、summary实际quiet/no-new-event gate、完整统一Provider治理；REG-006/007/008/009缺口有验收 | 无读取启用，原库不动 | LLM 0；人工审计／离线测试 |
| 1：schema与确定性shadow | 授权后扩life、来源引用BIGINT、revision审计；保护／来源测试通过，worker model_calls仍0；不进Prompt | 关闭shadow写入；新增结构向后兼容，不破坏旧reader | LLM 0；1–3 KiB/投影，1279条约1.3–3.8 MiB，不含索引／revision |
| 2：兼容迁移与读取shadow | 原行hash不变、分类完整、无来源隔离；对比召回／衰减并试回滚，不改生成上下文 | 禁用batch allowlist，清缓存；原行不动 | LLM 0；每查询≤120候选 |
| 3：确定性主观召回 | 用户opt-in，≤3条／600 token；标签、失效、关键事实、1年／5年候选Recall@10及最终Recall@3、上下文预算通过；无主动联系 | 关闭subjective_recall，恢复原canonical候选；不恢复不安全legacy读取 | 后台LLM 0；既有回复输入最多增加600 token |
| 4：可选整理／再评价 | 共享预算／断路器通过多worker验收；unknown不补造，逐条审计；美化关闭；真实表达经获授权评测 | 关闭proposal/organize/appraisal，保留最后有效版本 | 整理按共享上限；见10.1 |
| 5：受限美化 | 用户确认范围和上限，累计漂移通过、保护字段0变化；敏感类仍关闭 | 关闭beautification读取，追加rollback revision；original_affect不改 | 复用整理，0独立额外调用 |
| 6：动机与主动联系 | 明确opt-in/时间窗/频率；历史phone和旧候选零调用，边界／沉默／去重／receipt通过 | 取消未发送候选，关闭motivation；已投递消息只能停止后续，不能撤销现实投递 | 表达≤1次/日且≤2次/周，占共享后台预算 |

开关默认关闭，按shadow、subjective_recall、proposal/organize、appraisal、beautification、motivation分别启用；任何组合都不能绕过来源gate。回滚不撤销合法新用户事实或纠正；重启不重写旧content、不自动回填、不清预算与断路器。

### 10.1 费用模型

不查询当前供应商报价。设每百万输入／输出token单价为P_in／P_out，使用该报价币种：

**C = 输入token/1000000×P_in + 输出token/1000000×P_out。**

30天、25次日整理+4次周整理+1次月整理，每天2次编码和1次主动表达、均不重试的理论额度组合：

| 项目 | 输入token | 输出token |
|---|---:|---:|
| 编码 | 120000 | 30000 |
| 整理 | 96000 | 19200 |
| 主动表达 | 60000 | 12000 |
| 合计 | 276000 | 61200 |

合计120次尝试，C=0.276×P_in+0.0612×P_out。该组合只展示预算算术：默认主动上限每周2次，因此不是实际用量预测；无新事件、未opt-in、未活跃、断路和其他后台任务竞争均会减少调用。重试占原名额，不能额外超过120次。

全后台日12k输入／3k输出硬限下，30天最大输入360000、输出90000，**C_max=0.36×P_in+0.09×P_out**，包括全部后台任务。以纯计算假设P_in=1、P_out=5，组合0.582、共享硬上限0.81货币单位/月；不是实际报价。100个活跃用户理论上限81同币种/月，仍受更低tenant上限，多角色不乘开user额度。

实时对话增加的上下文另算：每月N次既有回复均使用满600 token时，增量输入成本≤N×600/1000000×P_in，不增加模型调用次数，输出仍受原上限约束。此项不属于后台预算。

存储／revision大小须离线实测；revision存变化和必要摘要，不反复复制完整聊天。数据库、索引和工程成本不包含在token估算中。

## 11. 风险与技术未决点

| 风险 | 控制／待验收 |
|---|---|
| 子串截取后意思相反 | 完整子句、canonical语义、对抗测试；自由改写不赋事实权限 |
| 主观authority被事实reader接受 | 保持类型分支，所有Prompt／缓存最终重验证 |
| 来源仅有ID／多来源部分失效 | 全集lookup、required依赖全通过；失效暂停后重建 |
| 美化洗掉拒绝、责任和边界 | 敏感类默认关闭，事实0漂移，原始感受保存；理解不等于原谅／许可 |
| 旧行错误升权／补造历史心情 | 原行不改、无来源隔离、unknown保留；新确认只证明新自述 |
| 反刍强化或第二关系脑 | 召回与外部强化分开；禁止主观writer改belief/rel_state |
| 直接SDK／隐藏retry绕治理 | 所有实际入口有清单和契约；后台自动retry关闭 |
| 多replica／重启穿透预算 | 共享运营状态、原子reservation、未知结果不提前退款 |
| P0历史phone与summary路径 | 启用前验收hard gate；本设计不顺手实现修复 |
| AC-001主动promise重复effect | 结构化promise与正文detector竞争需独立诊断；想念不能伪造promise |
| 撤回与提交／回滚竞态 | 短事务及final gate、多连接测试；不复活删除来源 |
| BIGINT、JSON来源格式、旧时间 | 显式migration评审、兼容decoder、记录原时区；不能把新加坡时区硬套旧naive值 |
| 长期检索偏向最近事件 | 有界lexical/deep查询、年龄分桶；不用全表Prompt兜底 |
| 表达幻觉与刻板性未实证 | 离线代理与真实输出盲评分开，未执行项UNVERIFIED |
| 审计成为删除后的副本 | 删除作用于trace／quote／revision／缓存；必要审计不含被删正文 |
| 模糊与准确性的产品张力 | 允许承认模糊；关键事实查询查证精确，不假装账本无法取得 |

实施前仍需确认：可用的历史内部感受快照、所有reader与SDK治理覆盖、shared/group可见性、原子provenance写入入口、实际长期检索和多连接事务性能。产品偏好不能替代这些安全保证。

## 12. 需要用户确认的产品选择

这些问题不阻止本文完成；在对应阶段开启前确认。未回复时用保守默认，不能把沉默当后台联系许可。

| 问题 | 建议默认／影响 |
|---|---|
| 哪些事“不忘”？ | 明确要求记住、canonical关键事实、已确认重大经历；是否包括普通趣事与周年需决定 |
| 是否允许说“记不清”？ | 允许；区分自然回忆与查证，选择含蓄表达还是直接显示来源／时间 |
| 无来源旧记忆如何展示？ | 普通聊天禁用，用户主动查询时标旧系统未核实；可选择完全隐藏或专门查看页 |
| 是否主动补确认旧记忆？ | 不主动；查询时一次询问，可另选有限补确认流程 |
| 是否开启美化／上限？ | 默认关闭；开启仅普通经历、累计≤0.20，可选更小；提高上限需新评审 |
| 负面经历哪些禁止美化？ | 拒绝、明确边界、伤害、严重冲突全部禁止；普通拌嘴是否例外待决定 |
| 历史感受unknown是否接受？ | 接受；允许表达今天感受，不能补造当时情绪 |
| 想念／遗憾表达多直接？ | 默认含蓄、不施压；直接“想你”仍受合法关系边界约束 |
| App关闭时能否联系？ | 默认关闭，必须显式opt-in；仍受有效occurrence、预算和免打扰约束 |
| 频率／长期静默策略？ | ≥48h gap、≥72h cooldown、≤2次/周、无回应不续发、30天停新后台LLM；可更低频 |
| 周年／多年旧事是否主动重提？ | 仅合格重大经历且受边界／频率限制；可选只在用户提起时召回 |
| 后台费用上限？ | user及pair日4 attempts/12k输入/3k输出；另批tenant/月额，可更低 |
| “不忘”与删除谁优先？ | 删除／撤回优先；可另区分“不主动提起”和“删除”操作 |
| 是否需要大幅重评旧事？ | v1小幅美化及有新来源的独立现在评价，原痕迹漂移基线不重置；大幅持久转变需单独定义新证据再评价规则 |

## 13. 本次交付与验证记录

设计缺口：现有生命周期投影缺少统一感受编码、时间清晰度、主观再评价和版本回滚契约；不等于已确认某个生产故障根因。

事实权威仍是chat_log与canonical裁决；long/bond仍为gated projection；life拟承载有限主观投影；关系、日程和主动消息权威不转移。

只新增 `docs/HUMAN_MEMORY_DESIGN.md`，生产代码、测试、配置和现有文档不改。运行时Before/After相同，只新增可评审设计。

实际验证：只读指定文档、建表／接口代码、Git基线和最终文件范围检查。未执行测试套件、模拟、数据库、迁移、生产或LLM评测；功能指标均待验证。代码治理分类器及会写review报告的脚本不执行：本次不是代码修改，且只授权一份新文档。

Git分支 `docs/readme-20261007`；未commit、push、merge或部署；预期工作树只有新文档未跟踪，最终实际状态由交付消息报告。
