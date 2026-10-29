"""A once promise is identified by its trigger instant, not by reply text."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import db_promise
from generation_effects import ENDPOINT_CHAT_TEXT, occurrence_key_for


ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 29, 7, 0, tzinfo=ZONE)
CONTEXT = 'PRIVATE_PROMISE_CONTEXT_勿记录正文'


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE for disposable PostgreSQL')
class PromiseIdentityPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def setUp(self):
        self.database.rollback()
        self.database.query("SET TIME ZONE 'Asia/Shanghai'")
        self.database.query('DROP SCHEMA public CASCADE')
        self.database.query('CREATE SCHEMA public')
        patcher = patch.object(db_promise, 'get_conn', return_value=self.database)
        patcher.start()
        self.addCleanup(patcher.stop)
        with redirect_stdout(io.StringIO()):
            db_promise.init_promise_table()

    def sql(self, query, params=()):
        cur = self.database.cursor()
        cur.execute(query, params)
        rows = cur.fetchall()
        self.database.commit()
        return rows

    def add(self, trigger_at=NOW, *, user_id='test-user', character_id='gojo',
            occurrence_key=None, context=CONTEXT):
        output = io.StringIO()
        with redirect_stdout(output):
            promise_id = db_promise.add_promise(
                character_id, user_id, 'once', context,
                trigger_at=trigger_at, origin_text='origin',
                occurrence_key=occurrence_key)
        return promise_id, output.getvalue()

    def test_same_trigger_inserts_once_and_returns_the_same_id(self):
        first, _ = self.add()
        second, log = self.add(context='另一句原话，文本不参与判重')
        self.assertEqual(first, second)
        self.assertIn(f'duplicate suppressed identity_id={first}', log)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM proactive_promise'), [(1,)])

    def test_two_producers_share_one_row_when_the_trigger_matches(self):
        detector_key = occurrence_key_for(
            ENDPOINT_CHAT_TEXT, 'evt-61', 'promise_detector')
        structured_key = occurrence_key_for(
            ENDPOINT_CHAT_TEXT, 'evt-61', 'proactive_promise')
        self.assertNotEqual(detector_key, structured_key)
        first, _ = self.add(occurrence_key=detector_key)
        second, log = self.add(occurrence_key=structured_key, context='结构化承诺')
        self.assertEqual(first, second)
        self.assertIn(f'duplicate suppressed identity_id={first}', log)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM proactive_promise'), [(1,)])

    def test_next_day_same_clock_is_a_different_promise(self):
        first, _ = self.add(NOW)
        second, log = self.add(NOW + timedelta(days=1))
        self.assertNotEqual(first, second)
        self.assertNotIn('duplicate suppressed', log)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM proactive_promise'), [(2,)])

    def test_fired_once_does_not_block_the_same_trigger_later(self):
        first, _ = self.add()
        db_promise.mark_fired(first, NOW)
        second, log = self.add()
        self.assertNotEqual(first, second)
        self.assertNotIn('duplicate suppressed', log)
        self.assertEqual(self.sql(
            'SELECT COUNT(*) FROM proactive_promise'), [(2,)])
        self.assertEqual(self.sql(
            '''SELECT COUNT(*) FROM proactive_promise
               WHERE is_fired=FALSE AND is_active=TRUE'''), [(1,)])

    def test_fired_once_leaves_the_active_and_due_sets(self):
        due_at = NOW - timedelta(minutes=10)
        promise_id, _ = self.add(due_at)
        listed = db_promise.list_active_promises('gojo', 'test-user')
        self.assertEqual([row['id'] for row in listed], [promise_id])
        self.assertFalse(listed[0]['is_fired'])
        db_promise.mark_fired(promise_id, NOW)
        output = io.StringIO()
        with redirect_stdout(output):
            due = db_promise.get_due_promises(NOW)
        self.assertEqual(due, [])
        self.assertEqual(db_promise.list_active_promises('gojo', 'test-user'), [])
        self.assertEqual(self.sql(
            'SELECT is_active, is_fired FROM proactive_promise WHERE id=%s',
            (promise_id,)), [(False, True)])

    def test_daily_mark_fired_stays_active(self):
        with redirect_stdout(io.StringIO()):
            promise_id = db_promise.add_promise(
                'gojo', 'test-user', 'daily', CONTEXT, trigger_time='07:00')
        db_promise.mark_fired(promise_id, NOW)
        self.assertEqual(self.sql(
            'SELECT is_active, is_fired FROM proactive_promise WHERE id=%s',
            (promise_id,)), [(True, False)])


if __name__ == '__main__':
    unittest.main()
