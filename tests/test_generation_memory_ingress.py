"""Completed reply extraction must reference the canonical assistant event."""
import os
import sys
import unittest
from unittest.mock import Mock


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import generation_effects


class GenerationMemoryIngressTests(unittest.TestCase):
    def _ctx(self, endpoint='chat_text'):
        turn_id = generation_effects.assistant_turn_id_for(endpoint, 'user-1')
        return {
            'user_id': 'u',
            'character_id': 'gojo',
            'source_event_id': 'user-1',
            'endpoint': endpoint,
            'user_text': '宝宝，你接受这个称呼吗？',
            'full_jp': 'うん、その呼び方でいいよ。',
            'get_active_events_by_ids': Mock(return_value=[{
                'event_id': turn_id,
                'role': 'assistant',
                'content': 'うん、その呼び方でいいよ。',
            }]),
            'save_short_memory': Mock(),
            'enqueue_private_extraction': Mock(return_value=1),
        }

    def test_existing_canonical_reply_is_forwarded_as_evidence(self):
        for endpoint, expected in (
                ('chat_text', 'chat_reply:user-1'),
                ('chat_image', 'image_reply:user-1')):
            with self.subTest(endpoint=endpoint):
                ctx = self._ctx(endpoint)
                generation_effects.apply_private_extraction(ctx)
                kwargs = ctx['enqueue_private_extraction'].call_args.kwargs
                self.assertEqual(kwargs['source_event_id'], 'user-1')
                self.assertEqual(kwargs['assistant_event_id'], expected)
                ctx['save_short_memory'].assert_not_called()

    def test_extraction_running_before_reply_effect_repairs_source_first(self):
        ctx = self._ctx()
        events = []
        order = []

        def read(*_args):
            return list(events)

        def save(_user, role, content, _character, **kwargs):
            order.append('canonical_reply')
            events.append({
                'event_id': kwargs['source_event_id'],
                'role': role,
                'content': content,
            })

        ctx['get_active_events_by_ids'] = Mock(side_effect=read)
        ctx['save_short_memory'] = Mock(side_effect=save)
        ctx['enqueue_private_extraction'].side_effect = (
            lambda *_args, **_kwargs: order.append('extraction'))
        generation_effects.apply_private_extraction(ctx)
        self.assertEqual(order, ['canonical_reply', 'extraction'])
        self.assertEqual(ctx['save_short_memory'].call_count, 1)

    def test_failed_canonical_mirror_does_not_enqueue_unverifiable_reply(self):
        ctx = self._ctx()
        ctx['get_active_events_by_ids'].return_value = []
        with self.assertRaisesRegex(RuntimeError, 'canonical_assistant_event_unavailable'):
            generation_effects.apply_private_extraction(ctx)
        ctx['enqueue_private_extraction'].assert_not_called()

    def test_source_reader_failure_is_retryable_without_enqueue(self):
        ctx = self._ctx()
        ctx['get_active_events_by_ids'].side_effect = RuntimeError('source lookup failed')
        with self.assertRaisesRegex(RuntimeError, 'source lookup failed'):
            generation_effects.apply_private_extraction(ctx)
        ctx['save_short_memory'].assert_not_called()
        ctx['enqueue_private_extraction'].assert_not_called()

    def test_wrong_role_does_not_satisfy_assistant_evidence(self):
        ctx = self._ctx()
        ctx['get_active_events_by_ids'].return_value = [{
            'event_id': 'chat_reply:user-1', 'role': 'user', 'content': 'yes',
        }]
        with self.assertRaisesRegex(RuntimeError, 'canonical_assistant_event_unavailable'):
            generation_effects.apply_private_extraction(ctx)
        ctx['enqueue_private_extraction'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
