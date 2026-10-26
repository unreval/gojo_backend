"""A soft-busy time request is seen before its clock, and still gets no reply now."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
TESTS = os.path.dirname(__file__)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import db_schedule
import prompt
import reply_availability
import test_schedule_reply_state as sched_tests

FakeConn = sched_tests.FakeConn
PhoneCheckStore = sched_tests.PhoneCheckStore
canonical_test_world = sched_tests.canonical_test_world

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 25, 22, 46, tzinfo=CN)
SAMPLED = datetime(2026, 10, 26, 7, 30, tzinfo=CN)
DEADLINE = datetime(2026, 10, 26, 6, 0, tzinfo=CN)
WAKE = '明天记得六点叫我起床去考试！听到了吗！'
ACTIVITY = {
    'id': 7,
    'start_time': '22:00',
    'end_time': '23:50',
    'title': '深夜事务处理',
    'reply_state': 'soft_busy',
    'can_reply': False,
    'character_id': 'gojo',
}


class TimeRequestParseTests(unittest.TestCase):
    def test_wake_up_request_is_tomorrow_morning(self):
        self.assertEqual(
            reply_availability.time_request_deadline(WAKE, NOW), DEADLINE)

    def test_clock_without_a_request_does_not_move_the_check(self):
        self.assertIsNone(reply_availability.time_request_deadline(
            '明天六点考试，好紧张', NOW))

    def test_past_clock_without_tomorrow_is_not_pushed_forward(self):
        self.assertIsNone(reply_availability.time_request_deadline(
            '记得六点叫我起床', NOW))

    def test_same_morning_clock_stays_today(self):
        five = datetime(2026, 10, 26, 5, 0, tzinfo=CN)
        self.assertEqual(
            reply_availability.time_request_deadline('记得六点叫我起床', five),
            DEADLINE)

    def test_evening_clock_keeps_the_half_hour(self):
        afternoon = datetime(2026, 10, 26, 15, 0, tzinfo=CN)
        self.assertEqual(
            reply_availability.time_request_deadline('晚上十点半记得提醒我', afternoon),
            datetime(2026, 10, 26, 22, 30, tzinfo=CN))


class SoftBusyTimeRequestTests(unittest.TestCase):
    def decide(self, text, *, sampled=SAMPLED, reply_state='soft_busy'):
        store = PhoneCheckStore()
        activity = dict(ACTIVITY, reply_state=reply_state, can_reply=False)
        world = canonical_test_world(activity, NOW, reply_state=reply_state)
        with patch.object(db_schedule, 'get_conn', lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=sampled), \
             patch.object(db_schedule, 'get_current_world_state',
                          return_value=world), \
             patch.object(db_schedule, 'get_next_free_time', return_value=None), \
             patch('db_promise.add_promise') as add_promise:
            decision = reply_availability.check_reply_availability(
                'gojo', 'u', source_event_id='evt-1', pending_text=text, now=NOW)
        row = list(store.rows.values())[0]
        return decision, row, add_promise, store

    def test_future_request_pulls_the_check_and_does_not_reply(self):
        decision, row, add_promise, _store = self.decide(WAKE)
        self.assertFalse(decision['can_reply'])
        self.assertEqual(decision['reply_state'], 'soft_busy')
        self.assertEqual(decision['next_phone_check_at'], DEADLINE)
        self.assertEqual(row['next_phone_check_at'], DEADLINE)
        self.assertIn('time_request', row['event_meta'])
        self.assertIn(DEADLINE.isoformat(), row['event_meta'])
        add_promise.assert_not_called()

    def test_ordinary_message_keeps_the_sampled_check(self):
        decision, row, add_promise, _store = self.decide('在忙，先这样')
        self.assertFalse(decision['can_reply'])
        self.assertEqual(row['next_phone_check_at'], SAMPLED)
        self.assertNotIn('time_request', row['event_meta'] or '')
        add_promise.assert_not_called()

    def test_check_already_earlier_than_the_deadline_is_not_delayed(self):
        earlier = datetime(2026, 10, 26, 5, 0, tzinfo=CN)
        _decision, row, _add, _store = self.decide(WAKE, sampled=earlier)
        self.assertEqual(row['next_phone_check_at'], earlier)

    def test_follow_up_request_only_moves_an_existing_check_earlier(self):
        store = PhoneCheckStore()
        world = canonical_test_world(ACTIVITY, NOW)
        with patch.object(db_schedule, 'get_conn', lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=SAMPLED), \
             patch.object(db_schedule, 'get_current_world_state',
                          return_value=world), \
             patch.object(db_schedule, 'get_next_free_time', return_value=None), \
             patch('db_promise.add_promise') as add_promise:
            first = reply_availability.check_reply_availability(
                'gojo', 'u', source_event_id='evt-1', pending_text='先记一下', now=NOW)
            second = reply_availability.check_reply_availability(
                'gojo', 'u', source_event_id='evt-2', pending_text=WAKE, now=NOW)
        row = list(store.rows.values())[0]
        self.assertFalse(first['can_reply'])
        self.assertFalse(second['can_reply'])
        self.assertEqual(first['next_phone_check_at'], SAMPLED)
        self.assertEqual(second['next_phone_check_at'], DEADLINE)
        self.assertEqual(row['pending_count'], 2)
        self.assertEqual(row['next_phone_check_at'], DEADLINE)
        add_promise.assert_not_called()

    def test_hard_busy_does_not_look_at_the_phone_early(self):
        decision, row, add_promise, _store = self.decide(WAKE, reply_state='hard_busy')
        self.assertFalse(decision['can_reply'])
        self.assertEqual(decision['reply_state'], 'hard_busy')
        self.assertIsNone(row['next_phone_check_at'])
        self.assertNotIn('time_request', row['event_meta'] or '')
        add_promise.assert_not_called()


class ConfirmationPromptTests(unittest.TestCase):
    def test_explicit_time_request_must_be_accepted_or_refused_in_words(self):
        text = prompt.OUTPUT_SPEC
        self.assertIn('她要求你明确应下', text)
        self.assertIn('必须选一种', text)
        self.assertIn('不许只字不提', text)
        self.assertIn('proactive_promise', text)
        self.assertIn('明确拒绝', text)
        self.assertIn('只出 reminder', text)


if __name__ == '__main__':
    unittest.main()
