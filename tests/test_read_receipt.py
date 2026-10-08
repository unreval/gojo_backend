import asyncio
import copy
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

import test_schedule_reply_state as sched_tests  # noqa: E402
import db_read_receipt  # noqa: E402
import db_schedule  # noqa: E402
import delayed_reply  # noqa: E402
import route_read_receipt  # noqa: E402
import proactive_msg  # noqa: E402


NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)
OLD = {'id': 7, 'start_time': '08:00', 'end_time': '09:00',
       'title': '旧会议', 'reply_state': 'soft_busy', 'can_reply': False}
CURRENT = {'id': 8, 'start_time': '10:00', 'end_time': '11:00',
           'title': '当前会议', 'reply_state': 'soft_busy', 'can_reply': False}


def _conn(store):
    return sched_tests.FakeConn(store)


def _row(store, ids, *, activity=OLD, next_at=NOW):
    row = {
        'id': 1, 'user_id': 'u', 'character_id': 'gojo',
        'sched_date': NOW.date(), 'start_time': activity['start_time'],
        'end_time': activity['end_time'], 'activity_title': activity['title'],
        'reply_state': activity['reply_state'], 'seen': False,
        'seen_at': None, 'seen_watermark': 0, 'can_reply': False,
        'pending_count': len(ids), 'pending_text': '\n'.join(ids),
        'event_meta': '\n'.join(json.dumps({'source_event_id': item}) for item in ids),
        'first_source_event_id': ids[0] if ids else '',
        'last_source_event_id': ids[-1] if ids else '',
        'next_phone_check_at': next_at, 'check_state': 'pending',
        'resolved_at': None, 'schedule_event_id': activity['id'],
        'phase_id': 1, 'event_revision': 1,
    }
    store.rows[('u', 'gojo', NOW.date(), activity['start_time'],
                activity['end_time'])] = row
    return row


def _world(activity, now, state):
    return sched_tests.canonical_test_world(activity, now, reply_state=state)


class ReadReceiptTests(unittest.TestCase):
    def setUp(self):
        self.store = sched_tests.PhoneCheckStore()
        for module in (db_schedule, db_read_receipt):
            p = patch.object(module, 'get_conn', side_effect=lambda: _conn(self.store))
            p.start()
            self.addCleanup(p.stop)

    def _receipt(self, source_id):
        return self.store.receipts.get(('u', 'gojo', source_id))

    def test_init_serializes_new_table_creation(self):
        conn = Mock()
        with patch.object(db_read_receipt, 'get_conn', return_value=conn):
            db_read_receipt.init_read_receipt_table()
        statements = conn.cursor.return_value.execute.call_args_list
        self.assertEqual(len(statements), 2)
        self.assertIn('pg_advisory_xact_lock', statements[0].args[0])
        self.assertIn('CREATE TABLE IF NOT EXISTS chat_read_receipt',
                      statements[1].args[0])
        conn.commit.assert_called_once()

    def test_free_immediate_marks_only_current_source(self):
        db_read_receipt.mark_immediate_seen('u', 'gojo', 'A')
        self.assertEqual(self._receipt('A')[1], 'immediate')
        self.assertIsNone(self._receipt('B'))

    def test_busy_inbound_waits_for_real_claim(self):
        with patch.object(db_schedule, 'sample_next_phone_check_at', return_value=LATER):
            result = db_schedule.decide_phone_check(
                'gojo', 'u', NOW, dict(CURRENT, character_id='gojo'),
                source_event_id='A', pending_text='hello',
                event_meta={'source_event_id': 'A'})
        self.assertFalse(result['seen'])
        self.assertEqual(result['pending_count'], 1)
        self.assertIsNone(self._receipt('A'))

    def test_claim_then_defer_keeps_seen_pending_and_waits(self):
        row = _row(self.store, ['A'], activity=CURRENT)
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda *_: _world(CURRENT, NOW, 'soft_busy')), \
             patch.object(db_schedule.random, 'random', return_value=0.99), \
             patch.object(db_schedule, 'sample_next_phone_check_at', return_value=LATER):
            result = db_schedule.evaluate_due_phone_check(row['id'], NOW)
        self.assertEqual(result['action'], 'defer')
        self.assertEqual(row['check_state'], 'deferred')
        self.assertEqual(row['next_phone_check_at'], LATER)
        self.assertEqual(self._receipt('A'), (NOW, 'phone_check', row['id']))
        self.assertIsNone(row['resolved_at'])
        self.assertEqual(db_schedule.iter_due_phone_checks(NOW + timedelta(minutes=1)), [])

    def test_append_after_seen_leaves_new_message_unread_until_next_claim(self):
        row = _row(self.store, ['A'], activity=CURRENT)
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda *_: _world(CURRENT, NOW, 'soft_busy')), \
             patch.object(db_schedule.random, 'random', return_value=0.99), \
             patch.object(db_schedule, 'sample_next_phone_check_at', return_value=LATER):
            db_schedule.evaluate_due_phone_check(row['id'], NOW)
        db_schedule.decide_phone_check(
            'gojo', 'u', NOW + timedelta(minutes=1),
            dict(CURRENT, character_id='gojo'), source_event_id='B',
            pending_text='second', event_meta={'source_event_id': 'B'})
        self.assertEqual(row['seen_watermark'], 1)
        self.assertEqual(row['pending_count'], 2)
        self.assertIsNone(self._receipt('B'))
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda *_: _world(CURRENT, LATER, 'soft_busy')), \
             patch.object(db_schedule.random, 'random', return_value=0):
            self.assertEqual(db_schedule.evaluate_due_phone_check(
                row['id'], LATER)['action'], 'reply')
        self.assertEqual(self._receipt('A')[0], NOW)
        self.assertEqual(self._receipt('B')[0], LATER)

    def test_generation_failure_preserves_receipt_and_retries(self):
        row = _row(self.store, ['A'])
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda _c, _u, now: _world(CURRENT, now, 'free')):
            failed = delayed_reply.process_due_phone_checks(
                NOW, generate_fn=lambda _bundle: {'ok': False, 'reason': 'model_error'})
            self.assertEqual(failed[0]['action'], 'failed')
            self.assertEqual(row['check_state'], 'pending')
            self.assertIsNone(row['resolved_at'])
            first_seen = self._receipt('A')[0]
            retried = delayed_reply.process_due_phone_checks(
                LATER, generate_fn=lambda _bundle: {'ok': True})
        self.assertEqual(retried[0]['action'], 'replied')
        self.assertEqual(row['check_state'], 'consumed')
        self.assertEqual(self._receipt('A')[0], first_seen)
        self.assertEqual(len(self.store.receipts), 1)

    def test_old_activity_free_replies_without_new_inbound(self):
        row = _row(self.store, ['A'])
        generated = Mock(return_value={'ok': True})
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda _c, _u, now: _world(CURRENT, now, 'free')):
            result = delayed_reply.process_due_phone_checks(NOW, generate_fn=generated)
        self.assertEqual(result[0]['action'], 'replied')
        generated.assert_called_once()
        self.assertEqual(row['check_state'], 'consumed')
        self.assertIsNotNone(self._receipt('A'))

    def test_old_activity_current_hard_busy_postpones_then_replies(self):
        row = _row(self.store, ['A'])
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda _c, _u, now: _world(CURRENT, now, 'hard_busy')):
            first = db_schedule.evaluate_due_phone_check(row['id'], NOW)
        self.assertEqual(first['action'], 'postpone')
        self.assertEqual(row['check_state'], 'deferred')
        self.assertEqual(row['next_phone_check_at'], NOW + timedelta(minutes=61))
        self.assertEqual(db_schedule.iter_due_phone_checks(LATER), [])
        after = NOW + timedelta(minutes=62)
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda _c, _u, now: _world(CURRENT, now, 'free')):
            result = delayed_reply.process_due_phone_checks(
                after, generate_fn=lambda _bundle: {'ok': True})
        self.assertEqual(result[0]['action'], 'replied')
        self.assertEqual(row['check_state'], 'consumed')

    def test_old_activity_uses_current_soft_busy_profile(self):
        row = _row(self.store, ['A'])
        seen_activity = []
        def profile(activity, _character_id):
            seen_activity.append(activity['id'])
            return type('Profile', (), {
                'busy_state': 'soft_busy', 'quick_reply_probability': 0.0})()
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda _c, _u, now: _world(CURRENT, now, 'soft_busy')), \
             patch('activity_phone.profile_for_activity', side_effect=profile), \
             patch.object(db_schedule.random, 'random', return_value=0.5), \
             patch.object(db_schedule, 'sample_next_phone_check_at', return_value=LATER):
            result = db_schedule.evaluate_due_phone_check(row['id'], NOW)
        self.assertEqual(result['action'], 'defer')
        self.assertEqual(seen_activity, [CURRENT['id']])
        self.assertIsNone(row['resolved_at'])

    def test_ended_pending_row_arms_and_transition_keeps_claim(self):
        row = _row(self.store, ['A'], next_at=LATER)
        cur = _conn(self.store).cursor()
        db_schedule.supersede_ended_phone_checks(cur, 'u', 'gojo', NOW)
        self.assertEqual(row['next_phone_check_at'], NOW)
        self.assertEqual(db_schedule.iter_due_phone_checks(NOW), [row['id']])
        db_schedule._reconcile_phone_checks_tx(cur, [OLD['id']], NOW,
                                                user_id='u', character_id='gojo')
        self.assertEqual(row['check_state'], 'pending')
        row['check_state'] = 'processing'
        db_schedule._reconcile_phone_checks_tx(cur, [OLD['id']], NOW,
                                                user_id='u', character_id='gojo')
        self.assertEqual(row['check_state'], 'processing')
        self.assertIsNone(row['resolved_at'])

    def test_exact_ids_and_first_seen_idempotency(self):
        row = _row(self.store, ['A', 'B'])
        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=lambda _c, _u, now: _world(CURRENT, now, 'free')):
            first = db_schedule.evaluate_due_phone_check(row['id'], NOW)
            db_schedule.abort_delayed_reply(
                row['id'], first['claimed']['claim_token'], now=NOW)
            second = db_schedule.evaluate_due_phone_check(
                row['id'], NOW + timedelta(minutes=2))
        self.assertEqual(set(source_id for _u, _c, source_id in self.store.receipts),
                         {'A', 'B'})
        self.assertIsNone(self._receipt('C'))
        self.assertEqual(self._receipt('A')[0], NOW)
        self.assertEqual(self._receipt('B')[0], NOW)
        self.assertEqual(second['claimed']['seen_at'], NOW)

    def test_legacy_snapshot_uses_only_known_ids_in_order(self):
        bundle = {
            'id': 9, 'pending_count': 4,
            'event_meta': '{"source_event_id":"B"}\n{"event_id":"B"}',
            'first_source_event_id': 'A', 'last_source_event_id': 'C',
            'pending_text': 'never an identity',
        }
        with patch('builtins.print') as diagnostic:
            ids = db_read_receipt.source_event_ids_from_claim(bundle)
        self.assertEqual(ids, ['A', 'B', 'C'])
        self.assertIn('legacy_snapshot_missing_ids', str(diagnostic.call_args))

    def test_unrelated_proactive_does_not_read_user_backlog(self):
        row = _row(self.store, ['A', 'B'])
        cur = Mock()
        cur.fetchone.return_value = (9, NOW)
        conn = Mock()
        conn.cursor.return_value = cur
        with patch.object(proactive_msg, 'get_conn', return_value=conn):
            proactive_msg.add_proactive_msg('gojo', 'u', 'life_share', '甘いものを食べた')
        self.assertEqual(self.store.receipts, {})
        self.assertEqual(row['seen_watermark'], 0)

    def test_query_api_scopes_and_validates_ids(self):
        db_read_receipt.mark_immediate_seen('u', 'gojo', 'A')
        def query(user='u', character='gojo', ids=None):
            response = asyncio.run(route_read_receipt.read_receipts_query({
                'user_id': user, 'character_id': character,
                'source_event_ids': ['A', 'B'] if ids is None else ids}))
            return response.status_code, json.loads(response.body)
        self.assertEqual([item['source_event_id'] for item in query()[1]['receipts']], ['A'])
        self.assertEqual(query(user='other')[1]['receipts'], [])
        self.assertEqual(query(character='geto')[1]['receipts'], [])
        self.assertEqual(query(ids=['A'] * 101)[0], 400)
        self.assertEqual(query(ids=['x' * 121])[0], 400)

    def test_claim_and_receipt_roll_back_together_on_failure(self):
        row = _row(self.store, ['A'])
        class TransactionalConn(sched_tests.FakeConn):
            def __init__(self, store):
                super().__init__(store)
                self.before_rows = copy.deepcopy(store.rows)
                self.before_receipts = copy.deepcopy(store.receipts)
            def rollback(self):
                self.store.rows.clear()
                self.store.rows.update(self.before_rows)
                self.store.receipts.clear()
                self.store.receipts.update(self.before_receipts)
        original = db_schedule.mark_source_events_seen_tx
        def insert_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('receipt transaction failed')
        with patch.object(db_schedule, 'get_conn', side_effect=lambda: TransactionalConn(self.store)), \
             patch.object(db_schedule, 'mark_source_events_seen_tx', side_effect=insert_then_fail):
            with self.assertRaises(RuntimeError):
                db_schedule.evaluate_due_phone_check(row['id'], NOW)
        restored = list(self.store.rows.values())[0]
        self.assertEqual(restored['check_state'], 'pending')
        self.assertFalse(restored['seen'])
        self.assertEqual(self.store.receipts, {})


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE for disposable PostgreSQL')
class ReadReceiptPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def test_claim_and_receipt_share_real_transaction(self):
        conn = self.database
        conn.query('DROP SCHEMA public CASCADE')
        conn.query('CREATE SCHEMA public')
        conn.query('''CREATE TABLE char_phone_check (
            id SERIAL PRIMARY KEY, user_id TEXT, character_id TEXT,
            pending_count INTEGER, pending_text TEXT, event_meta TEXT,
            reply_state TEXT, seen_watermark INTEGER DEFAULT 0,
            next_phone_check_at TIMESTAMPTZ, fallback_promise_id INTEGER,
            first_source_event_id TEXT, last_source_event_id TEXT,
            activity_title TEXT, start_time TEXT, end_time TEXT,
            sched_date DATE, schedule_event_id INTEGER, phase_id INTEGER,
            event_revision INTEGER, occurrence_id TEXT,
            check_state TEXT DEFAULT 'pending', resolved_at TIMESTAMPTZ,
            claimed_at TIMESTAMPTZ, claim_token TEXT, claim_owner TEXT,
            claim_expires_at TIMESTAMPTZ, seen BOOLEAN DEFAULT FALSE,
            seen_at TIMESTAMPTZ, updated_at TIMESTAMPTZ,
            fail_count INTEGER NOT NULL DEFAULT 0, retry_not_before TIMESTAMPTZ,
            last_fail_reason TEXT, last_fail_at TIMESTAMPTZ
        )''')
        with patch.object(db_read_receipt, 'get_conn', return_value=conn):
            db_read_receipt.init_read_receipt_table()
            db_read_receipt.init_read_receipt_table()
        conn.query('''INSERT INTO char_phone_check
            (user_id, character_id, pending_count, pending_text, event_meta,
             reply_state, next_phone_check_at, first_source_event_id,
             last_source_event_id, activity_title, start_time, end_time,
             sched_date, schedule_event_id, phase_id, event_revision)
            VALUES ('u','gojo',2,'A\nB',
                    '{"source_event_id":"A"}\n{"source_event_id":"B"}',
                    'soft_busy', '2026-09-18T10:00:00Z', 'A', 'B',
                    '会议','09:00','11:00','2026-09-18',7,1,1)''')
        conn.commit()

        cur = conn.cursor()
        first = db_schedule.claim_due_phone_check(cur, 1, NOW)
        self.assertEqual(first['seen_watermark'], 2)
        self.assertEqual(conn.query('SELECT count(*) AS n FROM chat_read_receipt')['rows'][0]['n'], 2)
        conn.rollback()
        self.assertEqual(conn.query('SELECT count(*) AS n FROM chat_read_receipt')['rows'][0]['n'], 0)
        self.assertFalse(conn.query('SELECT seen FROM char_phone_check WHERE id=1')['rows'][0]['seen'])
        conn.commit()

        claimed = db_schedule.claim_due_phone_check(conn.cursor(), 1, NOW)
        self.assertIsNotNone(claimed)
        conn.commit()
        self.assertEqual(conn.query('SELECT count(*) AS n FROM chat_read_receipt')['rows'][0]['n'], 2)
        conn.commit()

        cur = conn.cursor()
        db_read_receipt.mark_source_events_seen_tx(
            cur, 'u', 'gojo', ['A'], LATER, 'immediate')
        conn.commit()
        row = conn.query('''SELECT
            seen_at = TIMESTAMPTZ '2026-09-18T10:00:00Z' AS first_seen_kept,
            seen_via = 'phone_check' AS first_via_kept
            FROM chat_read_receipt WHERE source_event_id = 'A' ''')['rows'][0]
        self.assertTrue(row['first_seen_kept'])
        self.assertTrue(row['first_via_kept'])
        conn.commit()


if __name__ == '__main__':
    unittest.main()
