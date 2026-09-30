"""User cache SQL and source-first behavior on real disposable PostgreSQL."""
import unittest

from tests import test_private_source_first as fixture
import user_memory
import raw_events


class SaveUserShortMemoryOnceSqlTests(unittest.TestCase):
    setUpClass = classmethod(fixture.PrivateSourceFirstTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PrivateSourceFirstTests.tearDownClass.__func__)
    tearDown = fixture.PrivateSourceFirstTests.tearDown
    sql = fixture.PrivateSourceFirstTests.sql

    def setUp(self):
        fixture.PrivateSourceFirstTests.setUp(self)
        self.memory = user_memory

    def test_sql_uses_atomic_on_conflict_returning(self):
        sql = self.memory.INSERT_USER_EVENT_ONCE_SQL
        self.assertIn('ON CONFLICT (user_id, character_id, role, source_event_id)', sql)
        self.assertIn('WHERE source_event_id IS NOT NULL', sql)
        self.assertIn('DO NOTHING', sql)
        self.assertIn('RETURNING id', sql)
        self.assertNotIn('SELECT id FROM short_memory', sql)

    def test_assistant_short_memory_sql_is_keyed_idempotent(self):
        sql = self.memory.INSERT_SHORT_EVENT_ONCE_SQL
        self.assertIn('ON CONFLICT (user_id, character_id, role, source_event_id)', sql)
        self.assertIn('WHERE source_event_id IS NOT NULL', sql)
        self.assertIn('DO NOTHING', sql)

    def test_returning_id_means_inserted(self):
        self.assertTrue(self.memory.save_user_short_memory_once('u', 'hello', 'c', source_event_id='new'))
        self.assertEqual(self.sql('SELECT content,source_event_id FROM short_memory'), [('hello','new')])
        self.assertEqual(self.sql('SELECT event_id FROM chat_log'), [('new',)])
        self.assertEqual(self.sql('SELECT source_event_id FROM memory_jobs'), [('new',)])

    def test_empty_returning_means_duplicate(self):
        self.memory.save_user_short_memory_once('u', 'hello', 'c', source_event_id='dup')
        self.assertFalse(self.memory.save_user_short_memory_once('u', 'hello', 'c', source_event_id='dup'))
        self.assertEqual(self.sql('SELECT count(*) FROM short_memory')[0][0], 1)
        self.assertEqual(self.sql('SELECT count(*) FROM memory_jobs')[0][0], 1)

    def test_missing_event_id_uses_plain_insert(self):
        # Legacy callers now receive a source identity before the cache write.
        self.assertTrue(self.memory.save_user_short_memory_once('u', 'hello', 'c'))
        self.assertTrue(self.memory.save_user_short_memory_once('u', 'hello', 'c'))
        ids = [row[0] for row in self.sql('SELECT source_event_id FROM short_memory')]
        self.assertEqual(len(set(ids)), 2)
        self.assertTrue(all(ids))
        self.assertEqual(self.sql('SELECT count(*) FROM chat_log')[0][0], 2)
        self.assertEqual(self.sql('SELECT count(*) FROM memory_jobs')[0][0], 2)

    def test_unique_violation_returns_false_and_rollbacks(self):
        self.sql("ALTER TABLE short_memory ADD CONSTRAINT content_unique UNIQUE(content)")
        self.database.commit()
        self.assertTrue(self.memory.save_user_short_memory_once('u', 'hello', 'c', source_event_id='one'))
        self.assertFalse(self.memory.save_user_short_memory_once('u', 'hello', 'c', source_event_id='two'))
        self.assertEqual(self.sql('SELECT count(*) FROM short_memory')[0][0], 1)
        self.assertEqual(self.sql('SELECT count(*) FROM chat_log')[0][0], 2)
        self.assertEqual(self.sql('SELECT count(*) FROM memory_jobs')[0][0], 2)

    def test_other_errors_rollback_close_and_raise(self):
        self.sql('ALTER TABLE chat_log RENAME COLUMN text TO unavailable_text')
        self.database.commit()
        with self.assertRaises(Exception):
            self.memory.save_user_short_memory_once('u', 'hello', 'c', source_event_id='error')
        self.assertEqual(self.sql('SELECT count(*) FROM short_memory')[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM memory_jobs')[0][0], 0)


if __name__ == '__main__':
    unittest.main()
