"""Context-expanded queries share one vector and preserve recall protections."""
from contextlib import ExitStack
from types import ModuleType
from unittest.mock import Mock, patch
import sys
import unittest

import context_layer
import episodic_index
import raw_events
import smart_recall
from test_context_budget import NOW, _event


class RecallQueryContextTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        context_layer.use_memory_store(True)
        self.addCleanup(context_layer.use_memory_store, False)
        for name in ('maybe_store_spill_summary', 'list_rolling_summaries', 'list_pins'):
            self.stack.enter_context(patch.object(context_layer, name, return_value=[]))
        self.stack.enter_context(patch.object(context_layer, '_support_items', return_value=([], '')))
        self.stack.enter_context(patch.object(raw_events, 'deleted_event_ids', return_value=set()))
        self.authority = self.stack.enter_context(patch(
            'memory_authority.filter_recall_authority', side_effect=lambda result, *args: result))
        self.rag = ModuleType('memory_search')
        self.rag.is_vector_ready = Mock(return_value=True)
        self.rag.embed = Mock(return_value=[3.0, 4.0])
        self.rag._to_vec = Mock(return_value=(3.0, 4.0))
        self.stack.enter_context(patch.dict(sys.modules, {'memory_search': self.rag}))
        self.embedding = self.stack.enter_context(patch.object(
            context_layer, '_build_recall_query_embedding',
            wraps=context_layer._build_recall_query_embedding))
        self.recall = self.stack.enter_context(patch.object(smart_recall, 'two_level_recall',
            return_value={'facts': [{'id': 321, 'content': '京都那家老餐馆',
                'category': '偏好', 'timestamp': NOW, 'bonds': [],
                'source_event_ids': ['old-source']}], 'loose_bonds': [], 'tolds': []}))
        self.episodes = self.stack.enter_context(patch.object(
            episodic_index, 'recall_episodes', return_value=[]))
        self.exclude = self.stack.enter_context(patch.object(
            context_layer, 'exclude_recall_covered_by_recent',
            wraps=context_layer.exclude_recall_covered_by_recent))
        self.summary_dedupe = self.stack.enter_context(patch.object(
            context_layer, '_drop_summaries_covered_by_episodes',
            wraps=context_layer._drop_summaries_covered_by_episodes))

    def pack(self, events, query='你刚才说的那个呢', **kwargs):
        return context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo', user_message=query,
            now=NOW, auto_pin_enabled=False, **kwargs)

    def assert_query(self, expected, vector=(3.0, 4.0)):
        self.assertEqual(self.embedding.call_args.args, (expected,))
        self.assertEqual(self.recall.call_args.args, ('u', 'gojo', expected))
        self.assertEqual(self.episodes.call_args.args, ('u', 'gojo', expected))
        self.assertEqual(self.recall.call_args.kwargs['query_embedding'], vector)
        self.assertIs(self.episodes.call_args.kwargs['query_embedding'],
                      self.recall.call_args.kwargs['query_embedding'])

    def test_embedding_memory_and_episode_receive_exact_same_last_two_query(self):
        events = [_event(0, text='更早的话题'), _event(1, text='京都餐馆'),
                  _event(2, text='上次那份菜单')]
        pack = self.pack(events)
        self.assert_query('你刚才说的那个呢 京都餐馆 上次那份菜单')
        self.rag.embed.assert_called_once_with('你刚才说的那个呢 京都餐馆 上次那份菜单')
        self.assertEqual(self.recall.call_args.kwargs['exclude_event_ids'], ['e0', 'e1', 'e2'])
        self.assertEqual(pack.recent_event_ids, ['e0', 'e1', 'e2'])
        self.assertTrue(pack.recall_ready)
        self.authority.assert_called_once()

    def test_empty_and_blank_tails_use_hot_item_fallback_without_backfill(self):
        for events, query, expected in (
                ([], '当前问题', '当前问题'),
                ([_event(1, text='前一句')], '', '前一句'),
                ([dict(_event(1, text='原文'), prompt_text='提示正文')], '当前', '当前 提示正文'),
                ([dict(_event(1, text='原文'), prompt_text='')], '当前', '当前 原文'),
                ([_event(0, text='不得补位'),
                  dict(_event(1, text='不得回退'), prompt_text=' \t'),
                  _event(2, text=' \n')], '当前', '当前'),
                ([dict(_event(1), content=None)], '当前', '当前')):
            with self.subTest(query=query, expected=expected):
                self.pack(events, query=query)
                self.assert_query(expected)

    def test_episode_fully_covered_by_hot_window_is_still_excluded(self):
        self.episodes.return_value = [{'episode_id': 'hot-episode', 'summary': '热窗口餐馆',
                                      'source_event_ids': ['e1', 'e2']}]
        pack = self.pack([_event(1, text='京都餐馆'), _event(2, text='菜单')])
        self.assert_query('你刚才说的那个呢 京都餐馆 菜单')
        self.assertEqual(pack.recall_result['episodes'], [])
        self.assertEqual([row['id'] for row in pack.recall_result['facts']], [321])
        self.assertEqual(self.exclude.call_args.args[1], ['e1', 'e2'])
        self.summary_dedupe.assert_called_once_with([], [])

    def test_episode_failure_keeps_two_level_results(self):
        self.episodes.side_effect = RuntimeError('episode index unavailable')
        pack = self.pack([_event(1, text='餐馆')])
        self.assert_query('你刚才说的那个呢 餐馆')
        self.assertTrue(pack.recall_ready)
        self.assertEqual(pack.recall_result['episodes'], [])
        self.assertEqual([row['id'] for row in pack.recall_result['facts']], [321])
        self.recall.assert_called_once()

    def test_embedding_failure_keeps_expanded_lexical_query_and_shared_none(self):
        self.rag.embed.side_effect = RuntimeError('embedding unavailable')
        pack = self.pack([_event(1, text='京都餐馆')])
        self.assert_query('你刚才说的那个呢 京都餐馆', vector=None)
        self.assertTrue(pack.recall_ready)
        self.assertEqual([row['id'] for row in pack.recall_result['facts']], [321])
        self.rag._to_vec.assert_not_called()

    def test_recall_disabled_does_not_call_any_retriever_or_embedding(self):
        pack = self.pack([_event(1, text='京都餐馆')], include_recall=False)
        for call in (self.embedding, self.rag.embed, self.recall, self.episodes):
            call.assert_not_called()
        self.assertFalse(pack.recall_ready)

    def test_all_shared_profiles_expand_after_current_turn_exclusion(self):
        events = [_event(0, text='旧主题'), _event(1, text='京都餐馆'),
                  _event(2, text='上次菜单'),
                  dict(_event(4, text='当前问题'), event_id='current')]
        for profile in ('text', 'story', 'proactive', 'voice', 'image', 'group_chat', 'diary_writer'):
            with self.subTest(profile=profile), patch.object(
                    raw_events, 'get_hot_candidate_events', return_value=events):
                pack = context_layer.build_chat_context(
                    'u', 'gojo', user_message='当前问题', current_event_id='current',
                    profile=profile, now=NOW, include_recall=True, auto_pin_enabled=False)
                self.assert_query('当前问题 京都餐馆 上次菜单')
                self.assertNotIn('current', pack.recent_event_ids)
                self.assertEqual(self.recall.call_args.kwargs['exclude_event_ids'], pack.recent_event_ids)
