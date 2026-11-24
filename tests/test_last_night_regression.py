"""Pin last-night and cross-midnight behavior. No live models and no database.

Clocks are fixed user-local instants. Nothing reads the system clock.
"""
import os
import sys
import unittest
from datetime import datetime, timezone

import pytest


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import context_budget  # noqa: E402
import context_layer  # noqa: E402
import route_chat  # noqa: E402


def at(month, day, hour, minute):
    """2026 wall time in the configured user timezone (CN_TZ, UTC+8)."""
    return datetime(2026, month, day, hour, minute, tzinfo=context_layer.CN_TZ)


def event(event_id, role, text, when):
    return {
        'event_id': event_id,
        'role': role,
        'content': text,
        'timestamp': when,
        'metadata': {},
        'reply_to_event_id': '',
        'kind': 'text',
    }


MORNING = at(11, 24, 9, 0)


def clock_snapshot(now=MORNING):
    return {'now_local': now, 'now_utc': now.astimezone(timezone.utc)}


class HistoryLabelTests(unittest.TestCase):
    def render(self, events, now):
        return context_layer.format_history_messages(events, now=now)

    def test_a1_previous_calendar_evening_is_yesterday_with_cross_day_gap(self):
        sent = at(11, 23, 23, 5)
        now = at(11, 24, 8, 30)
        history, current = self.render([
            event('arrival', 'user', '晚上11点才到家', sent),
            event('current', 'user', '早', now),
        ], now)
        self.assertEqual(
            history['prompt_text'],
            '【历史消息；发送=2026-11-23T23:05:00+08:00；'
            '日期关系=昨天；距本轮=9小时25分】\n晚上11点才到家')
        self.assertNotIn('日期关系=今天', history['prompt_text'])
        self.assertTrue(current['prompt_text'].startswith(
            '【对话间隔；距上一条=9小时25分；跨日=是】'))

    def test_a2_seventy_five_minutes_across_midnight_stays_yesterday(self):
        sent = at(11, 23, 23, 5)
        now = at(11, 24, 0, 20)
        history, current = self.render([
            event('arrival', 'user', '晚上11点才到家', sent),
            event('current', 'user', '还没睡', now),
        ], now)
        self.assertEqual(
            history['prompt_text'],
            '【历史消息；发送=2026-11-23T23:05:00+08:00；'
            '日期关系=昨天；距本轮=1小时15分】\n晚上11点才到家')
        self.assertNotIn('日期关系=今天', history['prompt_text'])
        self.assertTrue(current['prompt_text'].startswith(
            '【对话间隔；距上一条=1小时15分；跨日=是】'))

    def test_a3_same_calendar_evening_stays_today_without_cross_day(self):
        sent = at(11, 24, 21, 0)
        now = at(11, 24, 23, 30)
        history, current = self.render([
            event('arrival', 'user', '刚到家', sent),
            event('current', 'user', '嗯', now),
        ], now)
        self.assertEqual(
            history['prompt_text'],
            '【历史消息；发送=2026-11-24T21:00:00+08:00；'
            '日期关系=今天；距本轮=2小时30分】\n刚到家')
        self.assertNotIn('日期关系=昨天', history['prompt_text'])
        self.assertTrue(current['prompt_text'].startswith(
            '【对话间隔；距上一条=2小时30分；跨日=否】'))


class HotWindowTests(unittest.TestCase):
    def window(self, events, now):
        return context_layer.select_hot_window(
            events, config=context_budget.BudgetConfig(), now=now)

    def test_b1_default_gap_drops_last_night_after_nine_hours(self):
        now = at(11, 24, 8, 30)
        events = [
            event('n2250', 'user', '到家了', at(11, 23, 22, 50)),
            event('n2305', 'user', '晚上11点才到家', at(11, 23, 23, 5)),
            event('current', 'user', '早', now),
        ]
        hot, spill = self.window(events, now)
        self.assertEqual([row['event_id'] for row in hot], ['current'])
        self.assertEqual([row['event_id'] for row in spill], ['n2250', 'n2305'])

    def test_b2_under_six_hours_keeps_last_night_and_labels_it_yesterday(self):
        now = at(11, 24, 3, 0)
        events = [
            event('n2250', 'user', '到家了', at(11, 23, 22, 50)),
            event('n2305', 'user', '晚上11点才到家', at(11, 23, 23, 5)),
            event('current', 'user', '还醒着', now),
        ]
        hot, spill = self.window(events, now)
        self.assertEqual([row['event_id'] for row in hot], ['n2250', 'n2305', 'current'])
        self.assertEqual(spill, [])
        night = context_layer.format_history_messages(
            [row for row in hot if row['event_id'] != 'current'], now=now)
        self.assertEqual(
            night[0]['prompt_text'],
            '【历史消息；发送=2026-11-23T22:50:00+08:00；'
            '日期关系=昨天；距本轮=4小时10分】\n到家了')
        self.assertEqual(
            night[1]['prompt_text'],
            '【历史消息；发送=2026-11-23T23:05:00+08:00；'
            '日期关系=昨天；距本轮=3小时55分】\n晚上11点才到家')
        self.assertNotIn('日期关系=今天', night[0]['prompt_text'])
        self.assertNotIn('日期关系=今天', night[1]['prompt_text'])

    def test_b3_hot_max_gap_stays_360_for_default_chat_and_text(self):
        self.assertEqual(context_budget.BudgetConfig().hot_max_gap_minutes, 360)
        self.assertEqual(
            context_budget.BudgetConfig.for_profile('chat').hot_max_gap_minutes, 360)
        self.assertEqual(
            context_budget.BudgetConfig.for_profile('text').hot_max_gap_minutes, 360)


class ClockPhaseTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = clock_snapshot()

    def check(self, jp, zh):
        text = f'{jp} {zh}'.strip()
        parsed = {'messages': [{'jp': jp, 'zh': zh}]}
        return (
            route_chat._find_reply_clock_phase_conflict(text, self.snapshot),
            route_chat._reject_temporal_candidate(parsed, self.snapshot),
        )

    def test_c1_present_night_claim_conflicts_at_nine_in_the_morning(self):
        found, rejected = self.check('現在は夜だね', '现在是晚上了')
        self.assertEqual(found, 'current_clock_phase_conflict')
        self.assertEqual(rejected, 'current_clock_phase_conflict')

    def test_c2_explicit_last_night_is_not_a_present_phase_claim(self):
        found, rejected = self.check('', '昨天晚上11点才到家，辛苦了')
        self.assertIsNone(found)
        self.assertIsNone(rejected)

    @pytest.mark.xfail(
        strict=True,
        reason='known gap: 今晚 未纳入时段检查，2026-11-24',
    )
    def test_c3_tonight_mispointing_last_night_is_blocked(self):
        # Scene: the user already said 「昨天晚上11点才到家」.
        # These checkers only see the reply, which is the gap under test.
        found, rejected = self.check('', '今晚这么晚才到家啊')
        self.assertIsNotNone(found)
        self.assertIsNotNone(rejected)

    @pytest.mark.xfail(
        strict=True,
        reason='known gap: 今夜 未纳入时段检查，2026-11-24',
    )
    def test_c4_konya_mispointing_last_night_is_blocked(self):
        found, rejected = self.check('今夜は遅かったね', '')
        self.assertIsNotNone(found)
        self.assertIsNotNone(rejected)
