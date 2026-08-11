"""db_schedule.py —— 角色自己的一天(日程表)

设计目的:
  让角色有自己的生活节奏 —— 他不是 24 小时待命的聊天机器人,
  上课/出任务/洗澡的时候是真的走不开,消息只会显示已读,忙完才回。

和用户自己的 tasks 表完全无关:
  tasks        —— 【用户】的待办,用户自己排
  char_schedule —— 【角色】的行程,LLM 每天按角色背景自动生成

关键字段 can_reply:
  由 LLM 生成日程时逐条判断,不是按时间一刀切。
    上课/出任务/洗澡/开会 → false(走不开,只已读)
    探店/逛街/查账/吃饭/发呆 → true(能摸鱼回消息)
"""
from datetime import datetime, date as _date
from db import get_conn


def init_schedule_table():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS char_schedule (
        id SERIAL PRIMARY KEY,
        character_id TEXT NOT NULL,
        user_id TEXT NOT NULL,
        sched_date DATE NOT NULL,
        start_time TEXT NOT NULL,        -- 'HH:MM'
        end_time TEXT NOT NULL,          -- 'HH:MM'
        title TEXT NOT NULL,             -- 做什么
        location TEXT DEFAULT '',        -- 在哪
        note TEXT DEFAULT '',            -- 角色口吻的一句碎碎念
        can_reply BOOLEAN DEFAULT TRUE,  -- 这段时间能不能回消息
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_sched_lookup
                   ON char_schedule (character_id, user_id, sched_date, start_time)''')
    # 同一天同一个开始时间只留一条,重复生成不会翻倍
    cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_uniq
                   ON char_schedule (character_id, user_id, sched_date, start_time)''')
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 角色日程表已就绪：char_schedule')


def save_schedule(character_id, user_id, sched_date, items):
    """写入一天的日程。items = [{start_time,end_time,title,location,note,can_reply}]
    同一天重复调用会先清空再写,避免混杂。返回写入条数。"""
    if not items:
        return 0
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            'DELETE FROM char_schedule WHERE character_id=%s AND user_id=%s AND sched_date=%s',
            (character_id, user_id, sched_date))
        n = 0
        for it in items:
            st = (it.get('start_time') or '').strip()
            et = (it.get('end_time') or '').strip()
            title = (it.get('title') or '').strip()
            if not st or not et or not title:
                continue
            cur.execute(
                '''INSERT INTO char_schedule
                     (character_id, user_id, sched_date, start_time, end_time,
                      title, location, note, can_reply)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING''',
                (character_id, user_id, sched_date, st, et, title[:80],
                 (it.get('location') or '')[:40],
                 (it.get('note') or '')[:120],
                 bool(it.get('can_reply', True)))
            )
            n += cur.rowcount
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print(f'[schedule] {character_id} {sched_date} 写入 {n} 条日程')
    return n


def get_schedule(character_id, user_id, sched_date):
    """取某天的完整日程,按开始时间排序。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, start_time, end_time, title, location, note, can_reply
           FROM char_schedule
           WHERE character_id=%s AND user_id=%s AND sched_date=%s
           ORDER BY start_time ASC''',
        (character_id, user_id, sched_date))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{
        'id': r[0], 'start_time': r[1], 'end_time': r[2],
        'title': r[3], 'location': r[4] or '', 'note': r[5] or '',
        'can_reply': bool(r[6]),
    } for r in rows]


def get_current_activity(character_id, user_id, now: datetime):
    """★ 核心:现在这一刻角色在干什么。返回 dict 或 None(没安排=空闲)。

    结果里带 can_reply,route_chat 靠它决定是正常回复还是只已读。
    """
    hhmm = now.strftime('%H:%M')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, start_time, end_time, title, location, note, can_reply
           FROM char_schedule
           WHERE character_id=%s AND user_id=%s AND sched_date=%s
             AND start_time <= %s AND end_time > %s
           ORDER BY start_time DESC LIMIT 1''',
        (character_id, user_id, now.date(), hhmm, hhmm))
    r = cur.fetchone()
    cur.close()
    conn.close()
    if not r:
        return None
    return {
        'id': r[0], 'start_time': r[1], 'end_time': r[2],
        'title': r[3], 'location': r[4] or '', 'note': r[5] or '',
        'can_reply': bool(r[6]),
    }


def get_next_free_time(character_id, user_id, now: datetime):
    """忙完之后最早什么时候有空。返回 'HH:MM' 或 None(今天剩下都忙/没安排)。

    逻辑:从当前时刻往后找,第一个 can_reply=true 的时段开始时间;
    如果后面全是忙的,就返回最后一个忙碌时段的结束时间。
    """
    hhmm = now.strftime('%H:%M')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT start_time, end_time, can_reply FROM char_schedule
           WHERE character_id=%s AND user_id=%s AND sched_date=%s
             AND end_time > %s
           ORDER BY start_time ASC''',
        (character_id, user_id, now.date(), hhmm))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return None
    for st, et, can_reply in rows:
        if can_reply:
            # 已经在这个时段里(理论上不该发生)就用现在,否则用它的开始时间
            return max(st, hhmm) if st <= hhmm else st
    # 后面全忙 → 最后一段结束时
    return rows[-1][1]


def has_schedule(character_id, user_id, sched_date) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT 1 FROM char_schedule WHERE character_id=%s AND user_id=%s AND sched_date=%s LIMIT 1',
        (character_id, user_id, sched_date))
    ok = cur.fetchone() is not None
    cur.close()
    conn.close()
    return ok


def clear_schedule(character_id, user_id, sched_date):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'DELETE FROM char_schedule WHERE character_id=%s AND user_id=%s AND sched_date=%s',
        (character_id, user_id, sched_date))
    n = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return n
