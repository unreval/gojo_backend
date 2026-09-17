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
        self.assertIn('exclude_event_ids', prompt)
        self.assertIn('current_event_id', chat)
        self.assertIn('append_current_user_turn', chat)
        self.assertIn('current_event_id', image)
        self.assertIn('append_current_user_turn', image)
        self.assertIn('current_event_id', voice)
        self.assertIn('append_current_user_turn', voice)
        self.assertIn("profile='voice'", voice)


class CurrentTurnAndCollapseTests(unittest.TestCase):
    def setUp(self):
        context_layer.use_memory_store(True)

    def tearDown(self):
        context_layer.use_memory_store(False)

    def _count_user_text(self, messages, text):
        count = 0
        for message in messages or []:
            if message.get('role') != 'user':
                continue
            content = message.get('content')
            if content == text:
                count += 1
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get('text') == text:
                        count += 1
                    elif block == text:
                        count += 1
        return count

    def test_current_user_turn_appears_once_when_already_in_hot(self):
        current = '本轮用户消息只应出现一次'
        events = [
            _event(1, text='昨天说过的', minutes_ago=12),
            {
                'event_id': 'cur-turn',
                'role': 'user',
                'content': current,
                'timestamp': NOW,
                'metadata': {},
                'kind': 'text',
            },
        ]
        pack = context_layer.assemble_from_events(
            events, user_id='u', character_id='gojo',
            now=NOW, include_recall=False, current_event_id='cur-turn')
        messages = context_layer.append_current_user_turn(pack.messages, current)
        self.assertEqual(self._count_user_text(messages, current), 1)
        self.assertNotIn('cur-turn', pack.recent_event_ids)

    def test_current_turn_dedup_covers_route_kinds(self):
        chat = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        image = Path(BACKEND, 'route_image.py').read_text(encoding='utf-8')
        voice = Path(BACKEND, 'route_voice_stream.py').read_text(encoding='utf-8')
        self.assertGreaterEqual(chat.count('_history_plus_current'), 5)
        self.assertIn("profile='text'", chat)
        self.assertIn("profile='story'", chat)
        self.assertIn("profile='voice'", chat)
        self.assertIn("profile='proactive'", chat)
        self.assertIn('append_current_user_turn', image)
        self.assertIn('append_current_user_turn', voice)
        self.assertIn('current_event_id=source_event_id', voice)

    def test_recall_source_exclusion_drops_fully_covered_facts(self):
        from recall_candidates import drop_recent_covered
        rows = [
            {'id': 1, 'content': '热窗口事实', 'source_event_ids': ['hot-1']},
            {'id': 2, 'content': '部分重叠', 'source_event_ids': ['hot-1', 'old-9']},
            {'id': 3, 'content': '更早的独立事实', 'source_event_ids': ['old-9']},
            {'id': 4, 'content': '无来源遗留', 'source_event_ids': []},
        ]
        kept = drop_recent_covered(rows, ['hot-1'])
        contents = [row['content'] for row in kept]
        self.assertNotIn('热窗口事实', contents)
        self.assertIn('部分重叠', contents)
        self.assertIn('更早的独立事实', contents)
        self.assertIn('无来源遗留', contents)
        partial = next(row for row in kept if row['content'] == '部分重叠')
        self.assertGreater(partial['recent_overlap_ratio'], 0)
        self.assertLess(partial['recent_overlap_ratio'], 1)
        src = Path(BACKEND, 'smart_recall.py').read_text(encoding='utf-8')
        self.assertIn('drop_recent_covered(pinned, recent_exclude)', src)
        self.assertIn('drop_recent_covered(pool, recent_exclude)', src)
        self.assertIn('drop_recent_covered(loose_bonds, recent_exclude)', src)
        self.assertIn('drop_recent_covered(tolds, recent_exclude)', src)

    def test_provenance_collapse_keeps_diary_subjective(self):
        from recall_candidates import RecallCandidate, collapse_candidates
        src_ids = ('e100', 'e101')
        fact = RecallCandidate(
            candidate_id='fact:1', candidate_type='fact',
            text='用户明天考试', source_event_ids=src_ids, priority=70)
        bond = RecallCandidate(
            candidate_id='bond:2', candidate_type='bond',
            text='用户明天考试', source_event_ids=src_ids, priority=45)
        diary = RecallCandidate(
            candidate_id='diary:3', candidate_type='diary',
            text='角色担心用户最近太累了', source_event_ids=src_ids, priority=20)
        kept = collapse_candidates([fact, bond, diary])
        exam = [row for row in kept if '明天考试' in row.text]
        self.assertEqual(len(exam), 1)
        self.assertEqual(exam[0].candidate_type, 'fact')
        subjective = [row for row in kept if row.subjective]
        self.assertEqual(len(subjective), 1)
        self.assertIn('太累', subjective[0].text)

    def test_legacy_missing_does_not_outrank_linked(self):
        from recall_candidates import RecallCandidate, collapse_candidates
        linked = RecallCandidate(
            candidate_id='fact:l', candidate_type='fact',
            text='她住在京都', source_event_ids=('e1',),
            provenance_quality='linked', relevance_score=0.2)
        legacy = RecallCandidate(
            candidate_id='fact:g', candidate_type='fact',
            text='她住在京都', source_event_ids=(),
            provenance_quality='legacy_missing', relevance_score=0.9)
        kept = collapse_candidates([legacy, linked])
        # no shared source: both may remain; legacy must not be preferred
        linked_kept = [row for row in kept if row.candidate_id == 'fact:l']
        self.assertTrue(linked_kept)
        if len(kept) == 1:
            self.assertEqual(kept[0].provenance_quality, 'linked')

    def test_dynamic_channels_are_budgeted(self):
        items = []
        kinds = [
            ('hot_raw', 'hot'),
            ('pinned', 'pin'),
            ('rolling_summary', 'sum'),
            ('recalled_memory', 'rec'),
            ('relationship_state', 'rel'),
            ('cognitive_state', 'cog'),
            ('diary', 'dia'),
            ('temporal', 'tmp'),
            ('schedule', 'sch'),
            ('character_lore', 'lor'),
            ('anti_repeat', 'anti'),
        ]
        blob = '动态上下文块' * 40
        for kind, prefix in kinds:
            for index in range(12):
                items.append(context_budget.ContextItem(
                    item_id=f'{prefix}{index}',
                    item_type=kind,
                    text=blob,
                    priority=50,
                    source_event_ids=(f'{prefix}-src-{index}',),
                    role='user' if kind == 'hot_raw' else '',
                ))
        cfg = context_budget.BudgetConfig(total_token_budget=900)
        manager = context_budget.ContextBudgetManager(cfg)
        kept = manager.allocate(items)
        used = sum(item.token_cost for item in kept)
        self.assertLessEqual(used, cfg.total_token_budget + max(
            item.token_cost for item in kept))
        self.assertLess(len(kept), len(items))
        grouped = context_budget.group_by_channel(items)
        for name in ('hot', 'pinned', 'summary', 'recall',
                     'relationship', 'cognitive', 'diary', 'aux'):
            self.assertIn(name, grouped)
            self.assertTrue(grouped[name])
        for item in kept:
            self.assertIn(item.item_type, context_budget.ITEM_TYPE_CHANNEL)


if __name__ == '__main__':
    unittest.main()
