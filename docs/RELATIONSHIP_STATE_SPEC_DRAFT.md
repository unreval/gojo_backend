**关系与内心状态唯一基线规格草案（Relationship State Spec Draft）**

> 文件：docs/RELATIONSHIP_STATE_SPEC_DRAFT.md
>
> 日期：2026-10-13（Asia/Singapore）。
>
> 状态：合并规格草案；尚未实施、迁移、回填或上线。技术路线以 A 为基线；产品待决项不因写入本文而获准启用。
>
> 当前静态核对基线：分支 `fix/current-turn-grounding-20261010`，HEAD `c22ac18534fdc75d91380ce1632118b326fcc4c2`。行号只对应此基线，实施前须重新核对。
>
> 本轮授权仅为新建本文。没有修改现有文档或代码，没有连接数据库，没有调用 LLM／Provider，没有运行应用测试，没有 commit、push 或部署。

**1. 来源、合并范围与事实状态**

A：仓库 `docs/RELATIONSHIP_STATE_DESIGN.md`，2026-10-10、11 节，作为架构、存储和生命周期基线。A 记录的旧源码基线 `ba697179…` 不作为本文的当前代码证据。

B：用户附件《关系与内心状态设计》v1，2026-10-13、18 节，附件路径为 `C:/Users/张三/.codex/attachments/d6a74790-18ae-4eb6-8052-de6388bea9ed/已粘贴的文本.txt`。独立“核对修订 v2”全文在本轮可定位输入中未找到；本文不把上一份核对报告冒称为 v2。v2 的独立增补暂列待输入，取得后只能按本节允许范围核对差异，不自动改写基线。

C：上一轮只读核对报告，作为纠正事实描述和列出实施缺口的依据。

B 仅保留四类内容：内心卡片 v2 的结构；标签集的候选想法；对外部桥接的评估；用户决策清单。B 的窗口算法、新表、欠账阈值、动力单位、预算数字、逐对 stream 验收和第二次失败策略均不另行合并。不得在 A 与 B 间混用单位或同时启用两条生产路径。

本文区分：

- **VERIFIED**：当前源码静态核实，不等于测试执行通过或生产部署一致。
- **UNVERIFIED**：线上数据、生成原因、效果、价格或尚未实现的接口能力。
- **规格要求**：后续实现必须满足的契约，当前不声称已存在。
- **待用户决定**：产品语义、体验或运行额度；未决定不隐含采用建议默认值。
- **A 候选参数**：保留作可重复 fixture 和评测的起点；不等于生产参数已获批准。

本轮交付风险为 GREEN（只新增文档）。后续机制的 YELLOW／RED 分类见第 4 节；本文不授权任何后续实施。

**2. 当前问题、唯一权威与保护边界**

已静态确认的结构问题：

1. 现有数值 writer 会限幅，封顶后不能表达继续上升的幅度；近期观察与长期账本尚未分开。
2. 四个关系语义键有定义和 reader，当前确定性主路径没有对应专用生产规则。缺合法结论时回落到 unresolved；合法既有结论仍可被读取。
3. panel 仍将部分数值和 legacy/debug 投影放入生成格式；旧投影没有关系身份权威。
4. 生成入口的候选、最终正文和发布检查不统一；voice_stream 有提前释放候选 JP 的路径。
5. 七个 apply_* 都调用 provenance，但事务外存在状态先提交、日志后提交的窗口，来源也可能为空。

源码：`relationship_engine.py:25`、`relationship_semantics.py:8`、`cognitive_worker.py:108`、`cognitive_revision.py:269`、`cognitive_reader.py:149`、`relationship_panel.py:14,113,183,194`、`relationship_state.py:130,163`、`route_voice_stream.py:225`，均在 `gojo_backend/` 下。

未核实：生产 build、角色 ID 对应、用户提供的线上分数、历史摩擦来源、标签准确率，以及中转对刻板回复的因果影响。本文不据这些未知项补写角色感情或用户事实。

| 内容 | 现有 authority／正式路径 | 本规格的变化边界 |
|---|---|---|
| 用户原话、角色已提交发言、主体与来源状态 | chat_log／raw_events 与 canonical ingress | 只引用结构化 ID、原文及有效性；不修改原话，不把显示称呼当 identity |
| 数值关系账本 | rel_state、正式 apply_*、rel_provenance_log | 本基线不增加主观数值写入，不重置或补偿历史账本 |
| 有效角色单方表态 | rel_declared_stance 及 canonical utterance/adjudication gate | 只记录正式校验过的完整角色原话，不承载用户边界或双边确认 |
| 关系命题、里程碑 | Cognitive questions／evidence／deterministic revision | 明确证据规则；用户接受必须有独立用户来源 |
| 主观内心 | 现有 Cognitive hypotheses 中的受限投影 | 增加有类型、有来源的主观分支，不新建 rel_inner_state／rel_inner_event |
| 标签注解与近期统计 | rel_interaction_stats 的拟议非权威注解，reader 派生窗口 | 注解不是事件事实或第二关系账本 |
| 服务应答义务 | cognitive_questions 的拟议 answer_debt metadata | 与关系命题分 scope，依据最终回答事件和 receipt |
| 卡片、路线、闸门、音频 | reader／generation／既有 commit 与 receipt／TTS | 控制表达与交付；不裁决事实，不把播放当新发言 |

authority 依据：`.codex/skills/gojo-safe-change/references/authority-map.md:111`。保护 INV-AUTH-001/002/003、INV-EVID-001/002/003、INV-COG-001/002/003、INV-ID-001、INV-STATE-001/002、INV-PERSIST-001/002、INV-FAIL-001/002、INV-GEN-001。

Slow Loop 只能提供候选并经过正式 evidence／validation／revision。不得在 cognitive_queue 的周期提交路径调用关系数值 writer。生成文本只有在 canonical commit 和正式裁决之后，才可在其有限范围内证明“角色确实说过这句话”；它不能证明用户心理、用户接受或外部承诺已兑现。

**3. 六处冲突的唯一处理**

| 冲突 | 采用 | 理由与规格 | 当前源码证据／实施缺口 |
|---|---|---|---|
| 近期窗口 | A | 最近 7×24 小时与此前 7×24 小时对照，最多扫描 14 天；输入为有效 canonical 事件及经校验的新注解。旧 provenance 仅用于历史账本审计，不作完整互动贡献输入。时间尺度的生产偏好仍待决定。 | relationship_state.py:130 只记 after-before；封顶可为 0。cognitive_replay.py:61 不实现这种窗口；新注解与计算待实现。 |
| 主观状态存储 | A | 复用 Cognitive hypotheses 的受限主观快照；不新增 B 的两张权威表。engagement_style／romantic_openness 从同一快照只读投影，romantic_label 独立。 | cognitive_db.py:239 有容器，:263 同 key 唯一；cognitive_reader.py:226 当前不读取所需 metadata；现类型、键共存和 writer 需扩展。 |
| 欠答案 | A | 确定性识别、cognitive_questions 服务 episode、最终文本检查、commit＋receipt 后正式计数／清偿。标签和模型自报 stance 只可提供候选，不能成立义务或自证已回答。强制力度待决定。 | cognitive_db.py:180 有 questions；generation_contract.py:31 没有 stance；cognitive_events.py:140 的 resolved 没有通用 reopen。 |
| stream 验收单位 | A | 一次完整逻辑 assistant 回复的全部 JP/ZH pairs。chunk／pair 只作交付单位。首音等待的体验方案待决定，不允许以逐对验收宣称整消息合规。 | route_voice_stream.py:229 先发 JP，:238 才查 pair；后段可改变整体含义，已播内容不能撤回。 |
| 第二次失败 | A | 所有硬错误共用首次＋至多一次修复；仍失败只可使用通过全部硬检查的确定性回应，或明确失败／取消。软重复不自动增加付费修复；具体软质量策略待决定。 | route_chat.py:608 限制候选数，:706 第二次拒绝返回空；generation_contract.py:95 只恢复正文，不是语义兜底。A 的安全兜底亦待实现。 |
| 中转对照结论 | A | 各 5 次仅作初步比较；固定版本、输入、采样和状态，区分原始输出与应用后文本。版本不匹配只比较服务组合，不作中转因果结论。 | ai_client.py:61 默认 temperature=None，:115,:139 仅显式值才传；route_chat.py:741 的最终处理也可能影响表达。当前无该实验结果。 |

A 的存储选择是本草案唯一技术路线。产品待决项通过版本化配置收敛，不允许实施者自行改成 B 的新表或第二处理器。

**4. 每个机制的运行契约与风险**

表中验收均为后续计划，当前未执行。回滚均保留 canonical 历史与审计记录，不以删表、清零或恢复已撤回授权作为回退办法。

| 编号／机制 | 是否调用 LLM | 写入 authority／容器 | 失败降级 | 必需验收 | 回滚 | 实施风险 |
|---|---|---|---|---|---|---|
| M01 注解、类型与迁移 | 不调用 | 拟扩展 rel_interaction_stats 注解；正式 Cognitive typed writer；不写 rel_state | 结构、类型或身份不兼容则禁用新写入，不临时换 store | schema/index 兼容、原子写、类型隔离、重启不改旧数据 | 停新 writer、退读版本，保留新增审计 | RED：schema／provenance／持久化 |
| M02 近期窗口与派生缓存 | 计算不调用；可读取已验收传感器注解 | 不新增权威写；窗口／缓存是 reader 投影 | 缺来源／覆盖不足显示 unknown 或观察不足 | 虚拟时钟、分桶、去重、失效、空缓存、饱和对照 | 关新窗口，保留安全 reader，不改旧账本 | YELLOW；新增注解 schema 归 M01 RED |
| M03 标签传感器与预算 | 可选异步 LLM；事实 Slow Loop 仍为零 LLM | 候选／验收注解；只能经正式路径进入 M04 | 失败、超额、认证错误不更新；stale 或 unknown；不阻塞聊天 | 标签标注集、引用、跨用户、额度并发、Provider 故障 | 停传感器，隔离候选，不继续读失效来源 | RED：新持久化来源语义及后台生产路径 |
| M04 主观快照、原因与重算 | 确定性；不为回放再调用 LLM | cognitive_hypotheses 受限类型及正式 cycle 审计 | 碰撞／来源失效立即停用相关投影；校验失败整事务回滚 | 类型不 promote、排序／分批一致、限幅、撤回重算、key 碰撞 | 停 typed writer、退版本、清派生缓存；审计保留 | RED：认知／provenance 语义 |
| M05 里程碑与表态 | 权威判断不调用；表达可用原生成 LLM | questions/evidence/revision；有效角色原话可进入 declared_stance | 不明确则 pending，不制造确认 | 单方／双边分离、目标 scope、拒绝撤回、删除、状态版本 | 退解释版本但不恢复撤回许可，不抹历史 | RED：关系语义与证据规则 |
| M06 问题应答与清偿 | 识别／计数／清偿不调用；回答沿原生成 | cognitive_questions answer_debt；canonical 回答＋receipt | 生成失败不计回避、不清偿；失效来源停授权并修订 episode | 一次清偿、拒候选零计数、交付未知、resolved 修订 | 关强制展示，不抹已发生应答，不错误 reopen | RED：服务 question 持久化与 receipt 关联 |
| M07 卡片 v2 | 渲染不调用 | 不写 authority；读有效快照、边界、question | 缺项明确未知；不补人设心理或数值 | 无数字／身份偷渡、来源失效、模板未知分支 | 退安全卡片版本，仍不以 legacy 授权身份 | YELLOW |
| M08 全入口生成闸门 | 检查不调用；至多一次原生成修复 | 通过后沿既有 canonical commit／effects；闸门不新建关系判断 store | 合规确定性回应、generation_failed 或可选消息取消 | 每入口完整候选、最终正文、零失败副作用、重试总量 | 退软策略；保留已确认的来源、身份、边界硬检查 | YELLOW；新增声明／receipt authority 合同为 RED |
| M09 完整语音与播放绑定 | 闸门无 LLM；语音内容沿原生成；TTS 为音频服务 | 同一已验收消息／receipt；播放无关系写 | 完整合规文字或失败；TTS 故障允许文字；未知交付恢复原消息 | 零提前字幕／音频／候选 TTS、断线、摘要／段序匹配 | 改完整验收后的非流式交付，不恢复提前泄露 | YELLOW；新增持久化 receipt/schema 为 RED |
| M10 旧边界隔离 | 不调用 | 不改历史 rel_state/log/hits；有效边界仍由原证据 authority | 未核实摩擦不当新内心证据；保留合法边界 | low 残留、用户／角色方向、未知来源、读侧兼容 | 停新投影，不恢复 observer 判分或撤回边界 | YELLOW；历史迁移／数值恢复另为 RED |
| M11 表达路线、近期句式与采样 | 路线／指纹不调用；生成用原 LLM | 可重建表达缓存，不写关系 authority | 合法路线为空则明确未知／边界回应；参数不支持显式记录 | 同 seed 重放、拒候选不入缓存、事实不变、重复／自然度 | 关路线及软去重，保留硬闸门 | YELLOW |
| M12 中转对照 | 后续实验调用 LLM；本轮零调用 | 仅去标识实验记录，不写业务 authority | 版本／输入无法对齐则结果不确定或服务组合比较 | 固定请求、随机交错、原始／处理后分开、有限样本结论 | 停实验，不改变业务状态 | YELLOW，付费／数据发送须另授权 |
| M13 可选关系侧数值 processor | 不调用 | 若另行批准：rel_event_applications＋正式 rel_state writer＋provenance 同事务 | 失败全回滚；结果未知单列；来源失效停止授权并重算 | 幂等、提交窗口、SQL 中止、并发、纠正／删除、版本升级 | 停新应用，按批准版本重建；不以负 delta 抵消 | RED；本基线不启用 |
| M14 启用、版本回退与保留 | 不调用 | 版本选择及既有审计／配置路径；不新建事实库 | 兼容失败关闭新分支；不在启动时自动回填 | 开关、重启、缓存丢失、批次中断、源撤回、回滚 | 停新接收、退兼容 reader；保留历史和有效失效标记 | YELLOW；DDL／数据迁移／生产操作为 RED |

**5. M01／M02：非权威注解与近期窗口**

**5.1 注解与应用身份**

在现有 rel_interaction_stats 容器中承载固定标签候选、接收／拒绝决定、引用范围、置信度、source snapshot、policy/model version、occurred_at、processed_at 和首次 accepted_at。这些是拟议新增列或版本化 metadata，不是当前已存在字段；DDL 见 relationship_db.py:274。

主体为稳定 user_id／character_id；源身份包含 source_chat_id／source_scope 与 canonical event_id，不假定 event_id 在所有聊天中全局唯一。同一事件跨文本、媒体说明、转写和重试不重复采样。注解本身不能证明“用户确实如何想”或双边身份。故事台词、日记生成、主动输出和离线内心不作为新的亲近样本。

必须先解决现索引：relationship_db.py:294 的唯一键已有 `(user_id,character_id,source_event_id)`。再增加含 policy_version 的唯一键并不能允许同源多版本行。实施前须评审“同一行内版本化注解”或经 RED 迁移改变索引的方案；未解决不写新版本，不绕过约束建第二表。

每个验收决定须保存输入版本、引用和依赖、拒绝理由、首次接收时间及消费身份。处理同一来源的重试不能改变 accepted_at、取得新额度或重复应用。来源在提交前失效则拒绝该贡献。

**5.2 时间与计算**

采用 A 的窗口定义：在 UTC 时刻 T，近期为 `[T−7天,T)`，对照为 `[T−14天,T−7天)`。对任意窗口结束时刻 T，d=0…6，第 d 桶为 `[T−(d+1)×24h,T−d×24h)`，桶中心年龄为 `(d+0.5)×24h`；边界事件只能进入一桶。显示转用户时区，计算不随显示时区改变。

以下为 A 候选参数 profile，不在生产默认启用：

- 每类标签每天最多接收三个独立用户事件，间隔至少 60 分钟；同轮同义重复只计一次。
- 每事件生成三个观察分量：亲近、收缩、互动压力，范围 [0,1]。亲近为 `clip(max(0,a_e)/0.12,0,1)`，收缩为 `clip(max(0,v_e)/0.12,0,1)`；压力在 DISAGREEMENT 为 0.6、仅 REQUEST_SPACE 为 0.3，其余为 0。
- 修复的负 V 贡献用于主观演化，不将窗口收缩分量记为负。AMBIGUOUS 不入样本；确定无关的 OTHER 为零观察。
- z_e 使用基础贡献形成，不预乘 confidence；q_e=confidence 在桶均值 `b_d=Σ(q_e×z_e)/max(1,Σq_e)` 中只乘一次。空桶为零。权重 `w_d=2^(−age_d/3天)`，age 为桶中心距窗口结束时刻的年龄。
- `R(T)=Σ(w_d×b_d)/Σw_d`，分母包括全部七桶。近期与对照分别用自己的结束时刻计算权重。
- `D=(R亲近−R收缩)近期−(R亲近−R收缩)对照`。D≥0.12／≤−0.12 为靠近／收缩，其余变化不明显；近期亲近、收缩均≥0.25 可显示拉扯。
- 两个窗口各至少三个合格事件且覆盖两个日期才显示对比趋势；否则观察不足，不把零样本说成疏远。

canonical 事件时间用于窗口；首次验收时间用于处理顺序和接收额度。迟到注解不能伪装成当时已知的内心。沉默不扣 trust，不证明用户拒绝。

每类每天三条与六十分钟间隔的采样策略还须冻结：按源事件日还是接收日、选择排序、同时间 tie-break 及来源失效后如何重选。不得由实现者临时决定而改变可见趋势。滚动桶是分段计算，不宣称与主观动力相同的连续平滑衰减。

旧 rel_provenance_log 保留审计用途。日志 after-before 是实际限幅结果，缺来源、漏记及并发覆盖都可能影响重放；不得把其净和当完整的新窗口输入。历史数据是否足够仍为 UNVERIFIED。

reader 缓存是拟新增派生能力，不称“已有 TTL 接口”。候选缓存键包括 user／character、有效来源版本、watermark、规则版本和时间桶，并保存 as_of。A 的 60 秒 TTL 只作评测起点；须明确允许的时间误差或在下个窗口边界／贡献到期时失效，不能仅凭同桶 key 返回过期趋势。trigger/dependency 来源、原文 hash 和配置版本检查先于 TTL；缓存丢失只增加重算，不改变结果或 authority。

**6. M03：唯一标签候选集与预算**

B 的标签想法只作为 A 规范集的别名／输入词汇。规范化只有一条路径，模型不得同时输出两套含义不同的标签。

| B 的想法 | A 规范候选 | 限制 |
|---|---|---|
| care | CARE_OFFER | 当前、指向角色的支持表达；不推断动机 |
| vulnerability | PERSONAL_DISCLOSURE | 用户主动分享经历／感受；不意味着自述现实事实已独立核实 |
| affection | AFFECTION_OR_FLIRT | 当前亲昵表达；不确认身份或角色感情 |
| probe | REQUEST_CLOSENESS 或 AMBIGUOUS | 明确希望靠近才用前者；泛泛试探保持未知 |
| direct_ask | FEELINGS_QUESTION | A/V 贡献零；仅提候选，确定性确认才建立义务 |
| conflict | DISAGREEMENT | 当前互动分歧；不推导恶意、越界或信任惩罚 |
| repair | REPAIR_OFFER | 修复提议不等于修复成功 |
| withdraw | REQUEST_SPACE 或 AMBIGUOUS | 明确暂停／距离请求；沉默、忙碌、短回复不算撤离 |
| boundary_set | DECLARATION_CANDIDATE／user_boundary | 经正式校验成为用户边界；设边界本身不加摩擦或动力，不映射成 BOUNDARY_ACK |
| neutral | OTHER 或 AMBIGUOUS | 明确无关才 OTHER；不确定必须 AMBIGUOUS |

A 的 BOUNDARY_ACK 保留为“明确尊重已知边界”的独立候选；它与设边界不同。DECLARATION_CANDIDATE 还可提单方表态／关系提议／撤回候选，均无直接确认权限。

模型只看 canonical 用户事件及必要上下文，不看旧身份标签和长期总分。输出只允许：程序提供的 event_id／target_ref、枚举标签、confidence、原文范围与 hash、literal／quoted／hypothetical／ambiguous、全部 dependency_event_refs。禁止自由 topic_id、用户动机、角色心理、数值增量和自由原因。每事件至多三个标签为 A 候选上限。

原文范围完全匹配仅证明引用真实，不证明分类正确。主体、对象、模式、来源、上下文和语义校验均必须通过。confidence 必须为有限数且在 [0,1]；A 的 confidence≥0.85 接收、0.65≤confidence<0.85 待观察、其余 unknown 作为标注校准起点；模型自报 confidence 不是已校准概率。数据库写入前逐条校验候选，不让一条非法候选污染其他来源；SQL／commit 失败则回滚整个当前事务，不捕获数据库异常后继续写或报成功。

A 候选批量为每五分钟或累计八条触发、每批最多八条、输入≤2400／输出≤800 tokens。候选额度为每用户／角色每天≤6次实际尝试、≤18k输入／≤6k输出；跨角色≤12次、≤48k总 tokens。是否发送真实消息、模型选择、token 截断策略和生产额度待用户决定；默认不启动传感器。

现有 cognitive_request_budget 是请求级容量／尝试控制，见 cognitive_request_budget.py:1,16,121；delayed_reply.py:476 是局部 circuit。不存在已核实的全后台每日费用 ledger／共享熔断接口。统一预算须先设计 reserve／settle、并发占额、结果未知计数、SDK 重试计数与退避；不得以“共用已有 spend guard”跳过这项前置条件。

标签失败跳过本次主观更新；事务失败必须回滚并报告失败；提交结果未知必须单列，不能吞异常后报成功。可验证旧状态可显示 stale，来源失效则不能继续展示。事实 Slow Loop 保持 model_calls=0，当前直接问题由确定性规则处理，不等待传感器。

**7. M04：主观快照、原因与有效来源重算**

**7.1 存储与正式提交**

遵循 A：当前主观快照留在 cognitive_hypotheses，标记 `record_class=subjective_relationship_v2`，存 A/V/C、原因、版本、完整来源、接收／处理时间及重算摘要。它不能 promote 成 factual belief、进入事实 recall 或写关系数值。

A 原拟使用 relationship.internal_conflict 作为快照入口；实现前须解决 key 与类型共存。cognitive_db.py:263 的唯一键不因 metadata.record_class 而允许同 key 并存；reader 当前可能优先已有 active belief。不得覆盖有效 canonical 项或借 subjective 类型改变既有结论。必须明确命名空间／键迁移／reader 优先级和当前版本选择，碰撞未解决时新分支保持不可用。

hypothesis_type 现有枚举没有这个独立主观类型，见 cognitive_db.py:239；cognitive_reader.py:226 的查询未获取所需 metadata。cognitive_output.py:1186 会拒绝非空 hypothesis_updates 等外部 judgment candidates。因此需要正式扩展 typed validator／writer／reader，不能把新 payload 填进旧接口后宣称已经支持。

cycle 的 hypothesis_updates／evidence_refs／summary 可作为 A 选定的审计容器方向，但输入、前后值、限幅、拒绝决定、时间、版本及来源的完整持久化须由正式事务契约证明。注解验收与快照消费身份、版本比较、额度决定不得留下“一处提交、一处漏写”的状态。

不得以 raw_events.attach_event_annotation():848 代替完整原子审计：该现有 helper 自开连接并提交，payload 还会截断到 4000 字符。新增审计载荷必须证明不丢来源／规则输入且可加入正式事务；失败 attempt 身份与首次 accepted application 身份分别保存。

engagement_style／romantic_openness 从同一合法主观快照作只读表达投影；romantic_label 只来自 M05。generation 卡片不另存关系判断。

**7.2 动力与原因**

保留 A 的 [0,1] 单位，不合入 B 的±15／3%衰减／连续三次规则。无来源为 unknown，不拿长期 100 初始化内心；首个合格观察从零起步并标 warming_up。

A 候选贡献：CARE_OFFER +0.08 A；PERSONAL_DISCLOSURE +0.06 A；AFFECTION_OR_FLIRT +0.10 A；REQUEST_CLOSENESS +0.06 A；REQUEST_SPACE +0.12 V；DISAGREEMENT +0.06 V；REPAIR_OFFER +0.06 A/−0.04 V；BOUNDARY_ACK +0.05 A/−0.03 V。亲昵与有效边界 scope 确有冲突时，才可条件性 +0.03 V；边界存在本身不制造 V 或纠结。

A 的平滑／限幅 profile 为评测起点：

- 每事件各动力 |Δ|≤0.12；固定五分钟逻辑桶各动力影响≤0.18，Provider 分批不改变桶。
- 每 UTC 接收日、每角色各动力正向／负向额度分别≤0.30，不能正负抵消绕额。
- 有界目标先 clip；投影每次至多推进目标距离的 0.35，溢出贡献留痕且不积存未来隐藏动力。
- A/V 无新证据半衰期七天；C 目标为 min(A,V)，须两侧有效支持，或有效边界与本轮靠近事件构成冲突；不能同一句换标签制造两侧证据。
- C 单事件变化≤0.08、每日正／负额度各≤0.20、无新证据半衰期三天。明显纠结候选门槛为≥0.30且两次独立支持相隔≥30分钟，退出≤0.18。
- A/V 档位候选进入值为 0.20／0.45／0.70，退出低 0.07。生产采用哪些参数须标注评测版本并获用户确认。

一次事件的计算链明确区分基础贡献、confidence 加权提议、bounded proposal、衰减后旧状态、target clip、0.35 推进和 actual applied delta。单事件 proposal 上限与逻辑桶／日额度的实际应用量不得混称 delta；同一事件多标签合并后只推进一次，C 也只更新一次。逻辑桶及每日正／负额度均计实际应用绝对量；超额或 clip 丢弃部分不留待以后释放。连续支持指不同 canonical 事件，不是标签数、API 次数或重试数。

原因只能来自固定目录和可核对触发：care_received、openness_received、affectionate_signal、closeness_invitation、requested_space、unresolved_disagreement、repair_received、boundary_respected、active_boundary_tension、unanswered_feelings、canon_expression_limit、insufficient_observation。

每项保存 trigger/dependency refs、quote、contribution、时间和 source hash。人设原因另带 config_ref／config_version，不能冒充用户原话，也不能从“怕伤害她”推断用户真实心理。具体人设目录及每卡最多两条还是三条待用户决定。

**7.3 撤回与重算**

同规则回放使用固定注解、首次接收时间、消费决定和事件排序，不重新调用 LLM。按 `(accepted_at,occurred_at,event_id)` 重建；改变分批不改变结果。规则升级或重新标注是新版本估计，不能伪装成旧历史回放。

验收、读取及生成发布前均校验全部 trigger/dependency 的 active 状态、主体／来源作用域、原文 hash 和配置版本。查询异常为 unknown 并停止相关授权，不能等待后台重算时继续用旧快照。规则升级只切换已校验的新版本并重建，不把新旧版本 delta 相加，不刷新原事件 accepted_at 或重复取得额度。

来源删除、纠正、撤回、supersede 后 reader 立即停止依赖授权，再从合法源重建。失效不能被平滑、滞后、TTL 或旧日志延长。版本重算中不得再次消费原事件额度；新版本与原接收决定如何对应是实施前必须冻结的技术契约。

**8. M05／M06：里程碑与服务应答分别管理**

**8.1 里程碑**

沿用明确表态、双方确认、撤回三类证据，但具体允许何种感情表达、何种命题表示何种关系由用户决定。

单方表态只证明某方说过；关系提议保持 pending；双边确认要求同一 proposition／对象／scope 的角色表态和独立用户接受均有效，并有 reply_to 或确定上下文绑定。拒绝／撤回收缩相应许可，不泛化为永久拒绝所有亲密。删除是证据失效，不伪造新的拒绝事件。

角色发言须完整验收、成功 canonical commit，并通过 evidence/adjudication；declare_stance() 物理 INSERT 不是裁决。当前有效 stance SQL 要求完整原话、active 角色来源与 applied adjudication，见 relationship_state.py:414,434。双方确认复用 questions/revision，不塞入角色单方 stance。

数值、A/V/C、标签、次数和欠答案清偿不能自动确认身份。未知语句保持 pending。已 resolved 的 question 不强写 reopen；撤回通过正式新版本／修订问题处理，reader 先停止旧授权。

**8.2 直接问题与应答 episode**

采用确定性有限语法、对象／reply_to／scope 识别，排除引用、虚构故事、第三方、假设、取消请求及群聊对象不明。FEELINGS_QUESTION 只提候选；无法确认时可提示“可能需要回应”，不增加强制义务。

每 episode 的稳定 identity 包括 user、character、source_chat_id／source_scope、ask_event_id、问题 scope；重复追问关联未结 episode。cognitive_questions.metadata.answer_debt 拟存 ask refs、last ask、eligible reply count、evasion count、force_due、answer mode、settled_by、policy version。它是服务应答记录，不是 romantic_label。

answer_intent 可作候选，须校验真实 JP/ZH 及 quote range。当前 generation schema 未支持该字段，见 generation_contract.py:31；是否增 envelope、使用既有可审计元数据或其他兼容方式须合同评审，不能只在 Prompt 要求额外键。

只有最终已验收且成功提交的回答事件，结合 receipt／实际交付状态，才能正式计数和清偿。拒候选只记 gate rejection；生成失败、忙碌、延迟、离线不算角色回避。同一 receipt 重放不二次计数。commit 成功但交付未知不能直接记“用户已得到答案”，须保留送达未知并恢复原消息；具体清偿时点待决定。

正面回答可以是获准感受、明确不确定、受支持的拒绝或具体限制，不强制说喜欢。“以后再说”只记 deferred，不自动清偿或创建 promise。清偿来源失效立即停止视为有效；通过修订 episode 处理，保留旧审计，不能绕过 cognitive_events.py:140 的 resolved 协议。

A 的强制 profile 保留供评测：首次问即要求正面处理；第二次追问、两次实际回避，或超过 24 小时且已有可答机会可 force_due；强制轮先 1–3 句回答，每轮最多处理一项。是否采用这些阈值、是否主动补答、延期清偿条件均待用户决定。联系限制、用户暂停／撤回及紧急主题优先，不能以欠答案为由催促用户互动。

Canon Lock 只修正误扩大的作用域，保留原作未知私人经历不可编造。canon_lock.py:10–18 的“这类话题”不能泛化成所有感情问题。

**9. M07：内心卡片 v2 唯一模板**

保留 B 的趋势、靠近／回避、纠结原因、待回应、里程碑、路线和表达范围结构；填充数据遵循 A。模板是程序从合法状态渲染，不让生成器现场补内心判断。

```text
【此刻的内心｜v2：读取已形成状态，只负责表达】
观察：截至[本地时间]；[已观察／正在形成／观察不足／更新滞后]。
近期：[最近7天相对前7天的趋势／不足以比较]；[有来源的近期变化摘要]。
靠近与回避：[可观察的靠近强度]；[可观察的收缩强度]。
纠结：[未观察到／正在形成／明显／尚不清楚]。
原因：
- [有效观察或已批准人设规则支持的短句]。[事件引用／配置版本]
- [另一个独立支持原因；无则省略]。[引用]
待回应：[有效问题的必要原文／无]；[应答状态，不臆造回避次数]。
本轮要求：[由已批准应答策略填写；允许坦率未知，不强制感情结论]。
里程碑与表态：[单方表达／提议待决／双方明确回应该命题／已撤回／未形成]。[有效引用]
本轮表达路线：[合法集合中按固定种子选择的一条]。
表达范围：[有支持且获准表达的内容]；[有效边界与明确设定禁限]。
避免重复：[最近已提交可见回复的少量句式摘要]。
```

不显示原始分数、legacy label 或 romantic_label 枚举；结构化命题授权单独读取，不由卡片自然语言裁决身份。来源详情可展开，生成只见本轮必要片段。

B 的“你都没有正面回应两次”和人设原因示例不作为事实默认文案。只有有效最终回答记录才能计回避；“不在同一次元”等内容须批准的人设配置，不能自动加到任意角色。数据不足时省略原因并显示未知，不写“其实很纠结”。

是否允许心动、在意、需要空间等表达及透明程度待用户决定。模板不得暗设永远朋友，也不得把主观靠近写成交往已成立。A 的约 500 中文字／700 tokens 仅为成本评测起点，实际 tokenizer 和可读性须测量。

**10. M08／M09：所有入口的完整生成与交付闸门**

**10.1 共同验收单位与顺序**

“逻辑消息”是一个角色一次准备发布的完整气泡集合，包括全部 JP、ZH 和影响表达的 intent。群聊以每角色一次生成集合为单位；故事以完整故事与框架句为单位；stream 不以 pair 为关系验收单位。

顺序：来源与授权快照 → 生成完整候选 → 协议及原有业务检查 → 完整关系／边界／应答检查 → sanitizer／合并／截断／切段 → 最终实际正文复验与来源版本协调 → 正式 commit／receipt → 被许可的交付与副作用。

最终检查必须早于该 assistant 候选的 schedule/promise/transaction、offline state、表态、问题清偿及可见发布。原用户事件仍可按既有 ingress 留存，拒 assistant 不重复或撤销用户原话。跨表原子性／outbox／既有 effects 协调须另行明确，不假定 callback 本身提供事务。

检查默认确定性、零 LLM：来源失效、越权身份／双边确认、用户心理和外部事实、表达边界、强制应答、JP/ZH 新增断言、段落矛盾及软重复。受限声明 intent 必须与正文一致；自由语言隐含关系无法靠词表证明完全理解，需盲评测漏检。

现 reject_fn(parsed) 在 route_chat.py:600,701，仅检查候选；_finalize_committed:741 不重跑语义 callback。当前 helper 没有完整关系闸门、hard/soft 策略或安全语义兜底；实现必须补合同，不能只新增一个 reason 字符串。

**10.2 逐入口清单**

下表全部为拟接入位置。所有生成入口的检查／渲染无新增 LLM，修复使用原生成模型；纯播放入口无 LLM。风险及通用回滚为 M08／M09，涉及新 authority／schema 升 RED。

| 入口 | 验收单位 | 最小接入位置与当前源码 | 失败／特殊边界 |
|---|---|---|---|
| /chat/text | 本轮全部最终 JP/ZH 气泡＋表达 intent | route_chat.py:1263 组合 calendar/schedule callback；:1300 finalize 后复验，早于 :1315 schedule commit、:1385 effects | 合规确定性回应或失败；拒候选零副作用 |
| /chat/story | 完整故事＋对用户的框架句 | route_chat.py:1460 补 callback；:1466 后复验，早于 :1470 状态、:1474 保存、:1485 TTS | 合规短故事或失败；人物台词不确认当前关系 |
| /chat/voice_story | 完整语音故事全部 JP/ZH | route_chat.py:1727 已有 schedule callback，组合检查；:1737 后复验，早于 :1741 提交／状态／保存／TTS | 合规短故事或失败；音频只取已验收正文 |
| /chat/voice_text | 本轮全部最终气泡 | route_chat.py:1629 组合 schedule callback；:1639 后、:1643 提交和 :1652 保存前复验 | 合规完整文字或失败；TTS 故障不撤销已提交文字 |
| /chat/voice_stream | 一次完整逻辑回复的全部 JP/ZH pairs | route_voice_stream.py:225–238 当前先发 JP；改为全缓冲、完整与最终验收后才 text_jp／TTS／audio／保存 | 截断、超时不释放半句；可用完整文字或失败；不吞 guard 错误放行 |
| /chat/proactive | 本次主动消息全部气泡 | route_chat.py:1536 补 callback；:1542 后、:1551 保存／:1555 canonical append／TTS 前复验 | 可选消息取消；不强发模板、不记回避 |
| /chat/voice/proactive | 最终保留的 1–2 个气泡 | route_chat.py:1866 组合 callback；:1875 finalize；现 :1881 schedule commit 早于 :1886 截断，须先确定最终集合并复验，再授权状态／保存／TTS | 被截掉后半段不能证明最终已回答；失败取消 |
| delayed_reply.generate_delayed_chat_reply | 绑定 pending bundle 的完整回复 | delayed_reply.py:287 attempts=1 无 callback，补完整检查；保留 :299 schedule；:303 后、:314 intent／TTS／:335 visible commit 前复验 | 失败保留可重试 pending；成功 commit 后才 consume／resolve；重试不二次补答 |
| image／video，/chat/image | 完整最终 JP/ZH 回复 | route_image.py:355 is_video；:578 独立循环；:605/:607 协议／schedule 后检查，:636 整理后、:655/:669/:744 effects 前复验 | 合规回应或失败；视觉摘要／OCR 猜测不授权关系事实 |
| 独立 /story/generate | 完整故事＋所有最终 chunks | route_story.py:232 直接 Provider；parse 后所有 best/rescue/fallback 同查；:283 切段后、:290 返回前复验 | 当前 :192 缺 user/source 关系上下文；补最小上下文或保持未知；只可选择已验收 best |
| story chunk 处理 | 完整故事语义＋实际切段集合 | route_story.py:283 的清洗／拆句后校验正文组合、段序和原译文 | chunk 只是播放单位；ZH 可只在首段，不能要求每个 chunk 独立双语或清偿 |
| /group/chat 首波及互动发言 | 每角色本次完整气泡集合 | route_group.py:369 共享 generator，:529 parse 后检查；最终 sanitize 后，首波 :758 保存前、互动 :811 repetition 后且 :816 保存／:817 TTS 前复验 | 当前角色失败可跳过；后角色只读前序已提交消息，不读拒候选 |
| /group/chat/continue | 续聊角色完整集合 | route_group.py:902 生成、:910 repetition 后、:917 保存前复验 | 可结束续聊；system trigger 不制造用户授权；复读停止不等于关系撤回 |
| group_bubbler | 最终截取的一条气泡 | group_bubbler.py:161 共享 generator；:170 msgs[:1] 后、:176 保存前复验 | 可选冒泡取消；截掉内容不参加清偿或授权 |
| proactive_scheduler.generate_from_promise | 本次完整 JP/ZH 主动文本 | proactive_scheduler.py:127 Provider parse 后检查；复验早于 :156 TTS、:160 入库、:167 canonical commit、:188 push、:195 mark_fired | 取消或保留待处理按原 promise 合同；说已完成不能证明外部兑现 |
| schedule_share | 完整生活分享 JP/ZH | schedule_share.py:179 生成、:200 近似去重后，:207 TTS／:211 发布前最终检查 | 可选分享取消；不改 canonical schedule、不反哺关系证据 |
| diary_scheduler 的 reaction | 对具体留言的完整 JP/ZH 回复 | diary_scheduler.py:231 生成后检查，:256 TTS／:261 发布／:271 effects 前复验 | 可选反应取消；用户留言与模型日记／摘要的权威分开 |
| /story/tts | 已验收故事的一段确定正文；无 LLM | route_story.py:298,:313 当前接任意 text；新故事交付拟绑定验收消息／段号／摘要后 TTS | 不匹配不合成新故事 chunk；音频失败不抹文字；旧故事兼容待决 |
| /tts/resynth | 要求重播的历史正文；无 LLM | route_tts.py:22,:35 当前检查可发声文本／音色；若绑定历史消息需单列播放合同 | 不新增发言、关系证据或清偿；撤回后能否重播、旧消息无 receipt 如何处理待用户决定 |

覆盖验收必须逐 producer 建 fixture，不能以本表存在证明已经覆盖。未来发现新的直接 Provider 或后台可见 producer，按同一契约加入覆盖清单，不另立闸门。

**10.3 重试、失败与安全兜底**

所有协议、翻译、日程和关系硬错误共享首次＋至多一次修复；story 的五次、image/group 的三次和外层 similarity 循环不能各自叠加。语义候选数、SDK 物理请求数、后台 occurrence 重试数分别记账，受统一上限。

修复使用同一源、卡片、合法路线与失败代码。新增关系 reason 必须有专用允许范围提示；不得靠目前 JSON 提示修复所有语义。当前 route_chat.py:707 会折叠未知 reason，此接口需扩展。

第二次硬失败只可产生经过全部检查、无业务 intent 的确定性完整回应，或明确 generation_failed。未知来源只能承认未知，不凑纠结原因。proactive／bubbler／可选分享失败取消；delayed 未完成不消耗 pending；story 可失败。软重复只记录或沿批准策略处理，不自动付费再生成。

generation_contract.degraded_reply:95 不能当语义兜底。预算不足、Provider 不稳或 guard 异常均不能放行违规断言。已提交但交付未知恢复原消息，不重新生成或二次清偿。

**10.4 完整语音与播放**

验收前零候选字幕、零音频、零候选 TTS；只允许无正文 heartbeat。A 的八段／2000 字符／20 秒仅作延迟与截断测试起点，产品限制待确认。

通过后使用稳定 assistant message identity／receipt，提交与字幕、音频、恢复均引用同一最终正文。TTS 故障可仅交付已验收文字。完整缓冲增加首音等待，需客户端合同测试；回滚只能转完整验收后的非流式路径，不能恢复先播后查。

新生成故事的播放绑定与历史重播政策分开。音频重合成不是角色今天新说一次，不形成新证据、不清偿问题。来源失效后是否还能重播旧原话、无 receipt 旧消息兼容等，不在本文默认改变。

**11. M10／M11／M12：旧边界、表达多样性与对照**

旧 observer 主路径继续不恢复。relationship_boundary.py:105 的 confidence 仅进日志，低置信度残留仍可改 friction，:122 还可能改 trust；它是后续需要独立修复／隔离的风险，不因本规格新增而已解决。

历史 rel_state、provenance、boundary_hits 原样保留。未核实摩擦不直接成为新主观 V、趋势或身份证据；是否在用户可见历史面板过滤由用户决定。有效用户边界、有效角色 stance 和明确设定约束分别读取，稳定 boundary_ref 不用自由 topic_id 代替。设边界／拒绝本身不是越界，不新加摩擦。

若后续审计，逐条归 source_verified／ambiguous／missing／invalid，历史实际完整率仍 UNVERIFIED。只从有效 canonical 来源形成新版本观察，不扣回历史分数，不自动回填。恢复任何数值影响另作 M13 RED 评审。

表达路线先按合法来源、场景、边界和应答义务筛选，再用稳定 user／character／event 或 occurrence／card／style version 种子选择。修复不抽新的感情方向。近期句式只来自已提交可见 assistant 历史，拒候选不入库；缓存可重建，不是关系证据。

A 的最近 12 条／最多七天、两次相同开场或七天三次固定句、提示 3–5 项均为评测参数。普通／故事／语音／修复 temperature 候选 0.8／0.9／0.7／0.3，传感器 0，仅在目标模型明确支持时测试；temperature=0 不保证位级一致。ai_client.py:61 的 None 表示供应商默认，不证明当前温度低。参数与软重复政策待批准，不以更多固定口头禅替代内容。

中转实验采用 A：后续独立授权后用去标识合成场景，冻结模型版本、system/messages/schema/card、采样、token、thinking/cache 和状态；两组各五次交错比较，只作初步信号。保存请求摘要、usage、版本证据、原始文本与应用后文本，不记凭证。若版本不同，只报告服务组合差异；更多样本也不能自动补足因果控制。没有业务写入、真实聊天读入或后台传感器运行。

费用不引用未核实报价。成本按实际尝试及模型计费计算：输入 tokens×输入单价＋输出 tokens×输出单价，加缓存、重试、视觉、thinking 和 TTS。给定假设的算术不等于供应商价格；模型／隐私／付费实验授权都是前置条件。

**12. M13：外部桥接评估与可选数值路径**

保留 B 的评估方向：外部 relationship_cognitive_bridge.py 与 cognitive_queue 调用方案不纳入本基线；保留确定性规则、逐谓词测试和来源审计的思路。当前仓库没有该新增 bridge/test，本轮没有重跑其故障实验，不把外部故障结果冒称本轮 PASS／FAIL。

本规格禁止：Slow Loop 直接写关系数值；用 belief metadata.rel_projected 作唯一去重；独立提交关系数值后再提交认知周期；吞 SQL／commit 异常仍返回成功；把 literal self-report 升格为越界或兑现验证。

本基线先只读有效认知条目形成卡片，不启动 numeric processor。若用户以后决定启用，最小正式路径须满足：

1. 关系侧读取已提交、来源仍有效的 belief，使用稳定结构化 identity，不依赖 created_by_cycle_id 扫描提供可靠重试。
2. 复用 relationship_db.py:91 的 try_begin_application(event_id,processor_type,processor_version,user_id,character_id,payload)。文本 event_id 可表示有命名空间的 belief identity，版本表示规则身份；主键见 :301。多规则／多版本／跨库身份必须明确。
3. 在同一线程的 relationship_txn():67 内 claim、全部正式 apply_*、provenance 和最终应用结果；False 则跳过写入。事务外 claim 会独立提交；空身份在 :97 直接返回 True，必须禁止。
4. 用同一 pair 锁／版本比较串行化不同事件，防止 relationship_state.load_state():55 读后写覆盖。唯一应用身份只防同事件重复。
5. 保存原始规则输入、来源快照、有效性、事件顺序、baseline、请求贡献、实际贡献、规则版本及结果，才能讨论确定性重算；result_json 容器本身不等于已有 replay API。
6. 来源纠正／撤回／删除后先停止相关授权，按批准 baseline 和事件顺序重建。apply_trust():224 的正负系数及限幅使简单取负回退不可靠。
7. SQL 失败须传播至外层回滚；commit 失败与提交结果未知分开，不使用 succeeded 描述未经确认的持久化。
8. 启用 cutoff、迟到事件、积压只读／dry-run 与版本升级另行决定，禁止 worker 启动自动投影旧积压。旧“311”数量未经本轮数据验证。

产品映射待决定：设边界／拒绝只限制表达；作出承诺只记待验证；自述兑现不等于独立验证。哪些外部事件足够、什么叫独立第二来源、什么构成重复触碰已知边界，以及是否给 trust/friction 小幅影响，都不能由本草案决定。未批准则零数值投影。

**13. 验收、回滚与本轮交付记录**

**13.1 后续必需验收**

先使用虚拟 UTC 时钟、固定 canonical fixture、预给标签和离线连接／fake Provider；无生产数据库、无真实 LLM。实施前须验证连接工厂、import-time alias 和所有 apply 路径均受离线保护，不能只 patch db.get_conn 就假定没有真实连接。可选真实模型评测另获授权。

至少覆盖：

- 同样封顶账本、不同近期合法观察得到不同趋势；空窗不解释为疏远。
- 分桶、迟到、乱序、重新分批、跨时区、重送、规则版本及额度的确定性。
- 引用／故事／假设／跨用户／target 错误／低置信度候选不产生未经授权状态。
- 主观类型不 promote、不过事实 recall、不写 rel_state；key 碰撞不覆盖 canonical 项。
- 纠正、撤回、删除发生在读取、生成、提交和缓存期间，旧来源不再授权。
- 单方表态、双边确认、拒绝 scope、服务 question 和关系 question 分离。
- 回避只来自有效最终回答；拒候选、生成失败、离线不计；清偿 receipt 至多一次；交付未知可恢复。
- 第 10 节每个 producer 的“首段合法、末段越权”“JP 合法、ZH 增加确认”“sanitize／截断改变应答”。
- 拒候选零 assistant commit、零 offline state、零 schedule/promise/transaction effects、零问题清偿。
- stream 验收前零正文／音频／候选 TTS；TTS 失败不抹已提交文字；纯播放零新关系效果。
- 各层重试总量、SDK 实际请求预算、认证熔断、超额、不确定提交及 pending 恢复。
- M13 若另批：去重、独立 claim 漏写窗口、SQL 中止、commit unknown、并发更新、回退非对称、版本切换。
- 开关中途变化、重启、缓存清空、批次中断和回滚，不改旧 canonical／rel_state，不恢复失效许可。

静态存在的回归参考：test_relationship_panel_authority、test_relationship_apply_once、test_chat_commit_gate、test_generation_receipt、test_cognitive_deterministic、test_cognitive_loop_v1、test_cognitive_revision、test_cognitive_worker_controls、test_cognitive_pending_resolution、test_cognitive_spend_guard、test_delayed_reply、test_nonverbal_tts，均在 tests/。这些不是新机制通过证明。

关注 REG-001/002、REG-006/007/008/009；REG-005 客户端音频缓存由对应客户端 owner 验证。新测试必须永久覆盖严重失效；架构测试检查任意间接 writer 和实际调用边界，不能仅按 cognitive_* 文件名前缀匹配。

A 的标注／盲评建议保留为待批准验收 profile：独立标签精度≥95%，检测与应答各自报告 precision/recall、覆盖和弃权；至少 24 个固定合成场景，两方案各三次生成，共 144 条，独立盲评；确定性 fixture 的跨用户、越权事实／身份和提前内容泄露为零。自然度、重复率、延迟和阈值须先冻结定义，不把有限样本达标解释为所有自由文本永远合规。

**13.2 M14 回滚、初始化与保留**

采用版本化 reader 和停止新接收，保留旧／新审计，不 DROP、不重置 rel_state。规则、模型、卡片、来源 snapshot 和消息 receipt 明确关联，旧版本无法理解新类型时只能排除，不能当 factual 记录读。

来源撤回在回滚后仍有效；已经形成的真实发言／清偿历史不抹除。清偿依据失效则停止当前清偿效力，保留修订历史。语音回退仍完整验收，纯播放不制造新发言。

关系侧统计 DDL 归 init_relationship_tables():174；认知 schema/type 归现有 init_cognitive_tables()。gojo_server.py:89–95 在顶层执行初始化，任何 init 仅建缺失结构／安全默认／明确授权迁移，不运行历史重放、LLM、状态重算或周期数值投影。保护 INV-PERSIST-001 与 REG-006。

审计保留期、主观贡献有效期、精确回放范围和用户删除策略待决定。A 的 90 天只是候选；无法保留必要合法输入时当前贡献须失效，不能声称无限回放。删除权不能被新增日志或配置阻碍。

**13.3 本轮文档交付记录**

只新增 docs/RELATIONSHIP_STATE_SPEC_DRAFT.md；原 A、附件 B 及其他既有文件保持不变。Before 为两份存在冲突的提案；After 为以 A 为唯一技术基线、保留 B 四类内容并列明实施缺口的草案。运行行为没有变化。

canonical authority、relationship authority、durable memory authority、provenance、schema、API、worker lifecycle、identity semantics 均未在本轮改变。没有业务测试 PASS／FAIL；全部运行效果仍 UNVERIFIED。

本轮只做输入／源码读取、文件完整性核对、Markdown/UTF-8／空白检查及 Git 范围核对。代码治理脚本和 review/latest.md 生成不适用于仅文档新增，未执行，避免范围外写入。semantic risk=GREEN（文档）；后续 RED 实施另走 Change Capsule 和批准流程。

Git 分支 fix/current-turn-grounding-20261010，HEAD c22ac185…；无 commit、push、merge 或部署。已有未跟踪文档及 review 文件保留，本文是本轮唯一新增文件。

**14. 待用户决定的问题清单**

未决定的产品项不默认启用；技术前置条件由实施方案提出可审查结果，不能通过猜测补齐。

| 编号 | 待决定事项 | 需要明确的边界 |
|---|---|---|
| D01 | 独立修订 v2 输入 | 是否有未纳入全文；只核对允许保留的四类增补 |
| D02 | 关系发展与感情表达 | 是否允许主观在意／喜欢／心动，明确禁限；不默认永远朋友，也不默认可发展 |
| D03 | 里程碑语义 | 单方感受、提议、双方接受分别代表什么；proposition／scope／撤回规则 |
| D04 | 主观推断与人设 | 是否 opt-in；允许推断的体验、角色回避基线、原因目录及可直接说出的范围 |
| D05 | 内心透明度 | 面板显示趋势／原因／来源到何程度；两条还是三条原因；是否展示未答提示 |
| D06 | 时间尺度与动力参数 | A 的 7＋7 天、贡献、半衰期、限幅、档位和观察门槛是否通过评测；不混入 B 单位 |
| D07 | 应答力度 | force_due 阈值、具体回答定义、不确定／拒绝／延期的清偿规则、暂停例外 |
| D08 | 清偿与交付 | 已 commit、已交付、交付未知怎样显示和结算；修订 episode 的用户表现 |
| D09 | 主动补答 | 是否允许、哪些既有联系许可和触发可用、频率；不因欠项增加催问消息 |
| D10 | 旧摩擦 | 保留历史显示、隔离未核实读侧影响或另批有来源重算；不清零、不自动补偿 |
| D11 | 语音体验 | 是否接受完整缓冲首音延迟；消息长度／超时／非流式替代的可接受范围 |
| D12 | 历史播放 | 撤回来源后是否仍可重播原话；旧故事／旧消息无 receipt 如何兼容 |
| D13 | 软重复与安全兜底体验 | 记录、接受、取消或确定性文字的场景策略；不以体验需要放行硬违规 |
| D14 | 模型、隐私与预算 | 可发送的真实片段、模型版本、每日 attempts/token/费用、缓存计费及跨后台共享额度 |
| D15 | 对照与评测 | 是否批准标注、盲评和付费合成实验；样本、阈值、评审与费用 |
| D16 | 数值 processor | 继续搁置还是另批；映射、独立兑现证据、cutoff、积压展示／dry-run |
| D17 | 保留与回放 | 审计／贡献期限、删除传播、精确历史可重算范围与版本升级政策 |

**15. 实施前置条件**

1. 确认本文为后续设计引用基线；取得独立 v2 后完成允许范围的差异核对。旧文档保留历史，后续变更引用规格版本。
2. 重新核对部署与实际调用路径；线上数值和历史摩擦另行授权审计。本轮未知不作为回填依据。
3. 为每阶段生成 Change Capsule：根因、owner、允许文件、保护 invariant、永久回归、风险与授权。schema／provenance／重要 worker／迁移均 RED。
4. 解决 hypotheses key/type 共存、metadata reader、非空 hypothesis_updates 的正式提交协议，以及 stats 旧唯一索引与多版本身份兼容；禁止替代 store。
5. 冻结 source validity、快照版本、首次 accepted_at、消费身份、并发锁／CAS、额度、重算顺序、失效传播与审计载荷。
6. 明确 questions 修订而非硬 reopen，生成 intent/schema、最终正文、receipt、送达未知和 effects 原子协调合同。
7. 逐入口覆盖表及 pure playback 合同完成影响审查；voice 客户端首音／断线／字幕／音频合同由 owner 验证。
8. 统一预算／熔断器先形成可验证接口，再允许任何真实传感器；不能复用请求级 limiter 冒称完成。
9. 全部 fixture 和回归先在有离线连接保护的环境执行；检查 import-time 连接绑定。未授权不得连生产、调用付费模型或启动真实 worker。
10. 提供 additive DDL、索引兼容、版本切换、停写、重算和回滚演练结果；不把 init 当 backfill。任何生产执行另获明确批准。

**16. 分阶段顺序**

| 阶段 | 范围与依赖 | LLM／authority | 放行条件／风险 | 回退 |
|---|---|---|---|---|
| P0 基线与契约 | 完成 D 项必要决策、类型／索引／来源／receipt／预算／入口设计；可另批只读历史审计 | 无 LLM；不写业务 authority | 关键未知有记录，迁移与测试计划可审查；文档 GREEN，实际跨域诊断按范围分类 | 保持现有运行路径 |
| P1 离线确定性基础 | 经批准实现 M01/M02、M05/M06 的最小正式路径，使用固定注解；准备 M08 的 fake producer 测试 | 零真实 LLM；统计注解与 Cognitive 正式 writer | 来源、幂等、类型、问题与身份分离、重启和版本测试通过；RED 写路径批准 | 停新 writer／reader，不改旧账本 |
| P2 安全卡片与入口 shadow | M07/M08/M09，完整 buffering、最终正文、全 producer；已有硬检查继续执行 | fake／原生成路径；shadow 不增加越权 authority | 逐入口零拒候选副作用、零提前内容泄露，客户端合同与延迟评审；YELLOW，持久化变更 RED | 退安全卡片，转完整验收非流式；shadow 不作为安全豁免 |
| P3 可选传感器 shadow | 先预算，再 M03/M04；关闭可见影响，人工校准固定候选集 | 授权后少量异步 LLM；受限 typed hypotheses | 独立标注、额度、类型隔离、来源失效、回放全部达标；RED 明确批准 | 停传感器，隔离新版本，保留审计 |
| P4 opt-in 展示与应答 | 批准 D 项后逐项启用卡片、应答、硬闸门与 M11 软质量策略；先单角色 | 沿原生成；不增加 rel_state 投影 | 固定盲评、回滚、实际预算及完整语音体验通过；跨域 YELLOW／持久化 RED | 分项关开关，保留有效边界、事实硬约束及真实历史 |
| P5 独立对照与可选数值评审 | M12 实验另批；M13 只在产品明确需要时单独设计、dry-run、审计 | 对照有 LLM；numeric processor 无 LLM且另批正式关系写 | 不把小样本当因果；数值原子、去重、并发、失效重算、cutoff 验收；processor RED | 停实验／新数值接收，按批准版本回退，不负增量补偿 |

P5 不是前四阶段的必需依赖。每阶段只实施已批准的最小范围，新增问题回到 Diagnosis 与 Change Capsule，不把全部规格一次性塞入一个修复。
