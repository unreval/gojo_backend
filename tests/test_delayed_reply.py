# -*- coding: utf-8 -*-
import inspect
import json
import os
import sys
import types
import unittest
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
import reply_availability  # noqa: E402
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


def _seed_soft_busy_bundle(store, texts, *, due=None, event_metas=None):
    due = due or (NOW - timedelta(minutes=1))
    start = due - timedelta(minutes=20)
    with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
         patch.object(db_schedule, 'sample_next_phone_check_at', return_value=due), \
         patch.object(db_schedule, 'postpone_past_hard_busy',
                      side_effect=lambda *a, **k: (a[2] if len(a) > 2 else due, False)):
        first_meta = (event_metas or [None] * len(texts))[0]
        first = db_schedule.decide_phone_check(
            'gojo', 'u', start, ACTIVITY,
            source_event_id='e1', pending_text=texts[0], event_meta=first_meta)
        oid = first['opportunity_id']
        for index, text in enumerate(texts[1:], start=2):
            meta = None
            if event_metas and index - 1 < len(event_metas):
                meta = event_metas[index - 1]
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
        self.bubbles = bubbles or [
            {'jp': '今見た', 'zh': '刚才看到了'},
            {'jp': 'ちょっと待ってた', 'zh': '让你等了一下'},
        ]
        self.history = history or []
        self.extra_messages = None

    def _turn_context(self, user_id, character_id, user_message='', profile='default',
                      current_event_id=None):
        self.context_calls += 1
        self.user_message = user_message
        self.current_event_id = current_event_id
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
        self._io_patches = [
            patch('behavior_evidence.record_reply_cycle', Mock()),
            patch('push_notify.push_to_user', Mock()),
            patch.object(delayed_reply, 'assistant_already_committed',
                         return_value=False),
        ]
        for item in self._io_patches:
            item.start()
            self.addCleanup(item.stop)

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
        self.assertEqual(helpers.relationship_calls, 1)
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
        self.assertEqual(results[0]['action'], 'failed')
        self.assertEqual(row['check_state'], 'pending')
        self.assertIsNone(row.get('resolved_at'))
        self.assertEqual(row['pending_count'], 2)
        self.assertIn('一', row['pending_text'])

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
        self.assertEqual(helpers.relationship_calls, 1)
        self.assertEqual(helpers.generate_calls, 1)
        self.assertEqual(jobs.call_count, 1)
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
        helpers = HelpersStub(history=[
            {'role': 'user', 'content': '一', 'event_id': 'e1'},
            {'role': 'user', 'content': '二', 'event_id': 'e2'},
            {'role': 'user', 'content': '三', 'event_id': 'e3'},
            {'role': 'user', 'content': '更早的话', 'event_id': 'old'},
        ])
        bundle = {
            'id': 8, 'user_id': 'u', 'character_id': 'gojo',
            'pending_text': '一\n二\n三', 'pending_count': 3,
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
                       extra_suffix=extra_suffix) or []), \
             patch('tts.tts_to_b64', return_value=''), \
             patch('proactive_msg.add_proactive_msg', return_value=(1, NOW)):
            delayed_reply.generate_delayed_chat_reply(bundle, helpers=helpers)
        self.assertEqual(helpers.user_message, '')
        self.assertEqual(set(helpers.current_event_id), {'e1', 'e2', 'e3'})
        history_text = ' '.join(
            item.get('content', '') for item in (helpers.extra_messages or [])
            if item.get('role') != 'user' or '系统内部' not in (item.get('content') or '')
        )
        self.assertNotIn('一', history_text)
        self.assertIn('【积压原文】\n一\n二\n三', captured['extra_suffix'])
        self.assertEqual(captured['extra_suffix'].count('【积压原文】'), 1)

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


if __name__ == '__main__':
    unittest.main()
