# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
TESTS = os.path.dirname(__file__)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import db_schedule  # noqa: E402
import test_schedule_reply_state as sched_tests  # noqa: E402

FakeConn = sched_tests.FakeConn
PhoneCheckStore = sched_tests.PhoneCheckStore


NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
ACTIVITY = {
    'id': 7,
    'start_time': '09:00',
    'end_time': '11:00',
    'title': '开会',
    'location': '会议室',
    'note': '',
    'reply_state': 'soft_busy',
    'can_reply': False,
    'character_id': 'gojo',
}


def _conn(store):
    return FakeConn(store)


class PhoneCheckAtomicTests(unittest.TestCase):
    def test_concurrent_claim_only_one_wins(self):
        store = PhoneCheckStore()
        due = NOW - timedelta(minutes=1)
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at', return_value=due), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          return_value=(due, False)):
            db_schedule.decide_phone_check(
                'gojo', 'u', due - timedelta(minutes=20), ACTIVITY,
                source_event_id='e1', pending_text='hi')
        row = list(store.rows.values())[0]
        row['next_phone_check_at'] = NOW
        row['check_state'] = 'pending'
        row['resolved_at'] = None
        cur = FakeConn(store).cursor()
        first = db_schedule.claim_due_phone_check(cur, row['id'], NOW)
        second = db_schedule.claim_due_phone_check(cur, row['id'], NOW)
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(row['check_state'], 'processing')

    def test_sequential_second_claim_loses(self):
        store = PhoneCheckStore()
        due = NOW - timedelta(minutes=1)
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          side_effect=[due, NOW + timedelta(minutes=10)]), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[-1] if a else due, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.99):
            db_schedule.decide_phone_check(
                'gojo', 'u', due - timedelta(minutes=10), ACTIVITY,
                source_event_id='e1', pending_text='hi')
            list(store.rows.values())[0]['next_phone_check_at'] = NOW
            store.schedule_rows = [{
                'id': 7, 'character_id': 'gojo', 'user_id': 'u',
                'sched_date': NOW.date(), 'start_time': '09:00', 'end_time': '11:00',
                'title': '开会', 'can_reply': False, 'reply_state': 'soft_busy',
            }]
            oid = list(store.rows.values())[0]['id']
            first = db_schedule.evaluate_due_phone_check(oid, NOW)
            second = db_schedule.evaluate_due_phone_check(oid, NOW)
        self.assertEqual(first.get('action'), 'defer')
        self.assertEqual(second.get('action'), 'skip')
        next_times = [
            row['next_phone_check_at'] for row in store.rows.values()
        ]
        self.assertEqual(len(set(next_times)), 1)

    def test_stale_processing_can_be_reclaimed(self):
        store = PhoneCheckStore()
        due = NOW - timedelta(minutes=1)
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at', return_value=due), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          return_value=(due, False)):
            db_schedule.decide_phone_check(
                'gojo', 'u', due - timedelta(minutes=10), ACTIVITY,
                source_event_id='e1', pending_text='hi')
            row = list(store.rows.values())[0]
            row['check_state'] = 'processing'
            row['claim_token'] = 'dead'
            row['claim_expires_at'] = NOW - timedelta(seconds=1)
            row['next_phone_check_at'] = NOW
            row['resolved_at'] = None
            with patch.object(db_schedule.random, 'random', return_value=0.0):
                decision = db_schedule.evaluate_due_phone_check(row['id'], NOW)
        self.assertEqual(decision.get('action'), 'reply')

    def test_resolved_cannot_be_reclaimed(self):
        store = PhoneCheckStore()
        due = NOW - timedelta(minutes=1)
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at', return_value=due), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          return_value=(due, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.0):
            db_schedule.decide_phone_check(
                'gojo', 'u', due - timedelta(minutes=10), ACTIVITY,
                source_event_id='e1', pending_text='hi')
            row = list(store.rows.values())[0]
            row['next_phone_check_at'] = NOW
            consumed = db_schedule.evaluate_due_phone_check(row['id'], NOW)
            db_schedule.complete_delayed_reply(
                row['id'], consumed['claimed']['claim_token'], NOW)
            again = db_schedule.evaluate_due_phone_check(row['id'], NOW)
        self.assertEqual(consumed.get('action'), 'reply')
        self.assertEqual(again.get('action'), 'skip')

    def test_activity_end_supersedes_old_check(self):
        store = PhoneCheckStore()
        store.rows[('u', 'gojo', NOW.date(), '08:00', '09:00')] = {
            'id': 99,
            'user_id': 'u',
            'character_id': 'gojo',
            'sched_date': NOW.date(),
            'start_time': '08:00',
            'end_time': '09:00',
            'check_state': 'pending',
            'resolved_at': None,
            'pending_count': 2,
            'seen': False,
            'can_reply': False,
            'pending_text': 'old',
            'event_meta': '',
            'seen_watermark': 0,
            'next_phone_check_at': NOW,
        }
        conn = _conn(store)
        cur = conn.cursor()
        db_schedule.supersede_ended_phone_checks(
            cur, 'u', 'gojo', NOW, ACTIVITY)
        old = store.rows[('u', 'gojo', NOW.date(), '08:00', '09:00')]
        self.assertNotEqual(old['check_state'], 'superseded')
        self.assertIsNone(old.get('resolved_at'))
        self.assertEqual(old['next_phone_check_at'], NOW)


if __name__ == '__main__':
    unittest.main()
