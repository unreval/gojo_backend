from contextlib import ExitStack
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

import db_chatlog  # noqa: E402
import db_generation_receipt as receipt  # noqa: E402
import sticker_registry  # noqa: E402


class _StickerCursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.sql = ''
        self.params = None
        self._fetched = []

    def execute(self, sql, params):
        self.sql = sql
        self.params = params
        limit = int(params[-1])
        ordered = sorted(self.rows, key=lambda row: row[0], reverse=True)
        self._fetched = ordered[:limit]

    def fetchall(self):
        return list(self._fetched)

    def close(self):
        return None


class _StickerConn:
    def __init__(self, rows):
        self._cursor = _StickerCursor(rows)

    def cursor(self):
        return self._cursor

    def close(self):
        return None


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

        def get_conn():
            rows = []
            for index, msg in enumerate(history, 1):
                if msg.get('role') not in ('gojo', 'assistant'):
                    continue
                rows.append((index, msg.get('kind') or 'text', msg.get('extra') or ''))
            return _StickerConn(rows)

        with self._enabled(), \
                patch.object(sticker_registry, 'get', return_value={
                    'id': 'happy_01', 'desc': '笑着比耶', 'file': 'happy_01.png',
                    'prefix': 'stickers/v1/'}), \
                patch.object(sticker_registry, 'signed_url', return_value='https://example/sticker'), \
                patch('db_chatlog.get_conn', side_effect=get_conn):
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
                turn_id = f'cool-{index}'
                history.append({'role': 'user', 'kind': 'text', 'text': 'hi', 'extra': ''})
                history.append({
                    'role': 'gojo', 'kind': 'text', 'text': 'やあ',
                    'extra': json.dumps({'assistant_turn_id': turn_id}),
                })
                if body.get('sticker'):
                    history.append({
                        'role': 'gojo', 'kind': 'sticker', 'text': '',
                        'extra': json.dumps({
                            'assistant_turn_id': turn_id,
                            'sticker_id': 'happy_01',
                            'sticker_desc': '笑着比耶',
                        }),
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


class StickerCooldownQueryTests(unittest.TestCase):
    def setUp(self):
        from test_chat_commit_gate import ChatCommitGateTests
        self.gate = ChatCommitGateTests(
            'test_provider_403_fails_once_without_assistant_commit')
        self.gate.setUp()

    def tearDown(self):
        self.gate.doCleanups()

    def _catalog(self, stack):
        stack.enter_context(patch.object(sticker_registry, 'is_enabled', return_value=True))

        def get(sticker_id):
            return {
                'id': sticker_id, 'desc': '笑着比耶',
                'file': f'{sticker_id}.png', 'prefix': 'stickers/v1/',
            }

        stack.enter_context(patch.object(sticker_registry, 'get', side_effect=get))
        stack.enter_context(patch.object(
            sticker_registry, 'signed_url', return_value='https://example/sticker'))

    def _resolve(self, rows, sticker_id='happy_01'):
        calls = []

        def get_conn():
            conn = _StickerConn(rows)
            calls.append(conn)
            return conn

        with patch('db_chatlog.get_conn', side_effect=get_conn):
            result = self.gate.route._resolve_sticker('u', 'gojo', sticker_id)
        return result, calls

    def test_cooldown_does_not_touch_media_paths(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError('media path')

        with ExitStack() as stack:
            self._catalog(stack)
            signed = stack.enter_context(
                patch('media_storage.signed_get_url', side_effect=boom))
            media = stack.enter_context(
                patch('db_chat_media.get_media_by_source_events', side_effect=boom))
            result, calls = self._resolve([])
        self.assertEqual(result, {
            'id': 'happy_01', 'url': 'https://example/sticker', 'desc': '笑着比耶',
        })
        self.assertEqual(signed.call_count, 0)
        self.assertEqual(media.call_count, 0)
        self.assertEqual(len(calls), 1)

    def test_cooldown_does_not_scan_hidden_aggregates(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError('aggregate scan')

        with ExitStack() as stack:
            self._catalog(stack)
            hidden = stack.enter_context(
                patch('db_chatlog.hidden_aggregate_event_ids', side_effect=boom))
            result, _calls = self._resolve([])
        self.assertEqual(result['id'], 'happy_01')
        self.assertEqual(hidden.call_count, 0)

    def test_cooldown_query_is_one_connection_and_limit_stays_at_most_120(self):
        with ExitStack() as stack:
            self._catalog(stack)
            _result, calls = self._resolve([])
        self.assertEqual(len(calls), 1)
        limit = calls[0].cursor().params[-1]
        self.assertEqual(limit, 60)
        self.assertLessEqual(limit, 120)
        self.assertNotIn('chat_media', calls[0].cursor().sql)

        import sys
        sys.modules['config'].STICKER_COOLDOWN_TURNS = 100
        sys.modules['config'].STICKER_REPEAT_WINDOW = 100
        with ExitStack() as stack:
            self._catalog(stack)
            _capped, capped_calls = self._resolve([])
        self.assertEqual(len(capped_calls), 1)
        self.assertEqual(capped_calls[0].cursor().params[-1], 120)

    def test_shared_assistant_turn_id_is_one_turn(self):
        turn_id = 'turn-1'
        rows = [
            (4, 'text', json.dumps({'assistant_turn_id': turn_id})),
            (3, 'text', json.dumps({'assistant_turn_id': turn_id})),
            (2, 'text', json.dumps({'assistant_turn_id': turn_id})),
            (1, 'sticker', json.dumps({
                'assistant_turn_id': turn_id, 'sticker_id': 'happy_01'})),
        ]
        calls = []

        def get_conn():
            conn = _StickerConn(rows)
            calls.append(conn)
            return conn

        with patch('db_chatlog.get_conn', side_effect=get_conn):
            turns = db_chatlog.recent_assistant_sticker_turns('u', 'gojo', 60)
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0], {'happy_01'})
        with ExitStack() as stack:
            self._catalog(stack)
            with patch('db_chatlog.get_conn', side_effect=get_conn):
                blocked = self.gate.route._resolve_sticker('u', 'gojo', 'sad_01')
        self.assertIsNone(blocked)

    def test_missing_assistant_turn_id_does_not_break_grouping(self):
        rows = [
            (3, 'text', '{}'),
            (2, 'sticker', json.dumps({'sticker_id': 'happy_01'})),
            (1, 'text', 'not-json'),
        ]
        with patch('db_chatlog.get_conn', side_effect=lambda: _StickerConn(rows)):
            turns = db_chatlog.recent_assistant_sticker_turns('u', 'gojo', 60)
        self.assertEqual(len(turns), 3)
        self.assertEqual(turns[0], set())
        self.assertEqual(turns[1], {'happy_01'})
        self.assertEqual(turns[2], set())

        unknown_rows = [(8, 'sticker', {}), (7, 'text', {})]
        with patch('db_chatlog.get_conn', side_effect=lambda: _StickerConn(unknown_rows)):
            unknown = db_chatlog.recent_assistant_sticker_turns('u', 'gojo', 60)
        self.assertEqual(unknown[0], {db_chatlog.UNKNOWN_STICKER})
        self.assertEqual(unknown[1], set())
        with ExitStack() as stack:
            self._catalog(stack)
            with patch('db_chatlog.get_conn', side_effect=lambda: _StickerConn(unknown_rows)):
                blocked = self.gate.route._resolve_sticker('u', 'gojo', 'happy_01')
        self.assertIsNone(blocked)

    def test_history_read_failure_drops_the_sticker_without_raising(self):
        with ExitStack() as stack:
            self._catalog(stack)
            stack.enter_context(patch(
                'db_chatlog.get_conn', side_effect=RuntimeError('db down')))
            result = self.gate.route._resolve_sticker('u', 'gojo', 'happy_01')
        self.assertIsNone(result)


class StickerHistoryTests(unittest.TestCase):
    def test_history_renders_desc_without_reading_the_catalog(self):
        self.assertEqual(
            db_chatlog._history_content(
                'gojo', '', '', 'sticker', {'sticker_desc': '笑着比耶'}),
            '【表情】笑着比耶')
        self.assertEqual(
            db_chatlog._history_content('gojo', '', '', 'sticker', {}),
            '【表情】')


if __name__ == '__main__':
    unittest.main()
