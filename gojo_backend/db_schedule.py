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
import json
import random
from db import get_conn


REPLY_FREE = 'free'
REPLY_SOFT_BUSY = 'soft_busy'
REPLY_HARD_BUSY = 'hard_busy'
REPLY_STATES = (REPLY_FREE, REPLY_SOFT_BUSY, REPLY_HARD_BUSY)
# soft_busy 第一次 / 每次 defer 后，随机排下一次看手机时间（分钟）
SOFT_BUSY_CHECK_MIN_MINUTES = 8
SOFT_BUSY_CHECK_MAX_MINUTES = 28
SOFT_BUSY_REPLY_CHANCE = 0.38


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
        reply_state TEXT DEFAULT 'free', -- free / soft_busy / hard_busy
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS char_phone_check (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        schedule_id INTEGER,
        sched_date DATE NOT NULL,
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        activity_title TEXT DEFAULT '',
        reply_state TEXT NOT NULL DEFAULT 'soft_busy',
        seen BOOLEAN NOT NULL DEFAULT FALSE,
        can_reply BOOLEAN NOT NULL DEFAULT FALSE,
        pending_count INTEGER NOT NULL DEFAULT 0,
        first_source_event_id TEXT DEFAULT '',
        last_source_event_id TEXT DEFAULT '',
        pending_text TEXT DEFAULT '',
        event_meta TEXT DEFAULT '',
        fallback_promise_id INTEGER,
        next_phone_check_at TIMESTAMPTZ,
        seen_at TIMESTAMPTZ,
        seen_watermark INTEGER NOT NULL DEFAULT 0,
        resolved_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (user_id, character_id, sched_date, start_time, end_time)
    )''')
    try:
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS reply_state TEXT DEFAULT \'free\'')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS event_meta TEXT DEFAULT \'\'')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS fallback_promise_id INTEGER')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS next_phone_check_at TIMESTAMPTZ')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS seen_at TIMESTAMPTZ')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS seen_watermark INTEGER DEFAULT 0')
    except Exception as e:
        conn.rollback()
        print(f'[init] 日程扩展字段迁移跳过：{e}')
        cur = conn.cursor()
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_sched_lookup
                   ON char_schedule (character_id, user_id, sched_date, start_time)''')
    # 同一天同一个开始时间只留一条,重复生成不会翻倍
    cur.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_sched_uniq
                   ON char_schedule (character_id, user_id, sched_date, start_time)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_phone_check_pending
                   ON char_phone_check (user_id, character_id, resolved_at, sched_date)''')
    conn.commit()
    cur.close()
    conn.close()
    print('[init] 角色日程表已就绪：char_schedule / char_phone_check')


def normalize_reply_state(value=None, can_reply=True):
    state = (value or '').strip()
    if state in REPLY_STATES:
        return state
    return REPLY_FREE if bool(can_reply) else REPLY_HARD_BUSY


def can_reply_from_state(reply_state):
    return normalize_reply_state(reply_state) == REPLY_FREE


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
            reply_state = normalize_reply_state(
                it.get('reply_state'), it.get('can_reply', True))
            cur.execute(
                '''INSERT INTO char_schedule
                     (character_id, user_id, sched_date, start_time, end_time,
                      title, location, note, can_reply, reply_state)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING''',
                (character_id, user_id, sched_date, st, et, title[:80],
                 (it.get('location') or '')[:40],
                 (it.get('note') or '')[:120],
                 can_reply_from_state(reply_state),
                 reply_state)
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
        '''SELECT id, start_time, end_time, title, location, note,
                  can_reply, COALESCE(reply_state, '')
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
        'reply_state': normalize_reply_state(r[7], r[6]),
        'can_reply': can_reply_from_state(normalize_reply_state(r[7], r[6])),
    } for r in rows]


def get_current_activity(character_id, user_id, now: datetime):
    """★ 核心:现在这一刻角色在干什么。返回 dict 或 None(没安排=空闲)。

    结果里带 can_reply,route_chat 靠它决定是正常回复还是只已读。
    """
    hhmm = now.strftime('%H:%M')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, start_time, end_time, title, location, note,
                  can_reply, COALESCE(reply_state, '')
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
        'reply_state': normalize_reply_state(r[7], r[6]),
        'can_reply': can_reply_from_state(normalize_reply_state(r[7], r[6])),
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
        '''SELECT start_time, end_time, can_reply, COALESCE(reply_state, '')
           FROM char_schedule
           WHERE character_id=%s AND user_id=%s AND sched_date=%s
             AND end_time > %s
           ORDER BY start_time ASC''',
        (character_id, user_id, now.date(), hhmm))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return None
    for st, et, can_reply, reply_state in rows:
        if can_reply_from_state(normalize_reply_state(reply_state, can_reply)):
            # 已经在这个时段里(理论上不该发生)就用现在,否则用它的开始时间
            return max(st, hhmm) if st <= hhmm else st
    # 后面全忙 → 最后一段结束时
    return rows[-1][1]


def _hhmm_to_dt(now: datetime, hhmm: str):
    hh, mm = hhmm.split(':')
    return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def _find_hard_busy_covering(character_id, user_id, when: datetime):
    """若 when 落在某段 hard_busy 内，返回那段活动，否则 None。"""
    hhmm = when.strftime('%H:%M')
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, start_time, end_time, title, location, note,
                      can_reply, COALESCE(reply_state, '')
               FROM char_schedule
               WHERE character_id=%s AND user_id=%s AND sched_date=%s
                 AND start_time <= %s AND end_time > %s
               ORDER BY start_time DESC''',
            (character_id, user_id, when.date(), hhmm, hhmm))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()
    for r in rows:
        state = normalize_reply_state(r[7], r[6])
        if state == REPLY_HARD_BUSY:
            return {
                'id': r[0], 'start_time': r[1], 'end_time': r[2],
                'title': r[3], 'location': r[4] or '', 'note': r[5] or '',
                'reply_state': state, 'can_reply': False,
            }
    return None


def sample_next_phone_check_at(now: datetime, activity, after=None):
    """为 soft_busy activity 抽下一次看手机时间。

    after: 若提供，则在 after 之后再抽（用于 defer 后的第二次 check）。
    """
    from datetime import timedelta
    base = after or now
    if base.tzinfo is None and getattr(now, 'tzinfo', None) is not None:
        base = base.replace(tzinfo=now.tzinfo)
    lo = SOFT_BUSY_CHECK_MIN_MINUTES
    hi = SOFT_BUSY_CHECK_MAX_MINUTES
    delay = random.randint(lo, hi)
    candidate = base + timedelta(minutes=delay)
    end_at = _hhmm_to_dt(now, activity['end_time'])
    if candidate >= end_at:
        # 抽到活动结束后：尽量落在结束前 1 分钟；若已经来不及就用 end_at
        candidate = max(base + timedelta(minutes=1), end_at - timedelta(minutes=1))
        if candidate <= base:
            candidate = end_at
    return candidate


def postpone_past_hard_busy(character_id, user_id, check_at: datetime):
    """若 check_at 落在 hard_busy 窗口，延期到该窗口结束之后。"""
    from datetime import timedelta
    hard = _find_hard_busy_covering(character_id, user_id, check_at)
    if not hard:
        return check_at, False
    end_at = _hhmm_to_dt(check_at, hard['end_time'])
    return end_at + timedelta(minutes=1), True


def decide_phone_check(character_id, user_id, now: datetime, activity,
                       source_event_id='', pending_text='', event_meta=None):
    """返回本条消息在当前日程下的 seen / can_reply 判定。

    soft_busy:
      · activity 共享一条 char_phone_check + 一个 next_phone_check_at
      · 新消息只进 pending inbox，不重抽 / 不推进 next_phone_check_at
      · 到点才写 seen_at，再决定 reply_now；defer 则生成下一次 check
      · hard_busy 窗口盖住 check 时，延期到 hard window 之后
    hard_busy: 只积压，不看手机、不回复。
    """
    if not activity:
        return {
            'reply_state': REPLY_FREE,
            'seen': True,
            'can_reply': True,
            'activity': None,
            'opportunity_id': None,
            'reused': False,
            'next_phone_check_at': None,
            'seen_at': None,
        }

    reply_state = normalize_reply_state(
        activity.get('reply_state'), activity.get('can_reply', True))
    if reply_state == REPLY_FREE:
        return {
            'reply_state': REPLY_FREE,
            'seen': True,
            'can_reply': True,
            'activity': activity,
            'opportunity_id': None,
            'reused': False,
            'next_phone_check_at': None,
            'seen_at': None,
        }

    source_event_id = (source_event_id or '')[:120]
    pending_text = (pending_text or '')[:800]
    event_meta_text = ''
    if event_meta:
        try:
            event_meta_text = json.dumps(event_meta, ensure_ascii=False)[:2000]
        except Exception:
            event_meta_text = ''

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT id, seen, can_reply, pending_count, pending_text, event_meta,
                      fallback_promise_id, next_phone_check_at, seen_at, resolved_at,
                      COALESCE(seen_watermark, 0)
               FROM char_phone_check
               WHERE user_id=%s
                 AND character_id=%s
                 AND sched_date=%s
                 AND start_time=%s
                 AND end_time=%s''',
            (user_id, character_id, now.date(),
             activity['start_time'], activity['end_time']))
        row = cur.fetchone()
        reused = False
        watermark = 0
        count = 1
        if row:
            (oid, _seen, prev_can_reply, count, existing_text, existing_meta,
             fallback_id, next_check_at, seen_at, resolved_at, watermark) = row
            watermark = watermark or 0
            # 上一轮已经 reply_now / resolve 过：同一 activity 开新一轮 check，不沿用 can_reply
            if resolved_at or prev_can_reply:
                next_check_at = None
                watermark = 0
                count = 1
                if reply_state == REPLY_SOFT_BUSY:
                    next_check_at = sample_next_phone_check_at(now, activity)
                    next_check_at, _ = postpone_past_hard_busy(
                        character_id, user_id, next_check_at)
                cur.execute(
                    '''UPDATE char_phone_check
                       SET seen=FALSE,
                           can_reply=FALSE,
                           seen_at=NULL,
                           seen_watermark=0,
                           resolved_at=NULL,
                           pending_count=1,
                           first_source_event_id=%s,
                           last_source_event_id=%s,
                           pending_text=%s,
                           event_meta=%s,
                           fallback_promise_id=NULL,
                           next_phone_check_at=%s,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s''',
                    (source_event_id, source_event_id, pending_text,
                     event_meta_text, next_check_at, oid))
                fallback_id = None
                seen_at = None
                reused = True
            else:
                merged_text = (existing_text or '')
                if pending_text:
                    prefix = '\n' if merged_text else ''
                    merged_text = (merged_text + prefix + pending_text)[:4000]
                merged_meta = (existing_meta or '')
                if event_meta_text:
                    prefix = '\n' if merged_meta else ''
                    merged_meta = (merged_meta + prefix + event_meta_text)[:6000]
                count = (count or 0) + 1
                # 新消息只更新 inbox，绝不改 next_phone_check_at / seen_watermark
                cur.execute(
                    '''UPDATE char_phone_check
                       SET pending_count=%s,
                           last_source_event_id=%s,
                           pending_text=%s,
                           event_meta=%s,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s''',
                    (count, source_event_id, merged_text, merged_meta, oid))
                reused = True
        else:
            fallback_id = None
            seen_at = None
            next_check_at = None
            watermark = 0
            count = 1
            if reply_state == REPLY_SOFT_BUSY:
                next_check_at = sample_next_phone_check_at(now, activity)
                next_check_at, _ = postpone_past_hard_busy(
                    character_id, user_id, next_check_at)
            cur.execute(
                '''INSERT INTO char_phone_check
                   (user_id, character_id, schedule_id, sched_date, start_time, end_time,
                    activity_title, reply_state, seen, can_reply, pending_count,
                    first_source_event_id, last_source_event_id, pending_text, event_meta,
                    next_phone_check_at, seen_watermark)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id''',
                (user_id, character_id, activity.get('id'), now.date(),
                 activity['start_time'], activity['end_time'],
                 activity.get('title', ''), reply_state, False, False, 1,
                 source_event_id, source_event_id, pending_text, event_meta_text,
                 next_check_at, 0))
            oid = cur.fetchone()[0]

        def _decision(seen, can_reply, extra=None):
            this_seen = bool(seen)
            out = {
                'reply_state': reply_state,
                'seen': this_seen,
                'can_reply': bool(can_reply),
                'activity': activity,
                'opportunity_id': oid,
                'fallback_promise_id': fallback_id,
                'reused': reused,
                'next_phone_check_at': next_check_at,
                'seen_at': seen_at if this_seen else None,
                'seen_watermark': watermark,
                'pending_count': count,
            }
            if extra:
                out.update(extra)
            return out

        # hard_busy: 只积压，不看手机
        if reply_state == REPLY_HARD_BUSY:
            conn.commit()
            return _decision(False, False)

        # soft_busy: 未到 check 时间 → 本条未读未回（不继承旧 bundle.seen）
        if not next_check_at or now < next_check_at:
            conn.commit()
            return _decision(count <= watermark, False)

        # 到点：若被 hard_busy 盖住，延期，不算 seen
        postponed, was_postponed = postpone_past_hard_busy(
            character_id, user_id, now)
        if was_postponed and postponed > now:
            next_check_at = postponed
            cur.execute(
                '''UPDATE char_phone_check
                   SET next_phone_check_at=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s''',
                (postponed, oid))
            conn.commit()
            return _decision(count <= watermark, False,
                             {'postponed_for_hard_busy': True})

        # 真正消费一次 phone check：先把当前 inbox 全部标 seen，再决定是否回复
        seen_at = now
        watermark = count
        reply_now = random.random() < SOFT_BUSY_REPLY_CHANCE
        if reply_now:
            new_next = None
            can_reply = True
            cur.execute(
                '''UPDATE char_phone_check
                   SET seen=TRUE,
                       seen_at=%s,
                       can_reply=TRUE,
                       next_phone_check_at=NULL,
                       resolved_at=%s,
                       seen_watermark=%s,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s''',
                (seen_at, now, watermark, oid))
        else:
            new_next = sample_next_phone_check_at(now, activity, after=now)
            new_next, _ = postpone_past_hard_busy(
                character_id, user_id, new_next)
            can_reply = False
            cur.execute(
                '''UPDATE char_phone_check
                   SET seen=TRUE,
                       seen_at=%s,
                       can_reply=FALSE,
                       next_phone_check_at=%s,
                       seen_watermark=%s,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s''',
                (seen_at, new_next, watermark, oid))
        next_check_at = new_next
        conn.commit()
        return _decision(True, can_reply, {'check_consumed': True})
    finally:
        cur.close()
        conn.close()


def merge_phone_check_event_meta(opportunity_id, event_meta):
    """busy 图片先落 inbox，Vision 摘要随后补进 event_meta。"""
    if not opportunity_id or not event_meta:
        return
    try:
        extra = json.dumps(event_meta, ensure_ascii=False)[:2000]
    except Exception:
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE char_phone_check
               SET event_meta = LEFT(
                     CASE
                       WHEN event_meta IS NULL OR event_meta = '' THEN %s
                       ELSE event_meta || E'\n' || %s
                     END, 6000),
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=%s''',
            (extra, extra, opportunity_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def attach_fallback_promise(opportunity_id, promise_id):
    if not opportunity_id or not promise_id:
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE char_phone_check
               SET fallback_promise_id=%s, updated_at=CURRENT_TIMESTAMP
               WHERE id=%s AND fallback_promise_id IS NULL''',
            (promise_id, opportunity_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def resolve_phone_check(opportunity_id):
    if not opportunity_id:
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE char_phone_check
               SET resolved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
               WHERE id=%s AND resolved_at IS NULL''',
            (opportunity_id,))
        conn.commit()
    finally:
        cur.close()
        conn.close()


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
