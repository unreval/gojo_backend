"""The rendered insert example must keep companionship available, without IO."""
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import re
import unittest
from unittest.mock import patch

import context_layer
import prompt


class InsertIntentPromptTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch(
            'psycopg2.connect', side_effect=AssertionError('live DB forbidden')))
        self.stack.enter_context(patch(
            'ai_client.get_anthropic', side_effect=AssertionError('model forbidden')))
        for target, value in (
                ('prompt.get_character', {'core_prompt': 'CORE-PERSONA'}),
                ('prompt.load_canon_lock', 'CANON-LOCK'),
                ('prompt.get_first_interaction_days', 20),
                ('prompt._accounts_block', ''),
                ('memory_authority.filter_recall_authority', {}),
                ('smart_recall.format_recall_for_prompt', ('', '', '')),
                ('db_schedule.format_world_prompt', ('', {})),
                ('context_layer.revalidate_pack_blocks', dict.fromkeys((
                    'cognitive_prompt_text', 'pinned_prompt_text',
                    'summary_prompt_text', 'episode_prompt_text'), ''))):
            self.stack.enter_context(patch(target, return_value=value))
        pack = context_layer.ChatContextPack(
            support_ready=True, recall_ready=True, recall_result={},
            temporal_snapshot=dict(
                now_utc=datetime(2026, 10, 17, 13, 57, tzinfo=timezone.utc),
                has_history=False))
        self.blocks = prompt.build_system_blocks(
            'u', 'gojo', '给我空出半小时陪我聊聊', context_pack=pack)
        text = '\n'.join(block['text'] for block in self.blocks)
        self.section = text.split('【日程事实与结构化行动意图】', 1)[1].split(
            '【提醒功能', 1)[0]
        decoder = json.JSONDecoder()
        self.examples = [decoder.raw_decode(self.section[match.start():])[0]
                         for match in re.finditer(r'\{"schedule_action_intent":', self.section)]

    def test_rendered_insert_example_is_free(self):
        inserts = [example for example in self.examples
                   if example['schedule_action_intent']['type'] == 'insert']
        self.assertEqual(len(inserts), 1)
        self.assertEqual(inserts[0]['schedule_action_intent']['event']['reply_state'], 'free')
        self.assertIn('"reply_state":"free"', self.section)
        self.assertNotIn('"reply_state":"soft_busy"', self.section)

    def test_rendered_rules_choose_reply_state_and_keep_companionship_free(self):
        for phrase in ('没法看手机→hard_busy', '能瞄一眼→soft_busy', '能正常回→free',
                       '为她空出来陪她的时段一律 free'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.section)

    def test_system_blocks_format_all_schedule_examples_as_valid_json(self):
        self.assertEqual(len(self.blocks), 3)
        self.assertIn('CORE-PERSONA', self.blocks[0]['text'])
        self.assertEqual({example['schedule_action_intent']['type']
                          for example in self.examples},
                         {'complete', 'extend', 'cancel', 'relocate', 'insert'})
        self.assertNotIn('{{', self.section)


if __name__ == '__main__':
    unittest.main()
