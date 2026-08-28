"""主动消息约定表 (proactive_promise)

背景:
之前的 proactive_scheduler 硬编码"每天 00:50 发任务汇报"—— 陌生用户也会收到,
不符合"关系深浅决定主动性"的原则。

改造后:
- 主动消息 ≠ 硬编码定时行为,而是【承诺驱动】
- 用户跟 gojo 聊到"某某时间你替我记着 / 你到时候说一句"时,
  chat prompt 检测出来,在这张表里存一条 promise
- Scheduler 定时醒来查表:哪些 promise 该触发但没触发过 → 生成
- 没 promise → scheduler 静默跳过,不再打扰陌生用户

支持两种触发方式:
- once: 一次性,triggered_at 是绝对时刻,触发后 is_fired=true
- daily: 每天,trigger_time 存 'HH:MM',每天到点触发一次

以后可扩展:weekly / monthly / 具体事件驱动等。
"""
from db import get_conn


def init_promise_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS proactive_promise (
        id SERIAL PRIMARY KEY,
        character_id TEXT NOT NULL DEFAULT 'gojo',
        user_id TEXT NOT NULL DEFAULT 'default',
        trigger_kind TEXT NOT NULL,             -- 'once' | 'daily'
        trigger_at TIMESTAMP,                    -- once 用:绝对时刻
        trigger_time TEXT,                       -- daily 用:'HH:MM'
        context TEXT NOT NULL,                   -- 给 LLM 生成时的 hint
        origin_text TEXT DEFAULT '',             -- 用户当时说的原话(参考)
        is_fired BOOLEAN DEFAULT FALSE,         -- once 触发过 = true
        last_fired_at TIMESTAMP,                 -- 循环: 上次触发的时间
        is_active BOOLEAN DEFAULT TRUE,          -- 用户撤销 = false
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_promise_user_active ON proactive_promise(user_id, character_id, is_active)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_promise_due ON proactive_promise(is_active, is_fired, trigger_at)')
    conn.commit()
    cur.close()
    conn.close()
    print('[promise] 约定表已就绪')


def add_promise(character_id, user_id, trigger_kind, context,
                trigger_at=None, trigger_time=None, origin_text=''):
    """新增一条 promise。
    - once: 必须传 trigger_at(datetime),trigger_time 为 None
    - daily: 必须传 trigger_time('HH:MM' 字符串),trigger_at 为 None
    """
    assert trigger_kind in ('once', 'daily'), f'不支持的 trigger_kind: {trigger_kind}'
    if trigger_kind == 'once' and not trigger_at:
        raise ValueError('once 类型必须传 trigger_at')
    if trigger_kind == 'daily' and not trigger_time:
        raise ValueError('daily 类型必须传 trigger_time (HH:MM)')

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO proactive_promise
        (character_id, user_id, trigger_kind, trigger_at, trigger_time, context, origin_text)
        VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
        (character_id, user_id, trigger_kind, trigger_at, trigger_time, context, origin_text)
    )
    pid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return pid


def get_due_promises(now):
    """查出所有【该触发但还没触发】的活跃 promise。

    - once: is_fired=false AND trigger_at <= now
    - daily: 今天还没触发过(last_fired_at 不是今天) AND HH:MM 时刻已经过

    返回: [{id, character_id, user_id, trigger_kind, context, ...}, ...]
    """
    conn = get_conn()
    cur = conn.cursor()

    # 一次性:未触发 + 到点
    cur.execute('''SELECT id, character_id, user_id, trigger_kind, context, origin_text
                   FROM proactive_promise
                   WHERE is_active = TRUE AND trigger_kind = 'once'
                     AND is_fired = FALSE AND trigger_at <= %s
                   ORDER BY trigger_at ASC''',
                (now,))
    once_rows = cur.fetchall()

    # 每天:今天未触发 + 时刻已经过
    #   条件:last_fired_at 是 NULL 或不是今天,并且 HH:MM 已经过了
    hh_mm = now.strftime('%H:%M')
    today_date = now.date()
    cur.execute('''SELECT id, character_id, user_id, trigger_kind, context, origin_text, trigger_time
                   FROM proactive_promise
                   WHERE is_active = TRUE AND trigger_kind = 'daily'
                     AND trigger_time <= %s
                     AND (last_fired_at IS NULL OR DATE(last_fired_at) < %s)
                   ORDER BY trigger_time ASC''',
                (hh_mm, today_date))
    daily_rows = cur.fetchall()

    cur.close()
    conn.close()

    result = []
    for r in once_rows:
        result.append({
            'id': r[0], 'character_id': r[1], 'user_id': r[2],
            'trigger_kind': r[3], 'context': r[4], 'origin_text': r[5],
        })
    for r in daily_rows:
        result.append({
            'id': r[0], 'character_id': r[1], 'user_id': r[2],
            'trigger_kind': r[3], 'context': r[4], 'origin_text': r[5],
            'trigger_time': r[6],
        })
    return result


def mark_fired(promise_id, fired_at):
    """标记 promise 已触发。once 设 is_fired=true;daily 更新 last_fired_at。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT trigger_kind FROM proactive_promise WHERE id = %s', (promise_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return
    kind = row[0]
    if kind == 'once':
        cur.execute('UPDATE proactive_promise SET is_fired = TRUE, last_fired_at = %s WHERE id = %s',
                    (fired_at, promise_id))
    else:  # daily
        cur.execute('UPDATE proactive_promise SET last_fired_at = %s WHERE id = %s',
                    (fired_at, promise_id))
    conn.commit()
    cur.close()
    conn.close()


def list_active_promises(character_id, user_id, limit=20):
    """列出用户跟某角色之间的活跃约定(给 LLM 或前端展示用)。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT id, trigger_kind, trigger_at, trigger_time, context, origin_text, created_at
                   FROM proactive_promise
                   WHERE character_id = %s AND user_id = %s AND is_active = TRUE
                   ORDER BY created_at DESC LIMIT %s''',
                (character_id, user_id, limit))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0], 'trigger_kind': r[1],
        'trigger_at': str(r[2]) if r[2] else None,
        'trigger_time': r[3], 'context': r[4], 'origin_text': r[5],
        'created_at': str(r[6]) if r[6] else None,
    } for r in rows]


def deactivate(promise_id):
    """撤销一个 promise(用户手动或 gojo 判断要撤)。不删除,只置 is_active=false。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE proactive_promise SET is_active = FALSE WHERE id = %s', (promise_id,))
    conn.commit()
    cur.close()
    conn.close()

# db_promise.py 补丁 — 在文件末尾加这个函数

def get_active_promises(character_id, user_id):
    """拿所有活跃的承诺(promise_detector 防重复用)。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, trigger_kind, trigger_at, trigger_time, context, origin_text
           FROM proactive_promise
           WHERE character_id=%s AND user_id=%s AND is_active=TRUE
           ORDER BY created_at DESC LIMIT 20''',
        (character_id, user_id)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0], 'trigger_kind': r[1],
        'trigger_at': r[2], 'trigger_time': r[3],
        'context': r[4] or '', 'origin_text': r[5] or '',
    } for r in rows]