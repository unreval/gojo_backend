"""Cache stability across real prompt rendering, without provider or DB access."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import context_layer
import episodic_index
from memory_authority import AUTHORITY
import prompt
import raw_events


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


class CurrentTurnGroundingTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        context_layer.use_memory_store(True)
        episodic_index.use_memory_store(True)
        self.addCleanup(context_layer.use_memory_store, False)
        self.addCleanup(episodic_index.use_memory_store, False)
        self.now = datetime(2026, 10, 1, 5, 30, tzinfo=timezone.utc)
        self.sources = {}
        for name, value in (
                ('get_character', {'core_prompt': 'CORE-PERSONA'}),
                ('load_canon_lock', 'CANON-LOCK'), ('get_first_interaction_days', 20),
                ('_accounts_block', '')):
            self.stack.enter_context(patch.object(prompt, name, return_value=value))
        self.stack.enter_context(patch.object(context_layer, '_support_items', return_value=([], '')))
        self.stack.enter_context(patch.object(context_layer, '_build_recall_query_embedding', return_value=None))
        self.stack.enter_context(patch('smart_recall.two_level_recall', return_value={
            'facts': [], 'loose_bonds': [], 'tolds': []}))
        self.stack.enter_context(patch('cognitive_reader.build_cognitive_prompt_context', return_value=''))
        self.stack.enter_context(patch('db_schedule.format_world_prompt', return_value=('', {})))
        self.stack.enter_context(patch.object(raw_events, 'deleted_event_ids', return_value=set()))
        self.stack.enter_context(patch.object(raw_events, 'get_active_events_by_ids', side_effect=self.active_sources))
        # No superseded answer dependencies; the real gate still validates raw sources.
        cursor = Mock()
        cursor.fetchall.return_value = []
        self.stack.enter_context(patch('db.get_conn', return_value=Mock(cursor=Mock(return_value=cursor))))

    def event(self, event_id, text, role='user', minutes_ago=1):
        row = dict(event_id=event_id, role=role, content=text,
                   timestamp=self.now - timedelta(minutes=minutes_ago), metadata={})
        self.sources[event_id] = row
        return row

    def active_sources(self, user_id, character_id, source_ids, conn=None):
        self.assertEqual((user_id, character_id), ('u', 'gojo'))
        return [self.sources[sid] for sid in source_ids if sid in self.sources]

    def pack(self, events=(), query='你刚才为什么说喜欢蓝色？'):
        return context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo', user_message=query,
            now=self.now, include_support=True,
            temporal_snapshot=dict(now_utc=self.now, has_history=bool(events)))

    def assert_grounding(self, blocks):
        dynamic = blocks[-1]['text']
        self.assertNotIn('cache_control', blocks[-1])
        self.assertEqual(dynamic.count('【当前消息优先】'), 1)
        rules = dynamic[dynamic.index('【当前消息优先】'):]
        priorities = ('1) 她这次实际说了什么', '2) 最近几轮的话题',
                      '3) 与当前话题明确相关的历史记忆', '4) 其他背景')
        self.assertEqual([rules.index(text) for text in priorities],
                         sorted(rules.index(text) for text in priorities))
        self.assertIn('无明显关联不主动提起', rules)
        self.assertIn('第一句必须回应她这次说的话', rules)
        self.assertTrue(dynamic.rstrip().endswith('系统触发语只负责执行。'))
        self.assertNotIn('【当前消息优先】', blocks[0]['text'] + blocks[1]['text'])

    def test_support_grounding_always_present_without_recall_and_with_only_summary(self):
        first = None
        for summary in ('', '深夜聊天，她提醒你该睡觉了。'):
            with self.subTest(summary=bool(summary)):
                if summary:
                    self.event('old-summary-source', summary, minutes_ago=1440)
                    context_layer.save_rolling_summary(
                        'u', 'gojo', summary, ['old-summary-source'],
                        range_end=self.now - timedelta(days=1))
                pack = self.pack()
                self.assertTrue(pack.support_ready)
                self.assertFalse(pack.memory_text or pack.episode_prompt_text)
                blocks = prompt.build_system_blocks('u', 'gojo', '蓝色？', context_pack=pack)
                self.assertEqual(bool(pack.summary_prompt_text), bool(summary))
                if summary:
                    self.assertIn(summary, blocks[-1]['text'])
                if first is not None:
                    self.assertEqual(first[:2], blocks[:2])
                first = blocks
                self.assert_grounding(blocks)

    def test_hot_topic_and_current_turn_survive_unrelated_derived_history(self):
        old = self.event('old-source', '深夜聊天，你叫她赶紧睡觉。', minutes_ago=60)
        context_layer.save_rolling_summary('u', 'gojo', old['content'], ['old-source'])
        episodic_index.save_episode(
            'u', 'gojo', episode_id='old-night', title='深夜作息',
            what_happened=old['content'], source_event_ids=['old-source'],
            range_end=old['timestamp'])
        hot = [self.event('hot-user', '海报用蓝色怎么样？', minutes_ago=2),
               self.event('hot-assistant', '我喜欢蓝色那版。', 'assistant')]
        current = '蓝色？'
        pack = self.pack(hot, query=current)
        messages = context_layer.append_current_user_turn(pack.messages, current)
        self.assertEqual(messages[-1], {'role': 'user', 'content': current})
        self.assertIn('海报用蓝色怎么样？', '\n'.join(row['content'] for row in messages))
        self.assertEqual(sum(row['content'] == current for row in messages), 1)
        blocks = prompt.build_system_blocks('u', 'gojo', current, context_pack=pack)
        self.assertIn('深夜', blocks[-1]['text'])
        self.assertTrue(pack.episode_prompt_text)
        self.assert_grounding(blocks)
        # Cached derived context loses its source; grounding still survives revalidation.
        self.sources.pop('old-source')
        withdrawn = prompt.build_system_blocks('u', 'gojo', current, context_pack=pack)
        self.assertNotIn('深夜', withdrawn[-1]['text'])
        self.assertEqual(blocks[:2], withdrawn[:2])
        self.assert_grounding(withdrawn)

    def test_delayed_grounding_follows_single_pending_section_at_tail(self):
        pending = '刚才那版海报为什么选蓝色？'
        blocks = prompt.build_system_blocks(
            'u', 'gojo', pending, context_pack=self.pack(query=pending),
            extra_suffix='【积压原文】\n' + pending)
        text = '\n'.join(block['text'] for block in blocks)
        self.assertEqual(text.count('【积压原文】'), 1)
        self.assertEqual(text.count(pending), 1)
        self.assert_grounding(blocks)
        self.assertLess(text.index(pending), text.index('【当前消息优先】'))
        self.assertIn('延迟回复以积压原文为待回应内容', blocks[-1]['text'])


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
