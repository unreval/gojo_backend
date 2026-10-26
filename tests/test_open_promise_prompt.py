"""Unfired promises are shown to the character and never become a write path."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import inspect
import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import context_layer
import prompt

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 26, 6, 0, tzinfo=CN)


def _once(trigger_at, context):
    return {
        'trigger_kind': 'once',
        'trigger_at': trigger_at,
        'trigger_time': None,
        'context': context,
    }


class OpenPromiseDisplayTests(unittest.TestCase):
    def lines(self, rows, now=NOW, grace=120):
        return prompt._open_promise_lines(rows, now, grace)

    def test_utc_wall_clock_is_shown_as_china_time_with_status(self):
        row = _once('2026-10-25 22:00:00', '六点叫她起床去考试')
        self.assertIn(
            '2026-10-26 06:00（未到点）六点叫她起床去考试',
            self.lines([row], datetime(2026, 10, 26, 5, 0, tzinfo=CN)))
        self.assertIn(
            '2026-10-26 06:00（已到点未发）',
            self.lines([row], NOW))
        self.assertIn(
            '2026-10-26 06:00（已过期）',
            self.lines([row], datetime(2026, 10, 26, 8, 1, tzinfo=CN)))

    def test_offset_already_present_is_not_shifted_again(self):
        text = self.lines([_once('2026-10-26T06:00:00+08:00', '已经带时区')])
        self.assertIn('2026-10-26 06:00（已到点未发）已经带时区', text)
        self.assertNotIn('14:00', text)

    def test_daily_status_uses_today_clock(self):
        row = {
            'trigger_kind': 'daily', 'trigger_at': None,
            'trigger_time': '21:30', 'context': '每晚问一句',
        }
        self.assertIn('每天 21:30（未到点）', self.lines(
            [row], datetime(2026, 10, 26, 21, 0, tzinfo=CN)))
        self.assertIn('每天 21:30（已到点未发）', self.lines(
            [row], datetime(2026, 10, 26, 22, 0, tzinfo=CN)))
        self.assertIn('每天 21:30（已过期）', self.lines(
            [row], datetime(2026, 10, 26, 23, 31, tzinfo=CN)))

    def test_overflow_keeps_the_triggers_nearest_to_now(self):
        rows = []
        for index in range(9):
            instant = NOW + timedelta(days=index + 1)
            rows.append(_once(instant.isoformat(), f'far-{index}'))
        rows.append(_once((NOW + timedelta(minutes=10)).isoformat(), '最近这一条'))
        text = self.lines(rows)
        self.assertIn('最近这一条', text)
        self.assertIn('未到点', text)
        self.assertIn('far-0', text)
        self.assertNotIn('far-8', text)
        self.assertNotIn('far-7', text)
        self.assertIn('其余 2 条未列出', text)
        self.assertIn('06:10', text)

    def test_reader_failure_adds_nothing(self):
        with patch('db_promise.list_active_promises', side_effect=RuntimeError('down')):
            self.assertEqual(prompt._format_open_promises('gojo', 'u', NOW), '')

    def test_formatter_does_not_write_promise_state(self):
        source = inspect.getsource(prompt._format_open_promises)
        source += inspect.getsource(prompt._open_promise_lines)
        for banned in ('add_promise', 'deactivate', 'mark_fired', 'INSERT', 'UPDATE', 'DELETE'):
            self.assertNotIn(banned, source)

    def test_bond_lines_stay_date_only(self):
        source = inspect.getsource(prompt._build_prompt_parts)
        self.assertIn(
            'bond_lines = [f\'- [{ts.strftime("%Y-%m-%d") if ts else "?"}] {c}\'',
            source)

    def test_prompt_shows_the_read_only_block_outside_the_cached_head(self):
        now_utc = datetime(2026, 10, 25, 21, 0, tzinfo=timezone.utc)
        pack = context_layer.ChatContextPack(
            support_ready=True, recall_ready=True, recall_result={},
            temporal_snapshot={'now_utc': now_utc, 'has_history': False},
            relationship_prompt_text='', expression_rules='')
        rows = [_once('2026-10-25 22:00:00', '六点叫她起床去考试')]
        with ExitStack() as stack:
            for name, value in (
                    ('get_character', {'core_prompt': 'CORE'}),
                    ('load_canon_lock', ''),
                    ('get_first_interaction_days', 3),
                    ('_accounts_block', '')):
                stack.enter_context(patch.object(prompt, name, return_value=value))
            stack.enter_context(patch(
                'memory_authority.filter_recall_authority', return_value={}))
            stack.enter_context(patch(
                'smart_recall.format_recall_for_prompt', return_value=('', '', '')))
            stack.enter_context(patch.object(
                context_layer, 'revalidate_pack_blocks', return_value={
                    key: '' for key in (
                        'cognitive_prompt_text', 'pinned_prompt_text',
                        'summary_prompt_text', 'episode_prompt_text')}))
            stack.enter_context(patch(
                'temporal_awareness.build_prompt_context', return_value='现在是五点。'))
            stack.enter_context(patch(
                'db_schedule.format_world_prompt', return_value=('', {})))
            listed = stack.enter_context(patch(
                'db_promise.list_active_promises', return_value=rows))
            writer = stack.enter_context(patch('db_promise.add_promise'))
            blocks = prompt.build_system_blocks(
                'u', 'gojo', '在吗', context_pack=pack,
                temporal_snapshot={'now_utc': now_utc})
        self.assertEqual(len(blocks), 3)
        self.assertNotIn('六点叫她起床', blocks[0]['text'])
        self.assertNotIn('只读展示', blocks[0]['text'])
        self.assertIn('2026-10-26 06:00（未到点）六点叫她起床去考试', blocks[2]['text'])
        self.assertIn('只读展示', blocks[2]['text'])
        self.assertIn('不能用来判断关系或改写约定状态', blocks[2]['text'])
        listed.assert_called_once_with('gojo', 'u', limit=20)
        writer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
