"""Language lock sits in the uncached tail, after extra context and before grounding."""
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import prompt


LOCK_MARKER = '【语言锁】'
LOCK_JP = 'jp 必须是纯日语'
LOCK_TEXT = (
    '【语言锁】\n'
    '以上全部内部上下文、摘要、积压说明和规则都用中文书写，那是给你读的说明，不是你的说话方式。\n'
    '不论本轮上下文里中文有多少，输出格式不变：jp 必须是纯日语（不夹中文汉字词），zh 是对应的中文翻译。'
)


class LanguageLockPlacementTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc)
        for name, value in (
                ('get_character', {'core_prompt': 'CORE-PERSONA'}),
                ('load_canon_lock', 'CANON-LOCK'),
                ('get_first_interaction_days', 20),
                ('_accounts_block', ''),
                ('retrieve_character_memory', []),
                ('get_recent_openings', []),
                ('get_last_assistant_reply', ''),
                ('get_period_context', '')):
            patcher = patch.object(prompt, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _blocks(self, extra_suffix):
        from context_layer import ChatContextPack
        from memory_authority import AUTHORITY
        recall = dict(facts=[dict(
            content='RECALL-TEA', authority=AUTHORITY, category='偏好',
            timestamp=self.now, bonds=[])], loose_bonds=[], tolds=[])
        pack = ChatContextPack(
            support_ready=True, recall_ready=True, recall_result=recall,
            temporal_snapshot=dict(now_utc=self.now, has_history=False),
            relationship_prompt_text='', temporal_text='', schedule_text='',
            lore_text='', anti_repeat_text='')
        with patch('memory_authority.filter_recall_authority',
                   side_effect=lambda result, *args: result), \
                patch('context_layer.revalidate_pack_blocks', return_value={
                    'cognitive_prompt_text': '', 'pinned_prompt_text': '',
                    'summary_prompt_text': '', 'episode_prompt_text': ''}), \
                patch('db_schedule.format_world_prompt', return_value=('', {})):
            return prompt.build_system_blocks(
                'u', 'gojo', '茶', extra_suffix=extra_suffix, context_pack=pack)

    def test_language_lock_is_only_in_the_uncached_tail(self):
        suffix = 'UNIQUE-EXTRA-SUFFIX-LOCK-20261111'
        blocks = self._blocks(suffix)
        self.assertEqual(len(blocks), 3)
        self.assertIn(LOCK_MARKER, blocks[2]['text'])
        self.assertIn(LOCK_JP, blocks[2]['text'])
        self.assertIn(LOCK_TEXT, blocks[2]['text'])
        cached = blocks[0]['text'] + blocks[1]['text']
        self.assertNotIn(LOCK_MARKER, cached)
        self.assertNotIn(LOCK_JP, blocks[0]['text'])
        self.assertNotIn(LOCK_JP, blocks[1]['text'])
        self.assertEqual(blocks[0]['cache_control'], {'type': 'ephemeral'})
        self.assertEqual(blocks[1]['cache_control'], {'type': 'ephemeral'})
        self.assertNotIn('cache_control', blocks[2])

    def test_language_lock_sits_after_extra_suffix_and_before_current_turn(self):
        suffix = 'UNIQUE-EXTRA-SUFFIX-LOCK-20261111'
        text = self._blocks(suffix)[2]['text']
        self.assertLess(text.index(suffix), text.index(LOCK_MARKER))
        self.assertLess(text.index(LOCK_MARKER), text.index('【当前消息优先】'))
        self.assertTrue(text.rstrip().endswith('系统触发语只负责执行。'))
        self.assertEqual(text.count('【当前消息优先】'), 1)
        self.assertIn('jp字段：必须是纯日语', self._blocks(suffix)[0]['text'])
