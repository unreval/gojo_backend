"""Real TTS entry points with provider HTTP intercepted; no paid requests."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import Mock, patch

import tts
import db_generation_receipt as receipt
from utils import classify_reply_content, has_visible_text, ingest_model_output, valid_reply_msg
from tests import test_receipt_windows as windows
from tests import test_voice_stream_commit_gate as voice

REAL_TTS = tts.tts_to_b64
SILENT = ('🥺', '🥺...？！', '...', '…', '……', '👩🏽‍💻', '❤️', '🇯🇵', '1️⃣',
          '・', '・・・', '…・…', '... ・ ……', '・…🥺')
BRACKET_TEXT = ('明天复习 [第三章]。', '配列の a[0] を見て。',
                '补充说明（可选）[第三章]，集合 {甲, 乙}。',
                '[第三章] 明天复习。', '集合 {1, 2} を見て。', '[[第三章]] 明天复习。')
PROTOCOL_TEXT = ('{"messages":[', '{"jp":"こんにちは"', '{"unknown"',
                 '["hello",', '[1,', '[true,', '[{"jp":"こんにちは"}',
                 '{unknown: broken}', '["hello"]', '{}', '[]', '[[1, 2]',
                 '说明：{"unknown"', '说明：["hello",', '说明：[1,',
                 '{broken', '[broken', '[[broken]',
                 'jp: こんにちは', '"unknown_field": "秘密"',
                 '<<<OFFLINE_CHARACTER_STATES>>>', 'OFFLINE_CHARACTER_STATES',
                 '{"inner":"秘密","intent":"待つ"}', 'state: thinking',
                 '<thinking>private</thinking>', '[angry]')


class NonverbalTTSTests(unittest.TestCase):
    def test_bracket_bodies_and_protocol_envelopes_are_distinct(self):
        for text in BRACKET_TEXT:
            with self.subTest(text=text):
                message = {'jp': text, 'zh': '这段说明我看到了。'}
                raw = json.dumps({'messages': [message]}, ensure_ascii=False)
                self.assertEqual(classify_reply_content(raw), 'invalid')
                _, parsed, state = ingest_model_output(raw)
                self.assertIsNone(state)
                self.assertEqual(parsed['messages'], [message])
                self.assertTrue(valid_reply_msg(parsed['messages'][0]))
                self.assertEqual(classify_reply_content(text), 'text')

    def test_protocol_fragments_never_reach_tts_or_http(self):
        with patch.object(tts, 'fish_tts', wraps=tts.fish_tts) as provider, patch.object(
                tts.requests, 'post', side_effect=AssertionError('provider HTTP called')) as http:
            for text in PROTOCOL_TEXT:
                with self.subTest(text=text):
                    self.assertEqual(classify_reply_content(text), 'invalid')
                    self.assertFalse(valid_reply_msg({'jp': text, 'zh': text}))
                    self.assertEqual(REAL_TTS(text, '平静'), '')
                    provider.assert_not_called()
                    self.assertEqual(tts.fish_tts(text), b'')
                    provider.reset_mock()
                    http.assert_not_called()

    def test_middle_dots_are_nonverbal_without_tts_failure(self):
        with patch.object(tts, 'fish_tts', wraps=tts.fish_tts) as provider, patch.object(
                tts.requests, 'post', side_effect=AssertionError('provider HTTP called')) as http, patch(
                'builtins.print') as log:
            for text in ('・', '・・・', '…・…', '... ・ ……'):
                with self.subTest(text=text):
                    self.assertFalse(has_visible_text(text))
                    self.assertEqual(classify_reply_content(text), 'nonverbal')
                    self.assertEqual(REAL_TTS(text, '愤怒'), '')
                    provider.assert_not_called()
                    self.assertEqual(tts.fish_tts(text), b'')
                    provider.reset_mock()
                    http.assert_not_called()
                    log.assert_not_called()

    def test_nonverbal_never_calls_provider_or_any_model_or_database(self):
        with patch.object(tts, 'fish_tts', wraps=tts.fish_tts) as provider, patch.object(
                tts.requests, 'post', side_effect=AssertionError('provider HTTP called')) as http, patch(
                'db.get_conn', side_effect=AssertionError('TTS database write')) as db, patch(
                'ai_client.create_chat', side_effect=AssertionError('TTS model call')) as model:
            for text in SILENT:
                with self.subTest(text=text):
                    self.assertEqual(REAL_TTS(text, '愤怒', None), '')
                    provider.assert_not_called()
                    http.assert_not_called()
                    db.assert_not_called()
                    model.assert_not_called()

    def test_single_displeased_emoji_stays_silent(self):
        with patch.object(tts, 'fish_tts') as provider:
            self.assertEqual(classify_reply_content('😒'), 'nonverbal')
            self.assertTrue(valid_reply_msg({'jp': '😒', 'zh': '😒'}))
            self.assertEqual(REAL_TTS('😒', '平静'), '')
            provider.assert_not_called()

    def test_direct_provider_and_empty_fallback_cannot_voice_silence(self):
        with patch.object(tts.requests, 'post') as http:
            for text in SILENT + ('', '  ', '!!!', '[angry]'):
                with self.subTest(text=text):
                    self.assertEqual(tts.fish_tts(text, '愤怒'), b'')
                    self.assertEqual(REAL_TTS(text, '愤怒'), '')
                    http.assert_not_called()

    def test_mixed_text_remains_spoken_without_changing_original(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'offline-mp3']
        with patch.object(tts.requests, 'post', return_value=response) as http, patch.object(
                tts, 'fish_tts', wraps=tts.fish_tts) as provider:
            for text, spoken in (('……你过来。🥺', '你过来。'), ('等一下…🥺', '等一下'),
                                 ('🥺来て。', '来て。'), ('ん？', 'ん？'),
                                 ('ジョン・スミス', 'ジョン・スミス'), ('コーヒー', 'コーヒー'),
                                 *((value, value) for value in BRACKET_TEXT)):
                with self.subTest(text=text):
                    provider.reset_mock()
                    http.reset_mock()
                    original = text
                    self.assertTrue(REAL_TTS(text, '平静', 'offline'))
                    self.assertEqual(provider.call_count, 1)
                    self.assertEqual(http.call_count, 1)
                    self.assertIn(spoken, http.call_args.kwargs['json']['text'])
                    self.assertEqual(text, original)

    def test_bracket_receipt_hydration_preserves_body_and_regenerates_audio(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'offline-mp3']
        with patch('characters.get_character', return_value={'voice_id': 'offline'}), patch.object(
                tts.requests, 'post', return_value=response) as http, patch.object(
                tts, 'fish_tts', wraps=tts.fish_tts) as provider:
            for text in BRACKET_TEXT:
                with self.subTest(text=text):
                    body = {'messages': [{'jp': text, 'zh': text, 'audio_b64': 'old-audio'}]}
                    replay = receipt.hydrate_replay(body, 'c')
                    self.assertEqual(replay['messages'][0]['jp'], text)
                    self.assertEqual(replay['messages'][0]['zh'], text)
                    self.assertTrue(replay['messages'][0]['audio_b64'])
                    self.assertNotEqual(replay['messages'][0]['audio_b64'], 'old-audio')
                    self.assertEqual(body['messages'][0]['audio_b64'], 'old-audio')
                    self.assertEqual(provider.call_count, 1)
                    self.assertEqual(http.call_count, 1)
                    provider.reset_mock()
                    http.reset_mock()

    def test_receipt_replay_discards_cached_audio_even_if_hydration_fails(self):
        with patch.object(tts, 'fish_tts') as provider:
            for text in SILENT:
                for unavailable in (False, True):
                    with self.subTest(text=text, unavailable=unavailable), patch(
                            'characters.get_character', return_value={'voice_id': 'offline'},
                            side_effect=RuntimeError('character read unavailable') if unavailable else None):
                        body = {'messages': [{'jp': text, 'zh': text, 'audio_b64': 'old-audio'}]}
                        replay = receipt.hydrate_replay(body, 'c')
                        self.assertEqual(replay['messages'][0], {'jp': text, 'zh': text, 'audio_b64': ''})
                        self.assertEqual(body['messages'][0]['audio_b64'], 'old-audio')
                        provider.assert_not_called()

    def test_resynth_returns_successful_silence_without_reference_voice(self):
        import route_tts
        with patch.object(route_tts, 'get_character', return_value={'name': 'offline'}), patch.object(
                tts, 'fish_tts') as provider:
            for text in SILENT:
                with self.subTest(text=text):
                    result = asyncio.run(route_tts.resynth({'text': text, 'character_id': 'c'}))
                    self.assertEqual(result.status_code, 200)
                    self.assertEqual(json.loads(result.body)['audio_b64'], '')
                    provider.assert_not_called()

    def test_delayed_reply_keeps_text_and_subtitles_with_zero_provider_calls(self):
        import delayed_reply
        from tests.test_delayed_reply import HelpersStub
        with ExitStack() as stack:
            for target, value in (
                ('characters.get_character', {'name': 'offline', 'voice_id': 'offline'}),
                ('user_memory.save_user_short_memory_once', True),
                ('user_memory.get_short_memory', []), ('user_memory.update_chat_days', 1),
                ('temporal_awareness.get_temporal_snapshot', {}),
                ('temporal_awareness.record_turn', None), ('prompt.build_system_blocks', []),
                ('behavior_evidence.record_reply_cycle', None),
                ('schedule_transition.commit_generated_schedule_intent', {'ok': True}),
                ('delayed_reply.assistant_already_committed', False),
                ('proactive_msg.add_proactive_msg', (1, datetime.now(timezone.utc)))):
                stack.enter_context(patch(target, return_value=value))
            commit = stack.enter_context(patch('user_memory.commit_visible_assistant_message'))
            provider = stack.enter_context(patch.object(tts, 'fish_tts'))
            stack.enter_context(patch.object(tts, 'tts_to_b64', REAL_TTS))
            for text in SILENT:
                with self.subTest(text=text):
                    helpers = HelpersStub(bubbles=[{'jp': text, 'zh': text}])
                    result = delayed_reply.generate_delayed_chat_reply(dict(
                        id=9, user_id='u', character_id='c', pending_text='你好',
                        pending_count=1, last_source_event_id='u1', reply_state='free'), helpers=helpers)
                    self.assertTrue(result['ok'], result)
                    self.assertEqual(result['messages'], [{'jp': text, 'zh': text, 'audio_b64': ''}])
                    self.assertEqual(commit.call_args.args[1], text)
                    self.assertEqual(commit.call_args.kwargs['subtitle'], text)
                    self.assertEqual(helpers.generate_calls, 1)
                    provider.assert_not_called()


class VoiceStreamSilentTests(unittest.TestCase):
    events = voice.VoiceStreamCommitGateTests.events

    def setUp(self):
        voice.VoiceStreamCommitGateTests.setUp(self)
        self.route.tts_to_b64 = REAL_TTS

    def test_silent_stream_preserves_segments_and_always_finishes(self):
        with patch.object(tts, 'fish_tts') as provider:
            for index, text in enumerate(SILENT):
                with self.subTest(text=text):
                    events = self.events(f'EMOTION: 平静\nJP: {text}\nZH: {text}\n',
                                         source_event_id=f'silent-{index}')
                    self.assertEqual(events[-1]['type'], 'done')
                    self.assertEqual(events[-1]['segments'], 1)
                    self.assertNotIn('generation_failed', [e['type'] for e in events])
                    segment = next(e for e in events if e['type'] == 'audio')
                    self.assertEqual((segment['jp'], segment['zh'], segment['audio_b64']), (text, text, ''))
                    self.assertEqual(self.short_rows[-1]['content'], text)
                    provider.assert_not_called()

    def test_mixed_stream_still_has_audio_and_finishes(self):
        with patch.object(tts, 'fish_tts', return_value=b'offline') as provider:
            text = '……こっちに来て。🥺'
            events = self.events(f'JP: {text}\nZH: ……你过来。🥺\n')
            self.assertEqual(events[-1]['type'], 'done')
            self.assertTrue(next(e for e in events if e['type'] == 'audio')['audio_b64'])
            provider.assert_called_once_with(text, '平静', 'v1')

    def test_bracket_stream_keeps_body_and_calls_real_provider_once(self):
        response = Mock(status_code=200)
        response.iter_content.return_value = [b'offline-mp3']
        with patch.object(tts.requests, 'post', return_value=response) as http, patch.object(
                tts, 'fish_tts', wraps=tts.fish_tts) as provider:
            for text in BRACKET_TEXT:
                with self.subTest(text=text):
                    events = self.events(f'JP: {text}\nZH: 这段我看到了。\n')
                    self.assertEqual(events[-1]['type'], 'done')
                    segment = next(e for e in events if e['type'] == 'audio')
                    self.assertEqual((segment['jp'], segment['zh']), (text, '这段我看到了。'))
                    self.assertTrue(segment['audio_b64'])
                    self.assertEqual(provider.call_count, 1)
                    self.assertEqual(http.call_count, 1)
                    provider.reset_mock()
                    http.reset_mock()


class NonverbalDeliveryTests(unittest.TestCase):
    setUpClass = classmethod(windows.ReceiptWindowTests.setUpClass.__func__)
    tearDownClass = classmethod(windows.ReceiptWindowTests.tearDownClass.__func__)
    setUp = windows.ReceiptWindowTests.setUp
    tearDown = windows.ReceiptWindowTests.tearDown
    sql = windows.ReceiptWindowTests.sql
    http = windows.ReceiptWindowTests.http
    request = windows.ReceiptWindowTests.request
    assistant_effect = windows.ReceiptWindowTests.assistant_effect
    drain_jobs = windows.ReceiptWindowTests.drain_jobs
    run_cycle = windows.ReceiptWindowTests.run_cycle

    def test_chat_and_receipt_replay_keep_raw_utterance_without_inferred_authority(self):
        from generation_side_effect_worker import process_one_side_effect
        with patch.object(tts, 'fish_tts') as provider:
            for index, text in enumerate(SILENT):
                with self.subTest(text=text), self.http(text, translation=text) as (route, generate), patch.object(
                        tts, 'tts_to_b64', REAL_TTS), patch.object(route, 'tts_to_b64', REAL_TTS):
                    source = f'nonverbal-{index}'
                    request = dict(self.request(source), text='你好')
                    for _ in range(2):
                        response = asyncio.run(route.chat_text(request))
                        self.assertEqual(response.status_code, 200)
                        msg = json.loads(response.body)['messages'][0]
                        self.assertEqual((msg['jp'], msg['zh'], msg['audio_b64']), (text, text, ''))
                    generate.assert_called_once()
                    self.assertTrue(process_one_side_effect(self.assistant_effect(source)))
                    self.drain_jobs()
                    self.run_cycle()
                    self.run_cycle()
                    self.assertEqual(self.sql("SELECT text FROM chat_log WHERE event_id=%s",
                                             ('chat_reply:' + source,)), [(text,)])
                    self.assertEqual(self.sql("SELECT payload->>'content' FROM cognitive_events WHERE source_event_id=%s",
                                             ('chat_reply:' + source,)), [(text,)])
                    provider.assert_not_called()
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory')[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM bond_memory WHERE content NOT LIKE '角色实际说过：%'")[0][0], 0)

    def test_proactive_cache_cannot_return_or_store_speech_for_nonverbal(self):
        import proactive_msg
        self.stack.enter_context(patch.object(proactive_msg, 'get_conn', return_value=self.database))
        proactive_msg.init_proactive_table()
        with patch.object(tts, 'fish_tts') as provider:
            for text in SILENT:
                mid, _ = proactive_msg.add_proactive_msg('c', 'u', 'promise', text, text, audio_b64='stale')
                self.assertEqual(self.sql('SELECT audio_b64 FROM proactive_msg WHERE id=%s', (mid,)), [('',)])
            # Simulate old rows that predate the gate, including old delayed audio.
            self.sql("UPDATE proactive_msg SET audio_b64='legacy-audio'")
            self.database.commit()
            messages = proactive_msg.get_pending('u', 'c')
            self.assertEqual([m['jp'] for m in messages], list(SILENT))
            self.assertEqual([m['zh'] for m in messages], list(SILENT))
            self.assertTrue(all(m['audio_b64'] == '' for m in messages))
            provider.assert_not_called()
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)

    def test_bracket_proactive_cache_preserves_text_and_audio(self):
        import proactive_msg
        self.stack.enter_context(patch.object(proactive_msg, 'get_conn', return_value=self.database))
        proactive_msg.init_proactive_table()
        for text in BRACKET_TEXT:
            proactive_msg.add_proactive_msg('c', 'u', 'promise', text, text, audio_b64='cached-audio')
        messages = proactive_msg.get_pending('u', 'c')
        self.assertEqual([(m['jp'], m['zh'], m['audio_b64']) for m in messages],
                         [(text, text, 'cached-audio') for text in BRACKET_TEXT])

    def test_proactive_generator_calls_once_and_keeps_silent_raw_source(self):
        import proactive_msg
        import proactive_scheduler as scheduler
        self.stack.enter_context(patch.object(proactive_msg, 'get_conn', return_value=self.database))
        proactive_msg.init_proactive_table()
        with ExitStack() as stack:
            for name, value in (('get_character', {'name': 'offline', 'voice_id': 'offline'}),
                                ('get_bond_memories', []), ('get_relations_text', ''),
                                ('get_temporal_snapshot', {}), ('build_prompt_context', ''),
                                ('record_assistant_message', None)):
                stack.enter_context(patch.object(scheduler, name, return_value=value))
            stack.enter_context(patch('context_layer.load_profile_transcript', return_value=('recent', None)))
            stack.enter_context(patch('push_notify.push_to_user'))
            stack.enter_context(patch.object(scheduler.db_promise, 'mark_fired'))
            stack.enter_context(patch.object(tts, 'tts_to_b64', REAL_TTS))
            provider = stack.enter_context(patch.object(tts, 'fish_tts'))
            generate = stack.enter_context(patch.object(scheduler.claude_client.messages, 'create'))
            stack.enter_context(patch.object(scheduler, 'extract_text', return_value=json.dumps(
                {'jp': '🥺...', 'zh': '🥺...', 'emotion': '平静'})))
            result = scheduler.generate_from_promise(dict(id=1, character_id='c', user_id='u',
                context='previous reminder', origin_text='', trigger_kind='time'), datetime.now(timezone.utc))
            self.assertIsNotNone(result)
            generate.assert_called_once()
            provider.assert_not_called()
        self.assertEqual(proactive_msg.get_pending('u', 'c')[0]['audio_b64'], '')
        self.assertEqual(self.sql("SELECT text,subtitle FROM chat_log"), [('🥺...', '🥺...')])
