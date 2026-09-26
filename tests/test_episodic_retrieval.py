import os
import sys
import types
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import context_budget  # noqa: E402
import context_layer  # noqa: E402
import episodic_index  # noqa: E402
import prompt  # noqa: E402
import raw_events  # noqa: E402
import recall_candidates  # noqa: E402
import smart_recall  # noqa: E402


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _event(event_id, *, minutes_ago=0):
    return {
        'event_id': event_id,
        'role': 'user',
        'content': 'canonical raw event',
        'timestamp': NOW - timedelta(minutes=minutes_ago),
    }


class EpisodicRetrievalTests(unittest.TestCase):
    def setUp(self):
        episodic_index.use_memory_store(True)
        context_layer.use_memory_store(True)

    def tearDown(self):
        episodic_index.use_memory_store(False)
        context_layer.use_memory_store(False)

    def _save(self, episode_id, *, text, source_ids=('source-1',),
               days_ago=0, status='active', rebuild_required=False,
               embedding_metadata=None, retrieval_text=None):
        end = NOW - timedelta(days=days_ago)
        if embedding_metadata is not None:
            embedding_metadata = dict(embedding_metadata)
            vector = embedding_metadata.get('vector')
            if vector is not None:
                embedding_fields = {
                    'title': text[:40],
                    'what_happened': text,
                    'outcome': '当时已记录的结果',
                    'unresolved': '',
                }
                embedding_metadata.setdefault(
                    'retrieval_text_hash',
                    episodic_index._retrieval_text_hash(embedding_fields),
                )
                embedding_metadata.setdefault('model', 'test-embedding')
                embedding_metadata.setdefault(
                    'processor_version', episodic_index.EPISODE_PROCESSOR_VERSION)
                embedding_metadata.setdefault('generated_at', NOW.isoformat())
                embedding_metadata.setdefault('dimensions', len(vector))
        return episodic_index.save_episode(
            'u', 'gojo',
            episode_id=episode_id,
            title=text[:40],
            what_happened=text,
            outcome='当时已记录的结果',
            unresolved='',
            source_event_ids=source_ids,
            range_start=end - timedelta(minutes=5),
            range_end=end,
            status=status,
            rebuild_required=rebuild_required,
            embedding_metadata=embedding_metadata,
            retrieval_text=retrieval_text,
        )

    def _active_sources(self, active_ids):
        active = {str(item) for item in active_ids}

        def lookup(_user_id, _character_id, ids):
            return [_event(str(item)) for item in ids if str(item) in active]

        stack = ExitStack()
        stack.enter_context(
            patch.object(raw_events, 'deleted_event_ids', return_value=set()))
        stack.enter_context(
            patch.object(raw_events, 'get_active_events_by_ids', side_effect=lookup))
        return stack

    def _recall(self, query, active_ids, **kwargs):
        with self._active_sources(active_ids):
            return episodic_index.recall_episodes('u', 'gojo', query, **kwargs)

    def test_only_active_complete_episodes_are_recalled(self):
        self._save('active', text='东京旅行的行程', source_ids=('a',))
        self._save('stale', text='东京旅行的旧行程', source_ids=('b',), status='stale')
        self._save('invalid', text='东京旅行的删除经历', source_ids=('c',), status='invalidated')
        self._save('superseded', text='东京旅行的旧版本', source_ids=('d',), status='superseded')
        self._save('rebuild', text='东京旅行的待重建版本', source_ids=('e',), rebuild_required=True)

        rows = self._recall('东京旅行', {'a', 'b', 'c', 'd', 'e'})

        self.assertEqual([row['episode_id'] for row in rows], ['active'])

    def test_non_recallable_statuses_are_filtered_before_semantic_candidates(self):
        cases = (
            ('stale-vector', 'stale', False),
            ('invalidated-vector', 'invalidated', False),
            ('superseded-vector', 'superseded', False),
            ('rebuild-vector', 'active', True),
        )
        active_ids = set()
        for episode_id, status, rebuild_required in cases:
            source_id = f'{episode_id}-source'
            active_ids.add(source_id)
            self._save(
                episode_id, text='与当前措辞没有词面交集的经历',
                source_ids=(source_id,), status=status,
                rebuild_required=rebuild_required,
                embedding_metadata={'vector': [1.0, 0.0]},
            )

        rows = self._recall(
            '完全不同的提问', active_ids, query_embedding=[1.0, 0.0])

        self.assertEqual(rows, [])

    def test_recalled_episode_keeps_exact_raw_event_provenance(self):
        self._save('traceable', text='一起讨论了周末看展', source_ids=('evt-a', 'evt-b'))

        rows = self._recall('周末看展', {'evt-a', 'evt-b'})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['source_event_ids'], ('evt-a', 'evt-b'))
        self.assertEqual(rows[0]['provenance_quality'], 'linked')
        self.assertNotIn('forged-event', rows[0]['source_event_ids'])

    def test_missing_one_source_fails_closed(self):
        self._save('partial-source', text='一起讨论了周末看展', source_ids=('evt-a', 'evt-b'))

        rows = self._recall('周末看展', {'evt-a'})

        self.assertEqual(rows, [])

    def test_tombstoned_raw_event_is_never_recalled(self):
        self._save('tombstoned', text='一起讨论了周末看展', source_ids=('evt-a',))
        with patch.object(raw_events, 'deleted_event_ids', return_value={'evt-a'}), \
             patch.object(raw_events, 'get_active_events_by_ids') as canonical:
            rows = episodic_index.recall_episodes('u', 'gojo', '周末看展')

        self.assertEqual(rows, [])
        canonical.assert_not_called()

    def test_source_lookup_error_fails_closed(self):
        self._save('lookup-error', text='一起讨论了周末看展', source_ids=('evt-a',))
        with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
             patch.object(raw_events, 'get_active_events_by_ids',
                          side_effect=raw_events.SourceValidityError('offline')):
            rows = episodic_index.recall_episodes('u', 'gojo', '周末看展')

        self.assertEqual(rows, [])

    def test_deletion_between_validation_and_return_never_revives_episode(self):
        self._save('late-delete', text='一起讨论了周末看展', source_ids=('evt-a',))
        calls = []

        def lookup(_user_id, _character_id, ids):
            calls.append(tuple(ids))
            return [_event('evt-a')] if len(calls) == 1 else []

        with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
             patch.object(raw_events, 'get_active_events_by_ids', side_effect=lookup):
            rows = episodic_index.recall_episodes('u', 'gojo', '周末看展')

        self.assertEqual(len(calls), 2)
        self.assertEqual(rows, [])

    def test_old_lexically_relevant_episode_beats_new_irrelevant_episode(self):
        self._save('old-trip', text='东京旅行的行程和车票安排', source_ids=('old',), days_ago=180)
        self._save('new-lunch', text='今天午饭吃面条', source_ids=('new',), days_ago=1)

        rows = self._recall('东京旅行还记得吗', {'old', 'new'})

        self.assertEqual(rows[0]['episode_id'], 'old-trip')

    def test_recent_safety_lane_is_reserved_for_vague_follow_ups(self):
        self._save('recent-context', text='上周一起讨论了咖啡店安排', source_ids=('recent',))

        vague = self._recall('然后呢？', {'recent'})
        specific = self._recall('完全无关的具体问题', {'recent'})

        self.assertEqual([row['episode_id'] for row in vague], ['recent-context'])
        self.assertEqual(specific, [])
        self.assertEqual(vague[0]['candidate_sources'], ('recent',))

    def test_old_semantically_relevant_episode_beats_new_irrelevant_episode(self):
        self._save(
            'old-semantic', text='过去的一次出行经历', source_ids=('old',), days_ago=180,
            embedding_metadata={'vector': [1.0, 0.0]},
        )
        self._save(
            'new-semantic', text='最近的一次无关经历', source_ids=('new',), days_ago=1,
            embedding_metadata={'vector': [0.0, 1.0]},
        )

        rows = self._recall('换一种说法的出行问题', {'old', 'new'},
                            query_embedding=[1.0, 0.0])

        self.assertEqual(rows[0]['episode_id'], 'old-semantic')
        self.assertGreater(rows[0]['semantic_score'], 0.9)

    def test_old_lexical_episode_survives_more_than_two_hundred_newer_rows(self):
        old = self._save(
            'old-japan-trip', text='她决定寒假独自去东京旅行',
            source_ids=('old-japan-source',), days_ago=31,
        )
        new_ids = set()
        with patch('builtins.print'):
            for index in range(221):
                source_id = f'new-lexical-{index}'
                new_ids.add(source_id)
                self._save(
                    f'new-lexical-{index}',
                    text=f'今天处理无关的例行事项编号{index}',
                    source_ids=(source_id,), days_ago=1,
                )

        rows = self._recall(
            '之前日本旅行那个安排呢？', new_ids | {'old-japan-source'})

        self.assertEqual(rows[0]['episode_id'], old['episode_id'])
        self.assertIn('lexical', rows[0]['candidate_sources'])
        self.assertNotIn('recent', rows[0]['candidate_sources'])

    def test_old_semantic_episode_survives_more_than_two_hundred_newer_rows(self):
        old = self._save(
            'old-semantic-trip', text='冬日的那段远行已经定下',
            source_ids=('old-semantic-source',), days_ago=31,
            embedding_metadata={'vector': [1.0, 0.0]},
        )
        new_ids = set()
        with patch('builtins.print'):
            for index in range(221):
                source_id = f'new-semantic-{index}'
                new_ids.add(source_id)
                self._save(
                    f'new-semantic-{index}',
                    text=f'普通例行事务记录{index}',
                    source_ids=(source_id,), days_ago=1,
                    embedding_metadata={'vector': [0.0, 1.0]},
                )

        rows = self._recall(
            '之前的日程应该怎么安排？', new_ids | {'old-semantic-source'},
                            query_embedding=[1.0, 0.0])

        self.assertEqual(rows[0]['episode_id'], old['episode_id'])
        self.assertIn('semantic', rows[0]['candidate_sources'])
        self.assertEqual(rows[0]['lexical_score'], 0.0)

    def test_vector_error_degrades_to_lexical_recall(self):
        self._save('lexical-fallback', text='东京旅行的行程', source_ids=('evt-a',))
        with self._active_sources({'evt-a'}), \
             patch.object(episodic_index, '_episode_vector',
                          side_effect=RuntimeError('embedding unavailable')):
            rows = episodic_index.recall_episodes(
                'u', 'gojo', '东京旅行', query_embedding=[1.0, 0.0])

        self.assertEqual([row['episode_id'] for row in rows], ['lexical-fallback'])
        self.assertEqual(rows[0]['semantic_score'], 0.0)

    def test_retrieval_text_ignores_untrusted_cached_text(self):
        self._save(
            'documented-fields', text='一起确定了周末看展', source_ids=('evt-a',),
            retrieval_text='FORGED-CACHE-TEXT-DO-NOT-RECALL',
        )

        forged = self._recall('FORGED-CACHE-TEXT-DO-NOT-RECALL', {'evt-a'})
        documented = self._recall('周末看展', {'evt-a'})

        self.assertEqual(forged, [])
        self.assertEqual([row['episode_id'] for row in documented], ['documented-fields'])

    def test_candidate_union_is_capped_without_expanding_the_recent_lane(self):
        with patch.object(episodic_index, 'list_episodes', return_value=[]) as listed:
            episodic_index.recall_episodes(
                'u', 'gojo', '任何问题', candidate_limit=9999)

        self.assertEqual(
            listed.call_args.kwargs['limit'],
            episodic_index.EPISODE_RECENT_POOL_LIMIT,
        )
        self.assertEqual(episodic_index.EPISODE_RECALL_CANDIDATE_LIMIT, 220)

    def test_hot_window_fully_covers_episode_and_excludes_it(self):
        result = context_layer.exclude_recall_covered_by_recent(
            {'episodes': [{'id': 'episode-hot', 'source_event_ids': ('a', 'b')}]},
            ['a', 'b'],
        )

        self.assertEqual(result['episodes'], [])

    def test_hot_window_partial_overlap_keeps_episode(self):
        result = context_layer.exclude_recall_covered_by_recent(
            {'episodes': [{'id': 'episode-partial', 'source_event_ids': ('a', 'b')}]},
            ['a'],
        )

        self.assertEqual(len(result['episodes']), 1)

    def test_partial_provenance_overlap_preserves_episode_and_fact(self):
        raw = {
            'episodes': [{
                'id': 'episode-partial', 'content': '东京旅行安排',
                'source_event_ids': ('a', 'b'), 'score': 0.8,
            }],
            'facts': [{
                'id': 'fact-partial', 'content': '东京旅行安排',
                'source_event_ids': ('a', 'c'), 'score': 0.9, 'bonds': [],
            }],
        }

        collapsed = recall_candidates.collapse_candidates(
            recall_candidates.from_recall_result(raw))
        result = recall_candidates.to_recall_result(collapsed, raw)

        self.assertEqual(len(result['episodes']), 1)
        self.assertEqual(len(result['facts']), 1)

    def test_nearly_duplicate_fact_can_replace_episode_but_not_other_facts(self):
        raw = {
            'episodes': [{
                'id': 'episode-duplicate', 'content': '东京旅行安排',
                'source_event_ids': ('a', 'b'), 'score': 0.8,
            }],
            'facts': [{
                'id': 'fact-duplicate', 'content': '东京旅行安排',
                'source_event_ids': ('a',), 'score': 0.9, 'bonds': [],
            }],
        }

        collapsed = recall_candidates.collapse_candidates(
            recall_candidates.from_recall_result(raw))
        result = recall_candidates.to_recall_result(collapsed, raw)

        self.assertEqual(result['episodes'], [])
        self.assertEqual([item['id'] for item in result['facts']], ['fact-duplicate'])

    def test_high_source_coverage_episode_replaces_rolling_summary_in_prompt_only(self):
        summaries = [{'summary_id': 'summary-1', 'source_event_ids': ('a', 'b', 'c')}]
        episodes = [{'episode_id': 'episode-1', 'source_event_ids': ('a', 'b', 'c')}]

        kept = context_layer._drop_summaries_covered_by_episodes(summaries, episodes)

        self.assertEqual(kept, [])
        self.assertEqual(summaries[0]['summary_id'], 'summary-1')

    def test_partial_source_coverage_keeps_summary_and_episode(self):
        summaries = [{'summary_id': 'summary-1', 'source_event_ids': ('a', 'b', 'c')}]
        episodes = [{'episode_id': 'episode-1', 'source_event_ids': ('a', 'b')}]

        kept = context_layer._drop_summaries_covered_by_episodes(summaries, episodes)

        self.assertEqual([item['summary_id'] for item in kept], ['summary-1'])

    def test_context_layer_passes_the_existing_query_vector_to_episode_recall(self):
        fake_rag = types.ModuleType('memory_search')
        fake_rag.is_vector_ready = lambda: True
        fake_rag.embed = Mock(return_value=[3.0, 4.0])
        fake_rag._to_vec = lambda _raw: ('existing-query-vector',)
        episode = self._save('context-episode', text='一起讨论东京旅行', source_ids=('old',))
        episode.update({
            'id': episode['episode_id'],
            'content': '一起讨论东京旅行',
            'score': 0.9,
            'provenance_quality': 'linked',
        })
        with patch.dict(sys.modules, {'memory_search': fake_rag}), \
             patch.object(smart_recall, 'two_level_recall', return_value={}) as two_level, \
             patch.object(episodic_index, 'recall_episodes', return_value=[episode]) as recalled:
            pack = context_layer.assemble_from_events(
                [_event('hot', minutes_ago=1)],
                user_id='u', character_id='gojo', user_message='东京旅行',
                now=NOW, include_recall=True,
            )

        self.assertEqual(fake_rag.embed.call_count, 1)
        self.assertEqual(
            two_level.call_args.kwargs['query_embedding'],
            ('existing-query-vector',),
        )
        self.assertEqual(recalled.call_args.kwargs['query_embedding'], ('existing-query-vector',))
        self.assertIn('【你们以前一起经历过的事】', pack.episode_prompt_text)
        self.assertIn('东京旅行', pack.episode_prompt_text)

    def test_episode_uses_existing_recall_budget_not_a_new_channel(self):
        item = context_budget.ContextItem(
            item_id='episode:e1', item_type='episodic_memory', text='一次可追溯的经历')
        grouped = context_budget.group_by_channel([item])

        self.assertEqual(item.channel, 'recall')
        self.assertEqual(grouped['recall'], [item])
        self.assertNotIn('episode', context_budget.CHANNEL_SPECS)

    def test_episode_and_budget_traces_never_include_episode_body(self):
        secret = 'PRIVATE-EPISODE-BODY-MUST-NOT-LEAK'
        self._save('trace-private', text=secret, source_ids=('evt-a',))
        with patch('builtins.print') as logged, self._active_sources({'evt-a'}):
            episodic_index.recall_episodes('u', 'gojo', secret)

        trace = '\n'.join(
            str(call.args[0]) for call in logged.call_args_list
            if call.args and str(call.args[0]).startswith('[recall_trace]')
        )
        self.assertIn('recent_count=', trace)
        self.assertIn('lexical_count=', trace)
        self.assertIn('semantic_count=', trace)
        self.assertIn('union_count=', trace)
        self.assertIn('candidate_sources=', trace)
        self.assertIn('episode_ranked', trace)
        self.assertNotIn(secret, trace)

    def test_episode_budget_drop_trace_never_includes_episode_body(self):
        secret = 'PRIVATE-EPISODE-BUDGET-BODY-MUST-NOT-LEAK'
        item = context_budget.ContextItem(
            item_id='episode:private', item_type='episodic_memory', text=secret,
            metadata={'recall_kind': 'episode', 'raw': {'score': 0.9}},
        )
        with patch('builtins.print') as logged:
            context_layer._trace_budget_dropped([item], {'recall': []})

        trace = '\n'.join(
            str(call.args[0]) for call in logged.call_args_list
            if call.args and str(call.args[0]).startswith('[recall_trace] budget_dropped')
        )
        self.assertIn('type=episode', trace)
        self.assertNotIn(secret, trace)

    def test_prompt_block_declares_priority_and_non_invention_rules(self):
        item = context_budget.ContextItem(
            item_id='episode:e1', item_type='episodic_memory', text='一起讨论东京旅行',
            metadata={'raw': {'range_end': NOW}},
        )

        block = context_layer._format_episode_block([item])
        prompt_source = Path(BACKEND, 'prompt.py').read_text(encoding='utf-8')

        self.assertIn('【你们以前一起经历过的事】', block)
        self.assertIn('当前用户消息/当前直接事件', block)
        self.assertIn('不得补写未记录的心理、动机、关系含义或细节', block)
        self.assertIn('episode_prompt_text', prompt_source)

    def test_episode_prompt_block_reaches_system_prompt(self):
        pack = context_layer.ChatContextPack(
            support_ready=True,
            recall_ready=True,
            recall_result={},
            memory_text='已有召回证据优先级',
            episode_prompt_text='【你们以前一起经历过的事】\n- 一起讨论东京旅行\n',
            accounts_text='账户上下文',
        )
        with patch.object(prompt, 'get_character',
                          return_value={'core_prompt': '角色核心设定'}), \
             patch.object(prompt, 'load_canon_lock', return_value=''), \
             patch.object(prompt, 'get_time_context', return_value=''), \
             patch.object(prompt, 'get_first_interaction_days', return_value=None):
            system_prompt = prompt.build_system_prompt(
                'u', 'gojo', user_message='东京旅行还记得吗', context_pack=pack)

        self.assertIn('【你们以前一起经历过的事】', system_prompt)
        self.assertIn('一起讨论东京旅行', system_prompt)

    def test_existing_fact_and_lifecycle_recall_stays_available_when_episode_empty(self):
        fact = {
            'id': 'fact-1', 'content': '用户喜欢抹茶', 'timestamp': NOW,
            'category': '偏好', 'score': 0.9, 'bonds': [],
        }
        lifecycle = {
            'id': 'life-1', 'content': '近期正在准备考试', 'timestamp': NOW,
            'updated_at': NOW, 'memory_kind': 'candidate', 'score': 0.6,
        }
        with patch.object(smart_recall, 'two_level_recall', return_value={
            'facts': [fact], 'lifecycle_memories': [lifecycle],
        }), patch.object(episodic_index, 'recall_episodes', return_value=[]):
            pack = context_layer.assemble_from_events(
                [_event('hot', minutes_ago=1)],
                user_id='u', character_id='gojo', user_message='抹茶和考试',
                now=NOW, include_recall=True,
            )

        self.assertIn('用户喜欢抹茶', pack.memory_text)
        self.assertIn('近期正在准备考试', pack.memory_text)
        self.assertEqual(pack.episode_prompt_text, '')
        self.assertEqual(pack.relationship_prompt_text, '')

    def test_episode_retrieval_does_not_write_other_memory_or_cognitive_modules(self):
        episode_source = Path(BACKEND, 'episodic_index.py').read_text(encoding='utf-8')
        context_source = Path(BACKEND, 'context_layer.py').read_text(encoding='utf-8')

        self.assertIn('def recall_episodes', episode_source)
        self.assertNotIn('append_raw_event(', episode_source)
        self.assertNotIn('save_long_memory(', episode_source)
        self.assertNotIn('save_bond_memory(', episode_source)
        self.assertNotIn('relationship_', episode_source)
        self.assertNotIn('cognitive_', episode_source)
        self.assertIn('recall_episodes', context_source)
        self.assertNotIn('save_episode(', context_source)


if __name__ == '__main__':
    unittest.main()
