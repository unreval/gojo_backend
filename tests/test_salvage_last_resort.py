"""Plaintext salvage waits for the last attempt, then one semantic retry."""
import json
import os
import sys
import types
import unittest
from contextlib import ExitStack
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

import route_chat  # noqa: E402


JAPANESE = '今日はゆっくり話そう'
CHINESE = '现在我们慢慢聊吧'
VALID = json.dumps(
    {'emotion': '平静', 'messages': [{'jp': 'そうだね', 'zh': '是啊'}]},
    ensure_ascii=False)
BROKEN = '{"messages":['


def _traces(logged):
    rows = []
    for call in logged.call_args_list:
        if not call.args or not isinstance(call.args[0], str):
            continue
        if call.args[0].startswith('[generation_trace] {'):
            rows.append(json.loads(call.args[0].split(' ', 1)[1]))
    return [row for row in rows if 'outcome' in row]


class SalvageLastResortTests(unittest.TestCase):
    def generate(self, raws, *, attempts=2):
        response = types.SimpleNamespace(stop_reason='end_turn', usage=None)
        queue = list(raws)
        with patch.object(
                route_chat, '_create_json',
                side_effect=lambda *_args, **_kwargs: (queue.pop(0), response)) as provider, \
                patch('builtins.print') as logged:
            result, state = route_chat._generate_or_none(
                'offline', 100, [], [], attempts=attempts, log_tag='test',
                cache_tag='test', salvage=True,
                generation_trace={'source_event_id': 'salvage-last-resort'})
        return result, state, provider, _traces(logged)

    def test_japanese_prose_retries_once_then_accepts_structured_translation(self):
        result, state, provider, traces = self.generate([JAPANESE, VALID])
        self.assertEqual(provider.call_count, 2)
        self.assertIsNone(state)
        self.assertEqual(traces[-1]['acceptance_mode'], 'structured')
        self.assertEqual(traces[-1]['outcome'], 'accepted')
        self.assertTrue(result['messages'][0]['zh'].strip())
        self.assertEqual(result['messages'][0]['zh'], '是啊')
        self.assertNotIn('_acceptance_mode', result)

    def test_first_rejection_is_plaintext_verbal_and_marks_retry(self):
        _result, _state, provider, traces = self.generate([JAPANESE, VALID])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(traces[0]['retry_reason'], 'plaintext_verbal')
        self.assertEqual(traces[0]['parse_invalid_reason'], 'plaintext_verbal')
        self.assertTrue(traces[0]['will_retry'])
        self.assertEqual(traces[0]['outcome'], 'parse_invalid')

    def test_two_japanese_prose_attempts_still_degrade_with_empty_zh(self):
        result, state, provider, traces = self.generate([JAPANESE, JAPANESE])
        self.assertEqual(provider.call_count, 2)
        self.assertIsNone(state)
        self.assertEqual(result['_acceptance_mode'], 'plaintext')
        self.assertEqual(result['messages'], [{'jp': JAPANESE, 'zh': ''}])
        self.assertEqual(traces[1]['acceptance_mode'], 'plaintext')
        self.assertFalse(traces[1]['will_retry'])
        from test_chat_commit_gate import ChatCommitGateTests
        host = ChatCommitGateTests()
        host.setUp()
        try:
            response, body = host.send([JAPANESE, JAPANESE], source_event_id='salvage-plain')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(host.route._create_json.call_count, 2)
            self.assertEqual(body['messages'][0]['zh'], '')
            self.assertNotIn('generation_failed', body)
        finally:
            host.doCleanups()

    def test_nonverbal_ellipsis_still_accepts_on_first_attempt(self):
        result, state, provider, traces = self.generate(['……', VALID])
        self.assertEqual(provider.call_count, 1)
        self.assertIsNone(state)
        self.assertEqual(result['_acceptance_mode'], 'nonverbal')
        self.assertEqual(result['messages'], [{'jp': '……', 'zh': '……'}])
        self.assertEqual(traces[0]['acceptance_mode'], 'nonverbal')
        self.assertFalse(traces[0]['will_retry'])

    def test_single_attempt_caller_still_degrades_japanese_prose_immediately(self):
        result, state, provider, traces = self.generate([JAPANESE], attempts=1)
        self.assertEqual(provider.call_count, 1)
        self.assertIsNone(state)
        self.assertEqual(result['_acceptance_mode'], 'plaintext')
        self.assertEqual(result['messages'], [{'jp': JAPANESE, 'zh': ''}])
        self.assertEqual(traces[0]['outcome'], 'accepted')
        self.assertFalse(traces[0]['will_retry'])

    def test_chinese_prose_is_still_rejected_after_retry(self):
        result, state, provider, traces = self.generate([CHINESE, CHINESE])
        self.assertIsNone(result)
        self.assertIsNone(state)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(traces[0]['retry_reason'], 'plaintext_verbal')
        self.assertTrue(traces[0]['will_retry'])
        self.assertEqual(traces[1]['retry_reason'], 'jp_not_japanese')
        self.assertFalse(traces[1]['will_retry'])
        from test_chat_commit_gate import ChatCommitGateTests
        host = ChatCommitGateTests()
        host.setUp()
        try:
            response, body = host.send([CHINESE, CHINESE], source_event_id='salvage-zh')
            self.assertEqual(response.status_code, 502)
            self.assertEqual(body['messages'], [])
            self.assertNotIn(CHINESE, json.dumps(body, ensure_ascii=False))
            assistant = [row for row in host.short_rows if row['role'] == 'assistant']
            self.assertEqual(assistant, [])
        finally:
            host.doCleanups()

    def test_malformed_json_fails_without_retry(self):
        result, state, provider, traces = self.generate([BROKEN, VALID])
        self.assertIsNone(result)
        self.assertIsNone(state)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(traces[0]['retry_reason'], 'malformed_json')
        self.assertFalse(traces[0]['will_retry'])

    def test_chat_text_second_attempt_persists_nonempty_subtitle(self):
        from test_chat_commit_gate import ChatCommitGateTests, chat_reply
        host = ChatCommitGateTests()
        host.setUp()
        try:
            response, body = host.send(
                [JAPANESE, chat_reply('そうだね', '是啊')],
                source_event_id='salvage-e2e')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(host.route._create_json.call_count, 2)
            segment = body['messages'][0]
            self.assertTrue(segment['zh'].strip())
            self.assertTrue(str(segment['event_id']).endswith(':0'))
        finally:
            host.doCleanups()

        import delayed_reply
        written = []

        def commit(user_id, content, character_id='gojo', **kwargs):
            written.append({'content': content, 'event_id': kwargs.get('event_id'),
                            'subtitle': kwargs.get('subtitle')})
            return kwargs.get('event_id')

        class Helpers:
            def _turn_context(self, *args, **kwargs):
                pack = types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')
                return pack, []

            def _history_plus_current(self, messages, content):
                return list(messages or []) + [{'role': 'user', 'content': content}]

            def _generate_or_none(self, *args, **kwargs):
                return route_chat._generate_or_none(*args, **kwargs)

            def _finalize_committed(self, result, min_messages=1):
                return route_chat._finalize_committed(result, min_messages)

            def _commit_offline_state(self, *args, **kwargs):
                return None

        with ExitStack() as stack:
            stack.enter_context(patch.object(
                route_chat, '_create_json',
                side_effect=[
                    (JAPANESE, types.SimpleNamespace(stop_reason='end_turn', usage=None)),
                    (VALID, types.SimpleNamespace(stop_reason='end_turn', usage=None)),
                ]))
            for target, value in (
                    ('characters.get_character', {'name': 'offline', 'voice_id': 'v'}),
                    ('user_memory.save_user_short_memory_once', True),
                    ('user_memory.get_short_memory', []),
                    ('user_memory.update_chat_days', 1),
                    ('temporal_awareness.get_temporal_snapshot', {}),
                    ('temporal_awareness.record_turn', None),
                    ('prompt.build_system_blocks', []),
                    ('tts.tts_to_b64', ''),
                    ('delayed_reply.assistant_already_committed', False),
                    ('schedule_transition.validate_generated_schedule_reply', (None, {})),
                    ('schedule_transition.commit_generated_schedule_intent', {'ok': True}),
                    ('behavior_evidence.record_reply_cycle', None)):
                stack.enter_context(patch(target, return_value=value))
            stack.enter_context(patch(
                'user_memory.commit_visible_assistant_message', side_effect=commit))
            result = delayed_reply.generate_delayed_chat_reply({
                'id': 259, 'user_id': 'u', 'character_id': 'gojo',
                'pending_text': '你好', 'pending_count': 1,
                'last_source_event_id': 'u1', 'reply_state': 'free',
            }, helpers=Helpers(), push=False)
        self.assertTrue(result['ok'], result)
        self.assertEqual(written[0]['event_id'], 'delayed_reply:259:0')
        self.assertTrue(written[0]['subtitle'].strip())
        self.assertEqual(written[0]['subtitle'], '是啊')
