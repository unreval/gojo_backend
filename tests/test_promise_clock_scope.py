"""Promise clocks bind to the appointment, not the first time of night."""
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
import promise_detector
from config import CN_TZ


ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 29, 2, 6, tzinfo=CN_TZ)
WRONG_ALARM = datetime(2026, 10, 30, 2, 0, tzinfo=CN_TZ)


class _FrozenNow(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is not None:
            return NOW.astimezone(tz)
        return NOW


class PromiseClockParseTests(unittest.TestCase):
    def test_verb_before_clock_is_seven(self):
        self.assertEqual(promise_detector._extract_clock('我叫你，七点'), (7, 0))

    def test_bare_current_time_is_not_a_promise_clock(self):
        self.assertEqual(promise_detector._extract_clock('都两点多了'), (None, None))

    def test_half_hour_next_to_the_call_wins(self):
        self.assertEqual(
            promise_detector._extract_clock('七点前就起来。六点半叫你。'),
            (6, 30))


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE for disposable PostgreSQL')
class PromiseClockPostgresTests(unittest.TestCase):
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
        for patcher in (
                patch.object(db_promise, 'get_conn', return_value=self.database),
                patch.object(promise_detector, 'datetime', _FrozenNow)):
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

    def save(self, reply_zh):
        captured = []
        real = db_promise.add_promise

        def spy(**kwargs):
            captured.append(kwargs)
            return real(**kwargs)

        with patch.object(db_promise, 'add_promise', spy), \
                redirect_stdout(io.StringIO()):
            promise_id = promise_detector.detect_and_save(
                'gojo', 'clock-user', '明早叫我', reply_zh)
        return promise_id, captured

    def test_production_reply_uses_seven_not_the_announced_two(self):
        reply = '还醒着呢？都两点多了。早八有吧，七点我叫你起来。'
        promise_id, captured = self.save(reply)
        self.assertIsNotNone(promise_id)
        self.assertEqual(len(captured), 1)
        trigger_at = captured[0]['trigger_at']
        self.assertEqual(trigger_at, datetime(2026, 10, 29, 7, 0, tzinfo=CN_TZ))
        self.assertNotEqual(trigger_at, WRONG_ALARM)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM proactive_promise'), [(1,)])

    def test_half_past_six_is_kept_when_seven_is_only_narrative(self):
        promise_id, captured = self.save('七点前就起来。六点半叫你。')
        self.assertIsNotNone(promise_id)
        self.assertEqual(
            captured[0]['trigger_at'],
            datetime(2026, 10, 29, 6, 30, tzinfo=CN_TZ))
        self.assertNotEqual(
            captured[0]['trigger_at'].hour, 7)

    def test_timed_without_a_clock_does_not_invent_two_hours(self):
        promise_id, captured = self.save('晚上提醒你')
        self.assertIsNone(promise_id)
        self.assertEqual(captured, [])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM proactive_promise'), [(0,)])

    def test_verb_before_clock_saves_seven_when_the_line_is_timed(self):
        promise_id, captured = self.save('晚上我叫你，七点')
        self.assertIsNotNone(promise_id)
        self.assertEqual(
            captured[0]['trigger_at'],
            datetime(2026, 10, 29, 7, 0, tzinfo=CN_TZ))


if __name__ == '__main__':
    unittest.main()
