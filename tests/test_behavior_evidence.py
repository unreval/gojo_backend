# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import behavior_evidence as be  # noqa: E402
import cognitive_queue  # noqa: E402


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class BehaviorEvidenceTests(unittest.TestCase):
    def setUp(self):
        be.use_memory_store(True)

    def tearDown(self):
        be.use_memory_store(False)

    def test_latency_ms_is_correct(self):
        user_at = NOW
        replied = NOW + timedelta(seconds=3.2)
        ms = be.ms_between(replied, user_at)
        self.assertAlmostEqual(ms, 3200, delta=1)

    def test_baseline_isolates_busy_state(self):
        for i in range(10):
            be.record_observation(
                'u', 'gojo', 'response_latency_ms', 30000 + i,
                busy_state='free', interaction_mode='text',
                source_user_event_id=f'u{i}', source_assistant_event_id=f'a{i}')
            be.record_observation(
                'u', 'gojo', 'response_latency_ms', 4000 + i,
                busy_state='soft_busy', interaction_mode='text',
                source_user_event_id=f'su{i}', source_assistant_event_id=f'sa{i}')
        free = be.get_baseline('gojo', 'free', 'text', 'response_latency_ms')
        busy = be.get_baseline('gojo', 'soft_busy', 'text', 'response_latency_ms')
        self.assertGreater(free['median'], 20000)
        self.assertLess(busy['median'], 10000)

    def test_insufficient_samples_no_anomaly(self):
        for i in range(3):
            be.record_observation(
                'u', 'gojo', 'response_latency_ms', 30000,
                busy_state='free', interaction_mode='text')
        result = be.record_observation(
            'u', 'gojo', 'response_latency_ms', 1000,
            busy_state='free', interaction_mode='text')
        self.assertIsNone(result['anomaly'])

    def test_fast_anomaly_detected(self):
        for i in range(12):
            be.record_observation(
                'u', 'gojo', 'response_latency_ms', 35000 + i * 10,
                busy_state='free', interaction_mode='text',
                source_user_event_id=f'u{i}', source_assistant_event_id=f'a{i}')
        result = be.record_observation(
            'u', 'gojo', 'response_latency_ms', 4000,
            busy_state='free', interaction_mode='text',
            source_user_event_id='u-fast', source_assistant_event_id='a-fast')
        self.assertIsNotNone(result['anomaly'])
        self.assertEqual(result['anomaly']['direction'], 'faster')
        self.assertEqual(result['observation']['source_user_event_id'], 'u-fast')
        self.assertEqual(result['observation']['source_assistant_event_id'], 'a-fast')

    def test_no_relationship_mutation(self):
        rel = Mock()
        with patch.dict(sys.modules, {'relationship_engine': rel}):
            be.record_observation(
                'u', 'gojo', 'response_latency_ms', 1000,
                busy_state='free', interaction_mode='text')
        rel.process_turn.assert_not_called()

    def test_diary_text_has_no_observation_api(self):
        src = Path(os.path.join(BACKEND, 'diary_engine.py')).read_text(encoding='utf-8')
        self.assertNotIn('record_observation', src)
        self.assertNotIn('record_reply_cycle', src)

    def test_recall_does_not_reinforce(self):
        for i in range(12):
            be.record_observation(
                'u', 'gojo', 'response_latency_ms', 35000,
                busy_state='free', interaction_mode='text')
        be.record_observation(
            'u', 'gojo', 'response_latency_ms', 2000,
            busy_state='free', interaction_mode='text')
        before = be.get_baseline('gojo', 'free', 'text', 'response_latency_ms')
        conf_before = [
            a.get('confidence') for a in be.list_recent_anomalies('u', 'gojo')
        ]
        be.recall_anomalies_without_reinforcement('u', 'gojo')
        after = be.get_baseline('gojo', 'free', 'text', 'response_latency_ms')
        conf_after = [
            a.get('confidence') for a in be.list_recent_anomalies('u', 'gojo')
        ]
        self.assertEqual(before['sample_count'], after['sample_count'])
        self.assertEqual(conf_before, conf_after)

    def test_cognitive_read_only_includes_anomalies(self):
        rows = be.recall_anomalies_without_reinforcement('u', 'gojo')
        self.assertEqual(rows, [])
        listed = cognitive_queue._read_behavior_anomalies('u', 'gojo')
        self.assertEqual(listed, [])
        for i in range(12):
            be.record_observation(
                'u', 'gojo', 'response_latency_ms', 30000,
                busy_state='free', interaction_mode='text')
        be.record_observation(
            'u', 'gojo', 'response_latency_ms', 1500,
            busy_state='free', interaction_mode='text')
        listed = cognitive_queue._read_behavior_anomalies('u', 'gojo')
        self.assertTrue(listed)
        self.assertEqual(listed[0]['direction'], 'faster')
        self.assertIn('not a relationship delta', listed[0]['note'])


if __name__ == '__main__':
    unittest.main()
