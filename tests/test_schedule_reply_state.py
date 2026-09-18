import importlib.util
import json
import os
import sys
import threading
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


fake_db = types.ModuleType('db')
fake_db.get_conn = lambda: (_ for _ in ()).throw(
    AssertionError('database should be patched by tests that need it')
)
sys.modules.setdefault('db', fake_db)

import db_schedule  # noqa: E402
import reply_availability  # noqa: E402


def load_schedule_engine():
    modules = {
        'characters': stub('characters', get_character=Mock()),
        'characters_data': stub('characters_data'),
        'characters_data._loader': stub(
            'characters_data._loader', load_core=Mock(return_value={})),
        'character_rhythm': stub(
            'character_rhythm',
            get_rhythm_text=Mock(return_value=''),
            get_sleep_window=Mock(return_value=None),
        ),
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            'schedule_engine_under_test',
            os.path.join(BACKEND, 'schedule_engine.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class PhoneCheckStore:
    def __init__(self):
        self.next_id = 1
        self.rows = {}
        self.schedule_rows = []
        self.sql = []
        self.lock = threading.Lock()


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._one = None
        self._many = []
        self.rowcount = 0

    def _find_by_id(self, oid):
        for row in self.store.rows.values():
            if row['id'] == oid:
                return row
        return None

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.store.sql.append((compact, tuple(params or ())))
        self._one = None
        self._many = []
        self.rowcount = 0
        params = tuple(params or ())

        if 'phone_check:recover' in compact:
            now = params[0] if params else None
            n = 0
            for row in self.store.rows.values():
                if (row.get('check_state') == 'processing'
                        and row.get('claim_expires_at')
                        and now
                        and row['claim_expires_at'] <= now
                        and not row.get('resolved_at')):
                    row['check_state'] = 'pending'
                    row['claimed_at'] = None
                    row['claim_token'] = None
                    row['claim_owner'] = None
                    row['claim_expires_at'] = None
                    n += 1
            self.rowcount = n
            return

        if 'phone_check:supersede' in compact:
            n = 0
            keep_key = None
            hhmm = None
            today = None
            if len(params) >= 9:
                _now, user_id, character_id, today, start, end, _t2, _t3, hhmm = params
                keep_key = (user_id, character_id, today, start, end)
            elif len(params) >= 6:
                _now, user_id, character_id, today, _t2, hhmm = params
            for key, row in list(self.store.rows.items()):
                if keep_key and key == keep_key:
                    continue
                if row.get('check_state') in (
                        'consumed', 'resolved', 'expired', 'superseded'):
                    continue
                if (row.get('pending_count') or 0) > 0:
                    continue
                ended = False
                if today is not None and hhmm is not None:
                    if row.get('sched_date') < today:
                        ended = True
                    elif row.get('sched_date') == today and row.get('end_time') <= hhmm:
                        ended = True
                else:
                    ended = True
                if not ended:
                    continue
                row['check_state'] = 'superseded'
                row['resolved_at'] = row.get('resolved_at') or (
                    params[0] if params else None)
                row['claimed_at'] = None
                row['claim_token'] = None
                row['claim_owner'] = None
                row['claim_expires_at'] = None
                n += 1
            self.rowcount = n
            return

        if 'phone_check:arm_ended' in compact:
            n = 0
            due_at = params[0] if params else None
            keep_key = None
            hhmm = None
            today = None
            if len(params) >= 10:
                due_at, _due2, user_id, character_id, today, start, end, _t2, _t3, hhmm = params
                keep_key = (user_id, character_id, today, start, end)
            elif len(params) >= 7:
                due_at, _due2, user_id, character_id, today, _t2, hhmm = params
            for key, row in list(self.store.rows.items()):
                if keep_key and key == keep_key:
                    continue
                if row.get('resolved_at'):
                    continue
                if (row.get('pending_count') or 0) <= 0:
                    continue
                if row.get('check_state') in (
                        'consumed', 'resolved', 'expired', 'superseded'):
                    continue
                ended = False
                if today is not None and hhmm is not None:
                    if row.get('sched_date') < today:
                        ended = True
                    elif row.get('sched_date') == today and row.get('end_time') <= hhmm:
                        ended = True
                else:
                    ended = True
                if not ended:
                    continue
                existing = row.get('next_phone_check_at')
                if existing is None or (due_at is not None and existing > due_at):
                    row['next_phone_check_at'] = due_at
                n += 1
            self.rowcount = n
            return

        if 'phone_check:release' in compact:
            oid, token = params
            row = self._find_by_id(oid)
            if (row and row.get('claim_token') == token
                    and row.get('check_state') == 'processing'
                    and not row.get('resolved_at')):
                row['check_state'] = 'pending'
                row['can_reply'] = False
                row['claimed_at'] = None
                row['claim_token'] = None
                row['claim_owner'] = None
                row['claim_expires_at'] = None
                self.rowcount = 1
            return

        if 'phone_check:claim' in compact:
            with self.store.lock:
                (claimed_at, token, owner, expires, seen_at,
                 oid, now_due, now_stale) = params
                row = self._find_by_id(oid)
                self.rowcount = 0
                if not row or row.get('resolved_at'):
                    return
                next_at = row.get('next_phone_check_at')
                if next_at is None or next_at > now_due:
                    return
                state = row.get('check_state') or 'pending'
                stale = (
                    state == 'processing'
                    and row.get('claim_expires_at')
                    and row['claim_expires_at'] <= now_stale
                )
                if state not in ('pending', 'deferred') and not stale:
                    return
                row['check_state'] = 'processing'
                row['claimed_at'] = claimed_at
                row['claim_token'] = token
                row['claim_owner'] = owner
                row['claim_expires_at'] = expires
                row['seen'] = True
                row['seen_at'] = seen_at
                row['seen_watermark'] = row.get('pending_count') or 0
                self.rowcount = 1
                self._one = (
                    row['id'], row['pending_count'], row.get('pending_text'),
                    row.get('event_meta'), row.get('reply_state'),
                    row.get('seen_watermark', 0), row.get('next_phone_check_at'),
                    row.get('fallback_promise_id'),
                    row.get('user_id'), row.get('character_id'),
                    row.get('first_source_event_id'), row.get('last_source_event_id'),
                    row.get('activity_title'), row.get('start_time'),
                    row.get('end_time'), row.get('sched_date'),
                )
                return

        if 'phone_check:finish_reply' in compact:
            now, oid, token = params
            row = self._find_by_id(oid)
            if (row and row.get('claim_token') == token
                    and row.get('check_state') == 'processing'):
                row['check_state'] = 'consumed'
                row['can_reply'] = True
                row['next_phone_check_at'] = None
                row['resolved_at'] = now
                row['claimed_at'] = None
                row['claim_token'] = None
                row['claim_owner'] = None
                row['claim_expires_at'] = None
                self.rowcount = 1
            return

        if 'phone_check:finish_defer' in compact:
            new_next, oid, token = params
            row = self._find_by_id(oid)
            if (row and row.get('claim_token') == token
                    and row.get('check_state') == 'processing'):
                row['check_state'] = 'pending'
                row['can_reply'] = False
                row['next_phone_check_at'] = new_next
                row['fallback_promise_id'] = None
                row['claimed_at'] = None
                row['claim_token'] = None
                row['claim_owner'] = None
                row['claim_expires_at'] = None
                self.rowcount = 1
            return

        if compact.startswith('SELECT id FROM char_phone_check'):
            now_due = params[1] if len(params) > 1 else None
            today = params[2] if len(params) > 2 else None
            hhmm = params[4] if len(params) > 4 else None
            ids = []
            for row in self.store.rows.values():
                if row.get('resolved_at'):
                    continue
                if (row.get('pending_count') or 0) <= 0:
                    continue
                state = row.get('check_state') or 'pending'
                stale = (
                    state == 'processing'
                    and row.get('claim_expires_at')
                    and now_due
                    and row['claim_expires_at'] <= now_due
                )
                if state not in ('pending', 'deferred') and not stale:
                    continue
                due = False
                next_at = row.get('next_phone_check_at')
                if next_at is not None and now_due is not None and next_at <= now_due:
                    due = True
                if today is not None and hhmm is not None:
                    if row.get('sched_date') < today:
                        due = True
                    elif row.get('sched_date') == today and row.get('end_time') <= hhmm:
                        due = True
                if due:
                    ids.append((row['id'],))
            self._many = ids[: int(params[-1] if params else 20)]
            return

        if compact.startswith('SELECT id, seen, can_reply'):
            user_id, character_id, sched_date, start_time, end_time = params
            row = self.store.rows.get(
                (user_id, character_id, sched_date, start_time, end_time))
            if row:
                self._one = (
                    row['id'], row['seen'], row['can_reply'],
                    row['pending_count'], row['pending_text'],
                    row['event_meta'], row.get('fallback_promise_id'),
                    row.get('next_phone_check_at'), row.get('seen_at'),
                    row.get('resolved_at'), row.get('seen_watermark', 0),
                    row.get('check_state', 'pending'),
                )
            return

        if compact.startswith('INSERT INTO char_phone_check'):
            (user_id, character_id, schedule_id, sched_date, start_time,
             end_time, activity_title, reply_state, seen, can_reply,
             pending_count, first_source_event_id, last_source_event_id,
             pending_text, event_meta, next_phone_check_at,
             seen_watermark) = params[:17]
            check_state = params[17] if len(params) > 17 else 'pending'
            row = {
                'id': self.store.next_id,
                'user_id': user_id,
                'character_id': character_id,
                'schedule_id': schedule_id,
                'sched_date': sched_date,
                'start_time': start_time,
                'end_time': end_time,
                'activity_title': activity_title,
                'reply_state': reply_state,
                'seen': seen,
                'can_reply': can_reply,
                'pending_count': pending_count,
                'first_source_event_id': first_source_event_id,
                'last_source_event_id': last_source_event_id,
                'pending_text': pending_text,
                'event_meta': event_meta,
                'fallback_promise_id': None,
                'next_phone_check_at': next_phone_check_at,
                'seen_at': None,
                'resolved_at': None,
                'seen_watermark': seen_watermark or 0,
                'check_state': check_state,
                'claimed_at': None,
                'claim_token': None,
                'claim_owner': None,
                'claim_expires_at': None,
            }
            self.store.next_id += 1
            self.store.rows[
                (user_id, character_id, sched_date, start_time, end_time)
            ] = row
            self._one = (row['id'],)
            self.rowcount = 1
            return

        if 'SET pending_count = pending_count + 1' in compact:
            if compact.count('%s') == 2:
                last_source_event_id, oid = params
                pending_text = None
                event_meta = None
            else:
                last_source_event_id, pending_text, event_meta, oid = params
            row = self._find_by_id(oid)
            if row:
                row['pending_count'] = (row.get('pending_count') or 0) + 1
                row['last_source_event_id'] = last_source_event_id
                if pending_text is not None:
                    row['pending_text'] = pending_text
                if event_meta is not None:
                    row['event_meta'] = event_meta
                self.rowcount = 1
                self._one = (row['pending_count'],)
            return

        if compact.startswith('UPDATE char_phone_check SET pending_count='):
            pending_count, last_source_event_id, pending_text, event_meta, oid = params
            row = self._find_by_id(oid)
            if row:
                row['pending_count'] = pending_count
                row['last_source_event_id'] = last_source_event_id
                row['pending_text'] = pending_text
                row['event_meta'] = event_meta
                self.rowcount = 1
            return

        if compact.startswith('UPDATE char_phone_check SET seen=FALSE'):
            (first_source_event_id, last_source_event_id, pending_text,
             event_meta, next_phone_check_at, oid) = params
            row = self._find_by_id(oid)
            if row:
                row.update({
                    'seen': False,
                    'can_reply': False,
                    'seen_at': None,
                    'seen_watermark': 0,
                    'resolved_at': None,
                    'pending_count': 1,
                    'first_source_event_id': first_source_event_id,
                    'last_source_event_id': last_source_event_id,
                    'pending_text': pending_text,
                    'event_meta': event_meta,
                    'fallback_promise_id': None,
                    'next_phone_check_at': next_phone_check_at,
                    'check_state': 'pending',
                    'claimed_at': None,
                    'claim_token': None,
                    'claim_owner': None,
                    'claim_expires_at': None,
                })
                self.rowcount = 1
            return

        if 'COALESCE(next_phone_check_at' in compact:
            now, oid, today, _t2, hhmm = params
            row = self._find_by_id(oid)
            if row and not row.get('resolved_at') and row.get('next_phone_check_at') is None:
                ended = False
                if row.get('sched_date') < today:
                    ended = True
                elif row.get('sched_date') == today and row.get('end_time') <= hhmm:
                    ended = True
                if ended:
                    row['next_phone_check_at'] = now
                    self.rowcount = 1
            return

        if compact.startswith('UPDATE char_phone_check SET next_phone_check_at='):
            next_phone_check_at, oid = params
            row = self._find_by_id(oid)
            if row:
                if ('check_state IN' in compact
                        and row.get('check_state') not in ('pending', 'deferred')):
                    return
                row['next_phone_check_at'] = next_phone_check_at
                self.rowcount = 1
            return

        if compact.startswith('UPDATE char_phone_check SET seen=TRUE'):
            if 'resolved_at=%s' in compact:
                seen_at, resolved_at, seen_watermark, oid = params
                row = self._find_by_id(oid)
                if row:
                    row['seen'] = True
                    row['seen_at'] = seen_at
                    row['can_reply'] = True
                    row['next_phone_check_at'] = None
                    row['resolved_at'] = resolved_at
                    row['seen_watermark'] = seen_watermark
                    row['check_state'] = 'consumed'
                    self.rowcount = 1
            else:
                seen_at, next_phone_check_at, seen_watermark, oid = params
                row = self._find_by_id(oid)
                if row:
                    row['seen'] = True
                    row['seen_at'] = seen_at
                    row['can_reply'] = False
                    row['next_phone_check_at'] = next_phone_check_at
                    row['seen_watermark'] = seen_watermark
                    row['fallback_promise_id'] = None
                    row['check_state'] = 'pending'
                    self.rowcount = 1
            return

        if compact.startswith('UPDATE char_phone_check SET fallback_promise_id='):
            return

        if 'SET event_meta = LEFT' in compact or 'event_meta ||' in compact:
            extra, _extra2, oid = params
            row = self._find_by_id(oid)
            if row:
                existing = row.get('event_meta') or ''
                row['event_meta'] = extra if not existing else (existing + '\n' + extra)
                self.rowcount = 1
            return

        if compact.startswith('UPDATE char_phone_check SET resolved_at='):
            oid = params[0]
            row = self._find_by_id(oid)
            if (row and not row.get('resolved_at')
                    and row.get('check_state') not in (
                        'expired', 'superseded')):
                row['resolved_at'] = row.get('claimed_at') or True
                row['check_state'] = 'resolved'
                row['claimed_at'] = None
                row['claim_token'] = None
                row['claim_owner'] = None
                row['claim_expires_at'] = None
                self.rowcount = 1
            return

        if compact.startswith('SELECT id, start_time, end_time, title'):
            character_id, user_id, sched_date, hhmm, hhmm2 = params
            matched = []
            for r in self.store.schedule_rows:
                if (r['character_id'] == character_id and r['user_id'] == user_id
                        and r['sched_date'] == sched_date
                        and r['start_time'] <= hhmm < r['end_time']):
                    matched.append((
                        r['id'], r['start_time'], r['end_time'], r['title'],
                        r.get('location', ''), r.get('note', ''),
                        r.get('can_reply', False), r.get('reply_state', ''),
                    ))
            self._many = matched
            self._one = matched[0] if matched else None
            return

        raise AssertionError(f'unhandled SQL: {compact}')

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)

    def close(self):
        pass


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


class ScheduleReplyStateTests(unittest.TestCase):
    def test_sanitize_preserves_real_soft_90_and_hard_10_minutes(self):
        schedule_engine = load_schedule_engine()
        items = schedule_engine._sanitize([
            {
                'start_time': '09:00',
                'end_time': '10:30',
                'title': '备课',
                'location': '办公室',
                'note': '堆着一摞材料',
                'reply_state': 'soft_busy',
            },
            {
                'start_time': '10:30',
                'end_time': '10:40',
                'title': '洗澡',
                'location': '家',
                'note': '十分钟冲掉一身汗',
                'reply_state': 'hard_busy',
            },
        ], character_id='gojo')

        self.assertEqual([i['reply_state'] for i in items],
                         ['soft_busy', 'hard_busy'])
        self.assertEqual([i['can_reply'] for i in items], [False, False])
        self.assertEqual(schedule_engine._dur(items[0]), 90)
        self.assertEqual(schedule_engine._dur(items[1]), 10)

    def test_ten_messages_do_not_change_existing_next_phone_check_at(self):
        store = PhoneCheckStore()
        now = datetime(2026, 9, 16, 9, 44, tzinfo=timezone.utc)
        activity = {
            'id': 7,
            'start_time': '09:00',
            'end_time': '10:30',
            'title': '备课',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        fixed_check = now + timedelta(minutes=13)  # 09:57
        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=fixed_check), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (fixed_check, False)):
            first = db_schedule.decide_phone_check(
                'gojo', 'u1', now, activity,
                source_event_id='evt-1', pending_text='A')
            locked = first['next_phone_check_at']
            for i in range(2, 11):
                later = now + timedelta(minutes=i)
                decision = db_schedule.decide_phone_check(
                    'gojo', 'u1', later, activity,
                    source_event_id=f'evt-{i}', pending_text=chr(64 + i))
                self.assertEqual(decision['next_phone_check_at'], locked)
                self.assertFalse(decision['seen'])
                self.assertFalse(decision['can_reply'])
                self.assertTrue(decision['reused'])

        row = next(iter(store.rows.values()))
        self.assertEqual(row['pending_count'], 10)
        self.assertEqual(row['next_phone_check_at'], locked)

    def test_defer_generates_second_phone_check(self):
        store = PhoneCheckStore()
        now = datetime(2026, 9, 16, 9, 44, tzinfo=timezone.utc)
        first_check = datetime(2026, 9, 16, 9, 57, tzinfo=timezone.utc)
        second_check = datetime(2026, 9, 16, 10, 19, tzinfo=timezone.utc)
        activity = {
            'id': 7,
            'start_time': '09:00',
            'end_time': '10:30',
            'title': '备课',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        store.schedule_rows = [{
            'id': 7, 'character_id': 'gojo', 'user_id': 'u1',
            'sched_date': now.date(), 'start_time': '09:00', 'end_time': '10:30',
            'title': '备课', 'can_reply': False, 'reply_state': 'soft_busy',
        }]
        samples = [first_check, second_check]

        def _sample(now_arg, activity_arg, after=None):
            return samples.pop(0)

        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          side_effect=_sample), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2], False)), \
             patch.object(db_schedule.random, 'random', return_value=0.99):
            created = db_schedule.decide_phone_check(
                'gojo', 'u1', now, activity,
                source_event_id='evt-1', pending_text='你好')
            self.assertFalse(created['seen'])
            self.assertEqual(created['next_phone_check_at'], first_check)

            inbound_due = db_schedule.decide_phone_check(
                'gojo', 'u1', first_check, activity,
                source_event_id='evt-2', pending_text='还在吗')
            self.assertFalse(inbound_due['can_reply'])
            self.assertFalse(inbound_due.get('check_consumed'))

            deferred = db_schedule.evaluate_due_phone_check(
                inbound_due['opportunity_id'], first_check)
            self.assertEqual(deferred['action'], 'defer')
            self.assertEqual(deferred['next_phone_check_at'], second_check)

        row = next(iter(store.rows.values()))
        self.assertEqual(row['next_phone_check_at'], second_check)
        self.assertTrue(row['seen'])
        self.assertFalse(row['can_reply'])
        self.assertEqual(row['seen_watermark'], 2)

    def test_new_message_after_defer_does_not_inherit_bundle_seen(self):
        """A/B/C 第一次 check 已 seen+defer；D 必须等下一次 check 才 seen。"""
        store = PhoneCheckStore()
        t0 = datetime(2026, 9, 16, 9, 44, tzinfo=timezone.utc)
        first_check = datetime(2026, 9, 16, 9, 57, tzinfo=timezone.utc)
        second_check = datetime(2026, 9, 16, 10, 19, tzinfo=timezone.utc)
        activity = {
            'id': 7,
            'start_time': '09:00',
            'end_time': '10:30',
            'title': '备课',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        store.schedule_rows = [{
            'id': 7, 'character_id': 'gojo', 'user_id': 'u1',
            'sched_date': t0.date(), 'start_time': '09:00', 'end_time': '10:30',
            'title': '备课', 'can_reply': False, 'reply_state': 'soft_busy',
        }]
        samples = [first_check, second_check]

        def _sample(now_arg, activity_arg, after=None):
            return samples.pop(0)

        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          side_effect=_sample), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2], False)), \
             patch.object(db_schedule.random, 'random', return_value=0.99):
            db_schedule.decide_phone_check(
                'gojo', 'u1', t0, activity, source_event_id='A', pending_text='A')
            db_schedule.decide_phone_check(
                'gojo', 'u1', t0 + timedelta(minutes=4), activity,
                source_event_id='B', pending_text='B')
            due_inbound = db_schedule.decide_phone_check(
                'gojo', 'u1', first_check, activity,
                source_event_id='C', pending_text='C')
            seen_bundle = db_schedule.evaluate_due_phone_check(
                due_inbound['opportunity_id'], first_check)
            late = db_schedule.decide_phone_check(
                'gojo', 'u1', first_check + timedelta(minutes=1), activity,
                source_event_id='D', pending_text='D')

        self.assertEqual(seen_bundle['action'], 'defer')
        row = next(iter(store.rows.values()))
        self.assertTrue(row['seen'])
        self.assertFalse(row['can_reply'])
        self.assertEqual(row['seen_watermark'], 3)
        self.assertFalse(late['seen'])
        self.assertFalse(late['can_reply'])
        self.assertIsNone(late['seen_at'])
        self.assertEqual(late['seen_watermark'], 3)
        self.assertEqual(late['pending_count'], 4)
        self.assertEqual(late['next_phone_check_at'], second_check)

    def test_hard_busy_covering_check_postpones(self):
        store = PhoneCheckStore()
        now = datetime(2026, 9, 16, 9, 50, tzinfo=timezone.utc)
        original = datetime(2026, 9, 16, 9, 55, tzinfo=timezone.utc)
        postponed = datetime(2026, 9, 16, 10, 11, tzinfo=timezone.utc)
        activity = {
            'id': 7,
            'start_time': '09:00',
            'end_time': '10:30',
            'title': '备课',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=original), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=[
                              (original, False),   # on create
                              (postponed, True),   # on consume at due time
                          ]):
            db_schedule.decide_phone_check(
                'gojo', 'u1', now, activity,
                source_event_id='evt-1', pending_text='hi')
            decision = db_schedule.decide_phone_check(
                'gojo', 'u1', original, activity,
                source_event_id='evt-2', pending_text='again')

        self.assertFalse(decision['seen'])
        self.assertFalse(decision['can_reply'])
        self.assertTrue(decision.get('postponed_for_hard_busy'))
        self.assertEqual(decision['next_phone_check_at'], postponed)

    def test_phone_check_bundle_context_includes_visual_summary(self):
        bundle = {
            'id': 3,
            'activity_title': '备课',
            'reply_state': 'soft_busy',
            'pending_count': 1,
            'pending_text': '📷 看这个',
            'event_meta': json.dumps({
                'kind': 'image',
                'visual_summary': '蓝色马克杯，杯沿有裂纹',
                'source_event_id': 'img-1',
            }, ensure_ascii=False),
        }
        ctx = reply_availability.format_pending_bundle_context(bundle)
        self.assertIn('蓝色马克杯', ctx)
        self.assertIn('image', ctx)
        self.assertIn('内部上下文', ctx)


class BusyImageVisionTests(unittest.TestCase):
    def test_busy_image_runs_vision_before_availability_and_keeps_unseen(self):
        import asyncio
        import importlib.util

        modules = {
            'config': stub(
                'config', ANTHROPIC_KEY='x', EMOTIONS={'平静'}, TTS_PROVIDER='fish',
                DEFAULT_CHARACTER_ID='gojo', MODEL_MAIN='m'),
            'db': stub('db', get_conn=Mock()),
            'utils': stub(
                'utils', ingest_model_output=Mock(), finalize_user_messages=Mock(),
                valid_reply_msg=Mock(), commit_ready_msgs=Mock(),
                extract_json=lambda raw: {'visual_summary': '蓝色马克杯，杯沿有裂纹'}),
            'ai_client': stub(
                'ai_client', extract_text=lambda r: r,
                response_metadata=Mock()),
            'tts': stub('tts', tts_to_b64=Mock(return_value='')),
            'prompt': stub(
                'prompt', build_system_blocks=Mock(return_value=[]),
                log_cache_usage=Mock()),
            'user_memory': stub(
                'user_memory',
                save_short_memory=Mock(),
                save_user_short_memory_once=Mock(return_value=True),
                get_short_memory=Mock(return_value=[]),
                get_short_memory_for_prompt=Mock(return_value=[]),
                attach_short_memory_event_meta=Mock(return_value=True),
                update_chat_days=Mock(return_value=1),
            ),
            'memory_jobs': stub('memory_jobs', enqueue_private_extraction=Mock()),
            'db_schedule': stub(
                'db_schedule', merge_phone_check_event_meta=Mock(return_value=True)),
            'temporal_awareness': stub(
                'temporal_awareness',
                get_temporal_snapshot=Mock(return_value={}),
                record_turn=Mock(),
                record_user_message=Mock(),
            ),
            'characters': stub(
                'characters',
                get_character=Mock(return_value={'id': 'gojo', 'voice_id': 'v'}),
            ),
            'tasks': stub(
                'tasks',
                find_duplicate_task=Mock(),
                find_and_delete_tasks_by_keyword=Mock(),
                delete_latest_task=Mock(),
            ),
            'task_dedup': stub('task_dedup', find_similar_task=Mock()),
            'fastapi': stub('fastapi', APIRouter=lambda: types.SimpleNamespace(
                post=lambda *a, **k: (lambda f: f),
            )),
            'fastapi.responses': stub(
                'fastapi.responses',
                JSONResponse=lambda body, status_code=200: types.SimpleNamespace(
                    body=json.dumps(body, default=str).encode(),
                    status_code=status_code,
                    data=body),
            ),
            'anthropic': stub(
                'anthropic',
                Anthropic=Mock(return_value=Mock(messages=Mock(create=Mock(
                    return_value='{"visual_summary":"蓝色马克杯，杯沿有裂纹"}'
                )))),
            ),
            'db_chat_media': stub(
                'db_chat_media',
                persist_image=Mock(return_value={
                    'id': 'media-busy-1',
                    'media_kind': 'image',
                    'object_key': 'chat-media/u1/gojo/img-busy-1/original.png',
                    'mime_type': 'image/png',
                }),
                public_media=lambda record: None if not record else {
                    'id': record['id'],
                    'kind': 'image',
                    'url': 'https://r2.example/signed',
                    'mime_type': record.get('mime_type') or 'image/png',
                },
            ),
            'media_storage': stub(
                'media_storage',
                is_configured=Mock(return_value=True),
                signed_get_url=Mock(return_value='https://r2.example/signed'),
                MediaStorageError=RuntimeError,
            ),
        }

        availability = {
            'can_reply': False,
            'seen': False,
            'reply_state': 'soft_busy',
            'activity': {'title': '备课', 'location': '办公室', 'end_time': '10:30'},
            'free_at': '10:30',
            'opportunity_id': 9,
            'seen_at': None,
            'next_phone_check_at': datetime(2026, 9, 16, 9, 57, tzinfo=timezone.utc),
        }
        captured = {}

        def _check(character_id, user_id, **kwargs):
            captured['event_meta'] = kwargs.get('event_meta')
            captured['pending_text'] = kwargs.get('pending_text')
            return availability

        modules['reply_availability'] = stub(
            'reply_availability', check_reply_availability=_check)

        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location(
                'route_image_under_test',
                os.path.join(BACKEND, 'route_image.py'))
            route_image = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(route_image)

            tiny_png = (
                'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8'
                'z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
            )
            response = asyncio.run(route_image.chat_image({
                'user_id': 'u1',
                'character_id': 'gojo',
                'image_base64': tiny_png,
                'text': '看这个杯子',
                'source_event_id': 'img-busy-1',
            }))

        body = json.loads(response.body)
        self.assertTrue(body['busy'])
        self.assertFalse(body['seen'])
        self.assertIsNone(body.get('seen_at'))
        self.assertIn('蓝色马克杯', body['visual_summary'])
        self.assertNotIn('visual_summary', captured.get('event_meta') or {})
        self.assertNotIn('【图片摘要】', captured.get('pending_text') or '')
        self.assertIn('看这个杯子', captured.get('pending_text') or '')
        self.assertIn('蓝色马克杯', body['event_meta']['visual_summary'])


class ShortMemoryPromptIsolationTests(unittest.TestCase):
    def test_visual_summary_is_assembled_at_prompt_time_not_in_content(self):
        from user_memory import assemble_prompt_content, format_media_short_memory

        raw = '📷 看这个'
        assembled = assemble_prompt_content(raw, {
            'kind': 'image',
            'visual_summary': '蓝色马克杯，杯沿有裂纹',
        })
        self.assertEqual(raw, '📷 看这个')
        self.assertNotIn('【图片摘要】', raw)
        self.assertIn('📷 看这个', assembled)
        self.assertIn('【图片摘要】蓝色马克杯', assembled)

        media = format_media_short_memory(
            '📷 看这个', '蓝色马克杯，杯沿有裂纹', {'kind': 'image'})
        self.assertIn('【图片摘要】蓝色马克杯', media)

    def test_delete_chatlog_does_not_clear_short_memory_source(self):
        src = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        self.assertIn('get_short_memory_for_prompt', src)
        img_src = Path(BACKEND, 'route_image.py').read_text(encoding='utf-8')
        self.assertIn('user_id, display_text, character_id', img_src)
        self.assertNotIn('format_media_short_memory(', img_src)

    def test_route_chat_prompt_prefers_short_memory_not_chatlog(self):
        from pathlib import Path
        src = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        self.assertIn('get_short_memory_for_prompt', src)
        self.assertNotIn(
            'Prefer durable chat_log because it contains image summaries', src)
        img_src = Path(BACKEND, 'route_image.py').read_text(encoding='utf-8')
        self.assertIn('get_short_memory_for_prompt', img_src)
        self.assertIn('_analyze_visual_summary', img_src)
        self.assertIn('busy/pending：单独做 visual summary', img_src)
        self.assertIn('free / immediate reply：一次 Vision', img_src)


class ActivityPhoneProfileTests(unittest.TestCase):
    def test_meeting_is_soft_even_if_llm_said_hard(self):
        schedule_engine = load_schedule_engine()
        items = schedule_engine._sanitize([
            {
                'start_time': '14:00',
                'end_time': '15:00',
                'title': '开会',
                'location': '会议室',
                'note': '例会',
                'reply_state': 'hard_busy',
            },
            {
                'start_time': '15:00',
                'end_time': '16:30',
                'title': '备课',
                'reply_state': 'hard_busy',
            },
            {
                'start_time': '16:30',
                'end_time': '17:30',
                'title': '处理报告',
                'reply_state': 'hard_busy',
            },
            {
                'start_time': '17:30',
                'end_time': '18:30',
                'title': '出任务',
                'reply_state': 'hard_busy',
            },
        ])
        states = {i['title']: i['reply_state'] for i in items}
        self.assertEqual(states['开会'], 'soft_busy')
        self.assertEqual(states['备课'], 'soft_busy')
        self.assertEqual(states['处理报告'], 'soft_busy')
        self.assertEqual(states['出任务'], 'hard_busy')

    def test_meeting_lesson_report_profiles_differ(self):
        import activity_phone
        meeting = activity_phone.profile_for_title('开会')
        prep = activity_phone.profile_for_title('备课')
        report = activity_phone.profile_for_title('处理报告')
        for profile in (meeting, prep, report):
            self.assertEqual(profile.busy_state, 'soft_busy')
        self.assertNotEqual(
            (meeting.check_interval_min, meeting.check_interval_max),
            (prep.check_interval_min, prep.check_interval_max),
        )
        self.assertNotEqual(
            (prep.check_interval_min, prep.check_interval_max),
            (report.check_interval_min, report.check_interval_max),
        )
        captured = []

        def fake_randint(lo, hi):
            captured.append((lo, hi))
            return lo

        now = datetime(2026, 9, 16, 14, 5, tzinfo=timezone.utc)
        with patch.object(db_schedule.random, 'randint', side_effect=fake_randint):
            db_schedule.sample_next_phone_check_at(
                now, {'title': '开会', 'end_time': '15:00', 'reply_state': 'soft_busy'})
            db_schedule.sample_next_phone_check_at(
                now, {'title': '备课', 'end_time': '15:00', 'reply_state': 'soft_busy'})
            db_schedule.sample_next_phone_check_at(
                now, {'title': '处理报告', 'end_time': '16:00', 'reply_state': 'soft_busy'})
        self.assertEqual(captured[0], (5, 18))
        self.assertEqual(captured[1], (12, 30))
        self.assertEqual(captured[2], (15, 35))

    def test_character_modifier_does_not_branch_on_name(self):
        import activity_phone
        fake_chars = types.SimpleNamespace(get_character=lambda cid: {
            'phone_behavior': {
                'check_interval_scale': 0.6,
                'quick_reply_bonus': 0.2,
            }
        })
        with patch.dict(sys.modules, {'characters': fake_chars}):
            scaled = activity_phone.apply_character_modifier(
                activity_phone.PROFILES['meeting'], 'not-a-named-branch')
        base = activity_phone.PROFILES['meeting']
        self.assertLess(scaled.check_interval_min, base.check_interval_min)
        self.assertGreater(scaled.quick_reply_probability, base.quick_reply_probability)
        src = Path(BACKEND, 'activity_phone.py').read_text(encoding='utf-8')
        self.assertNotIn('if character ==', src)
        self.assertNotIn("character_id == 'gojo'", src)

    def test_meeting_can_see_mid_activity_with_injected_rng(self):
        store = PhoneCheckStore()
        start = datetime(2026, 9, 16, 14, 5, tzinfo=timezone.utc)
        check_at = datetime(2026, 9, 16, 14, 12, tzinfo=timezone.utc)
        activity = {
            'id': 3,
            'start_time': '14:00',
            'end_time': '15:00',
            'title': '开会',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=check_at), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2] if len(a) > 2 else check_at, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.01):
            first = db_schedule.decide_phone_check(
                'gojo', 'u1', start, activity,
                source_event_id='m1', pending_text='在吗')
            self.assertFalse(first['seen'])
            self.assertFalse(first['can_reply'])
            self.assertEqual(first['next_phone_check_at'], check_at)
            oid = first['opportunity_id']
            mid = db_schedule.decide_phone_check(
                'gojo', 'u1', check_at, activity,
                source_event_id='m2', pending_text='第二句')
            self.assertFalse(mid['can_reply'])
            self.assertFalse(mid.get('check_consumed'))
            self.assertEqual(mid['opportunity_id'], oid)
            store.schedule_rows = [{
                'id': 3, 'character_id': 'gojo', 'user_id': 'u1',
                'sched_date': start.date(), 'start_time': '14:00',
                'end_time': '15:00', 'title': '开会',
                'can_reply': False, 'reply_state': 'soft_busy',
            }]
            decision = db_schedule.evaluate_due_phone_check(oid, check_at)
        self.assertEqual(decision.get('action'), 'reply')
        self.assertLess(check_at.hour * 60 + check_at.minute, 15 * 60)
        row = next(iter(store.rows.values()))
        self.assertTrue(row['seen'])
        self.assertEqual(row['check_state'], 'processing')
        self.assertIsNone(row.get('resolved_at'))

    def test_hard_busy_stays_unseen_until_activity_end(self):
        store = PhoneCheckStore()
        start = datetime(2026, 9, 16, 14, 5, tzinfo=timezone.utc)
        later = datetime(2026, 9, 16, 14, 40, tzinfo=timezone.utc)
        activity = {
            'id': 9,
            'start_time': '14:00',
            'end_time': '15:00',
            'title': '出任务',
            'reply_state': 'hard_busy',
            'can_reply': False,
        }
        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=later):
            first = db_schedule.decide_phone_check(
                'gojo', 'u1', start, activity, source_event_id='h1')
            second = db_schedule.decide_phone_check(
                'gojo', 'u1', later, activity, source_event_id='h2')
        self.assertFalse(first['seen'])
        self.assertFalse(first['can_reply'])
        self.assertIsNone(first['next_phone_check_at'])
        self.assertFalse(second['seen'])
        self.assertFalse(second['can_reply'])
        self.assertFalse(second.get('check_consumed'))

    def test_phone_check_persists_across_requests(self):
        store = PhoneCheckStore()
        now = datetime(2026, 9, 16, 14, 5, tzinfo=timezone.utc)
        check_at = datetime(2026, 9, 16, 14, 18, tzinfo=timezone.utc)
        activity = {
            'id': 3,
            'start_time': '14:00',
            'end_time': '15:00',
            'title': '开会',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=check_at), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (check_at, False)):
            first = db_schedule.decide_phone_check(
                'gojo', 'u1', now, activity, source_event_id='p1')
            again = db_schedule.decide_phone_check(
                'gojo', 'u1', now, activity, source_event_id='p2')
        self.assertEqual(first['opportunity_id'], again['opportunity_id'])
        self.assertEqual(first['next_phone_check_at'], again['next_phone_check_at'])
        self.assertEqual(len(store.rows), 1)

    def test_busy_path_does_not_create_proactive_promise(self):
        self.assertFalse(hasattr(reply_availability, 'ensure_busy_fallback'))
        src = Path(BACKEND, 'reply_availability.py').read_text(encoding='utf-8')
        self.assertNotIn('add_promise', src)
        self.assertNotIn('ensure_busy_fallback', src)

    def test_schedule_ui_distinguishes_soft_and_hard_copy(self):
        src = Path(ROOT, 'app', 'schedule', 'index.tsx').read_text(encoding='utf-8')
        self.assertIn('可能会看手机', src)
        self.assertIn('暂时无法查看消息', src)
        self.assertIn('当前无法使用手机', src)
        self.assertNotIn('完全走不开', src)
        self.assertNotRegex(src, r"soft_busy' \? '走不开")


if __name__ == '__main__':
    unittest.main()
