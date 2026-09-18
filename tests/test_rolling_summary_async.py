# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import context_budget  # noqa: E402
import context_layer  # noqa: E402
import rolling_summary  # noqa: E402


NOW = datetime(2026, 9, 18, 18, 0, tzinfo=timezone.utc)


def _event(index, *, text=None, minutes_ago=0):
    return {
        'event_id': f'e{index}',
        'role': 'user' if index % 2 == 0 else 'assistant',
        'content': text or f'讨论进度 {index} Canonical Raw Event 继续推进',
        'timestamp': NOW - timedelta(minutes=minutes_ago),
    }


class RollingSummaryAsyncTests(unittest.TestCase):
    def setUp(self):
        context_layer.use_memory_store(True)
        rolling_summary.reset_memory_jobs()

    def tearDown(self):
        context_layer.use_memory_store(False)
        rolling_summary.use_memory_store(False)

    def test_assemble_does_not_call_summary_llm(self):
        events = [_event(i, minutes_ago=80 - i) for i in range(40)]
        cfg = context_budget.BudgetConfig(
            hot_max_events=8, hot_min_events=4, min_summary_events=8,
            hot_token_budget=200, summary_min_tokens=50,
        )
        with patch.object(rolling_summary, 'generate_real_summary_text') as llm:
            pack = context_layer.assemble_from_events(
                events, user_id='u', character_id='gojo',
                now=NOW, config=cfg, include_recall=False)
        llm.assert_not_called()
        self.assertTrue(pack.spill_event_ids)
        jobs = rolling_summary.list_memory_jobs()
        self.assertTrue(jobs)
        self.assertEqual(jobs[0]['kind'], 'rolling_summary')
        summaries = context_layer.list_rolling_summaries('u', 'gojo')
        self.assertTrue(summaries)
        self.assertTrue(summaries[0].get('is_placeholder', True))

    def test_idempotent_enqueue_same_segment(self):
        events = [_event(i, minutes_ago=50 - i) for i in range(20)]
        cfg = context_budget.BudgetConfig(min_summary_events=8, summary_min_tokens=20)
        rolling_summary.enqueue_summary_job('u', 'gojo', events, config=cfg, now=NOW)
        rolling_summary.enqueue_summary_job('u', 'gojo', events, config=cfg, now=NOW)
        jobs = [j for j in rolling_summary.list_memory_jobs() if j['status'] == 'pending']
        self.assertEqual(len(jobs), 1)

    def test_retry_does_not_duplicate_real_summary(self):
        events = [_event(i, minutes_ago=40 - i) for i in range(12)]
        extra = {
            'source_event_ids': [f'e{i}' for i in range(12)],
            'events': events,
            'processor_version': rolling_summary.SUMMARY_PROCESSOR_VERSION,
        }
        with patch.object(rolling_summary, 'generate_real_summary_text',
                          return_value='情景摘要（12 条原文）：发生：做完 Recall'):
            ok1 = rolling_summary.process_summary_job('u', 'gojo', extra)
            ok2 = rolling_summary.process_summary_job('u', 'gojo', extra)
        self.assertTrue(ok1)
        self.assertTrue(ok2)
        active = [
            row for row in context_layer.list_rolling_summaries('u', 'gojo')
            if not row.get('is_placeholder', True)
        ]
        self.assertEqual(len(active), 1)

    def test_pending_uses_placeholder_in_prompt(self):
        events = [_event(i, minutes_ago=90 - i) for i in range(30)]
        cfg = context_budget.BudgetConfig(
            hot_max_events=6, hot_min_events=4, min_summary_events=8,
            hot_token_budget=180,
        )
        pack = context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo',
            now=NOW, config=cfg, include_recall=False)
        self.assertIn('滚动摘要', pack.summary_prompt_text)
        self.assertIn('离开热窗口', pack.summary_prompt_text)

    def test_merge_supersedes_instead_of_stacking(self):
        v1 = context_layer.save_rolling_summary(
            'u', 'gojo', '第一版摘要', ['e0', 'e1', 'e2'],
            summary_id='sum-v1', is_placeholder=False,
            processor_version=rolling_summary.SUMMARY_PROCESSOR_VERSION)
        extra = {
            'source_event_ids': ['e0', 'e1', 'e2', 'e3'],
            'events': [_event(i) for i in range(4)],
            'merge_from': 'sum-v1',
            'processor_version': rolling_summary.SUMMARY_PROCESSOR_VERSION,
        }
        with patch.object(rolling_summary, 'generate_real_summary_text',
                          return_value='情景摘要（4 条原文）：发生：合并后的版本'):
            rolling_summary.process_summary_job('u', 'gojo', extra)
        rows = [
            item for item in context_layer._SUMMARIES.values()
            if item['user_id'] == 'u'
        ]
        statuses = {item['summary_id']: item['status'] for item in rows}
        self.assertEqual(statuses.get('sum-v1'), 'superseded')
        real = [item for item in rows if not item.get('is_placeholder', True)
                and item.get('status') == 'active']
        self.assertEqual(len(real), 1)
        self.assertIn('合并后的版本', real[0]['text'])

    def test_unique_source_delete_invalidates(self):
        context_layer.save_rolling_summary(
            'u', 'gojo', '唯一来源', ['gone-1'],
            summary_id='sum-gone', is_placeholder=False)
        changed = context_layer.reconcile_summaries_for_deleted(
            'u', 'gojo', ['gone-1'])
        self.assertEqual(changed[0][0], 'invalidated')
        pack = context_layer.assemble_from_events(
            [_event(1)], user_id='u', character_id='gojo',
            now=NOW, include_recall=False, deleted_ids={'gone-1'})
        self.assertNotIn('唯一来源', pack.summary_prompt_text)

    def test_partial_delete_marks_stale(self):
        context_layer.save_rolling_summary(
            'u', 'gojo', '两源摘要', ['keep-1', 'gone-2'],
            summary_id='sum-partial', is_placeholder=False)
        changed = context_layer.reconcile_summaries_for_deleted(
            'u', 'gojo', ['gone-2'])
        self.assertEqual(changed[0][0], 'stale')
        item = context_layer._SUMMARIES['sum-partial']
        self.assertEqual(item['status'], 'stale')
        self.assertTrue(item.get('rebuild_required'))


if __name__ == '__main__':
    unittest.main()
