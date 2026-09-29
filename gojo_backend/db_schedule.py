"""db_schedule.py —— 角色自己的一天(日程表)

设计目的:
  让角色有自己的生活节奏 —— 他不是 24 小时待命的聊天机器人。
  上课/出任务/洗澡/驾驶是 hard_busy：没法看手机。
  开会/备课/处理报告是 soft_busy：可能瞄一眼。
  探店/逛街/吃饭/发呆是 free：能正常回。
  soft_busy 可另存 effective_busy_minutes：只缩短回复系统的忙碌窗，
  不改 UI 上的 start_time / end_time。NULL 表示整段都按 reply_state。

和用户自己的 tasks 表完全无关:
  tasks        —— 【用户】的待办,用户自己排
  char_schedule —— 【角色】的行程,LLM 每天按角色背景自动生成

关键字段 can_reply:
  由 LLM 生成日程时逐条判断,不是按时间一刀切。
    上课/出任务/洗澡/驾驶 → hard_busy(没法看手机)
    开会/备课/处理报告 → soft_busy(可能瞄一眼)
    探店/逛街/查账/吃饭/发呆 → free(能摸鱼回消息)
  可选字段 effective_busy_minutes:
    仅 soft_busy。真正无法正常回复的分钟数。NULL=整段 start–end 都忙。
    不改 start_time / end_time，也不替代 note。
"""
from datetime import datetime, date as _date, timedelta, timezone
import json
import os
import random
import threading
import uuid
from db import get_conn
from schedule_contract import (
    ACTION_CANCEL,
    ACTION_COMPLETE,
    ACTION_EXTEND,
    ACTION_INSERT,
    ACTION_RELOCATE,
    EVENT_ACTIVE,
    EVENT_CANCELLED,
    EVENT_COMPLETED,
    EVENT_PLANNED,
    EVENT_TERMINAL,
    FIXED,
    FLEXIBLE,
    FLAVOR,
    OPTIONAL,
    REPLY_FREE as _CANONICAL_REPLY_FREE,
    REPLY_HARD_BUSY as _CANONICAL_REPLY_HARD_BUSY,
    REPLY_SOFT_BUSY as _CANONICAL_REPLY_SOFT_BUSY,
    advance_phase_states,
    availability_for_phase,
    build_phase_plan,
    event_bounds,
    normalize_action_intent,
    timeline_is_valid,
)
from phone_check_occurrence import (
    finish_is_safe,
    inbound_occurrence_action,
    next_check_window,
)


REPLY_FREE = _CANONICAL_REPLY_FREE
REPLY_SOFT_BUSY = _CANONICAL_REPLY_SOFT_BUSY
REPLY_HARD_BUSY = _CANONICAL_REPLY_HARD_BUSY
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
        effective_busy_minutes INTEGER,  -- soft_busy 实际无法正常回复的分钟数；NULL=整段
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
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS effective_busy_minutes INTEGER')
        # Canonical event facts. start_time/end_time stay as a read-only UI
        # compatibility projection; all new business decisions use these
        # timestamps/statuses and char_schedule_phase below.
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS planned_start_at TIMESTAMPTZ')
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS planned_end_at TIMESTAMPTZ')
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS actual_start_at TIMESTAMPTZ')
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS actual_end_at TIMESTAMPTZ')
        cur.execute("ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'planned'")
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1')
        cur.execute("ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'routine'")
        cur.execute("ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS role_bucket TEXT NOT NULL DEFAULT 'personal'")
        cur.execute("ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS fixedness TEXT NOT NULL DEFAULT 'flexible'")
        cur.execute("ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS provenance JSONB NOT NULL DEFAULT '{}'::jsonb")
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS planned_place JSONB')
        cur.execute('ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS visited_at TIMESTAMPTZ')
        cur.execute("ALTER TABLE char_schedule ADD COLUMN IF NOT EXISTS cancel_reason TEXT NOT NULL DEFAULT ''")
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
        # A phone-check row is an occurrence/snapshot. It is never recycled
        # after a reply, and inbound work during a claim creates a successor.
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS occurrence_id TEXT')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS successor_of_id INTEGER')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS schedule_event_id INTEGER')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS phase_id INTEGER')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS event_revision INTEGER')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ')
        cur.execute('ALTER TABLE char_phone_check ADD COLUMN IF NOT EXISTS superseded_by_id INTEGER')
        # Generated constraint names are truncated by PostgreSQL. Match the
        # constraint definition so every deployed name is removed reliably.
        cur.execute('''DO $$
        DECLARE old_unique RECORD;
        BEGIN
            FOR old_unique IN
                SELECT conname FROM pg_constraint
                WHERE conrelid='char_phone_check'::regclass
                  AND contype='u'
                  AND pg_get_constraintdef(oid) =
                      'UNIQUE (user_id, character_id, sched_date, start_time, end_time)'
            LOOP
                EXECUTE format('ALTER TABLE char_phone_check DROP CONSTRAINT %I',
                               old_unique.conname);
            END LOOP;
        END $$''')
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        raise RuntimeError(f'canonical schedule migration failed: {e}') from e
    # Backfill legacy rows once into canonical event bounds. Existing data is
    # retained; it merely gains an authoritative planned representation.
    try:
        cur.execute('''UPDATE char_schedule
                       SET planned_start_at =
                               (sched_date::timestamp + start_time::time)
                               AT TIME ZONE 'Asia/Shanghai',
                           planned_end_at =
                               (sched_date::timestamp
                                + end_time::time
                                + CASE WHEN end_time <= start_time
                                       THEN INTERVAL '1 day' ELSE INTERVAL '0 day' END)
                               AT TIME ZONE 'Asia/Shanghai'
                       WHERE planned_start_at IS NULL OR planned_end_at IS NULL''')
    except Exception as e:
        conn.rollback()
        cur.close()
        conn.close()
        raise RuntimeError(f'canonical schedule backfill failed: {e}') from e
    cur.execute('''CREATE TABLE IF NOT EXISTS char_schedule_phase (
        id SERIAL PRIMARY KEY,
        schedule_id INTEGER NOT NULL REFERENCES char_schedule(id) ON DELETE CASCADE,
        ordinal INTEGER NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        phase_kind TEXT NOT NULL DEFAULT 'activity',
        planned_start_at TIMESTAMPTZ NOT NULL,
        planned_end_at TIMESTAMPTZ NOT NULL,
        actual_start_at TIMESTAMPTZ,
        actual_end_at TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'planned',
        reply_state TEXT NOT NULL DEFAULT 'free',
        planned_place JSONB,
        provenance JSONB NOT NULL DEFAULT '{}'::jsonb,
        revision INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (schedule_id, ordinal)
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS char_schedule_transition (
        id SERIAL PRIMARY KEY,
        schedule_id INTEGER,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        source_event_id TEXT DEFAULT '',
        intent JSONB NOT NULL,
        prior_revision INTEGER,
        committed_revision INTEGER,
        status TEXT NOT NULL DEFAULT 'committed',
        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS char_schedule_poi_cache (
        provider TEXT NOT NULL,
        provider_place_id TEXT NOT NULL,
        canonical_name TEXT NOT NULL,
        canonical_address TEXT NOT NULL,
        lat DOUBLE PRECISION NOT NULL,
        lng DOUBLE PRECISION NOT NULL,
        verified_category TEXT NOT NULL,
        provider_raw_type JSONB NOT NULL DEFAULT '{}'::jsonb,
        fetched_at TIMESTAMPTZ,
        PRIMARY KEY (provider, provider_place_id)
    )''')
    # Legacy schedules gain one phase so they migrate into the canonical
    # reader without a second availability system.
    cur.execute('''INSERT INTO char_schedule_phase
                      (schedule_id, ordinal, title, phase_kind,
                       planned_start_at, planned_end_at, status, reply_state,
                       provenance)
                   SELECT event.id, 0, event.title, 'legacy_migrated',
                          event.planned_start_at, event.planned_end_at,
                          CASE WHEN event.status IN ('completed','cancelled')
                               THEN event.status ELSE 'planned' END,
                          COALESCE(event.reply_state,
                                   CASE WHEN event.can_reply THEN 'free'
                                        ELSE 'hard_busy' END),
                          jsonb_build_object('kind','legacy_schedule_migration',
                                             'stable_preference',false)
                   FROM char_schedule event
                   WHERE event.planned_start_at IS NOT NULL
                     AND event.planned_end_at IS NOT NULL
                     AND NOT EXISTS (
                         SELECT 1 FROM char_schedule_phase phase
                         WHERE phase.schedule_id=event.id)
                   ON CONFLICT (schedule_id, ordinal) DO NOTHING''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_sched_lookup
                   ON char_schedule (character_id, user_id, sched_date, start_time)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_sched_world_current
                   ON char_schedule (character_id, user_id, status, planned_start_at, planned_end_at)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_sched_phase_current
                   ON char_schedule_phase (schedule_id, status, planned_start_at, planned_end_at)''')
    # Canonical replacement cancels prior revisions instead of deleting
    # history. A legacy unique start-time index would reject the replacement
    # row and turn force regeneration into a failed dual-write attempt.
    cur.execute('DROP INDEX IF EXISTS idx_sched_uniq')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_phone_check_pending
                   ON char_phone_check (user_id, character_id, resolved_at, sched_date)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_phone_check_claim
                   ON char_phone_check (check_state, next_phone_check_at, claim_expires_at)''')
    cur.execute('''CREATE INDEX IF NOT EXISTS idx_phone_check_occurrence
                   ON char_phone_check (user_id, character_id, schedule_event_id,
                                        phase_id, event_revision, id DESC)''')
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


def parse_effective_busy_minutes(value):
    """Return a positive minute count, or None for missing/invalid values.

    Only a real duration is accepted. 0, negatives, bools, and non-numeric
    strings fall back to NULL so old rows keep whole-block busy behavior.
    """
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        if value <= 0 or not value.is_integer():
            return None
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text)
    except (TypeError, ValueError):
        try:
            parsed = float(text)
        except (TypeError, ValueError):
            return None
        if parsed <= 0 or not parsed.is_integer():
            return None
        number = int(parsed)
    return number if number > 0 else None


def _activity_time_bounds(activity, now):
    """Map HH:MM start/end onto now's calendar date and timezone.

    `now` is only a date/tz context. Seconds on now are discarded.
    Overnight blocks (end_time <= start_time) place end_at on the next day
    so clamp cannot snap to an earlier same-day clock time.
    """
    start_at = _hhmm_to_dt(now, activity.get('start_time') or '')
    end_at = _hhmm_to_dt(now, activity.get('end_time') or '')
    if end_at <= start_at:
        end_at = end_at + timedelta(days=1)
    return start_at, end_at


def effective_busy_end(activity, now):
    """Deprecated pure compatibility projection through the phase contract.

    Runtime business callers use the phase returned by
    ``get_current_world_state``; this helper remains for old callers/tests and
    does not read schedule data or own availability decisions.
    """
    activity = activity or {}
    if not activity.get('end_time') or now is None:
        return None
    try:
        phases = build_phase_plan(
            activity, now.date(), now.tzinfo or _canonical_tz())
    except Exception:
        return None
    if not phases:
        return None
    event_start, event_end = event_bounds(
        activity, now.date(), now.tzinfo or _canonical_tz())
    if (normalize_reply_state(activity.get('reply_state'),
                              activity.get('can_reply', True))
            == REPLY_SOFT_BUSY
            and parse_effective_busy_minutes(
                activity.get('effective_busy_minutes')) is not None):
        return phases[0]['planned_end_at']
    return event_end


def effective_reply_state(activity, now):
    """Deprecated pure projection through canonical phase rules; no DB read."""
    if not activity or now is None:
        return REPLY_FREE
    try:
        phases = build_phase_plan(
            activity, now.date(), now.tzinfo or _canonical_tz())
        current = next((phase for phase in advance_phase_states(phases, now)
                        if phase.get('status') == EVENT_ACTIVE), None)
        return availability_for_phase(current)['reply_state']
    except Exception:
        return normalize_reply_state(
            activity.get('reply_state'), activity.get('can_reply', True))


def _raw_legacy_effective_reply_state(activity, now):
    """Archived pre-phase availability calculation; never used by production."""
    if not activity:
        return REPLY_FREE
    stored = normalize_reply_state(
        activity.get('reply_state'), activity.get('can_reply', True))
    if stored != REPLY_SOFT_BUSY:
        return stored
    if parse_effective_busy_minutes(activity.get('effective_busy_minutes')) is None:
        return stored
    busy_end = effective_busy_end(activity, now)
    if busy_end is not None and now >= busy_end:
        return REPLY_FREE
    return stored


def _schedule_item_from_row(row):
    reply_state = normalize_reply_state(row[7], row[6])
    minutes = parse_effective_busy_minutes(row[8]) if len(row) > 8 else None
    return {
        'id': row[0],
        'start_time': row[1],
        'end_time': row[2],
        'title': row[3],
        'location': row[4] or '',
        'note': row[5] or '',
        'reply_state': reply_state,
        'can_reply': can_reply_from_state(reply_state),
        'effective_busy_minutes': minutes,
    }


def _raw_legacy_save_schedule_impl(character_id, user_id, sched_date, items):
    """写入一天的日程。items = [{start_time,end_time,title,location,note,can_reply,reply_state,effective_busy_minutes}]
    同一天重复调用会先清空再写,避免混杂。返回写入条数。start_time/end_time 原样保存。"""
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
            busy_minutes = parse_effective_busy_minutes(
                it.get('effective_busy_minutes'))
            if reply_state != REPLY_SOFT_BUSY:
                busy_minutes = None
            cur.execute(
                '''INSERT INTO char_schedule
                     (character_id, user_id, sched_date, start_time, end_time,
                      title, location, note, can_reply, reply_state,
                      effective_busy_minutes)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT DO NOTHING''',
                (character_id, user_id, sched_date, st, et, title[:80],
                 (it.get('location') or '')[:40],
                 (it.get('note') or '')[:120],
                 can_reply_from_state(reply_state),
                 reply_state,
                 busy_minutes)
            )
            n += cur.rowcount
        conn.commit()
    finally:
        cur.close()
        conn.close()
    print(f'[schedule] {character_id} {sched_date} 写入 {n} 条日程')
    return n


def _raw_legacy_get_schedule_impl(character_id, user_id, sched_date):
    """取某天的完整日程,按开始时间排序。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, start_time, end_time, title, location, note,
                  can_reply, COALESCE(reply_state, ''),
                  effective_busy_minutes
           FROM char_schedule
           WHERE character_id=%s AND user_id=%s AND sched_date=%s
           ORDER BY start_time ASC''',
        (character_id, user_id, sched_date))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [_schedule_item_from_row(r) for r in rows]


def _raw_legacy_get_current_activity_impl(character_id, user_id, now: datetime):
    """★ 核心:现在这一刻角色在干什么。返回 dict 或 None(没安排=空闲)。

    结果里带 can_reply,route_chat 靠它决定是正常回复还是只已读。
    """
    hhmm = now.strftime('%H:%M')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, start_time, end_time, title, location, note,
                  can_reply, COALESCE(reply_state, ''),
                  effective_busy_minutes
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
    return _schedule_item_from_row(r)


def _raw_legacy_get_next_free_time_impl(character_id, user_id, now: datetime):
    """忙完之后最早什么时候有空。返回 'HH:MM' 或 None(今天剩下都忙/没安排)。

    逻辑:从当前时刻往后找,第一个 can_reply=true 的时段开始时间;
    如果后面全是忙的,就返回最后一个忙碌时段的结束时间。
    """
    hhmm = now.strftime('%H:%M')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT start_time, end_time, can_reply, COALESCE(reply_state, ''),
                  effective_busy_minutes
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
    for st, et, can_reply, reply_state, minutes in rows:
        item = {
            'start_time': st,
            'end_time': et,
            'can_reply': can_reply,
            'reply_state': reply_state,
            'effective_busy_minutes': minutes,
        }
        if _raw_legacy_effective_reply_state(item, now) == REPLY_FREE:
            # 已经在这个时段里(理论上不该发生)就用现在,否则用它的开始时间
            return max(st, hhmm) if st <= hhmm else st
        stored = normalize_reply_state(reply_state, can_reply)
        if (stored == REPLY_SOFT_BUSY
                and parse_effective_busy_minutes(minutes) is not None):
            busy_end = effective_busy_end(item, now)
            if busy_end is not None and busy_end > now:
                return busy_end.strftime('%H:%M')
    # 后面全忙 → 最后一段结束时
    return rows[-1][1]


def _hhmm_to_dt(now: datetime, hhmm: str):
    hh, mm = hhmm.split(':')
    return now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)


def sample_next_phone_check_at(now: datetime, activity, after=None,
                               replied_at=None):
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
    momentum_window = next_check_window(
        normalize_reply_state((activity or {}).get('reply_state'),
                              (activity or {}).get('can_reply', True)),
        now, replied_at=replied_at)
    if momentum_window:
        lo, hi = momentum_window
    delay = random.randint(lo, hi)
    candidate = base + timedelta(minutes=delay)
    end_at = _hhmm_to_dt(now, activity['end_time'])
    if candidate >= end_at:
        # `activity` is the canonical current phase. Its end—not its parent
        # event's end—bounds another phone check.
        candidate = max(base + timedelta(minutes=1), end_at - timedelta(minutes=1))
        if candidate <= base:
            candidate = end_at
    return candidate


def postpone_past_hard_busy(character_id, user_id, check_at: datetime):
    """Compatibility no-op; workers re-read canonical availability when due."""
    return check_at, False


def _claim_owner():
    return f'pid:{os.getpid()}:tid:{threading.get_ident()}'


def _lock_phone_check_owner_tx(cur, user_id, character_id):
    """Serialize occurrence creation and schedule reconciliation per chat."""
    lock_key = f'{user_id}:{character_id}:phone_check_occurrences'
    cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (lock_key,))


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
                      sched_date, schedule_event_id, phase_id, event_revision,
                      occurrence_id''',
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
        'schedule_event_id': row[16] if len(row) > 16 else None,
        'phase_id': row[17] if len(row) > 17 else None,
        'event_revision': row[18] if len(row) > 18 else None,
        'occurrence_id': row[19] if len(row) > 19 else None,
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
                consumed_at = CURRENT_TIMESTAMP,
               claimed_at = NULL,
               claim_token = NULL,
               claim_owner = NULL,
               claim_expires_at = NULL,
               updated_at = CURRENT_TIMESTAMP
            WHERE id = %s
              AND claim_token = %s
              AND check_state = 'processing'
              AND pending_count <= COALESCE(seen_watermark, pending_count) ''',
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

        # Commit the atomic claim before the canonical world reader reconciles
        # stale phase occurrences on its own connection. The processing row is
        # then a durable immutable claim snapshot, not a held database lock.
        conn.commit()

        character_id = claimed['character_id']
        user_id = claimed['user_id']
        # The worker reads the same canonical event/phase/availability bundle
        # as every inbound surface.  It must never rediscover availability by
        # querying the parent schedule block on its own.
        world = get_current_world_state(character_id, user_id, now)
        current = world.get('activity')
        current_event = world.get('event') or {}
        current_phase = world.get('phase') or {}
        availability = world.get('availability') or {}

        # A schedule transition may have happened after this occurrence was
        # armed.  The old occurrence cannot generate against stale context.
        claimed_event_id = claimed.get('schedule_event_id')
        claimed_phase_id = claimed.get('phase_id')
        claimed_revision = claimed.get('event_revision')
        stale_occurrence = bool(
            claimed_event_id and (
                current_event.get('id') != claimed_event_id
                or (claimed_revision is not None
                    and current_event.get('revision') != claimed_revision)
                or (claimed_phase_id is not None
                    and current_phase.get('id') != claimed_phase_id)
            )
        )
        if stale_occurrence:
            cur.execute(
                '''UPDATE char_phone_check
                   SET check_state='superseded', resolved_at=%s,
                       claim_token=NULL, claim_owner=NULL, claim_expires_at=NULL,
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=%s AND claim_token=%s AND check_state='processing' ''',
                (now, oid, claimed['claim_token']))
            conn.commit()
            return {'action': 'skip', 'claimed': None, 'reason': 'stale_schedule_occurrence'}

        activity = dict(current or {
            'start_time': claimed.get('start_time'),
            'end_time': claimed.get('end_time'),
            'title': claimed.get('activity_title') or '',
            'reply_state': claimed.get('reply_state'),
            'character_id': character_id,
        })
        state = availability.get('reply_state') or REPLY_FREE
        if state == REPLY_HARD_BUSY:
            phase_end = current_phase.get('planned_end_at')
            postponed = (phase_end + timedelta(minutes=1)
                         if isinstance(phase_end, datetime) else now + timedelta(minutes=1))
            finish_claimed_defer(cur, oid, claimed['claim_token'], now, postponed)
            conn.commit()
            return {
                'action': 'postpone',
                'claimed': claimed,
                'next_phone_check_at': postponed,
            }
        currently_free = bool(availability.get('can_reply', state == REPLY_FREE))

        reply_now = True
        if not currently_free and state == REPLY_SOFT_BUSY:
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
      · 一个 immutable occurrence 只拥有其 claim 时的 inbox 快照
      · 未 claim 时的新消息附加当前 occurrence；生成中的 occurrence 收到
        新消息时必须创建 successor，绝不推进或污染旧 watermark
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
        _lock_phone_check_owner_tx(cur, user_id, character_id)
        recover_stale_phone_checks(cur, now)
        supersede_ended_phone_checks(cur, user_id, character_id, now, activity)
        event_id = activity.get('event_id') or activity.get('id')
        phase_id = activity.get('phase_id')
        event_revision = activity.get('revision')
        cur.execute(
            '''SELECT id, seen, can_reply, pending_count, pending_text, event_meta,
                      fallback_promise_id, next_phone_check_at, seen_at, resolved_at,
                      COALESCE(seen_watermark, 0),
                      COALESCE(check_state, 'pending'), schedule_event_id,
                      phase_id, event_revision, consumed_at
               FROM char_phone_check
               WHERE user_id=%s
                 AND character_id=%s
                  AND sched_date=%s
                  AND start_time=%s
                  AND end_time=%s
                ORDER BY id DESC
                LIMIT 1
                FOR UPDATE''',
            (user_id, character_id, now.date(),
             activity['start_time'], activity['end_time']))
        row = cur.fetchone()
        reused = False
        watermark = 0
        count = 1
        check_state = PHONE_CHECK_PENDING
        successor_of_id = None
        replied_at = None
        if row:
            (oid, _seen, prev_can_reply, count, existing_text, existing_meta,
             fallback_id, next_check_at, seen_at, resolved_at, watermark,
             check_state) = row[:12]
            schedule_event_id = row[12] if len(row) > 12 else None
            phase_id = row[13] if len(row) > 13 else None
            stored_revision = row[14] if len(row) > 14 else None
            consumed_at = row[15] if len(row) > 15 else None
            watermark = watermark or 0
            check_state = check_state or PHONE_CHECK_PENDING
            # A processing row is a frozen generation snapshot. New inbound
            # content must become a successor, otherwise finish(A) could
            # consume B. Terminal rows are likewise never recycled.
            terminal = (
                resolved_at
                or prev_can_reply
                or check_state in PHONE_CHECK_TERMINAL
            )
            occurrence_action = inbound_occurrence_action(
                {'check_state': check_state, 'event_revision': stored_revision},
                event_revision=event_revision)
            if terminal or occurrence_action == 'successor':
                successor_of_id = oid
                replied_at = consumed_at if terminal else None
                # fall through to INSERT; do not mutate this occurrence.
                row = None
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
        if not row:
            fallback_id = None
            seen_at = None
            next_check_at = None
            watermark = 0
            count = 1
            if reply_state == REPLY_SOFT_BUSY:
                next_check_at = sample_next_phone_check_at(
                    now, activity, replied_at=replied_at)
            cur.execute(
                '''INSERT INTO char_phone_check
                   (user_id, character_id, schedule_id, sched_date, start_time, end_time,
                    activity_title, reply_state, seen, can_reply, pending_count,
                    first_source_event_id, last_source_event_id, pending_text, event_meta,
                    next_phone_check_at, seen_watermark, check_state,
                    occurrence_id, successor_of_id, schedule_event_id,
                    phase_id, event_revision)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                           %s,%s,%s,%s,%s)
                   RETURNING id''',
                (user_id, character_id, event_id, now.date(),
                 activity['start_time'], activity['end_time'],
                 activity.get('title', ''), reply_state, False, False, 1,
                 source_event_id, source_event_id, pending_text, event_meta_text,
                 next_check_at, 0, PHONE_CHECK_PENDING, str(uuid.uuid4()),
                 successor_of_id, event_id, phase_id, event_revision))
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


def _raw_legacy_has_schedule_impl(character_id, user_id, sched_date) -> bool:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT 1 FROM char_schedule WHERE character_id=%s AND user_id=%s AND sched_date=%s LIMIT 1',
        (character_id, user_id, sched_date))
    ok = cur.fetchone() is not None
    cur.close()
    conn.close()
    return ok


def _raw_legacy_clear_schedule_impl(character_id, user_id, sched_date):
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


# ────────────────────────────────────────────────────────────────────────────
# Canonical schedule/world state
# ────────────────────────────────────────────────────────────────────────────
#
# The older functions above remain temporarily for migration compatibility and
# old unit fixtures. New business entry points below are the sole authority for
# event status, phase progress, availability, map facts, and transitions. A
# legacy `start_time/end_time` row is only a UI projection of this state.


def _canonical_tz():
    try:
        from config import CN_TZ
        return CN_TZ
    except Exception:
        return timezone.utc


def _json_object(value, default=None):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            pass
    return dict(default or {})


def _iso(value):
    return value.isoformat() if getattr(value, 'isoformat', None) else value


def _hhmm(value):
    if not isinstance(value, datetime):
        return ''
    try:
        return value.astimezone(_canonical_tz()).strftime('%H:%M')
    except Exception:
        return value.strftime('%H:%M')


def _normalize_planned_place(place):
    """Keep only resolver-verified POI facts in canonical schedule rows."""
    if not isinstance(place, dict):
        return None
    required = (
        'provider', 'provider_place_id', 'canonical_name', 'canonical_address',
        'lat', 'lng', 'verified_category', 'provider_raw_type', 'fetched_at',
    )
    if not all(place.get(key) not in (None, '') for key in required):
        return None
    try:
        lat = float(place.get('lat'))
        lng = float(place.get('lng'))
    except (TypeError, ValueError):
        return None
    if not lat or not lng:
        return None
    return {
        'provider': str(place['provider'])[:40],
        'provider_place_id': str(place['provider_place_id'])[:120],
        'canonical_name': str(place['canonical_name'])[:160],
        'canonical_address': str(place['canonical_address'])[:300],
        'lat': lat,
        'lng': lng,
        'verified_category': str(place['verified_category'])[:60],
        'provider_raw_type': place.get('provider_raw_type') or {},
        'fetched_at': str(place['fetched_at'])[:80],
        'city': str(place.get('city') or '')[:40],
    }


def _general_area(value):
    """A model may describe an area, but cannot create a map point from it."""
    return str(value or '').strip()[:120]


def _category_for_item(item):
    category = str(item.get('category') or '').strip().lower()
    if category in ('obligation', 'routine', 'social', 'leisure', 'flavor'):
        return category
    text = f"{item.get('title', '')} {item.get('note', '')}"
    if any(word in text for word in ('家族', '会议', '授课', '任务', '报告', '文件', '安排')):
        return 'obligation'
    if any(word in text for word in ('甜', '咖啡', '蛋糕', '限定', '探店')):
        return 'flavor'
    if any(word in text for word in ('散步', '逛', '看展', '休息', '闲逛')):
        return 'leisure'
    if any(word in text for word in ('见面', '聚', '会面', '拜访')):
        return 'social'
    return 'routine'


def _role_bucket_for_item(item):
    try:
        from schedule_novelty import role_bucket_for_item
        return role_bucket_for_item(item)
    except Exception:
        return 'personal'


def _fixedness_for_item(item):
    value = str(item.get('fixedness') or '').strip().lower()
    if value in (FIXED, FLEXIBLE, OPTIONAL, FLAVOR):
        return value
    category = _category_for_item(item)
    if category == 'flavor':
        return FLAVOR
    if any(word in str(item.get('title') or '') for word in ('授课', '上课', '必须出席', '正式会议')):
        return FIXED
    return FLEXIBLE


def _canonical_event_from_row(row):
    (event_id, sched_date, start_time, end_time, title, location, note,
     can_reply, reply_state, effective_busy_minutes, planned_start_at,
     planned_end_at, actual_start_at, actual_end_at, status, revision,
     category, role_bucket, fixedness, provenance, planned_place, visited_at,
     cancel_reason) = row
    reply_state = normalize_reply_state(reply_state, can_reply)
    place = _json_object(planned_place) if planned_place else None
    return {
        'id': event_id,
        'event_id': event_id,
        'sched_date': sched_date,
        'start_time': start_time,
        'end_time': end_time,
        'title': title or '',
        'location': location or '',
        'note': note or '',
        'reply_state': reply_state,
        'can_reply': can_reply_from_state(reply_state),
        'effective_busy_minutes': parse_effective_busy_minutes(effective_busy_minutes),
        'planned_start_at': planned_start_at,
        'planned_end_at': planned_end_at,
        'actual_start_at': actual_start_at,
        'actual_end_at': actual_end_at,
        'status': status or EVENT_PLANNED,
        'revision': int(revision or 1),
        'category': category or 'routine',
        'role_bucket': role_bucket or 'personal',
        'fixedness': fixedness or FLEXIBLE,
        'provenance': _json_object(provenance),
        'planned_place': place,
        'visited_at': visited_at,
        'cancel_reason': cancel_reason or '',
    }


def _canonical_phase_from_row(row):
    (phase_id, schedule_id, ordinal, title, phase_kind, planned_start_at,
     planned_end_at, actual_start_at, actual_end_at, status, reply_state,
     planned_place, provenance, revision) = row
    return {
        'id': phase_id,
        'schedule_id': schedule_id,
        'ordinal': int(ordinal or 0),
        'title': title or '',
        'phase_kind': phase_kind or 'activity',
        'planned_start_at': planned_start_at,
        'planned_end_at': planned_end_at,
        'actual_start_at': actual_start_at,
        'actual_end_at': actual_end_at,
        'status': status or EVENT_PLANNED,
        'reply_state': normalize_reply_state(reply_state, True),
        'planned_place': _json_object(planned_place) if planned_place else None,
        'provenance': _json_object(provenance),
        'revision': int(revision or 1),
    }


_EVENT_COLUMNS = '''id, sched_date, start_time, end_time, title, location, note,
                    can_reply, COALESCE(reply_state, ''), effective_busy_minutes,
                    planned_start_at, planned_end_at, actual_start_at, actual_end_at,
                    status, revision, category, role_bucket, fixedness,
                    provenance, planned_place, visited_at, cancel_reason'''
_PHASE_COLUMNS = '''id, schedule_id, ordinal, title, phase_kind,
                    planned_start_at, planned_end_at, actual_start_at, actual_end_at,
                    status, reply_state, planned_place, provenance, revision'''


def _cache_poi_tx(cur, place):
    if not place:
        return
    cur.execute('''INSERT INTO char_schedule_poi_cache
                      (provider, provider_place_id, canonical_name,
                       canonical_address, lat, lng, verified_category,
                       provider_raw_type, fetched_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                   ON CONFLICT (provider, provider_place_id) DO UPDATE
                   SET canonical_name=EXCLUDED.canonical_name,
                       canonical_address=EXCLUDED.canonical_address,
                       lat=EXCLUDED.lat, lng=EXCLUDED.lng,
                       verified_category=EXCLUDED.verified_category,
                       provider_raw_type=EXCLUDED.provider_raw_type,
                       fetched_at=EXCLUDED.fetched_at''',
                (place['provider'], place['provider_place_id'],
                 place['canonical_name'], place['canonical_address'],
                 place['lat'], place['lng'], place['verified_category'],
                 json.dumps(place.get('provider_raw_type') or {}, ensure_ascii=False),
                 place.get('fetched_at')))


def _lookup_cached_poi_tx(cur, reference):
    if not isinstance(reference, dict):
        return None
    provider = str(reference.get('provider') or '').strip()
    provider_place_id = str(reference.get('provider_place_id') or '').strip()
    if not provider or not provider_place_id:
        return None
    cur.execute('''SELECT provider, provider_place_id, canonical_name,
                          canonical_address, lat, lng, verified_category,
                          provider_raw_type, fetched_at
                   FROM char_schedule_poi_cache
                   WHERE provider=%s AND provider_place_id=%s''',
                (provider, provider_place_id))
    row = cur.fetchone()
    if not row:
        return None
    return _normalize_planned_place({
        'provider': row[0], 'provider_place_id': row[1],
        'canonical_name': row[2], 'canonical_address': row[3],
        'lat': row[4], 'lng': row[5], 'verified_category': row[6],
        'provider_raw_type': _json_object(row[7]), 'fetched_at': _iso(row[8]),
    })


def _insert_canonical_event_tx(cur, character_id, user_id, sched_date, item,
                               *, now=None, provenance=None):
    timezone_value = _canonical_tz()
    event_start, event_end = event_bounds(item, sched_date, timezone_value)
    reply_state = normalize_reply_state(
        item.get('reply_state'), item.get('can_reply', True))
    planned_place = _normalize_planned_place(item.get('planned_place'))
    if planned_place:
        _cache_poi_tx(cur, planned_place)
        location = planned_place['canonical_name']
    else:
        location = _general_area(item.get('location'))
    event_provenance = {
        'kind': 'synthetic_schedule',
        **(provenance or {}),
        **(item.get('provenance') or {}),
        # Neither caller nor model-supplied provenance can turn generated
        # schedule content into a durable preference or an actual visit.
        'stable_preference': False,
        'visited_on_generation': False,
    }
    category = _category_for_item(item)
    role_bucket = _role_bucket_for_item(item)
    fixedness = _fixedness_for_item(item)
    cur.execute(
        '''INSERT INTO char_schedule
              (character_id, user_id, sched_date, start_time, end_time,
               title, location, note, can_reply, reply_state,
               effective_busy_minutes, planned_start_at, planned_end_at,
               actual_start_at, actual_end_at, status, revision, category,
               role_bucket, fixedness, provenance, planned_place)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                   NULL,NULL,%s,1,%s,%s,%s,%s::jsonb,%s::jsonb)
           RETURNING id''',
        (character_id, user_id, sched_date,
         event_start.strftime('%H:%M'), event_end.strftime('%H:%M'),
         str(item.get('title') or '').strip()[:80], location[:120],
         str(item.get('note') or '').strip()[:300],
         can_reply_from_state(reply_state), reply_state,
         parse_effective_busy_minutes(item.get('effective_busy_minutes'))
         if reply_state == REPLY_SOFT_BUSY else None,
         event_start, event_end, EVENT_PLANNED, category, role_bucket,
         fixedness, json.dumps(event_provenance, ensure_ascii=False),
         json.dumps(planned_place, ensure_ascii=False) if planned_place else None))
    schedule_id = cur.fetchone()[0]
    phase_item = dict(item)
    phase_item['planned_place'] = planned_place
    phase_item['provenance'] = event_provenance
    phases = build_phase_plan(phase_item, sched_date, timezone_value)
    for phase in phases:
        phase_place = _normalize_planned_place(phase.get('planned_place')) or planned_place
        cur.execute(
            '''INSERT INTO char_schedule_phase
                  (schedule_id, ordinal, title, phase_kind,
                   planned_start_at, planned_end_at, actual_start_at,
                   actual_end_at, status, reply_state, planned_place,
                   provenance, revision)
               VALUES (%s,%s,%s,%s,%s,%s,NULL,NULL,%s,%s,%s::jsonb,%s::jsonb,1)''',
            (schedule_id, phase['ordinal'], phase.get('title') or '',
             phase.get('kind') or phase.get('phase_kind') or 'activity',
             phase['planned_start_at'], phase['planned_end_at'], EVENT_PLANNED,
             normalize_reply_state(phase.get('reply_state'), True),
             json.dumps(phase_place, ensure_ascii=False) if phase_place else None,
             json.dumps(phase.get('provenance') or event_provenance,
                        ensure_ascii=False)))
    return schedule_id


def _advance_world_tx(cur, character_id, user_id, now):
    """Apply only deterministic clock transitions, never generated prose."""
    cur.execute(
        '''UPDATE char_schedule_phase AS phase
           SET status=%s,
               actual_start_at=COALESCE(phase.actual_start_at, phase.planned_start_at),
               actual_end_at=COALESCE(phase.actual_end_at, phase.planned_end_at),
               revision=phase.revision+1
           FROM char_schedule AS event
           WHERE phase.schedule_id=event.id
             AND event.character_id=%s AND event.user_id=%s
             AND event.status NOT IN (%s,%s)
             AND phase.status IN (%s,%s)
             AND phase.planned_end_at <= %s''',
        (EVENT_COMPLETED, character_id, user_id, EVENT_COMPLETED, EVENT_CANCELLED,
         EVENT_PLANNED, EVENT_ACTIVE, now))
    cur.execute(
        '''UPDATE char_schedule_phase AS phase
           SET status=%s,
               actual_start_at=COALESCE(phase.actual_start_at, phase.planned_start_at),
               revision=phase.revision+1
           FROM char_schedule AS event
           WHERE phase.schedule_id=event.id
             AND event.character_id=%s AND event.user_id=%s
             AND event.status NOT IN (%s,%s)
             AND phase.status=%s
             AND phase.planned_start_at <= %s AND phase.planned_end_at > %s''',
        (EVENT_ACTIVE, character_id, user_id, EVENT_COMPLETED, EVENT_CANCELLED,
         EVENT_PLANNED, now, now))
    cur.execute(
        '''UPDATE char_schedule
           SET status=%s,
               actual_start_at=COALESCE(actual_start_at, planned_start_at),
               revision=revision+1
           WHERE character_id=%s AND user_id=%s AND status=%s
             AND planned_start_at <= %s AND planned_end_at > %s''',
        (EVENT_ACTIVE, character_id, user_id, EVENT_PLANNED, now, now))
    cur.execute(
        '''UPDATE char_schedule AS event
           SET status=%s,
               actual_start_at=COALESCE(event.actual_start_at, event.planned_start_at),
               actual_end_at=COALESCE(event.actual_end_at, event.planned_end_at),
               revision=event.revision+1
           WHERE event.character_id=%s AND event.user_id=%s
             AND event.status IN (%s,%s)
             AND NOT EXISTS (
                 SELECT 1 FROM char_schedule_phase phase
                 WHERE phase.schedule_id=event.id
                   AND phase.status IN (%s,%s)
             )
           RETURNING event.id, event.character_id, event.user_id,
                     event.planned_place, event.note, event.sched_date,
                     event.visited_at''',
        (EVENT_COMPLETED, character_id, user_id, EVENT_PLANNED, EVENT_ACTIVE,
         EVENT_PLANNED, EVENT_ACTIVE))
    completed = cur.fetchall() or []
    completed_ids = [row[0] for row in completed]
    if completed_ids:
        _reconcile_phone_checks_tx(
            cur, completed_ids, now, user_id=user_id,
            character_id=character_id)

    # A clock-driven phase boundary changes which phase owns availability.
    # Carry unread inboxes to a successor before the old phase can fire.
    cur.execute(
        '''SELECT id, revision FROM char_schedule
           WHERE character_id=%s AND user_id=%s AND status=%s
           ORDER BY planned_start_at DESC, id DESC LIMIT 1''',
        (character_id, user_id, EVENT_ACTIVE))
    active_event = cur.fetchone()
    if active_event:
        event_id, revision = active_event
        cur.execute(
            '''SELECT id FROM char_schedule_phase
               WHERE schedule_id=%s AND status=%s
               ORDER BY ordinal ASC LIMIT 1''',
            (event_id, EVENT_ACTIVE))
        active_phase = cur.fetchone()
        _reconcile_phone_checks_tx(
            cur, [event_id], now,
            user_id=user_id, character_id=character_id,
            replacement_event={
                'id': event_id,
                'phase_id': active_phase[0] if active_phase else None,
                'revision': revision,
                'only_stale': True,
            })
    return completed


def _mark_completed_event_visited(events):
    """A plan becomes a visit only after a completed authoritative event."""
    for event_id, character_id, user_id, planned_place, note, sched_date, visited_at in events or []:
        if visited_at:
            continue
        place = _normalize_planned_place(_json_object(planned_place))
        if not place:
            continue
        try:
            import db_visited_places
            db_visited_places.add_visited(
                character_id, user_id, place, review=note or '',
                visit_date=sched_date, schedule_event_id=event_id,
                provenance='schedule_completed')
            conn = get_conn()
            cur = conn.cursor()
            try:
                cur.execute('UPDATE char_schedule SET visited_at=CURRENT_TIMESTAMP WHERE id=%s AND visited_at IS NULL',
                            (event_id,))
                conn.commit()
            finally:
                cur.close()
                conn.close()
        except Exception as exc:
            print(f'[schedule] completed visit write skipped event={event_id}: {exc}')


def get_current_world_state(character_id, user_id, now=None, *, conn=None):
    """Read the one canonical schedule/world state for every entry point.

    It returns the active event, active phase, and availability together so a
    caller cannot accidentally combine a planned event with a separate legacy
    availability calculation.
    """
    now = now or datetime.now(_canonical_tz())
    owns = conn is None
    if conn is None:
        conn = get_conn()
    cur = conn.cursor()
    completed = []
    try:
        completed = _advance_world_tx(cur, character_id, user_id, now)
        cur.execute(
            f'''SELECT {_EVENT_COLUMNS}
                FROM char_schedule
                WHERE character_id=%s AND user_id=%s AND status=%s
                ORDER BY COALESCE(actual_start_at, planned_start_at) DESC, id DESC
                LIMIT 1''',
            (character_id, user_id, EVENT_ACTIVE))
        event_row = cur.fetchone()
        event = _canonical_event_from_row(event_row) if event_row else None
        phase = None
        if event:
            cur.execute(
                f'''SELECT {_PHASE_COLUMNS}
                    FROM char_schedule_phase
                    WHERE schedule_id=%s AND status=%s
                    ORDER BY ordinal ASC LIMIT 1''',
                (event['id'], EVENT_ACTIVE))
            phase_row = cur.fetchone()
            phase = _canonical_phase_from_row(phase_row) if phase_row else None
        if owns:
            conn.commit()
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
    if completed and owns:
        _mark_completed_event_visited(completed)
    availability = availability_for_phase(phase)
    if not phase and event:
        availability = {
            'reply_state': REPLY_FREE,
            'can_reply': True,
            'phase_id': None,
            'phase_status': None,
        }
    activity = None
    if event:
        activity = dict(event)
        activity['event_start_time'] = event.get('start_time')
        activity['event_end_time'] = event.get('end_time')
        activity['phase_id'] = phase.get('id') if phase else None
        activity['phase_title'] = phase.get('title') if phase else ''
        activity['phase_kind'] = phase.get('phase_kind') if phase else ''
        activity['phase_status'] = phase.get('status') if phase else None
        activity['phase_start_at'] = phase.get('planned_start_at') if phase else None
        activity['phase_end_at'] = phase.get('planned_end_at') if phase else None
        activity['reply_state'] = availability['reply_state']
        activity['can_reply'] = availability['can_reply']
        if phase:
            # Availability and phone-check cadence are phase-scoped. Parent
            # event times remain separately available for schedule UI/prompt.
            activity['start_time'] = _hhmm(phase.get('planned_start_at'))
            activity['end_time'] = _hhmm(phase.get('planned_end_at'))
            # ``effective_busy_minutes`` was a legacy parent-block shortcut.
            # Once canonical phases exist it must not shorten or extend the
            # active phase's availability window.
            activity['effective_busy_minutes'] = None
        if phase and phase.get('planned_place'):
            activity['planned_place'] = phase['planned_place']
            activity['location'] = phase['planned_place'].get(
                'canonical_name', activity.get('location', ''))
    return {
        'event': event,
        'phase': phase,
        'activity': activity,
        'availability': availability,
        'now': now,
    }


def get_canonical_schedule(character_id, user_id, sched_date, now=None,
                           *, include_cancelled=False):
    """Return schedule UI data from canonical events plus their phases."""
    now = now or datetime.now(_canonical_tz())
    conn = get_conn()
    cur = conn.cursor()
    completed = []
    try:
        completed = _advance_world_tx(cur, character_id, user_id, now)
        where_cancelled = '' if include_cancelled else 'AND status <> %s'
        params = [character_id, user_id, sched_date]
        if not include_cancelled:
            params.append(EVENT_CANCELLED)
        cur.execute(
            f'''SELECT {_EVENT_COLUMNS}
                FROM char_schedule
                WHERE character_id=%s AND user_id=%s AND sched_date=%s
                {where_cancelled}
                ORDER BY planned_start_at ASC, id ASC''', params)
        events = [_canonical_event_from_row(row) for row in (cur.fetchall() or [])]
        ids = [event['id'] for event in events]
        phases_by_event = {event_id: [] for event_id in ids}
        if ids:
            cur.execute(
                f'''SELECT {_PHASE_COLUMNS}
                    FROM char_schedule_phase
                    WHERE schedule_id = ANY(%s)
                    ORDER BY schedule_id ASC, ordinal ASC''', (ids,))
            for row in cur.fetchall() or []:
                phase = _canonical_phase_from_row(row)
                phases_by_event.setdefault(phase['schedule_id'], []).append(phase)
        conn.commit()
    finally:
        cur.close()
        conn.close()
    if completed:
        _mark_completed_event_visited(completed)
    out = []
    for event in events:
        phases = phases_by_event.get(event['id'], [])
        payload = dict(event)
        payload['sched_date'] = str(event.get('sched_date')) if event.get('sched_date') else None
        payload['planned_start_at'] = _iso(event.get('planned_start_at'))
        payload['planned_end_at'] = _iso(event.get('planned_end_at'))
        payload['actual_start_at'] = _iso(event.get('actual_start_at'))
        payload['actual_end_at'] = _iso(event.get('actual_end_at'))
        payload['visited_at'] = _iso(event.get('visited_at'))
        payload['phases'] = [{
            **phase,
            'planned_start_at': _iso(phase.get('planned_start_at')),
            'planned_end_at': _iso(phase.get('planned_end_at')),
            'actual_start_at': _iso(phase.get('actual_start_at')),
            'actual_end_at': _iso(phase.get('actual_end_at')),
        } for phase in phases]
        out.append(payload)
    return out


def save_canonical_schedule(character_id, user_id, sched_date, items,
                             *, force=False, provenance=None, now=None):
    """Atomically replace planned content while preserving past actual facts."""
    items = [
        item for item in (items or [])
        if isinstance(item, dict)
        and item.get('start_time') and item.get('end_time') and item.get('title')
    ]
    if not items:
        return []
    if not timeline_is_valid(items, sched_date, _canonical_tz()):
        raise ValueError('canonical schedule cannot contain overlapping planned events')
    now = now or datetime.now(_canonical_tz())
    conn = get_conn()
    cur = conn.cursor()
    prior_event_ids = []
    try:
        if force:
            cur.execute(
                '''UPDATE char_schedule
                   SET status=%s, cancel_reason=%s, revision=revision+1
                   WHERE character_id=%s AND user_id=%s AND sched_date=%s
                     AND status IN (%s,%s)
                   RETURNING id''',
                (EVENT_CANCELLED, 'force_regenerated', character_id, user_id,
                 sched_date, EVENT_PLANNED, EVENT_ACTIVE))
            prior_event_ids = [row[0] for row in cur.fetchall() or []]
        inserted = []
        for item in items:
            event_id = _insert_canonical_event_tx(
                cur, character_id, user_id, sched_date, item,
                now=now, provenance=provenance)
            inserted.append(event_id)
        if prior_event_ids:
            _reconcile_phone_checks_tx(
                cur, prior_event_ids, now, user_id=user_id,
                character_id=character_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
    return get_canonical_schedule(character_id, user_id, sched_date, now=now)


def canonical_has_schedule(character_id, user_id, sched_date):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute('''SELECT 1 FROM char_schedule
                       WHERE character_id=%s AND user_id=%s AND sched_date=%s
                         AND status <> %s LIMIT 1''',
                    (character_id, user_id, sched_date, EVENT_CANCELLED))
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()


def get_recent_schedule_history(character_id, user_id, *, before_date=None,
                                days=14, limit=240):
    """Canonical recent history for novelty and role-balance validation."""
    before_date = before_date or datetime.now(_canonical_tz()).date()
    start_date = before_date - timedelta(days=max(1, int(days)))
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT sched_date, title, note, category, role_bucket,
                      planned_place, status, provenance
               FROM char_schedule
               WHERE character_id=%s AND user_id=%s
                 AND sched_date >= %s AND sched_date < %s
                 AND status <> %s
               ORDER BY sched_date DESC, id DESC LIMIT %s''',
            (character_id, user_id, start_date, before_date,
             EVENT_CANCELLED, limit))
        rows = cur.fetchall() or []
    finally:
        cur.close()
        conn.close()
    return [{
        'sched_date': row[0], 'title': row[1] or '', 'note': row[2] or '',
        'category': row[3] or 'routine', 'role_bucket': row[4] or 'personal',
        'planned_place': _json_object(row[5]) if row[5] else None,
        'status': row[6] or EVENT_PLANNED,
        'provenance': _json_object(row[7]),
    } for row in rows]


def clear_canonical_schedule(character_id, user_id, sched_date, *, now=None):
    now = now or datetime.now(_canonical_tz())
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE char_schedule
               SET status=%s, cancel_reason=%s, revision=revision+1
               WHERE character_id=%s AND user_id=%s AND sched_date=%s
                 AND status IN (%s,%s)
               RETURNING id''',
            (EVENT_CANCELLED, 'cleared', character_id, user_id, sched_date,
             EVENT_PLANNED, EVENT_ACTIVE))
        ids = [row[0] for row in cur.fetchall() or []]
        _reconcile_phone_checks_tx(
            cur, ids, now, user_id=user_id, character_id=character_id)
        conn.commit()
        return len(ids)
    finally:
        cur.close()
        conn.close()


def _reconcile_phone_checks_tx(cur, event_ids, now, *, user_id=None,
                               character_id=None, replacement_event=None):
    """Supersede old occurrences without discarding their unread bundle.

    Any pending content is copied to one immediate successor first. The old
    occurrence cannot fire after a schedule revision, while the user message
    remains deliverable exactly once by the successor.
    """
    event_ids = [int(event_id) for event_id in (event_ids or []) if event_id]
    if not event_ids:
        return []
    if user_id is not None and character_id is not None:
        _lock_phone_check_owner_tx(cur, user_id, character_id)
    stale_filter = ''
    filter_params = ()
    if replacement_event and replacement_event.get('only_stale'):
        stale_filter = '''AND (event_revision IS DISTINCT FROM %s
                               OR phase_id IS DISTINCT FROM %s)'''
        filter_params = (
            replacement_event.get('revision'),
            replacement_event.get('phase_id'),
        )
    cur.execute(
        '''SELECT id, user_id, character_id, schedule_id, sched_date, start_time,
                  end_time, activity_title, reply_state, pending_count,
                  first_source_event_id, last_source_event_id, pending_text,
                  event_meta, seen_watermark, check_state, event_revision
           FROM char_phone_check
           WHERE schedule_event_id = ANY(%s)
             AND check_state NOT IN ('consumed','resolved','expired','superseded')
           ''' + stale_filter + ''' FOR UPDATE''',
        (event_ids,) + filter_params)
    rows = cur.fetchall() or []
    successors = []
    for row in rows:
        (oid, user_id, character_id, schedule_id, sched_date, start_time,
         end_time, activity_title, reply_state, pending_count, first_source,
         last_source, pending_text, event_meta, watermark, check_state,
         event_revision) = row
        pending_count = int(pending_count or 0)
        watermark = int(watermark or 0)
        outstanding = pending_count - watermark if check_state == PHONE_CHECK_PROCESSING else pending_count
        successor_id = None
        if outstanding > 0:
            occurrence_id = str(uuid.uuid4())
            replacement = (replacement_event if replacement_event
                           and replacement_event.get('id') == schedule_id
                           else None)
            event_id = (replacement or {}).get('id')
            phase_id = (replacement or {}).get('phase_id')
            revision = (replacement or {}).get('revision')
            cur.execute(
                '''INSERT INTO char_phone_check
                      (user_id, character_id, schedule_id, sched_date,
                       start_time, end_time, activity_title, reply_state,
                       seen, can_reply, pending_count, first_source_event_id,
                       last_source_event_id, pending_text, event_meta,
                       next_phone_check_at, seen_watermark, check_state,
                       occurrence_id, successor_of_id, schedule_event_id,
                       phase_id, event_revision)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,FALSE,FALSE,%s,%s,%s,%s,%s,
                           %s,0,'pending',%s,%s,%s,%s,%s)
                   RETURNING id''',
                (user_id, character_id, event_id, sched_date, start_time, end_time,
                 activity_title or '日程变化后待回复', REPLY_SOFT_BUSY, outstanding,
                 first_source or '', last_source or '', pending_text or '',
                 event_meta or '', now, occurrence_id, oid, event_id, phase_id,
                 revision))
            successor_id = cur.fetchone()[0]
            successors.append(successor_id)
        cur.execute(
            '''UPDATE char_phone_check
               SET check_state='superseded', resolved_at=%s,
                   superseded_by_id=%s, claim_token=NULL, claim_owner=NULL,
                   claim_expires_at=NULL, updated_at=CURRENT_TIMESTAMP
               WHERE id=%s''', (now, successor_id, oid))
    return successors


def _fetch_event_for_transition_tx(cur, event_id, character_id, user_id):
    cur.execute(
        f'''SELECT {_EVENT_COLUMNS}
            FROM char_schedule
            WHERE id=%s AND character_id=%s AND user_id=%s
            FOR UPDATE''', (event_id, character_id, user_id))
    row = cur.fetchone()
    return _canonical_event_from_row(row) if row else None


def _has_active_event_tx(cur, character_id, user_id):
    cur.execute(
        '''SELECT 1 FROM char_schedule
           WHERE character_id=%s AND user_id=%s AND status=%s
           LIMIT 1 FOR UPDATE''',
        (character_id, user_id, EVENT_ACTIVE))
    return cur.fetchone() is not None


def _shift_future_flexible_events_tx(cur, character_id, user_id, *,
                                     after, minutes, skip_event_id=None):
    """Deterministically reflow flexible future items after an insertion/extend."""
    if minutes <= 0:
        return []
    cur.execute(
        '''SELECT id, fixedness FROM char_schedule
           WHERE character_id=%s AND user_id=%s
             AND status=%s AND planned_start_at >= %s
             AND (%s IS NULL OR id <> %s)
           ORDER BY planned_start_at ASC
           FOR UPDATE''',
        (character_id, user_id, EVENT_PLANNED, after, skip_event_id, skip_event_id))
    rows = cur.fetchall() or []
    fixed = [event_id for event_id, fixedness in rows if fixedness == FIXED]
    if fixed:
        return None
    ids = [event_id for event_id, _ in rows]
    if not ids:
        return []
    cur.execute(
        '''UPDATE char_schedule
           SET planned_start_at=planned_start_at + (%s * INTERVAL '1 minute'),
               planned_end_at=planned_end_at + (%s * INTERVAL '1 minute'),
               start_time=TO_CHAR((planned_start_at + (%s * INTERVAL '1 minute'))
                                  AT TIME ZONE 'Asia/Shanghai', 'HH24:MI'),
               end_time=TO_CHAR((planned_end_at + (%s * INTERVAL '1 minute'))
                                AT TIME ZONE 'Asia/Shanghai', 'HH24:MI'),
               revision=revision+1
           WHERE id = ANY(%s)''', (minutes, minutes, minutes, minutes, ids))
    cur.execute(
        '''UPDATE char_schedule_phase
           SET planned_start_at=planned_start_at + (%s * INTERVAL '1 minute'),
               planned_end_at=planned_end_at + (%s * INTERVAL '1 minute'),
               revision=revision+1
           WHERE schedule_id = ANY(%s) AND status=%s''',
        (minutes, minutes, ids, EVENT_PLANNED))
    return ids


def _insert_transition_event_tx(cur, character_id, user_id, now, event_spec):
    timezone_value = _canonical_tz()
    start = now.astimezone(timezone_value).replace(second=0, microsecond=0)
    end = start + timedelta(minutes=int(event_spec['duration_minutes']))
    item = {
        'start_time': start.strftime('%H:%M'),
        'end_time': end.strftime('%H:%M'),
        'title': event_spec['title'],
        'location': (event_spec.get('planned_place') or {}).get('area_description', ''),
        'reply_state': event_spec.get('reply_state', REPLY_SOFT_BUSY),
        'category': event_spec.get('category', 'routine'),
        'fixedness': event_spec.get('fixedness', FLEXIBLE),
        'planned_place': event_spec.get('planned_place'),
        'provenance': {
            'kind': 'structured_schedule_transition',
            'stable_preference': False,
            'visited_on_generation': False,
        },
    }
    return _insert_canonical_event_tx(
        cur, character_id, user_id, start.date(), item, now=now,
        provenance=item['provenance'])


def commit_schedule_transition(character_id, user_id, intent, *, now=None,
                              source_event_id=''):
    """Validate and atomically commit a structured action intent.

    This is intentionally the only path from a generator/cognitive proposal to
    schedule facts. No caller may infer this intent from the character's prose.
    """
    normalized = normalize_action_intent(intent)
    if not normalized:
        return {'ok': False, 'reason': 'invalid_structured_action_intent'}
    now = now or datetime.now(_canonical_tz())
    conn = get_conn()
    cur = conn.cursor()
    completed_events = []
    try:
        # Materialize clock-driven event/phase changes in the same transaction
        # as the proposed action. A transition must validate against the
        # authoritative state at commit time, not the state seen before LLM
        # generation began.
        completed_events.extend(
            _advance_world_tx(cur, character_id, user_id, now) or [])
        action = normalized['type']
        event = None
        if action != ACTION_INSERT:
            event = _fetch_event_for_transition_tx(
                cur, normalized['event_id'], character_id, user_id)
            if not event:
                conn.rollback()
                return {'ok': False, 'reason': 'event_not_found'}
            if event['revision'] != normalized['expected_revision']:
                conn.rollback()
                return {
                    'ok': False, 'reason': 'stale_event_revision',
                    'current_revision': event['revision'],
                }
            if event['status'] in EVENT_TERMINAL:
                conn.rollback()
                return {'ok': False, 'reason': 'event_not_mutable'}
            if (action in (ACTION_COMPLETE, ACTION_EXTEND)
                    and event['status'] != EVENT_ACTIVE):
                conn.rollback()
                return {'ok': False, 'reason': 'event_not_active'}

        result_event_id = event['id'] if event else None
        prior_revision = event['revision'] if event else None
        reflowed = []
        if action == ACTION_COMPLETE:
            cur.execute(
                '''UPDATE char_schedule
                   SET status=%s, actual_start_at=COALESCE(actual_start_at, planned_start_at),
                       actual_end_at=%s, revision=revision+1
                   WHERE id=%s
                   RETURNING id, character_id, user_id, planned_place, note,
                             sched_date, visited_at, revision''',
                (EVENT_COMPLETED, now, event['id']))
            completed = cur.fetchone()
            if completed:
                completed_events.append(completed[:7])
                committed_revision = completed[7]
            else:
                committed_revision = prior_revision
            cur.execute(
                '''UPDATE char_schedule_phase
                   SET status=%s,
                       actual_start_at=COALESCE(actual_start_at, planned_start_at),
                       actual_end_at=%s, revision=revision+1
                   WHERE schedule_id=%s AND status=%s''',
                (EVENT_COMPLETED, now, event['id'], EVENT_ACTIVE))
            cur.execute(
                '''UPDATE char_schedule_phase
                   SET status=%s, revision=revision+1
                   WHERE schedule_id=%s AND status=%s''',
                (EVENT_CANCELLED, event['id'], EVENT_PLANNED))
            _reconcile_phone_checks_tx(
                cur, [event['id']], now, user_id=user_id,
                character_id=character_id)
        elif action == ACTION_CANCEL:
            if event['fixedness'] == FIXED:
                conn.rollback()
                return {'ok': False, 'reason': 'fixed_event_requires_external_override'}
            cur.execute(
                '''UPDATE char_schedule
                   SET status=%s, actual_end_at=%s, cancel_reason=%s,
                       revision=revision+1
                   WHERE id=%s RETURNING revision''',
                (EVENT_CANCELLED, now, 'structured_cancel', event['id']))
            committed_revision = cur.fetchone()[0]
            cur.execute(
                '''UPDATE char_schedule_phase
                   SET status=%s, actual_end_at=COALESCE(actual_end_at, %s),
                       revision=revision+1
                   WHERE schedule_id=%s AND status IN (%s,%s)''',
                (EVENT_CANCELLED, now, event['id'], EVENT_PLANNED, EVENT_ACTIVE))
            _reconcile_phone_checks_tx(
                cur, [event['id']], now, user_id=user_id,
                character_id=character_id)
        elif action == ACTION_EXTEND:
            minutes = normalized['extend_minutes']
            old_end = event['planned_end_at']
            reflowed = _shift_future_flexible_events_tx(
                cur, character_id, user_id, after=old_end, minutes=minutes,
                skip_event_id=event['id'])
            if reflowed is None:
                conn.rollback()
                return {'ok': False, 'reason': 'fixed_future_event_conflict'}
            cur.execute(
                '''UPDATE char_schedule
                   SET planned_end_at=planned_end_at + (%s * INTERVAL '1 minute'),
                       end_time=TO_CHAR((planned_end_at + (%s * INTERVAL '1 minute'))
                                        AT TIME ZONE 'Asia/Shanghai', 'HH24:MI'),
                       revision=revision+1
                   WHERE id=%s RETURNING revision''',
                (minutes, minutes, event['id']))
            committed_revision = cur.fetchone()[0]
            cur.execute(
                '''UPDATE char_schedule_phase
                   SET planned_end_at=planned_end_at + (%s * INTERVAL '1 minute'),
                       revision=revision+1
                   WHERE id=(SELECT id FROM char_schedule_phase
                             WHERE schedule_id=%s AND status IN (%s,%s)
                             ORDER BY ordinal DESC LIMIT 1)''',
                (minutes, event['id'], EVENT_PLANNED, EVENT_ACTIVE))
            cur.execute(
                '''SELECT id FROM char_schedule_phase
                   WHERE schedule_id=%s AND status=%s
                   ORDER BY ordinal ASC LIMIT 1''',
                (event['id'], EVENT_ACTIVE))
            active_phase = cur.fetchone()
            _reconcile_phone_checks_tx(
                cur, [event['id']] + reflowed, now,
                user_id=user_id, character_id=character_id,
                replacement_event={
                    'id': event['id'], 'revision': committed_revision,
                    'phase_id': active_phase[0] if active_phase else None,
                })
        elif action == ACTION_RELOCATE:
            reference = normalized.get('planned_place')
            place = _lookup_cached_poi_tx(cur, reference) if reference else None
            if reference and not place:
                conn.rollback()
                return {'ok': False, 'reason': 'unresolved_or_unverified_poi'}
            if place:
                _cache_poi_tx(cur, place)
                location = place['canonical_name']
            else:
                location = _general_area((reference or {}).get('area_description'))
            cur.execute(
                '''UPDATE char_schedule
                   SET planned_place=%s::jsonb, location=%s, revision=revision+1
                   WHERE id=%s RETURNING revision''',
                (json.dumps(place, ensure_ascii=False) if place else None,
                 location, event['id']))
            committed_revision = cur.fetchone()[0]
            cur.execute(
                '''UPDATE char_schedule_phase
                   SET planned_place=%s::jsonb, revision=revision+1
                   WHERE schedule_id=%s AND status IN (%s,%s)''',
                (json.dumps(place, ensure_ascii=False) if place else None,
                 event['id'], EVENT_PLANNED, EVENT_ACTIVE))
            cur.execute(
                '''SELECT id FROM char_schedule_phase
                   WHERE schedule_id=%s AND status=%s
                   ORDER BY ordinal ASC LIMIT 1''',
                (event['id'], EVENT_ACTIVE))
            active_phase = cur.fetchone()
            _reconcile_phone_checks_tx(
                cur, [event['id']], now,
                user_id=user_id, character_id=character_id,
                replacement_event={
                    'id': event['id'], 'revision': committed_revision,
                    'phase_id': active_phase[0] if active_phase else None,
                })
        elif action == ACTION_INSERT:
            if _has_active_event_tx(cur, character_id, user_id):
                conn.rollback()
                return {
                    'ok': False,
                    'reason': 'active_event_requires_explicit_transition',
                }
            event_spec = dict(normalized['event'])
            reference = event_spec.get('planned_place')
            if reference and reference.get('provider'):
                place = _lookup_cached_poi_tx(cur, reference)
                if not place:
                    conn.rollback()
                    return {'ok': False, 'reason': 'unresolved_or_unverified_poi'}
                event_spec['planned_place'] = place
            inserted_id = _insert_transition_event_tx(
                cur, character_id, user_id, now, event_spec)
            result_event_id = inserted_id
            future_after = now + timedelta(minutes=event_spec['duration_minutes'])
            reflowed = _shift_future_flexible_events_tx(
                cur, character_id, user_id, after=now,
                minutes=event_spec['duration_minutes'], skip_event_id=inserted_id)
            if reflowed is None:
                conn.rollback()
                return {'ok': False, 'reason': 'fixed_future_event_conflict'}
            committed_revision = 1
            _reconcile_phone_checks_tx(
                cur, reflowed, now, user_id=user_id,
                character_id=character_id)
        else:
            conn.rollback()
            return {'ok': False, 'reason': 'unsupported_action'}

        cur.execute(
            '''INSERT INTO char_schedule_transition
                  (schedule_id, user_id, character_id, source_event_id, intent,
                   prior_revision, committed_revision, status)
               VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,'committed')''',
            (result_event_id, user_id, character_id, str(source_event_id or '')[:120],
             json.dumps(normalized, ensure_ascii=False), prior_revision,
             committed_revision))
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f'[schedule] transition failed: {exc}')
        return {'ok': False, 'reason': 'transition_error'}
    finally:
        cur.close()
        conn.close()
    if completed_events:
        # A transition can also cross a natural phase/event boundary while
        # generation was in flight. Keep those clock-driven completions
        # idempotent with explicit early completion writes.
        unique_completed = {row[0]: row for row in completed_events}
        _mark_completed_event_visited(list(unique_completed.values()))
    return {
        'ok': True,
        'event_id': result_event_id,
        'action': normalized['type'],
        'revision': committed_revision,
        'reflowed_event_ids': reflowed,
    }


def format_world_prompt(character_id, user_id, now=None):
    """One prompt representation for text, image, delayed, and voice routes."""
    world = get_current_world_state(character_id, user_id, now)
    event = world.get('event')
    activity = world.get('activity')
    phase = world.get('phase')
    if not event or not activity:
        return '', world
    where = f'（在{activity.get("location")}）' if activity.get('location') else ''
    note = f'\n你当时的想法：{activity.get("note")}' if activity.get('note') else ''
    state = world['availability']['reply_state']
    phase_line = ''
    if phase:
        phase_line = (
            f'\n当前 phase：{_hhmm(phase.get("planned_start_at"))}~'
            f'{_hhmm(phase.get("planned_end_at"))} {phase.get("title")}'
            f'（{state}）')
    intent_hint = (
        '\n如果你要让此事提前完成、延长、取消、换地点或插入新安排，'
        '只能额外输出结构化 schedule_action_intent；没有已提交 intent 时，'
        '绝不能说它已经结束。'
    )
    return (
        '\n【你此刻正在做的事——canonical world state】\n'
        f'event_id={event["id"]} revision={event["revision"]} status={event["status"]}\n'
        f'{activity["start_time"]}~{activity["end_time"]} {activity["title"]}{where}'
        f'{note}{phase_line}{intent_hint}\n'
        '这是唯一的当前现实：问在干嘛时照实说；不要另编一个，也不要用台词改写事实。',
        world,
    )


# Historical implementation is retained only for forensic migration support.
# Public APIs below do not fall back to it: initialized deployments have one
# canonical reader and one canonical writer.
_legacy_save_schedule = _raw_legacy_save_schedule_impl
_legacy_get_schedule = _raw_legacy_get_schedule_impl
_legacy_get_current_activity = _raw_legacy_get_current_activity_impl
_legacy_has_schedule = _raw_legacy_has_schedule_impl
_legacy_clear_schedule = _raw_legacy_clear_schedule_impl
_legacy_get_next_free_time = _raw_legacy_get_next_free_time_impl


def _canonical_day_exists(character_id, user_id, sched_date):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute('''SELECT 1 FROM char_schedule
                       WHERE character_id=%s AND user_id=%s AND sched_date=%s
                         AND planned_start_at IS NOT NULL LIMIT 1''',
                    (character_id, user_id, sched_date))
        return cur.fetchone() is not None
    finally:
        cur.close()
        conn.close()


def save_schedule(character_id, user_id, sched_date, items):
    return len(save_canonical_schedule(
        character_id, user_id, sched_date, items, force=True,
        provenance={'source': 'save_schedule_compat'}))


def get_schedule(character_id, user_id, sched_date):
    return get_canonical_schedule(character_id, user_id, sched_date)


def get_current_activity(character_id, user_id, now: datetime):
    return get_current_world_state(character_id, user_id, now).get('activity')


def has_schedule(character_id, user_id, sched_date):
    return canonical_has_schedule(character_id, user_id, sched_date)


def clear_schedule(character_id, user_id, sched_date):
    return clear_canonical_schedule(character_id, user_id, sched_date)


def get_next_free_time(character_id, user_id, now: datetime, *, world=None):
    world = world or get_current_world_state(character_id, user_id, now)
    if world['availability']['can_reply']:
        return now.strftime('%H:%M')
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT phase.planned_start_at, phase.planned_end_at,
                      phase.reply_state
               FROM char_schedule_phase phase
               JOIN char_schedule event ON event.id=phase.schedule_id
               WHERE event.character_id=%s AND event.user_id=%s
                 AND event.status IN (%s,%s)
                 AND phase.status IN (%s,%s)
                 AND phase.planned_end_at > %s
               ORDER BY phase.planned_start_at ASC''',
            (character_id, user_id, EVENT_PLANNED, EVENT_ACTIVE,
             EVENT_PLANNED, EVENT_ACTIVE, now))
        rows = cur.fetchall() or []
    finally:
        cur.close()
        conn.close()
    for start_at, _end_at, reply_state in rows:
        if normalize_reply_state(reply_state, True) == REPLY_FREE:
            return _hhmm(max(start_at, now))
    return _hhmm(rows[-1][1]) if rows else None
