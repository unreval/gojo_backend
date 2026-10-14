"""Offline endpoint assembly audits using real system prompt rendering."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch
import asyncio
import importlib
import importlib.util
from pathlib import Path
import unittest

import context_layer
import delayed_reply
import prompt
import route_chat
import temporal_awareness
from temporal_projection_offline import denied


NOW = datetime(2026, 10, 14, 14, 0, tzinfo=timezone(timedelta(hours=8)))
ROOT = Path(__file__).resolve().parents[1]


def snapshot(now=NOW):
    return dict(now_utc=now.astimezone(timezone.utc), now_local=now, has_history=False)


class SystemClockAuditTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.pack = context_layer.ChatContextPack(
            messages=[{'role': 'assistant', 'content': '合成的旧回复'}],
            recall_ready=True, support_ready=True, recall_result={},
            temporal_snapshot=snapshot())
        for name, value in (('get_character', {'core_prompt': 'SYNTHETIC-CORE'}),
                            ('load_canon_lock', ''), ('get_first_interaction_days', 1),
                            ('_accounts_block', '')):
            self.stack.enter_context(patch.object(prompt, name, return_value=value))
        self.stack.enter_context(patch.object(context_layer, 'revalidate_pack_blocks',
            return_value=dict.fromkeys(('cognitive_prompt_text', 'pinned_prompt_text',
                                       'summary_prompt_text', 'episode_prompt_text'), '')))
        self.stack.enter_context(patch('memory_authority.filter_recall_authority',
                                      return_value={}))
        self.stack.enter_context(patch('db_schedule.format_world_prompt',
                                      return_value=('', {})))
        self.stack.enter_context(patch('builtins.print'))

    def assert_system_clock(self, blocks, now=NOW):
        text = '\n'.join(block['text'] for block in blocks)
        self.assertIn('【现在的时间——必须遵守】', text)
        self.assertIn('当前时间：' + now.strftime('%Y年%m月%d日 %H:%M'), text)
        self.assertIn('TEMPORAL SNAPSHOT', text)
        self.assertNotIn('当前时间：', blocks[0]['text'] + blocks[1]['text'])

    def test_proactive_system_contains_frozen_current_time(self):
        self.check_proactive(route_chat.chat_proactive,
                             {'task_title': '合成提醒', 'mode': 'remind'})

    def test_voice_proactive_system_contains_frozen_current_time(self):
        self.check_proactive(route_chat.chat_voice_proactive, {'mode': 'greeting'})

    def check_proactive(self, handler, extra):
        with patch.object(route_chat, 'get_character',
                          return_value={'core_prompt': 'SYNTHETIC-CORE', 'voice_id': None}), \
                patch.object(route_chat, 'get_temporal_snapshot', return_value=snapshot()), \
                patch.object(route_chat, 'get_short_memory', return_value=[]), \
                patch.object(route_chat, '_turn_context', return_value=(self.pack, self.pack.messages)), \
                patch.object(route_chat, 'build_system_blocks', wraps=prompt.build_system_blocks), \
                patch.object(route_chat, '_generate_or_none', return_value=(None, None)) as generate, \
                patch('db_schedule.get_current_world_state', return_value={}):
            response = asyncio.run(handler({'user_id': 'u', 'character_id': 'gojo', **extra}))
        self.assertEqual(response.status_code, 502)  # Stop before delivery/writes.
        self.assert_system_clock(generate.call_args.args[2])
        self.assertNotIn('【后端当前时间事实】', str(generate.call_args.args[3]))

    def test_delayed_system_contains_time_but_context_takes_a_second_snapshot(self):
        clocks = Mock(side_effect=[snapshot(), snapshot(NOW + timedelta(minutes=1))])

        def fake_context(*_args, **kwargs):
            self.pack.temporal_snapshot = kwargs['temporal_snapshot']
            return self.pack

        with patch('characters.get_character', return_value={'core_prompt': 'SYNTHETIC-CORE'}), \
                patch.object(delayed_reply, 'assistant_already_committed', return_value=False), \
                patch.object(delayed_reply, '_persist_pending_user_messages'), \
                patch.object(temporal_awareness, 'get_temporal_snapshot', clocks), \
                patch.object(route_chat, 'get_temporal_snapshot', clocks), \
                patch.object(context_layer, 'build_chat_context', side_effect=fake_context), \
                patch.object(route_chat, '_generate_or_none', return_value=(None, None)) as generate:
            result = delayed_reply.generate_delayed_chat_reply({
                'id': 7, 'user_id': 'u', 'character_id': 'gojo',
                'pending_text': '合成积压消息', 'last_source_event_id': 'pending-synthetic'},
                helpers=route_chat)
        self.assertEqual(result, {'ok': False, 'reason': 'generation_failed'})
        self.assertEqual(clocks.call_count, 2)
        self.assertEqual(self.pack.temporal_snapshot['now_utc'],
                         (NOW + timedelta(minutes=1)).astimezone(timezone.utc))
        self.assert_system_clock(generate.call_args.args[2], NOW)
        self.assertNotIn('【后端当前时间事实】', str(generate.call_args.args[3]))

    def test_voice_stream_system_contains_frozen_current_time_without_iteration(self):
        import anthropic
        responses = importlib.import_module('fastapi.responses')
        captured = []

        def rendered(*args, **kwargs):
            blocks = prompt.build_system_blocks(*args, **kwargs)
            captured.append(blocks)
            return blocks

        with patch.object(anthropic, 'AsyncAnthropic', create=True,
                          return_value=SimpleNamespace(messages=SimpleNamespace(stream=denied))), \
                patch.object(responses, 'StreamingResponse', create=True,
                             side_effect=lambda content, **kwargs: SimpleNamespace(content=content, **kwargs)):
            spec = importlib.util.spec_from_file_location(
                'voice_stream_system_audit', ROOT / 'gojo_backend' / 'route_voice_stream.py')
            stream = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(stream)
        with patch.object(stream, 'get_character',
                          return_value={'core_prompt': 'SYNTHETIC-CORE', 'voice_id': None}), \
                patch.object(stream, 'get_temporal_snapshot', return_value=snapshot()), \
                patch.object(stream, 'save_user_short_memory_once'), \
                patch.object(stream, 'build_system_blocks', side_effect=rendered), \
                patch('reply_availability.check_reply_availability',
                      return_value={'can_reply': True}), \
                patch('db_read_receipt.mark_immediate_seen'), \
                patch.object(context_layer, 'build_chat_context', return_value=self.pack), \
                patch('db_generation_receipt.assign_source_event_id',
                      return_value=('stream-synthetic', False)):
            response = asyncio.run(stream.chat_voice_stream({
                'user_id': 'u', 'character_id': 'gojo', 'text': '合成通话消息'}))
        self.assertEqual(response.media_type, 'application/x-ndjson')
        self.assertEqual(len(captured), 1)
        self.assert_system_clock(captured[0])
        # The async generator is deliberately not consumed: no vendor/TTS call.


if __name__ == '__main__':
    unittest.main()
