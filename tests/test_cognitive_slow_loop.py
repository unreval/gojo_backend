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

import cognitive_db
import cognitive_inspect
import cognitive_output
import cognitive_queue
import cognitive_worker


NOW = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc)


def valid_output():
    return {
        'cycle_summary': {
            'summary': '用户明确表达长期在意，角色承认关心但尚未对等确认。',
            'salient_change': '角色首次明确承认会想到用户。',
            'uncertainty': '这份关心是否会发展为双向爱情仍未知。',
            'confidence': 'high',
        },
        'belief_updates': [{
            'belief_key': 'user.long_term_care',
            'statement': '用户持续、明确地表达长期在意。',
            'confidence': 0.9,
            'status': 'active',
            'evidence_refs': [27],
        }],
        'hypothesis_updates': [{
            'hypothesis_key': 'character.care_without_commitment',
            'statement': '角色存在关心，但尚未形成对等承诺。',
            'status': 'supported',
            'question_key': None,
            'evidence_refs': [27],
        }],
        'new_predictions': [{
            'prediction_key': 'care_signal.repeats.v1',
            'resolver_name': 'evidence_count_since_prediction_created',
            'fulfillment_operator': '>=',
            'fulfillment_value': 3,
            'violation_operator': None,
            'violation_value': None,
            'expires_in_seconds': 86400,
            'hypothesis_key': 'character.care_without_commitment',
            'metadata': {'description': '等待更多可观察证据'},
            'evidence_refs': [27],
        }],
        'evidence_refs': [{
            'event_id': 27,
            'reason': '该事件同时包含用户表态和角色明确立场。',
        }],
    }


class TransactionConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class StructuredCommitCursor:
    def __init__(self):
        self.one = None
        self.many = []
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        self.one = None
        self.many = []
        self.rowcount = 1
        if compact.startswith('SELECT user_id, character_id FROM cognitive_cycles'):
            self.one = ('u', 'gojo')
        elif compact.startswith('SELECT status, input_state_version'):
            self.one = ('running', 4)
        elif compact.startswith('SELECT COUNT(*), COUNT(*) FILTER'):
            self.one = (1, 1)
        elif compact.startswith('SELECT DISTINCT trigger.event_id'):
            self.many = [(27,)]
        elif compact.startswith('INSERT INTO cognitive_hypotheses'):
            self.one = (81,)
        elif compact.startswith('SELECT COUNT(*) FROM cognitive_cycles'):
            self.one = (1,)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class SnapshotCursor:
    def __init__(self):
        self.many = []
        self.executed = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        if 'FROM cognitive_cycles' in compact:
            self.many = [(
                7, 'succeeded', 'high_weight_evidence', 4, 5,
                json.dumps(valid_output()['cycle_summary'], ensure_ascii=False),
                json.dumps(valid_output()['belief_updates'], ensure_ascii=False),
                json.dumps(valid_output()['hypothesis_updates'], ensure_ascii=False),
                json.dumps(valid_output()['new_predictions'], ensure_ascii=False),
                json.dumps(valid_output()['evidence_refs'], ensure_ascii=False),
                'test-model', None, NOW, NOW, NOW,
            )]
        elif 'FROM cognitive_beliefs' in compact:
            self.many = [(
                'user.long_term_care', '用户持续表达在意。', 0.9, 'active',
                '[]', 7, NOW,
            )]
        elif 'FROM cognitive_hypotheses' in compact:
            self.many = [(
                'character.care_without_commitment', '角色关心但尚未承诺。',
                'supported', '[]', 7, NOW,
            )]
        elif 'FROM cognitive_predictions' in compact:
            self.many = [(
                'care_signal.repeats.v1', 'pending',
                'evidence_count_since_prediction_created', '>=', 3.0,
                None, None, None, NOW, None, 7, '{}',
            )]
        else:
            self.many = []

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class SnapshotConnection:
    def __init__(self):
        self._cursor = SnapshotCursor()

    def cursor(self):
        return self._cursor

    def close(self):
        pass


class SlowLoopValidationTests(unittest.TestCase):
    def test_required_output_is_normalized(self):
        result = cognitive_output.validate_slow_loop_output(
            valid_output(), allowed_event_ids={27},
        )
        self.assertEqual(result['cycle_summary']['confidence'], 'high')
        self.assertEqual(result['belief_updates'][0]['evidence_refs'], [27])
        self.assertEqual(result['new_predictions'][0]['fulfillment_value'], 3.0)

    def test_json_fence_is_tolerated_but_reasoning_is_not_saved(self):
        raw = 'preface\n```json\n' + json.dumps(valid_output()) + '\n```'
        parsed = cognitive_output.parse_slow_loop_output(raw)
        self.assertEqual(parsed['cycle_summary']['confidence'], 'high')
        self.assertNotIn('preface', json.dumps(parsed))

    def test_event_outside_cycle_is_rejected(self):
        output = valid_output()
        output['evidence_refs'][0]['event_id'] = 999
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError, 'not_in_cycle',
        ):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )

    def test_update_must_reference_declared_evidence(self):
        output = valid_output()
        output['belief_updates'][0]['evidence_refs'] = [28]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27, 28},
            )

    def test_arbitrary_prediction_resolver_is_rejected(self):
        output = valid_output()
        output['new_predictions'][0]['resolver_name'] = 'rel_state.passion'
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError, 'resolver_invalid',
        ):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )


class SlowLoopTransactionTests(unittest.TestCase):
    def test_structured_output_and_trigger_consumption_commit_together(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        result = cognitive_queue.commit_cycle_success(
            7,
            reasoning_context={'events': [{'event_id': 27}]},
            structured_output=valid_output(),
            worker_model='test-model',
            worker_usage={'input_tokens': 10, 'output_tokens': 20},
            conn=connection,
            now=NOW,
        )
        self.assertEqual(result['output_state_version'], 5)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        sql = '\n'.join(item[0] for item in cursor.executed)
        for table in (
            'cognitive_beliefs', 'cognitive_hypotheses',
            'cognitive_predictions', 'cognitive_cycles',
        ):
            self.assertIn(table, sql)
        self.assertIn("SET status = 'consumed'", sql)
        self.assertIn('cycle_summary', sql)
        self.assertIn('evidence_refs', sql)

    def test_invalid_output_rolls_back_before_consuming_triggers(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        output = valid_output()
        output['belief_updates'][0]['evidence_refs'] = [404]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_queue.commit_cycle_success(
                7, structured_output=output, conn=connection, now=NOW,
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertNotIn("SET status = 'consumed'", sql)


class SlowLoopWorkerTests(unittest.TestCase):
    def test_generate_retries_invalid_json_then_accepts_valid_output(self):
        responses = [
            ('not-json', {}),
            (json.dumps(valid_output(), ensure_ascii=False), {'input_tokens': 1}),
        ]
        call_model = Mock(side_effect=responses)
        output, usage = cognitive_worker.generate_cycle_output(
            {'events': [{'event_id': 27}]}, create_chat_fn=call_model,
        )
        self.assertEqual(call_model.call_count, 2)
        self.assertEqual(output['cycle_summary']['confidence'], 'high')
        self.assertEqual(usage['input_tokens'], 1)

    def test_worker_claims_builds_calls_and_commits(self):
        with patch.object(cognitive_worker, 'maintain_pending_cycles'), \
             patch.object(cognitive_worker, 'claim_next_cycle', return_value={
                 'status': 'running', 'cycle_id': 7,
             }), \
             patch.object(cognitive_worker, 'build_reasoning_context', return_value={
                 'events': [{'event_id': 27}],
             }), \
             patch.object(cognitive_worker, 'generate_cycle_output', return_value=(
                 valid_output(), {'input_tokens': 10},
             )), \
             patch.object(cognitive_worker, 'commit_cycle_success', return_value={
                 'status': 'succeeded', 'output_state_version': 1,
             }) as commit:
            result = cognitive_worker.run_worker_once(now=NOW)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(commit.call_args.kwargs['structured_output'], valid_output())

    def test_worker_failure_releases_cycle(self):
        with patch.object(cognitive_worker, 'maintain_pending_cycles'), \
             patch.object(cognitive_worker, 'claim_next_cycle', return_value={
                 'status': 'running', 'cycle_id': 7,
             }), \
             patch.object(cognitive_worker, 'build_reasoning_context', return_value={
                 'events': [{'event_id': 27}],
             }), \
             patch.object(
                 cognitive_worker, 'generate_cycle_output',
                 side_effect=cognitive_output.SlowLoopOutputError('bad_schema'),
             ), \
             patch.object(cognitive_worker, 'fail_cycle', return_value={
                 'status': 'failed', 'triggers': [],
             }) as fail:
            result = cognitive_worker.run_worker_once(now=NOW)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('bad_schema', fail.call_args.args[1])

    def test_worker_prompt_forbids_response_policy_and_relationship_writes(self):
        prompt = cognitive_worker._SYSTEM_PROMPT
        self.assertIn('Do not write dialogue', prompt)
        self.assertIn('modify relationship scores', prompt)
        source = inspect.getsource(cognitive_worker)
        self.assertNotIn('UPDATE rel_state', source)
        self.assertNotIn('INSERT INTO rel_state', source)


class SlowLoopSchemaTests(unittest.TestCase):
    def test_schema_contains_all_structured_outputs_and_beliefs(self):
        ddl = '\n'.join(cognitive_db.ddl_statements())
        for field in (
            'cycle_summary', 'belief_updates', 'hypothesis_updates',
            'new_predictions', 'evidence_refs',
        ):
            self.assertIn(field, ddl)
        self.assertIn('CREATE TABLE IF NOT EXISTS cognitive_beliefs', ddl)
        self.assertIn('cognitive_worker_migrations', ddl)


class SlowLoopInspectionTests(unittest.TestCase):
    def test_snapshot_exposes_conclusions_without_reasoning_context(self):
        connection = SnapshotConnection()
        snapshot = cognitive_inspect.fetch_cognitive_snapshot(
            'u', 'gojo', limit=5, conn=connection,
        )
        self.assertEqual(snapshot['cycles'][0]['cycle_id'], 7)
        self.assertEqual(snapshot['cycles'][0]['cycle_summary']['confidence'], 'high')
        self.assertEqual(snapshot['beliefs'][0]['status'], 'active')
        self.assertEqual(snapshot['hypotheses'][0]['status'], 'supported')
        self.assertEqual(snapshot['predictions'][0]['status'], 'pending')
        self.assertNotIn('reasoning_context', snapshot['cycles'][0])
        self.assertEqual(connection._cursor.executed[0][1], ('u', 'gojo', 5))


if __name__ == '__main__':
    unittest.main()
