"""Relays that drop Anthropic `system` must receive it inside messages."""
import os
import sys
import unittest

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


if __name__ == '__main__':
    unittest.main()
