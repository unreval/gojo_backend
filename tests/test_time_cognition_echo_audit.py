"""Characterize time-label echoes without accepting them as cognitive truth.

These are observation tests for the existing behavior, not a new reject gate.
Run with temporal_projection_offline.py to keep real vendors fail-on-call.
"""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch
import json
import unittest

import config
import context_layer
import raw_events
import route_chat
import temporal_awareness
import user_memory
from temporal_projection_offline import denied


PREFIXES = ('【历史消息', '【对话间隔', '【后端当前时间事实')
NOW = datetime(2026, 10, 14, 14, 0, tzinfo=timezone(timedelta(hours=8)))


class MemoryWriteAdapter:
    """An in-memory SQL adapter plus the canonical ledger boundary adapter."""
    def __init__(self):
        self.chat_log = []
        self.short_memory = []
        self.fetchone_value = None

    def append_raw_event(self, user_id, character_id, **event):
        self.chat_log.append(dict(user_id=user_id, character_id=character_id, **event))
        return dict(event)

    def cursor(self):
        return self

    def execute(self, sql, values):
        if sql.lstrip().startswith('INSERT INTO short_memory'):
            user_id, character_id, role, content, source_event_id = values
            self.short_memory.append(dict(user_id=user_id, character_id=character_id,
                                          role=role, content=content,
                                          source_event_id=source_event_id))
            self.fetchone_value = (len(self.short_memory),)
        elif sql.lstrip().startswith('DELETE FROM short_memory'):
            # Six one-turn fixtures never meet the production pruning limit.
            pass
        else:
            raise AssertionError('unhandled fake SQL')

    def fetchone(self):
        return self.fetchone_value

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def run_echo_case(prefix, mode, observer_error=False, temporal_log_error=False):
    from test_chat_commit_gate import ChatCommitGateTests
    fixture = ChatCommitGateTests()
    fixture.setUp()
    writer = MemoryWriteAdapter()
    if temporal_log_error:
        def fail_temporal_log(*args, **_kwargs):
            if args and str(args[0]).startswith('[temporal_check] '):
                raise BrokenPipeError('synthetic temporal log failure')
        fixture.log.side_effect = fail_temporal_log
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.dict('sys.modules', {
                'context_layer': context_layer, 'temporal_awareness': temporal_awareness,
                'config': config}))
            stack.enter_context(patch.object(user_memory, 'get_conn', return_value=writer))
            stack.enter_context(patch.object(raw_events, 'append_raw_event',
                                              side_effect=writer.append_raw_event))
            stack.enter_context(patch('reply_availability.check_reply_availability',
                                      return_value={'can_reply': True}))
            stack.enter_context(patch('db_read_receipt.mark_immediate_seen'))
            # Canonical schedule world is a fake adapter; the real reject helper
            # and real deterministic calendar guard are still executed.
            stack.enter_context(patch('schedule_transition.validate_generated_schedule_reply',
                                      return_value=(None, {})))
            fixture.route.get_temporal_snapshot = Mock(return_value={
                'now_utc': NOW.astimezone(timezone.utc), 'now_local': NOW,
                'has_history': False})
            fixture.route.find_reply_calendar_conflict = temporal_awareness.find_reply_calendar_conflict
            fixture.route._turn_context = Mock(return_value=(context_layer.ChatContextPack(), []))
            fixture.route.save_short_memory = user_memory.save_short_memory
            if observer_error:
                fixture.route._observe_temporal_candidate = Mock(
                    side_effect=RuntimeError('synthetic observer failure'))
            fixture.client.messages.create.side_effect = denied
            fixture.tts.side_effect = denied
            # TTS hydration is outside this parse/reject/store audit; the upper
            # delivery adapter returns the committed payload without synthesizing.
            import db_generation_receipt
            db_generation_receipt.hydrate_completed_generation_response = Mock(
                side_effect=lambda *_args, payload=None: {
                    key: value for key, value in (payload or {}).items()
                    if not key.startswith('_')})
            jp = prefix + '】これは合成監査の返答だ。'
            zh = prefix + '】这是合成审计回复。'
            raw = jp if mode == 'plaintext' else json.dumps({
                'emotion': '平静', 'messages': [{'jp': jp, 'zh': zh}]}, ensure_ascii=False)
            response, body = fixture.send([raw], text='你好', source_event_id='echo-synthetic')
            temporal_logs = [str(call.args[0]) for call in fixture.log.call_args_list
                             if call.args and str(call.args[0]).startswith('[temporal_check] ')]
            traces = [trace for trace in fixture.generation_traces() if 'outcome' in trace]
            fixture.client.messages.create.assert_not_called()
            fixture.tts.assert_not_called()
            return dict(prefix=prefix, mode=mode, status=response.status_code,
                        provider_adapter_calls=fixture.route._create_json.call_count,
                        trace_outcome=traces[-1]['outcome'],
                        acceptance_mode=body.get('_acceptance_mode',
                                                 traces[-1].get('acceptance_mode')),
                        visible_jp=body.get('messages', [{}])[0].get('jp'),
                        chat_log=writer.chat_log, short_memory=writer.short_memory,
                        temporal_logs=temporal_logs)
    finally:
        fixture.doCleanups()


class TimeLabelEchoObservationTests(unittest.TestCase):
    def assert_current_echo_behavior(self, prefix, mode):
        result = run_echo_case(prefix, mode)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['trace_outcome'], 'accepted')
        self.assertEqual(result['provider_adapter_calls'], 1)
        self.assertEqual(result['acceptance_mode'],
                         'plaintext' if mode == 'plaintext' else 'structured')
        self.assertTrue(result['visible_jp'].startswith(prefix))
        for name in ('chat_log', 'short_memory'):
            self.assertEqual(len(result[name]), 1)
            self.assertEqual(result[name][0]['role'], 'assistant')
            self.assertEqual(result[name][0]['content'], result['visible_jp'])
        logs = [json.loads(line.split(' ', 1)[1]) for line in result['temporal_logs']]
        self.assertEqual(len(logs), 1 if mode == 'plaintext' else 2)
        for row in logs:
            self.assertEqual(row['check_type'], 'prompt_marker_echo')
            self.assertEqual(row['error_code'], 'temporal_prompt_marker_echo')
            self.assertEqual(row['event_id'], 'echo-synthetic')
            self.assertEqual(row['now'], NOW.isoformat())
            self.assertEqual(row['intervals'], {
                'elapsed_since_previous_user': None,
                'elapsed_since_previous_assistant': None})
            self.assertTrue(row['candidate_prefix'].startswith(prefix))
            self.assertLessEqual(len(row['candidate_prefix']), 40)
            self.assertEqual(set(row), {'check_type', 'error_code', 'event_id', 'now',
                                        'intervals', 'candidate_prefix'})

    def test_history_plaintext_echo_is_accepted_and_written(self):
        self.assert_current_echo_behavior(PREFIXES[0], 'plaintext')

    def test_gap_plaintext_echo_is_accepted_and_written(self):
        self.assert_current_echo_behavior(PREFIXES[1], 'plaintext')

    def test_current_facts_plaintext_echo_is_accepted_and_written(self):
        self.assert_current_echo_behavior(PREFIXES[2], 'plaintext')

    def test_history_json_echo_is_accepted_and_written(self):
        self.assert_current_echo_behavior(PREFIXES[0], 'json')

    def test_gap_json_echo_is_accepted_and_written(self):
        self.assert_current_echo_behavior(PREFIXES[1], 'json')

    def test_current_facts_json_echo_is_accepted_and_written(self):
        self.assert_current_echo_behavior(PREFIXES[2], 'json')

    def test_leading_whitespace_each_field_and_message_logs_only_first_40(self):
        candidates = [prefix + '】' + '合成 正文\n' * 20 + '尾部不应进入日志'
                      for prefix in PREFIXES]
        parsed = {'emotion': '平静', 'messages': [
            {'jp': '\n\t ' + candidates[0], 'zh': ' \n' + candidates[1]},
            {'jp': '　\t' + candidates[2], 'zh': '普通回复。'}]}
        before = deepcopy(parsed)
        pack = context_layer.ChatContextPack(history_time_facts={
            'previous_user': NOW - timedelta(minutes=2),
            'previous_assistant': NOW - timedelta(minutes=5)})
        with patch('builtins.print') as logged:
            route_chat._observe_temporal_candidate(
                parsed, {'now_utc': NOW.astimezone(timezone.utc), 'now_local': NOW},
                pack, 'multi-field-event')
        self.assertEqual(parsed, before)
        lines = [call.args[0] for call in logged.call_args_list]
        logs = [json.loads(line.split(' ', 1)[1]) for line in lines]
        self.assertEqual(len(logs), 3)
        self.assertEqual([row['candidate_prefix'] for row in logs],
                         [candidate[:40] for candidate in candidates])
        for row in logs:
            self.assertEqual(row['check_type'], 'prompt_marker_echo')
            self.assertEqual(row['error_code'], 'temporal_prompt_marker_echo')
            self.assertEqual(row['event_id'], 'multi-field-event')
            self.assertEqual(row['now'], NOW.isoformat())
            self.assertEqual(row['intervals'], {
                'elapsed_since_previous_user': 120,
                'elapsed_since_previous_assistant': 300})
            self.assertEqual(len(row['candidate_prefix']), 40)
        self.assertNotIn('尾部不应进入日志', ''.join(lines))

    def test_marker_inside_body_does_not_trigger_prefix_observation(self):
        with patch('builtins.print') as logged:
            route_chat._observe_temporal_candidate(
                {'messages': [{'jp': '引用了' + PREFIXES[0],
                               'zh': '解释了' + PREFIXES[2]}]},
                {'now_utc': NOW.astimezone(timezone.utc), 'now_local': NOW},
                context_layer.ChatContextPack(), 'inside-body')
        logged.assert_not_called()

    def test_observer_exception_does_not_retry_reject_or_prevent_write(self):
        result = run_echo_case(PREFIXES[0], 'json', observer_error=True)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['trace_outcome'], 'accepted')
        self.assertEqual(result['provider_adapter_calls'], 1)
        self.assertEqual(result['acceptance_mode'], 'structured')
        self.assertTrue(result['visible_jp'].startswith(PREFIXES[0]))
        for name in ('chat_log', 'short_memory'):
            self.assertEqual(len(result[name]), 1)
            self.assertEqual(result[name][0]['content'], result['visible_jp'])
        self.assertEqual(result['temporal_logs'], [
            '[temporal_check] observer_error=RuntimeError'])

    def test_temporal_log_failure_does_not_retry_reject_or_prevent_write(self):
        result = run_echo_case(PREFIXES[0], 'json', temporal_log_error=True)
        self.assertEqual(result['status'], 200)
        self.assertEqual(result['trace_outcome'], 'accepted')
        self.assertEqual(result['provider_adapter_calls'], 1)
        self.assertTrue(result['visible_jp'].startswith(PREFIXES[0]))
        for name in ('chat_log', 'short_memory'):
            self.assertEqual(len(result[name]), 1)
            self.assertEqual(result[name][0]['content'], result['visible_jp'])
        # Both log attempts fail; neither exception may escape generation.
        self.assertEqual(len(result['temporal_logs']), 2)
        self.assertEqual(result['temporal_logs'][-1],
                         '[temporal_check] observer_error=BrokenPipeError')


if __name__ == '__main__':
    unittest.main()
