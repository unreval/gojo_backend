# 记忆候选产生端：影响评审

## Problem / Root Cause

固定基线为 `6c1d238`。现有自然语言录入仅使用有限句式的确定性解析；未命中的消息留下 `unsupported_or_implicit_semantics` pending 裁决。main 有候选校验与投影函数，但没有后台候选产生入口，也没有为这些候选建立完整裁决/稳定信念的协作路径。canonical bond 投影另缺少 fact 关联。

任务单样例 SQL 与实际 schema 不一致：`event_processing_state` 使用 `(event_id, processor_type, processor_version)`，没有 user/character/event_type 列；cycle 外键可空，不能用不存在的 cycle 0。实现使用现有 schema，未做迁移。

## Canonical Authority

- Raw source：`chat_log` / `raw_events`，必须为当前、未删除且内容未变化的 user 消息。
- 裁决与冲突策略：`cognitive_events`、`cognitive_revision._apply_literal_operation`、既有 question/belief resolver。
- 投影与召回：`memory_candidate.validate_candidate/apply_candidate`、`memory_authority.project_canonical_memory/authoritative_memory_sql`。
- EPS 仅记录批次额度、冷却和处理凭据，不作为事实 authority。

## Approved Scope / Risk

Semantic risk：**RED**，涉及 memory、cognitive-loop、后台生命周期；config 属于 generation 分类，治理路径分类修正属于 workflow-governance。

用户原任务授权 A/B/C、本地假 Provider 与临时 PGlite 验证；随后明确批准：仅补充 `pending/unsupported_or_implicit_semantics` 的候选 operation，保留原 operation/cycle；在后到的 deterministic worker 中保护 producer 已建立的裁决。其他既有裁决继续跳过。没有修改现有解析器。

## Final Design

- A：独立 daemon worker，默认禁用，默认 shadow；30 秒轮询，8 条或最早等待 300 秒触发；只使用原样 `MODEL_MAIN`、2000 token、SDK retry 0。截断/解析/非数组是整批失败，非法单项跳过；截断后批量降为 4。每日全局 20 批，按 SGT 起始日期计费式预占额度；连续失败 3 次用户暂停 1800 秒。
- Provider 调用之前提交额度和 pending 凭据；调用期间没有打开的事务或 source 行锁。用户级 session advisory lock 防止跨副本重入；事务锁顺序与 canonical writer 一致。提交结果未知时不退款、不补写成功。
- B：重新读取原始来源，经原有 G1–G9 和额外 value/time_scope 逐字及否定检查；复用 canonical literal writer 和冲突处理，使用 NULL cycle 外键。候选级 savepoint 保证被拒绝或异常时不会留下部分裁决/投影；成功投影与 EPS 完成状态由外层同一事务提交。
- 已有明确 applied/conflict/其他 pending 裁决不覆盖；仅经批准补充 unsupported pending。operation identity 不使用 summary/perspective，重复候选不重复写入。
- C：仅新插入的 canonical bond 尝试关联同用户、同角色、未晚于 bond 的 fact；目标必须通过完整 authority SQL，优先同源，然后最近时间。无目标时保留 NULL。关联本身不产生新事实或额外证据依赖。

## Protected Invariants / Diff Review

- 不引入第二事实裁决器或 relationship authority；模型只产生候选，提交路径零模型调用。
- 未新建表或列，未改变 public API，未更改 raw event、parser、prompt、route_chat 或前端。
- quote 不放宽；role=user、来源生命周期、authority SQL、稳定 belief 和 conflict policy 持续生效。
- 视角和展示 summary 不作为事实身份或额外证据。没有合并前一个 P0 启发式软放行补丁。
- worker 默认关闭，启动失败仅记录类型；运行失败不进入聊天同步路径。
- 最终 diff 中所有新增数据库写入都是任务所需的现有 EPS、canonical writer 与 bond link 协作。宽异常处理限于候选 savepoint 回滚或后台故障隔离，错误显式记录/上抛，不伪装有效记忆。
- REG-006：启动不重写既有记忆；REG-007：provider 失败保留 pending、有界重试/冷却；REG-008：derived/assistant 输出不能作为本模块的用户事实来源。owner repository 为本仓库，实际执行结果见 completion 与测试日志。

## Unverified / Follow-up

- 生产环境 `api.dzzi.ai` 的完整带标签 MODEL_MAIN 未验证；假 Provider 只证明配置值原样传递。
- 没有运行至少 100 条真实消息的影子评估，未评估生产提取准确率或召回收益；开关保持默认关闭/shadow。
- 本地 PGlite 验证 SQL 和状态合同，不能代替真实多副本 PostgreSQL 的并发、网络断连和连接池验收。
- G1–G9 和逐字/否定检查是保守的确定性检查，并不证明任意自然语言 object/predicate 分类正确。
- 当前 main 没有 `scripts/analyze_candidate_shadow.py`。旧本地 shadow 分支仍存在，但其日志分析器不是 main 的兼容合同；未合并旧分支。
- existing raw hard-purge 用裸 event_id 清理 EPS，若跨用户重用事件 ID 可能连带移除处理凭据；本次保留来源删除机制，稳定 operation identity 防止重复投影，此潜在跨用户清理问题需独立评估。
- 没有提交、推送、合并、部署、生产 DB 操作、生产回填或付费模型测试。
