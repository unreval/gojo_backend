import json
import os
import sys
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

import db_generation_receipt as receipt  # noqa: E402
import sticker_registry  # noqa: E402


def _reply(sticker_id='happy_01'):
    return json.dumps({
        'emotion': '平静',
        'messages': [{'jp': 'やあ。', 'zh': '嘿'}],
        'sticker_id': sticker_id,
    }, ensure_ascii=False)


class StickerIsolationTests(unittest.TestCase):
    def setUp(self):
        from test_chat_commit_gate import ChatCommitGateTests
        self.gate = ChatCommitGateTests(
            'test_provider_403_fails_once_without_assistant_commit')
        self.gate.setUp()

    def tearDown(self):
        self.gate.doCleanups()

    def test_finalize_keeps_messages_and_full_jp_free_of_sticker_text(self):
        base = {'emotion': '平静', 'messages': [{'jp': 'やあ。', 'zh': '嘿'}]}
        with_sticker = dict(base, sticker_id='happy_01')
        emotion, plain = self.gate.route._finalize_committed(base)
        emotion_with, stamped = self.gate.route._finalize_committed(with_sticker)
        self.assertEqual(emotion, emotion_with)
        self.assertEqual(plain, stamped)
        self.assertEqual(len(plain), 1)
        full_jp = ' '.join(item['jp'] for item in stamped)
        self.assertEqual(full_jp, 'やあ。')
        self.assertNotIn('happy_01', full_jp)
        self.assertNotIn('表情', full_jp)

    def _enabled(self):
        return patch.object(sticker_registry, 'is_enabled', return_value=True)

    def test_three_turns_keep_only_the_first_sticker(self):
        history = []

        def get_messages(user_id, chat_id, limit=200, before_id=None):
            return list(history), False

        with self._enabled(), \
                patch.object(sticker_registry, 'get', return_value={
                    'id': 'happy_01', 'desc': '笑着比耶', 'file': 'happy_01.png',
                    'prefix': 'stickers/v1/'}), \
                patch.object(sticker_registry, 'signed_url', return_value='https://example/sticker'), \
                patch('db_chatlog.get_messages', side_effect=get_messages):
            for index, expect in enumerate((True, False, False)):
                response, body = self.gate.send(
                    [_reply()], source_event_id=f'sticker-cool-{index}')
                self.assertEqual(response.status_code, 200, body)
                self.assertEqual(body['messages'][0]['jp'], 'やあ。')
                self.assertNotIn('happy_01', ' '.join(
                    item.get('jp') or '' for item in body['messages']))
                if expect:
                    self.assertEqual(body['sticker'], {
                        'id': 'happy_01', 'url': 'https://example/sticker', 'desc': '笑着比耶',
                    })
                else:
                    self.assertNotIn('sticker', body)
                history.append({'role': 'user', 'kind': 'text', 'text': 'hi', 'extra': ''})
                history.append({'role': 'gojo', 'kind': 'text', 'text': 'やあ', 'extra': ''})
                if body.get('sticker'):
                    history.append({
                        'role': 'gojo', 'kind': 'sticker', 'text': '',
                        'extra': json.dumps({
                            'sticker_id': 'happy_01', 'sticker_desc': '笑着比耶'}),
                    })
        assistant = [row['content'] for row in self.gate.short_rows if row['role'] == 'assistant']
        self.assertTrue(assistant)
        self.assertTrue(all('happy_01' not in content and '表情' not in content
                            for content in assistant))

    def test_text_reply_survives_missing_catalog_unconfigured_storage_and_unknown_id(self):
        previous_path = sticker_registry.MANIFEST_PATH
        previous_cache = sticker_registry._CACHE
        try:
            sticker_registry.MANIFEST_PATH = os.path.join(self.gate.route.__file__, 'missing.json')
            sticker_registry._CACHE = None
            with patch('media_storage.is_configured', return_value=True):
                response, body = self.gate.send(
                    [_reply()], source_event_id='sticker-missing')
            self.assertEqual(response.status_code, 200, body)
            self.assertEqual(body['messages'][0]['jp'], 'やあ。')
            self.assertNotIn('sticker', body)
            self.assertFalse(body.get('generation_failed'))
            self.assertFalse(body.get('generation_in_progress'))

            sticker_registry.MANIFEST_PATH = os.path.join(BACKEND, 'stickers.json')
            sticker_registry._CACHE = None
            with patch('media_storage.is_configured', return_value=False):
                response, body = self.gate.send(
                    [_reply()], source_event_id='sticker-unconfigured')
            self.assertEqual(response.status_code, 200, body)
            self.assertEqual(body['messages'][0]['zh'], '嘿')
            self.assertNotIn('sticker', body)

            sticker_registry._CACHE = None
            with patch('media_storage.is_configured', return_value=True):
                response, body = self.gate.send(
                    [_reply('no_such_sticker')], source_event_id='sticker-unknown')
            self.assertEqual(response.status_code, 200, body)
            self.assertNotIn('sticker', body)
            self.assertIn(
                '[sticker] unknown id=no_such_sticker',
                ' '.join(str(call.args[0]) for call in self.gate.log.call_args_list
                         if call.args))
        finally:
            sticker_registry.MANIFEST_PATH = previous_path
            sticker_registry._CACHE = previous_cache


class StickerReceiptTests(unittest.TestCase):
    def test_canonical_payload_keeps_id_and_desc_only(self):
        payload = receipt.canonical_payload({
            'emotion': '平静',
            'messages': [{'jp': 'やあ', 'zh': '嘿', 'audio_b64': 'secret'}],
            'sticker': {'id': 'happy_01', 'desc': '笑着比耶', 'url': 'https://dead.example/s'},
        })
        self.assertEqual(payload['sticker'], {'id': 'happy_01', 'desc': '笑着比耶'})
        self.assertNotIn('url', payload['sticker'])
        self.assertNotIn('audio_b64', payload['messages'][0])
        self.assertEqual(
            receipt.needed_effects('chat_text', {'messages': [{'jp': 'やあ', 'zh': '嘿'}]}),
            receipt.needed_effects('chat_text', payload),
        )

    def test_hydrate_replay_refills_or_drops_the_sticker(self):
        stored = {
            'emotion': '平静',
            'messages': [{'jp': 'やあ', 'zh': '嘿'}],
            'sticker': {'id': 'happy_01', 'desc': '笑着比耶'},
        }
        with patch('tts.tts_to_b64', return_value=''), \
                patch('sticker_registry.signed_url', return_value='https://fresh.example/s'):
            live = receipt.hydrate_replay(stored, 'gojo')
        self.assertEqual(live['sticker']['url'], 'https://fresh.example/s')
        self.assertEqual(live['sticker']['id'], 'happy_01')
        self.assertEqual(live['messages'][0]['jp'], 'やあ')

        with patch('tts.tts_to_b64', return_value=''), \
                patch('sticker_registry.signed_url', return_value=None):
            dropped = receipt.hydrate_replay(stored, 'gojo')
        self.assertNotIn('sticker', dropped)
        self.assertEqual(dropped['messages'][0]['jp'], 'やあ')

        with patch('tts.tts_to_b64', return_value=''), \
                patch('sticker_registry.signed_url', side_effect=RuntimeError('r2')):
            failed = receipt.hydrate_replay(stored, 'gojo')
        self.assertNotIn('sticker', failed)


if __name__ == '__main__':
    unittest.main()
