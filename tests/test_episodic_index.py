import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import context_layer  # noqa: E402
import episodic_index  # noqa: E402
import raw_events  # noqa: E402
import rolling_summary  # noqa: E402


NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _event(event_id, role='user', content='一条原始事件', minutes_ago=0):
    return {
        'event_id': event_id,
        'role': role,
        'content': content,
        'timestamp': NOW - timedelta(minutes=minutes_ago),
    }


class EpisodicIndexTests(unittest.TestCase):
    def setUp(self):
        episodic_index.use_memory_store(True)
        context_layer.use_memory_store(True)
        rolling_summary.reset_memory_jobs()

    def tearDown(self):
        episodic_index.use_memory_store(False)
        context_layer.use_memory_store(False)
        rolling_summary.use_memory_store(False)

    def _canonical_build(self, events, *, summary_text=''):
        canonical = [dict(item) for item in events]
        with patch.object(raw_events, 'sources_are_active', return_value=True), \
             patch.object(raw_events, 'get_active_events_by_ids', return_value=canonical):
            return episodic_index.build_episode_from_events(
                'u', 'gojo', events,
                summary_text=summary_text,
            )

    def test_same_stable_segment_retry_creates_exactly_one_active_episode(self):
        events = [
            _event('evt-1', 'user', '我们讨论周末的旅行安排', 5),
            _event('evt-2', 'assistant', '先确认日期，再看车票', 4),
        ]
        first = self._canonical_build(events, summary_text='发生：讨论旅行安排')
        second = self._canonical_build(events, summary_text='发生：讨论旅行安排')

        self.assertEqual(first['episode_id'], second['episode_id'])
        active = episodic_index.list_episodes('u', 'gojo')
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]['source_event_ids'], ('evt-1', 'evt-2'))

    def test_episode_provenance_is_input_only_and_traceable_to_raw_events(self):
        events = [
            _event('evt-a', 'user', '用户说周末去看展', 4),
            _event('evt-b', 'assistant', '角色回应会留出时间', 3),
        ]
        episode = self._canonical_build(
            events,
            summary_text=(
                '情景摘要（2 条原文）：发生：讨论周末看展；'
                '决定：周末再确认；未决：是否需要订票；'
                '有证据的情绪/关系：用户说有点焦虑；'
                'source_event_ids：["forged-event"]'
            ),
        )

        self.assertEqual(episode['source_event_ids'], ('evt-a', 'evt-b'))
        self.assertNotIn('forged-event', episode['source_event_ids'])
        self.assertEqual(episode['participants'], ('user', 'assistant'))
        self.assertIn('讨论周末看展', episode['what_happened'])
        self.assertNotIn('焦虑', episode['what_happened'])

    def test_builder_rejects_missing_or_noncanonical_sources(self):
        events = [_event('evt-real', 'user', '原始事件', 1)]
        with patch.object(raw_events, 'sources_are_active', return_value=True), \
             patch.object(raw_events, 'get_active_events_by_ids', return_value=[]):
            missing = episodic_index.build_episode_from_events('u', 'gojo', events)

        self.assertIsNone(missing)
        self.assertEqual(episodic_index.list_episodes('u', 'gojo'), [])

    def test_delete_all_sources_invalidates_episode(self):
        events = [_event('evt-1'), _event('evt-2', 'assistant')]
        episode = self._canonical_build(events)
        with patch.object(raw_events, 'deleted_event_ids', return_value={'evt-1', 'evt-2'}):
            changed = episodic_index.reconcile_deleted_sources(
                'u', 'gojo', ['evt-1', 'evt-2'])

        self.assertEqual(changed, [('invalidated', episode['episode_id'])])
        self.assertEqual(
            episodic_index.get_episode(episode['episode_id'])['status'], 'invalidated')

    def test_delete_some_sources_marks_episode_stale_without_dropping_provenance(self):
        events = [_event('evt-1'), _event('evt-2', 'assistant')]
        episode = self._canonical_build(events)
        with patch.object(raw_events, 'deleted_event_ids', return_value={'evt-2'}):
            changed = episodic_index.reconcile_deleted_sources('u', 'gojo', ['evt-2'])

        self.assertEqual(changed, [('stale', episode['episode_id'])])
        stale = episodic_index.get_episode(episode['episode_id'])
        self.assertEqual(stale['status'], 'stale')
        self.assertTrue(stale['rebuild_required'])
        self.assertEqual(stale['source_event_ids'], ('evt-1', 'evt-2'))

    def test_rebuild_creates_one_successor_version_and_supersedes_prior(self):
        events = [_event('evt-1'), _event('evt-2', 'assistant')]
        first = self._canonical_build(events, summary_text='发生：第一版经历')
        with patch.object(raw_events, 'sources_are_active', return_value=True), \
             patch.object(raw_events, 'get_active_events_by_ids', return_value=events):
            rebuilt = episodic_index.rebuild_episode(
                first['episode_id'], events, summary_text='发生：第二版经历')
            retried = episodic_index.rebuild_episode(
                first['episode_id'], events, summary_text='发生：第二版经历')

        old = episodic_index.get_episode(first['episode_id'])
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(old['superseded_by'], rebuilt['episode_id'])
        self.assertEqual(rebuilt['version'], 2)
        self.assertEqual(rebuilt['status'], 'active')
        self.assertEqual(rebuilt['episode_id'], retried['episode_id'])
        self.assertEqual(len(episodic_index.list_episodes('u', 'gojo', status=None)), 2)

    def test_summary_worker_queues_non_llm_episode_index_after_real_summary(self):
        events = [_event('evt-1'), _event('evt-2', 'assistant')]
        extra = {
            'source_event_ids': ['evt-1', 'evt-2'],
            'events': events,
            'processor_version': rolling_summary.SUMMARY_PROCESSOR_VERSION,
        }
        with patch.object(rolling_summary, 'generate_real_summary_text',
                          return_value='情景摘要（2 条原文）：发生：稳定片段'), \
             patch.object(rolling_summary, '_enqueue_episode_index') as enqueue:
            self.assertTrue(rolling_summary.process_summary_job('u', 'gojo', extra))

        enqueue.assert_called_once_with(
            'u', 'gojo', ['evt-1', 'evt-2'],
            '情景摘要（2 条原文）：发生：稳定片段')

    def test_episode_trace_omits_event_content(self):
        secret = 'PRIVATE-EPISODE-CONTENT-MUST-NOT-APPEAR-IN-TRACE'
        events = [_event('evt-1', content=secret)]
        with patch('builtins.print') as logged:
            self._canonical_build(events, summary_text=f'发生：{secret}')

        trace = '\n'.join(
            str(call.args[0]) for call in logged.call_args_list
            if call.args and str(call.args[0]).startswith('[episode_trace]')
        )
        self.assertIn('source_count=1', trace)
        self.assertIn('status=active', trace)
        self.assertNotIn(secret, trace)

    def test_episode_index_is_not_called_when_main_recall_is_disabled(self):
        event = _event('hot-1', content='仍只使用热窗口')
        with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
             patch.object(raw_events, 'get_hot_candidate_events', return_value=[event]), \
             patch.object(episodic_index, 'list_episodes',
                          side_effect=AssertionError('episode recall is not enabled')):
            pack = context_layer.build_chat_context(
                'u', 'gojo', include_recall=False, now=NOW)

        self.assertFalse(pack.failed_closed)
        self.assertEqual(pack.recent_event_ids, ['hot-1'])

    def test_episode_module_keeps_raw_relationship_and_cognitive_boundaries(self):
        source = Path(BACKEND, 'episodic_index.py').read_text(encoding='utf-8')
        context_source = Path(BACKEND, 'context_layer.py').read_text(encoding='utf-8')
        self.assertIn('get_active_events_by_ids', source)
        self.assertIn('sources_are_active', source)
        self.assertNotIn('append_raw_event(', source)
        self.assertNotIn('relationship_', source)
        self.assertNotIn('cognitive_', source)
        self.assertIn('recall_episodes', context_source)
        self.assertNotIn('save_episode(', context_source)
        self.assertNotIn('episodic_index', Path(BACKEND, 'smart_recall.py').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
