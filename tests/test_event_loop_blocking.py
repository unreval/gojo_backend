# -*- coding: utf-8 -*-
"""Sync chat handlers must not occupy the event loop.

Acceptance for the event-loop blocking change:
the three handlers are plain functions, voice stream stays async,
and a blocked chat_text request does not hold up another endpoint.
"""
import inspect
import json
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
TESTS = ROOT / 'tests'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import route_chat  # noqa: E402
import route_image  # noqa: E402
import route_voice_stream  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


# Captured from origin/main @ 5d3f4b5, where chat_text was still async.
# Same fixture as DeferredRouteTests.test_without_defer_the_availability_call_stays_unchanged.
_PRE_CHANGE_BUSY_BODY = {
    'busy': True,
    'seen': False,
    'can_reply': False,
    'reply_state': 'hard_busy',
    'activity': '睡',
    'location': '',
    'until': '08:00',
    'free_at': None,
    'phone_check_id': 4,
    'pending_phone_check_count': 1,
    'seen_at': None,
    'next_phone_check_at': None,
    'total_days': 1,
}


class HandlerKindTests(unittest.TestCase):
    def test_text_flush_and_image_are_not_coroutine_functions(self):
        self.assertFalse(inspect.iscoroutinefunction(route_chat.chat_text))
        self.assertFalse(inspect.iscoroutinefunction(route_chat.chat_flush))
        self.assertFalse(inspect.iscoroutinefunction(route_image.chat_image))

    def test_voice_stream_is_still_a_coroutine_function(self):
        self.assertTrue(inspect.iscoroutinefunction(route_voice_stream.chat_voice_stream))


class BusyReplyUnchangedTests(unittest.TestCase):
    def test_without_defer_busy_json_matches_the_async_handler(self):
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
            response = route_chat.chat_text(payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(json.loads(response.body), _PRE_CHANGE_BUSY_BODY)
        self.assertNotIn('batching', captured)


class EventLoopOverlapTests(unittest.TestCase):
    def test_blocked_chat_text_does_not_block_health(self):
        """Model call waits; /health on the same app returns first.

        The app mounts the real chat router. /health is a local async route
        rather than gojo_server.health, because importing gojo_server starts
        workers and the database. Starlette still runs the sync chat_text
        endpoint in its threadpool, which is the scheduling this change relies on.
        """
        app = FastAPI()
        app.include_router(route_chat.router)

        @app.get('/health')
        async def health():
            return {'status': 'ok'}

        entered = threading.Event()
        release = threading.Event()

        def block_model(*_args, **_kwargs):
            entered.set()
            release.wait(timeout=5)
            return ('', Mock())

        def gate(user_id, character_id, source_event_id, endpoint):
            return {
                'action': 'generate',
                'claim_token': 'tok',
                'source_event_id': source_event_id,
            }, None

        holder = {}
        worker = None

        def first_request(client):
            try:
                holder['response'] = client.post('/chat/text', json={
                    'user_id': 'u',
                    'character_id': 'gojo',
                    'text': '你好',
                    'source_event_id': 'block-1',
                })
            except Exception as exc:
                holder['error'] = exc

        patches = (
            patch.object(route_chat, 'get_character', return_value={'voice_id': 'v'}),
            patch.object(route_chat, 'save_user_short_memory_once', return_value=True),
            patch.object(route_chat, 'get_temporal_snapshot', return_value={}),
            patch('reply_availability.check_reply_availability', return_value={'can_reply': True}),
            patch.object(route_chat, 'update_chat_days', return_value=1),
            patch('db_read_receipt.mark_immediate_seen', return_value=None),
            patch.object(route_chat, '_gate_chat_generation', side_effect=gate),
            patch.object(route_chat, 'get_short_memory', return_value=[]),
            patch.object(route_chat, '_turn_context', return_value=(None, [])),
            patch.object(route_chat, 'build_system_blocks', return_value=[]),
            patch.object(route_chat, 'log_cache_usage'),
            patch.object(route_chat, '_create_json', side_effect=block_model),
        )
        for item in patches:
            item.start()
        try:
            with TestClient(app) as client:
                worker = threading.Thread(target=first_request, args=(client,))
                worker.start()
                self.assertTrue(entered.wait(timeout=5), 'model stub was not reached')
                started_at = time.perf_counter()
                health = client.get('/health')
                elapsed = time.perf_counter() - started_at
                self.assertTrue(worker.is_alive())
                self.assertEqual(health.status_code, 200)
                self.assertEqual(health.json(), {'status': 'ok'})
                self.assertLess(elapsed, 0.5)
        finally:
            release.set()
            if worker is not None:
                worker.join(timeout=5)
            for item in reversed(patches):
                item.stop()
        self.assertNotIn('error', holder)
        self.assertFalse(worker.is_alive())
