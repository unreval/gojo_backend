# -*- coding: utf-8 -*-
"""Live multi-message batching reuses the phone-check inbox.

REG-015: /chat/flush must not consult the delayed-reply hourly quota.
"""
import asyncio
import json
import os
import re
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
TESTS = ROOT / 'tests'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import db_schedule  # noqa: E402
import delayed_reply  # noqa: E402
import reply_availability  # noqa: E402
import test_schedule_reply_state as sched_tests  # noqa: E402

FakeConn = sched_tests.FakeConn
PhoneCheckStore = sched_tests.PhoneCheckStore

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
FREE_WORLD = {
    'activity': None,
    'event': {},
    'phase': {},
    'availability': {'reply_state': 'free', 'can_reply': True},
}
BUSY = {
    'id': 7,
    'start_time': '09:00',
    'end_time': '11:00',
    'title': '开会',
    'location': '会议室',
    'reply_state': 'soft_busy',
    'can_reply': False,
    'character_id': 'gojo',
}


def _conn(store):
    return FakeConn(store)


class MessageBatchingTests(unittest.TestCase):
    def _patch_store(self, store, world=None):
        world = FREE_WORLD if world is None else world
        return patch.object(db_schedule, 'get_conn', lambda: _conn(store)), patch.object(
            db_schedule, 'get_current_world_state', return_value=world)

    def _batch(self, store, text, source_id, when=NOW):
        with self._patch_store(store)[0], self._patch_store(store)[1]:
            return db_schedule.decide_phone_check(
                'gojo', 'u', when, None,
                source_event_id=source_id, pending_text=text,
                event_meta={'kind': 'text', 'source_event_id': source_id},
                batching=True)

    def test_batching_false_free_path_creates_no_row(self):
        store = PhoneCheckStore()
        with self._patch_store(store)[0], self._patch_store(store)[1]:
            decision = reply_availability.check_reply_availability(
                'gojo', 'u', source_event_id='e0', pending_text='在吗', now=NOW)
        self.assertTrue(decision['can_reply'])
        self.assertEqual(decision['reply_state'], 'free')
        self.assertIsNone(decision['opportunity_id'])
        self.assertEqual(store.rows, {})

    def test_busy_inbox_stays_unlabeled(self):
        store = PhoneCheckStore()
        with self._patch_store(store)[0], patch.object(
                db_schedule, 'sample_next_phone_check_at',
                return_value=NOW + timedelta(minutes=5)):
            decision = db_schedule.decide_phone_check(
                'gojo', 'u', NOW, BUSY,
                source_event_id='busy-1', pending_text='在吗')
        row = list(store.rows.values())[0]
        self.assertIsNone(row.get('inbox_reason'))
        self.assertEqual(row['reply_state'], 'soft_busy')
        self.assertFalse(decision['can_reply'])

    def test_three_messages_share_one_batching_row(self):
        store = PhoneCheckStore()
        for index, text in enumerate(('一', '二', '三'), start=1):
            self._batch(store, text, f'e{index}')
        self.assertEqual(len(store.rows), 1)
        row = list(store.rows.values())[0]
        self.assertEqual(row['inbox_reason'], 'batching')
        self.assertEqual(row['reply_state'], 'free')
        self.assertEqual(row['end_time'], '23:59')
        self.assertEqual(row['start_time'], '00:00')
        self.assertEqual(row['sched_date'], NOW.date())
        self.assertEqual(row['pending_count'], 3)
        self.assertEqual(row['pending_text'], '一\n二\n三')
        self.assertEqual(row['next_phone_check_at'], NOW + timedelta(seconds=60))

    def test_messages_across_minute_boundaries_share_one_row(self):
        store = PhoneCheckStore()
        moments = (
            datetime(2026, 11, 8, 19, 24, 58, tzinfo=timezone.utc),
            datetime(2026, 11, 8, 19, 25, 3, tzinfo=timezone.utc),
            datetime(2026, 11, 8, 19, 26, 10, tzinfo=timezone.utc),
        )
        for index, when in enumerate(moments, start=1):
            self._batch(store, f'第{index}', f'cross-{index}', when)
        self.assertEqual(len(store.rows), 1)
        row = list(store.rows.values())[0]
        self.assertEqual(row['inbox_reason'], 'batching')
        self.assertEqual(row['start_time'], '00:00')
        self.assertEqual(row['end_time'], '23:59')
        self.assertEqual(row['pending_count'], 3)
        self.assertEqual(row['pending_text'], '第1\n第2\n第3')
        self.assertEqual(
            [json.loads(line)['source_event_id'] for line in row['event_meta'].split('\n')],
            ['cross-1', 'cross-2', 'cross-3'])

    def test_consumed_batching_row_starts_a_successor(self):
        store = PhoneCheckStore()
        self._batch(store, '旧的', 'old')
        finished = list(store.rows.values())[0]
        finished_id = finished['id']
        finished['check_state'] = 'consumed'
        finished['resolved_at'] = NOW
        self._batch(store, '新的', 'new', NOW + timedelta(minutes=2))
        fresh = list(store.rows.values())[0]
        self.assertEqual(fresh['pending_count'], 1)
        self.assertEqual(fresh['pending_text'], '新的')
        self.assertEqual(fresh['check_state'], 'pending')
        self.assertEqual(fresh['inbox_reason'], 'batching')
        self.assertIsNone(fresh.get('resolved_at'))
        self.assertEqual(fresh['successor_of_id'], finished_id)
        self.assertNotEqual(fresh['id'], finished_id)

    def test_all_day_busy_row_does_not_merge_with_batching(self):
        store = PhoneCheckStore()
        self._batch(store, '连发', 'b1')
        activity = dict(BUSY, start_time='00:00', end_time='23:59', title='外出')
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
                patch.object(db_schedule, 'sample_next_phone_check_at',
                             return_value=NOW + timedelta(minutes=5)):
            db_schedule.decide_phone_check(
                'gojo', 'u', NOW, activity,
                source_event_id='busy-1', pending_text='忙里')
            self._batch(store, '又一条', 'b2', NOW + timedelta(minutes=1))
            db_schedule.decide_phone_check(
                'gojo', 'u', NOW + timedelta(minutes=2), activity,
                source_event_id='busy-2', pending_text='还在忙')
        rows = list(store.rows.values())
        self.assertEqual(len(rows), 2)
        batching = next(row for row in rows if row.get('inbox_reason') == 'batching')
        busy = next(row for row in rows if not row.get('inbox_reason'))
        self.assertEqual(batching['pending_count'], 2)
        self.assertEqual(batching['pending_text'], '连发\n又一条')
        self.assertEqual(busy['pending_count'], 2)
        self.assertEqual(busy['pending_text'], '忙里\n还在忙')
        self.assertEqual(busy['reply_state'], 'soft_busy')
        self.assertIn('连着发了几条', delayed_reply._delayed_trigger(batching))
        self.assertNotIn('有空看手机', delayed_reply._delayed_trigger(batching))
        self.assertIn('有空看手机', delayed_reply._delayed_trigger(busy))
        self.assertNotIn('连着发了几条', delayed_reply._delayed_trigger(busy))

    def test_identical_texts_both_enter_the_inbox(self):
        store = PhoneCheckStore()
        self._batch(store, '【表情】皱眉', 's1')
        self._batch(store, '【表情】皱眉', 's2')
        row = list(store.rows.values())[0]
        self.assertEqual(row['pending_count'], 2)
        self.assertEqual(row['pending_text'], '【表情】皱眉\n【表情】皱眉')

    def test_free_supersede_does_not_arm_an_open_batching_row(self):
        store = PhoneCheckStore()
        self._batch(store, '还在打', 'e1')
        row = list(store.rows.values())[0]
        original_next = row['next_phone_check_at']
        later = NOW + timedelta(minutes=5)
        conn = _conn(store)
        cur = conn.cursor()
        db_schedule.supersede_ended_phone_checks(cur, 'u', 'gojo', later, None)
        self.assertEqual(row['check_state'], 'pending')
        self.assertEqual(row['next_phone_check_at'], original_next)
        self.assertIsNone(row.get('resolved_at'))

    def _flush_world(self, store):
        return (
            patch.object(db_schedule, 'get_conn', lambda: _conn(store)),
            patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD),
            patch.object(delayed_reply, '_precheck_pending_bundle_sources',
                         side_effect=lambda bundle: bundle),
        )

    def test_flush_ignores_hourly_quota_and_does_not_push(self):
        store = PhoneCheckStore()
        self._batch(store, '一起回', 'e1')
        pushed = Mock()
        generated = []

        def generate_fn(bundle, push=True):
            generated.append(push)
            self.assertFalse(delayed_reply._generation_allowed())
            return {
                'ok': True,
                'messages': [{'jp': '見た', 'zh': '看到了'}],
                'full_jp': '見た',
                'turn_id': 'turn-1',
                'emotion': '平静',
            }

        calls_before = len(delayed_reply._generation_calls)
        with self._flush_world(store)[0], self._flush_world(store)[1], \
                self._flush_world(store)[2], \
                patch.object(delayed_reply, 'DELAYED_REPLY_MAX_PER_HOUR', 0), \
                patch.object(delayed_reply, '_push_delayed_reply', pushed):
            self.assertFalse(delayed_reply._generation_allowed())
            skipped = delayed_reply.process_due_phone_checks(NOW)
            result = delayed_reply.flush_batching_reply(
                'u', 'gojo', now=NOW, generate_fn=generate_fn)
        self.assertEqual(skipped, [])
        self.assertTrue(result['ok'])
        self.assertEqual(generated, [False])
        self.assertFalse(pushed.called)
        self.assertEqual(len(delayed_reply._generation_calls), calls_before)
        row = list(store.rows.values())[0]
        self.assertEqual(row['check_state'], 'consumed')

    def test_flush_and_worker_claim_once(self):
        store = PhoneCheckStore()
        self._batch(store, '只生成一次', 'e1')
        calls = []
        started = threading.Barrier(2)

        def generate_fn(bundle, push=True):
            calls.append(bundle.get('id'))
            return {'ok': True, 'messages': [{'jp': 'a', 'zh': 'A'}],
                    'full_jp': 'a', 'turn_id': 't', 'emotion': '平静'}

        results = []

        def run():
            started.wait(timeout=5)
            results.append(delayed_reply.flush_batching_reply(
                'u', 'gojo', now=NOW, generate_fn=generate_fn))

        with self._flush_world(store)[0], self._flush_world(store)[1], \
                self._flush_world(store)[2]:
            threads = [threading.Thread(target=run), threading.Thread(target=run)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
        oks = [item for item in results if item.get('ok')]
        losers = [item for item in results if not item.get('ok')]
        self.assertEqual(len(oks), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(losers), 1)
        self.assertIn(losers[0].get('reason'), ('skip', 'nothing_pending'))

    def test_batching_trigger_does_not_invent_a_busy_excuse(self):
        self.assertIn('连着发了几条', delayed_reply._delayed_trigger(
            {'inbox_reason': 'batching'}))
        self.assertNotIn('有空看手机', delayed_reply._delayed_trigger(
            {'inbox_reason': 'batching'}))
        self.assertNotIn('分别回', delayed_reply._delayed_trigger(
            {'inbox_reason': 'batching'}))
        busy = delayed_reply._delayed_trigger({'inbox_reason': None, 'reply_state': 'soft_busy'})
        self.assertIn('有空看手机', busy)
        self.assertIn('忙碌期间', busy)

    def test_frontend_keeps_attachment_locked_and_duplicate_guard(self):
        text = (ROOT / 'app' / 'chat' / '[id].tsx').read_text(encoding='utf-8')
        self.assertIn('const DUP_SEND_GUARD_MS = 400;', text)
        self.assertIn('now - lastSentRef.current.at < DUP_SEND_GUARD_MS', text)
        self.assertIn('if (!opts?.retry && text === lastSentRef.current.text', text)
        self.assertIn('onPress={showImagePicker} disabled={loading}', text)
        self.assertIn('if (loading) return;', text)
        self.assertIn('const BATCH_FLUSH_MS = 5000;', text)
        self.assertNotIn('< 2000', text)
        send_text = text.split('const sendText = async')[1].split('const retryLastGeneration')[0]
        busy_at = send_text.find('if (res.data?.busy)')
        flush_at = send_text.find('scheduleBatchFlush()')
        self.assertGreater(busy_at, 0)
        self.assertGreater(flush_at, busy_at)
        self.assertNotIn('scheduleBatchFlush', send_text[busy_at:flush_at])
        self.assertNotIn('/chat/flush', send_text[busy_at:flush_at])


class DeferredRouteTests(unittest.TestCase):
    def test_defer_returns_202_without_a_model_call(self):
        import route_chat
        order = []

        def avail(*_args, **kwargs):
            order.append(('avail', bool(kwargs.get('batching'))))
            return {'can_reply': True, 'pending_count': 2, 'opportunity_id': 9}

        def explode(*_args, **_kwargs):
            order.append('model')
            raise AssertionError('model must not run')

        payload = {
            'user_id': 'u', 'character_id': 'gojo', 'text': '第一句',
            'source_event_id': 'defer-1', 'defer': True,
        }
        with patch.object(route_chat, 'get_character', return_value={'voice_id': 'v'}), \
                patch.object(route_chat, 'save_user_short_memory_once',
                             side_effect=lambda *_a, **_k: order.append('save') or True), \
                patch.object(route_chat, 'get_temporal_snapshot',
                             return_value={'now_local': NOW}), \
                patch('reply_availability.check_reply_availability', side_effect=avail), \
                patch.object(route_chat, '_create_json', side_effect=explode), \
                patch.object(route_chat, '_provider_create', side_effect=explode):
            response = asyncio.run(route_chat.chat_text(payload))
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 202, body)
        self.assertTrue(body['deferred'])
        self.assertEqual(body['pending_count'], 2)
        self.assertEqual(body['source_event_id'], 'defer-1')
        self.assertEqual(order, ['save', ('avail', False), ('avail', True)])

    def test_without_defer_the_availability_call_stays_unchanged(self):
        import route_chat
        captured = {}

        def avail(*_args, **kwargs):
            captured.update(kwargs)
            return {
                'can_reply': False, 'seen': False, 'reply_state': 'hard_busy',
                'activity': {'title': '睡', 'location': '', 'end_time': '08:00'},
                'pending_count': 1, 'opportunity_id': 4,
                'free_at': None, 'seen_at': None, 'next_phone_check_at': None,
            }

        payload = {
            'user_id': 'u', 'character_id': 'gojo', 'text': '在吗',
            'source_event_id': 'plain-1',
        }
        with patch.object(route_chat, 'get_character', return_value={'voice_id': 'v'}), \
                patch.object(route_chat, 'save_user_short_memory_once', return_value=True), \
                patch.object(route_chat, 'get_temporal_snapshot', return_value={}), \
                patch.object(route_chat, 'update_chat_days', return_value=1), \
                patch('reply_availability.check_reply_availability', side_effect=avail):
            response = asyncio.run(route_chat.chat_text(payload))
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body['busy'])
        self.assertNotIn('batching', captured)

    def test_defer_while_busy_matches_the_plain_busy_response(self):
        import route_chat
        decision = {
            'can_reply': False, 'seen': False, 'reply_state': 'soft_busy',
            'activity': {'title': '开会', 'location': '会议室', 'end_time': '11:00'},
            'pending_count': 1, 'opportunity_id': 4,
            'free_at': '11:00', 'seen_at': None, 'next_phone_check_at': None,
        }
        flags = []

        def avail(*_args, **kwargs):
            flags.append(bool(kwargs.get('batching')))
            return decision

        async def both():
            results = []
            for extra in ({}, {'defer': True}):
                payload = {
                    'user_id': 'u', 'character_id': 'gojo', 'text': '在吗',
                    'source_event_id': 'busy-same', **extra,
                }
                results.append(await route_chat.chat_text(payload))
            return results

        with patch.object(route_chat, 'get_character', return_value={'voice_id': 'v'}), \
                patch.object(route_chat, 'save_user_short_memory_once', return_value=True), \
                patch.object(route_chat, 'get_temporal_snapshot', return_value={}), \
                patch.object(route_chat, 'update_chat_days', return_value=3), \
                patch('reply_availability.check_reply_availability', side_effect=avail):
            plain, deferred = asyncio.run(both())
        self.assertEqual(plain.status_code, deferred.status_code)
        self.assertEqual(plain.status_code, 200)
        self.assertEqual(json.loads(plain.body), json.loads(deferred.body))
        self.assertEqual(json.loads(plain.body)['busy'], True)
        self.assertEqual(flags, [False, False])

    def _route_with_world(self, store, world, payload):
        import route_chat
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=world), \
                patch.object(db_schedule, 'get_next_free_time', return_value='11:00'), \
                patch.object(db_schedule, 'sample_next_phone_check_at',
                             return_value=NOW + timedelta(minutes=5)), \
                patch.object(route_chat, 'get_character', return_value={'voice_id': 'v'}), \
                patch.object(route_chat, 'save_user_short_memory_once', return_value=True), \
                patch.object(route_chat, 'get_temporal_snapshot',
                             return_value={'now_local': NOW}), \
                patch.object(route_chat, 'update_chat_days', return_value=3):
            return asyncio.run(route_chat.chat_text(payload))

    def test_defer_while_busy_keeps_the_inbox_unlabeled(self):
        store = PhoneCheckStore()
        world = {
            'activity': BUSY, 'event': {}, 'phase': {},
            'availability': {'reply_state': 'soft_busy', 'can_reply': False},
        }
        response = self._route_with_world(store, world, {
            'user_id': 'u', 'character_id': 'gojo', 'text': '在忙吗',
            'source_event_id': 'busy-defer', 'defer': True,
        })
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200, body)
        self.assertTrue(body['busy'])
        self.assertNotIn('deferred', body)
        row = list(store.rows.values())[0]
        self.assertIsNone(row.get('inbox_reason'))
        self.assertEqual(row['reply_state'], 'soft_busy')
        self.assertIn('有空看手机', delayed_reply._delayed_trigger(row))
        self.assertNotIn('连着发了几条', delayed_reply._delayed_trigger(row))

    def test_defer_while_free_opens_a_batching_row(self):
        store = PhoneCheckStore()
        response = self._route_with_world(store, FREE_WORLD, {
            'user_id': 'u', 'character_id': 'gojo', 'text': '连发',
            'source_event_id': 'free-defer', 'defer': True,
        })
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 202, body)
        self.assertTrue(body['deferred'])
        row = list(store.rows.values())[0]
        self.assertEqual(row['inbox_reason'], 'batching')
        self.assertEqual(row['start_time'], '00:00')
        self.assertIn('连着发了几条', delayed_reply._delayed_trigger(row))
        self.assertNotIn('有空看手机', delayed_reply._delayed_trigger(row))


@unittest.skipUnless(
    os.getenv('COGNITIVE_TEST_PGLITE'),
    'set COGNITIVE_TEST_PGLITE to the local test-only package')
class DeferredStickerPersistenceTests(unittest.TestCase):
    from tests import test_private_source_first as fixture
    setUpClass = classmethod(fixture.PrivateSourceFirstTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PrivateSourceFirstTests.tearDownClass.__func__)
    sql = fixture.PrivateSourceFirstTests.sql
    tearDown = fixture.PrivateSourceFirstTests.tearDown

    def setUp(self):
        self.fixture.PrivateSourceFirstTests.setUp(self)

    def test_deferred_sticker_keeps_canonical_kind_and_skips_the_model(self):
        import route_chat
        import user_memory
        order = []

        def avail(*_args, **kwargs):
            order.append(bool(kwargs.get('batching')))
            return {'can_reply': True, 'pending_count': 1, 'opportunity_id': 1}

        def explode(*_args, **_kwargs):
            order.append('model')
            raise AssertionError('model must not run')

        payload = {
            'user_id': 'u',
            'character_id': 'gojo',
            'text': '【表情】皱眉满脸不认同',
            'source_event_id': 'defer-sticker',
            'defer': True,
            'sticker': {'id': 'frown_01', 'desc': '皱眉满脸不认同'},
        }
        with patch.object(route_chat, 'save_user_short_memory_once',
                          user_memory.save_user_short_memory_once), \
                patch.object(route_chat, 'get_character', return_value={'voice_id': 'v'}), \
                patch.object(route_chat, 'get_temporal_snapshot', return_value={}), \
                patch('reply_availability.check_reply_availability', side_effect=avail), \
                patch.object(route_chat, '_create_json', side_effect=explode), \
                patch.object(route_chat, '_provider_create', side_effect=explode):
            response = asyncio.run(route_chat.chat_text(payload))
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 202, body)
        self.assertEqual(order, [False, True])
        rows = self.sql(
            '''SELECT kind, extra FROM chat_log WHERE client_msg_id=%s''',
            ('defer-sticker',))
        self.assertEqual(len(rows), 1, rows)
        kind, extra = rows[0]
        if not isinstance(extra, dict):
            extra = json.loads(extra or '{}')
        self.assertEqual(kind, 'sticker')
        self.assertEqual(extra['sticker_id'], 'frown_01')


class _FlushHelpers:
    def __init__(self, sticker_id):
        self.sticker_id = sticker_id

    def _turn_context(self, *_args, **_kwargs):
        return {}, []

    def _history_plus_current(self, messages, _trigger):
        return messages

    def _generate_or_none(self, *_args, **_kwargs):
        return {
            'emotion': '平静',
            'sticker_id': self.sticker_id,
            'messages': [{'jp': '見た', 'zh': '看到了'}],
        }, None

    def _finalize_committed(self, result):
        return '平静', list(result.get('messages') or [])

    def _commit_offline_state(self, *_args, **_kwargs):
        return None


def _flush_bundle():
    return {
        'id': 9, 'user_id': 'u', 'character_id': 'gojo',
        'pending_text': '在吗', 'last_source_event_id': 'e1',
        'reply_state': 'free', 'inbox_reason': 'batching', 'fail_count': 0,
    }


class FlushStickerTests(unittest.TestCase):
    def _generate(self, sticker_id='smile_01', **patches):
        import sticker_registry
        helpers = _FlushHelpers(sticker_id)
        stack = [
            patch.object(delayed_reply, 'assistant_already_committed', return_value=False),
            patch.object(delayed_reply, '_persist_pending_user_messages'),
            patch.object(delayed_reply, 'format_pending_bundle_context', return_value='ctx'),
            patch.object(delayed_reply, '_apply_promise_effects'),
            patch('characters.get_character', return_value={'voice_id': 'v', 'name': '五条'}),
            patch('temporal_awareness.get_temporal_snapshot', return_value={}),
            patch('temporal_awareness.record_turn'),
            patch('prompt.build_system_blocks', return_value=[]),
            patch('schedule_transition.validate_generated_schedule_reply',
                  return_value=(None, {})),
            patch('schedule_transition.commit_generated_schedule_intent',
                  return_value={'ok': True, 'noop': True}),
            patch('tts.tts_to_b64', return_value=''),
            patch('user_memory.get_short_memory', return_value=[]),
            patch('user_memory.commit_visible_assistant_message'),
            patch('user_memory.update_chat_days', return_value=1),
            patch('proactive_msg.add_proactive_msg'),
            patch('behavior_evidence.record_reply_cycle'),
            patch.object(sticker_registry, 'get',
                         side_effect=patches.get('get', lambda sid: {
                             'id': sid, 'desc': '微笑'} if sid == 'smile_01' else None)),
            patch.object(sticker_registry, 'usable_by',
                         side_effect=patches.get('usable_by', lambda sid, actor: (
                             sid == 'smile_01' and actor == 'character'))),
            patch.object(sticker_registry, 'signed_url',
                         side_effect=patches.get('signed_url', lambda sid: f'https://signed.example/{sid}')),
            patch('db_chatlog.recent_assistant_sticker_turns',
                  side_effect=patches.get('history', lambda *_a, **_k: [])),
        ]
        if not patches.get('use_real_enabled'):
            stack.append(patch.object(
                sticker_registry, 'is_enabled',
                return_value=patches.get('enabled', True)))
        if 'load_manifest' in patches:
            stack.append(patch.object(
                sticker_registry, 'load_manifest', return_value=patches['load_manifest']))
        for item in stack:
            item.start()
            self.addCleanup(item.stop)
        return delayed_reply.generate_delayed_chat_reply(
            _flush_bundle(), helpers=helpers, push=False)

    def test_flush_generation_returns_a_signed_sticker(self):
        result = self._generate()
        self.assertTrue(result['ok'])
        self.assertEqual(result['messages'][0]['zh'], '看到了')
        self.assertEqual(result['sticker'], {
            'id': 'smile_01', 'desc': '微笑', 'url': 'https://signed.example/smile_01',
        })

    def test_second_flush_is_inside_the_sticker_cooldown(self):
        turns = iter(([], [{'smile_01'}]))
        first = self._generate(history=lambda *_a, **_k: next(turns))
        second = self._generate(history=lambda *_a, **_k: next(turns))
        self.assertIn('sticker', first)
        self.assertNotIn('sticker', second)
        self.assertTrue(second['ok'])
        self.assertEqual(second['messages'][0]['jp'], '見た')

    def test_character_cannot_send_a_user_only_sticker(self):
        result = self._generate(usable_by=lambda _sid, _actor: False)
        self.assertTrue(result['ok'])
        self.assertNotIn('sticker', result)
        self.assertEqual(result['full_jp'], '見た')

    def test_missing_catalog_or_signed_url_keeps_the_text(self):
        empty = {'version': 0, 'prefix': '', 'items': [], 'by_id': {}}
        missing = self._generate(use_real_enabled=True, load_manifest=empty)
        self.assertTrue(missing['ok'])
        self.assertNotIn('sticker', missing)
        self.assertEqual(missing['full_jp'], '見た')
        broken = self._generate(signed_url=Mock(side_effect=RuntimeError('r2 down')))
        self.assertTrue(broken['ok'])
        self.assertEqual(broken['messages'][0]['zh'], '看到了')
        self.assertNotIn('sticker', broken)

    def test_stickers_disabled_omits_the_key(self):
        catalog = {
            'version': 1, 'prefix': 'stickers',
            'items': [{'id': 'smile_01', 'desc': '微笑'}],
            'by_id': {'smile_01': {'id': 'smile_01', 'desc': '微笑'}},
        }
        with patch.dict(os.environ, {'STICKERS_ENABLED': 'false'}), \
                patch('media_storage.is_configured', return_value=True):
            result = self._generate(use_real_enabled=True, load_manifest=catalog)
        self.assertTrue(result['ok'])
        self.assertNotIn('sticker', result)
        self.assertEqual(result['messages'][0]['jp'], '見た')

    def test_flush_http_passes_sticker_and_omits_the_key_when_absent(self):
        import route_chat
        sticker = {'id': 'smile_01', 'desc': '微笑', 'url': 'https://signed.example/smile_01'}
        with patch('delayed_reply.flush_batching_reply', return_value={
                'ok': True, 'emotion': '平静',
                'messages': [{'jp': '見た', 'zh': '看到了'}],
                'turn_id': 't1', 'source_event_id': 'e1', 'full_jp': '見た',
                'sticker': sticker}):
            present = asyncio.run(route_chat.chat_flush(
                {'user_id': 'u', 'character_id': 'gojo'}))
        with patch('delayed_reply.flush_batching_reply', return_value={
                'ok': True, 'emotion': '平静',
                'messages': [{'jp': '見た', 'zh': '看到了'}],
                'turn_id': 't1', 'full_jp': '見た'}):
            absent = asyncio.run(route_chat.chat_flush(
                {'user_id': 'u', 'character_id': 'gojo'}))
        self.assertEqual(json.loads(present.body)['sticker'], sticker)
        self.assertNotIn('sticker', json.loads(absent.body))


_EVENT_ID_RE = re.compile(r'^delayed_reply:\d+:\d+$')


class _ReplyHelpers:
    def __init__(self, bubbles):
        self.bubbles = bubbles

    def _turn_context(self, *_args, **_kwargs):
        return {}, []

    def _history_plus_current(self, messages, _trigger):
        return messages

    def _generate_or_none(self, *_args, **_kwargs):
        return {'emotion': '平静', 'messages': self.bubbles}, None

    def _finalize_committed(self, result):
        if not result:
            return None, None
        return '平静', list(result.get('messages') or [])

    def _commit_offline_state(self, *_args, **_kwargs):
        return None


def _client_segment_id(seg, assistant_turn_id, index):
    """Same precedence as app/chat/[id].tsx assistantSegmentId. Not a frontend edit."""
    event_id = str((seg or {}).get('event_id') or '').strip()
    if event_id:
        return event_id
    turn_id = str(assistant_turn_id or '').strip()
    if turn_id:
        return f'{turn_id}:{index}'
    return f'local_{index}'


class FlushReturnedEventIdTests(unittest.TestCase):
    def _bubbles(self):
        return [
            {'jp': 'まだ待ってたの？偉いね、犬みたい。', 'zh': '还在等啊'},
            {'jp': '僕は今ドトールで昼飯。', 'zh': '我在doutor吃午饭'},
        ]

    def _patches(self, commits, proactive):
        def capture_commit(*_args, **kwargs):
            commits.append(kwargs)

        def capture_proactive(*_args, **kwargs):
            proactive.append(kwargs)
            return (len(proactive), NOW)

        return [
            patch.object(delayed_reply, 'assistant_already_committed', return_value=False),
            patch.object(delayed_reply, '_persist_pending_user_messages'),
            patch.object(delayed_reply, '_apply_promise_effects'),
            patch.object(delayed_reply, '_precheck_pending_bundle_sources',
                         side_effect=lambda bundle: bundle),
            patch('characters.get_character', return_value={'voice_id': 'v', 'name': '五条'}),
            patch('temporal_awareness.get_temporal_snapshot', return_value={}),
            patch('temporal_awareness.record_turn'),
            patch('prompt.build_system_blocks', return_value=[]),
            patch('schedule_transition.validate_generated_schedule_reply',
                  return_value=(None, {})),
            patch('schedule_transition.commit_generated_schedule_intent',
                  return_value={'ok': True, 'noop': True}),
            patch('tts.tts_to_b64', return_value=''),
            patch('user_memory.get_short_memory', return_value=[]),
            patch('user_memory.commit_visible_assistant_message',
                  side_effect=capture_commit),
            patch('user_memory.update_chat_days', return_value=1),
            patch('proactive_msg.add_proactive_msg', side_effect=capture_proactive),
            patch('behavior_evidence.record_reply_cycle'),
            patch('push_notify.push_to_user'),
        ]

    def _flush(self, *, push_via_worker=False):
        commits = []
        proactive = []
        helpers = _ReplyHelpers(self._bubbles())
        started = []
        for item in self._patches(commits, proactive):
            item.start()
            started.append(item)
            self.addCleanup(item.stop)
        if push_via_worker:
            bundle = {
                'id': 228, 'user_id': 'u', 'character_id': 'gojo',
                'pending_text': 'まだ待ってた', 'last_source_event_id': 'e228',
                'reply_state': 'free', 'inbox_reason': 'batching', 'fail_count': 0,
            }
            result = delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=helpers, push=True)
            return result, commits, proactive
        store = PhoneCheckStore()
        store.next_id = 228
        with patch.object(db_schedule, 'get_conn', lambda: _conn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
            db_schedule.decide_phone_check(
                'gojo', 'u', NOW, None,
                source_event_id='e228', pending_text='まだ待ってた',
                event_meta={'kind': 'text', 'source_event_id': 'e228'},
                batching=True)

            def generate_fn(bundle, push=True):
                self.assertIs(push, False)
                return delayed_reply.generate_delayed_chat_reply(
                    bundle, helpers=helpers, push=push)

            result = delayed_reply.flush_batching_reply(
                'u', 'gojo', now=NOW, generate_fn=generate_fn)
        self.assertEqual(result.get('id'), 228)
        return result, commits, proactive

    def test_flush_messages_echo_the_committed_three_part_event_id(self):
        result, commits, proactive = self._flush()
        messages = result['messages']
        self.assertEqual(len(messages), 2)
        self.assertEqual(len(commits), 2)
        for index, msg in enumerate(messages):
            expected = f'delayed_reply:228:{index}'
            self.assertEqual(msg['event_id'], expected)
            self.assertRegex(msg['event_id'], _EVENT_ID_RE)
            self.assertNotRegex(msg['event_id'], r'^delayed_reply:\d+:\d+:\d+$')
            self.assertEqual(msg['segment_index'], index)
            self.assertEqual(commits[index]['event_id'], expected)
            self.assertEqual(commits[index]['metadata']['assistant_turn_id'],
                             'delayed_reply:228:0')
            self.assertEqual(commits[index]['metadata']['segment_index'], index)
            self.assertEqual(proactive[index]['event_id'], expected)
            self.assertEqual(proactive[index]['assistant_turn_id'], 'delayed_reply:228:0')
            self.assertEqual(proactive[index]['segment_index'], index)
        self.assertEqual(result['turn_id'], 'delayed_reply:228:0')

    def test_worker_push_keeps_stored_event_ids(self):
        result, commits, proactive = self._flush(push_via_worker=True)
        self.assertTrue(result['ok'])
        self.assertEqual(len(commits), 2)
        for index in range(2):
            expected = f'delayed_reply:228:{index}'
            self.assertEqual(commits[index]['event_id'], expected)
            self.assertEqual(commits[index]['metadata'], {
                'proactive_kind': 'delayed_reply',
                'phone_check_id': 228,
                'assistant_turn_id': 'delayed_reply:228:0',
                'segment_index': index,
            })
            self.assertEqual(proactive[index]['event_id'], expected)
            self.assertEqual(proactive[index]['assistant_turn_id'], 'delayed_reply:228:0')
            self.assertEqual(proactive[index]['segment_index'], index)
            self.assertEqual(result['messages'][index]['segment_index'], index)
        import push_notify
        push_notify.push_to_user.assert_called_once()

    def test_already_delivered_returns_empty_messages(self):
        bundle = {
            'id': 228, 'user_id': 'u', 'character_id': 'gojo',
            'pending_text': 'まだ', 'pending_count': 1,
        }
        with patch.object(delayed_reply, 'assistant_already_committed', return_value=True), \
                patch('characters.get_character',
                      return_value={'name': '五条', 'voice_id': 'v'}), \
                patch('user_memory.commit_visible_assistant_message') as commit:
            result = delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=_ReplyHelpers(self._bubbles()), push=False)
        self.assertTrue(result['ok'])
        self.assertTrue(result['already_delivered'])
        self.assertEqual(result['messages'], [])
        self.assertEqual(result['turn_id'], 'delayed_reply:228:0')
        commit.assert_not_called()


@unittest.skipUnless(
    os.getenv('COGNITIVE_TEST_PGLITE'),
    'set COGNITIVE_TEST_PGLITE to the local test-only package')
class FlushAppendDedupTests(unittest.TestCase):
    from tests import test_private_source_first as fixture
    setUpClass = classmethod(fixture.PrivateSourceFirstTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PrivateSourceFirstTests.tearDownClass.__func__)
    sql = fixture.PrivateSourceFirstTests.sql
    tearDown = fixture.PrivateSourceFirstTests.tearDown

    def setUp(self):
        self.fixture.PrivateSourceFirstTests.setUp(self)

    def _assistant_ids(self, phone_check_id):
        rows = self.sql(
            '''SELECT client_msg_id FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND client_msg_id LIKE %s
                 AND deleted_at IS NULL
               ORDER BY id''',
            ('u', 'gojo', f'delayed_reply:{phone_check_id}:%'))
        return [row[0] for row in rows]

    def test_flush_then_client_append_does_not_double_chat_log(self):
        import route_chatlog
        store = PhoneCheckStore()
        store.next_id = 228
        helpers = _ReplyHelpers([
            {'jp': 'まだ待ってたの？偉いね、犬みたい。', 'zh': '还在等啊'},
            {'jp': '僕は今ドトールで昼飯。', 'zh': '我在doutor吃午饭'},
        ])
        patches = [
            patch.object(delayed_reply, '_persist_pending_user_messages'),
            patch.object(delayed_reply, '_apply_promise_effects'),
            patch.object(delayed_reply, '_precheck_pending_bundle_sources',
                         side_effect=lambda bundle: bundle),
            patch.object(db_schedule, 'get_conn', lambda: _conn(store)),
            patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD),
            patch('characters.get_character', return_value={'voice_id': 'v', 'name': '五条'}),
            patch('temporal_awareness.get_temporal_snapshot', return_value={}),
            patch('temporal_awareness.record_turn'),
            patch('prompt.build_system_blocks', return_value=[]),
            patch('schedule_transition.validate_generated_schedule_reply',
                  return_value=(None, {})),
            patch('schedule_transition.commit_generated_schedule_intent',
                  return_value={'ok': True, 'noop': True}),
            patch('tts.tts_to_b64', return_value=''),
            patch('user_memory.get_short_memory', return_value=[]),
            patch('user_memory.update_chat_days', return_value=1),
            patch('proactive_msg.add_proactive_msg', return_value=(1, NOW)),
            patch('behavior_evidence.record_reply_cycle'),
            patch('push_notify.push_to_user'),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        db_schedule.decide_phone_check(
            'gojo', 'u', NOW, None,
            source_event_id='e228', pending_text='まだ待ってた',
            event_meta={'kind': 'text', 'source_event_id': 'e228'},
            batching=True)

        def generate_fn(bundle, push=True):
            self.assertIs(push, False)
            return delayed_reply.generate_delayed_chat_reply(
                bundle, helpers=helpers, push=push)

        result = delayed_reply.flush_batching_reply(
            'u', 'gojo', now=NOW, generate_fn=generate_fn)
        self.assertTrue(result.get('ok'), result)
        messages = result['messages']
        self.assertEqual(len(messages), 2)
        committed = self._assistant_ids(228)
        self.assertEqual(committed, [
            'delayed_reply:228:0',
            'delayed_reply:228:1',
        ])
        client_messages = []
        for index, seg in enumerate(messages):
            client_messages.append({
                'client_msg_id': _client_segment_id(seg, result.get('turn_id'), index),
                'role': 'gojo',
                'text': seg['jp'],
                'subtitle': seg.get('zh') or '',
                'emotion': result.get('emotion') or '',
                'kind': 'text',
                'has_audio': bool(seg.get('audio_b64')),
            })
        response = asyncio.run(route_chatlog.append_chatlog({
            'user_id': 'u',
            'chat_id': 'gojo',
            'messages': client_messages,
        }))
        body = json.loads(response.body)
        self.assertTrue(body['ok'], body)
        self.assertEqual(body['written'], 0)
        self.assertEqual(self._assistant_ids(228), committed)
        self.assertEqual(len(committed), len(messages))
        for client_id in (item['client_msg_id'] for item in client_messages):
            self.assertRegex(client_id, _EVENT_ID_RE)


if __name__ == '__main__':
    unittest.main()
