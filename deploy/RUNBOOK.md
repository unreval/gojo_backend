# 从 Zeabur 迁到自己的服务器

这份文档是给人照着做的。不要把它交给 agent 执行，也不要把任何真实密钥、连接串或 dump 贴进对话。

目标是把现有数据库原样搬到一台一直开着的机器上，先对上数字，再改手机里的服务器地址。这次不改列类型，不改业务代码，不停掉还在服务的 Zeabur，除非新机器已经核对通过并稳定跑过一段时间。

代码侧没有 Zeabur 专用耦合：进程听 `0.0.0.0` 和 `PORT`，配置都在环境变量里，手机设置页可以改服务器地址，所以换机器不需要重新打包 APK。必须一直开机。`gojo_server.py` 在启动时拉起后台线程，不能用会缩到零的托管。数据库用普通 PostgreSQL 16，不需要 pgvector。

## 0. 现在就做，不要等新服务器

Zeabur 的每日自动备份会跟着服务停止一起消失。专用服务器按各自到期日关闭，到期即删，磁盘不保留，这个日期可能早于 12 月 31 日。共享集群 12 月 1 日起暂停，12 月 31 日全部停止。

1. 在 Zeabur 面板看清你的服务是专用服务器还是共享集群，以及具体到期日。
2. 按下面第 4 节先导出一份全库，放到仓库外面的目录，权限收紧。不要等新机器买好。
3. 准备自己的域名。`*.zeabur.app` 会失效。Android 默认不走明文 HTTP，没有域名就签不了 TLS 证书。
4. 开通新机器。当前倾向是腾讯云国际版 Lighthouse，新加坡或柔佛，包年包月。买能一直开机的规格，不要选会休眠的方案。
5. 所有密钥都换新的再写入新 `.env`。旧密钥按已经泄露处理。包括模型、中转、Fish、Groq、embedding、R2 访问密钥、数据库密码。R2 的桶可以继续用，访问密钥要换，旧密钥作废。

## 1. 准备新服务器

下面以 Ubuntu 为例。云面板里的防火墙和机器上的防火墙都要设，只放 22、80、443。不要放 5432。

云防火墙（Lighthouse 防火墙或安全组）入站只保留：

- TCP 22
- TCP 80
- TCP 443

然后在机器上：

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
docker compose version
sudo ufw default deny incoming
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

建一个非 root 用户，把 SSH 公钥放进去。先另开一个会话确认能登录，再考虑关掉 root 登录。不要在当前唯一的会话里改 SSH。

```bash
sudo adduser --disabled-password --gecos "" gojo
sudo usermod -aG docker gojo
sudo mkdir -p /srv/gojo
sudo chown gojo:gojo /srv/gojo
```

用这个用户把仓库克隆到 `/srv/gojo`。部署文件现在在本机分支 `chore/self-host-deploy-20261104`，还没有推送。新服务器只能拿到你已经推上去的提交。你自己决定要不要推这个分支；在那之前不要假定服务器上已经有 `deploy/`。合并进 main 并推送之后，服务器跟踪 main 即可。

```bash
git clone <仓库地址> /srv/gojo
cd /srv/gojo
git checkout <含有 deploy/ 的分支或 main>
```

## 2. 域名

把域名的 A 记录指到新服务器的公网 IP。在本机确认解析已经指向这台机器之后，再启动 Caddy。证书申请失败太多次会被 Let's Encrypt 限流。

```bash
dig +short chat.example.com
```

把 `chat.example.com` 换成你的域名。

## 3. 填写 deploy/.env

```bash
cd /srv/gojo/deploy
cp .env.example .env
chmod 600 .env
```

用编辑器填 `.env`。规则都写在示例文件顶部，这里只重复最容易填错的几条：

- `DATABASE_URL` 的主机名必须是 `db`，形如 `postgresql://gojo:同一密码@db:5432/gojo`。这是 compose 内部的服务名。不要填公网地址。
- `POSTGRES_PASSWORD` 与 URL 里的密码是同一个。密码若包含 `@ : / # ? &`，只在 URL 里做百分号编码。
- `SITE_DOMAIN` 是你的域名，不要带 `https://`。`PORT` 保持 `8080`，Caddyfile 转发到 `app:8080`。
- 布尔开关不要留空。`COGNITIVE_WORKER_ENABLED` 和 `SHADOW_AUDIT_ENABLED` 留空会被当成开启。`CANDIDATE_PRODUCER_ENABLED` 留空则是关闭。
- 示例里的 `false` / `true` 是代码默认，不是你的生产配置。尤其是 `CANDIDATE_PRODUCER_ENABLED` 和 `CANDIDATE_PRODUCER_SHADOW`：前者默认关，后者默认只记影子。生产如果已经在写记忆，两项都要照抄，只把前者改成 `true` 仍然不会写入。
- 容器日志时区由 compose 设为 `Asia/Shanghai`。这不改变数据库会话时区。会话时区仍由 `db.py` 在连接时固定为 UTC。不要改那一行。

填完后确认 `.env` 不在 `git status` 里。

## 4. 从 Zeabur 导出

在你自己的电脑上做，不必等新服务器。下面这段用 bash（Windows 用 Git Bash）。连接串从 Zeabur 面板复制到当前 shell，不要写进仓库，也不要放进命令行参数，避免留在历史记录里。

需要 Docker。客户端使用 `postgres:16`，与新库大版本一致。导出整个库，不要手挑表。序列、`cognitive_worker_migrations` 和表结构都要在这份 dump 里。

```bash
mkdir -p "$HOME/gojo-migrate"
chmod 700 "$HOME/gojo-migrate"
cd "$HOME/gojo-migrate"
bash
read -r -s ZEABUR_DATABASE_URL
echo
export ZEABUR_DATABASE_URL
docker run --rm \
  -e ZEABUR_DATABASE_URL \
  -v "$HOME/gojo-migrate:/backup" \
  postgres:16-alpine \
  sh -c 'pg_dump --format=custom --no-owner --no-acl --verbose --file=/backup/gojo.dump "$ZEABUR_DATABASE_URL"'
docker run --rm \
  -e ZEABUR_DATABASE_URL \
  -v "$HOME/gojo-migrate:/backup" \
  postgres:16-alpine \
  sh -c 'pg_dump --format=plain --no-owner --no-acl "$ZEABUR_DATABASE_URL" | gzip > /backup/gojo.sql.gz'
chmod 600 gojo.dump gojo.sql.gz
unset ZEABUR_DATABASE_URL
```

不要加 `--create`，也不要加 `--clean`。自定义格式那份用于导入；纯 SQL 那份留给你抽查。

如果报 SSL 错误，在 shell 里给连接串加上 `sslmode=require`（已有查询参数就用 `&sslmode=require`），然后重跑。不要把加过密的连接串写进文件再提交。

dump 是全部聊天和记忆。放在仓库目录外面。不要 `git add`。

导出之后、导入之前，对 Zeabur 跑一遍第 5 节的 SQL，把结果存下来。新库导入后逐项对比。

## 5. 导入新库

先只启动数据库，不要启动 app。app 一启动就会跑现有的建表和启动逻辑。空库上的导入应该发生在那之前。

把 dump 拷到新服务器，例如 `/home/gojo/gojo-migrate/`，权限 `700` 目录、`600` 文件。

```bash
cd /srv/gojo/deploy
docker compose up -d db
docker compose ps
```

等到 `db` 是 healthy。用户名和库名改成你的 `.env`。

自定义格式：

```bash
docker compose exec -T db \
  pg_restore --no-owner --no-acl --exit-on-error \
  -U gojo -d gojo < /home/gojo/gojo-migrate/gojo.dump
```

只有纯 SQL 时：

```bash
gunzip -c /home/gojo/gojo-migrate/gojo.sql.gz \
  | docker compose exec -T db \
    psql -v ON_ERROR_STOP=1 -U gojo -d gojo
```

如果 app 已经被误启动过，空卷里会先有一批空表，导入会报已经存在。这份卷里还没有迁过来的数据时，可以停掉并删掉这个新卷再来：

```bash
docker compose down
docker volume rm gojo_pgdata
docker compose up -d db
```

卷名是项目名 `gojo` 加卷名 `pgdata`。已经导入成功的卷不要删。

导入时如果报某个扩展不存在，先停下来看是哪一个。这套库不使用 pgvector。不要为了让导入通过而去装 pgvector。

### 不要在这次迁移里改这两列

`proactive_promise.trigger_at` 和 `long_memory.timestamp` 现在是 `timestamp without time zone`。历史数据里有两套钟面：更早的 `trigger_at` 是中国墙上时间，后来写入的是 UTC 墙上时间。用一条 `ALTER ... AT TIME ZONE 'UTC'` 会把其中一批整体拨错 8 小时，而且拨不回来。

这次只原样搬。列类型另开一单，自己带核对和回滚。导入和启动过程中都不要对这两列做类型转换。

### 导入后、启动 app 之前核对

下面的 SQL 在 Zeabur 和新库各跑一次。行数、最大 id、最新时间文本、`authority` 分组、序列都要一致。

`long_memory` 和 `bond_memory` 没有 `created_at`，时间列是 `timestamp`。

```bash
docker compose exec -T db psql -U gojo -d gojo
```

```sql
SELECT 'chat_log' AS table_name, COUNT(*)::bigint AS rows,
       MAX(id)::bigint AS max_id, MAX(created_at)::text AS latest
FROM chat_log
UNION ALL
SELECT 'long_memory', COUNT(*)::bigint, MAX(id)::bigint, MAX(timestamp)::text
FROM long_memory
UNION ALL
SELECT 'bond_memory', COUNT(*)::bigint, MAX(id)::bigint, MAX(timestamp)::text
FROM bond_memory
UNION ALL
SELECT 'cognitive_events', COUNT(*)::bigint, MAX(id)::bigint, MAX(created_at)::text
FROM cognitive_events
UNION ALL
SELECT 'cognitive_beliefs', COUNT(*)::bigint, MAX(id)::bigint, MAX(created_at)::text
FROM cognitive_beliefs
UNION ALL
SELECT 'candidate_audit_log', COUNT(*)::bigint, MAX(id)::bigint, MAX(created_at)::text
FROM candidate_audit_log
UNION ALL
SELECT 'char_phone_check', COUNT(*)::bigint, MAX(id)::bigint, MAX(created_at)::text
FROM char_phone_check
UNION ALL
SELECT 'proactive_promise', COUNT(*)::bigint, MAX(id)::bigint, MAX(created_at)::text
FROM proactive_promise
ORDER BY 1;
```

`long_memory.authority` 应能看到 `generated_recollection` 和 `canonical_evidence_v1`。还有别的值就记下来，不要删行。

```sql
SELECT COALESCE(authority, '<null>') AS authority, COUNT(*)
FROM long_memory
GROUP BY authority
ORDER BY 1;
```

列类型。前两列必须仍是 `timestamp without time zone`。`chat_log.created_at` 如果在 Zeabur 上已经是 `timestamp with time zone`，这里也应一样。

```sql
SELECT table_name, column_name, data_type
FROM information_schema.columns
WHERE (table_name, column_name) IN (
  ('proactive_promise', 'trigger_at'),
  ('long_memory', 'timestamp'),
  ('chat_log', 'created_at')
)
ORDER BY table_name, column_name;
```

序列最容易漏。序列没跟上，下一行插入就会主键冲突。`is_called = true` 时，`last_value` 必须大于等于该表 `max(id)`。`is_called = false` 时，`last_value` 必须大于 `max(id)`。空表可以跳过。

```sql
SELECT 'chat_log' AS table_name,
       (SELECT MAX(id) FROM chat_log)::bigint AS max_id,
       last_value, is_called
FROM chat_log_id_seq
UNION ALL
SELECT 'long_memory',
       (SELECT MAX(id) FROM long_memory)::bigint,
       last_value, is_called
FROM long_memory_id_seq
UNION ALL
SELECT 'bond_memory',
       (SELECT MAX(id) FROM bond_memory)::bigint,
       last_value, is_called
FROM bond_memory_id_seq
UNION ALL
SELECT 'cognitive_events',
       (SELECT MAX(id) FROM cognitive_events)::bigint,
       last_value, is_called
FROM cognitive_events_id_seq
UNION ALL
SELECT 'cognitive_beliefs',
       (SELECT MAX(id) FROM cognitive_beliefs)::bigint,
       last_value, is_called
FROM cognitive_beliefs_id_seq
UNION ALL
SELECT 'candidate_audit_log',
       (SELECT MAX(id) FROM candidate_audit_log)::bigint,
       last_value, is_called
FROM candidate_audit_log_id_seq
UNION ALL
SELECT 'char_phone_check',
       (SELECT MAX(id) FROM char_phone_check)::bigint,
       last_value, is_called
FROM char_phone_check_id_seq
UNION ALL
SELECT 'proactive_promise',
       (SELECT MAX(id) FROM proactive_promise)::bigint,
       last_value, is_called
FROM proactive_promise_id_seq
ORDER BY 1;
```

再看一眼全库还有哪些序列，避免上面八张表之外还有遗漏：

```sql
SELECT c.relname AS table_name, s.relname AS sequence_name
FROM pg_class s
JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
JOIN pg_class c ON c.oid = d.refobjid
WHERE s.relkind = 'S' AND c.relkind = 'r'
ORDER BY 1, 2;
```

只有某一行明确落后时，才把那一条序列拨到当前最大 id。这不改表里的数据。`MAX(id)` 为空就不要执行。例如：

```sql
SELECT setval('chat_log_id_seq', (SELECT MAX(id) FROM chat_log));
```

完整的 `pg_dump` 里已经带了 `setval`。正常导入后这里应该是齐的。不要为了“保险”对每一张表都重设。

## 6. 启动服务并看日志

核对通过后再构建并启动全部服务。第一次构建要拉镜像、装 Python 依赖。

```bash
cd /srv/gojo/deploy
docker compose up -d --build
docker compose ps
docker compose logs app --tail 200
```

app 的标准输出是无缓冲的，启动行会进 `docker compose logs`。进程会重启直到数据库能连上。若一直重启，先看日志里的连接错误，不要为了连上而把 5432 映射出去。

四个要确认的后台线程，以及日程表初始化，对应这些行：

- `[init] 角色日程表已就绪`
- `[generation_side_effects] worker 已启动`
- `[memory_jobs] worker 已启动`
- `[delayed_reply] phone-check delayed reply worker started`
- 认知 worker：生产开着时必须看到 `[cognitive_worker] started `。看到 `[cognitive_worker] disabled by COGNITIVE_WORKER_ENABLED` 就是 `.env` 没把开关带过来。

候选线程没有成功日志。`CANDIDATE_PRODUCER_ENABLED` 不是 `true` 时它不会启动，启动失败才会出现 `[init] candidate producer skipped`。不要把“没有这条跳过日志”当成已经在写记忆。以 `.env` 里那两项开关为准。

同一进程还会拉起日记排程、承诺排程和日程分享。能看到 `[diary_scheduler] 日记常驻排程已启动` 和 `[promise] 承诺驱动的主动排程已启动` 说明启动走到了后半段。`USE_RAG` 不是 `1` 时出现 `[rag] 未启用（USE_RAG != 1），使用全量注入 + prompt 缓存` 是预期，不是故障。

`chat_log.created_at` 若在导入后已经是 `timestamp with time zone`，现有启动迁移会跳过。若仍是 `timestamp without time zone`，现有代码会在启动时按 UTC 做一次转换。这是仓库里已经有的行为。不要在导入脚本里再改一次，也不要顺手改 `trigger_at` 或 `long_memory.timestamp`。

启动完成后可以再跑一次第 5 节的行数 SQL。若 dump 里已经有 `cognitive_worker_migrations` 的记录，这些启动修补不会再做一遍。行数若和导入前不一致，先停下来查，不要继续改手机地址。

## 7. 验证清单

- [ ] `docker compose ps` 里 app、db、caddy 都是 running，db 是 healthy
- [ ] 云防火墙和 `ufw status` 都只有 22/80/443，没有 5432
- [ ] 日志里有第 6 节列出的日程表行、generation、memory、delayed reply
- [ ] 认知 worker 的日志与 `.env` 里的开关一致
- [ ] `CANDIDATE_PRODUCER_ENABLED` 与 `CANDIDATE_PRODUCER_SHADOW` 与迁出前的生产值一致
- [ ] 第 5 节八张表的行数、最大 id、最新时间与 Zeabur 一致
- [ ] `long_memory` 的 `authority` 分组与 Zeabur 一致，能看到 `generated_recollection` 和 `canonical_evidence_v1`
- [ ] 八个序列的 `last_value` / `is_called` 没有落后于 `max(id)`
- [ ] `trigger_at` 与 `long_memory.timestamp` 仍是 `timestamp without time zone`
- [ ] `curl -fsS https://你的域名/health` 返回 `status` 为 `ok`，`db` 为 `postgresql`。配了 R2 时 `media_storage_ready` 为 true。`delayed_reply_worker` 为 true。`cognitive_worker` 与开关一致
- [ ] 发一条测试对话后，日志出现 `[recall] 两级召回完成`。若出现 `[recall] ⚠️ 两级召回失败`，先不要切换正式使用

`/health` 不包含密钥。测试对话会写进新库，不会写回 Zeabur。

语音是一条长连接（`/chat/voice_stream`），Caddy 对这一条不做 gzip，并把上游超时设为 300 秒。图片接口（`/chat/image`）的请求体上限是 32MB。一次语音回合超过 300 秒会被代理关掉；要放宽就改 `deploy/Caddyfile` 里的三个超时。

## 8. 切换

切换前先在手机设置页看清现在保存的地址，抄在一边，回滚用这个，不要凭记忆。

设置页里改服务器地址，保存的是运行时地址，不需要重新打包。地址形如 `https://你的域名`，不要带路径。保存后点「测试连接」，应提示服务器正常并给出角色数量。

仓库里的默认地址仍是 `constants/theme.ts` 的 `https://gojosatoru.zeabur.app`。已经装过的 app 用的是设置页保存的值。重装或清空应用数据会回到这个默认值。默认地址另开一单再改，不和这次搬家绑在一起。

## 9. 回滚

把设置页的地址改回第 8 节抄下来的 Zeabur 地址。确认新机器无误之前，不要在 Zeabur 上停服务、不要删库、不要销毁那台专用服务器。

切换之后产生的消息和记忆只在新库。把手机地址改回去，不会把这些新行搬回 Zeabur。如果切换后已经聊过一段，回滚前先考虑要不要把新库再导出一份。

## 10. 定时备份

Zeabur 的自动备份不会跟过来。夜间脚本把整库导出、压缩，上传到现有 R2 桶的 `db-backups/gojo_YYYYMMDD_HHMM.sql.gz`，并只删除这个命名规则下超过保留份数的对象。默认保留 14 份。它不会删除 `chat-media/` 或 `stickers/`。

脚本在宿主机运行，`pg_dump` 跑在 `db` 容器里，所以不需要把 5432 公布到宿主机。宿主机只需要 Docker、Python 3 和 `boto3`。

```bash
sudo apt-get install -y python3-venv
python3 -m venv /srv/gojo/.venv
/srv/gojo/.venv/bin/pip install boto3
sudo touch /var/log/gojo-backup.log
sudo chown gojo:gojo /var/log/gojo-backup.log
```

用 `gojo` 用户编辑 crontab（`crontab -e`）：

```cron
15 3 * * * cd /srv/gojo && /srv/gojo/.venv/bin/python scripts/backup_db_to_r2.py >> /var/log/gojo-backup.log 2>&1
```

时间是容器和系统的钟。脚本里的文件名用 `Asia/Shanghai`。先手工跑一次：

```bash
cd /srv/gojo && /srv/gojo/.venv/bin/python scripts/backup_db_to_r2.py
```

成功时日志有 `uploaded key=db-backups/gojo_...sql.gz` 和 `done key=`。任何失败都是非零退出码，并带 `[backup] failed`。不要配一个把退出码吃掉的包装。上传已经成功但清理失败时，日志会写明新对象还在，退出码仍是非零。

然后到 R2 上看一眼：`db-backups/` 下多了这个对象，`chat-media/` 和 `stickers/` 的对象没有减少。

压缩后的文件在上传时会整段读进内存，这是现有 `put_bytes` 的限制。未压缩的 SQL 写在临时文件里，不进内存。磁盘要能放下一份未压缩 dump。临时目录可用 `BACKUP_TMPDIR` 指到空间够的位置。

`BACKUP_DUMP_MODE` 保持 `compose`。不要为了让宿主机直接连库而修改 `DATABASE_URL` 或公布 5432。

## 这次不做的事

- 不把 `proactive_promise.trigger_at`、`long_memory.timestamp` 改成 `timestamptz`
- 不改 `db.py` 的 `TimeZone=UTC`
- 不引入 k3s、Coolify 或其它自建平台
- 不在仓库里放真实密钥
- 不在确认新机器之前停止 Zeabur
