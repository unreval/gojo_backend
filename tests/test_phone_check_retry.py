# -*- coding: utf-8 -*-
import os
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path[:0] = [os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend'),
                os.path.dirname(__file__)]

import db_schedule
from test_schedule_reply_state import FakeConn, PhoneCheckStore


NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


class PhoneCheckRetryTests(unittest.TestCase):
    def setUp(self):
        self.store = PhoneCheckStore()
        self.row = {
            'id': 1, 'user_id': 'u', 'character_id': 'gojo',
            'sched_date': NOW.date(), 'start_time': '08:00', 'end_time': '09:00',
            'schedule_event_id': 7, 'pending_count': 1, 'pending_text': '在吗',
            'check_state': 'pending', 'resolved_at': None,
            'next_phone_check_at': NOW, 'fail_count': 0,
        }
        self.store.rows[('u', 'gojo', NOW.date(), '08:00', '09:00')] = self.row
        self.cur = FakeConn(self.store).cursor()

    def fail_generation(self, now, *, reason='generation_failed', retry_at=None):
        claimed = db_schedule.claim_due_phone_check(self.cur, 1, now)
        self.assertIsNotNone(claimed)
        result = db_schedule.release_claimed_phone_check(
            self.cur, 1, claimed['claim_token'], reason=reason,
            retry_at=retry_at, now=now)
        self.assertEqual(result, 1)

    def test_backoff_survives_ended_window_and_all_rearming_paths(self):
        self.fail_generation(NOW)
        retry_at = NOW + timedelta(minutes=2)
        self.assertEqual(self.row['retry_not_before'], retry_at)
        self.assertEqual(self.row['fail_count'], 1)
        self.assertEqual(self.row['last_fail_reason'], 'generation_failed')
        self.assertEqual(self.row['last_fail_at'], NOW)
        before = NOW + timedelta(seconds=30)
        self.assertEqual(db_schedule.list_due_phone_check_ids(self.cur, before), [])
        db_schedule.supersede_ended_phone_checks(self.cur, 'u', 'gojo', before)
        self.assertEqual(db_schedule.list_due_phone_check_ids(self.cur, before), [])
        with patch.object(db_schedule, 'get_conn', lambda: FakeConn(self.store)):
            self.assertEqual(db_schedule.evaluate_due_phone_check(1, before)['action'], 'skip')
        db_schedule._reconcile_phone_checks_tx(self.cur, [7], before)
        self.assertEqual(self.row['next_phone_check_at'], before)
        self.assertEqual(self.row['retry_not_before'], retry_at)
        self.assertEqual(db_schedule.list_due_phone_check_ids(self.cur, before), [])
        self.assertIsNone(db_schedule.claim_due_phone_check(self.cur, 1, before))
        self.assertEqual(db_schedule.list_due_phone_check_ids(self.cur, retry_at), [1])

    def test_fifth_failure_expires_without_deleting_pending_message(self):
        now = NOW
        for count, minutes in enumerate((2, 10, 30, 120), 1):
            self.fail_generation(now)
            self.assertEqual(self.row['fail_count'], count)
            self.assertEqual(self.row['retry_not_before'], now + timedelta(minutes=minutes))
            now = self.row['retry_not_before']
        self.fail_generation(now)
        self.assertEqual(self.row['fail_count'], 5)
        self.assertEqual(self.row['check_state'], 'expired')
        self.assertEqual(self.row['resolved_at'], now)
        self.assertEqual(self.row['pending_count'], 1)
        self.assertEqual(self.row['pending_text'], '在吗')
        for field in ('claimed_at', 'claim_token', 'claim_owner', 'claim_expires_at'):
            self.assertIsNone(self.row[field])
        self.assertEqual(db_schedule.list_due_phone_check_ids(self.cur, now + timedelta(days=1)), [])
        self.assertIsNone(db_schedule.claim_due_phone_check(self.cur, 1, now + timedelta(days=1)))

    def test_source_invalid_expires_on_first_failure_without_retry(self):
        self.fail_generation(NOW, reason='source_invalid')
        self.assertEqual(self.row['check_state'], 'expired')
        self.assertEqual(self.row['fail_count'], 1)
        self.assertEqual(self.row['last_fail_reason'], 'source_invalid')
        self.assertEqual(self.row['resolved_at'], NOW)
        self.assertIsNone(self.row['retry_not_before'])
        self.assertEqual(self.row['pending_count'], 1)
        self.assertEqual(self.row['pending_text'], '在吗')
        self.assertEqual(db_schedule.list_due_phone_check_ids(
            self.cur, NOW + timedelta(days=1)), [])

    def test_auth_minimum_and_later_explicit_retry_are_preserved(self):
        self.fail_generation(NOW, reason='provider_auth_failed')
        self.assertEqual(self.row['retry_not_before'], NOW + timedelta(minutes=15))
        now = self.row['retry_not_before']
        later = now + timedelta(hours=3)
        self.fail_generation(now, retry_at=later)
        self.assertEqual(self.row['retry_not_before'], later)

    def test_release_requires_locked_matching_claim_and_counts_once(self):
        claimed = db_schedule.claim_due_phone_check(self.cur, 1, NOW)
        with patch.object(db_schedule, 'get_conn', lambda: FakeConn(self.store)):
            self.assertEqual(db_schedule.abort_delayed_reply(1, 'wrong', now=NOW), 0)
            self.assertEqual(self.row['fail_count'], 0)
            self.assertEqual(db_schedule.abort_delayed_reply(
                1, claimed['claim_token'], reason='test_failure', now=NOW), 1)
            self.assertEqual(db_schedule.abort_delayed_reply(
                1, claimed['claim_token'], reason='test_failure', now=NOW), 0)
        self.assertEqual(self.row['fail_count'], 1)
        locks = [sql for sql, _ in self.store.sql if sql.startswith('SELECT fail_count')]
        self.assertTrue(locks)
        self.assertTrue(all('FOR UPDATE' in sql for sql in locks))

    def test_actual_due_sql_applies_retry_bound_to_ended_window_or_branch(self):
        self.fail_generation(NOW)
        before = NOW + timedelta(seconds=30)
        db_schedule.list_due_phone_check_ids(self.cur, before)
        sql, params = self.store.sql[-1]
        # Execute the production WHERE expression, rather than the fixture's interpretation.
        with sqlite3.connect(':memory:') as conn:
            conn.execute('CREATE TABLE char_phone_check (id INTEGER, resolved_at TEXT, '
                         'pending_count INTEGER, check_state TEXT, claim_expires_at TEXT, '
                         'next_phone_check_at TEXT, sched_date TEXT, end_time TEXT, '
                         'retry_not_before TEXT)')
            conn.execute('INSERT INTO char_phone_check VALUES (?, NULL, 1, ?, NULL, ?, ?, ?, ?)',
                         (1, 'pending', NOW.isoformat(), NOW.date().isoformat(), '09:00',
                          self.row['retry_not_before'].isoformat()))
            adapted = [p.isoformat() if hasattr(p, 'isoformat') else p for p in params]
            self.assertEqual(conn.execute(sql.replace('%s', '?'), adapted).fetchall(), [])
            conn.execute('UPDATE char_phone_check SET retry_not_before = NULL')
            self.assertEqual(conn.execute(sql.replace('%s', '?'), adapted).fetchall(), [(1,)])


if __name__ == '__main__':
    unittest.main()
