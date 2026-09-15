import ast
import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
FRONTEND_CHAT = os.path.join(ROOT, 'app', 'chat', '[id].tsx')
ROUTE_CHAT = os.path.join(BACKEND, 'route_chat.py')
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


def chat_reply(jp, zh, emotion='平静'):
    return json.dumps({
        'emotion': emotion,
        'messages': [{'jp': jp, 'zh': zh}],
    }, ensure_ascii=False)


class ChatCommitGateTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
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

        self.save_user_once = Mock(side_effect=_save_user_once)
        self.save_short = Mock(side_effect=_save_short)
        self.memory = stub(
            'user_memory',
            save_short_memory=self.save_short,
            save_user_short_memory_once=self.save_user_once,
            get_short_memory=Mock(return_value=[]),
            update_chat_days=Mock(return_value=3),
            SHORT_MEMORY_MAX=20,
        )
        self.jobs = Mock()
        self.state = Mock()
        self.record_turn = Mock()
        self.record_assistant = Mock()
        self.tts = Mock(return_value='audio')
        self.rel = Mock()
        self.diary = Mock()
        self.promise = Mock()
        self.grumble = Mock()
        modules = {
            'anthropic': stub('anthropic', Anthropic=Mock(return_value=self.client)),
            'fastapi': stub('fastapi', APIRouter=Mock(return_value=router)),
            'fastapi.responses': stub(
                'fastapi.responses',
                JSONResponse=lambda content, status_code=200: types.SimpleNamespace(
                    body=json.dumps(content, ensure_ascii=False).encode(),
                    status_code=status_code,
                ),
            ),
            'config': stub(
                'config',
                ANTHROPIC_KEY='',
                EMOTIONS=['平静', '调皮', '疑惑'],
                TTS_PROVIDER='fish',
                DEFAULT_CHARACTER_ID='gojo',
                MODEL_MAIN='claude-test',
                MODEL_JP_AUX='claude-haiku-test',
            ),
            'db': stub('db', get_conn=Mock(side_effect=AssertionError('unexpected DB access'))),
            'ai_client': stub('ai_client', extract_text=lambda response, sep='': ''),
            'tts': stub('tts', tts_to_b64=self.tts, transcribe_audio_b64=Mock()),
            'prompt': stub(
                'prompt',
                build_system_blocks=Mock(return_value=[{'type': 'text', 'text': '角色设定'}]),
                log_cache_usage=Mock(),
            ),
            'user_memory': self.memory,
            'memory_jobs': stub('memory_jobs', enqueue_private_extraction=self.jobs),
            'temporal_awareness': stub(
                'temporal_awareness',
                get_temporal_snapshot=Mock(return_value={'now_utc': None}),
                find_reply_calendar_conflict=Mock(return_value=None),
                record_turn=self.record_turn,
                record_user_message=Mock(),
                record_assistant_message=self.record_assistant,
            ),
            'characters': stub(
                'characters',
                get_character=Mock(return_value={'voice_id': 'v1', 'core_prompt': 'core'}),
            ),
            'tasks': stub(
                'tasks',
                find_duplicate_task=Mock(return_value=None),
                find_and_delete_tasks_by_keyword=Mock(return_value=[]),
                delete_latest_task=Mock(return_value=[]),
            ),
            'task_dedup': stub('task_dedup', find_similar_task=Mock(return_value=None)),
            'relationship_state': stub(
                'relationship_state', save_offline_character_state=self.state),
            'diary_engine': stub(
                'diary_engine', maybe_write_diary_on_event=self.diary),
            'promise_detector': stub(
                'promise_detector', detect_and_save=self.promise),
            'grumble_engine': stub(
                'grumble_engine', maybe_write_grumble=self.grumble),
            'db_schedule': stub(
                'db_schedule',
                get_current_activity=Mock(return_value=None),
                get_next_free_time=Mock(return_value=None),
            ),
            'db_promise': stub('db_promise', add_promise=Mock()),
        }
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.route = load_source('route_chat', modules)
        self.route._start_relationship_update = self.rel
        self.route._quick_translate = Mock(return_value='译文')
        log_patch = patch('builtins.print')
        self.log = log_patch.start()
        self.addCleanup(log_patch.stop)

    def send(self, raws, text='你好', source_event_id='evt-1', handler=None, extra=None):
        queue = list(raws)
        self.route._create_json = Mock(
            side_effect=lambda *_args, **_kwargs: (queue.pop(0) if queue else '', Mock()))
        payload = {
            'user_id': 'u',
            'character_id': 'gojo',
            'text': text,
            'source_event_id': source_event_id,
        }
        if extra:
            payload.update(extra)
        fn = handler or self.route.chat_text
        response = asyncio.run(fn(payload))
        return response, json.loads(response.body)

    def user_memory_roles(self):
        return [row['role'] for row in self.short_rows]

    def assert_assistant_commit_skipped(self):
        self.assertNotIn('assistant', self.user_memory_roles())
        self.save_short.assert_not_called()
        self.jobs.assert_not_called()
        self.record_turn.assert_not_called()
        self.tts.assert_not_called()
        self.rel.assert_not_called()
        self.state.assert_not_called()
        self.diary.assert_not_called()
        self.promise.assert_not_called()
        self.grumble.assert_not_called()
        self.record_assistant.assert_not_called()

    def assert_generation_failed(self, response, body, attempts=3, user_saved=True):
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body['error'], 'generation_failed')
        self.assertTrue(body['generation_failed'])
        self.assertEqual(body['messages'], [])
        self.assertEqual(self.route._create_json.call_count, attempts)
        self.assert_assistant_commit_skipped()
        if user_saved:
            self.assertEqual(self.user_memory_roles(), ['user'])
            self.assertEqual(self.save_user_once.call_count, 1)
        self.assertIn(f'generation_failed after {attempts} attempts; commit skipped',
                      ' '.join(str(c) for c in self.log.call_args_list))

    def test_valid_short_reply_commits_memory_rel_and_tts(self):
        response, body = self.send([chat_reply('そうだね', '是啊')])
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('error', body)
        self.assertEqual(len(body['messages']), 1)
        self.assertIn('そうだね', body['messages'][0]['jp'])
        self.assertEqual(body['messages'][0]['zh'], '是啊')
        self.assertEqual(self.save_user_once.call_count, 1)
        self.assertEqual(self.save_short.call_count, 1)
        self.assertEqual(self.user_memory_roles(), ['user', 'assistant'])
        self.jobs.assert_called_once()
        self.record_turn.assert_called_once()
        self.tts.assert_called()
        self.rel.assert_called_once()
        self.assertEqual(self.route._create_json.call_count, 1)

    def test_empty_raw_retries_then_generation_failed(self):
        response, body = self.send(['', '', ''])
        self.assert_generation_failed(response, body)

    def test_ellipsis_is_not_a_character_reply(self):
        response, body = self.send([chat_reply('...', '...')])
        self.assert_generation_failed(response, body)

    def test_fullwidth_ellipsis_is_not_a_character_reply(self):
        response, body = self.send([chat_reply('……', '……')])
        self.assert_generation_failed(response, body)

    def test_offline_state_only_does_not_commit(self):
        raw = '<<<OFFLINE_CHARACTER_STATES>>> {"inner":"wait","intent":"silent","moodshift":"none"}'
        response, body = self.send([raw, raw, raw])
        self.assert_generation_failed(response, body)

    def test_ellipsis_json_payload_is_rejected(self):
        raw = '{"emotion":"平静","messages":[{"jp":"...","zh":"..."}]}'
        response, body = self.send([raw, raw, raw])
        self.assert_generation_failed(response, body)

    def test_short_kana_reply_is_accepted(self):
        response, body = self.send([chat_reply('ん？', '嗯？')])
        self.assertEqual(response.status_code, 200)
        self.assertIn('ん？', body['messages'][0]['jp'])
        self.assertEqual(body['messages'][0]['zh'], '嗯？')
        self.save_user_once.assert_called()
        self.save_short.assert_called()
        self.jobs.assert_called_once()

    def test_json_debris_as_visible_text_is_rejected(self):
        debris = '{"jp":"...","zh":"..."}'
        response, body = self.send([chat_reply(debris, debris)])
        self.assert_generation_failed(response, body)

    def test_valid_msg_punctuation_vs_kana(self):
        self.assertFalse(self.route._has_visible_text(None))
        self.assertFalse(self.route._has_visible_text(''))
        self.assertFalse(self.route._has_visible_text('   '))
        self.assertFalse(self.route._has_visible_text('...'))
        self.assertFalse(self.route._has_visible_text('……'))
        self.assertFalse(self.route._has_visible_text('。。。'))
        self.assertFalse(self.route._has_visible_text('!!!'))
        self.assertFalse(self.route._has_visible_text('???'))
        self.assertTrue(self.route._has_visible_text('ん？'))
        self.assertTrue(self.route._has_visible_text('え？'))
        self.assertTrue(self.route._has_visible_text('そうだね'))
        self.assertFalse(self.route._valid_msg({'jp': '...', 'zh': '...'}))
        self.assertFalse(self.route._valid_msg({'jp': '{"jp":"...","zh":"..."}', 'zh': '...'}))
        self.assertTrue(self.route._valid_msg({'jp': 'ん？', 'zh': '嗯？'}))
        self.assertTrue(self.route._valid_msg({'jp': 'え？', 'zh': '诶？'}))

    def test_no_fallback_pool_in_source(self):
        src = Path(ROUTE_CHAT).read_text(encoding='utf-8')
        self.assertNotIn('fallback_pool', src)
        self.assertNotIn('へえ、それで？', src)
        self.assertNotIn('話を聞かせてあげる', src)
        self.assertNotIn('ふっ、何か言った？', src)
        self.assertNotIn('もしもし、どうした？', src)
        self.assertNotIn('の時間だよ', src)
        tree = ast.parse(src)
        assigned = {
            node.targets[0].id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        }
        self.assertNotIn('fallback_pool', assigned)

    def test_generation_failed_keeps_user_event_once_and_retry_does_not_duplicate(self):
        response, body = self.send(['', '', ''], source_event_id='evt-retry')
        self.assert_generation_failed(response, body)
        self.assertEqual(self.user_memory_roles(), ['user'])

        self.route._create_json.reset_mock()
        response, body = self.send(['', '', ''], source_event_id='evt-retry')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.user_memory_roles(), ['user'])
        self.assertEqual(self.save_user_once.call_count, 2)
        self.assertEqual(self.save_short.call_count, 0)
        self.assert_assistant_commit_skipped()

    def test_retry_success_only_appends_assistant(self):
        self.send(['', '', ''], source_event_id='evt-ok')
        self.assertEqual(self.user_memory_roles(), ['user'])
        self.jobs.assert_not_called()
        self.rel.assert_not_called()
        self.tts.assert_not_called()

        self.route._create_json.reset_mock()
        response, body = self.send([chat_reply('そうだね', '是啊')], source_event_id='evt-ok')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.user_memory_roles(), ['user', 'assistant'])
        self.assertEqual(self.save_user_once.call_count, 2)
        self.assertEqual(self.save_short.call_count, 1)
        self.jobs.assert_called_once()
        self.rel.assert_called_once()
        self.tts.assert_called()

    def test_parse_generation_does_not_write_offline_state(self):
        raw = '<<<OFFLINE_CHARACTER_STATES>>> {"inner":"wait","intent":"silent","moodshift":"none"}'
        parsed, visible, state = self.route._ingest(raw, 'u', 'gojo')
        self.state.assert_not_called()
        self.assertIsNotNone(state)
        self.assertFalse(parsed)

    def test_story_empty_generation_does_not_fabricate_or_commit(self):
        response, body = self.send(['', '', '', '', ''], handler=self.route.chat_story)
        self.assert_generation_failed(response, body, attempts=5)
        self.assertEqual(self.user_memory_roles(), ['user'])

    def test_story_valid_reply_commits_assistant_only_once(self):
        raw = json.dumps({
            'emotion': '平静',
            'messages': [
                {'jp': '昔々、最強の呪術師がいてね。', 'zh': '很久很久以前，有一个最强的咒术师。'},
            ],
        }, ensure_ascii=False)
        response, body = self.send([raw], handler=self.route.chat_story)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.user_memory_roles(), ['user', 'assistant'])
        self.jobs.assert_called_once()
        self.tts.assert_called()
        self.state.assert_not_called()

    def test_proactive_empty_generation_does_not_fabricate_or_commit(self):
        response, body = self.send(
            ['', '', ''],
            handler=self.route.chat_proactive,
            extra={'task_title': '吃药', 'mode': 'remind'},
        )
        self.assertEqual(response.status_code, 502)
        self.assertTrue(body['generation_failed'])
        self.assertEqual(body['messages'], [])
        self.assertEqual(self.user_memory_roles(), [])
        self.save_user_once.assert_not_called()
        self.assert_assistant_commit_skipped()
        self.record_assistant.assert_not_called()

    def test_voice_text_empty_generation_does_not_fabricate(self):
        response, body = self.send(['', '', ''], handler=self.route.chat_voice_text)
        self.assert_generation_failed(response, body, attempts=3)

    def test_voice_story_empty_generation_does_not_fabricate(self):
        response, body = self.send(['', '', '', '', ''], handler=self.route.chat_voice_story)
        self.assert_generation_failed(response, body, attempts=5)

    def test_voice_proactive_empty_generation_does_not_fabricate(self):
        response, body = self.send(
            ['', '', ''],
            handler=self.route.chat_voice_proactive,
            extra={'mode': 'greeting'},
        )
        self.assertEqual(response.status_code, 502)
        self.assertTrue(body['generation_failed'])
        self.assertEqual(self.user_memory_roles(), [])
        self.assert_assistant_commit_skipped()


class FrontendCommitGateGuardTests(unittest.TestCase):
    def setUp(self):
        self.src = Path(FRONTEND_CHAT).read_text(encoding='utf-8')

    def test_no_character_fallback_bubble_path(self):
        self.assertNotIn('FALLBACK_LINES', self.src)
        self.assertNotIn('appendFallbackBubble', self.src)
        self.assertNotIn('信号好像不太好', self.src)
        self.assertNotIn('现在有点脱不开身', self.src)

    def test_generation_failed_stays_off_chatlog(self):
        self.assertIn('generation_failed', self.src)
        self.assertIn('localOnly', self.src)
        self.assertIn('generationFailure', self.src)
        self.assertIn('isEphemeralUiMessage', self.src)
        self.assertIn("if ((m as any).localOnly) return false;", self.src)
        self.assertIn("if ((m as any).generationFailure) return false;", self.src)
        self.assertIn('回复生成失败，点击重试', self.src)
        self.assertNotRegex(self.src, r"role:\s*'gojo'[^\n]*generationFailure")

    def test_send_image_passes_source_event_id(self):
        self.assertIn('source_event_id: sourceEventId', self.src)
        self.assertIn("lastFailedSendRef.current?.kind === 'image'", self.src)
        self.assertIn('sourceEventId', self.src)

    def test_proactive_flags_commit_only_after_success(self):
        self.assertNotIn('mode = \'remind\'; taskState.reminded = true;', self.src)
        self.assertNotIn("mode = 'overdue'; taskState.askedOverdue = true;", self.src)
        self.assertIn('const ok = await sendProactive', self.src)
        self.assertIn('if (ok)', self.src)
        self.assertIn('taskState.reminded = true', self.src)
        self.assertIn('taskState.askedOverdue = true', self.src)


class VoiceCallModalGateTests(unittest.TestCase):
    def setUp(self):
        self.src = Path(os.path.join(ROOT, 'components', 'VoiceCallModal.tsx')).read_text(encoding='utf-8')

    def test_voice_stream_sends_source_event_id(self):
        self.assertIn('sendToGojo(text, userMsg.id)', self.src)
        self.assertIn('source_event_id: sourceEventId', self.src)
        self.assertIn("evt.type === 'generation_failed'", self.src)


if __name__ == '__main__':
    unittest.main()
