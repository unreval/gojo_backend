# -*- coding: utf-8 -*-
import inspect
import json
import os
import sys
import types
import unittest
from collections import deque
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
TESTS = os.path.dirname(__file__)
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

import db_schedule  # noqa: E402
import delayed_reply  # noqa: E402
import proactive_scheduler  # noqa: E402
import raw_events  # noqa: E402
import reply_availability  # noqa: E402
from provider_error import ProviderHTTPError  # noqa: E402
import test_schedule_reply_state as sched_tests  # noqa: E402

FakeConn = sched_tests.FakeConn
PhoneCheckStore = sched_tests.PhoneCheckStore

NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)
ACTIVITY = {
    'id': 7,
    'start_time': '09:00',
    'end_time': '11:00',
    'title': '开会',
    'location': '会议室',
    'note': '',
    'reply_state': 'soft_busy',
    'can_reply': False,
    'character_id': 'gojo',
}


def _conn(store):
    return FakeConn(store)


def _active_source_rows(_user_id, _character_id, event_ids, **_kwargs):
    texts = {'e1': '一', 'e2': '二', 'e3': '三'}
    return [
        {'event_id': str(event_id), 'role': 'user',
         'content': texts.get(str(event_id), str(event_id)), 'kind': 'text'}
        for event_id in event_ids
    ]


def _synthetic_claim(oid):
    return {
        'id': oid, 'claim_token': 'token',
        'user_id': 'u', 'character_id': 'gojo',
        'pending_count': 1, 'pending_text': '一',
        'first_source_event_id': 'e1', 'last_source_event_id': 'e1',
    }


def _seed_soft_busy_bundle(store, texts, *, due=None, event_metas=None):
    due = due or (NOW - timedelta(minutes=1))
    start = due - timedelta(minutes=20)
    with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
         patch.object(db_schedule, 'sample_next_phone_check_at', return_value=due), \
         patch.object(db_schedule, 'postpone_past_hard_busy',
                      side_effect=lambda *a, **k: (a[2] if len(a) > 2 else due, False)):
        first_meta = dict((event_metas or [None] * len(texts))[0] or {})
        first_meta.setdefault('source_event_id', 'e1')
        first = db_schedule.decide_phone_check(
            'gojo', 'u', start, ACTIVITY,
            source_event_id='e1', pending_text=texts[0], event_meta=first_meta)
        oid = first['opportunity_id']
        for index, text in enumerate(texts[1:], start=2):
            meta = {}
            if event_metas and index - 1 < len(event_metas):
                meta = dict(event_metas[index - 1] or {})
            meta.setdefault('source_event_id', f'e{index}')
            db_schedule.decide_phone_check(
                'gojo', 'u', start + timedelta(minutes=index), ACTIVITY,
                source_event_id=f'e{index}', pending_text=text, event_meta=meta)
    row = list(store.rows.values())[0]
    row['next_phone_check_at'] = NOW
    row['check_state'] = 'pending'
    row['resolved_at'] = None
    store.schedule_rows = [{
        'id': 7, 'character_id': 'gojo', 'user_id': 'u',
        'sched_date': NOW.date(), 'start_time': '09:00', 'end_time': '11:00',
        'title': '开会', 'can_reply': False, 'reply_state': 'soft_busy',
    }]
    return oid, row


class HelpersStub:
    def __init__(self, bubbles=None, fail=False, history=None):
        self.fail = fail
        self.relationship_calls = 0
        self.generate_calls = 0
        self.context_calls = 0
        self.offline_calls = 0
        self.user_message = None
        self.current_event_id = None
        self.auto_pin_enabled = None
        self.bubbles = bubbles or [
            {'jp': '今見た', 'zh': '刚才看到了'},
            {'jp': 'ちょっと待ってた', 'zh': '让你等了一下'},
        ]
        self.history = history or []
        self.extra_messages = None

    def _turn_context(self, user_id, character_id, user_message='', profile='default',
                      current_event_id=None, auto_pin_enabled=True):
        self.context_calls += 1
        self.user_message = user_message
        self.current_event_id = current_event_id
        self.auto_pin_enabled = auto_pin_enabled
        skip = current_event_id or []
        if isinstance(skip, (str, int)):
            skip = [skip]
        skip = {str(item).strip() for item in skip if str(item).strip()}
        messages = [
            item for item in self.history
            if str(item.get('event_id') or '').strip() not in skip
        ]
        pack = types.SimpleNamespace(
            messages=list(messages), failed_closed=False, memory_text='')
        return pack, list(messages)

    def _history_plus_current(self, messages, content):
        out = list(messages or [])
        out.append({'role': 'user', 'content': content})
        self.extra_messages = out
        return out

    def _generate_or_none(self, *args, **kwargs):
        self.generate_calls += 1
        self.system_blocks = args[2]
        self.messages = args[3]
        if self.fail:
            return None, None
        return {'messages': self.bubbles, 'emotion': '平静'}, {'mood': 'ok'}

    def _finalize_committed(self, result, min_messages=1):
        if not result:
            return None, None
        msgs = result.get('messages') or []
        if len(msgs) < min_messages:
            return None, None
        return '平静', msgs

    def _commit_offline_state(self, *args, **kwargs):
        self.offline_calls += 1

    def _start_relationship_update(self, *args, **kwargs):
        self.relationship_calls += 1


class DelayedReplyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=0.0)
        self._io_patches = [
            patch.object(delayed_reply.time, 'monotonic', self.clock),
            patch.object(delayed_reply, '_pause_until', 0.0),
            patch.object(delayed_reply, '_generation_calls', deque()),
            patch.object(delayed_reply, 'DELAYED_REPLY_ENABLED', True),
            patch.object(delayed_reply, 'DELAYED_REPLY_MAX_PER_TICK', 1),
            patch.object(delayed_reply, 'DELAYED_REPLY_MAX_PER_HOUR', 20),
            patch('behavior_evidence.record_reply_cycle', Mock()),
            patch('push_notify.push_to_user', Mock()),
            patch.object(delayed_reply, 'assistant_already_committed',
                          return_value=False),
            patch.object(db_schedule, 'get_conn',
                         side_effect=AssertionError('unexpected database access')),
            patch.object(raw_events, 'get_active_events_by_ids',
                         side_effect=_active_source_rows),
            patch.object(
                db_schedule, 'get_current_world_state',
                side_effect=lambda character_id, user_id, now=None:
                sched_tests.canonical_test_world(ACTIVITY, now or NOW)),
        ]
        for item in self._io_patches:
            item.start()
            self.addCleanup(item.stop)

    def test_disabled_worker_never_lists_claims_or_generates(self):
        generate = Mock()
        with patch.object(delayed_reply, 'DELAYED_REPLY_ENABLED', False), \
             patch.object(db_schedule, 'iter_due_phone_checks') as due:
            self.assertEqual(delayed_reply.process_due_phone_checks(NOW, generate_fn=generate), [])
        due.assert_not_called()
        generate.assert_not_called()

        def sleep(seconds):
            if seconds == delayed_reply.TICK_SECONDS:
                delayed_reply._stop = True

        with patch.object(delayed_reply, 'DELAYED_REPLY_ENABLED', False), \
             patch.object(delayed_reply, '_stop', False), \
             patch.object(delayed_reply.time, 'sleep', side_effect=sleep), \
             patch.object(delayed_reply, 'cancel_orphan_busy_promises'), \
             patch.object(delayed_reply, 'process_due_phone_checks') as process:
            delayed_reply._loop()
        process.assert_not_called()

    def test_tick_limit_bounds_listing_and_generation(self):
        evaluate = Mock(side_effect=lambda oid, now: {
            'action': 'reply', 'claimed': _synthetic_claim(oid)})
        generate = Mock(return_value={'ok': True})
        with patch.object(db_schedule, 'iter_due_phone_checks', return_value=[1, 2, 3]) as due, \
             patch.object(db_schedule, 'complete_delayed_reply', return_value=1), \
             patch.object(db_schedule, 'abort_delayed_reply',
                          side_effect=AssertionError('unexpected abort')):
            result = delayed_reply.process_due_phone_checks(
                NOW, evaluate_fn=evaluate, generate_fn=generate)
        due.assert_called_once_with(NOW, limit=1)
        self.assertEqual(len(result), 1)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(evaluate.call_count, 1)

    def test_hourly_limit_counts_failures_and_expires_at_one_hour(self):
        generate = Mock(return_value={'ok': False, 'reason': 'generation_failed'})
        evaluate = Mock(return_value={
            'action': 'reply', 'claimed': _synthetic_claim(1)})
        with patch.object(db_schedule, 'iter_due_phone_checks', return_value=[1]) as due, \
             patch.object(db_schedule, 'abort_delayed_reply', return_value=1):
            for _ in range(20):
                delayed_reply.process_due_phone_checks(NOW, evaluate_fn=evaluate, generate_fn=generate)
            self.clock.return_value = 3599.0
            self.assertEqual(delayed_reply.process_due_phone_checks(
                NOW, evaluate_fn=evaluate, generate_fn=generate), [])
            self.assertEqual(due.call_count, 20)
            self.assertEqual(generate.call_count, 20)
            self.clock.return_value = 3600.0
            delayed_reply.process_due_phone_checks(NOW, evaluate_fn=evaluate, generate_fn=generate)
        self.assertEqual(generate.call_count, 21)
        self.assertEqual(len(delayed_reply._generation_calls), 1)

    def test_provider_circuit_stops_current_tick_and_all_rows_for_ten_minutes(self):
        for reason in ('provider_retryable', 'provider_auth_failed'):
            with self.subTest(reason=reason), \
                 patch.object(delayed_reply, '_pause_until', 0.0), \
                 patch.object(delayed_reply, 'DELAYED_REPLY_MAX_PER_TICK', 3), \
                 patch.object(db_schedule, 'iter_due_phone_checks', return_value=[1, 2]) as due, \
                 patch.object(db_schedule, 'abort_delayed_reply', return_value=1) as abort:
                self.clock.return_value = 0.0
                generate = Mock(return_value={'ok': False, 'reason': reason})
                evaluate = Mock(side_effect=lambda oid, now: {
                    'action': 'reply', 'claimed': _synthetic_claim(oid)})
                delayed_reply.process_due_phone_checks(NOW, evaluate_fn=evaluate, generate_fn=generate)
                self.assertEqual(generate.call_count, 1)
                self.assertEqual(evaluate.call_count, 1)
                self.assertEqual(abort.call_args.kwargs['reason'], reason)
                self.assertEqual(delayed_reply._pause_until, 600.0)
                self.clock.return_value = 599.0
                self.assertEqual(delayed_reply.process_due_phone_checks(
                    NOW, evaluate_fn=evaluate, generate_fn=generate), [])
                self.assertEqual(due.call_count, 1)
                self.clock.return_value = 600.0
                delayed_reply.process_due_phone_checks(NOW, evaluate_fn=evaluate, generate_fn=generate)
                self.assertEqual(generate.call_count, 2)

    def test_failed_abort_still_opens_circuit_and_does_not_invent_failure_count(self):
        claimed = dict(_synthetic_claim(1), fail_count=2)
        for abort_result in (0, RuntimeError('db unavailable')):
            with self.subTest(abort=abort_result), \
                 patch.object(delayed_reply, '_pause_until', 0.0), \
                 patch.object(db_schedule, 'iter_due_phone_checks', return_value=[1]), \
                 patch.object(db_schedule, 'log_phone_check_action') as logged, \
                 patch.object(db_schedule, 'abort_delayed_reply',
                              side_effect=abort_result if isinstance(abort_result, Exception) else None,
                              return_value=abort_result):
                delayed_reply.process_due_phone_checks(
                    NOW, evaluate_fn=lambda *a: {'action': 'reply', 'claimed': claimed},
                    generate_fn=lambda bundle: {'ok': False, 'reason': 'provider_retryable'})
                self.assertEqual(delayed_reply._pause_until, 600.0)
                logged.assert_called_once_with('generation_failed', claimed, reason='provider_retryable')

    def test_repeated_generation_failure_expires_after_five_calls(self):
        store = PhoneCheckStore()
        oid, row = _seed_soft_busy_bundle(store, ['在吗'])
        row['end_time'] = '09:00'
        generate = Mock(return_value={'ok': False, 'reason': 'generation_failed'})
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'get_current_world_state', return_value={
                 'event': None, 'phase': None, 'activity': None,
                 'availability': {'reply_state': 'free'}}):
            now = NOW
            for count in range(1, 6):
                result = delayed_reply.process_due_phone_checks(now, generate_fn=generate)
                self.assertEqual(result[0]['reason'], 'generation_failed')
                self.assertEqual(row['fail_count'], count)
                if count < 5:
                    self.assertEqual(db_schedule.iter_due_phone_checks(
                        now + timedelta(seconds=30)), [])
                    now = row['retry_not_before']
                    self.clock.return_value = (now - NOW).total_seconds()
            self.assertEqual(delayed_reply.process_due_phone_checks(
                now + timedelta(days=1), generate_fn=generate), [])
        self.assertEqual(generate.call_count, 5)
        self.assertEqual(row['check_state'], 'expired')
        self.assertEqual(row['pending_text'], '在吗')
        self.assertEqual(row['pending_count'], 1)

    def test_no_active_pending_sources_expires_without_generation(self):
        store = PhoneCheckStore()
        _oid, row = _seed_soft_busy_bundle(store, ['一', '二'])
        generate = Mock(return_value={'ok': True})
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule.random, 'random', return_value=0.0), \
             patch.object(raw_events, 'get_active_events_by_ids', return_value=[]) as active:
            first = delayed_reply.process_due_phone_checks(NOW, generate_fn=generate)
            second = delayed_reply.process_due_phone_checks(
                NOW + timedelta(days=1), generate_fn=generate)
        self.assertEqual(first[0]['reason'], 'source_invalid')
        self.assertEqual(second, [])
        active.assert_called_once_with('u', 'gojo', ['e1', 'e2'])
        generate.assert_not_called()
        self.assertEqual(row['check_state'], 'expired')
        self.assertEqual(row['last_fail_reason'], 'source_invalid')
        self.assertEqual(row['fail_count'], 1)
        self.assertIsNotNone(row['resolved_at'])

    def test_partial_active_sources_use_canonical_text_in_one_generation(self):
        store = PhoneCheckStore()
        _oid, row = _seed_soft_busy_bundle(
            store, ['截断说明', '已删除', '第三条旧副本'],
            event_metas=[
                {'kind': 'image', 'source_event_id': 'e1',
                 'caption': '媒体副本说明',
                 'visual_summary': '蓝色杯子'},
                {'kind': 'text', 'source_event_id': 'e2'},
                {'kind': 'text', 'source_event_id': 'e3'},
            ])
        canonical = [
            {'event_id': 'e1', 'role': 'user', 'content': '图片完整原文',
             'kind': 'image'},
            {'event_id': 'e3', 'role': 'user', 'content': '第三条完整原文',
             'kind': 'text'},
        ]
        generate = Mock(return_value={'ok': True})
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule.random, 'random', return_value=0.0), \
             patch.object(raw_events, 'get_active_events_by_ids',
                          return_value=canonical):
            result = delayed_reply.process_due_phone_checks(
                NOW, generate_fn=generate)
        self.assertEqual(result[0]['action'], 'replied')
        generate.assert_called_once()
        checked = generate.call_args.args[0]
        self.assertEqual(checked['pending_count'], 2)
        self.assertEqual(checked['pending_text'], '图片完整原文\n第三条完整原文')
        self.assertEqual(checked['first_source_event_id'], 'e1')
        self.assertEqual(checked['last_source_event_id'], 'e3')
        metas = reply_availability.parse_pending_event_meta(checked['event_meta'])
        self.assertEqual([meta['source_event_id'] for meta in metas], ['e1', 'e3'])
        self.assertEqual([meta['text'] for meta in metas],
                         ['图片完整原文', '第三条完整原文'])
        self.assertEqual(metas[0]['visual_summary'], '蓝色杯子')
        self.assertEqual(row['check_state'], 'consumed')

    def test_source_validity_error_expires_after_one_paid_attempt(self):
        store = PhoneCheckStore()
        _oid, row = _seed_soft_busy_bundle(store, ['在吗'])
        message = 'canonical source invalid ' + 'x' * 340

        def generate(_bundle):
            raise raw_events.SourceValidityError(message)

        generate_fn = Mock(side_effect=generate)
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule.random, 'random', return_value=0.0), \
             patch('builtins.print') as logged:
            first = delayed_reply.process_due_phone_checks(
                NOW, generate_fn=generate_fn)
            second = delayed_reply.process_due_phone_checks(
                NOW + timedelta(days=1), generate_fn=generate_fn)
        self.assertEqual(first[0]['reason'], 'source_invalid')
        self.assertEqual(second, [])
        self.assertEqual(generate_fn.call_count, 1)
        self.assertEqual(row['check_state'], 'expired')
        self.assertEqual(row['last_fail_reason'], 'source_invalid')
        self.assertEqual(row['fail_count'], 1)
        self.assertIsNone(row['retry_not_before'])
        failure_logs = [
            str(call.args[0]) for call in logged.call_args_list
            if call.args and '[delayed_reply] generate #' in str(call.args[0])
        ]
        self.assertEqual(len(failure_logs), 1)
        self.assertIn(message[:300], failure_logs[0])
        self.assertNotIn(message[:301], failure_logs[0])
        self.assertRegex(failure_logs[0], r'test_delayed_reply\.py:\d+')

    def test_soft_busy_three_messages_share_one_pending_bundle(self):
        store = PhoneCheckStore()
        oid, row = _seed_soft_busy_bundle(store, ['一', '二', '三'])
        self.assertEqual(len(store.rows), 1)
        self.assertEqual(row['pending_count'], 3)
        self.assertIn('一', row['pending_text'])
        self.assertIn('二', row['pending_text'])
        self.assertIn('三', row['pending_text'])
        self.assertEqual(row['first_source_event_id'], 'e1')
        self.assertEqual(row['last_source_event_id'], 'e3')
        self.assertEqual(oid, row['id'])

    def test_phone_check_due_enters_normal_chat_generator(self):
        store = PhoneCheckStore()
        oid, _row = _seed_soft_busy_bundle(store, ['一', '二', '三'])
        helpers = HelpersStub()
        captured = {}

        def generate_fn(bundle):
            captured['bundle'] = bundle
            return delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=helpers)

        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2] if len(a) > 2 else NOW, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.0), \
             patch('characters.get_character',
                   return_value={'name': '五条', 'voice_id': 'v'}), \
             patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch('user_memory.get_short_memory', return_value=[]), \
             patch('user_memory.commit_visible_assistant_message') as commit, \
             patch('user_memory.update_chat_days', return_value=1), \
             patch('memory_jobs.enqueue_private_extraction'), \
             patch('temporal_awareness.get_temporal_snapshot',
                   return_value={'now_utc': NOW}), \
             patch('temporal_awareness.record_turn'), \
             patch('prompt.build_system_blocks',
                   side_effect=lambda *a, extra_suffix='', **k: captured.update(
                       extra_suffix=extra_suffix) or []), \
             patch('tts.tts_to_b64', return_value='audio'), \
             patch('proactive_msg.add_proactive_msg',
                   side_effect=lambda *a, **k: (len(captured.setdefault('mids', [])) + 1, NOW)):
            results = delayed_reply.process_due_phone_checks(
                NOW, generate_fn=generate_fn)
        self.assertEqual(results[0]['action'], 'replied')
        self.assertEqual(helpers.context_calls, 1)
        self.assertEqual(helpers.generate_calls, 1)
        self.assertEqual(len(captured['bundle']['pending_text'].splitlines()), 3)
        self.assertIn('一', captured['extra_suffix'])
        self.assertIn('二', captured['extra_suffix'])
        self.assertIn('三', captured['extra_suffix'])
        self.assertIn('内部上下文', captured['extra_suffix'])
        self.assertEqual(commit.call_count, 2)
        self.assertEqual(helpers.relationship_calls, 0)
        row = list(store.rows.values())[0]
        self.assertEqual(row['check_state'], 'consumed')
        self.assertIsNotNone(row['resolved_at'])

    def test_delayed_reply_keeps_multi_bubble_output(self):
        helpers = HelpersStub(bubbles=[
            {'jp': 'a', 'zh': 'A'},
            {'jp': 'b', 'zh': 'B'},
            {'jp': 'c', 'zh': 'C'},
        ])
        bundle = {
            'id': 9, 'user_id': 'u', 'character_id': 'gojo',
            'pending_text': '一\n二\n三', 'pending_count': 3,
            'event_meta': '', 'last_source_event_id': 'e3',
            'reply_state': 'soft_busy',
        }
        with patch('characters.get_character',
                   return_value={'name': '五条', 'voice_id': 'v'}), \
             patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch('user_memory.get_short_memory', return_value=[]), \
             patch('user_memory.commit_visible_assistant_message'), \
             patch('user_memory.update_chat_days', return_value=1), \
             patch('memory_jobs.enqueue_private_extraction'), \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('temporal_awareness.record_turn'), \
             patch('prompt.build_system_blocks', return_value=[]), \
             patch('tts.tts_to_b64', return_value='audio'), \
             patch('proactive_msg.add_proactive_msg',
                   side_effect=lambda *a, **k: (1, NOW)):
            result = delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=helpers)
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['messages']), 3)
        for msg in result['messages']:
            self.assertIn('jp', msg)
            self.assertIn('zh', msg)
            self.assertEqual(msg['audio_b64'], 'audio')

    def test_busy_path_does_not_create_reply_promise(self):
        src = Path(BACKEND, 'reply_availability.py').read_text(encoding='utf-8')
        self.assertNotIn('add_promise', src)
        self.assertNotIn('ensure_busy_fallback', src)
        delayed_src = Path(BACKEND, 'delayed_reply.py').read_text(encoding='utf-8')
        self.assertNotIn('add_promise', delayed_src)
        self.assertNotIn('generate_from_promise', delayed_src)

    def test_proactive_scheduler_skips_phone_check_fallback(self):
        fired = []

        def mark_fired(pid, now):
            fired.append(pid)

        with patch.object(proactive_scheduler, 'db_promise',
                          types.SimpleNamespace(mark_fired=mark_fired)), \
             patch('characters.get_character') as get_char:
            result = proactive_scheduler.generate_from_promise({
                'id': 44,
                'character_id': 'gojo',
                'user_id': 'u',
                'context': 'busy fallback phone_check_id=12',
                'trigger_kind': 'busy_fallback',
                'origin_text': '',
            }, NOW)
        self.assertIsNone(result)
        self.assertEqual(fired, [44])
        get_char.assert_not_called()
        source = inspect.getsource(proactive_scheduler.generate_from_promise)
        self.assertIn("phone_check_id=", source)
        self.assertNotIn('resolve_phone_check', inspect.getsource(proactive_scheduler))

    def test_generation_failure_does_not_resolve_pending(self):
        store = PhoneCheckStore()
        oid, row = _seed_soft_busy_bundle(store, ['一', '二'])
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2] if len(a) > 2 else NOW, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.0):
            results = delayed_reply.process_due_phone_checks(
                NOW, generate_fn=lambda bundle: {'ok': False, 'reason': 'generation_failed'})
            self.assertEqual(delayed_reply.process_due_phone_checks(
                NOW + timedelta(seconds=30), generate_fn=Mock()), [])
        self.assertEqual(results[0]['action'], 'failed')
        self.assertEqual(row['check_state'], 'pending')
        self.assertIsNone(row.get('resolved_at'))
        self.assertEqual(row['pending_count'], 2)
        self.assertIn('一', row['pending_text'])
        self.assertEqual(row['fail_count'], 1)
        self.assertEqual(row['retry_not_before'], NOW + timedelta(minutes=2))

    def test_auth_failure_defers_phone_check_without_losing_inbox(self):
        store = PhoneCheckStore()
        oid, row = _seed_soft_busy_bundle(store, ['一', '二'])
        calls = []

        def generate(bundle):
            calls.append(bundle['id'])
            return {'ok': len(calls) == 2,
                    'reason': 'provider_auth_failed' if len(calls) == 1 else None}

        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2] if len(a) > 2 else NOW, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.0):
            first = delayed_reply.process_due_phone_checks(NOW, generate_fn=generate)
            self.assertEqual(first[0]['action'], 'failed')
            self.assertEqual(row['check_state'], 'deferred')
            self.assertEqual(row['next_phone_check_at'], NOW + timedelta(minutes=15))
            self.assertIsNone(row['resolved_at'])
            self.assertEqual(row['pending_count'], 2)
            self.assertIn('一', row['pending_text'])
            self.assertEqual(db_schedule.abort_delayed_reply(oid, 'wrong-token'), 0)
            self.assertEqual(delayed_reply.process_due_phone_checks(
                NOW + timedelta(seconds=30), generate_fn=generate), [])
            self.assertEqual(calls, [oid])
            self.clock.return_value = 900.0
            second = delayed_reply.process_due_phone_checks(
                NOW + timedelta(minutes=15), generate_fn=generate)
        self.assertEqual(second[0]['action'], 'replied')
        self.assertEqual(calls, [oid, oid])
        self.assertEqual(row['check_state'], 'consumed')

    def test_transient_provider_failure_defers_without_duplicate_generation(self):
        for error, delay in (
                (ProviderHTTPError(429, provider='anthropic', model='claude-test'), 120),
                (ProviderHTTPError(503, provider='anthropic', model='claude-test'), 60),
                (TimeoutError(), 60)):
            with self.subTest(error=type(error).__name__):
                self.clock.return_value = 0.0
                store = PhoneCheckStore()
                oid, row = _seed_soft_busy_bundle(store, ['在吗'])
                calls = []

                def generate(_bundle):
                    calls.append(oid)
                    if len(calls) == 1:
                        raise error
                    return {'ok': True}

                with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
                     patch.object(delayed_reply, '_pause_until', 0.0), \
                     patch.object(db_schedule, 'postpone_past_hard_busy',
                                  side_effect=lambda *a, **k: (a[2] if len(a) > 2 else NOW, False)), \
                     patch('activity_phone.profile_for_activity', return_value=None), \
                     patch.object(db_schedule.random, 'random', return_value=0.0):
                    first = delayed_reply.process_due_phone_checks(NOW, generate_fn=generate)
                    self.assertEqual(first[0]['reason'], 'provider_retryable')
                    self.assertEqual(row['check_state'], 'deferred')
                    self.assertEqual(row['next_phone_check_at'], NOW + timedelta(seconds=max(delay, 120)))
                    self.assertEqual(delayed_reply.process_due_phone_checks(
                        NOW + timedelta(seconds=delay - 1), generate_fn=generate), [])
                    self.assertEqual(calls, [oid])
                    self.clock.return_value = 600.0
                    second = delayed_reply.process_due_phone_checks(
                        NOW + timedelta(minutes=10), generate_fn=generate)
                self.assertEqual(second[0]['action'], 'replied')
                self.assertEqual(row['check_state'], 'consumed')
                self.assertEqual(calls, [oid, oid])

    def test_delayed_generation_marks_only_provider_auth_failure_for_defer(self):
        class AuthHelpers(HelpersStub):
            def _generate_or_none(self, *args, **kwargs):
                self.generation_attempts = kwargs['attempts']
                kwargs['error_out']['provider_auth_failed'] = True
                return None, None

        bundle = {'id': 9, 'user_id': 'u', 'character_id': 'gojo',
                  'pending_text': '在吗', 'pending_count': 1, 'event_meta': ''}
        helpers = AuthHelpers()
        with patch('characters.get_character', return_value={'name': '五条'}), \
             patch.object(delayed_reply, 'assistant_already_committed', return_value=False), \
             patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('prompt.build_system_blocks', return_value=[]):
            result = delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=helpers)
        self.assertEqual(result, {'ok': False, 'reason': 'provider_auth_failed'})
        self.assertEqual(helpers.generation_attempts, 1)

    def test_source_replay_failure_precedes_llm_and_schedule_commit(self):
        helpers = HelpersStub()
        bundle = {
            'id': 9, 'user_id': 'u', 'character_id': 'gojo',
            'pending_text': '来源原文', 'pending_count': 1,
            'event_meta': [{'source_event_id': 'e1', 'text': '来源原文'}],
            'first_source_event_id': 'e1', 'last_source_event_id': 'e1',
        }
        with patch('characters.get_character', return_value={'name': '五条'}), \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('prompt.build_system_blocks', return_value=[]), \
             patch('user_memory.save_user_short_memory_once',
                   side_effect=raw_events.SourceValidityError('source withdrawn')), \
             patch('schedule_transition.commit_generated_schedule_intent') as commit:
            with self.assertRaises(raw_events.SourceValidityError):
                delayed_reply.generate_delayed_chat_reply(
                    bundle, helpers=helpers)
        self.assertEqual(helpers.generate_calls, 0)
        commit.assert_not_called()

    def test_thrown_provider_auth_error_is_deferred_without_logging_body(self):
        store = PhoneCheckStore()
        _oid, row = _seed_soft_busy_bundle(store, ['在吗'])

        def denied(_bundle):
            raise ProviderHTTPError(403, provider='anthropic', model='claude-test',
                                    message='sk-FAKE-SECRET')

        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2] if len(a) > 2 else NOW, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.0), \
             patch('builtins.print') as logged:
            result = delayed_reply.process_due_phone_checks(NOW, generate_fn=denied)
        self.assertEqual(result[0]['reason'], 'provider_auth_failed')
        self.assertEqual(row['check_state'], 'deferred')
        self.assertNotIn('FAKE-SECRET', str(logged.call_args_list))

    def test_success_resolves_exactly_once(self):
        store = PhoneCheckStore()
        oid, row = _seed_soft_busy_bundle(store, ['一'])
        generates = []
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2] if len(a) > 2 else NOW, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.0):
            first = delayed_reply.process_due_phone_checks(
                NOW, generate_fn=lambda bundle: generates.append(bundle) or {'ok': True})
            second = delayed_reply.process_due_phone_checks(
                NOW, generate_fn=lambda bundle: generates.append(bundle) or {'ok': True})
        self.assertEqual(first[0]['action'], 'replied')
        self.assertTrue(first[0].get('resolved'))
        self.assertEqual(len(generates), 1)
        self.assertTrue(not second or second[0]['action'] == 'skip')
        self.assertEqual(row['check_state'], 'consumed')

    def test_image_pending_keeps_visual_metadata_in_context(self):
        meta = {
            'kind': 'image',
            'caption': '看这个',
            'visual_summary': '蓝色马克杯，杯沿有裂纹',
            'source_event_id': 'img-1',
        }
        store = PhoneCheckStore()
        _oid, row = _seed_soft_busy_bundle(
            store, ['📷 看这个'], event_metas=[meta])
        ctx = reply_availability.format_pending_bundle_context(row)
        self.assertIn('蓝色马克杯', ctx)
        self.assertIn('image', ctx)
        captured = {}
        helpers = HelpersStub()
        with patch('characters.get_character',
                   return_value={'name': '五条', 'voice_id': 'v'}), \
             patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch('user_memory.get_short_memory', return_value=[]), \
             patch('user_memory.commit_visible_assistant_message'), \
             patch('user_memory.update_chat_days', return_value=1), \
             patch('memory_jobs.enqueue_private_extraction'), \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('temporal_awareness.record_turn'), \
             patch('prompt.build_system_blocks',
                   side_effect=lambda *a, extra_suffix='', **k: captured.update(
                       extra_suffix=extra_suffix) or []), \
             patch('tts.tts_to_b64', return_value=''), \
             patch('proactive_msg.add_proactive_msg',
                   return_value=(1, NOW)):
            delayed_reply.generate_delayed_chat_reply(row, helpers=helpers)
        self.assertIn('蓝色马克杯', captured['extra_suffix'])
        self.assertIn('img-1', captured['extra_suffix'])

    def test_relationship_and_memory_commit_once_on_success(self):
        helpers = HelpersStub()
        bundle = {
            'id': 4, 'user_id': 'u', 'character_id': 'gojo',
            'pending_text': '一\n二', 'pending_count': 2,
            'event_meta': '\n'.join([
                json.dumps({'kind': 'text', 'source_event_id': 'e1'},
                           ensure_ascii=False),
                json.dumps({'kind': 'text', 'source_event_id': 'e2'},
                           ensure_ascii=False),
            ]),
            'last_source_event_id': 'e2',
            'reply_state': 'soft_busy',
        }
        with patch('characters.get_character',
                   return_value={'name': '五条', 'voice_id': 'v'}), \
             patch('user_memory.save_user_short_memory_once', return_value=True) as save_user, \
             patch('user_memory.get_short_memory', return_value=[]), \
             patch('user_memory.commit_visible_assistant_message') as commit, \
             patch('user_memory.update_chat_days', return_value=1), \
             patch('memory_jobs.enqueue_private_extraction') as jobs, \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('temporal_awareness.record_turn'), \
             patch('prompt.build_system_blocks', return_value=[]), \
             patch('tts.tts_to_b64', return_value=''), \
             patch('proactive_msg.add_proactive_msg',
                   side_effect=[(11, NOW), (12, NOW)]):
            result = delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=helpers)
        self.assertTrue(result['ok'])
        self.assertEqual(helpers.relationship_calls, 0)
        self.assertEqual(helpers.generate_calls, 1)
        jobs.assert_not_called()
        self.assertEqual(commit.call_count, 2)
        self.assertGreaterEqual(save_user.call_count, 1)

    def test_cancel_orphan_busy_promises_does_not_generate(self):
        fake = types.SimpleNamespace(
            deactivate_legacy_busy_fallbacks=lambda now: [8],
        )
        with patch.dict(sys.modules, {'db_promise': fake}):
            n = delayed_reply.cancel_orphan_busy_promises(NOW)
        self.assertEqual(n, 1)

    def test_pending_bundle_appears_once_in_prompt(self):
        pending_lines = ['积压蓝色问题A', '积压蓝色问题B', '积压蓝色问题C']
        helpers = HelpersStub(history=[
            {'role': 'user', 'content': pending_lines[0], 'event_id': 'e1'},
            {'role': 'user', 'content': pending_lines[1], 'event_id': 'e2'},
            {'role': 'user', 'content': pending_lines[2], 'event_id': 'e3'},
            {'role': 'user', 'content': '更早的话', 'event_id': 'old'},
        ])
        bundle = {
            'id': 8, 'user_id': 'u', 'character_id': 'gojo',
            'pending_text': '\n'.join(pending_lines), 'pending_count': 3,
            'event_meta': '\n'.join([
                json.dumps({'kind': 'text', 'source_event_id': 'e1'},
                           ensure_ascii=False),
                json.dumps({'kind': 'text', 'source_event_id': 'e2'},
                           ensure_ascii=False),
                json.dumps({'kind': 'text', 'source_event_id': 'e3'},
                           ensure_ascii=False),
            ]),
            'first_source_event_id': 'e1',
            'last_source_event_id': 'e3',
            'reply_state': 'soft_busy',
        }
        captured = {}
        with patch('characters.get_character',
                   return_value={'name': '五条', 'voice_id': 'v'}), \
             patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch('user_memory.get_short_memory', return_value=[]), \
             patch('user_memory.commit_visible_assistant_message'), \
             patch('user_memory.update_chat_days', return_value=1), \
             patch('memory_jobs.enqueue_private_extraction'), \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('temporal_awareness.record_turn'), \
             patch('prompt.build_system_blocks',
                    side_effect=lambda *a, extra_suffix='', **k: captured.update(
                        extra_suffix=extra_suffix) or [{'type': 'text', 'text': extra_suffix}]), \
             patch('tts.tts_to_b64', return_value=''), \
             patch('proactive_msg.add_proactive_msg', return_value=(1, NOW)):
            delayed_reply.generate_delayed_chat_reply(bundle, helpers=helpers)
        self.assertEqual(helpers.user_message, bundle['pending_text'])
        self.assertIs(helpers.auto_pin_enabled, False)
        self.assertEqual(set(helpers.current_event_id), {'e1', 'e2', 'e3'})
        history_text = ' '.join(
            item.get('content', '') for item in (helpers.extra_messages or [])
            if item.get('role') != 'user' or '系统内部' not in (item.get('content') or '')
        )
        for line in pending_lines:
            self.assertNotIn(line, history_text)
        self.assertIn('【积压原文】\n' + bundle['pending_text'], captured['extra_suffix'])
        self.assertEqual(captured['extra_suffix'].count('【积压原文】'), 1)
        final_prompt = '\n'.join(block['text'] for block in helpers.system_blocks)
        final_prompt += '\n' + '\n'.join(item['content'] for item in helpers.messages)
        self.assertEqual(final_prompt.count(bundle['pending_text']), 1)
        self.assertEqual(final_prompt.count('【积压原文】'), 1)
        for line in pending_lines:
            self.assertEqual(final_prompt.count(line), 1)

    def test_crash_after_commit_does_not_resend(self):
        helpers = HelpersStub()
        bundle = {
            'id': 15, 'user_id': 'u', 'character_id': 'gojo',
            'pending_text': '一', 'pending_count': 1,
            'event_meta': '', 'last_source_event_id': 'e1',
            'reply_state': 'soft_busy',
        }
        adds = []
        commits = []
        with patch('characters.get_character',
                   return_value={'name': '五条', 'voice_id': 'v'}), \
             patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch('user_memory.get_short_memory', return_value=[]), \
             patch('user_memory.commit_visible_assistant_message',
                   side_effect=lambda *a, event_id='', **k: commits.append(event_id)), \
             patch('user_memory.update_chat_days', return_value=1), \
             patch('memory_jobs.enqueue_private_extraction'), \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('temporal_awareness.record_turn'), \
             patch('prompt.build_system_blocks', return_value=[]), \
             patch('tts.tts_to_b64', return_value=''), \
             patch('proactive_msg.add_proactive_msg',
                   side_effect=lambda *a, **k: adds.append(1) or (len(adds), NOW)):
            first = delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=helpers)
            with patch.object(delayed_reply, 'assistant_already_committed',
                              return_value=True):
                second = delayed_reply.generate_delayed_chat_reply(
                    bundle, helpers=helpers)
        self.assertTrue(first['ok'])
        self.assertTrue(second['ok'])
        self.assertTrue(second.get('already_delivered'))
        self.assertEqual(helpers.generate_calls, 1)
        self.assertEqual(len(adds), 2)
        self.assertEqual(commits, [
            'delayed_reply:15:0', 'delayed_reply:15:1',
        ])

    def test_schedule_guard_rejects_delayed_completion_before_delivery(self):
        helpers = HelpersStub(bubbles=[{'jp': '会議はもう終わった。', 'zh': '会议已经结束了。'}])
        bundle = dict(_synthetic_claim(8), reply_state='soft_busy')
        with patch('characters.get_character', return_value={'name': '五条'}), \
             patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('prompt.build_system_blocks', return_value=[]), \
             patch.object(db_schedule, 'get_current_world_state', return_value={
                 'event': {'id': 7, 'status': 'active', 'revision': 3}}), \
             patch('schedule_transition.commit_generated_schedule_intent') as transition, \
             patch('user_memory.commit_visible_assistant_message') as commit, \
             patch('proactive_msg.add_proactive_msg') as deliver:
            result = delayed_reply.generate_delayed_chat_reply(bundle, helpers=helpers)
        self.assertEqual(result, {'ok': False, 'reason': 'active_event_completion_claim_without_intent'})
        self.assertIs(helpers.auto_pin_enabled, False)
        transition.assert_not_called()
        commit.assert_not_called()
        deliver.assert_not_called()

    def test_hard_busy_and_soft_busy_inbound_do_not_create_promise(self):
        backend = Path(BACKEND)
        for name in ('reply_availability.py', 'delayed_reply.py',
                     'route_chat.py', 'route_image.py'):
            src = (backend / name).read_text(encoding='utf-8')
            self.assertNotIn('ensure_busy_fallback', src)
            self.assertNotIn('attach_fallback_promise(', src)
        ra = (backend / 'reply_availability.py').read_text(encoding='utf-8')
        self.assertNotIn('add_promise', ra)
        store = PhoneCheckStore()
        hard = {
            **ACTIVITY, 'reply_state': 'hard_busy', 'title': '出任务',
            'start_time': '14:00', 'end_time': '15:00',
        }
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch('db_promise.add_promise') as add_promise:
            db_schedule.decide_phone_check(
                'gojo', 'u', NOW, hard,
                source_event_id='img-1', pending_text='📷 看这个',
                event_meta={'kind': 'image', 'visual_summary': '杯子'})
            db_schedule.decide_phone_check(
                'gojo', 'u', NOW, ACTIVITY,
                source_event_id='e1', pending_text='在吗')
        add_promise.assert_not_called()
        self.assertTrue(all(
            row.get('fallback_promise_id') in (None, 0)
            for row in store.rows.values()
        ))

    def test_genuine_proactive_promise_still_enters_generator(self):
        with patch.object(proactive_scheduler, 'get_character', return_value=None):
            result = proactive_scheduler.generate_from_promise({
                'id': 7,
                'character_id': 'gojo',
                'user_id': 'u',
                'context': '明天记得祝她生日快乐',
                'trigger_kind': 'once',
                'origin_text': '记得祝我生日快乐',
            }, NOW)
        self.assertIsNone(result)
        self.assertFalse(delayed_reply.is_busy_fallback_promise({
            'context': '明天记得祝她生日快乐',
        }))
        self.assertTrue(delayed_reply.is_busy_fallback_promise({
            'context': 'hard_busy phone_check_id=8 image pending',
        }))

    def test_production_phone_check_marker_not_used_to_generate(self):
        sched = Path(BACKEND, 'proactive_scheduler.py').read_text(encoding='utf-8')
        self.assertIn("if 'phone_check_id=' in (context or '')", sched)
        self.assertNotIn('resolve_phone_check', sched)
        self.assertNotIn('ensure_busy_fallback', sched)
        delayed_src = Path(BACKEND, 'delayed_reply.py').read_text(encoding='utf-8')
        self.assertIn('generate_delayed_chat_reply', delayed_src)
        self.assertIn('_turn_context', delayed_src)
        self.assertIn('_generate_or_none', delayed_src)
        self.assertNotIn('messages.create', delayed_src)
        self.assertNotIn('anthropic.Anthropic', delayed_src)

    def test_exclude_pending_ids_from_hot_context(self):
        from context_layer import exclude_current_turn_events
        events = [
            {'event_id': 'e1', 'content': '一'},
            {'event_id': 'e2', 'content': '二'},
            {'event_id': 'old', 'content': '更早'},
        ]
        kept = exclude_current_turn_events(events, ['e1', 'e2'])
        self.assertEqual([item['event_id'] for item in kept], ['old'])

    def test_delayed_reply_proactive_event_id_matches_chat_log(self):
        helpers = HelpersStub(bubbles=[
            {'jp': 'a', 'zh': 'A'},
            {'jp': 'b', 'zh': 'B'},
            {'jp': 'c', 'zh': 'C'},
        ])
        bundle = {
            'id': 8, 'user_id': 'u', 'character_id': 'gojo',
            'pending_text': '一\n二\n三', 'pending_count': 3,
            'event_meta': '', 'last_source_event_id': 'e3',
            'reply_state': 'soft_busy',
        }
        commits = []
        proactive_calls = []

        def capture_commit(*_args, **kwargs):
            commits.append(kwargs)

        def capture_proactive(*_args, **kwargs):
            proactive_calls.append(kwargs)
            return (len(proactive_calls), NOW)

        with patch('characters.get_character',
                   return_value={'name': '五条', 'voice_id': 'v'}), \
             patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch('user_memory.get_short_memory', return_value=[]), \
             patch('user_memory.commit_visible_assistant_message',
                   side_effect=capture_commit), \
             patch('user_memory.update_chat_days', return_value=1), \
             patch('memory_jobs.enqueue_private_extraction'), \
             patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
             patch('temporal_awareness.record_turn'), \
             patch('prompt.build_system_blocks', return_value=[]), \
             patch('tts.tts_to_b64', return_value='audio'), \
             patch('proactive_msg.add_proactive_msg',
                   side_effect=capture_proactive):
            result = delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=helpers)
        self.assertTrue(result['ok'])
        self.assertEqual(len(commits), 3)
        self.assertEqual(len(proactive_calls), 3)
        for index in range(3):
            expected = f'delayed_reply:8:{index}'
            self.assertEqual(commits[index]['event_id'], expected)
            self.assertEqual(proactive_calls[index]['event_id'], expected)
            self.assertEqual(
                proactive_calls[index]['assistant_turn_id'],
                'delayed_reply:8:0',
            )
            self.assertEqual(proactive_calls[index]['segment_index'], index)
            self.assertFalse(
                str(proactive_calls[index]['event_id']).startswith(
                    'proactive:delayed_reply')
            )

    def test_pending_bundle_replies_at_effective_busy_end(self):
        store = PhoneCheckStore()
        start = datetime(2026, 9, 19, 14, 5, tzinfo=timezone.utc)
        due = datetime(2026, 9, 19, 14, 15, tzinfo=timezone.utc)
        activity = {
            **ACTIVITY,
            'start_time': '14:00',
            'end_time': '14:15',
            'title': '处理报告',
            'reply_state': 'soft_busy',
            'effective_busy_minutes': None,
        }
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2] if len(a) > 2 else due, False)), \
             patch.object(db_schedule.random, 'randint', return_value=29):
            created = db_schedule.decide_phone_check(
                'gojo', 'u', start, activity,
                source_event_id='e1', pending_text='在吗')
        row = list(store.rows.values())[0]
        self.assertLessEqual(created['next_phone_check_at'], due)
        row['next_phone_check_at'] = created['next_phone_check_at']
        row['check_state'] = 'pending'
        row['resolved_at'] = None
        store.schedule_rows = [{
            'id': 7, 'character_id': 'gojo', 'user_id': 'u',
            'sched_date': due.date(), 'start_time': '14:00', 'end_time': '16:00',
            'title': '处理报告', 'can_reply': False, 'reply_state': 'soft_busy',
            'effective_busy_minutes': 15,
        }]
        generate_calls = []

        def generate_fn(bundle):
            generate_calls.append(bundle)
            return {'ok': True, 'messages': []}

        free_activity = {
            **activity,
            'start_time': '14:15',
            'end_time': '16:00',
            'reply_state': 'free',
            'can_reply': True,
        }
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
             patch.object(db_schedule, 'get_current_world_state',
                          return_value=sched_tests.canonical_test_world(
                              free_activity, due)), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2] if len(a) > 2 else due, False)), \
             patch.object(db_schedule.random, 'random', return_value=0.99):
            results = delayed_reply.process_due_phone_checks(
                due, generate_fn=generate_fn)
        self.assertEqual(results[0]['action'], 'replied')
        self.assertEqual(len(generate_calls), 1)
        self.assertEqual(row['check_state'], 'consumed')


class DelayedGroundingTests(unittest.TestCase):
    def setUp(self):
        import context_layer
        import route_chat

        self.context = context_layer
        self.route = route_chat
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        context_layer.use_memory_store(True)
        self.addCleanup(context_layer.use_memory_store, False)
        self.sources = {}
        self.hot = []
        self.stack.enter_context(patch.object(
            raw_events, 'get_active_events_by_ids', side_effect=lambda u, c, ids, **k:
            [self.sources[sid] for sid in ids if sid in self.sources]))
        self.stack.enter_context(patch.object(raw_events, 'deleted_event_ids', return_value=set()))
        self.stack.enter_context(patch.object(
            raw_events, 'get_hot_candidate_events', side_effect=lambda *a, **k: deepcopy(self.hot)))
        self.stack.enter_context(patch.object(context_layer, '_support_items', return_value=([], '')))
        self.stack.enter_context(patch.object(context_layer, '_build_recall_query_embedding', return_value=None))
        self.recall = self.stack.enter_context(patch('smart_recall.two_level_recall', return_value={
            'facts': [], 'loose_bonds': [], 'tolds': []}))
        self.stack.enter_context(patch('episodic_index.recall_episodes', return_value=[]))
        self.stack.enter_context(patch('cognitive_reader.build_cognitive_prompt_context', return_value=''))
        self.stack.enter_context(patch('characters.get_character', return_value={
            'name': '五条', 'core_prompt': 'CORE-PERSONA'}))
        self.stack.enter_context(patch('prompt.get_character', return_value={
            'name': '五条', 'core_prompt': 'CORE-PERSONA'}))
        self.stack.enter_context(patch('prompt.load_canon_lock', return_value='CANON-LOCK'))
        self.stack.enter_context(patch('prompt.get_first_interaction_days', return_value=20))
        self.stack.enter_context(patch('prompt._accounts_block', return_value=''))
        self.stack.enter_context(patch('db_schedule.format_world_prompt', return_value=('', {})))
        self.stack.enter_context(patch('temporal_awareness.get_temporal_snapshot', return_value={
            'now_utc': NOW, 'now_local': NOW, 'has_history': True}))
        self.stack.enter_context(patch.object(route_chat, 'get_temporal_snapshot', return_value={
            'now_utc': NOW, 'now_local': NOW, 'has_history': True}))
        self.stack.enter_context(patch.object(delayed_reply, 'assistant_already_committed', return_value=False))
        cursor = Mock()
        cursor.fetchall.return_value = []
        self.stack.enter_context(patch('db.get_conn', return_value=Mock(cursor=Mock(return_value=cursor))))

    def event(self, source_id, text, timestamp=None, role='user'):
        event = {'event_id': source_id, 'role': role, 'content': text,
                 'timestamp': timestamp, 'metadata': {}}
        self.sources[source_id] = event
        return event

    def bundle(self, source_ids, **extra):
        return dict(
            id=8, user_id='u', character_id='gojo', pending_text='旧副本',
            pending_count=len(source_ids), first_source_event_id=source_ids[0],
            last_source_event_id=source_ids[-1],
            event_meta=[{'source_event_id': sid} for sid in source_ids], **extra)

    def generate_failed(self, checked):
        stub = HelpersStub(fail=True)
        helpers = types.SimpleNamespace(
            _turn_context=self.route._turn_context,
            _history_plus_current=self.route._history_plus_current,
            _generate_or_none=stub._generate_or_none,
            _finalize_committed=stub._finalize_committed)
        with patch('user_memory.save_user_short_memory_once', return_value=True), \
             patch.object(self.route, '_turn_context', wraps=self.route._turn_context) as turn:
            helpers._turn_context = turn
            result = delayed_reply.generate_delayed_chat_reply(checked, helpers=helpers)
        self.assertEqual(result, {'ok': False, 'reason': 'generation_failed'})
        self.assertEqual(turn.call_args.args[2], checked['pending_text'])
        self.assertIs(turn.call_args.kwargs['auto_pin_enabled'], False)
        self.assertEqual(turn.call_args.kwargs['current_event_id'],
                         delayed_reply._pending_source_event_ids(checked))
        self.assertEqual(self.recall.call_args.args[2], checked['pending_text'])
        return stub

    def test_canonical_send_times_align_with_sources_without_metadata_writes(self):
        first_time = NOW - timedelta(hours=2)
        last_time = (NOW - timedelta(minutes=10)).astimezone(timezone(timedelta(hours=8)))
        self.event('e1', '第一条蓝色原文\n含换行', first_time)
        self.event('e3', '第三条原文', last_time)
        self.event('assistant-copy', 'assistant不能成为积压', NOW, role='assistant')
        bundle = self.bundle(['e1', 'deleted', 'assistant-copy', 'e3'])
        original = deepcopy(bundle)
        raw_before = deepcopy(self.sources)
        checked = delayed_reply._precheck_pending_bundle_sources(bundle)
        self.assertEqual(bundle, original)
        self.assertEqual(checked['pending_text'], '第一条蓝色原文\n含换行\n第三条原文')
        self.assertEqual(checked['pending_prompt_events'], [
            {'source_event_id': 'e1', 'text': '第一条蓝色原文\n含换行', 'timestamp': first_time},
            {'source_event_id': 'e3', 'text': '第三条原文', 'timestamp': last_time},
        ])
        rendered = reply_availability.format_pending_bundle_context(checked)
        first_display = '[发送时间 2026-09-18 08:00:00+00:00] 第一条蓝色原文\n含换行'
        last_display = '[发送时间 2026-09-18 17:50:00+08:00] 第三条原文'
        self.assertIn(first_display, rendered)
        self.assertIn(last_display, rendered)
        self.assertLess(rendered.index(first_display), rendered.index(last_display))
        self.assertEqual(rendered.count('【积压原文】'), 1)
        self.assertNotIn('assistant不能成为积压', rendered)
        self.assertNotIn('deleted', rendered)
        with patch('user_memory.save_user_short_memory_once', return_value=True) as save:
            delayed_reply._persist_pending_user_messages(checked)
        self.assertEqual([call.kwargs['source_event_id'] for call in save.call_args_list], ['e1', 'e3'])
        self.assertEqual([call.args[1] for call in save.call_args_list],
                         ['第一条蓝色原文\n含换行', '第三条原文'])
        for call in save.call_args_list:
            self.assertNotIn('timestamp', call.kwargs['event_meta'])
            self.assertNotIn('pending_prompt_events', call.kwargs['event_meta'])
            self.assertNotIn('[发送时间', call.args[1])
        self.assertEqual(self.sources, raw_before)

    def test_missing_canonical_time_keeps_original_and_ignores_copied_times(self):
        pending = ' \n缺时间仍保留这条原文\n '
        self.event('e1', pending)
        bundle = self.bundle(['e1'], seen_at=NOW, claimed_at=NOW)
        bundle['event_meta'][0]['timestamp'] = '2099-01-01T00:00:00+00:00'
        bundle['pending_prompt_events'] = [{'text': '伪造显示', 'timestamp': NOW}]
        checked = delayed_reply._precheck_pending_bundle_sources(bundle)
        self.assertEqual(checked['pending_prompt_events'], [
            {'source_event_id': 'e1', 'text': pending, 'timestamp': None}])
        self.assertEqual(checked['pending_text'], pending)
        rendered = reply_availability.format_pending_bundle_context(checked)
        self.assertIn('【积压原文】\n缺时间仍保留这条原文\n', rendered)
        self.assertNotIn('[发送时间', rendered)
        self.assertNotIn('2099', rendered)
        self.assertNotIn('伪造显示', rendered)
        self.hot = [self.event('hot-topic', '当前海报话题', NOW - timedelta(minutes=1))]
        self.generate_failed(checked)

    def test_real_delayed_context_queries_pending_once_and_creates_no_pin(self):
        import auto_pin

        pending = '现在 push，顺便解释刚刚为什么选蓝色？'
        current = self.event('e1', pending, NOW - timedelta(minutes=2))
        self.hot = [self.event('old-topic', '海报蓝色还是绿色？', NOW - timedelta(minutes=3)), current]
        checked = delayed_reply._precheck_pending_bundle_sources(self.bundle(['e1']))
        with patch.object(auto_pin, 'maybe_auto_pin', wraps=auto_pin.maybe_auto_pin) as pin:
            stub = self.generate_failed(checked)
        pin.assert_not_called()
        self.assertEqual(self.context._PINS, {})
        final_prompt = '\n'.join(block['text'] for block in stub.system_blocks)
        final_prompt += '\n' + '\n'.join(item['content'] for item in stub.messages)
        self.assertEqual(final_prompt.count(pending), 1)
        self.assertEqual(final_prompt.count('【积压原文】'), 1)
        self.assertIn('海报蓝色还是绿色？', final_prompt)
        self.assertIn('【当前消息优先】', final_prompt)
        self.assertIn('系统内部', stub.messages[-1]['content'])
        self.assertNotIn(pending, '\n'.join(item['content'] for item in stub.messages))

    def test_failed_delayed_replay_keeps_newer_conflicting_pin_unchanged(self):
        import auto_pin

        for old_text, new_text in [('现在 push', '不要 push'), ('不要 push', '现在 push')]:
            with self.subTest(old_text=old_text):
                self.context.reset_memory_store()
                old = self.event('old-pending', old_text, NOW - timedelta(minutes=4))
                self.event('newer-user', new_text, NOW - timedelta(minutes=2))
                self.hot = [self.event('hot-topic', '刚刚说到海报颜色', NOW - timedelta(minutes=1)), old]
                auto_pin.maybe_auto_pin('u', 'gojo', old_text, source_event_ids=['old-pending'],
                                       now=old['timestamp'])
                auto_pin.maybe_auto_pin('u', 'gojo', new_text, source_event_ids=['newer-user'],
                                       now=self.sources['newer-user']['timestamp'])
                before = deepcopy(self.context._PINS)
                checked = delayed_reply._precheck_pending_bundle_sources(self.bundle(['old-pending']))
                with patch.object(auto_pin, 'maybe_auto_pin', wraps=auto_pin.maybe_auto_pin) as pin:
                    for _attempt in range(2):
                        self.generate_failed(checked)
                        self.assertEqual(self.context._PINS, before)
                pin.assert_not_called()

    def test_pending_query_and_display_keep_existing_4000_character_budget(self):
        first = self.event('e1', '旧段落' + 'x' * 3870, NOW - timedelta(minutes=3))
        second = self.event('e2', '最新问题：刚刚为什么选蓝色？\n这条消息还有一行', NOW - timedelta(minutes=2))
        third = self.event('e3', '末段' + 'y' * 300, NOW - timedelta(minutes=1))
        expected = '\n'.join(event['content'] for event in (first, second, third))[:4000]
        checked = delayed_reply._precheck_pending_bundle_sources(self.bundle(['e1', 'e2', 'e3']))
        self.assertEqual(len(checked['pending_text']), 4000)
        self.assertEqual(checked['pending_text'], expected)
        self.assertIn(second['content'], checked['pending_text'])
        self.assertEqual('\n'.join(event['text'] for event in checked['pending_prompt_events']), expected)
        self.hot = [self.event('hot-topic', '当前海报话题', NOW - timedelta(minutes=1))]
        stub = self.generate_failed(checked)
        final_prompt = '\n'.join(block['text'] for block in stub.system_blocks)
        self.assertEqual(final_prompt.count(second['content']), 1)
        self.assertEqual(final_prompt.count('【积压原文】'), 1)
        self.assertNotIn(third['content'], final_prompt)
        with patch('user_memory.save_user_short_memory_once', return_value=True) as save:
            delayed_reply._persist_pending_user_messages(checked)
        self.assertEqual([call.args[1] for call in save.call_args_list],
                         [first['content'], second['content'], third['content']])


if __name__ == '__main__':
    unittest.main()
