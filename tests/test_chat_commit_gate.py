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
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

from receipt_passthrough import passthrough_generation_receipt  # noqa: E402


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

        def _save_short(user_id, role, content, character_id='gojo',
                        source_event_id=None, metadata=None, **_kwargs):
            self.short_rows.append({
                'role': role, 'content': content,
                'character_id': character_id, 'source_event_id': source_event_id,
                'metadata': metadata,
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
            'behavior_evidence': stub(
                'behavior_evidence', record_reply_cycle=Mock()),
            'context_layer': stub(
                'context_layer',
                build_chat_context=Mock(return_value=types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')),
                assemble_fallback_from_messages=Mock(return_value=types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')),
                append_current_user_turn=lambda msgs, content: list(msgs or []) + [
                    {'role': 'user', 'content': content}],
            ),
            'db_generation_receipt': passthrough_generation_receipt(),
        }
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.route = load_source('route_chat', modules)
        self.route._start_relationship_update = self.rel
        try:
            import tts as _real_tts
            tts_lock = patch.object(_real_tts, 'tts_to_b64', self.tts)
            tts_lock.start()
            self.addCleanup(tts_lock.stop)
        except Exception:
            pass
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

    def generation_traces(self):
        return [json.loads(call.args[0].split(' ', 1)[1])
                for call in self.log.call_args_list
                if call.args and isinstance(call.args[0], str)
                and call.args[0].startswith('[generation_trace] {')]

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

    def assert_generation_failed(self, response, body, attempts=1, user_saved=True):
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body['error'], 'generation_failed')
        self.assertTrue(body['generation_failed'])
        self.assertEqual(body['messages'], [])
        self.assertEqual(self.route._create_json.call_count, attempts)
        self.assert_assistant_commit_skipped()
        if user_saved:
            self.assertEqual(self.user_memory_roles(), ['user'])
            self.assertEqual(self.save_user_once.call_count, 1)
        self.assertIn('generation_failed; max_semantic_attempts=2; commit skipped',
                      ' '.join(str(c) for c in self.log.call_args_list))

    def test_prompt_messages_do_not_fallback_on_source_validity_error(self):
        import raw_events
        secret = 'fallback也不能喂给模型'

        def boom(*_args, **_kwargs):
            raise raw_events.SourceValidityError('deleted-event db down')

        with patch('user_memory.get_short_memory_for_prompt', create=True, side_effect=boom):
            out = self.route._prompt_messages(
                'u', 'gojo', [{'role': 'user', 'content': secret}])
        self.assertEqual(out, [])
        self.assertNotIn(secret, json.dumps(out, ensure_ascii=False))

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
        self.jobs.assert_not_called()
        self.record_turn.assert_called_once()
        self.tts.assert_called()
        self.rel.assert_not_called()
        self.assertEqual(self.route._create_json.call_count, 1)
        self.grumble.assert_not_called()

    def test_verbal_plaintext_in_three_languages_displays_once_without_fake_zh(self):
        valid = chat_reply('そうだね', '是啊')
        for index, raw in enumerate(('今日はゆっくり話そう', '今天慢慢聊吧',
                                     'We can talk now')):
            with self.subTest(raw=raw):
                response, body = self.send([raw, valid], source_event_id=f'plain-{index}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.route._create_json.call_count, 1)
                self.assertEqual(body['messages'][0]['jp'], raw)
                self.assertEqual(body['messages'][0]['zh'], '')
                for field in ('pending_transaction', 'reminder', 'schedule_transition'):
                    self.assertNotIn(field, body)

    def test_two_semantic_rejections_fail_closed_with_specific_reasons(self):
        private_raw = '今日は疲れたよ'
        response, body = self.send([
            chat_reply(private_raw, private_raw),
            chat_reply('そうだね', 'ねえ、悟って呼んで。'),
        ])
        self.assert_generation_failed(response, body, attempts=2)
        traces = self.generation_traces()
        self.assertEqual([t['parse_invalid_reason'] for t in traces],
                         ['jp_equals_zh_verbal', 'zh_contains_kana'])
        self.assertEqual([t['retry_reason'] for t in traces],
                         ['jp_equals_zh_verbal', None])
        self.assertNotIn(private_raw, json.dumps(traces, ensure_ascii=False))
        calls = self.route._create_json.call_args_list
        self.assertIn('jp_equals_zh_verbal', calls[1].args[2][-1]['text'])
        self.assertNotIn(private_raw, calls[1].args[2][-1]['text'])

    def test_retry_prompt_is_transient_protocol_only_and_never_echoes_output(self):
        private_raw = '今日は疲れたよ'
        original_blocks = [{'type': 'text', 'text': '角色设定'}]
        self.route.build_system_blocks.return_value = original_blocks
        response, body = self.send([
            chat_reply(private_raw, private_raw), chat_reply('そうだね', '是啊')])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 2)
        calls = self.route._create_json.call_args_list
        self.assertEqual(calls[0].args[2], original_blocks)
        self.assertEqual(calls[1].args[2][:-1], original_blocks)
        self.assertEqual(original_blocks, [{'type': 'text', 'text': '角色设定'}])
        for call in calls[1:]:
            hint = call.args[2][-1]['text']
            self.assertIn('jp_equals_zh_verbal', hint)
            self.assertIn('JSON', hint)
            self.assertIn('jp', hint)
            self.assertIn('zh', hint)
            self.assertNotIn(private_raw, hint)
            self.assertEqual(call.args[3], calls[0].args[3])
        self.assertNotIn(private_raw, json.dumps(self.short_rows, ensure_ascii=False))
        self.assertEqual(body['messages'][0]['zh'], '是啊')

    def test_equal_verbal_pair_retries_and_recovers(self):
        response, body = self.send([
            chat_reply('今日は疲れたよ', '今日は疲れたよ'),
            chat_reply('今日は疲れたよ', '今天有点累。'),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 2)
        self.assertEqual(self.generation_traces()[0]['parse_invalid_reason'],
                         'jp_equals_zh_verbal')
        self.assertEqual(body['messages'][0]['zh'], '今天有点累。')

    def test_mostly_japanese_zh_retries_but_short_quote_passes(self):
        response, body = self.send([
            chat_reply('そうだね', 'ねえ、悟って呼んで。'),
            chat_reply('そうだね', '是啊，叫我悟就好。'),
        ])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 2)
        self.assertEqual(self.generation_traces()[0]['parse_invalid_reason'],
                         'zh_contains_kana')
        self.assertEqual(body['messages'][0]['zh'], '是啊，叫我悟就好。')

        self.log.reset_mock()
        response, body = self.send([
            chat_reply('そうだね', '他说「おはよう」，我听懂了。'),
        ], source_event_id='short-quote')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(body['messages'][0]['zh'], '他说「おはよう」，我听懂了。')

        response, body = self.send([
            chat_reply('そうだね', '他说「おはよう」。'),
        ], source_event_id='short-quote-little-cn')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(body['messages'][0]['zh'], '他说「おはよう」。')

    def test_reply_to_reaches_model_but_not_memory_or_relationship_input(self):
        response, body = self.send(
            [chat_reply('その話ね。', '你说那件事啊。')],
            text='我接着说',
            extra={'reply_to': {
                'id': 'old-1',
                'name': '五条悟',
                'text': '刚才那杯水别碰。',
                'role': 'gojo',
            }},
        )
        self.assertEqual(response.status_code, 200)
        sent_messages = self.route._create_json.call_args.args[3]
        self.assertIn('【引用回复】', sent_messages[-1]['content'])
        self.assertIn('刚才那杯水别碰。', sent_messages[-1]['content'])
        self.assertEqual(self.short_rows[0]['content'], '我接着说')
        self.rel.assert_not_called()
        self.assertEqual(self.save_user_once.call_args.args[1], '我接着说')

    def test_empty_raw_fails_without_format_retry(self):
        response, body = self.send(['', '', ''])
        self.assert_generation_failed(response, body)

    def test_ellipsis_is_an_intentional_reply(self):
        response, body = self.send([chat_reply('...', '...')])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['messages'][0]['jp'], '...')
        self.assertEqual(self.route._create_json.call_count, 1)
        self.client.messages.create.assert_not_called()

    def test_fullwidth_ellipsis_is_an_intentional_reply(self):
        response, body = self.send([chat_reply('……', '……')])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['messages'][0]['jp'], '……')
        self.assertEqual(self.route._create_json.call_count, 1)
        self.client.messages.create.assert_not_called()

    def test_offline_state_only_does_not_commit(self):
        raw = '<<<OFFLINE_CHARACTER_STATES>>> {"inner":"wait","intent":"silent","moodshift":"none"}'
        response, body = self.send([raw, raw, raw])
        self.assert_generation_failed(response, body)

    def test_ellipsis_json_payload_is_accepted(self):
        raw = '{"emotion":"平静","messages":[{"jp":"...","zh":"..."}]}'
        response, body = self.send([raw])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['messages'][0]['jp'], '...')

    def test_short_kana_reply_is_accepted(self):
        response, body = self.send([chat_reply('ん？', '嗯？')])
        self.assertEqual(response.status_code, 200)
        self.assertIn('ん？', body['messages'][0]['jp'])
        self.assertEqual(body['messages'][0]['zh'], '嗯？')
        self.save_user_once.assert_called()
        self.save_short.assert_called()
        self.jobs.assert_not_called()

    def test_structured_bracket_bodies_pass_without_translation(self):
        for jp, zh in (('配列の a[0] を見て。', '明天复习 [第三章]。'),
                       ('集合 {1, 2} を見て。', '补充说明 [可选]，集合 {甲, 乙}。')):
            with self.subTest(jp=jp):
                response, body = self.send([chat_reply(jp, zh)])
                self.assertEqual(response.status_code, 200)
                self.assertEqual((body['messages'][0]['jp'], body['messages'][0]['zh']), (jp, zh))
                self.assertEqual(self.route._create_json.call_count, 1)
                self.client.messages.create.assert_not_called()
                self.state.assert_not_called()

    def test_safe_raw_bracket_notes_degrade_without_copying_translation(self):
        for index, text in enumerate(('配列の a[0] を見て。',
                                      '补充说明（可选）[第三章]，集合 {甲, 乙}。')):
            with self.subTest(text=text):
                self.client.messages.create.reset_mock()
                response, body = self.send([text] * 3, source_event_id=f'raw-bracket-{index}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(body['messages'][0]['jp'], text)
                self.assertEqual(body['messages'][0]['zh'], '')
                self.assertEqual(self.route._create_json.call_count, 1)
                self.client.messages.create.assert_not_called()
                self.state.assert_not_called()

    def test_json_debris_as_visible_text_is_rejected(self):
        debris = '{"jp":"...","zh":"..."}'
        response, body = self.send([chat_reply(debris, debris)])
        self.assert_generation_failed(response, body, attempts=2)

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
        self.assertTrue(self.route._valid_msg({'jp': '...', 'zh': '...'}))
        self.assertFalse(self.route._valid_msg({'jp': '{"jp":"...","zh":"..."}', 'zh': '...'}))
        self.assertTrue(self.route._valid_msg({'jp': 'ん？', 'zh': '嗯？'}))
        self.assertTrue(self.route._valid_msg({'jp': 'え？', 'zh': '诶？'}))

    def test_bilingual_gate_rejects_identical_text_and_mostly_japanese_chinese(self):
        from utils import valid_reply_pair

        for jp, zh in (('今日は疲れたよ', '今日は疲れたよ'),
                       ('そうだね', 'ねえ、悟って呼んで。')):
            with self.subTest(jp=jp, zh=zh):
                message = {'jp': jp, 'zh': zh}
                self.assertFalse(self.route._valid_msg(message))
                self.assertFalse(valid_reply_pair(jp, zh))
                self.assertEqual(self.route._finalize_committed(
                    {'messages': [message]}), (None, None))
        self.assertTrue(valid_reply_pair('了解', '知道了'))
        self.assertTrue(valid_reply_pair('😒', '😒'))
        self.assertTrue(valid_reply_pair('そうだね', '是啊ね'))
        self.assertTrue(valid_reply_pair('そうだね', '他说「おはよう」，我听懂了。'))
        self.assertTrue(valid_reply_pair('そうだね', '他说「おはよう」。'))

    def test_invalid_bilingual_candidates_retry_before_commit(self):
        for index, pair in enumerate((('今日は疲れたよ', '今日は疲れたよ'),
                                      ('そうだね', 'ねえ、悟って呼んで。'))):
            with self.subTest(pair=pair):
                raw = chat_reply(*pair)
                response, body = self.send([raw] * 3,
                                           source_event_id=f'invalid-pair-{index}')
                self.assertEqual(response.status_code, 502)
                self.assertEqual(body['messages'], [])
                self.assertEqual(self.route._create_json.call_count, 2)

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
        self.jobs.assert_not_called()
        self.rel.assert_not_called()
        self.tts.assert_called()

    def test_parse_generation_does_not_write_offline_state(self):
        raw = '<<<OFFLINE_CHARACTER_STATES>>> {"inner":"wait","intent":"silent","moodshift":"none"}'
        parsed, visible, state = self.route._ingest(raw, 'u', 'gojo')
        self.state.assert_not_called()
        self.assertIsNotNone(state)
        self.assertFalse(parsed)

    def test_story_empty_generation_does_not_fabricate_or_commit(self):
        response, body = self.send(['', '', '', '', ''], handler=self.route.chat_story)
        self.assert_generation_failed(response, body)
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
        self.jobs.assert_not_called()
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
        self.assert_generation_failed(response, body)

    def test_text_voice_use_one_snapshot_and_degrade_plaintext(self):
        from datetime import datetime, timezone
        now = datetime(2026, 9, 30, 13, 30, tzinfo=timezone.utc)
        snapshot = {'now_utc': now, 'now_local': now}
        for handler in (self.route.chat_text, self.route.chat_voice_text):
            with self.subTest(handler=handler.__name__), patch.object(
                    self.route, 'get_temporal_snapshot', return_value=snapshot) as clock, patch.object(
                    self.route, '_turn_context', return_value=(None, [])) as context:
                response, body = self.send([
                    '今日はゆっくり話そう', chat_reply('今日はゆっくり話そう。', '今天慢慢聊吧。')], handler=handler,
                                           source_event_id=handler.__name__)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(body['messages'][0]['jp'], '今日はゆっくり話そう')
                self.assertEqual(body['messages'][0]['zh'], '')
                self.assertEqual(self.route._create_json.call_count, 1)
                clock.assert_called_once()
                self.assertIs(context.call_args.kwargs['temporal_snapshot'], snapshot)
                self.assertIs(self.route.build_system_blocks.call_args.kwargs['temporal_snapshot'], snapshot)
                self.client.messages.create.assert_not_called()

    def test_voice_story_empty_generation_does_not_fabricate(self):
        response, body = self.send(['', '', '', '', ''], handler=self.route.chat_voice_story)
        self.assert_generation_failed(response, body)

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


    def test_raw_nonverbal_accepts_first_attempt_without_translation(self):
        for token in ('😒', '🥺', '🥺...', '...', '……'):
            with self.subTest(token=token):
                response, body = self.send([token])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(self.route._create_json.call_count, 1)
                self.assertEqual(body['messages'][0]['jp'], token)
                self.assertEqual(body['messages'][0]['zh'], token)
                self.client.messages.create.assert_not_called()
                self.state.assert_not_called()
                self.rel.assert_not_called()
                self.jobs.assert_not_called()
                for field in ('pending_transaction', 'reminder', 'schedule_transition'):
                    self.assertNotIn(field, body)

    def test_structured_nonverbal_preserves_tokens_and_separate_bubbles(self):
        raw = json.dumps({'messages': [{'jp': t, 'zh': t} for t in ('🥺...', '...', '……')]})
        response, body = self.send([raw])
        self.assertEqual(response.status_code, 200)
        self.assertEqual([m['jp'] for m in body['messages']], ['🥺...', '...', '……'])
        self.assertEqual(self.route._create_json.call_count, 1)
        self.client.messages.create.assert_not_called()

    def test_malformed_json_and_internal_debris_are_never_salvaged(self):
        for raw in ('{"jp":"こんにちは"', '{"messages":[',
                    '{"unknown"', '["hello",', '[1,', '[true,',
                    '{unknown: broken}', '[{"jp":"こんにちは"}',
                    '[[1, 2]', '说明：{"unknown"', '说明：["hello",', '说明：[1,',
                    '{broken', '[broken', '[[broken]',
                    'jp: こんにちは', '"unknown_field": "秘密"', 'state: thinking',
                    'pending_transaction: 100', '{"inner":"秘密","intent":"待つ"}',
                    '🥺... <<<OFFLINE_CHARACTER_STATES>>> {"inner":"x"}',
                    '<thinking>private</thinking>', '   '):
            with self.subTest(raw=raw):
                response, body = self.send([raw, raw, raw])
                self.assertEqual(response.status_code, 502)
                self.assertEqual(self.route._create_json.call_count, 1)
                self.client.messages.create.assert_not_called()
                self.assert_assistant_commit_skipped()

    def test_plaintext_salvage_runs_truth_guard_before_acceptance(self):
        reject = Mock(side_effect=['candidate_rejected', None])
        self.route._create_json = Mock(side_effect=[('🥺...', Mock()), ('……', Mock())])
        result, state = self.route._generate_or_none(
            'offline', 100, [], [], attempts=3, log_tag='test', cache_tag='test',
            salvage=True, reject_fn=reject)
        self.assertEqual(self.route._create_json.call_count, 2)
        self.assertEqual(result, {'emotion': '平静', 'messages': [{'jp': '……', 'zh': '……'}], '_acceptance_mode': 'nonverbal'})
        self.assertIsNone(state)
        self.client.messages.create.assert_not_called()

    def test_source_failure_stops_before_generator(self):
        self.save_user_once.side_effect = RuntimeError('private failure text')
        response, body = self.send([chat_reply('こんにちは', '你好')])
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body['error'], 'canonical_source_unavailable')
        self.route._create_json.assert_not_called()
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
        self.assertIn('m?.localOnly', self.src)
        self.assertIn('m?.generationFailure', self.src)
        self.assertIn('回复生成失败，点击重试', self.src)
        self.assertNotRegex(self.src, r"role:\s*'gojo'[^\n]*generationFailure")

    def test_send_image_passes_source_event_id(self):
        self.assertIn('source_event_id: sourceEventId', self.src)
        self.assertIn("lastFailedSendRef.current?.kind === 'image'", self.src)
        self.assertIn('sourceEventId', self.src)

    def test_reply_and_visual_metadata_are_synced_to_chatlog(self):
        self.assertIn('extra.reply_to = m.replyTo', self.src)
        self.assertIn('extra.source_event_id = m.sourceEventId', self.src)
        self.assertIn('extra.visual_summary = m.visualSummary', self.src)
        self.assertIn('extra.event_meta = m.eventMeta', self.src)
        self.assertIn('assistantTurnId: res.data?.assistant_turn_id', self.src)
        self.assertIn('generation_in_progress', self.src)
        self.assertIn('仍在生成', self.src)
        self.assertIn('function assistantSegmentId', self.src)

    def test_proactive_flags_commit_only_after_success(self):
        self.assertNotIn('mode = \'remind\'; taskState.reminded = true;', self.src)
        self.assertNotIn("mode = 'overdue'; taskState.askedOverdue = true;", self.src)
        self.assertIn('const ok = await sendProactive', self.src)
        self.assertIn('if (ok)', self.src)
        self.assertIn('taskState.reminded = true', self.src)
        self.assertIn('taskState.askedOverdue = true', self.src)

    def test_proactive_reuses_backend_canonical_event_id(self):
        self.assertNotIn('proactive_${Date.now()}_${i}', self.src)
        self.assertNotIn('proactive_${p.id}', self.src)
        self.assertIn('res.data?.event_id || res.data?.assistant_turn_id', self.src)
        self.assertIn('p.event_id || p.assistant_turn_id', self.src)
        self.assertIn('assistant_turn_id: turnId', self.src)
        self.assertIn('segment_index: i', self.src)
        self.assertIn('extra.assistant_turn_id = m.eventMeta.assistant_turn_id', self.src)
        self.assertIn('client_request_id: clientRequestId', self.src)
        self.assertIn('proactive:chat:task:', self.src)
        self.assertIn('sendProactive(task.title, mode, task.id, dueDateStr)', self.src)


class VoiceCallModalGateTests(unittest.TestCase):
    def setUp(self):
        self.src = Path(os.path.join(ROOT, 'components', 'VoiceCallModal.tsx')).read_text(encoding='utf-8')

    def test_voice_stream_sends_source_event_id(self):
        self.assertIn('sendToGojo(text, userMsg.id)', self.src)
        self.assertIn('source_event_id: sourceEventId', self.src)
        self.assertIn("evt.type === 'generation_failed'", self.src)

    def test_voice_proactive_sends_client_request_id(self):
        self.assertIn('client_request_id: requestId', self.src)
        self.assertIn('newVoiceRequestId', self.src)
        self.assertNotIn('idle_${Date.now()}', self.src)
        self.assertNotIn('greet_${Date.now()}_${i}', self.src)


class VoiceProactiveIdentityTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        router = Mock()
        router.post.side_effect = lambda *_args, **_kwargs: lambda function: function
        self.short_rows = []

        def _save_user_once(user_id, content, character_id='gojo', source_event_id=None):
            return True

        def _save_short(user_id, role, content, character_id='gojo',
                        source_event_id=None, metadata=None, **_kwargs):
            self.short_rows.append({
                'role': role, 'content': content,
                'character_id': character_id, 'source_event_id': source_event_id,
                'metadata': metadata,
            })

        self.memory = stub(
            'user_memory',
            save_short_memory=_save_short,
            save_user_short_memory_once=_save_user_once,
            get_short_memory=Mock(return_value=[]),
            update_chat_days=Mock(return_value=3),
            SHORT_MEMORY_MAX=20,
        )
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
            'tts': stub('tts', tts_to_b64=Mock(return_value='audio'), transcribe_audio_b64=Mock()),
            'prompt': stub(
                'prompt',
                build_system_blocks=Mock(return_value=[{'type': 'text', 'text': '角色设定'}]),
                log_cache_usage=Mock(),
            ),
            'user_memory': self.memory,
            'memory_jobs': stub('memory_jobs', enqueue_private_extraction=Mock()),
            'temporal_awareness': stub(
                'temporal_awareness',
                get_temporal_snapshot=Mock(return_value={'now_utc': None}),
                find_reply_calendar_conflict=Mock(return_value=None),
                record_turn=Mock(),
                record_user_message=Mock(),
                record_assistant_message=Mock(),
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
                'relationship_state', save_offline_character_state=Mock()),
            'diary_engine': stub('diary_engine', maybe_write_diary_on_event=Mock()),
            'promise_detector': stub('promise_detector', detect_and_save=Mock()),
            'grumble_engine': stub('grumble_engine', maybe_write_grumble=Mock()),
            'db_schedule': stub(
                'db_schedule',
                get_current_activity=Mock(return_value=None),
                get_next_free_time=Mock(return_value=None),
            ),
            'db_promise': stub('db_promise', add_promise=Mock()),
            'behavior_evidence': stub(
                'behavior_evidence', record_reply_cycle=Mock()),
            'context_layer': stub(
                'context_layer',
                build_chat_context=Mock(return_value=types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')),
                assemble_fallback_from_messages=Mock(return_value=types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')),
                append_current_user_turn=lambda msgs, content: list(msgs or []) + [
                    {'role': 'user', 'content': content}],
            ),
            'db_generation_receipt': passthrough_generation_receipt(),
        }
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.route = load_source('route_chat', modules)
        log_patch = patch('builtins.print')
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_resolve_voice_proactive_event_id_is_stable(self):
        src = Path(ROUTE_CHAT).read_text(encoding='utf-8')
        self.assertNotIn('%Y%m%d%H%M', src)
        a = self.route.resolve_voice_proactive_event_id(
            {'client_request_id': 'req-a', 'mode': 'idle'})
        b = self.route.resolve_voice_proactive_event_id(
            {'client_request_id': 'req-b', 'mode': 'idle'})
        self.assertNotEqual(a, b)
        self.assertEqual(a, 'req-a')
        retry = self.route.resolve_voice_proactive_event_id(
            {'client_request_id': 'req-a', 'mode': 'idle'})
        self.assertEqual(retry, 'req-a')
        reused = self.route.resolve_voice_proactive_event_id(
            {'event_id': 'voice_proactive:already'})
        self.assertEqual(reused, 'voice_proactive:already')
        generated = self.route.resolve_voice_proactive_event_id({'mode': 'idle'})
        generated_again = self.route.resolve_voice_proactive_event_id({'mode': 'idle'})
        self.assertTrue(generated.startswith('voice_proactive:'))
        self.assertNotEqual(generated, generated_again)

    def test_resolve_chat_proactive_event_id_is_stable_operation_identity(self):
        src = Path(ROUTE_CHAT).read_text(encoding='utf-8')
        self.assertNotIn("proactive:chat:{digest}:{day}", src)
        self.assertNotIn('hashlib.sha1', src)
        self.assertNotRegex(src, r"proactive:chat:\{digest\}")
        same_content = {'task_title': '喝水', 'mode': 'remind'}
        first = self.route.resolve_chat_proactive_event_id({
            **same_content,
            'client_request_id': 'proactive:chat:task:1:2026-09-17:remind',
        })
        retry = self.route.resolve_chat_proactive_event_id({
            **same_content,
            'client_request_id': 'proactive:chat:task:1:2026-09-17:remind',
        })
        other = self.route.resolve_chat_proactive_event_id({
            **same_content,
            'client_request_id': 'proactive:chat:task:2:2026-09-17:remind',
        })
        self.assertEqual(first, retry)
        self.assertNotEqual(first, other)
        occ = self.route.resolve_chat_proactive_event_id({
            **same_content,
            'task_id': 't9',
            'due_date': '2026-01-02',
            'mode': 'overdue',
        })
        self.assertEqual(occ, 'proactive:chat:task:t9:2026-01-02:overdue')
        generated = self.route.resolve_chat_proactive_event_id(same_content)
        generated_again = self.route.resolve_chat_proactive_event_id(same_content)
        self.assertTrue(generated.startswith('proactive:chat:'))
        self.assertNotEqual(generated, generated_again)
        self.assertNotRegex(generated, r':\d{8}$')


if __name__ == '__main__':
    unittest.main()
