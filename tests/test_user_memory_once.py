import importlib.util
import os
import sys
import types
import unittest
from datetime import timedelta, timezone
from unittest.mock import Mock, patch


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def load_source(name, modules):
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            name + '_under_test', os.path.join(BACKEND, name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class FakeCursor:
    def __init__(self, returning=None, error=None):
        self.statements = []
        self.params_list = []
        self.returning = returning
        self.error = error
        self.closed = False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params_list.append(params)
        if self.error is not None and 'ON CONFLICT' in sql:
            raise self.error

    def fetchone(self):
        return self.returning

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class UniqueViolation(Exception):
    pgcode = '23505'


class SaveUserShortMemoryOnceSqlTests(unittest.TestCase):
    def setUp(self):
        self.cursor = FakeCursor(returning=(11,))
        self.conn = FakeConn(self.cursor)
        self.get_conn = Mock(return_value=self.conn)
        modules = {
            'anthropic': stub('anthropic', Anthropic=Mock()),
            'config': stub(
                'config',
                ANTHROPIC_KEY='',
                CN_TZ=timezone(timedelta(hours=8)),
                DEFAULT_CHARACTER_ID='gojo',
            ),
            'db': stub('db', get_conn=self.get_conn),
            'character_relations': stub('character_relations', get_relations_text=Mock(return_value='')),
            'raw_events': stub(
                'raw_events',
                append_raw_event=Mock(return_value={'inserted': True, 'event_id': 'evt'}),
            ),
        }
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        log_patch = patch('builtins.print')
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.memory = load_source('user_memory', modules)

    def test_sql_uses_atomic_on_conflict_returning(self):
        sql = self.memory.INSERT_USER_EVENT_ONCE_SQL
        self.assertIn('INSERT INTO short_memory', sql)
        self.assertIn('ON CONFLICT (user_id, character_id, role, source_event_id)', sql)
        self.assertIn('WHERE source_event_id IS NOT NULL', sql)
        self.assertIn('DO NOTHING', sql)
        self.assertIn('RETURNING id', sql)
        self.assertNotIn('SELECT id FROM short_memory', sql)

    def test_assistant_short_memory_sql_is_keyed_idempotent(self):
        sql = self.memory.INSERT_SHORT_EVENT_ONCE_SQL
        self.assertIn('INSERT INTO short_memory', sql)
        self.assertIn('ON CONFLICT (user_id, character_id, role, source_event_id)', sql)
        self.assertIn('WHERE source_event_id IS NOT NULL', sql)
        self.assertIn('DO NOTHING', sql)

    def test_returning_id_means_inserted(self):
        self.cursor.returning = (42,)
        inserted = self.memory.save_user_short_memory_once(
            'u', 'hello', 'gojo', source_event_id='evt-new')
        self.assertTrue(inserted)
        self.assertEqual(len(self.cursor.statements), 2)
        insert_sql = self.cursor.statements[0]
        self.assertIn('ON CONFLICT', insert_sql)
        self.assertIn('DO NOTHING', insert_sql)
        self.assertIn('RETURNING id', insert_sql)
        self.assertEqual(self.cursor.params_list[0], ('u', 'gojo', 'hello', 'evt-new', ''))
        self.assertIn('DELETE FROM short_memory', self.cursor.statements[1])
        self.assertEqual(self.conn.commits, 1)
        self.assertTrue(self.cursor.closed)
        self.assertTrue(self.conn.closed)

    def test_empty_returning_means_duplicate(self):
        self.cursor.returning = None
        inserted = self.memory.save_user_short_memory_once(
            'u', 'hello', 'gojo', source_event_id='evt-dup')
        self.assertFalse(inserted)
        self.assertEqual(len(self.cursor.statements), 1)
        self.assertIn('ON CONFLICT', self.cursor.statements[0])
        self.assertNotIn('DELETE FROM short_memory', self.cursor.statements[0])
        self.assertEqual(self.conn.commits, 1)
        self.assertTrue(self.cursor.closed)
        self.assertTrue(self.conn.closed)

    def test_missing_event_id_uses_plain_insert(self):
        inserted = self.memory.save_user_short_memory_once('u', 'hello', 'gojo')
        self.assertTrue(inserted)
        self.assertNotIn('ON CONFLICT', self.cursor.statements[0])
        self.assertEqual(self.cursor.params_list[0], ('u', 'gojo', 'user', 'hello', None, ''))
        self.assertTrue(self.cursor.closed)
        self.assertTrue(self.conn.closed)

    def test_unique_violation_returns_false_and_rollbacks(self):
        self.cursor.error = UniqueViolation('duplicate')
        inserted = self.memory.save_user_short_memory_once(
            'u', 'hello', 'gojo', source_event_id='evt-race')
        self.assertFalse(inserted)
        self.assertEqual(self.conn.rollbacks, 1)
        self.assertTrue(self.cursor.closed)
        self.assertTrue(self.conn.closed)

    def test_other_errors_rollback_close_and_raise(self):
        self.cursor.error = RuntimeError('db down')
        with self.assertRaises(RuntimeError):
            self.memory.save_user_short_memory_once(
                'u', 'hello', 'gojo', source_event_id='evt-err')
        self.assertEqual(self.conn.rollbacks, 1)
        self.assertTrue(self.cursor.closed)
        self.assertTrue(self.conn.closed)


if __name__ == '__main__':
    unittest.main()
