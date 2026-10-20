# 记忆候选产生端 + 裁决补全 + linked_fact_id：验收报告

## Status

**NEEDS REVIEW**。A/B/C 已实现，最终全量执行中直接受影响的 15 个测试模块全部通过（287 个 test node）；47 个新增测试全部通过。全量仓库仍存在基线失败，不能宣称全绿或可直接生产启用。

## Risk

**RED**。semantic risk 与 classifier 均为 RED；diff audit 为 NEEDS_REVIEW，未检测到超出已授权 domains 的生产文件；自动 review report 为 NEEDS REVIEW / RED。authority-sensitive 改动和新增后台行为已按 `impact-review.md` 人工逐项检查，原任务及后续用户批准覆盖这些必要改动。没有待执行的提交、推送或部署。

## Root Cause

有限句式的 deterministic 解析无法覆盖大部分自然自述；main 缺少候选产生 worker。既有 `apply_candidate` 不创建完整裁决/稳定 belief，单独投影不足以通过完整 authority SQL；canonical bond 插入也不建立 `linked_fact_id`。

此外，真实 deterministic 流程会先留下非空 unsupported pending。仅允许空裁决的样例无法处理这些事件。经用户明确授权，现仅补充这类 pending 的新 operation，并保护后到 worker 不覆盖 producer 裁决；原裁决 operation/cycle 完整保留。

## Canonical Authority

`chat_log` / `raw_events` → `cognitive_events` → 原有 `cognitive_revision._apply_literal_operation` / question/belief resolver → `memory_candidate.apply_candidate` / `memory_authority.project_canonical_memory`。最终验收执行完整 `authoritative_memory_sql`，没有另建事实裁决器。

## Files Changed

Production：

- `gojo_backend/candidate_producer.py`：独立后台批次、额度/冷却/幂等、假 Provider 接口、日志、原子裁决与投影协作。
- `gojo_backend/config.py`：默认关闭与默认 shadow 开关。
- `gojo_backend/gojo_server.py`：在既有 memory worker 启动位置接入。
- `gojo_backend/cognitive_revision.py`：经批准的最小 producer 裁决保护条件；解析器未改。
- `gojo_backend/memory_authority.py`：新 canonical bond 关联现有有效 fact。

Tests：`tests/test_candidate_producer.py`（24 项）、`tests/test_candidate_producer_projection.py`（23 项）。

Docs / governance / validation：classifier 增加两个准确的 memory 路径；`review/run_memory_validation.py`；本报告、影响评审、实际日志与最终 patch。最终代码 patch 含 9 个文件，不把测试日志和临时数据库打入 patch。

## Behavior

Before：未命中固定句式的消息无法获得完整候选录入；即使调用候选投影，authority SQL 仍依赖不存在的裁决/belief。新 canonical bond 没有 fact link。

After：默认行为保持关闭。显式启用后，worker 每 30 秒检查用户消息，满 8 条或等待 300 秒触发；模型使用原样 MODEL_MAIN、2000 token、SDK retry 0。shadow 运行真实闸门且只写 EPS/log。live 中仅验证通过的候选进入现有 canonical writer；冲突依旧 pending，失败/异常以 savepoint 回滚候选部分写入。已完成事件和稳定 operation 双重去重。截断整批 pending、批量降为 4；每日 SGT 全局最多 20 批，连续 3 次失败暂停用户 30 分钟。

新 bond 只关联同用户/角色、时间不晚于 bond 且通过完整 authority SQL 的 fact；优先同源，然后最近时间，无 fact 时保持 NULL。

## Architecture Impact

| 项目 | 结果 |
|---|---|
| Canonical authority | 权威 owner 不变；增加经批准的候选协作入口，复用原 writer/conflict policy |
| Relationship authority | 不变，无直接 relationship 状态修改 |
| Durable memory authority | 最终 authority SQL 和 stable belief 要求不变 |
| Provenance | quote、role=user、原始内容、来源生命周期要求不放宽 |
| Database schema | 不变，无新表/列；EPS 为原有 TEXT processor_type/version |
| API contract | 不变 |
| Worker lifecycle | 新增独立、默认关闭的 daemon worker；模型调用期间无 source 行锁或打开事务 |
| Identity semantics | 现有身份键不变；候选 operation 稳定哈希排除 summary/perspective |

`cognitive_questions` 的 `(user_id, character_id, question_key)` 和 `cognitive_beliefs` 的 `(user_id, character_id, belief_key)` 唯一约束已查证并在 PGlite 中实际执行。非 cycle 写入使用允许的 NULL 外键，不使用不存在的 cycle 0。

## Tests Actually Executed

所有 Python 命令使用 `C:/Users/张三/AppData/Local/GojoAssistant-test-env/python311/python.exe`（下称 `$Py`）。工作目录为本 worktree。runner 使用现有本地 pytest 9.1.1 和 stdio PGlite 0.3.14，安装 external-network / real-psycopg2 guard，使用 dummy keys，未下载依赖或调用付费 API。

| 实际命令 | 结果 |
|---|---|
| `$Py review/run_memory_validation.py full --baseline 6c1d238 --label baseline-full` | 1592 passed，63 failure records，1007 subtests passed，0 errors/skips |
| `$Py review/run_memory_validation.py targeted --label final-targeted` | 中间定向轮 286 passed，290 subtests passed，0 failures/errors/skips |
| `$Py review/run_memory_validation.py targeted --label final-producer --path tests/test_candidate_producer.py --path tests/test_candidate_producer_projection.py` | 最后产品改动后 47 passed，33 subtests passed，0 failures/errors/skips |
| `$Py review/run_memory_validation.py full --label final-full` | 1638 passed，58 failure records，1046 subtests passed，0 errors/skips，324.58 秒 |
| `$Py review/run_memory_validation.py targeted --label worker-controls-isolated --path tests/test_cognitive_worker_controls.py::WorkerControlsPersistenceTests::test_disabled_owned_duplicate_persists_recovery_across_connections` | 1 passed，0 failures/errors/skips |
| `$Py review/run_memory_validation.py targeted --label worker-controls-confirmation --path tests/test_cognitive_worker_controls.py` | 14 passed，6 subtests passed，0 failures/errors/skips |
| 只读设置 `GIT_DIR=(git rev-parse --absolute-git-dir)` 后执行 `$Py review/run_memory_validation.py targeted --baseline 6c1d238 --label baseline-cost-with-history --path tests/test_time_cognition_cost_audit.py::TokenCostAuditTests::test_fixed_twenty_message_matrix --path tests/test_time_cognition_cost_audit.py::TokenCostAuditTests::test_fixed_forty_message_matrix`，finally 恢复环境变量 | 2 passed，6 subtests passed，0 failures/errors/skips |

最后 full 的通过 node 与 15 个受影响测试文件取交集：**287 passed、0 failed**，包括题单要求的既有 candidate/source/authority/lifecycle/cognitive revision/deterministic 模块；结果在 `final-affected-suite.json`，是最终全量的切片，不是一次额外执行。

`comparison.json` 使用 `Counter((test, phase))` 比较，保留重复 subtest 记录：

- 57 条失败在 baseline/final 同时出现；没有改动这些不相关子系统。
- 唯一新增失败是 Windows `TemporaryDirectory.cleanup` 删除 PGlite 文件时的 `WinError 32`，无业务断言失败；上述单例和整文件复跑通过。不能据此把原 full 结果改为全通过。
- baseline 消失的 6 条是两个成本审计方法各三个 subtest。原 Git archive 无 `.git`，无法 `git show` 历史 context_layer fixture；补充只读历史后六个全部通过。不能将这项差异归功于本次功能。
- cleanup 失败遗留的 `tmpajr_m7rd` 在测试进程结束后，校验其绝对路径严格位于本 worktree 且不是 reparse point，再单独删除成功；没有改测试夹具或忽略目录规则。

## Regression Protection

47 个新增测试永久覆盖题单 1–19，并补充来源调用中改变、同事件多候选、展示文本变化重放、两种 worker 顺序、保留已有裁决、冲突策略、单项回滚、未知 commit ack、全局额度、锁顺序、来源删除和 fact link 跨用户/跨角色/失效/未来时间隔离。

REG-006、REG-007、REG-008 的 owner 为本仓库。已有 startup/memory structured output/rolling summary/source/authority 测试实际执行；上述相关 15 模块最终 full 全部通过。未修改快环解析器，也未将先前 P0 聊天启发式软放行扩展到记忆路径。

## Final Git Diff Review

- `git diff --check`：PASS。
- `git apply --check --reverse review/final-diff.patch`：PASS；patch 采用原始字节生成，包含 tracked diff 与四个新增代码/测试/runner 文件，没有 stage。
- `classify_change.py --base 6c1d238`：RED。
- `audit_diff.py --base 6c1d238 --allow-domain memory --allow-domain cognitive-loop --allow-domain generation --allow-domain workflow-governance`：NEEDS_REVIEW，no out-of-scope production file。
- `build_review_report.py` 同样 domains：NEEDS REVIEW / RED，`review/latest.md`。
- 人工审查：没有新 authority store、schema/API/parser 改动、silent success fallback、未授权写入者或无关重构。敏感 SQL/后台异常处理用于既有状态协作、回滚与隔离；不放宽 validator。

## Remaining Risks / Unverified

- 生产 `api.dzzi.ai` 的确切带标签模型名未验证。假 Provider 证明 MODEL_MAIN 原样传入，没有通过模型列表或付费请求猜名称。
- 未运行真实 ≥100 条消息的 shadow 评估；不能证明自然语言提取准确率。尤其 object/predicate 分类仍需影子评估，确定性逐字/否定检查不等于完整语义证明。配置保持默认禁用与 shadow。
- PGlite 不等于真实 PostgreSQL 多副本和连接池并发验收；未知 ack 用事务提交后抛异常模拟，不是真实网络断连。
- linked_fact_id 优先同源/最近有效 fact，仍是粗关联，没有新增语义匹配。
- 最终 full 不是全绿；57 条共同失败和一次 cleanup flake 原始记录完整保留。
- main 无 `scripts/analyze_candidate_shadow.py`。旧 shadow 分支仍存在，未合并其不同日志协议。
- 旧 raw hard-purge 按裸 event_id 清理 EPS，跨用户重用 ID 时可能清理其他处理凭据；稳定 operation 防重复投影，此潜在清理问题留作独立 follow-up。

## Git State

- 本任务：`.worktrees/memory-candidate-producer-20261020`，detached HEAD `6c1d2387be05708443489ed9fe0107f7921b8e0d`。
- 修改状态：5 个 tracked 文件、3 个新增产品/测试文件，以及 review 支持文件；均未暂存、未提交。
- 原工作目录仍为 `feat/time-cognition-projection-20261014 @ b22841ebc900939985120c6e94d6f41f79a9dd69`，原有改动和 P0 worktree 未修改。
- 本地 main 仍 `610564654f79229aea4590f5608723458c9b0145`，origin/main 仍 `6c1d2387be05708443489ed9fe0107f7921b8e0d`；没有移动这些引用。
- Commit：无。Push：无。Merge main：无。Deploy：无。生产 DB / backfill / 付费 API 测试：无。
