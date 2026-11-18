"""Rolling-summary prompt rules stay on the shared formatter."""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import context_layer
import prompt
from context_budget import ContextItem
from memory_authority import AUTHORITY

ROOT = os.path.dirname(os.path.dirname(__file__))
TITLE = '【稍早连续讨论的滚动摘要——不能替代原文 Raw Event】'
SUMMARY_RULES = (
    '使用规则：',
    '1. 摘要里「我」说的部分，只说明你当时那样说过，不等于那件事被核实过。',
    '2. 可以和自己上次说过的保持一致，但不要用「我说过」来证明一件事是真的。',
    '3. 「她」说的部分有原话可查，按记得处理。',
    '4. 摘要不记录精确时间，不要据此断定是哪一天。',
)
EPISODE_RULES = (
    '使用规则：',
    '1. 这是从可追溯 Raw Event 组织出的历史经历，不是新的事实、关系结论或情感判断。',
    '2. 优先级始终是：她当前的消息/当前直接事件 > 当前重要事项、active 关系状态、较新的明确事实 > 这段历史经历。',
    '3. 只能自然记得这里已经描述的经过；不得补写未记录的心理、动机、关系含义或细节。',
    '4. 当前消息已说明旧事完成、取消、变化或相反时，以当前证据为准，不要坚持旧摘要。',
)


def _item(text):
    return ContextItem('sum-1', 'rolling_summary', text, source_event_ids=('event-1',))


class SummaryBlockRuleTests(unittest.TestCase):
    def test_rules_follow_the_items_and_the_title_stays_verbatim(self):
        rendered = context_layer._format_summary_block([
            _item('我说香草系的甜味。'),
            ContextItem('sum-2', 'rolling_summary', '她问过这个。'),
        ])
        self.assertTrue(rendered.startswith('\n' + TITLE + '\n'))
        self.assertLess(rendered.index('- 我说香草系的甜味。'), rendered.index('- 她问过这个。'))
        self.assertLess(rendered.index('- 她问过这个。'), rendered.index('使用规则：'))
        for line in SUMMARY_RULES:
            self.assertIn(line, rendered)
        self.assertEqual(rendered.count('使用规则：'), 1)

    def test_empty_summary_emits_nothing(self):
        self.assertEqual(context_layer._format_summary_block([]), '')
        self.assertEqual(context_layer._format_summary_block(()), '')

    def test_episode_rules_stay_verbatim(self):
        stamp = datetime(2026, 11, 11, 1, 0, tzinfo=timezone.utc)
        item = ContextItem(
            'episode:e1', 'episodic_memory', '一起讨论东京旅行',
            metadata={'raw': {'range_end': stamp}},
        )
        rendered = context_layer._format_episode_block([item])
        self.assertIn('【你们以前一起经历过的事】', rendered)
        self.assertIn('- [2026-11-11] 一起讨论东京旅行', rendered)
        for line in EPISODE_RULES:
            self.assertIn(line, rendered)
        source = open(
            os.path.join(ROOT, 'gojo_backend', 'context_layer.py'), encoding='utf-8').read()
        episode = source.split('def _format_episode_block', 1)[1].split(
            '\ndef _source_coverage', 1)[0]
        for line in EPISODE_RULES:
            self.assertIn(line, episode)
        summary = source.split('def _format_summary_block', 1)[1].split(
            '\ndef _format_episode_block', 1)[0]
        self.assertIn(TITLE, summary)
        self.assertNotIn('不得补写未记录的心理', summary)


class SummaryRulesCacheTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc)
        self.patches = [
            patch.object(prompt, 'get_character', return_value={'core_prompt': 'CORE-PERSONA'}),
            patch.object(prompt, 'load_canon_lock', return_value='CANON-LOCK'),
            patch.object(prompt, 'get_first_interaction_days', return_value=20),
            patch.object(prompt, '_accounts_block', return_value=''),
            patch.object(prompt, 'retrieve_character_memory', return_value=[]),
            patch.object(prompt, 'get_recent_openings', return_value=[]),
            patch.object(prompt, 'get_last_assistant_reply', return_value=''),
            patch.object(prompt, 'get_period_context', return_value=''),
            patch('memory_authority.filter_recall_authority', side_effect=lambda result, *_: result),
            patch('db_schedule.format_world_prompt', return_value=('', {})),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def _pack(self, summary_text, message='十一号发生了什么'):
        summary = context_layer._format_summary_block([_item(summary_text)])
        blocks = dict(
            cognitive_prompt_text='',
            pinned_prompt_text='',
            summary_prompt_text=summary,
            episode_prompt_text='',
        )
        pack = context_layer.ChatContextPack(
            support_ready=True,
            recall_ready=True,
            recall_result=dict(facts=[dict(
                content='她说「你就是臭手」', authority=AUTHORITY, category='其他',
                timestamp=self.now, bonds=[])], loose_bonds=[], tolds=[]),
            temporal_snapshot=dict(now_utc=self.now, has_history=False),
            summary_prompt_text=summary,
        )
        return pack, blocks, message

    def test_rules_land_in_the_uncached_tail(self):
        first_pack, first_blocks, first_message = self._pack('我说香草系的甜味。')
        second_summary = context_layer._format_summary_block([_item('她后来又问了一遍。')])
        second_blocks = dict(first_blocks, summary_prompt_text=second_summary)
        second_pack = context_layer.ChatContextPack(
            support_ready=True, recall_ready=True,
            recall_result=first_pack.recall_result,
            temporal_snapshot=dict(
                now_utc=self.now + timedelta(days=1), has_history=False),
            summary_prompt_text=second_summary)
        with patch('context_layer.revalidate_pack_blocks', side_effect=[first_blocks, second_blocks]):
            first = prompt.build_system_blocks('u', 'gojo', first_message, context_pack=first_pack)
            second = prompt.build_system_blocks(
                'u', 'gojo', '周末的书', extra_suffix='PRIVATE-SCENE', context_pack=second_pack)
        self.assertEqual(first[0]['text'].encode(), second[0]['text'].encode())
        self.assertEqual(first[1]['text'].encode(), second[1]['text'].encode())
        self.assertNotEqual(first[2]['text'], second[2]['text'])
        for blocks, summary in (
                (first, '我说香草系的甜味。'),
                (second, '她后来又问了一遍。')):
            self.assertEqual(
                [block.get('cache_control') for block in blocks],
                [{'type': 'ephemeral'}, {'type': 'ephemeral'}, None])
            self.assertNotIn('cache_control', blocks[2])
            for line in SUMMARY_RULES:
                self.assertIn(line, blocks[2]['text'])
                self.assertNotIn(line, blocks[0]['text'] + blocks[1]['text'])
            self.assertIn(TITLE, blocks[2]['text'])
            self.assertIn(summary, blocks[2]['text'])
            self.assertIn('【当前消息优先】', blocks[2]['text'])
            self.assertNotIn('【当前消息优先】', blocks[0]['text'] + blocks[1]['text'])
        self.assertIn('PRIVATE-SCENE', second[2]['text'])
        self.assertNotIn('PRIVATE-SCENE', second[0]['text'] + second[1]['text'])


if __name__ == '__main__':
    unittest.main()
