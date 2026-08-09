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
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 聊天记录表已就绪：chat_log')


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
                     (m.get('client_msg_id') or '')[:120], role,
                     (m.get('text') or '')[:4000],
                     (m.get('subtitle') or '')[:4000],
                     (m.get('emotion') or '')[:20],
                     (m.get('kind') or 'text')[:20],
                     (m.get('extra') or '')[:2000],
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
                     (m.get('client_msg_id') or '')[:120], role,
                     (m.get('text') or '')[:4000],
                     (m.get('subtitle') or '')[:4000],
                     (m.get('emotion') or '')[:20],
                     (m.get('kind') or 'text')[:20],
                     (m.get('extra') or '')[:2000],
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