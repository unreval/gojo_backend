"""Own past utterances are not in recall, so the fixed rules must say so.

The section belongs in the cached fixed-rules block that every character
receives on every turn. It must not enter the dynamic tail, and it must not
rewrite the sentences already pinned by the prompt tests.
"""
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import prompt
from memory_authority import AUTHORITY


SECTION = (
    '【关于你自己说过的话】\n'
    '你手上没有自己过去发言的记录。你能查到的只有她说过什么。\n'
    '所以当她提起你说过某句话、而你没有印象时：\n'
    '- 不要断定「我没说过」。你没有证据说没说过，就像没有证据说说过一样。\n'
    '- 可以说不记得了、可能说过、让她说说当时的情形。\n'
    '- 可以表示怀疑，但不能把怀疑说成事实。\n'
    '- 她坚持的时候，别反复否认同一件事。\n'
    '这只适用于你自己的发言。关于她说过什么，记忆里有记录的照常当作记得。'
)

REPLY_SCOPE = (
    '【★ 这一条回复的分寸——最后再确认一遍】\n'
    '1. 长度跟着【她这句话的分量】走，不要一律短促：\n'
    '   · 她随口一句、开玩笑、简单确认 → 短短接住就好（1 条气泡，10~25 字），这时候话多反而假。\n'
    '   · 她说了要紧的事，或情绪明显起伏（撒娇、赌气、示弱、告白、难过、认真发问）\n'
    '     → 【这正是该多说两句的时刻】：把你的反应说完整，1~3 条气泡、总共 30~80 字。\n'
    '       先接住她的情绪，再说你想说的。用一句话打发过去，会显得你不在意。\n'
    '   · 你自己聊到在意的人或喜欢的东西 → 自然地多说几句，别端着。\n'
    '2. 情绪浓的时候，你的反应也该有温度：可以调侃，但调侃之后要有下文，别只丢一句就没了。\n'
    '3. 严格按最上方规定的单行 JSON 输出，不要有任何多余文字。'
)

CURRENT_TURN = (
    '【当前消息优先】\n'
    '话题优先级：1) 她这次实际说了什么；2) 最近几轮的话题；'
    '3) 与当前话题明确相关的历史记忆；4) 其他背景，无明显关联不主动提起。\n'
    '历史对话中的“现在、刚才、今晚、今天、明天、昨天”都以那句话的发送时刻为参照，'
    '不能重新以本轮时刻解释。后端当前时间事实决定本轮的当前日期、时刻和经过时间；'
    '与旧对话中的当前时间说法冲突时，以后端事实为准。'
    '凌晨的生活日解释也必须相对原句发送时刻应用。\n'
    '旧话题可作为过去引用；长间隔、emoji 或角色自己的重复，都不能证明用户原来的临时状态仍成立。'
    '最新用户明确陈述的状态优先。\n'
    '第一句必须回应她这次说的话。\n'
    '延迟回复以积压原文为待回应内容；系统触发语只负责执行。'
)

CHARACTERS = {
    'gojo': '你是五条悟（Gojo Satoru）',
    'geto': '你是夏油杰（Geto Suguru）',
    'minato': '你是波风水门（Namikaze Minato）',
}


class OwnUtteranceUncertaintyPromptTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc)
        self.stack.enter_context(patch.object(
            prompt, 'get_character', side_effect=self._character))
        for name, value in (
                ('get_first_interaction_days', 20),
                ('_accounts_block', ''),
                ('_format_open_promises', '')):
            self.stack.enter_context(patch.object(prompt, name, return_value=value))
        self.stack.enter_context(patch(
            'memory_authority.filter_recall_authority',
            side_effect=lambda result, *args: result))
        self.stack.enter_context(patch(
            'context_layer.revalidate_pack_blocks', return_value=dict(
                cognitive_prompt_text='', pinned_prompt_text='',
                summary_prompt_text='', episode_prompt_text='')))
        self.stack.enter_context(patch(
            'db_schedule.format_world_prompt', return_value=('', {})))

    def _character(self, character_id):
        core = prompt.load_core(character_id)
        self.assertIsNotNone(core)
        return {'core_prompt': core['core_prompt']}

    def _pack(self, recall_text, now=None):
        from context_layer import ChatContextPack
        return ChatContextPack(
            support_ready=True, recall_ready=True,
            recall_result=dict(facts=[dict(
                content=recall_text, authority=AUTHORITY, category='偏好',
                timestamp=now or self.now, bonds=[])], loose_bonds=[], tolds=[]),
            temporal_snapshot=dict(now_utc=now or self.now, has_history=False),
            relationship_prompt_text='\nRELATIONSHIP-RULES\n',
            temporal_text='', schedule_text='', lore_text='',
            anti_repeat_text='', accounts_text='')

    def _blocks(self, character_id='gojo', message='你说过烦', recall='她提过喜欢茶',
                extra_suffix='', now=None):
        return prompt.build_system_blocks(
            'u', character_id, message, extra_suffix=extra_suffix,
            context_pack=self._pack(recall, now))

    def test_section_sits_after_reply_scope_inside_cached_rules(self):
        blocks = self._blocks()
        self.assertEqual(len(blocks), 3)
        rules = blocks[1]['text']
        dynamic = blocks[2]['text']
        self.assertEqual(rules, REPLY_SCOPE + '\n\n' + SECTION)
        self.assertEqual(rules.count(SECTION), 1)
        self.assertNotIn(SECTION, blocks[0]['text'])
        self.assertNotIn('【关于你自己说过的话】', dynamic)
        self.assertNotIn(SECTION, dynamic)
        self.assertLess(rules.index('不要有任何多余文字。'), rules.index(SECTION))

    def test_gojo_geto_and_minato_receive_the_same_fixed_section(self):
        rules = []
        for character_id, identity in CHARACTERS.items():
            blocks = self._blocks(character_id, message=f'{character_id}说过烦')
            rules.append(blocks[1]['text'])
            with self.subTest(character_id=character_id):
                self.assertIn(identity, blocks[0]['text'])
                self.assertIn(SECTION, blocks[1]['text'])
                self.assertNotIn(SECTION, blocks[0]['text'])
                self.assertNotIn(SECTION, blocks[2]['text'])
        self.assertEqual(rules[0], rules[1])
        self.assertEqual(rules[1], rules[2])

    def test_pinned_prompt_sentences_stay_verbatim(self):
        blocks = self._blocks(extra_suffix='UNIQUE-EXTRA-SUFFIX-OWN-UTTERANCE')
        dynamic = blocks[2]['text']
        joined = '\n'.join(block['text'] for block in blocks)
        self.assertIn(REPLY_SCOPE, blocks[1]['text'])
        self.assertEqual(dynamic.count(CURRENT_TURN), 1)
        self.assertEqual(joined.count(CURRENT_TURN), 1)
        self.assertNotIn(CURRENT_TURN, blocks[0]['text'] + blocks[1]['text'])
        self.assertLess(
            dynamic.index('UNIQUE-EXTRA-SUFFIX-OWN-UTTERANCE'),
            dynamic.index('【当前消息优先】'))
        self.assertTrue(dynamic.rstrip().endswith('系统触发语只负责执行。'))
        source = open(os.path.join(BACKEND, 'prompt.py'), encoding='utf-8').read()
        self.assertEqual(source.count(CURRENT_TURN), 1)
        self.assertEqual(source.count(REPLY_SCOPE), 1)
        self.assertEqual(source.count(SECTION), 1)

    def test_cache_breakpoints_stay_on_the_first_two_blocks(self):
        first = self._blocks(message='你说过烦', recall='RECALL-TEA')
        second = self._blocks(
            message='周末的书', recall='RECALL-BOOK', extra_suffix='PRIVATE-SCENE',
            now=self.now + timedelta(days=1, hours=2))
        self.assertEqual(first[0]['text'].encode(), second[0]['text'].encode())
        self.assertEqual(first[1]['text'].encode(), second[1]['text'].encode())
        self.assertNotEqual(first[2]['text'], second[2]['text'])
        for blocks in (first, second):
            self.assertEqual(
                [block.get('cache_control') for block in blocks],
                [{'type': 'ephemeral'}, {'type': 'ephemeral'}, None])
            self.assertNotIn('cache_control', blocks[2])
            self.assertIn(SECTION, blocks[1]['text'])
            self.assertNotIn(SECTION, blocks[2]['text'])
            self.assertIn('【当前消息优先】', blocks[2]['text'])
            self.assertNotIn('【当前消息优先】', blocks[0]['text'] + blocks[1]['text'])
        self.assertIn('RECALL-TEA', first[2]['text'])
        self.assertIn('RECALL-BOOK', second[2]['text'])
        self.assertIn('PRIVATE-SCENE', second[2]['text'])
        self.assertNotIn('PRIVATE-SCENE', second[0]['text'] + second[1]['text'])


if __name__ == '__main__':
    unittest.main()
