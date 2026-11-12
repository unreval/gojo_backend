"""Relays that drop Anthropic `system` must receive it inside messages."""
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_client


class FoldHelperTests(unittest.TestCase):
    def setUp(self):
        self._flag = ai_client.FOLD_SYSTEM_INTO_MESSAGES
        ai_client.FOLD_SYSTEM_INTO_MESSAGES = True

    def tearDown(self):
        ai_client.FOLD_SYSTEM_INTO_MESSAGES = self._flag

    def test_folded_head_keeps_blocks_cache_control_and_tail(self):
        system = [
            {'type': 'text', 'text': 'HEAD', 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': 'RULES', 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': 'TAIL'},
            {'type': 'image', 'source': {'data': 'skip'}},
            {'type': 'text', 'text': '   '},
        ]
        messages = [
            {'role': 'user', 'content': 'old'},
            {'role': 'assistant', 'content': 'prev'},
            {'role': 'user', 'content': 'now'},
        ]
        folded_system, folded = ai_client.fold_system_into_messages(system, messages)
        self.assertIsNone(folded_system)
        self.assertEqual(folded[0]['role'], 'user')
        self.assertEqual(folded[0]['content'], [
            {'type': 'text', 'text': 'HEAD', 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': 'RULES', 'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': 'TAIL'},
        ])
        self.assertEqual(folded[0]['content'][0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(folded[0]['content'][1]['cache_control'], {'type': 'ephemeral'})
        self.assertNotIn('cache_control', folded[0]['content'][2])
        self.assertEqual(folded[1], {'role': 'assistant', 'content': '了解。'})
        self.assertEqual(folded[2:], messages)
        self.assertEqual(messages[0]['role'], 'user')

    def test_assistant_first_history_stays_a_legal_list(self):
        messages = [{'role': 'assistant', 'content': '先开口'}]
        folded_system, folded = ai_client.fold_system_into_messages(
            [{'type': 'text', 'text': '人设'}], messages)
        self.assertIsNone(folded_system)
        self.assertEqual([row['role'] for row in folded], ['user', 'assistant', 'assistant'])
        self.assertEqual(folded[2], {'role': 'assistant', 'content': '先开口'})

    def test_string_system_becomes_one_leading_block(self):
        folded_system, folded = ai_client.fold_system_into_messages(
            '暗号是 BANANA7', [{'role': 'user', 'content': '暗号是什么'}])
        self.assertIsNone(folded_system)
        self.assertEqual(folded[0]['content'], [{'type': 'text', 'text': '暗号是 BANANA7'}])
        self.assertEqual(folded[2]['content'], '暗号是什么')

    def test_switch_off_returns_the_same_objects(self):
        ai_client.FOLD_SYSTEM_INTO_MESSAGES = False
        system = [{'type': 'text', 'text': 'SYS', 'cache_control': {'type': 'ephemeral'}}]
        messages = [{'role': 'user', 'content': 'hi'}]
        out_system, out_messages = ai_client.fold_system_into_messages(system, messages)
        self.assertIs(out_system, system)
        self.assertIs(out_messages, messages)

    def test_empty_system_does_not_insert_a_turn(self):
        messages = [{'role': 'assistant', 'content': '已有'}]
        cases = (
            None,
            '',
            '   ',
            [],
            [{'type': 'text', 'text': ''}],
            [{'type': 'text', 'text': '  \n'}],
            [{'type': 'image', 'source': {'data': 'x'}}],
        )
        for system in cases:
            with self.subTest(system=system):
                out_system, out_messages = ai_client.fold_system_into_messages(
                    system, messages)
                self.assertEqual(out_system, system)
                self.assertIs(out_messages, messages)


def _anthropic_response(text):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type='text', text=text)],
        stop_reason='end_turn',
        usage=None,
        id='fold-response',
    )


def _message_texts(messages):
    texts = []
    for message in messages:
        content = message.get('content')
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            texts.extend(
                block.get('text') or ''
                for block in content
                if isinstance(block, dict))
    return texts


class AnthropicCreateChatFoldTests(unittest.TestCase):
    def setUp(self):
        self._flag = ai_client.FOLD_SYSTEM_INTO_MESSAGES
        ai_client.FOLD_SYSTEM_INTO_MESSAGES = True

    def tearDown(self):
        ai_client.FOLD_SYSTEM_INTO_MESSAGES = self._flag

    def test_create_chat_folds_system_once(self):
        client = Mock()
        client.messages.create.return_value = _anthropic_response('ok')
        system = [{'type': 'text', 'text': 'SYS-ONCE',
                   'cache_control': {'type': 'ephemeral'}}]
        messages = [{'role': 'user', 'content': 'hi'}]
        with patch.object(ai_client, '_get_anthropic', return_value=client):
            ai_client.create_chat('claude-test', messages, system=system)
        kwargs = client.messages.create.call_args.kwargs
        self.assertNotIn('system', kwargs)
        self.assertEqual(_message_texts(kwargs['messages']).count('SYS-ONCE'), 1)
        self.assertEqual(kwargs['messages'][0]['role'], 'user')
        self.assertEqual(kwargs['messages'][0]['content'][0]['cache_control'],
                         {'type': 'ephemeral'})
        self.assertEqual(kwargs['messages'][1]['content'], '了解。')
        self.assertEqual(kwargs['messages'][2:], messages)

    def test_switch_off_passes_system_and_messages_unchanged(self):
        ai_client.FOLD_SYSTEM_INTO_MESSAGES = False
        client = Mock()
        client.messages.create.return_value = _anthropic_response('ok')
        system = [{'type': 'text', 'text': 'SYS'}]
        messages = [{'role': 'user', 'content': 'hi'}]
        with patch.object(ai_client, '_get_anthropic', return_value=client):
            ai_client.create_chat('claude-test', messages, system=system)
        kwargs = client.messages.create.call_args.kwargs
        self.assertEqual(kwargs['system'], system)
        self.assertEqual(kwargs['messages'], messages)

    def test_deepseek_still_uses_a_system_role_message(self):
        response = Mock(status_code=200)
        response.json.return_value = {
            'id': 'ds-1',
            'choices': [{'message': {'content': 'ds'}, 'finish_reason': 'stop'}],
            'usage': {},
        }
        messages = [{'role': 'user', 'content': 'hi'}]
        with patch.object(ai_client, 'DEEPSEEK_KEY', 'test-key'), \
                patch.object(ai_client.requests, 'post', return_value=response) as post:
            text, usage = ai_client.create_chat(
                'deepseek-chat', messages, system='SYS-DS')
        self.assertEqual(text, 'ds')
        self.assertEqual(usage['provider'], 'deepseek')
        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['messages'], [
            {'role': 'system', 'content': 'SYS-DS'},
            {'role': 'user', 'content': 'hi'},
        ])
        self.assertNotIn('了解', str(payload))


if __name__ == '__main__':
    unittest.main()
