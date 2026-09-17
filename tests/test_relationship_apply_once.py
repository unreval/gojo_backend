import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import raw_events  # noqa: E402
import relationship_engine  # noqa: E402


WARMTH_SIGNAL = {
    'signal_type': 'genuine_care',
    'actor': 'user',
    'confidence': 'high',
    'brief': 'care',
    'attributes': {},
}


class RelationshipApplyOnceTests(unittest.TestCase):
    def test_crash_after_application_commit_does_not_reapply_delta(self):
        """Ledger/application already committed, finish_processor not yet succeeded."""
        routes = {'n': 0}

        def route(*_args, **_kwargs):
            routes['n'] += 1
            return {'action': 'warmth+'}

        @contextmanager
        def txn():
            yield object()

        begins = {'n': 0}

        def begin(*_args, **_kwargs):
            begins['n'] += 1
            return begins['n'] == 1

        finishes = []

        def finish(*args, **kwargs):
            finishes.append(kwargs.get('status') or (args[3] if len(args) > 3 else None))

        with patch.object(relationship_engine, 'ensure_state_row'), \
             patch.object(relationship_engine, 'extract_signals',
                          return_value={'signals': [WARMTH_SIGNAL]}), \
             patch.object(relationship_engine, '_route_signal', side_effect=route), \
             patch.object(relationship_engine, '_log_interaction_stats'), \
             patch.object(relationship_engine, '_log_temporal_observation'), \
             patch.object(relationship_engine, 'cleanup_hypotheses'), \
             patch.object(relationship_engine, 'check_retreat_boundary_superseded'), \
             patch.object(relationship_engine, 'relationship_txn', txn), \
             patch.object(relationship_engine, 'try_begin_application',
                          side_effect=begin), \
             patch.object(relationship_engine, 'get_application_payload',
                          return_value={'signals': [WARMTH_SIGNAL]}), \
             patch.object(relationship_engine, '_ingest_v4',
                          return_value={'status': 'duplicate'}), \
             patch.object(raw_events, 'is_raw_event_deleted', return_value=False), \
             patch.object(raw_events, 'claim_processor', return_value='claimed'), \
             patch.object(raw_events, 'finish_processor', side_effect=finish):
            first = relationship_engine.process_turn(
                'u', 'gojo', 'hi', source_event_id='evt-rel-1')
            self.assertEqual(routes['n'], 1)
            self.assertEqual(len(first['applied']), 1)
            # Crash window: unique application already exists, processor retry.
            second = relationship_engine.process_turn(
                'u', 'gojo', 'hi', source_event_id='evt-rel-1')

        self.assertEqual(routes['n'], 1)
        self.assertEqual(second['applied'], [])
        self.assertGreaterEqual(begins['n'], 2)
        self.assertIn('succeeded', finishes)

    def test_source_validity_error_does_not_apply_or_ingest(self):
        routes = {'n': 0}

        def route(*_args, **_kwargs):
            routes['n'] += 1
            return {'action': 'warmth+'}

        ingested = {'n': 0}

        def ingest(*_args, **_kwargs):
            ingested['n'] += 1
            return {'status': 'inserted'}

        finishes = []

        def finish(*args, **kwargs):
            finishes.append(kwargs.get('status') or (args[3] if len(args) > 3 else None))

        def boom(*_args, **_kwargs):
            raise raw_events.SourceValidityError('db down')

        with patch.object(relationship_engine, 'ensure_state_row'), \
             patch.object(relationship_engine, 'extract_signals') as extract, \
             patch.object(relationship_engine, '_route_signal', side_effect=route), \
             patch.object(relationship_engine, '_ingest_v4', side_effect=ingest), \
             patch.object(raw_events, 'is_raw_event_deleted', side_effect=boom), \
             patch.object(raw_events, 'finish_processor', side_effect=finish):
            result = relationship_engine.process_turn(
                'u', 'gojo', 'hi', source_event_id='evt-rel-err')

        extract.assert_not_called()
        self.assertEqual(routes['n'], 0)
        self.assertEqual(ingested['n'], 0)
        self.assertEqual(result.get('skipped'), 'source_validity_unknown')
        self.assertIn('failed', finishes)
        self.assertNotIn('succeeded', finishes)

    def _claim_failure_harness(self, claim_side_effect):
        routes = {'n': 0}

        def route(*_args, **_kwargs):
            routes['n'] += 1
            return {'action': 'warmth+'}

        begins = {'n': 0}

        def begin(*_args, **_kwargs):
            begins['n'] += 1
            raise AssertionError('must not begin relationship application')

        stats = {'n': 0}

        def log_stats(*_args, **_kwargs):
            stats['n'] += 1

        finishes = []

        def finish(*args, **kwargs):
            finishes.append(kwargs.get('status') or (args[3] if len(args) > 3 else None))

        with patch.object(relationship_engine, 'ensure_state_row'), \
             patch.object(relationship_engine, 'extract_signals') as extract, \
             patch.object(relationship_engine, '_route_signal', side_effect=route), \
             patch.object(relationship_engine, '_log_interaction_stats',
                          side_effect=log_stats), \
             patch.object(relationship_engine, '_log_temporal_observation'), \
             patch.object(relationship_engine, 'cleanup_hypotheses'), \
             patch.object(relationship_engine, 'check_retreat_boundary_superseded'), \
             patch.object(relationship_engine, 'try_begin_application',
                          side_effect=begin), \
             patch.object(relationship_engine, '_ingest_v4') as ingest, \
             patch.object(raw_events, 'sources_are_active', return_value=True), \
             patch.object(raw_events, 'claim_processor', side_effect=claim_side_effect), \
             patch.object(raw_events, 'finish_processor', side_effect=finish):
            result = relationship_engine.process_turn(
                'u', 'gojo', 'hi', source_event_id='evt-claim-fail')

        extract.assert_not_called()
        ingest.assert_not_called()
        self.assertEqual(routes['n'], 0)
        self.assertEqual(begins['n'], 0)
        self.assertEqual(stats['n'], 0)
        self.assertEqual(result.get('skipped'), 'claim_failed')
        self.assertEqual(result.get('applied'), [])
        self.assertNotIn('succeeded', finishes)
        return finishes

    def test_claim_processor_exception_does_not_apply(self):
        finishes = self._claim_failure_harness(RuntimeError('claim db down'))
        self.assertIn('failed', finishes)

    def test_claim_processor_false_failure_does_not_apply(self):
        finishes = self._claim_failure_harness(lambda *_args, **_kwargs: False)
        self.assertIn('failed', finishes)

    def test_claim_processor_claim_failed_does_not_apply(self):
        finishes = self._claim_failure_harness(lambda *_args, **_kwargs: 'claim_failed')
        self.assertIn('failed', finishes)


if __name__ == '__main__':
    unittest.main()
