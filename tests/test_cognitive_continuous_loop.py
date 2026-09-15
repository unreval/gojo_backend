import inspect
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cognitive_reader
import cognitive_scheduler
import cognitive_worker
import shared_relation_prompt


NOW = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc)


class ReaderCursor:
    def __init__(self):
        self.one = None
        self.many = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.one = None
        self.many = []
        if compact.startswith('SELECT cycle_summary'):
            self.one = ({
                'summary': '角色保持实际关心，同时没有确认爱情。',
                'salient_change': '角色首次明确承认在意。',
                'uncertainty': '后续是否出现对等回应仍未知。',
                'confidence': 'medium',
            }, NOW)
        elif compact.startswith('SELECT belief_key'):
            self.many = [(
                'character.practical_care', '角色多次给出具体生活关怀。',
                0.82, NOW,
            )]
        elif compact.startswith('SELECT hypothesis_key'):
            self.many = [(
                'character.care_without_commitment',
                '角色的关心目前可能仍停留在友情。', 'open', NOW,
            )]
        elif compact.startswith('SELECT id, note_key'):
            self.many = [(
                5, 'reply.pending.topic', '记得下次接她没说完的签证话题。',
                'active', NOW, NOW,
            )]
        elif compact.startswith('SELECT id, diary_key'):
            self.many = [(
                6, 'reflection.topic', '我把这件事先记下来，但不能当成结论。',
                'event', '[]', NOW,
            )]

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class EventCursor:
    def __init__(self, payload):
        self.payload = payload

    def execute(self, sql, params=None):
        pass

    def fetchone(self):
        return (self.payload,)


class DueCursor:
    def execute(self, sql, params=None):
        self.params = params

    def fetchall(self):
        return [('u1', 'gojo', NOW), ('u2', 'gojo', NOW)]

    def close(self):
        pass


class CognitiveReaderTests(unittest.TestCase):
    def test_reader_exposes_conclusions_with_uncertainty_not_hidden_reasoning(self):
        context = cognitive_reader.build_cognitive_prompt_context(
            'u', 'gojo', conn=Connection(ReaderCursor()),
        )
        self.assertIn('近期认知复盘', context)
        self.assertIn('不是关系定论', context)
        self.assertIn('待验证理解（不能当成事实）', context)
        self.assertIn('后续是否出现对等回应仍未知', context)
        self.assertIn('便利贴备忘', context)
        self.assertIn('不是长期记忆或关系证据', context)
        self.assertNotIn('近期反思日记', context)
        self.assertNotIn('我把这件事先记下来，但不能当成结论。', context)
        self.assertNotIn('reasoning_context', context)
        self.assertNotIn('new_predictions', context)

    def test_shared_relation_prompt_reads_cognitive_context(self):
        source = inspect.getsource(shared_relation_prompt.build_relation_rules)
        self.assertIn('build_cognitive_prompt_context', source)
        self.assertIn('parts.append(cognitive_summary)', source)


class SemanticPredictionTests(unittest.TestCase):
    def test_current_event_uses_signal_selectors_and_negative_evidence_wins(self):
        from cognitive_predictions import _resolve_current_event_signal_outcome

        prediction = {
            'user_id': 'u',
            'character_id': 'gojo',
            'metadata': {
                'description': '角色会明确维持边界。',
                'fulfillment_signals': [{
                    'signal_type': 'character_stance_declared',
                    'actor': 'character',
                    'attributes': {'stance_type': 'boundary_stated'},
                }],
                'violation_signals': [{
                    'signal_type': 'character_reciprocal',
                    'actor': 'character',
                }],
            },
        }
        payload = {'signals': [
            {
                'signal_type': 'character_stance_declared',
                'actor': 'character',
                'confidence': 'high',
                'attributes': {'stance_type': 'boundary_stated'},
            },
            {
                'signal_type': 'character_reciprocal',
                'actor': 'character',
                'confidence': 'high',
                'attributes': {},
            },
        ]}
        outcome = _resolve_current_event_signal_outcome(
            EventCursor(json.dumps(payload, ensure_ascii=False)),
            prediction, 10, NOW,
        )
        self.assertEqual(outcome, -1.0)

    def test_worker_accepts_historical_evidence_already_present_in_context(self):
        self.assertEqual(
            cognitive_worker._event_ids({
                'events': [{'event_id': 27}],
                'current_beliefs': [{
                    'evidence_refs': [{'event_id': 12, 'reason': 'earlier'}],
                }],
            }),
            {12, 27},
        )


class ReflectionRunnerTests(unittest.TestCase):
    def test_due_scan_enqueues_every_active_pair(self):
        connection = Connection(DueCursor())
        with patch.object(
            cognitive_scheduler,
            'enqueue_scheduled_reflection',
            side_effect=[{'status': 'pending'}, {'status': 'duplicate'}],
        ) as enqueue:
            results = cognitive_scheduler.enqueue_due_reflections(
                scheduled_for=NOW, conn=connection,
            )
        self.assertEqual(len(results), 2)
        self.assertEqual(enqueue.call_count, 2)
        self.assertEqual(enqueue.call_args_list[0].args, ('u1', 'gojo'))
        self.assertFalse(connection.closed)

    def test_worker_scan_is_rate_limited(self):
        with patch.object(
            cognitive_worker, 'enqueue_due_reflections', return_value=[],
        ) as enqueue:
            cognitive_worker._LAST_REFLECTION_SCAN_AT = None
            cognitive_worker.maintain_scheduled_reflections(now=NOW)
            cognitive_worker.maintain_scheduled_reflections(now=NOW)
        enqueue.assert_called_once_with(scheduled_for=NOW)


if __name__ == '__main__':
    unittest.main()
