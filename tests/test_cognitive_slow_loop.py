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
        'question_updates': [{
            'question_key': 'character.care.boundary.question',
            'question_text': '角色的关心是否仍停留在保持边界的照看？',
            'status': 'active',
            'evidence_refs': [27],
        }],
        'belief_updates': [],
        'hypothesis_updates': [{
            'hypothesis_key': 'character.care_without_commitment',
            'statement': '角色存在关心，但尚未形成对等承诺。',
            'hypothesis_type': 'relationship',
            'confidence': 0.62,
            'status': 'supported',
            'question_key': 'character.care.boundary.question',
            'supporting_evidence_refs': [27],
            'contradicting_evidence_refs': [],
        }],
        'new_predictions': [{
            'prediction_key': 'care_signal.repeats.v1',
            'resolver_name': 'current_event_signal_outcome',
            'fulfillment_operator': '>=',
            'fulfillment_value': 1,
            'violation_operator': '<=',
            'violation_value': -1,
            'expires_in_seconds': 86400,
            'question_key': 'character.care.boundary.question',
            'hypothesis_key': 'character.care_without_commitment',
            'metadata': {
                'description': '等待角色后续是否仍明确保持边界。',
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
            'evidence_refs': [27],
        }],
        'evidence_refs': [{
            'event_id': 27,
            'reason': '该事件同时包含用户表态和角色明确立场。',
        }],
        'reflection_note': {
            'content': '下次若继续谈在一起，保留关心与边界之间的不确定性。',
            'evidence_refs': [27],
        },
    }


def committable_output():
    output = valid_output()
    output['evidence_refs'] = [
        {'event_id': 12, 'reason': '历史事件显示角色多次保持边界。'},
        {'event_id': 27, 'reason': '本轮事件再次显示角色保持边界。'},
    ]
    output['belief_updates'] = [{
        'belief_key': 'character.boundary_care_pattern',
        'statement': '角色常以保持边界的方式继续照看用户。',
        'confidence': 0.84,
        'status': 'active',
        'belief_type': 'interaction_pattern',
        'from_hypothesis_key': 'character.care_without_commitment',
        'evidence_refs': [12, 27],
    }]
    output['hypothesis_updates'][0]['supporting_evidence_refs'] = [12, 27]
    return output


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
        elif compact.startswith('SELECT id FROM cognitive_events'):
            self.many = [(12,)]
        elif compact.startswith('INSERT INTO cognitive_questions'):
            self.one = (71,)
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
                json.dumps(valid_output()['question_updates'], ensure_ascii=False),
                json.dumps(valid_output()['belief_updates'], ensure_ascii=False),
                '[]',
                json.dumps(valid_output()['hypothesis_updates'], ensure_ascii=False),
                json.dumps(valid_output()['new_predictions'], ensure_ascii=False),
                json.dumps(valid_output()['evidence_refs'], ensure_ascii=False),
                json.dumps(valid_output()['reflection_note'], ensure_ascii=False),
                '[]', '[]',
                'test-model', None, NOW, NOW, NOW,
            )]
        elif 'FROM cognitive_questions' in compact:
            self.many = [(
                'character.care.boundary.question',
                '角色的关心是否仍停留在保持边界的照看？',
                'active', '[]', 7, NOW,
            )]
        elif 'FROM cognitive_beliefs' in compact:
            self.many = [(
                'user.long_term_care', '用户持续表达在意。', 0.9, 'active',
                'user_model', '[]', 81, '{}', 7, NOW,
            )]
        elif 'FROM cognitive_hypotheses' in compact:
            self.many = [(
                'character.care_without_commitment', '角色关心但尚未承诺。',
                'supported', 'relationship', 0.62, '[]', '[]', '[]', 7, NOW,
            )]
        elif 'FROM cognitive_predictions' in compact:
            self.many = [(
                'care_signal.repeats.v1', 'pending',
                'current_event_signal_outcome', '>=', 1.0,
                '<=', -1.0, None, NOW, None, 7, '{}',
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
        self.assertEqual(result['question_updates'][0]['evidence_refs'], [27])
        self.assertEqual(
            result['hypothesis_updates'][0]['supporting_evidence_refs'], [27],
        )
        self.assertEqual(result['new_predictions'][0]['fulfillment_value'], 1.0)
        self.assertEqual(result['reflection_note']['evidence_refs'], [27])

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
        output['hypothesis_updates'][0]['supporting_evidence_refs'] = [28]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27, 28},
            )

    def test_confidence_update_must_reference_current_evidence(self):
        output = valid_output()
        output['evidence_refs'].insert(0, {
            'event_id': 12,
            'reason': '历史事件不能单独给本轮 confidence 加分。',
        })
        output['hypothesis_updates'][0]['supporting_evidence_refs'] = [12]
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError,
            'must_reference_current_evidence',
        ):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={12, 27}, current_event_ids={27},
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

    def test_optional_sticky_notes_and_diary_require_grounding(self):
        output = valid_output()
        output['sticky_note_updates'] = [{
            'note_key': 'reply.pending.topic',
            'content': '签证那件事她没讲完。之后得再问一句。',
            'status': 'active',
            'expires_in_seconds': 3600,
            'evidence_refs': [27],
        }]
        output['diary_entries'] = [{
            'diary_key': 'reflection.20260913.topic',
            'content': '今天这轮让我意识到，她并不是随口提起那件事。',
            'reflection_kind': 'event',
            'evidence_refs': [27],
        }]

        result = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27},
        )

        self.assertEqual(result['sticky_note_updates'][0]['status'], 'active')
        self.assertEqual(result['diary_entries'][0]['reflection_kind'], 'event')

    def test_audit_sticky_is_dropped_natural_sticky_kept(self):
        self.assertFalse(cognitive_output.is_user_facing_sticky_content(
            '用户明天要抽徽章，让我帮她选号码'))
        self.assertTrue(cognitive_output.is_user_facing_sticky_content(
            '她明天要抽徽章，还让我帮她选号。到时候看看。'))
        output = valid_output()
        output['sticky_note_updates'] = [
            {
                'note_key': 'user.gacha.audit',
                'content': '用户明天要抽徽章，让我帮她选号码',
                'status': 'active',
                'expires_in_seconds': 3600,
                'evidence_refs': [27],
            },
            {
                'note_key': 'user.gacha.personal',
                'content': '她明天要抽徽章，还让我帮她选号。到时候看看。',
                'status': 'active',
                'expires_in_seconds': 3600,
                'evidence_refs': [27],
            },
        ]
        result = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27},
        )
        keys = [item['note_key'] for item in result['sticky_note_updates']]
        self.assertEqual(keys, ['user.gacha.personal'])
        self.assertEqual(
            result['question_updates'][0]['question_key'],
            'character.care.boundary.question',
        )
        self.assertEqual(len(result['hypothesis_updates']), 1)

    def test_audit_sticky_does_not_fail_slow_loop(self):
        output = valid_output()
        output['sticky_note_updates'] = [{
            'note_key': 'user.gacha.audit',
            'content': '用户明天要抽徽章，让我帮她选号码',
            'status': 'active',
            'expires_in_seconds': 3600,
            'evidence_refs': [27],
        }]
        result = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27},
        )
        self.assertEqual(result['sticky_note_updates'], [])
        self.assertTrue(result['question_updates'])
        self.assertTrue(result['hypothesis_updates'])
        self.assertEqual(result['diary_entries'], [])

    def test_self_claim_only_cannot_commit_belief(self):
        update = committable_output()['belief_updates'][0]
        refs = [{'event_id': 1}, {'event_id': 2}]
        metadata = {
            1: {
                'source_event_type': 'character_self_claim',
                'source_event_id': 'a',
                'payload': {'evidence_category': 'character_self_claim'},
            },
            2: {
                'source_event_type': 'character_self_claim',
                'source_event_id': 'b',
                'payload': {'evidence_category': 'character_self_claim'},
            },
        }
        decision = cognitive_output._belief_commit_decision(
            update, refs, metadata, hypothesis_id=81,
        )
        self.assertEqual(decision['action'], 'held')
        self.assertEqual(decision['reason'], 'character_self_claim_only')


class SlowLoopTransactionTests(unittest.TestCase):
    def test_structured_output_and_trigger_consumption_commit_together(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        result = cognitive_queue.commit_cycle_success(
            7,
            reasoning_context={
                'events': [{'event_id': 27, 'occurred_at': NOW}],
                'temporal': {'queued_at': NOW},
            },
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
            'cognitive_questions', 'cognitive_hypotheses',
            'cognitive_predictions', 'cognitive_cycles',
        ):
            self.assertIn(table, sql)
        self.assertNotIn('INSERT INTO cognitive_beliefs', sql)
        self.assertIn("SET status = 'consumed'", sql)
        self.assertIn('cycle_summary', sql)
        self.assertIn('belief_commit_decisions', sql)
        self.assertIn('evidence_refs', sql)
        cycle_update = next(
            params for statement, params in cursor.executed
            if statement.startswith('UPDATE cognitive_cycles SET status')
        )
        self.assertIn('2026-09-13T08:00:00+00:00', cycle_update[1])

    def test_belief_commit_candidate_passes_gate_with_independent_evidence(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        result = cognitive_queue.commit_cycle_success(
            7,
            reasoning_context={
                'events': [{'event_id': 27, 'occurred_at': NOW}],
                'current_beliefs': [{
                    'evidence_refs': [{'event_id': 12, 'reason': 'earlier'}],
                }],
                'temporal': {'queued_at': NOW},
            },
            structured_output=committable_output(),
            worker_model='test-model',
            conn=connection,
            now=NOW,
        )
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertIn('INSERT INTO cognitive_beliefs', sql)
        self.assertEqual(
            result['output']['belief_commit_decisions'][0]['action'],
            'committed',
        )

    def test_invalid_output_rolls_back_before_consuming_triggers(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        output = valid_output()
        output['hypothesis_updates'][0]['supporting_evidence_refs'] = [404]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_queue.commit_cycle_success(
                7, structured_output=output, conn=connection, now=NOW,
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertNotIn("SET status = 'consumed'", sql)


class SlowLoopWorkerTests(unittest.TestCase):
    def test_invalid_stance_is_corrected_using_the_validator_enum(self):
        from cognitive_predictions import PREDICTION_STANCE_TYPES

        bad = valid_output()
        bad['new_predictions'][0]['metadata']['violation_signals'] = [{
            'signal_type': 'character_stance_declared',
            'actor': 'character',
            'attributes': {'stance_type': 'romantic_rejection'},
        }]
        call_model = Mock(side_effect=[
            (json.dumps(bad), {}),
            (json.dumps(valid_output()), {}),
        ])
        output, _ = cognitive_worker.generate_cycle_output(
            {'events': [{'event_id': 27}]}, create_chat_fn=call_model)
        self.assertEqual(call_model.call_count, 2)
        prompt = call_model.call_args.kwargs['system']
        for stance in PREDICTION_STANCE_TYPES:
            self.assertIn(stance, prompt)
        self.assertIn('violation_signals', prompt)
        self.assertIn('new_predictions may be []', prompt)
        self.assertEqual(output['evidence_refs'][0]['event_id'], 27)
        self.assertNotIn('romantic_rejection', json.dumps(output))

    def test_unsupported_stance_still_rejected_after_retry(self):
        bad = valid_output()
        bad['new_predictions'][0]['metadata']['violation_signals'] = [{
            'signal_type': 'character_stance_declared',
            'actor': 'character',
            'attributes': {'stance_type': 'invented_stance'},
        }]
        with self.assertRaisesRegex(cognitive_output.SlowLoopOutputError,
                                    'violation_signals_0_stance_type_invalid'):
            cognitive_worker.generate_cycle_output(
                {'events': [{'event_id': 27}]},
                create_chat_fn=Mock(return_value=(json.dumps(bad), {})))

    def test_empty_output_retry_does_not_send_empty_assistant_message(self):
        call_model = Mock(side_effect=[('', {}), (json.dumps(valid_output()), {})])
        cognitive_worker.generate_cycle_output(
            {'events': [{'event_id': 27}]}, create_chat_fn=call_model)
        self.assertTrue(all(message['content'].strip()
                            for message in call_model.call_args.kwargs['messages']))

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
        with patch.object(cognitive_worker, 'maintain_scheduled_reflections'), \
             patch.object(cognitive_worker, 'maintain_pending_cycles'), \
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
        with patch.object(cognitive_worker, 'maintain_scheduled_reflections'), \
             patch.object(cognitive_worker, 'maintain_pending_cycles'), \
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
        self.assertIn('question_updates', prompt)
        self.assertIn('character_self_claim', prompt)
        self.assertIn('belief_updates are commit candidates', prompt)
        self.assertIn('reflection_note is a compact internal note', prompt)
        self.assertIn('FIRST PERSON', prompt)
        self.assertIn('private sticky note', prompt)
        self.assertIn('self_disclosure', prompt)
        source = inspect.getsource(cognitive_worker)
        self.assertNotIn('UPDATE rel_state', source)
        self.assertNotIn('INSERT INTO rel_state', source)


class SlowLoopSchemaTests(unittest.TestCase):
    def test_schema_contains_all_structured_outputs_and_beliefs(self):
        ddl = '\n'.join(cognitive_db.ddl_statements())
        for field in (
            'cycle_summary', 'question_updates', 'belief_updates',
            'belief_commit_decisions', 'hypothesis_updates',
            'new_predictions', 'evidence_refs', 'reflection_note',
        ):
            self.assertIn(field, ddl)
        self.assertIn('CREATE TABLE IF NOT EXISTS cognitive_beliefs', ddl)
        self.assertIn('supporting_evidence_refs', ddl)
        self.assertIn('contradicting_evidence_refs', ddl)
        self.assertIn('self_model_evidence', ddl)
        self.assertIn('cognitive_worker_migrations', ddl)

    def test_datetime_serialization_failures_are_requeued_once(self):
        source = inspect.getsource(cognitive_db.init_cognitive_tables)
        self.assertIn('slow_worker_v1_datetime_serialization_recovery', source)
        self.assertIn(
            'slow_loop_typeerror:Object_of_type_datetime_is_not_JSON_serializable',
            source,
        )
        self.assertIn("status IN ('pending', 'dead_letter')", source)

    def test_old_count_predictions_are_superseded_once(self):
        source = inspect.getsource(cognitive_db.init_cognitive_tables)
        self.assertIn('semantic_prediction_v2_supersede_count_resolvers', source)
        self.assertIn('superseded_nonsemantic_prediction_v2', source)
        self.assertIn("resolver_name <> 'current_event_signal_outcome'", source)


class SlowLoopInspectionTests(unittest.TestCase):
    def test_snapshot_exposes_conclusions_without_reasoning_context(self):
        connection = SnapshotConnection()
        snapshot = cognitive_inspect.fetch_cognitive_snapshot(
            'u', 'gojo', limit=5, conn=connection,
        )
        self.assertEqual(snapshot['cycles'][0]['cycle_id'], 7)
        self.assertEqual(snapshot['cycles'][0]['cycle_summary']['confidence'], 'high')
        self.assertEqual(snapshot['cycles'][0]['reflection_note']['evidence_refs'], [27])
        self.assertEqual(snapshot['questions'][0]['status'], 'active')
        self.assertEqual(snapshot['beliefs'][0]['status'], 'active')
        self.assertEqual(snapshot['beliefs'][0]['belief_type'], 'user_model')
        self.assertEqual(snapshot['hypotheses'][0]['status'], 'supported')
        self.assertEqual(snapshot['hypotheses'][0]['confidence'], 0.62)
        self.assertEqual(snapshot['predictions'][0]['status'], 'pending')
        self.assertNotIn('reasoning_context', snapshot['cycles'][0])
        self.assertEqual(connection._cursor.executed[0][1], ('u', 'gojo', 5))

    def test_duplicate_diary_entries_are_not_inserted_twice(self):
        output = valid_output()
        output['diary_entries'] = [{
            'diary_key': 'exam.noticed',
            'content': '她把考试说得很轻。',
            'reflection_kind': 'event',
            'evidence_refs': [27],
        }]
        cursor = StructuredCommitCursor()
        cursor.existing_diary = [('exam.noticed', json.dumps([{'event_id': 27}]))]
        original_execute = cursor.execute

        def execute(sql, params=None):
            compact = ' '.join(sql.split())
            original_execute(sql, params)
            if compact.startswith('SELECT diary_key'):
                cursor.many = list(cursor.existing_diary)
            elif compact.startswith('SELECT id, source_event_type'):
                cursor.many = [(
                    27, 'user_message', 'e27', 'chat',
                    {'evidence_category': 'user_statement'},
                )]
            elif compact.startswith('INSERT INTO cognitive_diary_entries'):
                cursor.inserted_diary = getattr(cursor, 'inserted_diary', [])
                cursor.inserted_diary.append(params)

        cursor.execute = execute
        cognitive_output.persist_slow_loop_output(
            cursor, cycle_id=7, user_id='u', character_id='gojo',
            output=output, now=NOW)
        self.assertEqual(getattr(cursor, 'inserted_diary', []), [])


class MemoryContaminationGuardTests(unittest.TestCase):
    def test_character_self_claims_are_routed_away_from_bond_memory(self):
        with open(os.path.join(BACKEND, 'user_memory.py'), encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('character_self_claim', source)
        self.assertIn('not_bond_memory', source)
        self.assertIn('self_model_evidence', source)
        self.assertIn('bond 改道自我陈述证据', source)
        self.assertIn('_looks_like_character_self_claim(content)', source)


if __name__ == '__main__':
    unittest.main()
