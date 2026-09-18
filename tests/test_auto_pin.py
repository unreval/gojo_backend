# -*- coding: utf-8 -*-
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import auto_pin  # noqa: E402
import context_layer  # noqa: E402


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


if __name__ == '__main__':
    unittest.main()
