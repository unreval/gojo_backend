"""Offline D-stage checks for partial translation and single-bubble repair."""
import asyncio
import copy
import importlib.util
import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def _load_source(name, stubs):
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(
            f'{name}_translation_test', os.path.join(BACKEND, f'{name}.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class _Router:
    def post(self, _path):
        return lambda fn: fn

    def get(self, _path):
        return lambda fn: fn


class _JSONResponse:
    def __init__(self, body, status_code=200):
        self.body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        self.status_code = status_code


_db_stub = types.ModuleType('db')
_db_stub.get_conn = lambda: None
_fastapi_stub = types.ModuleType('fastapi')
_fastapi_stub.APIRouter = _Router
_responses_stub = types.ModuleType('fastapi.responses')
_responses_stub.JSONResponse = _JSONResponse
_stubs = {'db': _db_stub, 'fastapi': _fastapi_stub,
          'fastapi.responses': _responses_stub}
db_chatlog = _load_source('db_chatlog', _stubs)
receipts = _load_source('db_generation_receipt', _stubs)
generation_contract = _load_source('generation_contract', _stubs)
db_translation = _load_source('db_translation', _stubs)
proactive_msg = _load_source('proactive_msg', _stubs)
route_chatlog_search = _load_source(
    'route_chatlog_search', {**_stubs, 'db_chatlog': db_chatlog})
route_translation = _load_source(
    'route_translation', {**_stubs, 'db_translation': db_translation})


class FakeConnection:
    def __init__(self, *, status='completed', jp='今日は話そう', zh='',
                 subtitle='', row_status='active', tombstoned=False,
                 event_id='chat_reply:source-1:0'):
        self.payload = {
            'messages': [{'event_id': 'chat_reply:source-1:0',
                          'jp': jp, 'zh': zh}],
            'source_event_id': 'source-1',
        }
        self.status = status
        self.endpoint = 'chat_text'
        self.event_id = event_id
        self.row = {
            'id': 7, 'role': 'gojo', 'text': jp, 'subtitle': subtitle,
            'extra': '{}', 'status': row_status, 'tombstoned': tombstoned,
        }
        self.proactive = {
            'id': 21, 'jp': jp, 'zh': zh, 'translation_source': ''}
        self.commits = 0
        self.rollbacks = 0
        self.writes = []

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.one = None
        self.many = []
        self.rowcount = 0

    def close(self):
        pass

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many

    def execute(self, sql, params):
        compact = ' '.join(sql.split())
        self.rowcount = 0
        self.one = None
        self.many = []
        if compact.startswith('SELECT status, response_json FROM chat_generation_receipt'):
            user, chat, source, endpoint = params
            if (user, chat, source, endpoint) == (
                    'u', 'gojo', 'source-1', self.conn.endpoint):
                self.one = (self.conn.status, copy.deepcopy(self.conn.payload))
        elif compact.startswith('SELECT id, role, text, subtitle, extra,'):
            user, chat, event = params
            if (user, chat, event) == ('u', 'gojo', self.conn.event_id):
                r = self.conn.row
                self.one = (r['id'], r['role'], r['text'], r['subtitle'],
                            r['extra'], r['status'], r['tombstoned'])
        elif compact.startswith('SELECT id, jp, zh, translation_source FROM proactive_msg'):
            user, chat, event = params
            if (user, chat, event) == ('u', 'gojo', self.conn.event_id):
                r = self.conn.proactive
                self.many = [(r['id'], r['jp'], r['zh'],
                              r['translation_source'])]
        elif compact.startswith('SELECT id, character_id, kind, jp, zh,'):
            user, chat = params
            if (user, chat) == ('u', 'gojo'):
                r = self.conn.proactive
                self.many = [(r['id'], 'gojo', 'delayed_reply', r['jp'],
                              r['zh'], '平静', '', datetime(2026, 10, 4),
                              self.conn.event_id, self.conn.event_id, 0,
                              r['translation_source'])]
        elif compact.startswith('UPDATE chat_log SET subtitle='):
            subtitle, extra, row_id, user, chat, jp, allowed_subtitle = params
            r = self.conn.row
            if (row_id, user, chat, jp) == (7, 'u', 'gojo', r['text']) \
                    and r['status'] == 'active' \
                    and r['subtitle'] in ('', allowed_subtitle):
                r['subtitle'], r['extra'] = subtitle, extra
                self.rowcount = 1
                self.conn.writes.append('chat_log')
        elif compact.startswith('UPDATE chat_generation_receipt SET response_json='):
            encoded, user, chat, source, endpoint = params
            if (user, chat, source, endpoint) == (
                    'u', 'gojo', 'source-1', self.conn.endpoint) \
                    and self.conn.status == 'completed':
                self.conn.payload = json.loads(encoded)
                self.rowcount = 1
                self.conn.writes.append('receipt')
        elif compact.startswith('UPDATE proactive_msg SET zh='):
            zh, source, row_id, user, chat, event, jp = params
            r = self.conn.proactive
            if (row_id, user, chat, event, jp) == (21, 'u', 'gojo',
                                                  self.conn.event_id, r['jp']) \
                    and not r['zh']:
                r['zh'], r['translation_source'] = zh, source
                self.rowcount = 1
                self.conn.writes.append('proactive_msg')
        else:
            raise AssertionError(f'unexpected SQL: {compact}')


class TranslationRepairTests(unittest.TestCase):
    def setUp(self):
        self.conn = FakeConnection()
        self.conn_patch = patch.object(
            db_translation, 'get_conn', side_effect=lambda: self.conn)
        self.conn_patch.start()
        self.addCleanup(self.conn_patch.stop)
        self.request = {
            'user_id': 'u', 'character_id': 'gojo',
            'source_event_id': 'source-1', 'endpoint': 'chat_text',
            'event_id': 'chat_reply:source-1:0',
            'jp': '今日は話そう', 'zh': '今天聊聊吧',
        }

    def call(self, **changes):
        payload = dict(self.request, **changes)
        response = asyncio.run(route_translation.repair_translation(payload))
        return response.status_code, json.loads(response.body)

    def call_restore(self, **changes):
        payload = dict(self.request, **changes)
        payload.pop('zh', None)
        response = asyncio.run(route_translation.restore_translation(payload))
        return response.status_code, json.loads(response.body)

    def test_partial_reply_is_flagged_without_fake_chinese_or_paid_call(self):
        with patch.dict(sys.modules, {
                'tts': types.SimpleNamespace(tts_to_b64=lambda *_args: 'audio'),
                'characters': types.SimpleNamespace(get_character=lambda *_args: {})}):
            result = receipts.hydrate_replay(
                self.conn.payload, 'gojo', user_id=None)
        self.assertTrue(result['translation_missing'])
        self.assertTrue(result['messages'][0]['translation_missing'])
        self.assertEqual(result['messages'][0]['zh'], '')
        self.assertEqual(self.conn.payload['messages'][0]['zh'], '')

        nonverbal = {'messages': [{'jp': '😒', 'zh': '😒'}]}
        with patch.dict(sys.modules, {
                'tts': types.SimpleNamespace(tts_to_b64=lambda *_args: ''),
                'characters': types.SimpleNamespace(get_character=lambda *_args: {})}):
            result = receipts.hydrate_replay(nonverbal, 'gojo', user_id=None)
        self.assertFalse(result['translation_missing'])

    def test_adjacent_complete_and_missing_bubbles_have_independent_status(self):
        payload = {'messages': [
            {'event_id': 'chat_reply:source-1:0', 'jp': 'そうだね', 'zh': '是啊'},
            {'event_id': 'chat_reply:source-1:1', 'jp': 'またね', 'zh': ''},
        ]}
        with patch.dict(sys.modules, {
                'tts': types.SimpleNamespace(tts_to_b64=lambda *_args: ''),
                'characters': types.SimpleNamespace(get_character=lambda *_args: {})}):
            result = receipts.hydrate_replay(payload, 'gojo', user_id=None)
        self.assertTrue(result['translation_missing'])
        self.assertEqual([m['translation_missing'] for m in result['messages']],
                         [False, True])
        self.assertEqual([m['zh'] for m in result['messages']], ['是啊', ''])

    def test_search_and_date_rows_expose_partial_translation(self):
        base = (7, 'chat_reply:source-1:0', 'gojo', '今日は話そう', '',
                '', 'text', '', False,
                datetime(2026, 10, 4, tzinfo=timezone.utc),
                'chat_reply:source-1:0')
        missing = route_chatlog_search._search_row(base)
        self.assertTrue(missing['translation_missing'])
        translated = route_chatlog_search._search_row(
            base[:4] + ('今天聊聊吧',) + base[5:7]
            + ('{"translation_source":"user_supplied"}',) + base[8:])
        self.assertFalse(translated['translation_missing'])
        self.assertEqual(translated['translation_source'], 'user_supplied')

    def test_explicit_repair_updates_only_one_subtitle_and_reloads(self):
        status, body = self.call()
        self.assertEqual((status, body['status']), (200, 'repaired'))
        self.assertEqual(body['translation_source'], 'user_supplied')
        self.assertEqual(self.conn.writes, ['chat_log', 'receipt'])
        self.assertEqual(self.conn.commits, 1)
        self.assertEqual(self.conn.payload['messages'][0]['zh'], '今天聊聊吧')
        self.assertEqual(self.conn.row['text'], '今日は話そう')
        self.assertEqual(self.conn.row['subtitle'], '今天聊聊吧')
        self.assertEqual(json.loads(self.conn.row['extra'])['translation_source'],
                         'user_supplied')
        self.assertNotIn('audio_b64', self.conn.payload['messages'][0])

        row = (7, 'chat_reply:source-1:0', 'gojo', self.conn.row['text'],
               self.conn.row['subtitle'], '', 'text', self.conn.row['extra'],
               False, datetime(2026, 10, 4, tzinfo=timezone.utc),
               'chat_reply:source-1:0')
        reloaded = db_chatlog._row_to_message(row)
        self.assertEqual(reloaded['subtitle'], body['zh'])
        self.assertEqual(reloaded['text'], '今日は話そう')

        again, duplicate = self.call()
        self.assertEqual((again, duplicate['status']), (200, 'already_identical'))
        self.assertEqual(self.conn.writes, ['chat_log', 'receipt'])

    def test_one_segment_repair_preserves_other_missing_segment(self):
        self.conn.payload['messages'].append({
            'event_id': 'chat_reply:source-1:1', 'jp': 'またね', 'zh': ''})
        status, body = self.call()
        self.assertEqual((status, body['status']), (200, 'repaired'))
        self.assertTrue(body['translation_missing'])
        self.assertEqual(self.conn.payload['messages'][1]['zh'], '')
        self.assertTrue(self.conn.payload['translation_missing'])

    def test_existing_receipt_translation_repairs_lost_chatlog_subtitle(self):
        self.conn.payload['messages'][0]['zh'] = '今天聊聊吧'
        status, body = self.call()
        self.assertEqual((status, body['status']), (200, 'repaired'))
        self.assertEqual(body['translation_source'], 'receipt')
        self.assertEqual(self.conn.writes, ['chat_log'])
        self.assertEqual(self.conn.row['subtitle'], '今天聊聊吧')

    def test_restore_uses_successful_receipt_without_user_translation(self):
        self.conn.payload['messages'][0]['zh'] = '已有的准确译文'
        status, body = self.call_restore()
        self.assertEqual((status, body['status']), (200, 'repaired'))
        self.assertEqual(body['zh'], '已有的准确译文')
        self.assertEqual(body['translation_source'], 'receipt')
        self.assertEqual(self.conn.row['subtitle'], '已有的准确译文')
        self.assertEqual(self.conn.writes, ['chat_log'])

        # A racing manual submission cannot replace the successful version.
        status, body = self.call(zh='用户另写的一版')
        self.assertEqual((status, body['status']), (200, 'already_identical'))
        self.assertEqual(body['translation_source'], 'receipt')
        self.assertEqual(self.conn.row['subtitle'], '已有的准确译文')

    def test_restore_without_successful_source_keeps_missing_state(self):
        status, body = self.call_restore()
        self.assertEqual((status, body['status']), (200, 'translation_unavailable'))
        self.assertEqual(self.conn.writes, [])
        self.assertEqual(self.conn.row['subtitle'], '')
        self.assertEqual(self.conn.payload['messages'][0]['zh'], '')

        self.conn.payload['messages'][0]['zh'] = '已有译文'
        status, body = self.call_restore(jp='别的原文')
        self.assertEqual((status, body['status']), (409, 'identity_mismatch'))
        self.assertEqual(self.conn.writes, [])

    def test_image_reply_restores_only_matching_event_and_original(self):
        self.conn.endpoint = 'chat_image'
        self.conn.event_id = 'image_reply:source-1:0'
        self.conn.payload['messages'][0]['event_id'] = self.conn.event_id
        self.conn.payload['messages'][0]['zh'] = '这是已有的图片回复译文'
        status, body = self.call_restore(
            endpoint='chat_image', event_id=self.conn.event_id)
        self.assertEqual((status, body['status']), (200, 'repaired'))
        self.assertEqual(body['translation_source'], 'receipt')
        self.assertEqual(self.conn.row['subtitle'], '这是已有的图片回复译文')
        self.assertEqual(self.conn.writes, ['chat_log'])

    def test_restore_does_not_override_conflicting_existing_subtitle(self):
        self.conn.payload['messages'][0]['zh'] = '已有译文'
        self.conn.row['subtitle'] = '其他译文'
        status, body = self.call_restore()
        self.assertEqual((status, body['status']), (409, 'translation_conflict'))
        self.assertEqual(self.conn.writes, [])

    def test_delayed_reply_repairs_delivery_and_canonical_subtitle(self):
        self.conn.event_id = 'delayed_reply:check-1:0'
        with patch.object(proactive_msg, 'get_conn', return_value=self.conn):
            self.assertTrue(proactive_msg.get_pending('u', 'gojo')[0][
                'translation_missing'])
        status, body = self.call(endpoint='delayed_reply',
                                 source_event_id='check-1',
                                 event_id=self.conn.event_id)
        self.assertEqual((status, body['status']), (200, 'repaired'))
        self.assertEqual(self.conn.writes, ['chat_log', 'proactive_msg'])
        self.assertEqual(self.conn.proactive['zh'], '今天聊聊吧')
        self.assertEqual(self.conn.row['subtitle'], '今天聊聊吧')
        self.assertEqual(self.conn.proactive['translation_source'], 'user_supplied')
        with patch.object(proactive_msg, 'get_conn', return_value=self.conn):
            pending = proactive_msg.get_pending('u', 'gojo')[0]
        self.assertFalse(pending['translation_missing'])
        self.assertEqual(pending['translation_source'], 'user_supplied')
        again, duplicate = self.call(endpoint='delayed_reply',
                                     source_event_id='check-1',
                                     event_id=self.conn.event_id)
        self.assertEqual((again, duplicate['status']), (200, 'already_identical'))

    def test_delayed_restore_uses_same_event_and_original_version(self):
        self.conn.event_id = 'delayed_reply:check-1:0'
        self.conn.proactive['zh'] = '已有延迟译文'
        status, body = self.call_restore(endpoint='delayed_reply',
                                          source_event_id='check-1',
                                          event_id=self.conn.event_id)
        self.assertEqual((status, body['status']), (200, 'repaired'))
        self.assertEqual(body['translation_source'], 'delivery')
        self.assertEqual(self.conn.row['subtitle'], '已有延迟译文')
        self.assertEqual(self.conn.writes, ['chat_log'])

    def test_japanese_teaching_example_is_legal_only_with_chinese_explanation(self):
        good = '「おはようございます」的意思是“早上好”。'
        status, body = self.call(zh=good)
        self.assertEqual((status, body['status']), (200, 'repaired'))
        self.assertEqual(self.conn.row['subtitle'], good)
        self.assertEqual(body['translation_source'], 'user_supplied')

        for invalid in ('おはようございます早上好',
                        '「おはようございます」',
                        '「おはようございます」好',
                        '今日は話そう',
                        '{"messages":[{"jp":"おはよう"}]}'):
            with self.subTest(invalid=invalid):
                self.conn = FakeConnection()
                status, body = self.call(zh=invalid)
                self.assertEqual((status, body['status']),
                                 (400, 'invalid_translation'))
                self.assertEqual(self.conn.writes, [])

    def test_generation_gate_accepts_quoted_teaching_examples_but_rejects_japanese_subtitles(self):
        for zh in ('「おはようございます」的意思是“早上好”。',
                   '“今日はいい天気ですね”表示“今天天气不错”。'):
            with self.subTest(accepted=zh):
                self.assertIsNone(generation_contract.rejection_reason({
                    'messages': [{'jp': 'そうだね', 'zh': zh}]}))
        for zh in ('ねえ、悟って呼んで。', '「おはようございます」',
                   '「おはようございます」好', 'そうだね'):
            with self.subTest(rejected=zh):
                self.assertIsNotNone(generation_contract.rejection_reason({
                    'messages': [{'jp': 'そうだね', 'zh': zh}]}))

    def test_delayed_reply_requires_matching_original_delivery(self):
        self.conn.event_id = 'delayed_reply:check-1:0'
        self.conn.proactive['jp'] = '別の発言'
        status, body = self.call(endpoint='delayed_reply',
                                 source_event_id='check-1',
                                 event_id=self.conn.event_id)
        self.assertEqual((status, body['status']), (409, 'identity_mismatch'))
        self.assertEqual(self.conn.writes, [])

    def test_scope_deletion_conflicts_and_fake_translation_are_rejected(self):
        cases = (
            ({'user_id': 'other'}, 'receipt_unavailable'),
            ({'character_id': 'other'}, 'receipt_unavailable'),
            ({'event_id': 'chat_reply:source-1:1'}, 'identity_mismatch'),
            ({'jp': '別の発言'}, 'identity_mismatch'),
            ({'zh': '今日は話そう'}, 'invalid_translation'),
            ({'zh': ''}, 'invalid_translation'),
        )
        for changes, expected in cases:
            with self.subTest(changes=changes):
                status, body = self.call(**changes)
                self.assertGreaterEqual(status, 400)
                self.assertEqual(body['status'], expected)
                self.assertEqual(self.conn.writes, [])
        self.conn.row['status'] = 'deleted'
        self.assertEqual(self.call()[1]['status'], 'deleted_rejected')
        self.conn.row['status'] = 'active'
        self.conn.row['tombstoned'] = True
        self.assertEqual(self.call()[1]['status'], 'deleted_rejected')
        self.conn.row['tombstoned'] = False
        self.conn.row['subtitle'] = '别的译文'
        self.assertEqual(self.call()[1]['status'], 'translation_conflict')
        self.assertEqual(self.conn.writes, [])

    def test_not_synced_or_unfinished_receipt_never_creates_event(self):
        self.conn.status = 'processing'
        self.assertEqual(self.call()[1]['status'], 'receipt_unavailable')
        self.conn.status = 'completed'
        self.conn.row['id'] = 8
        # An event that has not reached chat_log cannot be repaired through a
        # receipt alone; that would turn a deleted or unsynced bubble visible.
        with patch.object(self.conn, 'cursor', return_value=MissingRowCursor(self.conn)):
            self.assertEqual(self.call()[1]['status'], 'message_not_synced')
        self.assertEqual(self.conn.writes, [])


class MissingRowCursor(FakeCursor):
    def execute(self, sql, params):
        super().execute(sql, params)
        if 'FROM chat_log WHERE user_id=' in ' '.join(sql.split()):
            self.one = None
