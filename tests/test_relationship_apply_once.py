import os
import sys
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import raw_events  # noqa: E402
import relationship_engine  # noqa: E402
from tests import test_cognitive_deterministic as acceptance


class RelationshipApplyOnceTests(unittest.TestCase):
    setUpClass = classmethod(acceptance.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(acceptance.OfflineDatabaseTests.tearDownClass.__func__)
    setUp = acceptance.OfflineDatabaseTests.setUp
    tearDown = acceptance.OfflineDatabaseTests.tearDown
    sql = acceptance.OfflineDatabaseTests.sql
    source = acceptance.OfflineDatabaseTests.source
    run_cycle = acceptance.OfflineDatabaseTests.run_cycle

    def test_observer_failure_marks_raw_processor_failed_then_retry_applies_once(self):
        """Migrated: source failure retries without ever calling an observer."""
        self.source('retry', '我喜欢咖啡')
        with patch('raw_events.get_active_events_by_ids', side_effect=raw_events.SourceValidityError('read failed')):
            with self.assertRaises(raw_events.SourceValidityError):
                relationship_engine.process_turn('u','c','copied',source_event_id='retry')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_events')[0][0],0)
        first=relationship_engine.process_turn('u','c','copied',source_event_id='retry')
        second=relationship_engine.process_turn('u','c','copied',source_event_id='retry')
        self.assertEqual(first['cognitive_ingress']['status'],'inserted')
        self.assertEqual(second['cognitive_ingress']['status'],'duplicate')
        self.run_cycle()
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_beliefs')[0][0],1)
        self.assertEqual(first['signals_applied'],0)

    def test_crash_after_application_commit_does_not_reapply_delta(self):
        """The source/queue transaction, not a second ledger, owns apply-once."""
        self.source('commit','我喜欢咖啡')
        relationship_engine.process_turn('u','c','copied',source_event_id='commit')
        self.run_cycle()
        before=self.sql('SELECT statement,confidence,evidence_refs FROM cognitive_beliefs')
        result=relationship_engine.process_turn('u','c','changed copied text',source_event_id='commit')
        self.assertEqual(result['cognitive_ingress']['status'],'duplicate')
        self.assertEqual(self.sql('SELECT statement,confidence,evidence_refs FROM cognitive_beliefs'),before)
        self.assertEqual(result['applied'],[])

    def test_source_validity_error_does_not_apply_or_ingest(self):
        with patch('raw_events.get_active_events_by_ids', side_effect=raw_events.SourceValidityError('db down')):
            with self.assertRaises(raw_events.SourceValidityError):
                relationship_engine.process_turn('u','c','我喜欢咖啡',source_event_id='unknown')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_events')[0][0],0)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_beliefs')[0][0],0)


    def test_claim_processor_exception_does_not_apply(self):
        """Migrated: trigger creation failure rolls back the source insert."""
        self.source('claim','我喜欢咖啡')
        with patch('cognitive_events.create_trigger_occurrence',side_effect=RuntimeError('queue write failed')):
            with self.assertRaises(RuntimeError):
                relationship_engine.process_turn('u','c','copied',source_event_id='claim')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_events')[0][0],0)
        result=relationship_engine.process_turn('u','c','copied',source_event_id='claim')
        self.assertEqual(result['cognitive_ingress']['status'],'inserted')

    def test_claim_processor_false_failure_does_not_apply(self):
        """A missing source is pending, even when copied text claims certainty."""
        result=relationship_engine.process_turn('u','c','我喜欢咖啡',source_event_id='missing')
        self.assertEqual(result['cognitive_ingress']['status'],'pending_canonical_source')
        self.assertEqual(result['applied'],[])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_events')[0][0],0)

    def test_claim_processor_claim_failed_does_not_apply(self):
        """A deleted source cannot claim a cognitive job."""
        self.source('deleted','我喜欢咖啡')
        self.sql("UPDATE chat_log SET status='deleted'")
        self.database.commit()
        result=relationship_engine.process_turn('u','c','我喜欢咖啡',source_event_id='deleted')
        self.assertEqual(result['cognitive_ingress']['status'],'pending_canonical_source')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_event_triggers')[0][0],0)


if __name__ == '__main__':
    unittest.main()
