# -*- coding: utf-8 -*-
import os
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import auto_pin  # noqa: E402
import context_layer  # noqa: E402
import raw_events  # noqa: E402


class AutoPinTests(unittest.TestCase):
    def setUp(self):
        context_layer.use_memory_store(True)

    def tearDown(self):
        context_layer.use_memory_store(False)

    def test_explicit_decision_is_pinned(self):
        rows = auto_pin.maybe_auto_pin(
            'u', 'gojo', '就按这个方案做 Recall v2',
            source_event_ids=['e-decision'])
        self.assertTrue(rows)
        self.assertEqual(rows[0]['pin_type'], 'explicit_decision')
        pins = context_layer.list_pins('u', 'gojo')
        self.assertTrue(any('就按这个方案' in (p.get('text') or '') for p in pins))

    def test_current_task_is_pinned(self):
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '先把 Recall v2 做完再看别的',
            source_event_ids=['e-task'])
        pins = context_layer.list_pins('u', 'gojo')
        self.assertTrue(any(p.get('pin_type') == 'current_task' for p in pins))

    def test_pending_request_do_not_push(self):
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '改完先给我看，不要 push',
            source_event_ids=['e-nopush'])
        pins = context_layer.list_pins('u', 'gojo')
        self.assertTrue(any(p.get('pin_type') == 'pending_request' for p in pins))
        self.assertTrue(any('不要 push' in (p.get('text') or '')
                            or '不要push' in (p.get('text') or '').replace(' ', '')
                            for p in pins))

    def test_allow_push_supersedes_block(self):
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '不要 push', source_event_ids=['e1'])
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '现在 push', source_event_ids=['e2'])
        pins = context_layer.list_pins('u', 'gojo', status=None, limit=20)
        blocked = [
            p for p in context_layer._PINS.values()
            if p['user_id'] == 'u' and 'git-push-block' in p['pin_id']
        ]
        self.assertTrue(any(p.get('status') == 'superseded' for p in blocked))
        active = context_layer.list_pins('u', 'gojo', status='active')
        self.assertTrue(any('现在 push' in (p.get('text') or '') for p in active))

    def test_casual_bath_is_not_pinned(self):
        auto_pin.maybe_auto_pin('u', 'gojo', '我要洗澡', source_event_ids=['e-bath'])
        self.assertEqual(context_layer.list_pins('u', 'gojo'), [])

    def test_repeated_read_does_not_raise_priority(self):
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '就按这个方案', source_event_ids=['e1'])
        first = context_layer.list_pins('u', 'gojo')[0]
        p1 = first['priority']
        context_layer.list_pins('u', 'gojo')
        context_layer.list_pins('u', 'gojo')
        second = context_layer.list_pins('u', 'gojo')[0]
        self.assertEqual(second['priority'], p1)

    def test_default_context_pins_current_canonical_ids_and_ignores_assistant_history(self):
        now = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
        current_text = '就按这个方案'
        events = [
            {'event_id': 'assistant-old', 'role': 'assistant',
             'content': '不要 push', 'timestamp': now - timedelta(minutes=1)},
            {'event_id': 'canonical-1', 'role': 'user',
             'content': current_text, 'timestamp': now},
            {'event_id': 'canonical-2', 'role': 'user',
             'content': '继续', 'timestamp': now},
        ]
        with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
             patch.object(raw_events, 'get_hot_candidate_events', return_value=events), \
             patch.object(context_layer, '_support_items', return_value=([], '')):
            pack = context_layer.build_chat_context(
                'u', 'gojo', user_message=current_text, include_recall=False,
                current_event_id=('canonical-1', 'canonical-2'), now=now)
        self.assertFalse(pack.failed_closed)
        self.assertEqual(pack.recent_event_ids, ['assistant-old'])
        pins = list(context_layer._PINS.values())
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]['pin_type'], 'explicit_decision')
        self.assertEqual(pins[0]['text'], current_text)
        self.assertEqual(set(pins[0]['source_event_ids']),
                         {'canonical-1', 'canonical-2'})

    def test_disabled_context_auto_pin_does_not_call_writer_or_change_pins(self):
        now = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
        for old_text, new_text in [('现在 push', '不要 push'),
                                   ('不要 push', '现在 push')]:
            with self.subTest(old_text=old_text, new_text=new_text):
                context_layer.reset_memory_store()
                auto_pin.maybe_auto_pin(
                    'u', 'gojo', old_text, source_event_ids=['old-pending'], now=now)
                auto_pin.maybe_auto_pin(
                    'u', 'gojo', new_text, source_event_ids=['later-user'], now=now)
                before = deepcopy(context_layer._PINS)
                events = [
                    {'event_id': 'older-hot', 'role': 'user', 'content': '你好',
                     'timestamp': now - timedelta(minutes=1)},
                    {'event_id': 'old-pending', 'role': 'user', 'content': old_text,
                     'timestamp': now},
                ]
                with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
                     patch.object(raw_events, 'get_hot_candidate_events', return_value=events), \
                     patch.object(context_layer, '_support_items', return_value=([], '')), \
                     patch.object(auto_pin, 'maybe_auto_pin', wraps=auto_pin.maybe_auto_pin) as writer:
                    for _attempt in range(2):
                        pack = context_layer.build_chat_context(
                            'u', 'gojo', user_message=old_text, include_recall=False,
                            current_event_id=('old-pending',), now=now,
                            auto_pin_enabled=False)
                        self.assertEqual(pack.recent_event_ids, ['older-hot'])
                        self.assertEqual(context_layer._PINS, before)
                writer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
