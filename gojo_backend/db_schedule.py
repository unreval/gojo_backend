"""db_schedule.py —— 角色自己的一天(日程表)

设计目的:
  让角色有自己的生活节奏 —— 他不是 24 小时待命的聊天机器人。
  上课/出任务/洗澡/驾驶是 hard_busy：没法看手机。
  开会/备课/处理报告是 soft_busy：可能瞄一眼。
  探店/逛街/吃饭/发呆是 free：能正常回。

和用户自己的 tasks 表完全无关:
  tasks        —— 【用户】的待办,用户自己排
  char_schedule —— 【角色】的行程,LLM 每天按角色背景自动生成

关键字段 can_reply:
  由 LLM 生成日程时逐条判断,不是按时间一刀切。
    上课/出任务/洗澡/驾驶 → hard_busy(没法看手机)
    开会/备课/处理报告 → soft_busy(可能瞄一眼)
    探店/逛街/查账/吃饭/发呆 → free(能摸鱼回消息)
"""
from datetime import datetime, date as _date, timedelta
import json
import os
import random
import threading
import uuid
from db import get_conn


REPLY_FREE = 'free'
REPLY_SOFT_BUSY = 'soft_busy'
REPLY_HARD_BUSY = 'hard_busy'
REPLY_STATES = (REPLY_FREE, REPLY_SOFT_BUSY, REPLY_HARD_BUSY)
# soft_busy 第一次 / 每次 defer 后，随机排下一次看手机时间（分钟）
SOFT_BUSY_CHECK_MIN_MINUTES = 8
SOFT_BUSY_CHECK_MAX_MINUTES = 28
SOFT_BUSY_REPLY_CHANCE = 0.38

PHONE_CHECK_PENDING = 'pending'
PHONE_CHECK_PROCESSING = 'processing'
PHONE_CHECK_CONSUMED = 'consumed'
PHONE_CHECK_DEFERRED = 'deferred'
PHONE_CHECK_RESOLVED = 'resolved'
PHONE_CHECK_EXPIRED = 'expired'
PHONE_CHECK_SUPERSEDED = 'superseded'
PHONE_CHECK_STATES = (
    PHONE_CHECK_PENDING, PHONE_CHECK_PROCESSING, PHONE_CHECK_CONSUMED,
    PHONE_CHECK_DEFERRED, PHONE_CHECK_RESOLVED, PHONE_CHECK_EXPIRED,
    PHONE_CHECK_SUPERSEDED,
)
PHONE_CHECK_TERMINAL = (
    PHONE_CHECK_CONSUMED, PHONE_CHECK_RESOLVED,
    PHONE_CHECK_EXPIRED, PHONE_CHECK_SUPERSEDED,
)
PHONE_CHECK_CLAIMABLE = (PHONE_CHECK_PENDING, PHONE_CHECK_DEFERRED)
CLAIM_TTL_SECONDS = 180


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
        cur.execute("ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS check_state TEXT NOT NULL DEFAULT 'pending'")
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS claim_token TEXT')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS claim_owner TEXT')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ')
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
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_phone_check_claim
                   ON char_phone_check (check_state, next_phone_check_at, claim_expires_at)''')
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
    if not r:
        try:
            supersede_ended_phone_checks(cur, user_id, character_id, now, None)
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
        cur.close()
        conn.close()
        return None
    cur.close()
    conn.close()
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
    Interval comes from ActivityPhoneProfile, not a single global window.
    """
    from datetime import timedelta
    base = after or now
    if base.tzinfo is None and getattr(now, 'tzinfo', None) is not None:
        base = base.replace(tzinfo=now.tzinfo)
    lo = SOFT_BUSY_CHECK_MIN_MINUTES
    hi = SOFT_BUSY_CHECK_MAX_MINUTES
    try:
        from activity_phone import profile_for_activity
        profile = profile_for_activity(
            activity, (activity or {}).get('character_id'))
        if profile and profile.busy_state == REPLY_SOFT_BUSY:
            lo = max(1, int(profile.check_interval_min or lo))
            hi = max(lo, int(profile.check_interval_max or hi))
    except Exception:
        pass
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
    try:
        hard = _find_hard_busy_covering(character_id, user_id, check_at)
    except Exception:
        return check_at, False
    if not hard:
        return check_at, False
    end_at = _hhmm_to_dt(check_at, hard['end_time'])
    return end_at + timedelta(minutes=1), True


def _claim_owner():
    return f'pid:{os.getpid()}:tid:{threading.get_ident()}'


def recover_stale_phone_checks(cur, now):
    """processing past TTL → pending. Never revive consumed/resolved."""
    cur.execute(
        '''UPDATE char_phone_check SET -- phone_check:recover
               check_state = 'pending',
               claimed_at = NULL,
               claim_token = NULL,
               claim_owner = NULL,
               claim_expires_at = NULL,
               updated_at = CURRENT_TIMESTAMP
           WHERE check_state = 'processing'
             AND claim_expires_at IS NOT NULL
             AND claim_expires_at <= %s
             AND resolved_at IS NULL''',
        (now,))
    return cur.rowcount


def supersede_ended_phone_checks(cur, user_id, character_id, now, activity=None):
    """Ended windows with no pending inbox can be closed.

    Windows that still have pending messages are armed for delayed reply
    instead of resolved, so busy fallback never drops unread content.
    """
    hhmm = now.strftime('%H:%M')
    today = now.date()
    keep = ''
    keep_params = ()
    if activity:
        keep = 'AND NOT (sched_date=%s AND start_time=%s AND end_time=%s)'
        keep_params = (today, activity.get('start_time'), activity.get('end_time'))
    ended = '(sched_date < %s OR (sched_date=%s AND end_time <= %s))'
    ended_params = (today, today, hhmm)
    cur.execute(
        '''UPDATE char_phone_check SET -- phone_check:arm_ended
               next_phone_check_at = COALESCE(
                   LEAST(next_phone_check_at, %s), %s),
               updated_at = CURRENT_TIMESTAMP
           WHERE user_id=%s AND character_id=%s
             AND resolved_at IS NULL
             AND COALESCE(pending_count, 0) > 0
             AND check_state NOT IN ('consumed','resolved','expired','superseded')
             ''' + keep + '''
             AND ''' + ended,
        (now, now, user_id, character_id) + keep_params + ended_params)
    armed = cur.rowcount
    cur.execute(
        '''UPDATE char_phone_check SET -- phone_check:supersede
               check_state = 'superseded',
               resolved_at = COALESCE(resolved_at, %s),
               claimed_at = NULL,
               claim_token = NULL,
               claim_owner = NULL,
               claim_expires_at = NULL,
               updated_at = CURRENT_TIMESTAMP
           WHERE user_id=%s AND character_id=%s
             AND check_state NOT IN ('consumed','resolved','expired','superseded')
             AND COALESCE(pending_count, 0) <= 0
             ''' + keep + '''
             AND ''' + ended,
        (now, user_id, character_id) + keep_params + ended_params)
    return armed + cur.rowcount


def claim_due_phone_check(cur, oid, now, *, token=None, owner=None):
    """Exactly one consumer can move a due pending/deferred (or stale processing) row."""
    token = token or str(uuid.uuid4())
    owner = owner or _claim_owner()
    expires = now + timedelta(seconds=CLAIM_TTL_SECONDS)
    cur.execute(
        '''UPDATE char_phone_check SET -- phone_check:claim
               check_state = 'processing',
               claimed_at = %s,
               claim_token = %s,
               claim_owner = %s,
               claim_expires_at = %s,
               seen = TRUE,
               seen_at = %s,
               seen_watermark = pending_count,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = %s
             AND resolved_at IS NULL
             AND next_phone_check_at IS NOT NULL
             AND next_phone_check_at <= %s
             AND (
                   check_state IN ('pending', 'deferred')
                   OR (check_state = 'processing' AND claim_expires_at IS NOT NULL
                       AND claim_expires_at <= %s)
                 )
           RETURNING id, pending_count, pending_text, event_meta, reply_state,
                     seen_watermark, next_phone_check_at, fallback_promise_id,
                     user_id, character_id, first_source_event_id,
                     last_source_event_id, activity_title, start_time, end_time,
                     sched_date''',
        (now, token, owner, expires, now, oid, now, now))
    row = cur.fetchone()
    if not row:
        return None
    return {
        'id': row[0],
        'pending_count': row[1],
        'pending_text': row[2],
        'event_meta': row[3],
        'reply_state': row[4],
        'seen_watermark': row[5],
        'next_phone_check_at': row[6],
        'fallback_promise_id': row[7],
        'user_id': row[8],
        'character_id': row[9],
        'first_source_event_id': row[10],
        'last_source_event_id': row[11],
        'activity_title': row[12],
        'start_time': row[13],
        'end_time': row[14],
        'sched_date': row[15],
        'claim_token': token,
        'claim_owner': owner,
        'seen_at': now,
    }


def finish_claimed_reply(cur, oid, token, now):
    cur.execute(
        '''UPDATE char_phone_check SET -- phone_check:finish_reply
               check_state = 'consumed',
               can_reply = TRUE,
               next_phone_check_at = NULL,
               resolved_at = %s,
               claimed_at = NULL,
               claim_token = NULL,
               claim_owner = NULL,
               claim_expires_at = NULL,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = %s
             AND claim_token = %s
             AND check_state = 'processing' ''',
        (now, oid, token))
    return cur.rowcount


def finish_claimed_defer(cur, oid, token, now, new_next):
    cur.execute(
        '''UPDATE char_phone_check SET -- phone_check:finish_defer
               check_state = 'pending',
               can_reply = FALSE,
               next_phone_check_at = %s,
               fallback_promise_id = NULL,
               claimed_at = NULL,
               claim_token = NULL,
               claim_owner = NULL,
               claim_expires_at = NULL,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = %s
             AND claim_token = %s
             AND check_state = 'processing' ''',
        (new_next, oid, token))
    return cur.rowcount


def release_claimed_phone_check(cur, oid, token):
    """Generation failed: keep pending inbox, allow a later claim."""
    cur.execute(
        '''UPDATE char_phone_check SET -- phone_check:release
               check_state = 'pending',
               can_reply = FALSE,
               claimed_at = NULL,
               claim_token = NULL,
               claim_owner = NULL,
               claim_expires_at = NULL,
               updated_at = CURRENT_TIMESTAMP
           WHERE id = %s
             AND claim_token = %s
             AND check_state = 'processing'
             AND resolved_at IS NULL''',
        (oid, token))
    return cur.rowcount


def list_due_phone_check_ids(cur, now, *, limit=20):
    """Rows the delayed-reply worker may claim. Inbox is still the SoT."""
    hhmm = now.strftime('%H:%M')
    today = now.date()
    cur.execute(
        '''SELECT id FROM char_phone_check
           WHERE resolved_at IS NULL
             AND COALESCE(pending_count, 0) > 0
             AND (
                   check_state IN ('pending', 'deferred')
                   OR (check_state = 'processing' AND claim_expires_at IS NOT NULL
                       AND claim_expires_at <= %s)
                 )
             AND (
                   (next_phone_check_at IS NOT NULL AND next_phone_check_at <= %s)
                   OR (sched_date < %s OR (sched_date = %s AND end_time <= %s))
                 )
           ORDER BY next_phone_check_at NULLS LAST, id
           LIMIT %s''',
        (now, now, today, today, hhmm, limit))
    return [row[0] for row in cur.fetchall()]


def evaluate_due_phone_check(oid, now, *, conn=None):
    """Claim a due phone-check and decide reply vs defer. Does not generate.

    reply: claim stays in processing; caller must generate then finish_claimed_reply.
    defer/postpone/skip: claim is already released or never taken.
    Failure to generate must call release_claimed_phone_check.
    """
    owns = conn is None
    if conn is None:
        conn = get_conn()
    cur = conn.cursor()
    try:
        recover_stale_phone_checks(cur, now)
        hhmm = now.strftime('%H:%M')
        today = now.date()
        cur.execute(
            '''UPDATE char_phone_check
               SET next_phone_check_at = COALESCE(next_phone_check_at, %s),
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = %s
                 AND resolved_at IS NULL
                 AND next_phone_check_at IS NULL
                 AND (sched_date < %s OR (sched_date = %s AND end_time <= %s))''',
            (now, oid, today, today, hhmm))
        claimed = claim_due_phone_check(cur, oid, now)
        if not claimed:
            conn.commit()
            return {'action': 'skip', 'claimed': None}

        character_id = claimed['character_id']
        user_id = claimed['user_id']
        postponed, was_postponed = postpone_past_hard_busy(
            character_id, user_id, now)
        if was_postponed and postponed > now:
            finish_claimed_defer(cur, oid, claimed['claim_token'], now, postponed)
            conn.commit()
            return {
                'action': 'postpone',
                'claimed': claimed,
                'next_phone_check_at': postponed,
            }

        activity = {
            'start_time': claimed.get('start_time'),
            'end_time': claimed.get('end_time'),
            'title': claimed.get('activity_title') or '',
            'reply_state': claimed.get('reply_state'),
            'character_id': character_id,
        }
        window_ended = False
        try:
            end_at = _hhmm_to_dt(now, claimed.get('end_time') or '23:59')
            sched_date = claimed.get('sched_date')
            if sched_date is not None and sched_date < now.date():
                window_ended = True
            elif end_at <= now:
                window_ended = True
        except Exception:
            window_ended = False

        current = None
        try:
            hhmm_now = now.strftime('%H:%M')
            cur.execute(
                '''SELECT id, start_time, end_time, title, location, note,
                          can_reply, COALESCE(reply_state, '')
                   FROM char_schedule
                   WHERE character_id=%s AND user_id=%s AND sched_date=%s
                     AND start_time <= %s AND end_time > %s
                   ORDER BY start_time DESC LIMIT 1''',
                (character_id, user_id, now.date(), hhmm_now, hhmm_now))
            current_row = cur.fetchone()
            if current_row:
                current = {
                    'id': current_row[0],
                    'start_time': current_row[1],
                    'end_time': current_row[2],
                    'title': current_row[3],
                    'reply_state': normalize_reply_state(
                        current_row[7], current_row[6]),
                    'can_reply': can_reply_from_state(
                        normalize_reply_state(current_row[7], current_row[6])),
                }
        except Exception:
            current = None
        currently_free = (not current) or (
            normalize_reply_state(
                current.get('reply_state'), current.get('can_reply', True))
            == REPLY_FREE)

        reply_now = True
        if (not window_ended and not currently_free
                and claimed.get('reply_state') == REPLY_SOFT_BUSY):
            reply_chance = SOFT_BUSY_REPLY_CHANCE
            try:
                from activity_phone import profile_for_activity
                profile = profile_for_activity(activity, character_id)
                if profile and profile.busy_state == REPLY_SOFT_BUSY:
                    reply_chance = float(profile.quick_reply_probability)
            except Exception:
                pass
            reply_now = random.random() < reply_chance

        if not reply_now:
            new_next = sample_next_phone_check_at(now, activity, after=now)
            new_next, _ = postpone_past_hard_busy(character_id, user_id, new_next)
            finish_claimed_defer(cur, oid, claimed['claim_token'], now, new_next)
            conn.commit()
            return {
                'action': 'defer',
                'claimed': claimed,
                'next_phone_check_at': new_next,
            }

        conn.commit()
        return {'action': 'reply', 'claimed': claimed}
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        cur.close()
        if owns:
            conn.close()


def complete_delayed_reply(oid, token, now):
    conn = get_conn()
    cur = conn.cursor()
    try:
        n = finish_claimed_reply(cur, oid, token, now)
        conn.commit()
        return n
    finally:
        cur.close()
        conn.close()


def abort_delayed_reply(oid, token):
    conn = get_conn()
    cur = conn.cursor()
    try:
        n = release_claimed_phone_check(cur, oid, token)
        conn.commit()
        return n
    finally:
        cur.close()
        conn.close()


def iter_due_phone_checks(now, *, limit=20):
    conn = get_conn()
    cur = conn.cursor()
    try:
        recover_stale_phone_checks(cur, now)
        ids = list_due_phone_check_ids(cur, now, limit=limit)
        conn.commit()
        return ids
    finally:
        cur.close()
        conn.close()


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

    activity = dict(activity)
    activity.setdefault('character_id', character_id)
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
        recover_stale_phone_checks(cur, now)
        supersede_ended_phone_checks(cur, user_id, character_id, now, activity)
        cur.execute(
            '''SELECT id, seen, can_reply, pending_count, pending_text, event_meta,
                      fallback_promise_id, next_phone_check_at, seen_at, resolved_at,
                      COALESCE(seen_watermark, 0),
                      COALESCE(check_state, 'pending')
               FROM char_phone_check
               WHERE user_id=%s
                 AND character_id=%s
                 AND sched_date=%s
                 AND start_time=%s
                 AND end_time=%s
               FOR UPDATE''',
            (user_id, character_id, now.date(),
             activity['start_time'], activity['end_time']))
        row = cur.fetchone()
        reused = False
        watermark = 0
        count = 1
        check_state = PHONE_CHECK_PENDING
        if row:
            (oid, _seen, prev_can_reply, count, existing_text, existing_meta,
             fallback_id, next_check_at, seen_at, resolved_at, watermark,
             check_state) = row
            watermark = watermark or 0
            check_state = check_state or PHONE_CHECK_PENDING
            # 上一轮已经 reply_now / resolve 过：同一 activity 开新一轮 check，不沿用 can_reply
            terminal = (
                resolved_at
                or prev_can_reply
                or check_state in PHONE_CHECK_TERMINAL
            )
            if terminal:
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
                           check_state='pending',
                           claimed_at=NULL,
                           claim_token=NULL,
                           claim_owner=NULL,
                           claim_expires_at=NULL,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s''',
                    (source_event_id, source_event_id, pending_text,
                     event_meta_text, next_check_at, oid))
                fallback_id = None
                seen_at = None
                check_state = PHONE_CHECK_PENDING
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
                       SET pending_count = pending_count + 1,
                           last_source_event_id=%s,
                           pending_text=%s,
                           event_meta=%s,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s
                       RETURNING pending_count''',
                    (source_event_id, merged_text, merged_meta, oid))
                bumped = cur.fetchone()
                if bumped:
                    count = bumped[0]
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
            try:
                cur.execute(
                    '''INSERT INTO char_phone_check
                       (user_id, character_id, schedule_id, sched_date, start_time, end_time,
                        activity_title, reply_state, seen, can_reply, pending_count,
                        first_source_event_id, last_source_event_id, pending_text, event_meta,
                        next_phone_check_at, seen_watermark, check_state)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                       RETURNING id''',
                    (user_id, character_id, activity.get('id'), now.date(),
                     activity['start_time'], activity['end_time'],
                     activity.get('title', ''), reply_state, False, False, 1,
                     source_event_id, source_event_id, pending_text, event_meta_text,
                     next_check_at, 0, PHONE_CHECK_PENDING))
                oid = cur.fetchone()[0]
            except Exception as insert_exc:
                pgcode = getattr(insert_exc, 'pgcode', None)
                if pgcode != '23505':
                    raise
                conn.rollback()
                cur.execute(
                    '''SELECT id, seen, can_reply, pending_count, pending_text, event_meta,
                              fallback_promise_id, next_phone_check_at, seen_at, resolved_at,
                              COALESCE(seen_watermark, 0),
                              COALESCE(check_state, 'pending')
                       FROM char_phone_check
                       WHERE user_id=%s AND character_id=%s AND sched_date=%s
                         AND start_time=%s AND end_time=%s
                       FOR UPDATE''',
                    (user_id, character_id, now.date(),
                     activity['start_time'], activity['end_time']))
                row = cur.fetchone()
                oid = row[0]
                count = (row[3] or 0) + 1
                fallback_id = row[6]
                next_check_at = row[7]
                seen_at = row[8]
                watermark = row[10] or 0
                check_state = row[11]
                reused = True
                cur.execute(
                    '''UPDATE char_phone_check
                       SET pending_count = pending_count + 1,
                           last_source_event_id=%s,
                           updated_at=CURRENT_TIMESTAMP
                       WHERE id=%s
                       RETURNING pending_count''',
                    (source_event_id, oid))
                bumped = cur.fetchone()
                if bumped:
                    count = bumped[0]

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
                'check_state': check_state,
            }
            if extra:
                out.update(extra)
            return out

        # hard_busy: 只积压，不看手机。到期回复由 delayed_reply worker 生成。
        if reply_state == REPLY_HARD_BUSY:
            conn.commit()
            return _decision(False, False)

        # inbound 永不在本请求生成。到点只把 inbox 留给 worker。
        if not next_check_at or now < next_check_at:
            conn.commit()
            return _decision(count <= watermark, False)

        postponed, was_postponed = postpone_past_hard_busy(
            character_id, user_id, now)
        if was_postponed and postponed > now:
            next_check_at = postponed
            cur.execute(
                '''UPDATE char_phone_check
                   SET next_phone_check_at=%s, updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s
                     AND check_state IN ('pending', 'deferred')''',
                (postponed, oid))
            conn.commit()
            return _decision(count <= watermark, False,
                             {'postponed_for_hard_busy': True})

        conn.commit()
        return _decision(count <= watermark, False, {'due_waiting_worker': True})
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
               SET resolved_at=CURRENT_TIMESTAMP,
                   check_state='resolved',
                   claimed_at=NULL,
                   claim_token=NULL,
                   claim_owner=NULL,
                   claim_expires_at=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=%s AND resolved_at IS NULL
                 AND check_state NOT IN ('expired', 'superseded')''',
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
