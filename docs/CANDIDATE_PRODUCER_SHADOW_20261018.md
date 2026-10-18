# 阶段 1 候选产生端：影子模式实施与审查报告

任务分支：`feat/candidate-producer-shadow-20261018`。
基线：`origin/main` 的 `bb144ef93b23f6ae393722e198e6a96634adc0ef`。
规格以用户在本任务中提供的方案第 7.1–7.7 节及补充约束为准；原文在
claude.ai 项目空间，不在 Git 仓库中。本任务未推断或重建缺失文档。

## Status / Risk

语义风险 **RED**：新增后台模型调用和持久化尝试额度，属于用户明确授权的范围。
本实现只支持影子观察，没有切换到投影的选项。默认关闭，未部署、未执行真实模型评测。
最终治理和全量测试结果见本报告末尾及 `review/latest.md`。

## Root Cause / Canonical Authority

原实现只有确定性候选闸门，没有独立候选产生入口。窄句式以外的原始消息不能通过
模型产生受控候选。此次新增只读候选验证和日志，不改变这些消息的事实裁决。

- 原始消息：`chat_log`，通过 `raw_events.get_active_events_by_ids` 取回、检查 scope。
- canonical 用户事件及原始快照：`cognitive_events`。
- 确定性判定：原有 `memory_candidate.validate_candidate`，G1–G9 原样调用。
- durable memory authority：原有 cognitive adjudication / provenance / gated projection，未改。
- 技术处理标记：既有 `event_processing_state`。它只保存去重、预算、失败计数和冷却；
  不保存候选、quote、summary 或 perspective 正文，不成为新的记忆权威。

## Files Changed

Production:

- `gojo_backend/memory_candidate_shadow.py`：批处理、模型调用、影子校验、JSONL 日志。
- `gojo_backend/delayed_reply_retry.py`：从 delayed reply 提取既有纯重试策略。
- `gojo_backend/db_schedule.py`、`gojo_backend/delayed_reply.py`：改用该策略，原语义保持。
- `gojo_backend/memory_jobs.py`：现有 memory worker 启动入口同时尝试启动独立影子线程；
  影子启动失败单独记录，不阻塞既有 worker。

Tests:

- `tests/test_memory_candidate_shadow.py`：真实离线 SQL、失败恢复、零记忆写入。
- `tests/test_memory_candidate_shadow_controls.py`：worker 隔离、开关、日志解析及审计去重。
- `tests/test_delayed_reply_retry.py`：共享重试策略兼容性。

Docs / Tools:

- 本报告。
- `scripts/analyze_candidate_shadow.py`：离线日志统计，支持人工标签计算召回率和摘要准确率。

## Behavior

Before：没有异步候选产生端，候选闸门仅能被已有调用者使用。

After：设置 `MEMORY_CANDIDATE_SHADOW_ENABLED=true` 后，独立 daemon worker 每 5 秒轮询。
同一用户/角色累计 8 条未经本模块观察的 canonical 用户消息即触发；不足 8 条时，
最早未处理消息等待满 300 秒触发。实际触发精度受轮询和当前批次执行时间影响。
每批最多 8 条消息、最多每源 1 个候选。源无效或群聊 scope 不符时跳过并继续扫描，
不会让前面的无效来源挡住后面的合法消息。

“未处理”由本模块 processor receipt 判断，**不依赖 adjudication.status**。
真实 canonical ingress 默认 adjudication 为 `{}`，已有 pending/applied/rejected 状态
也不会被本模块改变。成功观察后即使没有候选或候选被拒，也记录本模块完成标记；
整批失败不记录来源完成标记，原事件和原裁决原样保留，下批可重试。

模型为 `MODEL_MAIN`，通过现有 `get_anthropic(max_retries=0, timeout=120)` 调用，
`max_tokens=2000`。不降级模型，不做即刻应用层重试，不增加 SDK 重试。
输入仅含程序提供的 character ID、canonical user source ID 和正文，没有角色发言、
历史记忆或关系结论。输出必须是完整 JSON 数组，不做 prefix 提取、补 JSON 或部分 salvage。

模型每项只允许以下字段：

| 字段 | 要求 |
|---|---|
| source_event_id | 必填；必须属于本批输入 |
| quote | 必填；交原有 G2/G3 校验逐字引用与长度 |
| summary | 必填；交原有 G4–G7/G9 校验 |
| predicate | 必填；枚举来自现有 `_PREDICATES` |
| time_scope | 可省略；仅 `YYYY-MM-DD` 或 `unspecified` |
| perspective | 可省略/留空；原有 G8 可移除此字段 |

任何额外字段（含 category、重要度分数）拒绝该项，其余项仍校验。
同批重复 source 只记录一条拒绝，原始项数与折叠项数单独统计。
每个被记录的候选均实际调用现有 validator；没有复制或另写闸门算法。

### 与现有 validator 的两处适配

1. 当前 G7 除模型六字段外还要求 `object/value`。程序在调用 validator 的临时副本中
   将两者均设为原样 quote；这只满足现有形状检查，不建立语义 claim、question key 或投影。
   模型不允许生成这两个字段。未来正式投影需要另行设计语义构造，不能把本适配直接用于写入。
2. validator 在首个硬拒绝处短路，只返回 `(accepted, reason)`。日志按其实际执行顺序
   G1→G2→G3→G4→G5→G6→G7→G9→G8 展开：前序通过，失败 gate 拒绝，后序
   `not_evaluated`，不伪造通过。非字符串 summary 跳过的 G4–G6 也记 `not_evaluated`。
   `perspective_dropped` 表示 G8 拒绝该字段，候选本身仍可 accepted。

### 额度、重试与并发

- 全模块合计每天最多 20 次预留批次，跨用户、角色、进程和模块版本共享；按 `CN_TZ`
  的自然日（当前 UTC+8）重置。失败、超时、进程退出不退还已预留额度。
- 固定 `daily_budget_v1` 预算键与候选 `MODULE_VERSION` 去重键分离，升级版本不重置当日额度。
- 独立的短事务 advisory lock 保护全局预留；模型调用前已提交，不把这个锁带入网络调用。
- 独立 namespace 的每用户 session advisory lock 避免多个实例同时处理该用户；它与聊天
  ingress/cognitive 锁不同，且不在模型调用期间持有原文或 cognitive 行锁。
- 原有 delayed reply 的 120、600、1800、7200 秒增长退避和 600 秒 provider pause
  提取成共享纯策略；没有新建第二套重试算法或修改原 phone-check 状态机。
- 连续第 5 次失败后该用户暂停 7200 秒；之后允许半开尝试，成功才清零，继续失败则继续暂停。
  认证错误至少冷却 900 秒，临时 provider 错误还遵守已有 retry delay 下限。
- `stop_reason=max_tokens` / `finish_reason=length` 或输出 token 达到 2000，均整批拒绝，
  下一批上限持久降为 4，max_tokens 不变，不在当前批内立刻重试。
- 超额不建立 job 队列；保留 canonical 消息，下个自然日扫描时仍可观察。
- 模型结果未知、数据库 commit 未知单独记录，禁止当作成功；事务失败整体回滚。

## JSONL 日志契约 / 拟提交说明

每次输出一个 JSON 对象并 flush；`log_type="memory_candidate_shadow"`。
从服务日志导出这些 JSON 行给脚本，不要包含平台时间戳前缀。
没有候选正文入库，日志本身须按既有用户数据日志访问规则保管。

共有字段：`module_version`、`user_id`、`character_id`、`batch_id`（随机唯一 ID）、
`batch_sequence`（当日全局已预留批次序号）。

| kind | 主要字段及含义 |
|---|---|
| candidate | `record_id`、`source_event_id`、`source_in_batch`、`candidate_index`、`quote`（前 40 字）、`summary`、`predicate`、`accepted`、`reason`、`gates` |
| candidate | `perspective_output_empty` 是原输出是否为空，`perspective_empty` 是 validator 返回时该字段是否为空（若 G8 未执行，仍反映原值，应结合 gate 状态）；`perspective` 仅记录最终接受且经 G8 保留的正文，供人工评价角色视角；拒绝或被移除的内容为 null |
| batch | `status`、`input_count`、`input_source_event_ids`、`candidate_count`、`passed_count`、`rejection_counts`；成功批另含 `raw_candidate_count`、`duplicate_candidate_count`、`perspective_dropped_count` |
| batch_failure | `status`、`stage`、`reason`；模型故障附已有脱敏 provider diagnostics；失败也输出一条 batch 汇总 |
| worker_failure | startup/tick/连接等无法进入正常批次的错误，`status=failed`；启动错误含 `stage=startup` |

`candidate_count` 是实际去重后的候选日志行数，等于通过数加拒绝计数总和。
`raw_candidate_count` 是模型原始数组项数；二者差为同源重复折叠数。
每个 `gates.Gn` 都有 `status`（`passed` / `rejected` / `not_evaluated`）及 `reason`。
失败阶段包括 `model_call`、`parse`、`truncation`，另外明确区分 reservation、validation、
persistence、logging 和 cleanup，不能把数据库失败归作模型成功。

合法本批来源的 `record_id` 由 user/character/source/version 稳定计算，便于合并日志导出时去重。
错引其它批次来源或缺失来源的无效候选额外包含 batch/index 身份，且 `source_in_batch=false`；
它不能占用后续真实来源的审计 ID 或进入该来源的召回指标。

离线使用：

```text
python scripts/analyze_candidate_shadow.py shadow.jsonl
python scripts/analyze_candidate_shadow.py shadow.jsonl --labels human-labels.jsonl
```

人工标签每行的格式：

```json
{"user_id":"u","character_id":"c","source_event_id":"s","module_version":"candidate_shadow_v1","should_remember":true,"summary_correct":true}
```

`should_remember` 用于召回率分母，`summary_correct` 用于人工评估摘要准确率。
脚本分别报告候选召回率和通过闸门后的召回率；没有人工标签时不虚报真实召回率/准确率。
空候选批也记录输入 ID，便于人工回看遗漏消息。脚本另报告 G1–G9 结果计数、视角空值数、
失败阶段/理由和候选行与汇总的对账差异。

### 日志交付边界

成功 processor receipt 提交确认后才输出 candidate 行，所以普通重放及进程重启不会重复输出。
数据库与 stdout 不是原子事务：**提交后、日志输出前崩溃可能漏行**。这是 at-most-once
日志交付，不是 exactly-once；稳定记录 ID 能去重已有行，不能恢复完全未输出的批次。
候选不落库的约束下不建立审计 outbox。日志对账异常应从人工评测样本中剔除并调查。
上游模型请求也没有 exactly-once 保证；断线后的请求结果可能未知，额度仍计入。

## linked_fact_id 调查（只读）

**现有实际赋值仅在 legacy writer，canonical 投影没有挂靠。候选影子或未来单独放开
候选投影，都不能自动恢复 fact→bond 的第二级关联召回。**

| 函数 / 调用点（基线行号） | 条件与行为 |
|---|---|
| `user_memory.save_bond_memory:923`，赋值 `:959` | kind 非 told；有效来源（若提供）；无近似重复；成功插入 bond；匹配器返回 fact ID 后 UPDATE linked_fact_id |
| `user_memory.merge_bond_memories:997`，赋值 `:1104` | 合并检查通过、旧内容保留、来源有效；合并插入后匹配非空才 UPDATE；候选查询排除 canonical_evidence_v1 |
| `smart_recall.link_bond_to_fact:674` | 只选择 ID，本身不写；对相同用户、角色或 shared 的 active 未过期 long_memory 按标点/空白片段匹配，至少命中一片；不使用 LLM、向量或 authoritative_memory_sql |

真实可达的旧调用仍存在：`diary_engine.peek_user_diary:327`、
`diary_scheduler._generate_comment_reaction:298`、`route_diary.comment_char_diary:69`、
`route_game.gomoku_save_memory:254` 调用 save；`route_stats.manual_merge_memory:118`
（`/memory/merge`）调用 merge。它们的 INSERT 默认 authority 为 generated_recollection，
挂上 ID 也不能获得 canonical recall 资格。关联异常可能被旧 writer 忽略。

不应误算为活跃写入的路径：`_apply_extracted_bond_delta` 没有生产调用者；
`_apply_communication_convention` 的唯一外部生产调用在
`communication_convention_backfill.validate_existing_convention:369`，固定 dry_run=True；
`resolve_bond_memories` 的现有调用均不传可触发新 bond 写入的 new_content。

canonical `memory_authority.project_canonical_memory:244` 的 bond INSERT（`:279`）
没有 linked_fact_id，后续只写来源依赖。`memory_candidate.apply_candidate:266`
及 deterministic cognitive 路径都调用它，也没有补挂代码。

读取端仍保留：`smart_recall.two_level_recall:386` 按选中的 fact IDs 读取 linked bond，
同时强制 authoritative_memory_sql；`:438` 的独立 bond 池读取无 linked_fact_id 的项，
同样保留 authority gate。所以第一层事实与独立 bond 召回不等于关联召回已恢复。
已有合法且带关联的历史 canonical 数据如果存在仍可能被读出；本任务未查生产 DB，
不声称线上没有这类数据，也不声称上述旧入口近期实际执行过。

FOLLOW-UP：独立设计 canonical 挂靠及来源失效机制；不能恢复旧 writer 来绕过 authority。
本轮对上述关联写入和召回代码零修改。

## Architecture Impact

Canonical authority、relationship authority、durable-memory authority、provenance、
schema、API contract、identity semantics 均 unchanged。
Worker lifecycle 增加默认关闭的独立影子线程；原 memory worker 和 delayed reply
状态机行为保持。没有迁移、生产 backfill、memory projection 或付费测试调用。

相关回归 owner 均为本后端：REG-006（启动不改写记忆）、REG-007（provider失败不获权威）、
REG-008（派生内容不成为事实）、REG-009（phone-check pending恢复）。新 SQL 测试对
apply_candidate / project_canonical_memory 设置调用即失败的 mock，并检查所有非 processing 表
前后状态相同。真实聊天 ingress 可在候选模型等待中继续写入原事件/认知事件，候选失败不回滚它。

## Verification / Remaining Risks

定向候选 SQL：28 PASS；worker/日志控制：7 PASS；共享重试相关：53 PASS。
最终相关回归：262 PASS，0 failures、0 errors、0 skipped，覆盖新模块、既有 candidate
gates、memory authority、delayed reply、phone check、provider errors、结构化摘要、
memory restart 和 cognitive worker controls。
以上均实际执行，使用 offline guard 禁止外网和真实 psycopg2 连接，数据库为本地 PGlite。
完整套件在独立、固定的 main 基线和最终分支分别执行，结果如下：

| Runner | main `bb144ef` | 最终分支 |
|---|---|---|
| unittest full | 1434 项；16 failure / 40 error 记录 | 1473 项；16 failure / 40 error 记录 |
| pytest-native 补充 | 213 PASS | 213 PASS |

新增永久测试 39 项；两边均无 skipped。按 runner / kind / test / occurrence 精确比较，
56 条失败记录身份完全一致，新增 0、消失 0。仅归一化两个工作树的绝对路径后，
相同失败身份的异常原因变化也为 0。`suite-comparison.json` 的实际结果为
**NO_NEW_FAILURES**；这不是把全量套件宣称为全部通过。

残留记录分组：private_memory_fact_gate 16、flirt_response 10、cognitive_spend_guard 9、
time_cognition_echo_audit 8、cognitive_loop_v1 5、cognitive_continuous_loop 2、
receipt_windows 2、generation_audit_trace 2、explicit_answer_sql 1、time_cognition_projection 1。
实际错误包括 mock 不存在的属性、旧 lambda 未接收 temporal_snapshot、数量断言不符、
空列表和缺字段访问；本次未修复或笼统认定它们的根因，完整 traceback 留在证据中。

证据目录：`C:/Users/张三/AppData/Local/Temp/gojo_candidate_producer_shadow_20261018_evidence`。
`affected-commands.json`、`baseline-commands.json`、`final-commands.json` 保存完整 Python
可执行文件路径、实际 argv、离线环境变量、退出码和运行时间。相关回归使用仓库的
`tests/run_offline.py --pattern ...`；完整套件使用 `--full`，再补跑导入 pytest 的原生
测试文件。后者含混合风格文件中的重复用例，两类 runner 总数不能简单相加为独立用例数。

独立静态审查结论 **SAFE，未发现剩余阻断项**，不降低 RED 语义风险；实际范围和证据
见该目录 `final-static-review.md`。受保护文件与基线逐字节（仅归一化 CRLF）比较，
包括 memory_candidate、cognitive_revision、memory_authority、raw_events、smart_recall、
user_memory、gojo_server 和 .env.example，均未改，哈希保存于 `protected-files.json`。

最终执行治理命令：

```text
python .codex/skills/gojo-safe-change/scripts/classify_change.py
python .codex/skills/gojo-safe-change/scripts/audit_diff.py --allow-domain memory --allow-domain raw-event/evidence --allow-domain schedule/proactive
python .codex/skills/gojo-safe-change/scripts/build_review_report.py --allow-domain memory --allow-domain raw-event/evidence --allow-domain schedule/proactive
```

实际分类 **YELLOW**（5 个生产文件，跨 memory / schedule）；语义风险维持 **RED**。
Diff audit 为 **NEEDS_REVIEW**（退出码 1），无越界生产文件，触发项为后台线程、
event_processing_state 写入和异常隔离。最终语义代码审查逐项确认：只有技术处理状态
写入、没有权威写入；异常明确记录 failed/commit_unknown；聊天和既有 worker 隔离。
这些审查项已完成，不是自动审批拒绝。生成器的自动结果和手工补充保存在 `review/latest.md`，
原始命令、退出码和日志保存在 `governance-commands.json` 及对应 log。

真实 100 条用户消息的人工影子评估 **UNVERIFIED**，真实模型摘要准确率、视角质量、
2000 token 下截断频率 **UNVERIFIED**；本轮没有为验证运行付费 API。
跨真实 PostgreSQL 多实例锁竞争未在线执行；SQL 与失败恢复在离线 PGlite 中验证。
现存 main 的残留失败不会因本次功能测试通过被标记为已修复。

## Git State

独立任务工作树：`C:/Users/张三/AppData/Local/Temp/gojo_candidate_producer_shadow_20261018`。
当前分支：`feat/candidate-producer-shadow-20261018`。未 commit、未 push、未 merge、未 deploy。
原 IDE 工作区及其无关未提交改动保留；本地 main 不变。
