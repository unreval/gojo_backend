# -*- coding: utf-8 -*-
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

import context_budget  # noqa: E402
import context_layer  # noqa: E402
import raw_events  # noqa: E402
import smart_recall  # noqa: E402
import user_memory  # noqa: E402


NOW = datetime(2026, 9, 17, 18, 0, tzinfo=timezone.utc)


def _event(index, *, text=None, minutes_ago=0, task_id=None, reply_to=None, now=NOW):
    ts = now - timedelta(minutes=minutes_ago)
    extra = {}
    if task_id:
        extra['task_id'] = task_id
    return {
        'event_id': f'e{index}',
        'role': 'user' if index % 2 == 0 else 'assistant',
        'content': text if text is not None else f'进度{index} Database v1 继续',
        'timestamp': ts,
        'metadata': extra,
        'reply_to_event_id': reply_to or '',
        'kind': 'text',
    }


class ContextBudgetManagerTests(unittest.TestCase):
    def test_estimate_tokens_is_local_and_stable(self):
        self.assertGreater(context_budget.estimate_tokens('汉' * 10), 0)
        self.assertEqual(
            context_budget.estimate_tokens('hello world'),
            context_budget.estimate_tokens('hello world'),
        )

    def test_allocate_does_not_split_item_text(self):
        long_text = '决定下一步做 Recall v2。' * 40
        items = [
            context_budget.ContextItem(
                item_id=f'h{i}', item_type='hot_raw', text=long_text,
                source_event_ids=(f'e{i}',), priority=100, role='user',
            )
            for i in range(12)
        ]
        cfg = context_budget.BudgetConfig(total_token_budget=800)
        kept = context_budget.ContextBudgetManager(cfg).allocate(items)
        originals = {item.text for item in items}
        for item in kept:
            self.assertIn(item.text, originals)
            self.assertFalse(item.text.endswith('Recall v') and item.text != long_text)
        self.assertEqual(kept[-1].item_id, items[-1].item_id)
        self.assertLessEqual(
            sum(item.token_cost for item in kept),
            cfg.total_token_budget + kept[-1].token_cost,
        )

    def test_unused_budget_is_borrowed(self):
        hot = [
            context_budget.ContextItem(
                item_id='h1', item_type='hot_raw', text='最近一句',
                source_event_ids=('e1',), role='user',
            )
        ]
        pins = [
            context_budget.ContextItem(
                item_id='p1', item_type='pinned',
                text='Database / Memory v1 已本地 commit 9c2dda7，尚未 push。',
                source_event_ids=('e0',), priority=90,
            )
        ]
        grouped = context_budget.allocate_channels(
            {'hot': hot, 'pinned': pins, 'summary': [], 'recall': [], 'aux': []},
            context_budget.BudgetConfig(total_token_budget=2000),
        )
        self.assertEqual(len(grouped['hot']), 1)
        self.assertEqual(len(grouped['pinned']), 1)


class AdaptiveHotContextTests(unittest.TestCase):
    def setUp(self):
        context_layer.use_memory_store(True)

    def tearDown(self):
        context_layer.use_memory_store(False)

    def test_high_frequency_keeps_more_than_40(self):
        events = [
            _event(i, minutes_ago=60 - i * 0.4)
            for i in range(120)
        ]
        cfg = context_budget.BudgetConfig()
        hot, spill = context_layer.select_hot_window(
            events, config=cfg, now=NOW, token_budget=4000)
        self.assertGreater(len(hot), 40)
        self.assertEqual(hot[-1]['event_id'], 'e119')
        self.assertIn('进度119', hot[-1]['content'])
        pack = context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo',
            now=NOW, config=cfg, include_recall=False)
        self.assertGreater(len(pack.messages), 40)
        self.assertGreater(len(pack.recent_event_ids), 40)
        self.assertEqual(pack.recent_event_ids[-1], 'e119')

    def test_token_overflow_keeps_latest_turn_whole(self):
        blob = '这是一条很长的连续讨论内容。' * 80
        events = [
            _event(i, text=blob, minutes_ago=30 - i)
            for i in range(20)
        ]
        cfg = context_budget.BudgetConfig(total_token_budget=1200, hot_token_budget=900)
        pack = context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo',
            now=NOW, config=cfg, include_recall=False)
        self.assertTrue(pack.messages)
        self.assertEqual(pack.messages[-1]['content'], blob)
        for msg in pack.messages:
            self.assertEqual(msg['content'], blob)
        used = sum(context_budget.estimate_tokens(m['content']) for m in pack.messages)
        self.assertLessEqual(used, cfg.total_token_budget + context_budget.estimate_tokens(blob))

    def test_same_task_does_not_cut_early(self):
        events = [
            _event(i, minutes_ago=200 - i, task_id='recall-v2')
            for i in range(100)
        ]
        cfg = context_budget.BudgetConfig(
            hot_time_horizon_minutes=60,
            hot_token_budget=5000,
            hot_max_events=180,
        )
        hot, _spill = context_layer.select_hot_window(events, config=cfg, now=NOW)
        self.assertGreater(len(hot), 40)
        self.assertEqual(hot[0]['metadata']['task_id'], 'recall-v2')

    def test_topic_shift_moves_old_segment_to_summary(self):
        old = [
            _event(i, text=f'Canonical Raw Event 讨论 {i}', minutes_ago=280 - i,
                   task_id='db-v1')
            for i in range(40)
        ]
        new = [
            _event(100 + i, text=f'今晚晚饭吃什么 {i}', minutes_ago=20 - i * 0.4,
                   task_id='dinner')
            for i in range(20)
        ]
        events = old + new
        cfg = context_budget.BudgetConfig(min_summary_events=8)
        pack = context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo',
            now=NOW, config=cfg, include_recall=False)
        hot_ids = set(pack.recent_event_ids)
        self.assertTrue(any(eid.startswith('e100') or eid.startswith('e11') for eid in hot_ids))
        self.assertIn('晚饭', ''.join(m['content'] for m in pack.messages))
        self.assertTrue(pack.spill_event_ids)
        self.assertTrue(
            pack.summary_prompt_text or context_layer.list_rolling_summaries('u', 'gojo'))
        summaries = context_layer.list_rolling_summaries('u', 'gojo')
        self.assertTrue(summaries)
        self.assertTrue(summaries[0]['source_event_ids'])
        self.assertIn('e0', summaries[0]['source_event_ids'])


class RollingSummaryAndPinTests(unittest.TestCase):
    def setUp(self):
        context_layer.use_memory_store(True)

    def tearDown(self):
        context_layer.use_memory_store(False)

    def test_rolling_summary_has_provenance_and_keeps_raw_events(self):
        events = [_event(i, minutes_ago=80 - i) for i in range(50)]
        cfg = context_budget.BudgetConfig(
            hot_max_events=12, hot_min_events=8, min_summary_events=8,
            hot_token_budget=400,
        )
        pack = context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo',
            now=NOW, config=cfg, include_recall=False)
        self.assertTrue(pack.spill_event_ids)
        summaries = context_layer.list_rolling_summaries('u', 'gojo')
        self.assertTrue(summaries)
        self.assertTrue(summaries[0]['source_event_ids'])
        self.assertIn('滚动摘要', pack.summary_prompt_text)
        self.assertEqual(len(events), 50)

    def test_pin_survives_100_later_messages(self):
        context_layer.upsert_pin(
            'u', 'gojo',
            'Database / Memory v1 已本地 commit 9c2dda7，尚未 push。',
            pin_type='decision', source_event_ids=['e-decision'],
            priority=95, pin_id='pin-decision',
        )
        events = [_event(i, minutes_ago=100 - i) for i in range(100)]
        pack = context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo',
            now=NOW, include_recall=False)
        self.assertIn('9c2dda7', pack.pinned_prompt_text)
        self.assertNotIn('e-decision', pack.recent_event_ids)

    def test_resolved_pin_stays_out_of_prompt(self):
        context_layer.upsert_pin(
            'u', 'gojo', '等 Cursor 改完再 push',
            source_event_ids=['e-wait'], pin_id='pin-wait')
        context_layer.resolve_pin('pin-wait')
        pack = context_layer.assemble_from_events(
            [_event(1)], user_id='u', character_id='gojo',
            now=NOW, include_recall=False)
        self.assertNotIn('再 push', pack.pinned_prompt_text)

    def test_deleted_unique_source_drops_summary_and_pin(self):
        context_layer.upsert_pin(
            'u', 'gojo', '唯一来源的决定',
            source_event_ids=['gone-1'], pin_id='pin-gone')
        context_layer.save_rolling_summary(
            'u', 'gojo', '唯一来源的摘要', ['gone-1'],
            summary_id='sum-gone')
        pack = context_layer.assemble_from_events(
            [_event(1)], user_id='u', character_id='gojo',
            now=NOW, include_recall=False, deleted_ids={'gone-1'})
        self.assertNotIn('唯一来源的决定', pack.pinned_prompt_text)
        self.assertNotIn('唯一来源的摘要', pack.summary_prompt_text)

    def test_source_validity_error_is_fail_closed(self):
        def boom(*_a, **_k):
            raise raw_events.SourceValidityError('db down')

        with patch.object(raw_events, 'deleted_event_ids', side_effect=boom):
            pack = context_layer.build_chat_context('u', 'gojo', include_recall=False)
        self.assertTrue(pack.failed_closed)
        self.assertEqual(pack.messages, [])
        self.assertEqual(pack.pinned_prompt_text, '')

    def test_recent_event_ids_exclude_recall_duplicates(self):
        recent = ['hot-1', 'hot-2']
        recall = {
            'facts': [
                {'id': 1, 'content': '重复的热窗口事实',
                 'source_event_ids': ['hot-1'], 'bonds': []},
                {'id': 2, 'content': '更早的独立事实',
                 'source_event_ids': ['old-9'], 'bonds': []},
            ],
            'loose_bonds': [],
            'tolds': [],
        }
        filtered = context_layer.exclude_recall_covered_by_recent(recall, recent)
        contents = [row['content'] for row in filtered['facts']]
        self.assertNotIn('重复的热窗口事实', contents)
        self.assertIn('更早的独立事实', contents)
        self.assertEqual(set(filtered['exclude_event_ids']), set(recent))

    def test_two_level_recall_accepts_exclude_event_ids(self):
        src = Path(BACKEND, 'smart_recall.py').read_text(encoding='utf-8')
        self.assertIn('exclude_event_ids=None', src)
        self.assertIn("'exclude_event_ids'", src)

    def test_compatibility_get_short_memory_still_caps_at_40(self):
        self.assertEqual(user_memory._short_limit(200), user_memory.SHORT_MEMORY_MAX)
        self.assertEqual(user_memory.SHORT_MEMORY_MAX, 40)
        src = Path(BACKEND, 'raw_events.py').read_text(encoding='utf-8')
        self.assertIn('min(int(n or 40), 40)', src)
        self.assertIn('def get_hot_candidate_events', src)

    def test_main_routes_call_context_layer(self):
        chat = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        image = Path(BACKEND, 'route_image.py').read_text(encoding='utf-8')
        voice = Path(BACKEND, 'route_voice_stream.py').read_text(encoding='utf-8')
        prompt = Path(BACKEND, 'prompt.py').read_text(encoding='utf-8')
        self.assertIn('def _turn_context', chat)
        self.assertIn("profile='text'", chat)
        self.assertIn('short_memories = get_short_memory', chat)
        self.assertIn("profile='story'", chat)
        self.assertIn("profile='voice'", chat)
        self.assertIn("profile='proactive'", chat)
        self.assertIn('context_pack=pack', chat)
        self.assertIn("profile='image'", image)
        self.assertIn('context_pack=pack', image)
        self.assertIn("profile='voice'", voice)
        self.assertIn('context_pack=pack', voice)
        self.assertIn('context_pack=None', prompt)
        self.assertIn('pinned_prompt_text', prompt)


if __name__ == '__main__':
    unittest.main()
