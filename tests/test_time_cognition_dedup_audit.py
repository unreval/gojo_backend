"""Missing event-ID regressions, run unchanged against current and HEAD code.

The voice handlers generate IDs before context construction. Their None-ID
cases inject that exact condition at the context adapter boundary, then forward
to the real implementation. All history assembly and deduplication stay real.
"""
import asyncio
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import unittest

import context_layer
import db_chatlog
import raw_events
import route_chat
import user_memory


ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 14, 12, 0, tzinfo=ZONE)
CURRENT_TEXT = '这条当前原文已经在聊天账本里了'
DEDUP_OBSERVATIONS = []


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz) if tz is not None else NOW.replace(tzinfo=None)


def synthetic_chat_log():
    return [
        dict(event_id='previous-user', role='user', content='上一轮的问题',
             timestamp=NOW - timedelta(minutes=4), metadata={}, status='active'),
        dict(event_id='previous-assistant', role='assistant', content='前の返事だよ。',
             timestamp=NOW - timedelta(minutes=2), metadata={}, status='active'),
        dict(event_id='already-written-current', role='user', content=CURRENT_TEXT,
             timestamp=NOW, metadata={}, status='active'),
    ]


def body_occurrences(messages, text):
    count = 0
    for message in messages:
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if isinstance(content, str):
            count += content.count(text)
        elif isinstance(content, list):
            count += sum(block.get('text', '').count(text)
                         for block in content if block.get('type') == 'text')
    return count


class DedupAuditTests(unittest.TestCase):
    def run_missing_id_case(self, handler_name, path):
        rows = synthetic_chat_log()
        original = deepcopy(rows)
        frozen = dict(now_utc=NOW.astimezone(timezone.utc), now_local=NOW,
                      latest_user_at=NOW, has_history=True)
        actual_turn_context = route_chat._turn_context
        calls = []
        sent = []

        def missing_identity_adapter(*args, **kwargs):
            original_id = kwargs.get('current_event_id')
            if handler_name == 'chat_story':
                self.assertIsNone(original_id)
            else:
                # Voice requests without source_event_id generate a fresh ID.
                # Force the requested None condition at the adapter boundary.
                self.assertTrue(original_id)
            forwarded = dict(kwargs, current_event_id=None)
            calls.append(dict(handler_id=original_id,
                              forwarded_id=forwarded['current_event_id']))
            return actual_turn_context(*args, **forwarded)

        def fake_generator(_model, _max_tokens, _system, messages, **_kwargs):
            sent.extend(deepcopy(messages))
            # Stop before any assistant persistence, side effect, or TTS.
            return None, None

        with ExitStack() as stack:
            def adapter(target, name, **kwargs):
                return stack.enter_context(patch.object(target, name, **kwargs))

            hot_reader = adapter(raw_events, 'get_hot_candidate_events',
                                 side_effect=lambda *_a, **_k:
                                 deepcopy(rows) if path == 'main' else [])
            recent_reader = adapter(raw_events, 'get_recent_events',
                                    side_effect=lambda *_a, **_k: deepcopy(rows))
            adapter(raw_events, 'deleted_event_ids', return_value=[])
            adapter(db_chatlog, 'assistant_turn_facts', return_value={})
            adapter(user_memory, '_fetch_short_memory_cache', return_value=[])
            # HEAD fallback still calls datetime.now; freeze that same clock.
            adapter(user_memory, 'datetime', new=FrozenDatetime)
            adapter(context_layer, 'maybe_store_spill_summary')
            adapter(context_layer, 'list_rolling_summaries', return_value=[])
            adapter(context_layer, 'list_pins', return_value=[])
            adapter(context_layer, '_support_items', return_value=([], ''))
            adapter(context_layer, '_build_recall_query_embedding', return_value=None)
            stack.enter_context(patch('smart_recall.two_level_recall', return_value=None))
            stack.enter_context(patch('auto_pin.maybe_auto_pin'))
            adapter(route_chat, 'get_character', return_value={'voice_id': 'fake'})
            adapter(route_chat, 'get_temporal_snapshot', return_value=deepcopy(frozen))
            adapter(route_chat, 'update_chat_days', return_value=1)
            adapter(route_chat, '_gate_voice_inbound', return_value=(None, None))
            adapter(route_chat, '_turn_context', side_effect=missing_identity_adapter)
            adapter(route_chat, 'build_system_blocks', return_value=[])
            save = adapter(route_chat, 'save_user_short_memory_once', return_value=False)
            generate = adapter(route_chat, '_generate_or_none', side_effect=fake_generator)
            with patch('builtins.print'):
                response = asyncio.run(getattr(route_chat, handler_name)({
                    'user_id': 'synthetic-user', 'character_id': 'gojo',
                    'text': CURRENT_TEXT, 'source_event_id': None,
                }))
            self.assertEqual(response.status_code, 502)
            self.assertEqual(len(calls), 1)
            self.assertIsNone(calls[0]['forwarded_id'])
            generate.assert_called_once()
            save.assert_called_once()
            hot_reader.assert_called_once()
            if path == 'fallback':
                recent_reader.assert_called_once()
            else:
                recent_reader.assert_not_called()
            self.assertEqual(rows, original)

        count = body_occurrences(sent, CURRENT_TEXT)
        DEDUP_OBSERVATIONS.append(dict(
            handler=handler_name, path=path, current_event_id=None,
            voice_none_injected=handler_name != 'chat_story',
            occurrences=count, message_count=len(sent), messages=deepcopy(sent)))
        self.assertEqual(count, 1,
                         f'{handler_name}/{path}: current user body occurs {count} times')

    def test_story_main_current_already_in_chat_log_without_event_id(self):
        self.run_missing_id_case('chat_story', 'main')

    def test_story_fallback_current_already_in_chat_log_without_event_id(self):
        self.run_missing_id_case('chat_story', 'fallback')

    def test_voice_text_main_missing_context_event_id(self):
        self.run_missing_id_case('chat_voice_text', 'main')

    def test_voice_text_fallback_missing_context_event_id(self):
        self.run_missing_id_case('chat_voice_text', 'fallback')

    def test_voice_story_main_missing_context_event_id(self):
        self.run_missing_id_case('chat_voice_story', 'main')

    def test_voice_story_fallback_missing_context_event_id(self):
        self.run_missing_id_case('chat_voice_story', 'fallback')


class KnownEventIdentityTests(unittest.TestCase):
    def test_known_current_id_preserves_an_independent_old_same_text(self):
        rows = synthetic_chat_log()
        rows[0]['content'] = CURRENT_TEXT
        frozen = dict(now_utc=NOW.astimezone(timezone.utc), now_local=NOW,
                      latest_user_at=NOW, has_history=True)
        with patch.object(context_layer, 'maybe_store_spill_summary'), \
                patch.object(context_layer, 'list_rolling_summaries', return_value=[]), \
                patch.object(context_layer, 'list_pins', return_value=[]):
            pack = context_layer.assemble_from_events(
                deepcopy(rows), user_id='synthetic-user', character_id='gojo',
                current_event_id='already-written-current', user_message=CURRENT_TEXT,
                temporal_snapshot=frozen, include_recall=False,
                include_support=False, auto_pin_enabled=False)
            messages = route_chat._history_plus_current(pack.messages, CURRENT_TEXT)
        self.assertIn('previous-user', pack.recent_event_ids)
        self.assertNotIn('already-written-current', pack.recent_event_ids)
        self.assertEqual(body_occurrences(messages, CURRENT_TEXT), 2)


if __name__ == '__main__':
    unittest.main()
