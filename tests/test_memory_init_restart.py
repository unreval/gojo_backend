"""Canonical recall across real isolated database process restarts."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests import test_cognitive_deterministic as acceptance
from tests.offline_pg import Connection


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'requires disposable PGlite')
class MemoryInitRestartTests(unittest.TestCase):
    sql = acceptance.OfflineDatabaseTests.sql
    source = acceptance.OfflineDatabaseTests.source
    ingest = acceptance.OfflineDatabaseTests.ingest
    run_cycle = acceptance.OfflineDatabaseTests.run_cycle
    report = acceptance.OfflineDatabaseTests.report

    @classmethod
    def setUpClass(cls):
        pass

    @classmethod
    def tearDownClass(cls):
        pass

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[2])
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / 'database'
        self.database = Connection(self.data_dir)
        acceptance.OfflineDatabaseTests.setUp(self)

    def tearDown(self):
        try:
            acceptance.OfflineDatabaseTests.tearDown(self)
        finally:
            self.database.shutdown()

    def test_two_process_restarts_preserve_canonical_recall_and_raw_text(self):
        import db
        import smart_recall
        from memory_authority import authoritative_memory_sql

        self.report('raw', '我喜欢咖啡')
        original = self.sql('SELECT content,semantic_payload FROM long_memory')[0]
        raw = self.sql('SELECT text FROM chat_log')[0][0]
        db.init_db()
        for restart_index in range(2):
            self.database.commit()
            self.database.shutdown()
            self.database = Connection(self.data_dir)
            with patch('db.get_conn', return_value=self.database), \
                 patch('smart_recall.get_conn', return_value=self.database), \
                 patch('memory_lifecycle.recall_lifecycle_memories', return_value=[]), \
                 patch('memory_lifecycle.recall_sticky_notes', return_value=[]), \
                 patch('memory_lifecycle.recall_diary_memories', return_value=[]):
                db.init_db()
                stored = self.sql('SELECT content,semantic_payload FROM long_memory')[0]
                self.assertEqual(stored[1], original[1])
                self.assertEqual(stored[0], original[0] if restart_index == 0 else '显示文本已重排')
                self.assertEqual(self.sql('SELECT text FROM chat_log')[0][0], raw)
                gate = authoritative_memory_sql('long_memory')
                self.assertEqual(len(self.sql(f'SELECT id FROM long_memory WHERE {gate}')), 1)
                recalled = smart_recall.two_level_recall('u', 'c', '咖啡')
                self.assertEqual(len(recalled['facts']), 1)
                if restart_index == 0:
                    self.sql("UPDATE long_memory SET content='显示文本已重排'")
                    self.database.commit()


if __name__ == '__main__':
    unittest.main()
