"""db_chatlog.py —— 单聊完整聊天记录(服务器端)

为什么要这张表:
  之前单聊气泡只存在手机 AsyncStorage 里,后果是
    · 卸载重装 APK → 聊天记录全没
    · 换手机 → 记录不同步
    · 手机丢了/坏了 → 永久丢失
  short_memory 那张表是给 LLM 用的(24 小时 / 40 条上限),不是完整历史。

和 short_memory 的分工:
  short_memory —— 给 LLM 看的上下文,会过期、有上限、只存文本
  chat_log     —— 给人看的完整记录,永久保存、带气泡渲染需要的全部字段

设计:
  · client_msg_id 做幂等键 —— 前端重发/重试不会写重复
  · 音频不存这里(base64 太占空间),只标记有没有,重播走 TTS 重新合成
  · 按 chat_id 分组,单聊用 character_id,以后要扩展也方便
"""
import json

from db import get_conn


def init_chatlog_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS chat_log (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,              -- 单聊=character_id,群聊=group_xx
        client_msg_id TEXT,                 -- 前端生成的唯一 id,用来幂等
        role TEXT NOT NULL,                 -- 'user' | 'gojo'
        text TEXT NOT NULL DEFAULT '',      -- 主体文字(用户=中文,角色=日语)
        subtitle TEXT DEFAULT '',           -- 角色消息的中文翻译
        emotion TEXT DEFAULT '',
        kind TEXT DEFAULT 'text',           -- text / image / call_log / system
        extra TEXT DEFAULT '',              -- JSON:图片 uri、通话时长等附加信息
        has_audio BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_chatlog_lookup
                   ON chat_log (user_id, chat_id, id)''')
    # 幂等:同一个 client_msg_id 只存一条
    cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_chatlog_client
                   ON chat_log (user_id, chat_id, client_msg_id)
                   WHERE client_msg_id IS NOT NULL AND client_msg_id <> \'\'''')
    # 单条删除墓碑:挡住「DELETE 先到、append 后到」把气泡复活。
    # 只服务聊天记录删除,与 short/long/bond memory 无关。
    cur.execute('''CREATE TABLE IF NOT EXISTS chat_log_tombstone (
        user_id TEXT NOT NULL,
        chat_id TEXT NOT NULL,
        client_msg_id TEXT NOT NULL,
        deleted_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (user_id, chat_id, client_msg_id)
    )''')
    # ★ 时区修复(幂等,已经是 timestamptz 就跳过)
    #   原来 timestamp without time zone 存不住时区:
    #   前端 toISOString() 传来的是 UTC,Z 被丢掉;读出来没有偏移量,
    #   前端 new Date() 当本地时间解析 → 差 8 小时。
    #   改成 timestamptz 后 Postgres 自己管转换,isoformat() 会带 +00:00。
    try:
        cur.execute("""SELECT data_type FROM information_schema.columns
                       WHERE table_name='chat_log' AND column_name='created_at'""")
        row = cur.fetchone()
        if row and row[0] == 'timestamp without time zone':
            cur.execute("""ALTER TABLE chat_log
                           ALTER COLUMN created_at TYPE timestamptz
                           USING created_at AT TIME ZONE 'UTC'""")
            conn.commit()
            print('[init] chat_log.created_at 迁移为 timestamptz(修时区差 8 小时)')
    except Exception as e:
        conn.rollback()
        print(f'[init] chat_log 时区迁移跳过：{e}')
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 聊天记录表已就绪：chat_log / chat_log_tombstone')


def append_messages(user_id, chat_id, msgs):
    """批量追加。msgs = [{client_msg_id, role, text, subtitle, emotion, kind, extra, has_audio}]
    重复的 client_msg_id 自动跳过。返回实际写入条数。"""
    if not msgs:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    written = 0
    try:
        for m in msgs:
            role = (m.get('role') or '').strip()
            if role not in ('user', 'gojo'):
                continue
            client_msg_id = (m.get('client_msg_id') or '')[:120]
            if client_msg_id:
                cur.execute(
                    '''SELECT 1
                       FROM chat_log_tombstone
                       WHERE user_id=%s
                         AND chat_id=%s
                         AND client_msg_id=%s''',
                    (user_id, chat_id, client_msg_id))
                if cur.fetchone():
                    continue
            # ★ 前端传了真实时间就用它,没传才用当前时间。
            #   补传历史消息时这个很关键,不然全挤在同一时刻。
            ts = (m.get('ts') or '').strip()
            if ts:
                cur.execute(
                    '''INSERT INTO chat_log
                         (user_id, chat_id, client_msg_id, role, text, subtitle,
                          emotion, kind, extra, has_audio, created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING''',
                    (user_id, chat_id,
                     client_msg_id, role,
                     (m.get('text') or '')[:4000],
                     (m.get('subtitle') or '')[:4000],
                     (m.get('emotion') or '')[:20],
                     (m.get('kind') or 'text')[:20],
                     (m.get('extra') or '')[:6000],
                     bool(m.get('has_audio')), ts)
                )
            else:
                cur.execute(
                    '''INSERT INTO chat_log
                         (user_id, chat_id, client_msg_id, role, text, subtitle,
                          emotion, kind, extra, has_audio)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       ON CONFLICT DO NOTHING''',
                    (user_id, chat_id,
                     client_msg_id, role,
                     (m.get('text') or '')[:4000],
                     (m.get('subtitle') or '')[:4000],
                     (m.get('emotion') or '')[:20],
                     (m.get('kind') or 'text')[:20],
                     (m.get('extra') or '')[:6000],
                     bool(m.get('has_audio')))
                )
            written += cur.rowcount
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return written


def get_messages(user_id, chat_id, limit=200, before_id=None):
    """取历史,新→旧翻页。返回 (消息列表[旧→新], 是否还有更早的)。"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        if before_id:
            cur.execute(
                '''SELECT id, client_msg_id, role, text, subtitle, emotion,
                          kind, extra, has_audio, created_at
                   FROM chat_log
                   WHERE user_id=%s AND chat_id=%s AND id < %s
                   ORDER BY id DESC LIMIT %s''',
                (user_id, chat_id, before_id, limit + 1))
        else:
            cur.execute(
                '''SELECT id, client_msg_id, role, text, subtitle, emotion,
                          kind, extra, has_audio, created_at
                   FROM chat_log
                   WHERE user_id=%s AND chat_id=%s
                   ORDER BY id DESC LIMIT %s''',
                (user_id, chat_id, limit + 1))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    has_more = len(rows) > limit
    rows = rows[:limit]
    out = [{
        'id': r[0],
        'client_msg_id': r[1] or '',
        'role': r[2],
        'text': r[3] or '',
        'subtitle': r[4] or '',
        'emotion': r[5] or '',
        'kind': r[6] or 'text',
        'extra': r[7] or '',
        'has_audio': bool(r[8]),
        'ts': r[9].isoformat() if r[9] else None,
    } for r in rows]
    out.reverse()          # 旧→新,前端直接铺
    return out, has_more


def _parse_extra(extra):
    if not extra:
        return {}
    try:
        parsed = json.loads(extra)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _history_content(role, text, subtitle, kind, extra):
    bits = []
    reply_to = extra.get('reply_to') or extra.get('replyTo')
    if isinstance(reply_to, dict) and reply_to.get('text'):
        name = reply_to.get('name') or '上一条消息'
        bits.append(f'【引用】{name}: {str(reply_to.get("text") or "")[:500]}')

    visual = extra.get('visual_summary') or extra.get('visualSummary')
    event_meta = extra.get('event_meta') or extra.get('eventMeta')
    if kind in ('image', 'video') or visual:
        label = '视频' if kind == 'video' or (isinstance(event_meta, dict) and event_meta.get('kind') == 'video') else '图片'
        if visual:
            bits.append(f'【{label}摘要】{str(visual)[:800]}')
        elif text:
            bits.append(f'【{label}】{text}')

    if text:
        bits.append(text)
    if role != 'user' and subtitle:
        bits.append(f'（中文：{subtitle}）')
    return '\n'.join(bits).strip()


def get_prompt_history(user_id, chat_id, limit=24):
    """Return chat_log as Anthropic-style prompt messages.

    This is read-only UI history, not memory evidence. It lets text and image
    endpoints share one history source and keeps image/reply metadata available
    for later turns without writing it into short_memory or relationship state.
    """
    limit = max(1, min(80, int(limit or 24)))
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT role, text, subtitle, kind, extra
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
               ORDER BY id DESC LIMIT %s''',
            (user_id, chat_id, limit))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    rows.reverse()
    out = []
    for role, text, subtitle, kind, extra in rows:
        prompt_role = 'user' if role == 'user' else 'assistant'
        content = _history_content(
            role, text or '', subtitle or '',
            kind or 'text', _parse_extra(extra or ''))
        if content:
            out.append({'role': prompt_role, 'content': content})
    return out


def _tombstone_client_msg_id(client_msg_id):
    """前端旧记录可能用 srv_123 这种合成 id,不能写进墓碑。"""
    cid = (client_msg_id or '')[:120]
    if not cid or cid.startswith('srv_'):
        return ''
    return cid


def delete_message(user_id, chat_id, client_msg_id='', server_id=None):
    """删除单条聊天记录。

    只动 chat_log / chat_log_tombstone,不删 short/long/bond/character
    memory,也不改 relationship ledger / provenance / cognitive evidence。
    先写墓碑再 DELETE,避免 append 晚到把气泡复活。
    """
    client_msg_id = (client_msg_id or '')[:120]
    conn = get_conn()
    cur = conn.cursor()
    try:
        real_client_msg_id = ''
        if server_id is not None:
            cur.execute(
                '''SELECT client_msg_id
                   FROM chat_log
                   WHERE id=%s AND user_id=%s AND chat_id=%s''',
                (server_id, user_id, chat_id))
            row = cur.fetchone()
            if row:
                real_client_msg_id = (row[0] or '')[:120]
        tombstone_id = _tombstone_client_msg_id(
            real_client_msg_id or client_msg_id)
        if tombstone_id:
            cur.execute(
                '''INSERT INTO chat_log_tombstone
                   (user_id, chat_id, client_msg_id)
                   VALUES (%s,%s,%s)
                   ON CONFLICT DO NOTHING''',
                (user_id, chat_id, tombstone_id))
        if server_id is not None:
            cur.execute(
                '''DELETE FROM chat_log
                   WHERE id=%s
                     AND user_id=%s
                     AND chat_id=%s''',
                (server_id, user_id, chat_id))
        else:
            cur.execute(
                '''DELETE FROM chat_log
                   WHERE user_id=%s
                     AND chat_id=%s
                     AND client_msg_id=%s''',
                (user_id, chat_id, client_msg_id))
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        cur.close()
        conn.close()


def clear_chat(user_id, chat_id):
    """清空某个聊天的记录(对应聊天页的「清空」按钮)。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM chat_log WHERE user_id=%s AND chat_id=%s',
                (user_id, chat_id))
    n = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    print(f'[chatlog] 清空 {user_id}/{chat_id}: {n} 条')
    return n


def count_messages(user_id, chat_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM chat_log WHERE user_id=%s AND chat_id=%s',
                (user_id, chat_id))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n
