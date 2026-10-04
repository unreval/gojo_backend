"""db_chatlog.py —— 单聊完整聊天记录(服务器端)

为什么要这张表:
  之前单聊气泡只存在手机 AsyncStorage 里,后果是
    · 卸载重装 APK → 聊天记录全没
    · 换手机 → 记录不同步
    · 手机丢了/坏了 → 永久丢失
  short_memory 那张表是给 LLM 用的(24 小时 / 40 条上限),不是完整历史。

和 short_memory 的分工:
  short_memory —— LLM 近窗 compatibility cache，会过期、有上限，不是事实源
  chat_log     —— Canonical Raw Event / 完整记录，永久保存、带气泡渲染字段

设计:
  · client_msg_id 做幂等键 —— 前端重发/重试不会写重复
  · 音频不存这里(base64 太占空间),只标记有没有,重播走 TTS 重新合成
  · 按 chat_id 分组,单聊用 character_id,以后要扩展也方便
"""
import json

from assistant_turn import (
    collapse_assistant_logical_turns,
    filter_hidden_aggregates,
    hidden_aggregate_ids_from_rows,
    turn_facts_from_rows,
)
from db import get_conn

ASSISTANT_IDENTITY_SQL = (
    '''SELECT event_id, client_msg_id, role, extra,
              COALESCE(status, 'active')
       FROM chat_log
       WHERE user_id=%s AND chat_id=%s
         AND role IN ('gojo', 'assistant')'''
)


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
    # L0 ledger columns. Idempotent; chat_log remains the only source of truth.
    for ddl in (
        "ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS event_id TEXT",
        "ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'active'",
        "ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMPTZ",
        "ALTER TABLE chat_log ADD COLUMN IF NOT EXISTS reply_to_event_id TEXT",
    ):
        cur.execute(ddl)
    cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_chatlog_event_id
                   ON chat_log (user_id, chat_id, event_id)
                   WHERE event_id IS NOT NULL AND event_id <> '' ''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_chatlog_active_time
                   ON chat_log (user_id, chat_id, created_at DESC)
                   WHERE COALESCE(status, 'active') = 'active' ''')
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


def append_messages(user_id, chat_id, msgs, *, conn=None):
    """批量追加。msgs = [{client_msg_id, role, text, subtitle, emotion, kind, extra, has_audio}]
    重复的 client_msg_id 自动跳过。返回实际写入条数。"""
    if not msgs:
        return 0
    owns_connection = conn is None
    conn = get_conn() if owns_connection else conn
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
            event_id = (m.get('event_id') or client_msg_id or '')[:120]
            reply_to_event_id = (m.get('reply_to_event_id') or '')[:120]
            extra = (m.get('extra') or '')[:6000]
            try:
                import raw_events
                parsed = {}
                if extra:
                    loaded = json.loads(extra)
                    if isinstance(loaded, dict):
                        parsed = loaded
                stamped = raw_events.infer_assistant_identity(
                    role, event_id or client_msg_id, parsed)
                if stamped:
                    extra = json.dumps(stamped, ensure_ascii=False)[:6000]
            except Exception:
                pass
            if ts:
                cur.execute(
                    '''INSERT INTO chat_log
                         (user_id, chat_id, client_msg_id, role, text, subtitle,
                          emotion, kind, extra, has_audio, created_at,
                          event_id, status, reply_to_event_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)
                       ON CONFLICT DO NOTHING''',
                    (user_id, chat_id,
                     client_msg_id, role,
                     (m.get('text') or '')[:4000],
                     (m.get('subtitle') or '')[:4000],
                     (m.get('emotion') or '')[:20],
                     (m.get('kind') or 'text')[:20],
                     extra,
                     bool(m.get('has_audio')), ts, event_id, reply_to_event_id)
                )
            else:
                cur.execute(
                    '''INSERT INTO chat_log
                         (user_id, chat_id, client_msg_id, role, text, subtitle,
                          emotion, kind, extra, has_audio,
                          event_id, status, reply_to_event_id)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s)
                       ON CONFLICT DO NOTHING''',
                    (user_id, chat_id,
                     client_msg_id, role,
                     (m.get('text') or '')[:4000],
                     (m.get('subtitle') or '')[:4000],
                     (m.get('emotion') or '')[:20],
                     (m.get('kind') or 'text')[:20],
                     extra,
                     bool(m.get('has_audio')), event_id, reply_to_event_id)
                )
            written += cur.rowcount
        if owns_connection:
            conn.commit()
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            conn.close()
    return written


def _reply_row(cur, user_id, chat_id, reference_id):
    """Read one active quoted event in this conversation, never its client preview."""
    reference_id = str(reference_id or '').strip()[:120]
    if not reference_id:
        return None
    if reference_id.startswith('srv_') and reference_id[4:].isdigit():
        where = 'id=%s'
        key = int(reference_id[4:])
    else:
        where = '(event_id=%s OR client_msg_id=%s)'
        key = (reference_id, reference_id)
    params = (user_id, chat_id, *(key if isinstance(key, tuple) else (key,)))
    cur.execute(
        '''SELECT id, client_msg_id, COALESCE(NULLIF(event_id, ''), client_msg_id),
                  role, text, subtitle, created_at, extra
           FROM chat_log
           WHERE user_id=%s AND chat_id=%s AND ''' + where + '''
             AND COALESCE(status, 'active') = 'active'
             AND deleted_at IS NULL
             AND NOT EXISTS (
                 SELECT 1 FROM chat_log_tombstone tomb
                 WHERE tomb.user_id=chat_log.user_id
                   AND tomb.chat_id=chat_log.chat_id
                   AND tomb.client_msg_id=chat_log.client_msg_id)
           LIMIT 2 FOR SHARE''',
        params)
    rows = cur.fetchall()
    if len(rows) != 1 or not rows[0][2]:
        return None
    row = rows[0]
    return {
        'server_id': row[0], 'client_msg_id': row[1] or '',
        'source_event_id': row[2], 'role': row[3],
        'name': '角色' if row[3] in ('gojo', 'assistant') else '用户',
        'text': row[4] or '', 'subtitle': row[5] or '',
        'ts': row[6].isoformat() if row[6] else None,
        'translation_source': _parse_extra(row[7]).get('translation_source') or '',
    }


def resolve_reply_reference(user_id, chat_id, reference, *, conn=None):
    """Resolve every supplied identifier to the same active chat_log event."""
    if not reference:
        return None
    if isinstance(reference, dict):
        ids = [reference.get(key) for key in
               ('source_event_id', 'sourceEventId', 'event_id',
                'eventId', 'id')]
    else:
        ids = [reference]
    ids = list(dict.fromkeys(str(value).strip()[:120] for value in ids if value))
    if not ids:
        return None
    owned = conn is None
    conn = get_conn() if owned else conn
    cur = conn.cursor()
    try:
        targets = [_reply_row(cur, user_id, chat_id, value) for value in ids]
        if any(target is None for target in targets):
            return None
        first = targets[0]
        if any(target['server_id'] != first['server_id'] for target in targets[1:]):
            return None
        return first
    finally:
        cur.close()
        if owned:
            conn.close()


def _reply_preview(target):
    text = target['text']
    return {
        'id': target['client_msg_id'] or target['source_event_id'],
        'source_event_id': target['source_event_id'],
        'role': target['role'],
        'name': '角色' if target['role'] in ('gojo', 'assistant') else '用户',
        'text': text[:500],
        'subtitle': target['subtitle'][:500],
        'translation_source': target['translation_source'],
        'ts': target['ts'],
        'truncated': len(text) > 500,
    }


def append_messages_confirmed(user_id, chat_id, msgs, *, conn=None):
    """Append with per-message receipts; never accept an ID alone as a retry."""
    if not msgs:
        return []
    owns_connection = conn is None
    conn = get_conn() if owns_connection else conn
    cur = conn.cursor()
    results = []
    try:
        for m in msgs:
            client_id = str(m.get('client_msg_id') or '').strip()[:120]
            event_id = str(m.get('event_id') or client_id).strip()[:120]
            role = str(m.get('role') or '').strip()
            receipt = {'id': client_id or event_id}
            if not client_id or role not in ('user', 'gojo'):
                results.append({**receipt, 'status': 'conflict',
                                'reason': 'invalid_message_identity'})
                continue
            cur.execute(
                '''SELECT 1 FROM chat_log_tombstone
                   WHERE user_id=%s AND chat_id=%s AND client_msg_id=%s''',
                (user_id, chat_id, client_id))
            if cur.fetchone():
                results.append({**receipt, 'status': 'deleted_rejected'})
                continue

            raw_extra = m.get('extra') or ''
            if raw_extra and not isinstance(raw_extra, dict):
                try:
                    if not isinstance(json.loads(raw_extra), dict):
                        raise ValueError('extra must be an object')
                except (TypeError, ValueError, json.JSONDecodeError):
                    results.append({**receipt, 'status': 'conflict',
                                    'reason': 'invalid_extra'})
                    continue
            extra = _parse_extra(raw_extra)
            if role == 'gojo':
                import raw_events
                extra = raw_events.infer_assistant_identity(role, event_id, extra)
            preview = extra.get('reply_to') or extra.get('replyTo') if role == 'user' else None
            identifiers = [m.get('reply_to_event_id'), extra.get('reply_to_event_id')]
            if role == 'user':
                identifiers.append(extra.get('reply_to_source_event_id'))
            if role == 'user' and isinstance(preview, dict):
                identifiers.extend(preview.get(key) for key in
                                   ('source_event_id', 'sourceEventId',
                                    'event_id', 'eventId', 'id'))
            identifiers = list(dict.fromkeys(
                str(value).strip()[:120] for value in identifiers if value))
            target = resolve_reply_reference(
                user_id, chat_id,
                {'source_event_id': identifiers[0]} if len(identifiers) == 1
                else identifiers[0] if identifiers else None,
                conn=conn) if identifiers else None
            if target and any(
                    resolve_reply_reference(user_id, chat_id, value, conn=conn)
                    != target for value in identifiers[1:]):
                results.append({**receipt, 'status': 'conflict',
                                'reason': 'reference_identity_mismatch'})
                continue
            if target and target['source_event_id'] == event_id:
                target = None
            if role == 'user':
                for key in ('reply_to', 'replyTo', 'reply_to_event_id',
                            'reply_to_source_event_id'):
                    extra.pop(key, None)
            if target:
                extra['reply_to_event_id'] = target['source_event_id']
                if role == 'user':
                    extra['reply_to'] = _reply_preview(target)
            reply_id = target['source_event_id'] if target else ''
            if (identifiers or isinstance(preview, dict)) and not target:
                receipt['reference_status'] = 'unavailable'
                if role == 'user':
                    extra['reply_unavailable'] = True

            text = str(m.get('text') or '')[:4000]
            subtitle = str(m.get('subtitle') or '')[:4000]
            emotion = str(m.get('emotion') or '')[:20]
            kind = str(m.get('kind') or 'text')[:20]
            has_audio = bool(m.get('has_audio'))
            extra_json = json.dumps(extra, ensure_ascii=False) if extra else ''
            cur.execute(
                '''SELECT id, client_msg_id, event_id, role, text, subtitle,
                          emotion, kind, extra, has_audio, reply_to_event_id,
                          COALESCE(status, 'active')
                   FROM chat_log WHERE user_id=%s AND chat_id=%s
                     AND (client_msg_id=%s OR event_id=%s)
                   ORDER BY id DESC LIMIT 1 FOR UPDATE''',
                (user_id, chat_id, client_id, event_id))
            row = cur.fetchone()
            if row is None:
                ts = str(m.get('ts') or '').strip()
                columns = '''(user_id,chat_id,client_msg_id,role,text,subtitle,
                              emotion,kind,extra,has_audio,event_id,status,
                              reply_to_event_id'''
                values = '''(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s'''
                args = [user_id, chat_id, client_id, role, text, subtitle,
                        emotion, kind, extra_json, has_audio, event_id, reply_id]
                if ts:
                    columns += ',created_at'
                    values += ',%s'
                    args.append(ts)
                cur.execute(
                    'INSERT INTO chat_log ' + columns + ') VALUES ' + values
                    + ') ON CONFLICT DO NOTHING RETURNING id', args)
                inserted = cur.fetchone()
                if inserted:
                    results.append({**receipt, 'status': 'inserted',
                                    'server_id': inserted[0],
                                    'reply_to_event_id': reply_id})
                    continue
                cur.execute(
                    '''SELECT id, client_msg_id, event_id, role, text, subtitle,
                              emotion, kind, extra, has_audio, reply_to_event_id,
                              COALESCE(status, 'active')
                       FROM chat_log WHERE user_id=%s AND chat_id=%s
                         AND (client_msg_id=%s OR event_id=%s)
                       ORDER BY id DESC LIMIT 1 FOR UPDATE''',
                    (user_id, chat_id, client_id, event_id))
                row = cur.fetchone()
            if not row:
                results.append({**receipt, 'status': 'conflict',
                                'reason': 'identity_unavailable'})
                continue
            if row[11] != 'active':
                results.append({**receipt, 'status': 'deleted_rejected'})
                continue
            # A repaired subtitle is display metadata. A stale client with an
            # empty subtitle may retry the same assistant event, but may never
            # replace the server's repaired translation or the Japanese text.
            same_subtitle = (row[5] == subtitle or
                             (role == 'gojo' and not subtitle and bool(row[5])))
            same = (row[1] == client_id and (not row[2] or row[2] == event_id)
                    and row[3] == role and row[4] == text
                    and same_subtitle and row[6:8] == (emotion, kind)
                    and bool(row[9]) == has_audio)
            if not same:
                results.append({**receipt, 'status': 'conflict',
                                'reason': 'semantic_identity_mismatch'})
                continue
            old_reply = row[10] or ''
            if reply_id and old_reply and old_reply != reply_id:
                results.append({**receipt, 'status': 'conflict',
                                'reason': 'reference_conflict'})
                continue
            old_extra = _parse_extra(row[8] or '')
            needs_preview = role == 'user' and old_extra.get('reply_to') != _reply_preview(target) if target else False
            if reply_id and (not old_reply or old_extra.get('reply_to_event_id') != reply_id
                             or needs_preview):
                old_extra['reply_to_event_id'] = reply_id
                if role == 'user':
                    old_extra['reply_to'] = _reply_preview(target)
                cur.execute(
                    '''UPDATE chat_log SET reply_to_event_id=%s, extra=%s
                       WHERE id=%s AND user_id=%s AND chat_id=%s
                         AND COALESCE(status, 'active')='active'
                         AND COALESCE(reply_to_event_id, '') IN ('', %s)''',
                    (reply_id, json.dumps(old_extra, ensure_ascii=False),
                     row[0], user_id, chat_id, reply_id))
                results.append({**receipt, 'status': 'metadata_enriched',
                                'server_id': row[0],
                                'reply_to_event_id': reply_id})
            else:
                results.append({**receipt, 'status': 'already_identical',
                                'server_id': row[0],
                                'reply_to_event_id': old_reply})
        if owns_connection:
            conn.commit()
        return results
    except Exception:
        if owns_connection:
            conn.rollback()
        raise
    finally:
        cur.close()
        if owns_connection:
            conn.close()


def _row_to_message(row):
    message = {
        'id': row[0],
        'client_msg_id': row[1] or '',
        'role': row[2],
        'text': row[3] or '',
        'subtitle': row[4] or '',
        'emotion': row[5] or '',
        'kind': row[6] or 'text',
        'extra': row[7] or '',
        'has_audio': bool(row[8]),
        'ts': row[9].isoformat() if row[9] else None,
        'event_id': (row[10] if len(row) > 10 else '') or '',
        'reply_to_event_id': (row[11] if len(row) > 11 else '') or '',
    }
    if message['role'] == 'gojo':
        from generation_contract import translation_missing
        message['translation_missing'] = translation_missing({
            'jp': message['text'], 'zh': message['subtitle']})
        message['translation_source'] = _parse_extra(message['extra']).get(
            'translation_source') or ''
    return message


def _fetch_message_page(user_id, chat_id, limit, before_id=None):
    conn = get_conn()
    cur = conn.cursor()
    try:
        if before_id:
            cur.execute(
                '''SELECT id, client_msg_id, role, text, subtitle, emotion,
                          kind, extra, has_audio, created_at, event_id,
                          reply_to_event_id
                   FROM chat_log
                   WHERE user_id=%s AND chat_id=%s AND id < %s
                     AND COALESCE(status, 'active') = 'active'
                     AND NOT EXISTS (
                         SELECT 1 FROM chat_log_tombstone tomb
                         WHERE tomb.user_id=chat_log.user_id
                           AND tomb.chat_id=chat_log.chat_id
                           AND tomb.client_msg_id=chat_log.client_msg_id)
                   ORDER BY id DESC LIMIT %s''',
                (user_id, chat_id, before_id, limit))
        else:
            cur.execute(
                '''SELECT id, client_msg_id, role, text, subtitle, emotion,
                          kind, extra, has_audio, created_at, event_id,
                          reply_to_event_id
                   FROM chat_log
                   WHERE user_id=%s AND chat_id=%s
                     AND COALESCE(status, 'active') = 'active'
                     AND NOT EXISTS (
                         SELECT 1 FROM chat_log_tombstone tomb
                         WHERE tomb.user_id=chat_log.user_id
                           AND tomb.chat_id=chat_log.chat_id
                           AND tomb.client_msg_id=chat_log.client_msg_id)
                   ORDER BY id DESC LIMIT %s''',
                (user_id, chat_id, limit))
        return [_row_to_message(row) for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def assistant_identity_rows(user_id, chat_id):
    """Assistant chat_log rows including deleted historical segments."""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(ASSISTANT_IDENTITY_SQL, (user_id, chat_id))
        rows = [{
            'event_id': row[0] or '',
            'client_msg_id': row[1] or '',
            'role': row[2],
            'extra': row[3] or '',
            'status': (row[4] if len(row) > 4 else '') or 'active',
        } for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()
    for row in rows:
        if not row['event_id']:
            row['event_id'] = row['client_msg_id']
    return rows


def assistant_turn_facts(user_id, chat_id):
    return turn_facts_from_rows(assistant_identity_rows(user_id, chat_id))


def hidden_aggregate_event_ids(user_id, chat_id):
    """Aggregates that must not appear as extra chat bubbles."""
    return hidden_aggregate_ids_from_rows(assistant_identity_rows(user_id, chat_id))


def filter_visible_messages(user_id, chat_id, msgs):
    return filter_hidden_aggregates(
        msgs, hidden_aggregate_event_ids(user_id, chat_id))


def get_messages(user_id, chat_id, limit=200, before_id=None):
    """取历史,新→旧翻页。返回 (消息列表[旧→新], 是否还有更早的)。

    Aggregate + segments → only segments are user-visible.
    Aggregate only remains as a durability fallback.
    Sibling lookup is chat-wide so pagination cannot resurface an aggregate.
    """
    limit = max(1, int(limit or 200))
    hidden = hidden_aggregate_event_ids(user_id, chat_id)
    visible = []
    cursor = before_id
    while True:
        batch = _fetch_message_page(
            user_id, chat_id, limit + 1, before_id=cursor)
        if not batch:
            break
        for msg in batch:
            visible.extend(filter_hidden_aggregates([msg], hidden))
            if len(visible) > limit:
                break
        if len(visible) > limit:
            break
        if len(batch) <= limit:
            break
        next_cursor = batch[-1]['id']
        if next_cursor == cursor:
            break
        cursor = next_cursor
    has_more = len(visible) > limit
    out = visible[:limit]
    out.reverse()
    _attach_chat_media(user_id, chat_id, out)
    _attach_reply_display(user_id, chat_id, out)
    return out, has_more


def _attach_reply_display(user_id, chat_id, msgs):
    """Derive quote previews from active sources for this response only."""
    for msg in msgs:
        if msg.get('role') != 'user':
            continue
        extra = _parse_extra(msg.get('extra'))
        preview = extra.get('reply_to') or extra.get('replyTo')
        ref = (msg.get('reply_to_event_id') or extra.get('reply_to_event_id')
               or extra.get('reply_to_source_event_id')
               or ((preview.get('source_event_id') or preview.get('id'))
                   if isinstance(preview, dict) else ''))
        if not ref and not preview:
            continue
        target = resolve_reply_reference(user_id, chat_id, ref)
        extra.pop('replyTo', None)
        if target:
            extra['reply_to'] = _reply_preview(target)
            extra['reply_to_source_event_id'] = target['source_event_id']
            extra.pop('reply_unavailable', None)
        else:
            extra.pop('reply_to', None)
            extra['reply_unavailable'] = True
        msg['extra'] = json.dumps(extra, ensure_ascii=False)


def _attach_chat_media(user_id, chat_id, msgs):
    """Hydrate user image bubbles from chat_media via source_event_id.

    Signed URLs are generated at read time and never written back to extra.
    """
    event_ids = []
    seen = set()
    for msg in msgs or []:
        if (msg.get('role') != 'user') or (msg.get('kind') != 'image'):
            continue
        for key in (msg.get('client_msg_id'), msg.get('event_id')):
            value = str(key or '').strip()
            if value and value not in seen:
                seen.add(value)
                event_ids.append(value)
    if not event_ids:
        return
    try:
        import db_chat_media
        records = db_chat_media.get_media_by_source_events(
            user_id, chat_id, event_ids, media_kind='image')
    except Exception as e:
        print(f'[chat-media] hydrate lookup skipped:{e}')
        return
    if not records:
        return
    for msg in msgs:
        if (msg.get('role') != 'user') or (msg.get('kind') != 'image'):
            continue
        rec = records.get(msg.get('event_id') or '') or records.get(
            msg.get('client_msg_id') or '')
        if not rec:
            msg['media'] = None
            continue
        try:
            msg['media'] = db_chat_media.public_media(rec)
        except Exception as e:
            print(f'[chat-media] signed url failed source_event_id='
                  f'{rec.get("source_event_id")}:{e}')
            msg['media'] = None


def list_message_event_keys(user_id, chat_id, client_msg_id='', server_id=None):
    """client_msg_id / event_id for one chat_log row (active or deleted)."""
    keys = []
    seen = set()

    def _add(value):
        text = str(value or '').strip()[:120]
        if text and text not in seen:
            seen.add(text)
            keys.append(text)

    _add(client_msg_id)
    conn = get_conn()
    cur = conn.cursor()
    try:
        if server_id is not None:
            cur.execute(
                '''SELECT client_msg_id, event_id
                   FROM chat_log
                   WHERE id=%s AND user_id=%s AND chat_id=%s''',
                (server_id, user_id, chat_id))
        elif client_msg_id:
            cur.execute(
                '''SELECT client_msg_id, event_id
                   FROM chat_log
                   WHERE user_id=%s AND chat_id=%s AND client_msg_id=%s
                   ORDER BY id DESC LIMIT 1''',
                (user_id, chat_id, client_msg_id))
        else:
            return keys
        row = cur.fetchone()
        if row:
            _add(row[0])
            _add(row[1])
    finally:
        cur.close()
        conn.close()
    return keys


def _parse_extra(extra):
    if isinstance(extra, dict):
        return dict(extra)
    if not extra:
        return {}
    try:
        parsed = json.loads(extra)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _history_content(role, text, subtitle, kind, extra):
    bits = []
    reply_to = extra.get('verified_reply_to')
    if isinstance(reply_to, dict):
        bits.append(
            f'【明确引用 source_event_id={reply_to["source_event_id"]} '
            f'说话者={reply_to["name"]}({reply_to["role"]}) '
            f'发生时间={reply_to["ts"] or "未知"}】'
            f'{reply_to["text"][:500]}')
    elif extra.get('reply_unavailable'):
        bits.append('【引用原始事件不可用；缓存预览不作为事实】')

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
    if role != 'user' and subtitle and extra.get('translation_source') != 'user_supplied':
        bits.append(f'（中文：{subtitle}）')
    return '\n'.join(bits).strip()


def get_prompt_history(user_id, chat_id, limit=24):
    """Return chat_log as Anthropic-style prompt messages.

    This is read-only UI history, not memory evidence. It lets text and image
    endpoints share one history source and keeps image/reply metadata available
    for later turns without writing it into short_memory or relationship state.
    """
    limit = max(1, min(80, int(limit or 24)))
    fetch_limit = max(1, min(80, limit * 3))
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT role, text, subtitle, kind, extra,
                      event_id, client_msg_id, reply_to_event_id
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND COALESCE(status, 'active') = 'active'
                 AND NOT EXISTS (
                     SELECT 1 FROM chat_log_tombstone tomb
                     WHERE tomb.user_id=chat_log.user_id
                       AND tomb.chat_id=chat_log.chat_id
                       AND tomb.client_msg_id=chat_log.client_msg_id)
               ORDER BY id DESC LIMIT %s''',
            (user_id, chat_id, fetch_limit))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    rows.reverse()
    events = []
    for row in rows:
        role, text, subtitle, kind, extra = row[:5]
        event_id = row[5] if len(row) > 5 else ''
        client_msg_id = row[6] if len(row) > 6 else ''
        events.append({
            'role': role,
            'text': text or '',
            'subtitle': subtitle or '',
            'kind': kind or 'text',
            'extra': extra or '',
            'event_id': event_id or client_msg_id or '',
            'client_msg_id': client_msg_id or '',
            'reply_to_event_id': row[7] if len(row) > 7 else '',
        })
    events = collapse_assistant_logical_turns(
        events, turn_facts=assistant_turn_facts(user_id, chat_id))[-limit:]
    out = []
    for event in events:
        role = event.get('role')
        prompt_role = 'user' if role == 'user' else 'assistant'
        extra = event.get('extra')
        if isinstance(extra, dict):
            extra_data = extra
        else:
            extra_data = _parse_extra(extra or '')
        extra_data = dict(extra_data)
        if role == 'user':
            top_ref = event.get('reply_to_event_id') or ''
            extra_ref = extra_data.get('reply_to_event_id') or ''
            extra_data.pop('reply_to', None)
            extra_data.pop('replyTo', None)
            if top_ref and extra_ref and top_ref != extra_ref:
                extra_data['reply_unavailable'] = True
            elif top_ref or extra_ref:
                target = resolve_reply_reference(
                    user_id, chat_id, top_ref or extra_ref)
                if target:
                    extra_data['verified_reply_to'] = target
                else:
                    extra_data['reply_unavailable'] = True
        content = _history_content(
            role, event.get('text') or '', event.get('subtitle') or '',
            event.get('kind') or 'text', extra_data)
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
    """软删除一条 canonical Raw Event。

    只动 chat_log / chat_log_tombstone：status=deleted + 墓碑。
    不物理删除行，也不在这里改 relationship scoring / cognitive trigger。
    派生记忆失效由 raw_events.invalidate_memories_for_deleted_event 处理。
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
                '''UPDATE chat_log
                   SET status='deleted', deleted_at=CURRENT_TIMESTAMP
                   WHERE id=%s
                     AND user_id=%s
                     AND chat_id=%s
                     AND COALESCE(status, 'active') = 'active' ''',
                (server_id, user_id, chat_id))
        else:
            cur.execute(
                '''UPDATE chat_log
                   SET status='deleted', deleted_at=CURRENT_TIMESTAMP
                   WHERE user_id=%s
                     AND chat_id=%s
                     AND client_msg_id=%s
                     AND COALESCE(status, 'active') = 'active' ''',
                (user_id, chat_id, client_msg_id))
        n = cur.rowcount
        conn.commit()
        return n
    finally:
        cur.close()
        conn.close()


def clear_chat(user_id, chat_id):
    """清空某个聊天：软删除全部 active 行，并给已有 client_msg_id 打墓碑。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO chat_log_tombstone (user_id, chat_id, client_msg_id)
           SELECT user_id, chat_id, client_msg_id
           FROM chat_log
           WHERE user_id=%s AND chat_id=%s
             AND client_msg_id IS NOT NULL AND client_msg_id <> ''
             AND client_msg_id NOT LIKE 'srv_%%'
           ON CONFLICT DO NOTHING''',
        (user_id, chat_id))
    cur.execute(
        '''UPDATE chat_log
           SET status='deleted', deleted_at=CURRENT_TIMESTAMP
           WHERE user_id=%s AND chat_id=%s
             AND COALESCE(status, 'active') = 'active' ''',
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
    cur.execute(
        '''SELECT COUNT(*) FROM chat_log
           WHERE user_id=%s AND chat_id=%s
             AND COALESCE(status, 'active') = 'active' ''',
                (user_id, chat_id))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    hidden = hidden_aggregate_event_ids(user_id, chat_id)
    return max(0, int(n or 0) - len(hidden))
