# 修复：promise 触发时刻解析错误 + 重复创建

日期：2026-10-29
分支建议：`fix/promise-identity-20261029`
适用仓库：`unreval/gojo_backend`

---

## 1. 根因

### 根因 A —— 触发时刻取错（`promise_detector.py`）

`_extract_clock(text)`（`promise_detector.py:43`）返回**整段回复里第一个**能解析的时钟表达。
角色在深夜回复时通常先播报当前时间，再给出承诺时间，于是第一个匹配到的是"现在几点"，不是"答应几点"。

随后 `promise_detector.py:144`：

```python
if trigger_at <= now and not ('明天' in reply_zh or '后天' in reply_zh):
    trigger_at += timedelta(days=1)
```

把这个已经过去的错误时刻推到第二天同一时刻，于是变成凌晨闹钟。

**生产证据**（`proactive_msg` 两段同属一次延迟回复，`created_at = 2026-10-28 18:06:02` UTC）：

| id | jp |
|---|---|
| 411 | まだ起きてたのか。**二時過ぎ**だぞ。 |
| 412 | 早八あんだろ、**七時**には起こしてやるから… |

产出 `proactive_promise#61`：`context = 角色答应在特定时间联系。原话:「七点叫你」`，
`trigger_at = 2026-10-29 18:00 UTC`（= 中国时间 10-30 **02:00**）。
取到的是「两点」，不是「七点」。

同一根因的其他行：`#52`（原话「十点了我叫你」→ 02:00）、`#56`（原话「七点叫你」→ 04:00）、
`#53`（原话「七点前就起来。六点半叫你」→ 取了 07:00，真正的 06:30 被丢弃）。

### 根因 B —— promise 的身份口径是文本/轮次，不是触发时刻

四处叠加：

1. `generation_effects.py:35`
   ```python
   def occurrence_key_for(endpoint, source_event_id, effect):
       return f'{endpoint}:{source_event_id}:{effect}'
   ```
   `source_event_id` 每条用户消息唯一，所以同一晚反复确认几次就反复建。
   并且两个生产者的 `effect` 不同（`apply_proactive_promise` 用 `proactive_promise`，
   `apply_promise_detector` 用 `promise_detector`），**同一轮**两条路的 key 天生不碰撞。

2. `promise_detector.py:190` 的内容去重 `if not occurrence_key:` 被永远短路（route_chat 总是传 key）。
   该段**不应恢复**：它按 `context[:20]` 判重，而 `get_active_promises` 不过滤 `is_fired`，
   恢复后会把"隔几天再次请求七点叫我"误判成重复而静默丢弃。身份口径本身就是错的。

3. `db_promise.mark_fired()` 对 `once` 只置 `is_fired=TRUE`，`is_active` 永远留 `TRUE`。
   连带后果：`prompt.py:504` 的【你还没兑现的约定】通过 `list_active_promises` 取数，
   而该函数 SELECT 里没有 `is_fired`，所以 `_open_promise_lines`（`prompt.py:455`）无法过滤，
   已兑现的旧承诺会一直作为"欠着的"注入提示词。生产库 12 行里 11 行是 `is_fired=TRUE, is_active=TRUE`。

4. `proactive_scheduler.py:220` 发送前按 `context[:10]` 判重。
   所有正则路 context 都以「角色答应在特」开头 ⇒ 同一 tick 内**不同时刻**的承诺
   （7:00 起床 与 10:00 提醒）会被判为重复并静默 `mark_fired`，永远不发出。

**生产证据**：一次「明早七点叫我」产生 6 行（#53 #56 #59 #60 #61 #62）；
前一天那次产生 4 行（#54 #55 #57 #58）。真正重复的那几行 `trigger_at` 逐字节相同
（`2026-10-28 23:00:00`，因为分秒被 `replace(minute=0, second=0)` 削平）。

---

## 2. 权威来源

`gojo_backend/db_promise.py` 是 `proactive_promise` 的唯一权威。
本次修改**不新建**任何第二套承诺存储或判重表。

---

## 3. 身份定义（本次改动的核心）

一条 `once` promise 的身份是：

```
(user_id, character_id, trigger_kind='once', trigger_at)   且 is_fired=FALSE AND is_active=TRUE
```

一条 `daily` promise 的身份是：

```
(user_id, character_id, trigger_kind='daily', trigger_time)  且 is_active=TRUE
```

**文本内容不参与判重。** 跨天的同类请求（连续几天都要七点叫醒）`trigger_at` 的日期部分不同，
天然是不同的 promise，必须各自存在。

---

## 4. 改动清单

### 4.1 `gojo_backend/promise_detector.py`

**(a) `_extract_clock` 改为带作用域的解析。**
新增内部函数，只接受**出现在承诺动词附近**的时钟：承诺动词集合
`叫|喊|提醒|催|找你|联系|发消息|起来|起床`。
实现建议：对 `_CN_CLOCK` 的每个 `re.finditer` 匹配，检查其结束位置后 0–8 个字符内
是否命中承诺动词；命中则采纳，否则继续找下一个。全部不命中则返回 `(None, None)`。

保留 `_cn_hour`、中文数字、「半」→30 分的现有行为，不要动。

**(b) `matched_type == 'timed'` 分支取消 `+2h` 兜底。**
`promise_detector.py:131` 的 `trigger_at = now + timedelta(hours=2)` 在时钟解析失败时
会造出一个毫无依据的触发时刻。改为：`timed` 类型若 `_extract_clock` 返回 `None`，
直接 `return None`，不创建 promise。

`contact` / `remind` / `spam` 三个分支的相对延迟（1–2 小时）**保持不变**，不在本次范围内。

**(c) 删除 `promise_detector.py:190–195` 的 `context[:20]` 内容去重整段。**
不是改条件，是整段删除。判重下沉到 `db_promise`。

### 4.2 `gojo_backend/db_promise.py`

**(a) `init_promise_table()` 增加两个 partial unique index：**

```sql
CREATE UNIQUE INDEX IF NOT EXISTS idx_promise_identity_once
  ON proactive_promise (user_id, character_id, trigger_at)
  WHERE trigger_kind = 'once' AND is_fired = FALSE AND is_active = TRUE;

CREATE UNIQUE INDEX IF NOT EXISTS idx_promise_identity_daily
  ON proactive_promise (user_id, character_id, trigger_time)
  WHERE trigger_kind = 'daily' AND is_active = TRUE;
```

照现有写法用 `SAVEPOINT` / `ROLLBACK TO SAVEPOINT` 包起来，失败只打日志不抛。

**(b) `add_promise()` 处理身份冲突。**

⚠️ 不要试图把新索引塞进现有的 `ON CONFLICT (occurrence_key)` 子句——
一条 `INSERT` 只能推断一个冲突目标，而且两条路径（有 key / 无 key）都要覆盖。

改法：现有 INSERT 逻辑外层包 `SAVEPOINT`，捕获 `psycopg2.errors.UniqueViolation`：
`ROLLBACK TO SAVEPOINT` → 按上面第 3 节的身份条件 `SELECT id` → 返回已有 id →
打印 `[promise] duplicate suppressed identity_id=<id>`。
`occurrence_key` 的 `ON CONFLICT` 行为保持原样不动。

**(c) `mark_fired()`：`once` 分支同时置 `is_active = FALSE`。**

```sql
UPDATE proactive_promise SET is_fired = TRUE, is_active = FALSE, last_fired_at = %s WHERE id = %s
```

`daily` 分支不动（daily 要反复触发）。

**(d) `get_due_promises()` 的 `once` 查询补上 `trigger_at`，并写进返回 dict。**
调度器需要它做分桶判重。当前 SELECT（约 `db_promise.py:146`）没有取这一列。
`daily` 行已经带 `trigger_time`，另外把该分支算出的 `trigger_at` 也放进 dict。

**(e) `list_active_promises()` 的 SELECT 补 `is_fired`，并在返回 dict 里带上。**
配合 (c) 之后活跃集合本身就干净了，这一项是给 `prompt.py` 留显式依据。

### 4.3 `gojo_backend/proactive_scheduler.py`

`_tick()`（`proactive_scheduler.py:210–231`）的 `seen` 判重：
把 `ctx[:10]` 换成按触发时刻分桶——
`once` 用 `trigger_at` 截到 15 分钟桶，`daily` 用 `trigger_time`，
桶键为 `(character_id, bucket)`。同桶只发第一条，其余 `mark_fired`。

### 4.4 新增 `gojo_backend/migrate_promise_identity.py`

仿 `migrate_two_level_recall.py` 的写法。**不要**在 `gojo_server.py` 启动时自动调用，
由使用者手动执行一次。顺序必须是：

```python
# 1) 历史已兑现的 once 退出活跃集合
UPDATE proactive_promise SET is_active = FALSE
 WHERE trigger_kind = 'once' AND is_fired = TRUE AND is_active = TRUE;

# 2) 未触发的重复行只保留 id 最小的一条
UPDATE proactive_promise p SET is_active = FALSE, last_error = 'dedup_migration'
 WHERE p.trigger_kind = 'once' AND p.is_fired = FALSE AND p.is_active = TRUE
   AND EXISTS (SELECT 1 FROM proactive_promise q
                WHERE q.trigger_kind = 'once' AND q.is_fired = FALSE AND q.is_active = TRUE
                  AND q.user_id = p.user_id AND q.character_id = p.character_id
                  AND q.trigger_at = p.trigger_at AND q.id < p.id);

# 3) daily 同理按 trigger_time 去重

# 4) 最后才建 4.2(a) 的唯一索引
```

步骤 4 必须在 1–3 之后，否则建索引会因存量重复而失败。
每一步打印受影响行数。

---

## 5. 必须保持不变的不变量

- `db_promise` 仍是承诺的唯一权威，不新增第二套存储。
- 快环判断不依赖 LLM：`promise_detector` 保持纯确定性，不要引入模型调用。
- **主动消息不设任何时段限制**。本次不得引入"夜间不打扰"之类的时间窗——
  是否开口由角色性格和关系阶段决定，这是既定设计。
- 跨天的同类请求必须各自成立，不得被判为重复。
- `apply_promise_detector` 与 `apply_proactive_promise` 两个生产者都保留，不停用任何一个。

---

## 6. 测试要求

### 6.1 新增 `tests/test_promise_clock_scope.py`

1. **真实 fixture 回归**：`reply_zh = '还醒着呢？都两点多了。早八有吧，七点我叫你起来。'`，
   `now = 2026-10-29 02:06+08:00` → 断言 `trigger_at == 2026-10-29 07:00+08:00`，
   并且**不等于** `2026-10-30 02:00+08:00`。这是 #61 的永久回归测试。
2. `reply_zh = '七点前就起来。六点半叫你。'` → `06:30`，不是 `07:00`。
3. `timed` 类但无可用时钟 → `detect_and_save` 返回 `None`，表内零行（验证 `+2h` 兜底已移除）。
4. 承诺动词在时钟**之前**的语序（「我叫你，七点」）也要能取到 07:00。

### 6.2 新增 `tests/test_promise_identity.py`

1. 同 `user/character/trigger_at` 插两次 → 表内 1 行，第二次返回同一 id。
2. 两个生产者各自的 `occurrence_key`（`...:promise_detector` 与 `...:proactive_promise`）
   指向同一 `trigger_at` → 仍然只有 1 行。
3. **跨天不判重**：`trigger_at` 相差一天的两条 → 2 行都在。
4. `mark_fired(once)` 之后，同一 `trigger_at` 可以再插入成功（当天第二次请求不被旧行挡住）。
5. `mark_fired(once)` → `is_active` 为 `FALSE`；`get_due_promises` 不再返回；
   `list_active_promises` 不再包含。
6. `daily` 的 `mark_fired` 之后 `is_active` 仍为 `TRUE`。

### 6.3 新增 `tests/test_proactive_tick_dedup.py`

1. 两条 `trigger_at` 相同的 due promise → 只产生 1 条消息。
2. 一条 07:00、一条 10:00 同时 due → **产生 2 条消息**。
   这是 `context[:10]` 静默吞掉第二条的回归测试。

### 6.4 需要同步修改的既有测试（不要反过来改生产代码去迁就它们）

- `tests/test_db_promise.py:148` `test_fired_and_inactive_once_are_not_reclassified`
  当前断言 `(fired, True, True, None)`，即 `mark_fired` 后 `is_active` 仍为 `True`。
  按 4.2(c) 应改为 `(fired, False, True, None)`。
- 同一个测试里 `self.promise(trigger_at=trigger_at)` 连续建了两条**相同 trigger_at**
  的 promise，新索引下第二条会被判为同一条。该 fixture 需改成两个不同的 `trigger_at`
  （或不同 `user_id`），否则测试意图会失真。
  `tests/test_db_promise.py:80` 的 `promise()` helper 使用固定的 `'gojo' / 'test-user'`，
  请全文件检查还有哪些用例依赖"可以重复插入同一 trigger_at"。
- `tests/test_open_promise_prompt.py`：确认 4.2(e) 之后展示逻辑仍然通过。

### 6.5 回归套件

至少跑：`tests/test_db_promise.py`、`tests/test_open_promise_prompt.py`、
`tests/test_delayed_reply.py`，以及三个新文件。
Postgres 相关用例需要 `COGNITIVE_TEST_PGLITE` 环境变量。

---

## 7. 不在本次范围内

- `char_phone_check` 的积压没有过期窗（`db_schedule.py:883` `list_due_phone_check_ids`
  对 `sched_date < today` 的行没有时间上限）。单独开一次改动。
- `contact` / `remind` / `spam` 分支的相对延迟兜底。
- 时区相关的任何改动。已核实 `SHOW TIME ZONE` 为 UTC，`created_at` 与 `trigger_at`
  在两条写入路径上都以 UTC 存储，前端 `app/chat/[id].tsx:1057` 补 `'Z'` 的解析是正确的，
  **显示时间没有偏移，不要去"修"它**。

---

## 8. 完成后请报告

根因 / 权威来源 / 改动文件 / 改动前行为 / 改动后行为 /
实际执行的测试与结果 / 架构或 API 变更 / 遗留风险 / 分支与工作区状态。

未经明确授权不要 merge 到 main、不要 force push、不要部署、
不要执行生产库迁移（第 4.4 节的脚本交由使用者手动运行）。
