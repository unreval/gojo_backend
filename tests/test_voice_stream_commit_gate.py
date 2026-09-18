import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def load_source(name, modules):
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            name + '_under_test', os.path.join(BACKEND, name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class FakeStream:
    def __init__(self, text):
        self._text = text

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    @property
    def text_stream(self):
        async def gen():
            yield self._text
        return gen()


class VoiceStreamCommitGateTests(unittest.TestCase):
    def setUp(self):
        router = Mock()
        router.post.side_effect = lambda *_args, **_kwargs: lambda function: function
        self.short_rows = []

        def _save_user_once(user_id, content, character_id='gojo', source_event_id=None):
            sid = (str(source_event_id).strip() if source_event_id else '') or None
            if sid:
                for row in self.short_rows:
                    if (row['role'] == 'user'
                            and row.get('source_event_id') == sid
                            and row.get('character_id') == character_id):
                        return False
            self.short_rows.append({
                'role': 'user', 'content': content,
                'character_id': character_id, 'source_event_id': sid,
            })
            return True

        def _save_short(user_id, role, content, character_id='gojo', source_event_id=None):
            self.short_rows.append({
                'role': role, 'content': content,
                'character_id': character_id, 'source_event_id': source_event_id,
            })

        def _get_short(user_id, n=6, character_id='gojo'):
            rows = [r for r in self.short_rows if r.get('character_id') == character_id]
            return [(r['role'], r['content']) for r in rows[-n:]]

        self.save_user_once = Mock(side_effect=_save_user_once)
        self.save_short = Mock(side_effect=_save_short)
        self.get_short = Mock(side_effect=_get_short)
        self.jobs = Mock()
        self.record_turn = Mock()
        self.tts = Mock(return_value='audio' * 40)
        self.llm_text = ''
        self.stream_calls = []
        self.memory = stub(
            'user_memory',
            save_short_memory=self.save_short,
            save_user_short_memory_once=self.save_user_once,
            get_short_memory=self.get_short,
        )
        modules = {
            'anthropic': stub('anthropic', Anthropic=Mock(), AsyncAnthropic=Mock()),
            'fastapi': stub('fastapi', APIRouter=Mock(return_value=router)),
            'fastapi.responses': stub(
                'fastapi.responses',
                StreamingResponse=lambda content, media_type=None: types.SimpleNamespace(
                    body_iterator=content, media_type=media_type,
                ),
            ),
            'config': stub(
                'config',
                ANTHROPIC_KEY='',
                EMOTIONS=['平静', '调皮', '疑惑'],
                DEFAULT_CHARACTER_ID='gojo',
                MODEL_JP_AUX='claude-haiku-test',
            ),
            'tts': stub('tts', tts_to_b64=self.tts),
            'prompt': stub(
                'prompt',
                build_system_blocks=Mock(return_value=[{'type': 'text', 'text': '角色设定'}]),
            ),
            'user_memory': self.memory,
            'context_layer': stub(
                'context_layer',
                build_chat_context=Mock(return_value=types.SimpleNamespace(
                    messages=[],
                    failed_closed=False,
                    recent_event_ids=[],
                    recall_ready=False,
                    pinned_prompt_text='',
                    summary_prompt_text='',
                    memory_text='',
                    bond_text='',
                    told_text='',
                    recall_result=None,
                )),
                assemble_fallback_from_messages=lambda rows, user_id='', character_id='', profile='voice': types.SimpleNamespace(
                    messages=[
                        {'role': item[0], 'content': item[1]}
                        if isinstance(item, (tuple, list)) and len(item) >= 2
                        else {'role': item.get('role'), 'content': item.get('content')}
                        for item in (rows or [])
                    ],
                    failed_closed=False,
                    support_ready=False,
                ),
                append_current_user_turn=lambda messages, content: (
                    list(messages or [])
                    if not content or (
                        messages
                        and messages[-1].get('role') == 'user'
                        and messages[-1].get('content') == content
                    )
                    else list(messages or []) + [{'role': 'user', 'content': content}]
                ),
            ),
            'memory_jobs': stub('memory_jobs', enqueue_private_extraction=self.jobs),
            'characters': stub(
                'characters',
                get_character=Mock(return_value={'voice_id': 'v1'}),
            ),
            'temporal_awareness': stub(
                'temporal_awareness',
                get_temporal_snapshot=Mock(return_value={}),
                record_turn=self.record_turn,
            ),
        }
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.route = load_source('route_voice_stream', modules)

        def _stream(**kwargs):
            self.stream_calls.append(kwargs)
            return FakeStream(self.llm_text)

        self.route.async_claude.messages.stream = _stream
        log_patch = patch('builtins.print')
        self.log = log_patch.start()
        self.addCleanup(log_patch.stop)

    def events(self, llm_text, **extra):
        self.llm_text = llm_text
        data = {'text': '你好', 'user_id': 'u', 'character_id': 'gojo'}
        data.update(extra)

        async def collect():
            resp = await self.route.chat_voice_stream(data)
            chunks = []
            async for part in resp.body_iterator:
                chunks.append(part.decode() if isinstance(part, bytes) else part)
            return ''.join(chunks)

        raw = asyncio.run(collect())
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def types(self, events):
        return [e.get('type') for e in events]

    def test_ellipsis_pair_does_not_yield_audio_or_save_assistant(self):
        events = self.events('EMOTION: 平静\nJP: ...\nZH: ...\n')
        self.assertNotIn('audio', self.types(events))
        self.assertIn('generation_failed', self.types(events))
        failed = next(e for e in events if e['type'] == 'generation_failed')
        self.assertTrue(failed['generation_failed'])
        self.assertEqual(failed['messages'], [])
        self.save_short.assert_not_called()
        self.jobs.assert_not_called()
        self.record_turn.assert_not_called()
        self.tts.assert_not_called()
        self.assertEqual([r['role'] for r in self.short_rows], ['user'])

    def test_short_kana_pair_yields_audio(self):
        events = self.events('EMOTION: 平静\nJP: ん？\nZH: 嗯？\n')
        self.assertIn('audio', self.types(events))
        audio = next(e for e in events if e['type'] == 'audio')
        self.assertEqual(audio['jp'], 'ん？')
        self.assertEqual(audio['zh'], '嗯？')
        self.assertNotIn('generation_failed', self.types(events))
        self.save_short.assert_called_once()
        self.assertEqual(self.save_short.call_args.args[1], 'assistant')
        self.jobs.assert_called_once()
        self.record_turn.assert_called_once()
        self.tts.assert_called()
        self.assertEqual([r['role'] for r in self.short_rows], ['user', 'assistant'])

    def test_zero_valid_pairs_emits_generation_failed(self):
        events = self.events('EMOTION: 平静\nnot a pair\n')
        self.assertIn('generation_failed', self.types(events))
        self.assertNotIn('audio', self.types(events))
        self.save_short.assert_not_called()
        self.jobs.assert_not_called()
        self.record_turn.assert_not_called()

    def test_user_event_saved_even_when_generation_fails(self):
        self.events('JP: ...\nZH: ...\n', source_event_id='voice-1')
        self.save_user_once.assert_called()
        self.assertEqual(self.save_user_once.call_args.kwargs['source_event_id'], 'voice-1')
        self.assertEqual([r['role'] for r in self.short_rows], ['user'])
        self.save_short.assert_not_called()

    def test_same_source_event_id_retry_does_not_duplicate_user(self):
        self.events('JP: ...\nZH: ...\n', source_event_id='voice-dup')
        self.events('JP: ...\nZH: ...\n', source_event_id='voice-dup')
        self.assertEqual(self.save_user_once.call_count, 2)
        self.assertEqual([r['role'] for r in self.short_rows], ['user'])
        self.assertEqual(self.short_rows[0]['source_event_id'], 'voice-dup')

    def test_json_debris_pair_is_rejected(self):
        events = self.events('JP: "jp"\nZH: "messages"\n')
        self.assertNotIn('audio', self.types(events))
        self.assertIn('generation_failed', self.types(events))
        self.save_short.assert_not_called()

    def test_current_user_turn_appears_once_in_stream_messages(self):
        self.short_rows.append({
            'role': 'user', 'content': '上次的话',
            'character_id': 'gojo', 'source_event_id': 'old',
        })
        events = self.events(
            'EMOTION: 平静\nJP: ...\nZH: ...\n',
            text='你好啊',
            source_event_id='voice-now',
        )
        self.assertEqual(len(self.stream_calls), 1)
        msgs = self.stream_calls[0]['messages']
        self.assertEqual([m['content'] for m in msgs], ['上次的话', '你好啊'])
        current = [m for m in msgs if m.get('role') == 'user' and m.get('content') == '你好啊']
        self.assertEqual(len(current), 1)
        self.assertIn('generation_failed', self.types(events))
        saved_current = [
            r for r in self.short_rows
            if r['role'] == 'user' and r['content'] == '你好啊'
        ]
        self.assertEqual(len(saved_current), 1)
        self.assertEqual(saved_current[0]['source_event_id'], 'voice-now')
        self.save_short.assert_not_called()

    def test_retry_same_source_event_id_appears_once_in_stream_messages(self):
        user_text = 'retry this voice turn'
        source_event_id = 'voice-retry-1'
        first_events = self.events(
            'JP: ...\nZH: ...\n',
            text=user_text,
            source_event_id=source_event_id,
        )
        self.assertIn('generation_failed', self.types(first_events))
        self.assertEqual(len(self.short_rows), 1)
        self.assertEqual(self.short_rows[0]['role'], 'user')
        self.assertEqual(self.short_rows[0]['source_event_id'], source_event_id)
        self.save_short.assert_not_called()
        self.jobs.assert_not_called()
        self.record_turn.assert_not_called()
        self.tts.assert_not_called()

        second_events = self.events(
            'JP: OK\nZH: OK\n',
            text=user_text,
            source_event_id=source_event_id,
        )
        self.assertEqual(len(self.stream_calls), 2)
        second_messages = self.stream_calls[1]['messages']
        self.assertEqual(
            [m for m in second_messages
             if m['role'] == 'user' and m['content'] == user_text],
            [{'role': 'user', 'content': user_text}],
        )
        saved_current = [
            row for row in self.short_rows
            if row['role'] == 'user' and row['source_event_id'] == source_event_id
        ]
        self.assertEqual(len(saved_current), 1)
        self.assertEqual(self.save_user_once.call_count, 2)
        self.assertNotIn('generation_failed', self.types(second_events))
        self.assertIn('audio', self.types(second_events))
        self.save_short.assert_called_once()
        self.jobs.assert_called_once()
        self.record_turn.assert_called_once()
        self.tts.assert_called_once()


if __name__ == '__main__':
    unittest.main()
