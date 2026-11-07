# -*- coding: utf-8 -*-
"""Live multi-message batching reuses the phone-check inbox.

REG-015: /chat/flush must not consult the delayed-reply hourly quota.
"""
import asyncio
import json
import os
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
        self.assertEqual(row['start_time'], '10:00')
        self.assertEqual(row['sched_date'], NOW.date())
        self.assertEqual(row['pending_count'], 3)
        self.assertEqual(row['pending_text'], '一\n二\n三')
        self.assertEqual(row['next_phone_check_at'], NOW + timedelta(seconds=60))

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


class DeferredRouteTests(unittest.TestCase):
    def test_defer_returns_202_without_a_model_call(self):
        import route_chat
        order = []

        def avail(*_args, **kwargs):
            order.append(('avail', kwargs.get('batching')))
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
        self.assertEqual(order, ['save', ('avail', True)])

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
            order.append(kwargs.get('batching'))
            return {'can_reply': False, 'pending_count': 1, 'opportunity_id': 1}

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
        self.assertEqual(order, [True])
        rows = self.sql(
            '''SELECT kind, extra FROM chat_log WHERE client_msg_id=%s''',
            ('defer-sticker',))
        self.assertEqual(len(rows), 1, rows)
        kind, extra = rows[0]
        if not isinstance(extra, dict):
            extra = json.loads(extra or '{}')
        self.assertEqual(kind, 'sticker')
        self.assertEqual(extra['sticker_id'], 'frown_01')


if __name__ == '__main__':
    unittest.main()
