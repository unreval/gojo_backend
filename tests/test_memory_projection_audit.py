"""Read-only legacy projection audit and repeated startup on disposable SQL."""
import os
import unittest

from tests import test_cognitive_deterministic as acceptance
from memory_projection_audit import audit_memory_projections


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'set COGNITIVE_TEST_PGLITE to the local test-only package')
class MemoryProjectionAuditTests(unittest.TestCase):
    setUpClass = classmethod(acceptance.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(acceptance.OfflineDatabaseTests.tearDownClass.__func__)
    setUp = acceptance.OfflineDatabaseTests.setUp
    tearDown = acceptance.OfflineDatabaseTests.tearDown
    sql = acceptance.OfflineDatabaseTests.sql
    source = acceptance.OfflineDatabaseTests.source
    ingest = acceptance.OfflineDatabaseTests.ingest
    run_cycle = acceptance.OfflineDatabaseTests.run_cycle
    report = acceptance.OfflineDatabaseTests.report

    def _status(self):
        return next(row['status'] for row in audit_memory_projections(
            self.database, 'u', 'c')['records'] if row['table'] == 'long_memory')

    def test_audit_identifies_old_startup_rewrite_without_restoring_authority(self):
        self.report('raw', '我喜欢咖啡')
        # Model a pre-upgrade canonical row and its matching deterministic verdict.
        original = '用户明确自述：我喜欢咖啡（仅限这次自述，不推断隐含心理）'
        self.sql("UPDATE long_memory SET content=%s,projection_version='legacy_v1',semantic_payload=NULL", (original,))
        self.sql('UPDATE cognitive_beliefs SET statement=%s', (original,))
        self.sql("UPDATE cognitive_events SET adjudication=jsonb_set(adjudication,'{operations,main,memory_content}',to_jsonb(%s::text))", (original,))
        self.database.commit()
        self.assertEqual(self._status(), 'current_legacy_user')
        self.sql("UPDATE long_memory SET content=REPLACE(content,'用户','她') WHERE content LIKE '用户%'")
        changed = self.sql('SELECT content FROM long_memory')[0][0]
        self.assertNotEqual(changed, original)
        self.assertEqual(self._status(), 'legacy_startup_rewrite_candidate')
        self.assertEqual(self.sql('SELECT text FROM chat_log')[0][0], '我喜欢咖啡')
        # The audit only reports the current authority gate; it never repairs.
        self.assertTrue(audit_memory_projections(self.database, 'u', 'c')['records'][0]['recall_eligible'])
        self.assertEqual(self.sql('SELECT content FROM long_memory')[0][0], changed)

    def test_audit_separates_history_and_missing_evidence(self):
        self.report('raw', '我喜欢咖啡')
        self.sql("INSERT INTO long_memory(user_id,character_id,content) VALUES ('u','c','用户住在火星')")
        self.database.commit()
        audit = audit_memory_projections(self.database, 'u', 'c')
        self.assertEqual(audit['counts']['historical_only'], 1)
        self.assertFalse(next(row['recall_eligible'] for row in audit['records'] if row['status'] == 'historical_only'))
        self.assertEqual(sum(audit['counts'].get(key, 0) for key in
                             ('current_legacy_user', 'current_legacy_she', 'current_projection',
                              'current_semantic_projection')), 1, audit)
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='raw'")
        self.database.commit()
        self.assertIn('raw_source_inactive', audit_memory_projections(
            self.database, 'u', 'c')['counts'])
        self.sql("UPDATE chat_log SET status='active',chat_id='other' WHERE event_id='raw'")
        self.database.commit()
        self.assertIn('raw_source_scope_mismatch', audit_memory_projections(
            self.database, 'u', 'c')['counts'])
        self.sql("UPDATE long_memory SET recall_status='superseded' WHERE authority_event_id IS NOT NULL")
        self.database.commit()
        self.assertIn('projection_inactive', audit_memory_projections(
            self.database, 'u', 'c')['counts'])

    def test_repeated_init_preserves_canonical_projection_and_raw_source(self):
        import db
        from memory_authority import authoritative_memory_sql

        self.report('raw', '我喜欢咖啡')
        before = self.sql('SELECT content,authority,authority_event_id FROM long_memory')
        source = self.sql('SELECT text FROM chat_log')
        gate = f"SELECT content FROM long_memory WHERE {authoritative_memory_sql('long_memory')}"
        self.assertEqual(len(self.sql(gate)), 1)
        db.init_db()
        db.init_db()
        self.assertEqual(self.sql('SELECT content,authority,authority_event_id FROM long_memory'), before)
        self.assertEqual(self.sql('SELECT text FROM chat_log'), source)
        self.assertEqual(len(self.sql(gate)), 1)

    def test_semantic_projection_survives_display_only_edit(self):
        from memory_authority import authoritative_memory_sql

        self.report('raw', '我喜欢咖啡')
        self.assertEqual(self._status(), 'current_semantic_projection')
        self.sql("UPDATE long_memory SET content='显示文字改了，语义记录未变'")
        self.database.commit()
        self.assertEqual(self._status(), 'current_semantic_display_changed')
        self.assertEqual(len(self.sql(f"SELECT id FROM long_memory WHERE {authoritative_memory_sql('long_memory')}")), 1)
