"""Promise grace windows against disposable, offline PostgreSQL."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import importlib.util
import io
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import db_promise


ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 24, 23, 30, tzinfo=ZONE)
CONTEXT = 'PRIVATE_PROMISE_CONTEXT_勿记录正文'
ORIGIN = 'PRIVATE_ORIGIN_TEXT_勿记录用户原话'


class PromiseGraceConfigTests(unittest.TestCase):
    def load_config(self, value):
        spec = importlib.util.spec_from_file_location(
            '_promise_config_test', BACKEND / 'db_promise.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ):
            if value is None:
                os.environ.pop('PROMISE_GRACE_MINUTES', None)
            else:
                os.environ['PROMISE_GRACE_MINUTES'] = value
            spec.loader.exec_module(module)
        return module.PROMISE_GRACE_MINUTES

    def test_default_grace_is_120_minutes(self):
        self.assertEqual(self.load_config(None), 120)

    def test_environment_overrides_grace_minutes(self):
        self.assertEqual(self.load_config('17'), 17)


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE for disposable PostgreSQL')
class PromiseGracePostgresTests(unittest.TestCase):
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
                patch.object(db_promise, 'PROMISE_GRACE_MINUTES', 120, create=True)):
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

    def promise(self, kind='once', *, trigger_at=None, trigger_time='21:30'):
        return db_promise.add_promise(
            'gojo', 'test-user', kind, CONTEXT,
            trigger_at=(trigger_at if trigger_at is not None else NOW)
            if kind == 'once' else None,
            trigger_time=trigger_time if kind == 'daily' else None,
            origin_text=ORIGIN)

    def due(self, now=NOW):
        output = io.StringIO()
        with redirect_stdout(output):
            result = db_promise.get_due_promises(now)
        return result, output.getvalue()

    def assert_audit(self, output, pid, trigger_at, delay):
        self.assertIn(f'id={pid}', output)
        match = re.search(r'trigger_at=(\S+)', output)
        self.assertIsNotNone(match)
        recorded_at = datetime.fromisoformat(match.group(1))
        if recorded_at.tzinfo is None:
            recorded_at = recorded_at.replace(tzinfo=ZONE)
        self.assertEqual(recorded_at, trigger_at)
        self.assertIn(f'delay_minutes={delay:.2f}', output)
        self.assertNotIn(CONTEXT, output)
        self.assertNotIn(ORIGIN, output)

    def test_once_within_grace_returns_original_payload(self):
        expected_at = NOW - timedelta(minutes=10)
        pid = self.promise(trigger_at=expected_at)
        due, output = self.due()
        self.assertEqual(output, '')
        self.assertEqual(len(due), 1)
        row = due[0]
        self.assertEqual(row['id'], pid)
        self.assertEqual(row['character_id'], 'gojo')
        self.assertEqual(row['user_id'], 'test-user')
        self.assertEqual(row['trigger_kind'], 'once')
        self.assertEqual(row['context'], CONTEXT)
        self.assertEqual(row['origin_text'], ORIGIN)
        got = row['trigger_at']
        if got.tzinfo is None:
            got = got.replace(tzinfo=ZONE)
        self.assertEqual(got.astimezone(ZONE), expected_at)

    def test_once_at_exact_grace_boundary_remains_due(self):
        pid = self.promise(trigger_at=NOW - timedelta(minutes=120))
        self.assertEqual([row['id'] for row in self.due()[0]], [pid])

    def test_once_one_second_beyond_grace_expires_permanently(self):
        trigger_at = NOW - timedelta(minutes=120, seconds=1)
        pid = self.promise(trigger_at=trigger_at)
        due, output = self.due()
        self.assertEqual(due, [])
        self.assertEqual(self.sql('''SELECT is_active, is_fired, last_error
            FROM proactive_promise WHERE id=%s''', (pid,)),
            [(False, False, 'expired')])
        self.assertFalse(self.database.in_transaction)
        self.assert_audit(output, pid, trigger_at, 120 + 1 / 60)
        repeated_due, repeated_log = self.due(NOW + timedelta(minutes=10))
        self.assertEqual(repeated_due, [])
        self.assertEqual(repeated_log, '')

    def test_once_overnight_outage_does_not_send_backlog(self):
        trigger_at = datetime(2026, 10, 23, 21, 30, tzinfo=ZONE)
        pid = self.promise(trigger_at=trigger_at)
        due, output = self.due(datetime(2026, 10, 24, 5, 36, tzinfo=ZONE))
        self.assertEqual(due, [])
        self.assert_audit(output, pid, trigger_at, 486)
        self.assertEqual(self.sql('''SELECT is_active, last_error
            FROM proactive_promise WHERE id=%s''', (pid,)), [(False, 'expired')])

    def test_future_once_does_not_expire_or_trigger(self):
        pid = self.promise(trigger_at=NOW + timedelta(seconds=1))
        self.assertEqual(self.due(), ([], ''))
        self.assertEqual(self.sql('''SELECT is_active, is_fired
            FROM proactive_promise WHERE id=%s''', (pid,)), [(True, False)])

    def test_fired_and_inactive_once_are_not_reclassified(self):
        fired_at = NOW - timedelta(days=3)
        inactive_at = fired_at + timedelta(hours=1)
        fired = self.promise(trigger_at=fired_at)
        inactive = self.promise(trigger_at=inactive_at)
        db_promise.mark_fired(fired, fired_at)
        db_promise.deactivate(inactive)
        self.assertEqual(self.due(), ([], ''))
        self.assertEqual(self.sql('''SELECT id, is_active, is_fired, last_error
            FROM proactive_promise ORDER BY id'''),
            [(fired, False, True, None), (inactive, False, False, None)])

    def test_once_comparison_keeps_session_timezone_semantics(self):
        self.database.query("SET TIME ZONE 'UTC'")
        trigger_at = NOW - timedelta(minutes=30)
        pid = self.promise(trigger_at=trigger_at)
        # This adapter sends ISO strings without psycopg2's timestamptz OID.
        self.sql('UPDATE proactive_promise SET trigger_at=%s::timestamptz WHERE id=%s',
                 (trigger_at, pid))
        self.assertEqual([row['id'] for row in self.due()[0]], [pid])
        due, output = self.due(NOW + timedelta(minutes=91))
        self.assertEqual(due, [])
        self.assertIn(f'id={pid}', output)
        self.assertIn('delay_minutes=121.00', output)

    def test_query_failure_rolls_back_expiration_and_closes_resources(self):
        pid = self.promise(trigger_at=NOW - timedelta(minutes=121))
        cursor = self.database.cursor()
        tracked_cursor = Mock(wraps=cursor)
        tracked_connection = Mock(wraps=self.database)
        tracked_connection.cursor.return_value = tracked_cursor

        def execute(query, params=()):
            if query.lstrip().startswith('SELECT') and "'daily'" in query:
                raise RuntimeError('offline daily query failed')
            return cursor.execute(query, params)

        tracked_cursor.execute.side_effect = execute
        output = io.StringIO()
        with patch.object(db_promise, 'get_conn', return_value=tracked_connection), \
             redirect_stdout(output), \
             self.assertRaisesRegex(RuntimeError, 'offline daily query failed'):
            db_promise.get_due_promises(NOW)
        tracked_connection.rollback.assert_called_once()
        tracked_connection.commit.assert_not_called()
        tracked_cursor.close.assert_called_once()
        tracked_connection.close.assert_called_once()
        self.assertEqual(output.getvalue(), '')
        self.assertEqual(self.sql('''SELECT is_active, last_error
            FROM proactive_promise WHERE id=%s''', (pid,)), [(True, None)])

    def test_daily_within_grace_and_exact_boundary_are_due(self):
        within = self.promise('daily', trigger_time='22:30')
        edge = self.promise('daily', trigger_time='21:30')
        future = self.promise('daily', trigger_time='23:31')
        due, output = self.due()
        self.assertEqual([row['id'] for row in due], [edge, within])
        self.assertTrue(all(row['context'] == CONTEXT for row in due))
        self.assertNotIn(future, [row['id'] for row in due])
        self.assertEqual(output, '')

    def test_daily_one_second_beyond_window_skips_today_then_recurs(self):
        pid = self.promise('daily')
        yesterday = NOW - timedelta(days=1, hours=2)
        db_promise.mark_fired(pid, yesterday)
        initial = self.sql('''SELECT is_active, is_fired, last_fired_at
            FROM proactive_promise WHERE id=%s''', (pid,))
        due, output = self.due(NOW + timedelta(seconds=1))
        self.assertEqual(due, [])
        self.assert_audit(output, pid, NOW - timedelta(hours=2), 120 + 1 / 60)
        self.assertEqual(self.sql('''SELECT is_active, is_fired, last_fired_at
            FROM proactive_promise WHERE id=%s''', (pid,)), initial)
        self.assertEqual(self.due(NOW + timedelta(minutes=10))[0], [])
        tomorrow = NOW + timedelta(days=1, hours=-2)
        self.assertEqual([row['id'] for row in self.due(tomorrow)[0]], [pid])
        db_promise.mark_fired(pid, tomorrow)
        self.assertEqual(self.due(tomorrow + timedelta(minutes=10))[0], [])

    def test_daily_midnight_does_not_replay_yesterday_occurrence(self):
        pid = self.promise('daily', trigger_time='23:30')
        after_midnight = datetime(2026, 10, 25, 0, 30, tzinfo=ZONE)
        self.assertEqual(self.due(after_midnight), ([], ''))
        self.assertEqual([row['id'] for row in self.due(
            after_midnight.replace(hour=23, minute=30))[0]], [pid])

    def test_invalid_daily_time_does_not_block_valid_due_or_expiration_audit(self):
        trigger_at = NOW - timedelta(minutes=121)
        expired = self.promise(trigger_at=trigger_at)
        invalid = self.promise('daily', trigger_time='21:30:00')
        valid = self.promise('daily', trigger_time='22:30')
        due, output = self.due()
        self.assertEqual([row['id'] for row in due], [valid])
        self.assertEqual(self.sql('''SELECT is_active, last_error
            FROM proactive_promise WHERE id=%s''', (expired,)), [(False, 'expired')])
        self.assert_audit(output, expired, trigger_at, 121)
        self.assertIn(f'id={invalid}', output)
        self.assertNotIn('21:30:00', output)

    def test_nondefault_grace_applies_to_once_and_daily(self):
        self.promise(trigger_at=NOW - timedelta(minutes=31))
        self.promise('daily', trigger_time='22:59')
        with patch.object(db_promise, 'PROMISE_GRACE_MINUTES', 30):
            due, output = self.due()
        self.assertEqual(due, [])
        self.assertEqual(output.count('delay_minutes=31.00'), 2)

    def test_scheduler_stops_retrying_failed_once_after_window(self):
        import proactive_scheduler
        pid = self.promise(trigger_at=NOW)
        with patch.object(proactive_scheduler, '_now', return_value=NOW) as now, \
             patch.object(proactive_scheduler, 'db_promise', db_promise), \
             patch.object(proactive_scheduler, 'generate_from_promise',
                          return_value=None) as generate, \
             redirect_stdout(io.StringIO()):
            proactive_scheduler._tick()
            now.return_value = NOW + timedelta(minutes=10)
            proactive_scheduler._tick()
            self.assertEqual(generate.call_count, 2)
            now.return_value = NOW + timedelta(minutes=120, seconds=1)
            proactive_scheduler._tick()
            now.return_value += timedelta(minutes=10)
            proactive_scheduler._tick()
            self.assertEqual(generate.call_count, 2)
        self.assertEqual(self.sql('''SELECT is_active, last_error
            FROM proactive_promise WHERE id=%s''', (pid,)), [(False, 'expired')])

    def test_schema_initialization_is_idempotent_with_nullable_last_error(self):
        with redirect_stdout(io.StringIO()):
            db_promise.init_promise_table()
        self.assertEqual(self.sql('''SELECT data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='proactive_promise'
              AND column_name='last_error' '''), [('text', 'YES')])

    def test_legacy_schema_adds_last_error_without_changing_existing_promise(self):
        pid = self.promise()
        self.sql('ALTER TABLE proactive_promise DROP COLUMN IF EXISTS last_error')
        with redirect_stdout(io.StringIO()):
            db_promise.init_promise_table()
            db_promise.init_promise_table()
        self.assertEqual(self.sql('''SELECT column_name
            FROM information_schema.columns
            WHERE table_schema='public' AND table_name='proactive_promise'
              AND column_name='last_error' '''), [('last_error',)])
        self.assertEqual(self.sql('''SELECT context, origin_text, last_error
            FROM proactive_promise WHERE id=%s''', (pid,)), [(CONTEXT, ORIGIN, None)])


def _adapt_datetimes_like_psycopg2(sql, params):
    """Quote datetimes the way psycopg2 does, without changing production SQL.

    Aware values become ``%s::timestamptz``; naive values stay ``%s::timestamp``.
    The offline adapter otherwise sends an ISO string and PostgreSQL drops the
    offset on a ``timestamp`` column, which hides the production bug.
    """
    params = tuple(params or ())
    parts = sql.split('%s')
    if len(parts) - 1 != len(params):
        return sql, params
    rewritten = [parts[0]]
    for tail, value in zip(parts[1:], params):
        if isinstance(value, datetime) and value.tzinfo is not None:
            token = '%s::timestamptz'
        elif isinstance(value, datetime):
            token = '%s::timestamp'
        else:
            token = '%s'
        rewritten.append(token)
        rewritten.append(tail)
    return ''.join(rewritten), params


class _PsycopgLikeCursor:
    def __init__(self, connection):
        self.inner = connection.cursor()

    def execute(self, sql, params=None):
        sql, params = _adapt_datetimes_like_psycopg2(sql, params)
        return self.inner.execute(sql, params)

    def fetchone(self):
        return self.inner.fetchone()

    def fetchall(self):
        return self.inner.fetchall()

    def close(self):
        self.inner.close()


class _PsycopgLikeConnection:
    def __init__(self, connection):
        self.inner = connection

    def cursor(self):
        return _PsycopgLikeCursor(self.inner)

    def commit(self):
        return self.inner.commit()

    def rollback(self):
        return self.inner.rollback()

    def close(self):
        return self.inner.close()


class LlmPromiseTimezoneTests(unittest.TestCase):
    def test_string_trigger_is_labeled_china_time_without_shifting(self):
        import generation_effects
        from config import CN_TZ

        captured = {}

        def add_promise(**kwargs):
            captured.update(kwargs)
            return 7

        saved = generation_effects.apply_proactive_promise({
            'user_id': 'u',
            'character_id': 'gojo',
            'endpoint': 'chat_text',
            'source_event_id': 'evt-wakeup',
            'user_text': '明天记得六点叫我起床',
            'add_promise': add_promise,
            'proactive_promise': {
                'trigger_kind': 'once',
                'trigger_at': '2026-10-26 06:00',
                'context': '六点叫她起床去考试',
            },
        })
        trigger_at = captured['trigger_at']
        self.assertEqual(saved['promise_id'], 7)
        self.assertEqual(trigger_at.tzinfo, CN_TZ)
        self.assertEqual(trigger_at, datetime(2026, 10, 26, 6, 0, tzinfo=CN_TZ))

    def test_aware_trigger_is_not_relabeled(self):
        import generation_effects

        captured = {}
        utc = datetime(2026, 10, 25, 22, 0, tzinfo=timezone.utc)

        def add_promise(**kwargs):
            captured.update(kwargs)
            return 8

        generation_effects.apply_proactive_promise({
            'user_id': 'u',
            'character_id': 'gojo',
            'endpoint': 'chat_text',
            'source_event_id': 'evt-utc',
            'add_promise': add_promise,
            'proactive_promise': {
                'trigger_kind': 'once',
                'trigger_at': utc,
                'context': 'already aware',
            },
        })
        self.assertIs(captured['trigger_at'], utc)


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE for disposable PostgreSQL')
class LlmPromiseTimezonePostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def setUp(self):
        self.database.rollback()
        self.database.query("SET TIME ZONE 'UTC'")
        self.database.query('DROP SCHEMA public CASCADE')
        self.database.query('CREATE SCHEMA public')
        self.adapted = _PsycopgLikeConnection(self.database)
        for patcher in (
                patch.object(db_promise, 'get_conn', return_value=self.adapted),
                patch.object(db_promise, 'PROMISE_GRACE_MINUTES', 120, create=True)):
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

    def save_llm_promise(self, trigger_at='2026-10-26 06:00', source='evt-llm'):
        import generation_effects
        with redirect_stdout(io.StringIO()):
            saved = generation_effects.apply_proactive_promise({
                'user_id': 'test-user',
                'character_id': 'gojo',
                'endpoint': 'chat_text',
                'source_event_id': source,
                'user_text': '明天记得六点叫我起床去考试',
                'proactive_promise': {
                    'trigger_kind': 'once',
                    'trigger_at': trigger_at,
                    'context': CONTEXT,
                },
            })
        return saved['promise_id']

    def due(self, now):
        output = io.StringIO()
        with redirect_stdout(output):
            result = db_promise.get_due_promises(now)
        return result, output.getvalue()

    def test_llm_trigger_is_due_at_china_six_when_session_is_utc(self):
        pid = self.save_llm_promise()
        six = datetime(2026, 10, 26, 6, 0, tzinfo=ZONE)
        self.assertEqual([row['id'] for row in self.due(six)[0]], [pid])
        self.assertEqual(self.sql('''SELECT is_active, is_fired, last_error
            FROM proactive_promise WHERE id=%s''', (pid,)), [(True, False, None)])

    def test_llm_trigger_is_not_due_eight_hours_later_under_utc(self):
        pid = self.save_llm_promise(source='evt-late')
        fourteen = datetime(2026, 10, 26, 14, 0, tzinfo=ZONE)
        due, output = self.due(fourteen)
        self.assertEqual(due, [])
        self.assertIn(f'id={pid}', output)
        self.assertEqual(self.sql('''SELECT is_active, last_error
            FROM proactive_promise WHERE id=%s''', (pid,)), [(False, 'expired')])

    def test_same_promise_is_also_due_when_session_matches_china(self):
        self.database.query("SET TIME ZONE 'Asia/Shanghai'")
        pid = self.save_llm_promise(source='evt-shanghai')
        six = datetime(2026, 10, 26, 6, 0, tzinfo=ZONE)
        self.assertEqual([row['id'] for row in self.due(six)[0]], [pid])


if __name__ == '__main__':
    unittest.main()
