"""Cache stability across real prompt rendering, without provider or DB access."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import context_layer
from memory_authority import AUTHORITY
import prompt


class PromptCacheStabilityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc)
        for name, value in (
                ('get_character', {'core_prompt': 'CORE-PERSONA'}),
                ('load_canon_lock', 'CANON-LOCK'), ('get_first_interaction_days', 20),
                ('_accounts_block', ''), ('retrieve_character_memory', []),
                ('get_recent_openings', []), ('get_last_assistant_reply', ''),
                ('get_period_context', '')):
            self.stack.enter_context(patch.object(prompt, name, return_value=value))
        self.long = self.stack.enter_context(patch.object(prompt, 'get_long_memory'))
        self.bonds = self.stack.enter_context(patch.object(prompt, 'get_bond_memories'))
        self.authority = self.stack.enter_context(patch(
            'memory_authority.filter_recall_authority', side_effect=lambda result, *args: result))
        self.current = dict(cognitive_prompt_text='\nCRITICAL-JUDGMENT:accept; OPEN-QUESTION:unresolved\n',
                            pinned_prompt_text='\nPINNED-ITEM\n', summary_prompt_text='\nSUMMARY-ITEM\n',
                            episode_prompt_text='\nEPISODE-ITEM\n')
        self.revalidate = self.stack.enter_context(patch.object(
            context_layer, 'revalidate_pack_blocks', return_value=self.current))
        self.schedule = self.stack.enter_context(patch(
            'db_schedule.format_world_prompt', return_value=('\nSCHEDULE-READING\n', {})))

    def recall(self, text):
        return dict(facts=[dict(content=text, authority=AUTHORITY,
                               category='偏好', timestamp=self.now, bonds=[])],
                    loose_bonds=[], tolds=[])

    def pack(self, text='RECALL-TEA', now=None):
        return context_layer.ChatContextPack(
            support_ready=True, recall_ready=True, recall_result=self.recall(text),
            temporal_snapshot=dict(now_utc=now or self.now, has_history=False),
            relationship_prompt_text='\nCRITICAL-RELATIONSHIP:friendship\n',
            temporal_text='STALE-TIME', schedule_text='STALE-SCHEDULE',
            lore_text='\nLORE-ITEM\n', anti_repeat_text='\nANTI-REPEAT\n')

    def test_cache_prefix_is_byte_identical_across_query_clock_schedule_and_recall(self):
        first = prompt.build_system_blocks('u', 'gojo', '茶', context_pack=self.pack())
        self.schedule.return_value = ('\nSCHEDULE-WALKING\n', {})
        second = prompt.build_system_blocks(
            'u', 'gojo', '周末的书', extra_suffix='PRIVATE-SCENE',
            context_pack=self.pack('RECALL-BOOK', self.now + timedelta(days=1, hours=2)))
        for index in (0, 1):
            self.assertTrue(first[index]['cache_control'])
            self.assertEqual(first[index]['text'].encode(), second[index]['text'].encode())
        self.assertNotIn('cache_control', first[2])
        self.assertNotEqual(first[2]['text'], second[2]['text'])
        for blocks, recall, schedule, time in (
                (first, 'RECALL-TEA', 'SCHEDULE-READING', '13:30'),
                (second, 'RECALL-BOOK', 'SCHEDULE-WALKING', '15:30')):
            static = '\n'.join(block['text'] for block in blocks[:2])
            dynamic = blocks[2]['text']
            self.assertIn('CORE-PERSONA', static)
            self.assertIn('CANON-LOCK', static)
            for text in (recall, schedule, time, 'CRITICAL-JUDGMENT', 'OPEN-QUESTION',
                         'CRITICAL-RELATIONSHIP', 'PINNED-ITEM', 'SUMMARY-ITEM', 'EPISODE-ITEM',
                         'LORE-ITEM', 'ANTI-REPEAT', 'TEMPORAL SNAPSHOT'):
                self.assertIn(text, dynamic)
                self.assertNotIn(text, static)
            self.assertNotIn('STALE-', dynamic)
            self.assertEqual('\n'.join(b['text'] for b in blocks).count('这一条回复的分寸'), 1)
        self.assertNotIn('RECALL-TEA', second[2]['text'])
        self.assertIn('PRIVATE-SCENE', second[2]['text'])
        self.assertNotIn('PRIVATE-SCENE', second[0]['text'] + second[1]['text'])
        self.assertEqual(self.authority.call_count, 2)
        self.assertEqual(self.revalidate.call_count, 2)
        self.long.assert_not_called()
        self.bonds.assert_not_called()

    def test_source_revalidation_still_removes_withdrawn_recall_and_critical_items(self):
        pack = self.pack('REVOKED-RECALL')
        pack.cognitive_prompt_text = 'REVOKED-COGNITION'
        pack.pinned_prompt_text = 'REVOKED-PIN'
        self.authority.side_effect = None
        self.authority.return_value = {}
        self.revalidate.return_value = dict.fromkeys(self.current, '')
        blocks = prompt.build_system_blocks('u', 'gojo', 'query', context_pack=pack)
        text = '\n'.join(b['text'] for b in blocks)
        self.assertNotIn('REVOKED-', text)
        self.authority.assert_called_once_with(pack.recall_result, 'u', 'gojo')
        self.revalidate.assert_called_once_with(pack, 'u', 'gojo', 'query')
        self.assertIn('认知与表达的职责边界', text)

    def test_no_pack_fallback_also_keeps_query_recall_out_of_cache(self):
        with patch('smart_recall.two_level_recall', side_effect=[self.recall('RECALL-A'),
                self.recall('RECALL-B')]), patch.object(prompt.memory_search, 'is_vector_ready',
                return_value=False), patch.object(prompt, 'build_relation_rules',
                return_value='RELATIONSHIP-RULES'), patch('diary_engine.build_diary_hint', return_value=''):
            first = prompt.build_system_blocks('u', 'gojo', 'A', temporal_snapshot=dict(
                now_utc=self.now, has_history=False))
            second = prompt.build_system_blocks('u', 'gojo', 'B', temporal_snapshot=dict(
                now_utc=self.now, has_history=False))
        self.assertEqual(first[:2], second[:2])
        self.assertIn('RECALL-A', first[2]['text'])
        self.assertIn('RECALL-B', second[2]['text'])
        self.assertNotIn('RECALL-A', second[2]['text'])
        self.long.assert_not_called()
        self.bonds.assert_not_called()


class CacheUsageRatioTests(unittest.TestCase):
    def test_provider_usage_ratios_and_legacy_savings_estimate(self):
        for read, created, plain, read_ratio, uncached_ratio, effective in (
                (9880, 0, 9688, '0.5049', '0.4951', 10676),
                (50, 25, 25, '0.5000', '0.2500', 55),
                (0, 0, 0, '0.0000', '0.0000', 0),
                (0, 0, 100, '0.0000', '1.0000', 100),
                (100, 0, 0, '1.0000', '0.0000', 10)):
            with self.subTest(read=read, created=created, plain=plain), patch('builtins.print') as logged:
                prompt.log_cache_usage('test', SimpleNamespace(usage=SimpleNamespace(
                    cache_read_input_tokens=read, cache_creation_input_tokens=created,
                    input_tokens=plain)))
            logged.assert_called_once()
            line = logged.call_args.args[0]
            for expected in (f'cache_read_ratio={read_ratio}', f'uncached_ratio={uncached_ratio}',
                             f'effective_input_estimate={effective}', 'savings_estimate=0.9*cache_read'):
                self.assertIn(expected, line)
            if read or created:
                for expected in (f'命中={read}', f'新建={created}', f'未缓存={plain}',
                                 f'总输入={read + created + plain}', f'约省={int(read * .9)} tokens'):
                    self.assertIn(expected, line)
            else:
                self.assertIn('未命中缓存', line)
