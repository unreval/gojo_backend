"""Read-only legacy projection audit and repeated startup on disposable SQL."""
import os
import unittest
from unittest.mock import patch

from tests import test_cognitive_deterministic as acceptance
from memory_projection_audit import audit_memory_projections, recover_memory_projections


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

    def _legacy_rewrite(self):
        self.report('raw', '我喜欢咖啡')
        original = '用户明确自述：我喜欢咖啡（仅限这次自述，不推断隐含心理）'
        self.sql("UPDATE long_memory SET content=%s,projection_version='legacy_v1',semantic_payload=NULL", (original,))
        self.sql('UPDATE cognitive_beliefs SET statement=%s', (original,))
        self.sql("UPDATE cognitive_events SET adjudication=jsonb_set(adjudication,'{operations,main,memory_content}',to_jsonb(%s::text))", (original,))
        self.sql("UPDATE cognitive_events SET adjudication=jsonb_set(adjudication,'{operations,main,projection_version}', '\"legacy_v1\"'::jsonb)")
        self.sql("UPDATE long_memory SET content=REPLACE(content,'用户','她') WHERE content LIKE '用户%'")
        self.database.commit()
        return original

    def test_audit_identifies_old_startup_rewrite_without_restoring_authority(self):
        self.report('raw', '我喜欢咖啡')
        # Model a pre-upgrade canonical row and its matching deterministic verdict.
        original = '用户明确自述：我喜欢咖啡（仅限这次自述，不推断隐含心理）'
        self.sql("UPDATE long_memory SET content=%s,projection_version='legacy_v1',semantic_payload=NULL", (original,))
        self.sql('UPDATE cognitive_beliefs SET statement=%s', (original,))
        self.sql("UPDATE cognitive_events SET adjudication=jsonb_set(adjudication,'{operations,main,memory_content}',to_jsonb(%s::text))", (original,))
        self.sql("UPDATE cognitive_events SET adjudication=jsonb_set(adjudication,'{operations,main,projection_version}', '\"legacy_v1\"'::jsonb)")
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
        self.assertEqual(recover_memory_projections(self.database, 'u', 'c')['eligible_ids'], [])
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
        import smart_recall
        from memory_authority import authoritative_memory_sql

        self.report('raw', '我喜欢咖啡')
        before = self.sql('SELECT content,authority,authority_event_id FROM long_memory')
        source = self.sql('SELECT text FROM chat_log')
        gate = f"SELECT content FROM long_memory WHERE {authoritative_memory_sql('long_memory')}"
        self.assertEqual(len(self.sql(gate)), 1)
        def recall_count():
            with patch('smart_recall.get_conn', return_value=self.database), \
                 patch('memory_lifecycle.recall_lifecycle_memories', return_value=[]), \
                 patch('memory_lifecycle.recall_sticky_notes', return_value=[]), \
                 patch('memory_lifecycle.recall_diary_memories', return_value=[]):
                return len(smart_recall.two_level_recall('u', 'c', '咖啡')['facts'])
        self.assertEqual(recall_count(), 1)
        self.database.rollback()
        db.init_db()
        db.init_db()
        self.assertEqual(self.sql('SELECT content,authority,authority_event_id FROM long_memory'), before)
        self.assertEqual(self.sql('SELECT text FROM chat_log'), source)
        self.assertEqual(len(self.sql(gate)), 1)
        self.assertEqual(recall_count(), 1)

    def test_semantic_projection_survives_display_only_edit(self):
        from memory_authority import authoritative_memory_sql

        self.report('raw', '我喜欢咖啡')
        self.assertEqual(self._status(), 'current_semantic_projection')
        self.sql("UPDATE long_memory SET content='显示文字改了，语义记录未变'")
        self.database.commit()
        self.assertEqual(self._status(), 'current_semantic_display_changed')
        self.assertEqual(len(self.sql(f"SELECT id FROM long_memory WHERE {authoritative_memory_sql('long_memory')}")), 1)

    def test_unknown_projection_version_does_not_enter_legacy_gate(self):
        from memory_authority import authoritative_memory_sql
        self.report('raw', '我喜欢咖啡')
        gate = f"SELECT id FROM long_memory WHERE {authoritative_memory_sql('long_memory')}"
        self.assertEqual(len(self.sql(gate)), 1)
        self.sql("UPDATE long_memory SET projection_version='future_version'")
        self.database.commit()
        self.assertEqual(self.sql(gate), [])

    def test_recovery_is_dry_run_by_default_and_rechecks_authority(self):
        from memory_authority import authoritative_memory_sql
        self._legacy_rewrite()
        before = self.sql('SELECT content,projection_version,semantic_payload FROM long_memory')
        preview = recover_memory_projections(self.database, 'u', 'c')
        self.assertTrue(preview['dry_run'])
        self.assertEqual(preview['recovery_counts'].get('recoverable'), 1, preview)
        self.assertEqual(preview['applied'], 0)
        self.assertEqual(self.sql('SELECT content,projection_version,semantic_payload FROM long_memory'), before)
        applied = recover_memory_projections(self.database, 'u', 'c', dry_run=False)
        self.assertEqual(applied['applied'], 1)
        self.assertEqual(self.sql('SELECT projection_version FROM long_memory')[0][0], 'role_view_v2')
        self.assertEqual(len(self.sql(f"SELECT id FROM long_memory WHERE {authoritative_memory_sql('long_memory')}")), 1)
        self.sql("UPDATE long_memory SET content='全新显示文字'")
        self.database.commit()
        self.assertEqual(len(self.sql(f"SELECT id FROM long_memory WHERE {authoritative_memory_sql('long_memory')}")), 1)

    def test_recovery_rejects_missing_provenance_or_inactive_raw(self):
        self._legacy_rewrite()
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='raw'")
        self.database.commit()
        self.assertEqual(recover_memory_projections(self.database, 'u', 'c')['eligible_ids'], [])
        self.sql("UPDATE chat_log SET status='active' WHERE event_id='raw'")
        self.sql("DELETE FROM memory_source_events WHERE source_event_id='raw'")
        self.database.commit()
        result = recover_memory_projections(self.database, 'u', 'c')
        self.assertEqual(result['eligible_ids'], [])
        self.assertEqual(result['recovery_counts'].get('memory_source_links_incomplete'), 1)
        self.sql("INSERT INTO memory_source_events(memory_type,memory_id,source_event_id) SELECT 'long_memory',id,'raw' FROM long_memory")
        self.sql("UPDATE cognitive_beliefs SET status='retracted'")
        self.database.commit()
        self.assertEqual(recover_memory_projections(self.database, 'u', 'c')['eligible_ids'], [])
        self.assertEqual(self.sql("SELECT projection_version FROM long_memory")[0][0], 'legacy_v1')
