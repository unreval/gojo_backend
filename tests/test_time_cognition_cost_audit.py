"""Exact local token measurements with fixed text and clocks, without vendors."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

import context_budget
import context_layer
import user_memory
from time_cognition_audit_support import baseline_generation_code

ZONE = timezone(timedelta(hours=8))
USER_BODY = '我正在整理项目资料，核对已经完成。接下来把记录按顺序排好，再核对一遍。' * 4
ASSISTANT_BODY = '資料の確認は終わったね。次は記録を順番に並べて、もう一度確かめよう。' * 4
EXPECTED = {
    (20, 'two_minutes'): (3178, 3610, 20, 20),
    (20, 'two_hours'): (3204, 4028, 8, 8),
    (20, 'cross_midnight'): (3178, 3626, 20, 20),
    (40, 'two_minutes'): (6388, 7253, 26, 22),
    (40, 'two_hours'): (6434, 8078, 8, 8),
    (40, 'cross_midnight'): (6388, 7269, 26, 22),
}


def synthetic_history(count, spacing):
    # cross_midnight keeps two-minute spacing, but crosses 23:59 -> 00:01.
    now = datetime(2026, 10, 14, 0, 21, tzinfo=ZONE) if spacing == 'cross_midnight' else (
        datetime(2026, 10, 14, 12, 1, tzinfo=ZONE))
    interval = timedelta(hours=2) if spacing == 'two_hours' else timedelta(minutes=2)
    last = now - timedelta(minutes=2)
    rows = [dict(event_id=f'audit-{i}', role='user' if i % 2 == 0 else 'assistant',
                 content=USER_BODY if i % 2 == 0 else ASSISTANT_BODY,
                 timestamp=last - interval * (count - i - 1), metadata={})
            for i in range(count)]
    return now, rows


def _assemble(rows, now):
    # Supply the synthetic candidate list directly without any SQL fetch;
    # the hot-window and final budget themselves are real.
    with ExitStack() as stack:
        stack.enter_context(patch.object(context_layer, 'maybe_store_spill_summary'))
        stack.enter_context(patch.object(context_layer, 'list_rolling_summaries', return_value=[]))
        stack.enter_context(patch.object(context_layer, 'list_pins', return_value=[]))
        stack.enter_context(patch('builtins.print'))
        return context_layer.assemble_from_events(
            rows, user_id='audit-user', character_id='gojo', now=now,
            config=context_budget.BudgetConfig(), include_recall=False,
            include_support=False, auto_pin_enabled=False)


def measure_scenario(count, spacing):
    now, rows = synthetic_history(count, spacing)
    original = deepcopy(rows)
    estimate = context_budget.estimate_tokens
    old_texts = [user_memory._time_marker(row['timestamp'], now, now.date()) + row['content']
                 for row in rows]
    new_rows = context_layer.format_history_messages(rows, now=now)
    old_cost = sum(estimate(text) for text in old_texts)
    new_cost = sum(estimate(row['prompt_text']) for row in new_rows)
    eligible_rows = [row for row in rows if row['timestamp'] >= now - timedelta(hours=72)]
    current = _assemble(rows, now)
    filtered_current = (_assemble(eligible_rows, now) if len(eligible_rows) != count else current)
    with baseline_generation_code():
        baseline = _assemble(rows, now)
        filtered_baseline = (_assemble(eligible_rows, now)
                             if len(eligible_rows) != count else baseline)
    assert rows == original
    return dict(
        input_messages=count, spacing=spacing, frozen_now=now.isoformat(),
        raw_tokens=sum(estimate(row['content']) for row in rows),
        old_time_marker_tokens=old_cost, new_label_tokens=new_cost,
        token_delta=new_cost - old_cost,
        hot_token_budget=context_budget.BudgetConfig().hot_token_budget,
        total_token_budget=context_budget.BudgetConfig().total_token_budget,
        baseline_hot_messages=len(baseline.messages), new_hot_messages=len(current.messages),
        retained_delta=len(current.messages) - len(baseline.messages),
        baseline_hot_tokens=baseline.allocation.get('hot', 0),
        new_hot_tokens=current.allocation.get('hot', 0),
        newer_history_gap_labels=sum('【对话间隔' in row['prompt_text'] for row in new_rows),
        candidate_reader_72h_eligible=len(eligible_rows),
        filtered_baseline_hot_messages=len(filtered_baseline.messages),
        filtered_new_hot_messages=len(filtered_current.messages),
        filtered_baseline_hot_tokens=filtered_baseline.allocation.get('hot', 0),
        filtered_new_hot_tokens=filtered_current.allocation.get('hot', 0),
    )


def token_cost_matrix():
    return [measure_scenario(count, spacing) for count in (20, 40)
            for spacing in ('two_minutes', 'two_hours', 'cross_midnight')]


class TokenCostAuditTests(unittest.TestCase):
    def test_fixed_twenty_message_matrix(self):
        self.check_count(20)

    def test_fixed_forty_message_matrix(self):
        self.check_count(40)

    def check_count(self, count):
        for spacing in ('two_minutes', 'two_hours', 'cross_midnight'):
            with self.subTest(spacing=spacing):
                result = measure_scenario(count, spacing)
                self.assertEqual((result['old_time_marker_tokens'], result['new_label_tokens'],
                                  result['baseline_hot_messages'], result['new_hot_messages']),
                                 EXPECTED[count, spacing])
                self.assertIsInstance(result['token_delta'], int)
                self.assertGreater(result['token_delta'], 0)
                self.assertEqual(result['hot_token_budget'], 4000)
                self.assertEqual(result['total_token_budget'], 8000)
                self.assertEqual(result['retained_delta'],
                                 result['new_hot_messages'] - result['baseline_hot_messages'])
                self.assertLessEqual(result['new_hot_tokens'], result['hot_token_budget'])
                self.assertLessEqual(result['baseline_hot_tokens'], result['hot_token_budget'])
                self.assertEqual(result['filtered_baseline_hot_messages'],
                                 result['baseline_hot_messages'])
                self.assertEqual(result['filtered_new_hot_messages'],
                                 result['new_hot_messages'])
                self.assertEqual(result['candidate_reader_72h_eligible'],
                                 36 if count == 40 and spacing == 'two_hours' else count)
                if spacing == 'cross_midnight':
                    self.assertEqual(result['newer_history_gap_labels'], 1)
                if spacing == 'two_hours':
                    # Both unchanged continuity algorithms stop after min=8;
                    # this result must not be attributed to token pressure.
                    self.assertEqual(result['baseline_hot_messages'], 8)
                    self.assertEqual(result['new_hot_messages'], 8)


if __name__ == '__main__':
    unittest.main()
