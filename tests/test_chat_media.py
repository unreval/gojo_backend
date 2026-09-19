import ast
import asyncio
import base64
import hashlib
import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from uuid import UUID


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
FRONTEND_CHAT = os.path.join(ROOT, 'app', 'chat', '[id].tsx')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import db_chat_media  # noqa: E402
import db_chatlog  # noqa: E402
import media_storage  # noqa: E402
import route_chatlog  # noqa: E402
import route_image  # noqa: E402


class _PassthroughHeartbeat:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def stop(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


PNG_B64 = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ'
    '/pLvAAAAAElFTkSuQmCC'
)
PNG_BYTES = base64.b64decode(PNG_B64)


def _record_tuple(row):
    return (
        row['id'], row['user_id'], row['chat_id'], row['source_event_id'],
        row['media_kind'], row['object_key'], row['mime_type'],
        row['size_bytes'], row['sha256'], row['created_at'], row['deleted_at'],
    )


class ChatMediaStore:
    def __init__(self):
        self.rows = []
        self.sql = []
        self.chat_log = []
        self.tombstones = set()
        self.next_log_id = 1


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._one = None
        self._many = []

    def close(self):
        pass

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        params = tuple(params or ())
        self.store.sql.append((compact, params))
        self.rowcount = 0
        self._one = None
        self._many = []

        if compact.startswith('CREATE TABLE') or compact.startswith('CREATE UNIQUE INDEX') \
                or compact.startswith('CREATE INDEX') or compact.startswith('ALTER TABLE'):
            return
        if 'information_schema.columns' in compact:
            self._one = ('timestamp with time zone',)
            return

        if compact.startswith('SELECT id, user_id, chat_id, source_event_id'):
            self._select_media(compact, params)
            return
        if compact.startswith('INSERT INTO chat_media'):
            (media_id, user_id, chat_id, source_event_id, media_kind,
             object_key, mime_type, size_bytes, digest) = params
            for row in self.store.rows:
                if (row['user_id'] == user_id and row['chat_id'] == chat_id
                        and row['source_event_id'] == source_event_id
                        and row['media_kind'] == media_kind):
                    raise RuntimeError('duplicate chat_media')
            row = {
                'id': media_id,
                'user_id': user_id,
                'chat_id': chat_id,
                'source_event_id': source_event_id,
                'media_kind': media_kind,
                'object_key': object_key,
                'mime_type': mime_type,
                'size_bytes': size_bytes,
                'sha256': digest,
                'created_at': datetime.now(timezone.utc),
                'deleted_at': None,
            }
            self.store.rows.append(row)
            self._one = _record_tuple(row)
            self._many = [self._one]
            self.rowcount = 1
            return
        if compact.startswith('UPDATE chat_media') and 'SET deleted_at=NULL' in compact:
            object_key, mime_type, size_bytes, digest, media_id = params
            for row in self.store.rows:
                if str(row['id']) == str(media_id):
                    row['deleted_at'] = None
                    row['object_key'] = object_key
                    row['mime_type'] = mime_type
                    row['size_bytes'] = size_bytes
                    row['sha256'] = digest
                    self._one = _record_tuple(row)
                    self._many = [self._one]
                    self.rowcount = 1
                    return
            return
        if compact.startswith('UPDATE chat_media') and 'SET deleted_at=CURRENT_TIMESTAMP' in compact:
            matched = []
            for row in self.store.rows:
                if row['deleted_at'] is not None:
                    continue
                if row['user_id'] != params[0] or row['chat_id'] != params[1]:
                    continue
                if 'AND source_event_id=%s' in compact:
                    if row['source_event_id'] != params[2]:
                        continue
                    if 'AND media_kind=%s' in compact and row['media_kind'] != params[3]:
                        continue
                row['deleted_at'] = datetime.now(timezone.utc)
                matched.append(_record_tuple(row))
            self._many = matched
            self.rowcount = len(matched)
            return

        if compact.startswith('SELECT 1 FROM chat_log_tombstone'):
            self._one = None
            return
        if compact.startswith('INSERT INTO chat_log_tombstone'):
            return
        if compact.startswith('INSERT INTO chat_log'):
            user_id, chat_id, client_msg_id, role, text, subtitle, emotion, kind, extra, has_audio = params[:10]
            event_id = client_msg_id
            if len(params) >= 11:
                event_id = params[10] or client_msg_id
            row = {
                'id': self.store.next_log_id,
                'user_id': user_id,
                'chat_id': chat_id,
                'client_msg_id': client_msg_id,
                'event_id': event_id,
                'role': role,
                'text': text,
                'subtitle': subtitle,
                'emotion': emotion,
                'kind': kind,
                'extra': extra,
                'has_audio': bool(has_audio),
                'created_at': datetime.now(timezone.utc),
                'status': 'active',
            }
            self.store.next_log_id += 1
            self.store.chat_log.append(row)
            self.rowcount = 1
            return
        if compact.startswith('SELECT event_id, client_msg_id, role, extra'):
            user_id, chat_id = params
            matched = [
                row for row in self.store.chat_log
                if row['user_id'] == user_id and row['chat_id'] == chat_id
                and row.get('role') in ('gojo', 'assistant')
            ]
            self._many = [
                (row.get('event_id') or '', row.get('client_msg_id') or '',
                 row['role'], row.get('extra') or '',
                 row.get('status') or 'active')
                for row in matched
            ]
            return
        if compact.startswith('SELECT id, client_msg_id'):
            user_id, chat_id = params[0], params[1]
            matched = [
                row for row in self.store.chat_log
                if row['user_id'] == user_id and row['chat_id'] == chat_id
                and row.get('status', 'active') == 'active'
            ]
            matched.sort(key=lambda r: r['id'], reverse=True)
            limit = params[-1]
            matched = matched[:limit]
            self._many = [
                (r['id'], r['client_msg_id'], r['role'], r['text'],
                 r['subtitle'], r['emotion'], r['kind'], r['extra'],
                 r['has_audio'], r['created_at'], r.get('event_id') or '')
                for r in matched
            ]
            return
        if compact.startswith('SELECT client_msg_id, event_id'):
            if 'WHERE id=%s' in compact:
                server_id, user_id, chat_id = params
                for row in self.store.chat_log:
                    if row['id'] == server_id and row['user_id'] == user_id and row['chat_id'] == chat_id:
                        self._one = (row.get('client_msg_id') or '', row.get('event_id') or '')
                        return
                return
            user_id, chat_id, client_msg_id = params
            for row in reversed(self.store.chat_log):
                if (row['user_id'] == user_id and row['chat_id'] == chat_id
                        and row['client_msg_id'] == client_msg_id):
                    self._one = (row.get('client_msg_id') or '', row.get('event_id') or '')
                    return
            return
        if compact.startswith('UPDATE chat_log') and "status='deleted'" in compact.replace(' ', ''):
            n = 0
            if 'AND client_msg_id=%s' in compact:
                user_id, chat_id, client_msg_id = params
                for row in self.store.chat_log:
                    if (row['user_id'] == user_id and row['chat_id'] == chat_id
                            and row['client_msg_id'] == client_msg_id
                            and row.get('status', 'active') == 'active'):
                        row['status'] = 'deleted'
                        n += 1
            else:
                user_id, chat_id = params[:2]
                for row in self.store.chat_log:
                    if (row['user_id'] == user_id and row['chat_id'] == chat_id
                            and row.get('status', 'active') == 'active'):
                        row['status'] = 'deleted'
                        n += 1
            self.rowcount = n
            return
        raise AssertionError(f'unhandled SQL: {compact}')

    def _select_media(self, compact, params):
        user_id = params[0]
        chat_id = params[1]
        matched = [
            row for row in self.store.rows
            if row['user_id'] == user_id and row['chat_id'] == chat_id
        ]
        if 'AND source_event_id=%s' in compact:
            source_event_id = params[2]
            media_kind = params[3]
            matched = [
                row for row in matched
                if row['source_event_id'] == source_event_id
                and row['media_kind'] == media_kind
            ]
        elif 'AND source_event_id IN' in compact:
            media_kind = params[2]
            ids = params[3:]
            matched = [
                row for row in matched
                if row['media_kind'] == media_kind
                and row['source_event_id'] in ids
            ]
        elif 'AND media_kind=%s' in compact:
            media_kind = params[2]
            matched = [row for row in matched if row['media_kind'] == media_kind]
        if 'AND deleted_at IS NULL' in compact:
            matched = [row for row in matched if row['deleted_at'] is None]
        tuples = [_record_tuple(row) for row in matched]
        self._many = tuples
        self._one = tuples[0] if tuples else None


class FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class ChatMediaPersistTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatMediaStore()
        self.put = Mock()
        self.patchers = [
            patch.object(db_chat_media, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(db_chatlog, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(media_storage, 'is_configured', return_value=True),
            patch.object(media_storage, 'put_bytes', self.put),
            patch.object(media_storage, 'signed_get_url', return_value='https://r2.example/v1'),
            patch.object(media_storage, 'delete_object', Mock()),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)

    def persist(self, source_event_id='evt-1', user_id='u1', chat_id='gojo',
                data=PNG_BYTES, mime='image/png'):
        return db_chat_media.persist_image(
            user_id, chat_id, source_event_id, data, mime_type=mime)

    def test_persist_twice_is_idempotent(self):
        first = self.persist()
        second = self.persist()
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(first['id'], second['id'])
        self.assertEqual(self.put.call_count, 1)

    def test_object_key_is_stable(self):
        first = self.persist('photo-9')
        second = self.persist('photo-9')
        expected = db_chat_media.object_key_for('u1', 'gojo', 'photo-9', 'image/png')
        self.assertEqual(first['object_key'], expected)
        self.assertEqual(second['object_key'], expected)
        self.assertTrue(expected.endswith('/original.png'))
        UUID(first['id'])

    def test_sha256_is_of_original_bytes(self):
        rec = self.persist()
        self.assertEqual(rec['sha256'], hashlib.sha256(PNG_BYTES).hexdigest())
        self.assertEqual(rec['size_bytes'], len(PNG_BYTES))

    def test_isolation_by_user_chat_and_event(self):
        self.persist('evt-1', user_id='u1', chat_id='gojo')
        self.persist('evt-1', user_id='u2', chat_id='gojo')
        self.persist('evt-1', user_id='u1', chat_id='geto')
        self.persist('evt-2', user_id='u1', chat_id='gojo')
        own = db_chat_media.get_media_by_source_events(
            'u1', 'gojo', ['evt-1', 'evt-2'])
        self.assertEqual(set(own), {'evt-1', 'evt-2'})
        other_user = db_chat_media.get_media_for_source_event('u2', 'gojo', 'evt-1')
        self.assertEqual(other_user['user_id'], 'u2')
        missing = db_chat_media.get_media_for_source_event('u1', 'gojo', 'nope')
        self.assertIsNone(missing)

    def test_signed_url_is_not_stored(self):
        rec = self.persist()
        stored = self.store.rows[0]
        blob = json.dumps(stored, default=str)
        self.assertNotIn('https://r2.example', blob)
        self.assertNotIn('url', stored)
        view = db_chat_media.public_media(rec)
        self.assertEqual(view['url'], 'https://r2.example/v1')
        self.assertEqual(stored['object_key'], rec['object_key'])

    def test_init_creates_expires_free_schema(self):
        db_chat_media.init_chat_media_table()
        sql = '\n'.join(item[0] for item in self.store.sql)
        self.assertIn('CREATE TABLE IF NOT EXISTS chat_media', sql)
        self.assertIn('object_key TEXT NOT NULL', sql)
        self.assertNotIn('signed_url', sql.lower())
        self.assertNotIn('public_url', sql.lower())


class ChatImagePersistRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatMediaStore()
        self.put = Mock()
        self.llm = Mock(side_effect=RuntimeError('llm down'))
        route_image.claude_client = types.SimpleNamespace(messages=types.SimpleNamespace(create=self.llm))
        self.patchers = [
            patch.object(db_chat_media, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(route_image, 'persist_image', wraps=db_chat_media.persist_image),
            patch.object(media_storage, 'is_configured', return_value=True),
            patch.object(media_storage, 'put_bytes', self.put),
            patch.object(media_storage, 'signed_get_url', return_value='https://r2.example/v1'),
            patch.object(route_image, 'get_character', return_value={'voice_id': None}),
            patch.object(route_image, 'get_temporal_snapshot', return_value={}),
            patch.object(route_image, 'update_chat_days', return_value=1),
            patch.object(route_image, 'save_user_short_memory_once', Mock()),
            patch.object(route_image, 'save_short_memory', Mock()),
            patch.object(route_image, 'build_system_blocks', return_value=[]),
            patch.object(route_image, 'log_cache_usage', Mock()),
            patch.object(route_image, 'enqueue_private_extraction', Mock()),
            patch.object(route_image, '_turn_context', return_value=(None, [])),
            patch('reply_availability.check_reply_availability', return_value={'can_reply': True}),
            patch('context_layer.append_current_user_turn', side_effect=lambda msgs, content: list(msgs or []) + [{'role': 'user', 'content': content}]),
            patch('db_generation_receipt.resolve_generation', side_effect=lambda user_id, character_id, source_event_id, endpoint, **k: {
                'action': 'generate',
                'claim_token': 'passthrough',
                'source_event_id': source_event_id,
            }),
            patch('db_generation_receipt.fail_generation', return_value=True),
            patch('db_generation_receipt.complete_generation', return_value=True),
            patch('db_generation_receipt.ensure_completed_generation_effects', return_value=[]),
            patch('db_generation_receipt.after_generation_commit', return_value=[]),
            patch(
                'db_generation_receipt.hydrate_completed_generation_response',
                side_effect=lambda *_args, payload=None, **_kwargs: {
                    key: val for key, val in dict(payload or {}).items()
                    if not str(key).startswith('_')
                },
            ),
            patch('db_generation_receipt.release_generation', return_value=True),
            patch('db_generation_receipt.renew_generation_lease', return_value=True),
            patch('db_generation_receipt.GenerationHeartbeat', new=_PassthroughHeartbeat),
            patch('builtins.print'),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)

    def test_llm_failure_after_put_keeps_chat_media(self):
        response = asyncio.run(route_image.chat_image({
            'user_id': 'u1',
            'character_id': 'gojo',
            'image_base64': PNG_B64,
            'source_event_id': 'img-keep-1',
            'text': '看',
        }))
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body['error'], 'generation_failed')
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.put.call_count, 1)
        self.assertEqual(body['media']['id'], self.store.rows[0]['id'])
        self.assertEqual(body['media']['url'], 'https://r2.example/v1')

    def test_put_failure_does_not_pretend_success(self):
        self.put.side_effect = RuntimeError('r2 down')
        response = asyncio.run(route_image.chat_image({
            'user_id': 'u1',
            'character_id': 'gojo',
            'image_base64': PNG_B64,
            'source_event_id': 'img-fail-1',
        }))
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(body['error'], 'media_persist_failed')
        self.assertTrue(body['retryable'])
        self.assertEqual(self.store.rows, [])
        self.llm.assert_not_called()

    def test_video_frames_are_not_persisted_as_original(self):
        response = asyncio.run(route_image.chat_image({
            'user_id': 'u1',
            'character_id': 'gojo',
            'images': [{'data': PNG_B64}, {'data': PNG_B64}],
            'is_video': True,
            'source_event_id': 'vid-1',
        }))
        body = json.loads(response.body)
        self.assertNotEqual(body.get('error'), 'media_persist_failed')
        self.assertIsNone(body.get('media'))
        self.assertEqual(self.store.rows, [])
        self.put.assert_not_called()


class ChatlogMediaHydrationTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatMediaStore()
        urls = iter(['https://r2.example/a', 'https://r2.example/b'])
        self.patchers = [
            patch.object(db_chat_media, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(db_chatlog, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(media_storage, 'is_configured', return_value=True),
            patch.object(media_storage, 'put_bytes', Mock()),
            patch.object(media_storage, 'signed_get_url', side_effect=lambda *a, **k: next(urls)),
            patch.object(media_storage, 'delete_object', Mock()),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)
        extra = json.dumps({'source_event_id': 'img-1', 'visual_summary': '红点'})
        db_chatlog.append_messages('u1', 'gojo', [{
            'client_msg_id': 'img-1',
            'role': 'user',
            'text': '📷 [图片]',
            'kind': 'image',
            'extra': extra,
        }])
        db_chat_media.persist_image('u1', 'gojo', 'img-1', PNG_BYTES, 'image/png')

    def test_get_chatlog_returns_fresh_signed_media_url(self):
        msgs, _ = db_chatlog.get_messages('u1', 'gojo')
        image = [m for m in msgs if m['kind'] == 'image'][0]
        self.assertEqual(image['media']['url'], 'https://r2.example/a')
        self.assertEqual(image['media']['kind'], 'image')
        self.assertNotIn('https://r2.example', image['extra'])
        msgs2, _ = db_chatlog.get_messages('u1', 'gojo')
        self.assertEqual(msgs2[0]['media']['url'], 'https://r2.example/b')
        stored = json.dumps(self.store.rows[0], default=str)
        self.assertNotIn('https://r2.example', stored)

    def test_legacy_image_uri_does_not_crash_history(self):
        db_chatlog.append_messages('u1', 'gojo', [{
            'client_msg_id': 'old-file',
            'role': 'user',
            'text': '📷 [图片]',
            'kind': 'image',
            'extra': json.dumps({'imageUri': 'file:///data/cache/old.jpg'}),
        }])
        msgs, _ = db_chatlog.get_messages('u1', 'gojo')
        legacy = [m for m in msgs if m['client_msg_id'] == 'old-file'][0]
        extra = json.loads(legacy['extra'])
        self.assertTrue(extra['imageUri'].startswith('file://'))
        self.assertIsNone(legacy.get('media'))


class ChatMediaDeleteTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatMediaStore()
        self.delete_object = Mock()
        self.patchers = [
            patch.object(db_chat_media, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(db_chatlog, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(media_storage, 'is_configured', return_value=True),
            patch.object(media_storage, 'put_bytes', Mock()),
            patch.object(media_storage, 'signed_get_url', return_value='https://r2.example/x'),
            patch.object(media_storage, 'delete_object', self.delete_object),
            patch('builtins.print'),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)
        db_chatlog.append_messages('u1', 'gojo', [{
            'client_msg_id': 'img-1', 'role': 'user', 'text': '📷', 'kind': 'image',
        }])
        db_chatlog.append_messages('u1', 'geto', [{
            'client_msg_id': 'img-other', 'role': 'user', 'text': '📷', 'kind': 'image',
        }])
        db_chat_media.persist_image('u1', 'gojo', 'img-1', PNG_BYTES, 'image/png')
        db_chat_media.persist_image('u1', 'geto', 'img-other', PNG_BYTES, 'image/png')

    def test_delete_message_soft_deletes_media_and_object(self):
        response = asyncio.run(route_chatlog.delete_chatlog_message(
            'u1', 'gojo', client_msg_id='img-1'))
        body = json.loads(response.body)
        self.assertTrue(body['ok'])
        gojo = db_chat_media.get_media_for_source_event('u1', 'gojo', 'img-1')
        self.assertIsNone(gojo)
        self.assertIsNotNone(self.store.rows[0]['deleted_at'])
        self.delete_object.assert_called()
        other = db_chat_media.get_media_for_source_event('u1', 'geto', 'img-other')
        self.assertIsNotNone(other)

    def test_clear_chat_only_removes_that_chat(self):
        response = asyncio.run(route_chatlog.clear_chatlog('u1', 'gojo'))
        body = json.loads(response.body)
        self.assertTrue(body['ok'])
        self.assertIsNone(db_chat_media.get_media_for_source_event('u1', 'gojo', 'img-1'))
        other = db_chat_media.get_media_for_source_event('u1', 'geto', 'img-other')
        self.assertIsNotNone(other)
        deleted_keys = [call.args[0] for call in self.delete_object.call_args_list]
        self.assertTrue(any('gojo' in key for key in deleted_keys))
        self.assertFalse(any('/geto/' in key for key in deleted_keys))

    def test_r2_delete_failure_still_deletes_chat_log(self):
        self.delete_object.side_effect = RuntimeError('network')
        response = asyncio.run(route_chatlog.delete_chatlog_message(
            'u1', 'gojo', client_msg_id='img-1'))
        body = json.loads(response.body)
        self.assertTrue(body['ok'])
        self.assertTrue(body['deleted'] >= 1)


class ChatMediaBackfillTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatMediaStore()
        self.put = Mock()
        self.patchers = [
            patch.object(db_chat_media, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(media_storage, 'is_configured', return_value=True),
            patch.object(media_storage, 'put_bytes', self.put),
            patch.object(media_storage, 'signed_get_url', return_value='https://r2.example/backfill'),
            patch.object(route_image, 'claude_client', Mock()),
            patch('builtins.print'),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)

    def test_backfill_does_not_call_llm_or_memory(self):
        src = Path(BACKEND, 'route_image.py').read_text(encoding='utf-8')
        tree = ast.parse(src)
        fn = [node for node in tree.body if getattr(node, 'name', '') == 'chat_media_backfill'][0]
        dump = ast.dump(fn)
        self.assertNotIn('save_short_memory', dump)
        self.assertNotIn('extract_and_save_memory', dump)
        self.assertNotIn('enqueue_private_extraction', dump)
        self.assertNotIn('check_reply_availability', dump)
        self.assertNotIn('process_turn', dump)
        self.assertIn('_persist_chat_image', dump)

    def test_backfill_is_idempotent(self):
        payload = {
            'user_id': 'u1',
            'chat_id': 'gojo',
            'source_event_id': 'legacy-1',
            'image_base64': PNG_B64,
            'media_type': 'image/png',
        }
        first = json.loads(asyncio.run(route_image.chat_media_backfill(payload)).body)
        second = json.loads(asyncio.run(route_image.chat_media_backfill(payload)).body)
        self.assertTrue(first['ok'])
        self.assertEqual(first['media']['id'], second['media']['id'])
        self.assertEqual(len(self.store.rows), 1)
        self.assertEqual(self.put.call_count, 1)
        route_image.claude_client.messages.create.assert_not_called()


class MediaStorageConfigTests(unittest.TestCase):
    def test_is_configured_requires_all_secrets(self):
        env = {
            'R2_ENDPOINT': 'https://example.r2.cloudflarestorage.com',
            'R2_ACCESS_KEY_ID': 'id',
            'R2_SECRET_ACCESS_KEY': 'secret',
            'R2_BUCKET': 'gojo-chat-media',
        }
        with patch.dict(os.environ, env, clear=False):
            self.assertTrue(media_storage.is_configured())
        missing = dict(env)
        missing['R2_BUCKET'] = ''
        with patch.dict(os.environ, missing, clear=False):
            self.assertFalse(media_storage.is_configured())

    def test_health_exposes_media_storage_ready(self):
        src = Path(BACKEND, 'gojo_server.py').read_text(encoding='utf-8')
        self.assertIn('media_storage_ready', src)
        self.assertIn('init_chat_media_table()', src)


class FrontendChatMediaGuardTests(unittest.TestCase):
    def setUp(self):
        self.src = Path(FRONTEND_CHAT).read_text(encoding='utf-8')

    def test_send_image_still_shows_local_uri_immediately(self):
        self.assertIn('imageUri: localUri', self.src)
        self.assertIn('messageImageSrc(msg)', self.src)
        self.assertIn('msg?.mediaUrl || msg?.imageUri', self.src)

    def test_server_media_url_outranks_legacy_image_uri(self):
        self.assertIn('mediaUrl: media?.url', self.src)
        self.assertIn('applyMediaFields', self.src)

    def test_to_server_msg_does_not_write_local_picker_uri(self):
        start = self.src.index('function toServerMsg')
        end = self.src.index('function isEphemeralUiMessage')
        body = self.src[start:end]
        self.assertIn('isDurableHttpUri(m.imageUri)', body)
        self.assertIn('isLocalMediaUri', body)
        self.assertNotIn('if (m.imageUri) extra.imageUri = m.imageUri;', body)
        self.assertNotIn('extra.mediaUrl', body)
        self.assertIn('extra.media_id = m.mediaId', body)

    def test_legacy_file_uri_still_falls_back(self):
        self.assertIn('imageUri: extra.imageUri', self.src)

    def test_legacy_backfill_switches_to_media_url(self):
        self.assertIn('/chat/media/backfill', self.src)
        self.assertIn('[chat-media] recovered legacy image source_event_id=', self.src)
        self.assertIn('applyMediaFields(item, res.data.media)', self.src)

    def test_missing_local_file_is_skipped_safely(self):
        self.assertIn('[chat-media] legacy local file missing source_event_id=', self.src)
        self.assertIn('FileSystem.getInfoAsync', self.src)
        self.assertIn('if (!info.exists)', self.src)


if __name__ == '__main__':
    unittest.main()
