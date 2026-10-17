"""Future schedule rendering through both generation readers, without live IO."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

import context_layer
import db_schedule
import prompt


class FutureSchedulePromptIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = datetime(2026, 10, 17, 8, tzinfo=timezone.utc)
        self.snapshot = dict(now_utc=self.now, has_history=False)
        self.world = dict(event=None, activity=None, phase=None,
                          availability=dict(reply_state='free', can_reply=True))
        self.planned = [(47, 3, self.now + timedelta(hours=3),
                         self.now + timedelta(hours=5), '晚饭', '涩谷 · 一般区域',
                         'flexible', 'meal', 'free')]
        self.transitions = []
        self.failure = None
        self.cursor = Mock(spec=['execute', 'fetchall', 'close'])
        self.cursor.execute.side_effect = self.execute
        self.connection = Mock(spec=['cursor', 'close', 'commit', 'rollback'])
        self.connection.cursor.return_value = self.cursor
        self.current = self.stack.enter_context(patch.object(
            db_schedule, 'get_current_world_state', return_value=self.world))
        self.stack.enter_context(patch.object(db_schedule, 'get_conn', return_value=self.connection))
        self.stack.enter_context(patch('psycopg2.connect', side_effect=AssertionError('live DB forbidden')))
        self.stack.enter_context(patch('ai_client.get_anthropic', side_effect=AssertionError('model forbidden')))
        for target, value in (
                ('prompt.get_character', {'core_prompt': 'CORE-PERSONA'}),
                ('prompt.load_canon_lock', 'CANON-LOCK'),
                ('prompt.get_first_interaction_days', 20),
                ('prompt._accounts_block', ''), ('prompt.get_period_context', ''),
                ('user_memory.get_first_interaction_days', 20),
                ('user_memory.get_recent_openings', []),
                ('user_memory.get_last_assistant_reply', ''),
                ('relationship_reader.build_state_summary', ''),
                ('cognitive_reader.iter_active_cognitive_items', []),
                ('diary_engine.build_diary_hint', ''),
                ('characters.retrieve_character_memory', []),
                ('memory_authority.filter_recall_authority', {}),
                ('context_layer.revalidate_pack_blocks', dict.fromkeys((
                    'cognitive_prompt_text', 'pinned_prompt_text',
                    'summary_prompt_text', 'episode_prompt_text'), ''))):
            self.stack.enter_context(patch(target, return_value=value))

    def execute(self, sql, params):
        self.assertTrue(sql.strip().startswith('SELECT'))
        self.assertEqual(params[:2], ('gojo', 'u'))
        kind = 'transitions' if 'FROM char_schedule_transition' in sql else 'planned'
        if self.failure == kind:
            raise RuntimeError(f'{kind} unavailable')
        self.cursor.fetchall.return_value = getattr(self, kind)

    def blocks(self):
        pack = context_layer.ChatContextPack(
            support_ready=True, recall_ready=True, recall_result={},
            temporal_snapshot=self.snapshot, schedule_text='STALE-SCHEDULE')
        return prompt.build_system_blocks('u', 'gojo', '今晚有空吗？', context_pack=pack)

    def support(self):
        return context_layer._support_items(
            'u', 'gojo', '今晚有空吗？', [], temporal_snapshot=self.snapshot)[0]

    def test_future_without_active_event_reaches_uncached_generation_block(self):
        first = self.blocks()
        self.assertIn('【今天接下来的安排', first[-1]['text'])
        self.assertIn('event_id=47 revision=3', first[-1]['text'])
        self.assertIn('晚饭（涩谷 · 一般区域）', first[-1]['text'])
        self.assertNotIn('STALE-SCHEDULE', first[-1]['text'])
        self.assertNotIn('cache_control', first[-1])
        self.assertNotIn('event_id=47', first[0]['text'] + first[1]['text'])
        self.current.assert_called_once()
        row = self.planned[0]
        self.planned = [(row[0], 4, *row[2:])]
        second = self.blocks()
        self.assertEqual(first[:2], second[:2])
        self.assertIn('event_id=47 revision=4', second[-1]['text'])
        self.assertNotIn('event_id=47 revision=3', second[-1]['text'])

    def test_future_without_active_event_reaches_context_support(self):
        items = self.support()
        schedule = [item for item in items if item.item_type == 'schedule']
        self.assertEqual(len(schedule), 1)
        self.assertIn('event_id=47 revision=3', schedule[0].text)
        self.assertIn('晚饭（涩谷 · 一般区域）', schedule[0].text)
        self.assertIsNone(schedule[0].metadata['event_id'])
        self.current.assert_called_once()

    def test_query_and_render_errors_leave_generation_available_without_stale_schedule(self):
        for failure in ('planned', 'transitions', 'render'):
            with self.subTest(failure=failure), patch('builtins.print') as logged:
                self.failure = failure
                self.transitions = [({'type': 'cancel'}, object(), '晚饭')] if failure == 'render' else []
                blocks = self.blocks()
                self.assertIn('CORE-PERSONA', blocks[0]['text'])
                self.assertIn('【当前消息优先】', blocks[-1]['text'])
                for omitted in ('STALE-SCHEDULE', '【今天接下来的安排', 'event_id=47'):
                    self.assertNotIn(omitted, blocks[-1]['text'])
                self.assertTrue(any('日程注入跳过' in str(call) for call in logged.call_args_list))
        self.assertEqual(self.connection.close.call_count, 3)
        self.connection.commit.assert_not_called()

    def test_query_and_render_errors_leave_context_support_available(self):
        for failure in ('planned', 'transitions', 'render'):
            with self.subTest(failure=failure), patch('builtins.print') as logged:
                self.failure = failure
                self.transitions = [({'type': 'cancel'}, object(), '晚饭')] if failure == 'render' else []
                items = self.support()
                self.assertFalse(any(item.item_type == 'schedule' for item in items))
                self.assertTrue(any(item.item_type == 'temporal' for item in items))
                self.assertTrue(any('schedule skipped' in str(call) for call in logged.call_args_list))
        self.assertEqual(self.connection.close.call_count, 3)
        self.connection.commit.assert_not_called()


if __name__ == '__main__':
    unittest.main()
