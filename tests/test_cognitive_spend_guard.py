"""Offline regression tests. Injected providers never contact a paid API."""
import copy
import json
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

BACKEND = Path(__file__).resolve().parent.parent / 'gojo_backend'
if BACKEND.is_dir():
    sys.path.insert(0, str(BACKEND))
import cognitive_request_budget as budget
import ai_client
import cognitive_queue as queue
import cognitive_worker as worker

NOW = datetime(2026, 9, 30, 5, tzinfo=timezone.utc)


def context():
    return {'cycle_id': 1, 'events': [{'event_id': 27, 'payload': {'text': '本轮事实'}}],
            'triggers': [], 'current_hypotheses': []}


def valid_output():
    return {'cycle_summary': {'summary': '记录本轮事实。', 'salient_change': '',
                             'uncertainty': '', 'confidence': 'low'},
            'question_updates': [], 'belief_updates': [], 'hypothesis_updates': [],
            'new_predictions': [], 'evidence_refs': [{'event_id': 27, 'reason': '本轮事实'}],
            'reflection_note': {'content': '', 'evidence_refs': []}}


def good_response():
    return json.dumps(valid_output(), ensure_ascii=False), {'stop_reason': 'end_turn'}


class Cursor:
    def __init__(self, attempts=3, last_error=None):
        self.attempts = attempts
        self.last_error = last_error
        self.executed = []
        self.rows = []
        self.one = None
        self.rowcount = 0

    def execute(self, sql, params=None):
        sql = ' '.join(sql.split())
        self.executed.append((sql, params))
        self.one, self.rows = None, []
        if sql.startswith('SELECT user_id, character_id'):
            self.one = ('u', 'gojo')
        elif sql.startswith('SELECT status FROM cognitive_cycles'):
            self.one = ('running',)
        elif sql.startswith('SELECT trigger.id, trigger.attempt_count'):
            self.rows = [(1, self.attempts)]
        elif sql.startswith('SELECT id, claimed_by_cycle_id, attempt_count'):
            self.rows = [(1, 4, self.attempts, self.last_error)]
        elif sql.startswith('UPDATE cognitive_event_triggers'):
            self.attempts = params[1]

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows[:]

    def close(self):
        pass


class Connection:
    def __init__(self, cursor):
        self.cur = cursor
        self.commits = self.rollbacks = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class RetryAccountingTests(unittest.TestCase):
    def test_preserve_flag_does_not_refund_attempt(self):
        cur = Cursor(attempts=1)
        result = queue.fail_cycle(4, 'slow_loop_output_truncated', preserve_raw_evidence=True,
                                  conn=Connection(cur), now=NOW)
        self.assertEqual(cur.attempts, 1)
        self.assertEqual(result['triggers'][0]['status'], 'pending')

    def test_preserved_evidence_can_reach_dead_letter(self):
        cur = Cursor(attempts=3)
        result = queue.fail_cycle(4, 'slow_loop_output_truncated', preserve_raw_evidence=True,
                                  conn=Connection(cur), now=NOW)
        self.assertEqual(result['triggers'][0]['status'], 'dead_letter')
        self.assertEqual(cur.attempts, 3)
        self.assertTrue(result['raw_evidence_preserved'])
        self.assertIsNone(result['output_state_version'])

    def test_same_trigger_never_starts_fourth_cycle(self):
        for reason in ('model_output_truncated', '503_no_available_accounts', 'invalid_json'):
            with self.subTest(reason=reason):
                cur, statuses = Cursor(attempts=0), []
                for _ in range(4):
                    cur.attempts += 1  # The existing claim operation.
                    result = queue.fail_cycle(4, 'slow_loop_' + reason, preserve_raw_evidence=True,
                                              conn=Connection(cur), now=NOW)
                    statuses.append(result['triggers'][0]['status'])
                    if statuses[-1] == 'dead_letter':
                        break
                self.assertEqual(statuses, ['pending', 'pending', 'dead_letter'])

    def test_lease_recovery_cannot_refund_failed_attempt(self):
        cur = Cursor(attempts=3, last_error='slow_loop_output_truncated')
        queue._recover_expired_claims(cur, 'u', 'gojo', NOW)
        updates = [p for sql, p in cur.executed if sql.startswith('UPDATE cognitive_event_triggers')]
        self.assertEqual(updates[0][0:2], ('dead_letter', 3))
        self.assertEqual(updates[0][2], 'slow_loop_output_truncated')

    def test_failure_does_not_delete_sources_or_consume_evidence(self):
        cur = Cursor()
        queue.fail_cycle(4, 'slow_loop_failed', preserve_raw_evidence=True,
                         conn=Connection(cur), now=NOW)
        sql = '\n'.join(s for s, _ in cur.executed)
        self.assertNotIn('DELETE', sql)
        self.assertNotIn('UPDATE chat_log', sql)
        self.assertNotIn("SET status = 'consumed'", sql)
        self.assertIn('consumed_cycle_id = NULL', sql)

    def test_original_default_retry_path_still_bounded(self):
        cur = Cursor()
        result = queue.fail_cycle(4, 'other_error', conn=Connection(cur), now=NOW)
        self.assertEqual(result['triggers'][0]['status'], 'dead_letter')


class RequestBudgetTests(unittest.TestCase):
    def test_large_revision_log_is_not_reinjected(self):
        original = context()
        history = [{
            'cycle_id': index,
            'confidence': 0.5,
            'status': 'supported',
            'supporting_evidence_refs': [{'event_id': 27}],
            'contradicting_evidence_refs': [{'event_id': 8}],
        } for index in range(600)]
        original['current_hypotheses'] = [{
            'statement': '保留当前判断',
            'supporting_evidence_refs': [{'event_id': 27}],
            'contradicting_evidence_refs': [{'event_id': 8}],
            'evidence': history,
        }]
        before = copy.deepcopy(original)
        projected = budget.project_request_context(original)
        self.assertEqual(original, before)
        hypothesis = projected['current_hypotheses'][0]
        self.assertNotIn('evidence', hypothesis)
        self.assertEqual(hypothesis['contradicting_evidence_refs'], [{'event_id': 8}])
        self.assertEqual(hypothesis['evidence_history_omitted_count'], len(history))
        budget.check_request_budget('schema', [{'role': 'user', 'content': budget.compact_json(projected)}])

    def test_critical_history_with_correction_or_provenance_is_not_omitted(self):
        original = context()
        history = [{
            'cycle_id': 3,
            'confidence': 0.2,
            'status': 'rejected',
            'supporting_evidence_refs': [{'event_id': 27}],
            'contradicting_evidence_refs': [{'event_id': 8}],
            'explicit_correction': {'event_id': 8, 'content': '此前判断不成立'},
            'provenance': {'source_event_id': 'source-turn-8'},
        }]
        original['current_hypotheses'] = [{
            'statement': '保留当前判断',
            'supporting_evidence_refs': [{'event_id': 27}],
            'contradicting_evidence_refs': [{'event_id': 8}],
            'evidence': history,
        }]
        before = copy.deepcopy(original)
        projected = budget.project_request_context(original)
        self.assertEqual(projected['current_hypotheses'][0]['evidence'], history)
        self.assertNotIn('evidence_history_omitted_count', projected['current_hypotheses'][0])
        self.assertEqual(original, before)

    def test_history_only_legacy_evidence_is_not_removed(self):
        data = context()
        data['current_hypotheses'] = [{'evidence': [{'event_id': 999}]}]
        self.assertEqual(budget.project_request_context(data), data)

    def test_empty_reference_channels_do_not_authorize_history_removal(self):
        data = context()
        data['current_hypotheses'] = [{'supporting_evidence_refs': [],
                                      'contradicting_evidence_refs': [],
                                      'evidence': [{'event_id': 999}]}]
        self.assertEqual(budget.project_request_context(data), data)

    def test_unrepresented_historical_ref_is_retained(self):
        data = context()
        data['current_hypotheses'] = [{'supporting_evidence_refs': [{'event_id': 27}],
                                      'contradicting_evidence_refs': [],
                                      'evidence': [{'event_id': 999}]}]
        self.assertEqual(budget.project_request_context(data), data)

    def test_legacy_integer_historical_ref_is_not_lost(self):
        data = context()
        data['current_hypotheses'] = [{'supporting_evidence_refs': [27],
                                      'contradicting_evidence_refs': [],
                                      'evidence': [{'supporting_evidence_refs': [999]}]}]
        self.assertEqual(budget.project_request_context(data), data)

    def test_unrepresented_canonical_source_id_is_retained(self):
        data = context()
        data['current_hypotheses'] = [{'supporting_evidence_refs': [{'event_id': 27}],
                                      'contradicting_evidence_refs': [],
                                      'evidence': [{'source_event_id': 'old-canonical-event'}]}]
        self.assertEqual(budget.project_request_context(data), data)

    def test_oversized_current_event_rejected_before_api(self):
        data = context()
        data['events'][0]['payload']['text'] = 'a' * 114117
        before = copy.deepcopy(data)
        provider = Mock(return_value=good_response())
        with self.assertRaisesRegex(budget.SlowLoopRequestBudgetError, 'request_budget_exceeded'):
            worker.generate_cycle_output(data, create_chat_fn=provider)
        provider.assert_not_called()
        self.assertEqual(data, before)

    def test_utf8_bytes_not_character_count(self):
        with self.assertRaises(budget.SlowLoopRequestBudgetError):
            budget.check_request_budget('', [{'role': 'user', 'content': '汉' * 25000}])

    def test_system_prompt_counts_against_limit(self):
        with self.assertRaises(budget.SlowLoopRequestBudgetError):
            budget.check_request_budget('a' * 65536, [])

    def test_all_retry_messages_count_against_limit(self):
        messages = [{'role': 'user', 'content': 'a' * 34000},
                    {'role': 'user', 'content': 'b' * 34000}]
        with self.assertRaises(budget.SlowLoopRequestBudgetError):
            budget.check_request_budget('schema', messages)

    def test_exact_boundary(self):
        messages = [{'role': 'user', 'content': '正常内容'}]
        size = budget.check_request_budget('schema', messages)
        self.assertEqual(budget.check_request_budget('schema', messages, limit=size), size)
        with self.assertRaises(budget.SlowLoopRequestBudgetError):
            budget.check_request_budget('schema', messages, limit=size - 1)

    def test_current_events_resolutions_and_opposition_are_unchanged(self):
        data = context()
        data.update({'explicit_resolutions': [{'value': 'no', 'event_id': 30}],
                     'current_questions': [{'question_key': 'q', 'metadata': {'current_judgment': 'no'}}]})
        self.assertEqual(budget.project_request_context(data), data)

    def test_compact_json_is_lossless(self):
        data = {'quote': '不是讨厌你。\n"原话"', 'refs': [27, 8]}
        self.assertEqual(json.loads(budget.compact_json(data)), data)

    def test_oversized_update_list_rejected(self):
        value = valid_output()
        value['question_updates'] = [{}, {}, {}]
        with self.assertRaisesRegex(budget.SlowLoopRequestBudgetError, 'output_budget_exceeded'):
            budget.validate_output_budget(value)

    def test_empty_updates_are_valid(self):
        value = valid_output()
        self.assertEqual(budget.validate_output_budget(value), value)


class WorkerRecoveryTests(unittest.TestCase):
    def test_truncation_retry_changes_budget_once(self):
        provider = Mock(side_effect=[('{', {'stop_reason': 'max_tokens'}), good_response()])
        with patch.object(worker, 'COGNITIVE_WORKER_MAX_TOKENS', 1800), \
             patch.object(worker, 'COGNITIVE_WORKER_MODEL_ATTEMPTS', 2):
            value, _ = worker.generate_cycle_output(context(), create_chat_fn=provider)
        self.assertEqual([c.kwargs['max_tokens'] for c in provider.call_args_list], [1800, 3600])
        self.assertEqual(value['cycle_summary']['summary'], '记录本轮事实。')

    def test_repeated_truncation_stops_after_two_calls(self):
        provider = Mock(return_value=('{', {'stop_reason': 'max_tokens'}))
        with patch.object(worker, 'COGNITIVE_WORKER_MODEL_ATTEMPTS', 50):
            with self.assertRaises(worker.SlowLoopOutputError):
                worker.generate_cycle_output(context(), create_chat_fn=provider)
        self.assertEqual(provider.call_count, 2)

    def test_at_output_ceiling_no_identical_truncation_retry(self):
        provider = Mock(return_value=('{', {'stop_reason': 'max_tokens'}))
        with patch.object(worker, 'COGNITIVE_WORKER_MAX_TOKENS', 4096):
            with self.assertRaises(worker.SlowLoopOutputError):
                worker.generate_cycle_output(context(), create_chat_fn=provider)
        self.assertEqual(provider.call_count, 1)

    def test_complete_json_with_truncation_still_fails_closed(self):
        provider = Mock(return_value=(good_response()[0], {'stop_reason': 'max_tokens'}))
        with self.assertRaises(worker.SlowLoopOutputError):
            worker.generate_cycle_output(context(), create_chat_fn=provider)

    def test_provider_503_not_validation_retried(self):
        provider = Mock(side_effect=RuntimeError('503 no_available_accounts'))
        with self.assertRaisesRegex(RuntimeError, '503'):
            worker.generate_cycle_output(context(), create_chat_fn=provider)
        self.assertEqual(provider.call_count, 1)

    def test_retry_does_not_add_failed_model_text_as_evidence(self):
        provider = Mock(side_effect=[('UNTRUSTED_PREVIOUS_OUTPUT', {'stop_reason': 'max_tokens'}), good_response()])
        worker.generate_cycle_output(context(), create_chat_fn=provider)
        messages = provider.call_args_list[-1].kwargs['messages']
        self.assertTrue(all(m['role'] == 'user' for m in messages))
        self.assertNotIn('UNTRUSTED_PREVIOUS_OUTPUT', repr(messages))

    def test_default_slow_loop_call_disables_sdk_retries(self):
        fake = types.ModuleType('ai_client')
        fake.create_chat = Mock(return_value=good_response())
        with patch.dict(sys.modules, {'ai_client': fake}):
            worker.generate_cycle_output(context())
        self.assertEqual(fake.create_chat.call_args.kwargs.get('max_retries'), 0)

    def test_unmodified_context_is_kept_for_commit_owner(self):
        data = context()
        data['current_hypotheses'] = [{'supporting_evidence_refs': [{'event_id': 27}],
                                      'contradicting_evidence_refs': [],
                                      'evidence': [{'event_id': 27, 'history': 'a' * 1000}]}]
        before = copy.deepcopy(data)
        worker.generate_cycle_output(data, create_chat_fn=Mock(return_value=good_response()))
        self.assertEqual(data, before)

    def test_configured_large_output_is_capped(self):
        provider = Mock(return_value=good_response())
        with patch.object(worker, 'COGNITIVE_WORKER_MAX_TOKENS', 100000):
            worker.generate_cycle_output(context(), create_chat_fn=provider)
        self.assertEqual(provider.call_args.kwargs['max_tokens'], 4096)


class ClientRetryIsolationTests(unittest.TestCase):
    def test_anthropic_sdk_retry_override_is_opt_in(self):
        response = types.SimpleNamespace(
            content=[], usage=None, stop_reason='end_turn', id='response-1',
        )
        configured_client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=Mock(return_value=response)),
        )
        client = types.SimpleNamespace(
            messages=types.SimpleNamespace(create=Mock(return_value=response)),
            with_options=Mock(return_value=configured_client),
        )
        messages = [{'role': 'user', 'content': 'offline'}]
        with patch.object(ai_client, '_get_anthropic', return_value=client):
            ai_client.create_chat('claude-test', messages)
            ai_client.create_chat('claude-test', messages, max_retries=0)
        client.with_options.assert_called_once_with(max_retries=0)
        client.messages.create.assert_called_once()
        configured_client.messages.create.assert_called_once()


if __name__ == '__main__':
    unittest.main()
