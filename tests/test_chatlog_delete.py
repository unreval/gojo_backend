import asyncio
import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
FRONTEND_CHAT = os.path.join(ROOT, 'app', 'chat', '[id].tsx')
MEMORY_PAGE = os.path.join(ROOT, 'app', '(tabs)', 'memory.tsx')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

fake_db = types.ModuleType('db')
fake_db.get_conn = lambda: (_ for _ in ()).throw(
    AssertionError('database should be patched by tests that need it')
)
sys.modules.setdefault('db', fake_db)

fake_fastapi = types.ModuleType('fastapi')
fake_router = types.SimpleNamespace(
    get=lambda *_args, **_kwargs: (lambda fn: fn),
    post=lambda *_args, **_kwargs: (lambda fn: fn),
    delete=lambda *_args, **_kwargs: (lambda fn: fn),
)
fake_fastapi.APIRouter = lambda: fake_router
fake_responses = types.ModuleType('fastapi.responses')
fake_responses.JSONResponse = lambda content, status_code=200: types.SimpleNamespace(
    body=json.dumps(content, ensure_ascii=False).encode(),
    status_code=status_code,
)
sys.modules.setdefault('fastapi', fake_fastapi)
sys.modules.setdefault('fastapi.responses', fake_responses)

import db_chatlog  # noqa: E402
import route_chatlog  # noqa: E402


FORBIDDEN_SQL = (
    'short_memory',
    'long_memory',
    'bond_memory',
    'character_memory',
    'relationship',
    'provenance',
    'cognitive',
)


class ChatlogStore:
    def __init__(self):
        self.next_id = 1
        self.rows = []
        self.tombstones = set()
        self.sql = []

    def has_tombstone(self, user_id, chat_id, client_msg_id):
        return (user_id, chat_id, client_msg_id) in self.tombstones

    def insert_tombstone(self, user_id, chat_id, client_msg_id):
        key = (user_id, chat_id, client_msg_id)
        if key in self.tombstones:
            return 0
        self.tombstones.add(key)
        return 1

    def insert_row(self, user_id, chat_id, client_msg_id, role, text,
                   subtitle, emotion, kind, extra, has_audio, created_at=None):
        if client_msg_id:
            for row in self.rows:
                if (row['user_id'] == user_id
                        and row['chat_id'] == chat_id
                        and row['client_msg_id'] == client_msg_id):
                    return 0
        row = {
            'id': self.next_id,
            'user_id': user_id,
            'chat_id': chat_id,
            'client_msg_id': client_msg_id,
            'role': role,
            'text': text,
            'subtitle': subtitle,
            'emotion': emotion,
            'kind': kind,
            'extra': extra,
            'has_audio': bool(has_audio),
            'created_at': created_at or datetime.now(timezone.utc),
        }
        self.next_id += 1
        self.rows.append(row)
        return 1


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._one = None
        self._many = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        params = tuple(params or ())
        self.store.sql.append((compact, params))
        lower = compact.lower()
        for name in FORBIDDEN_SQL:
            if name in lower:
                raise AssertionError(f'chat_log 删除路径碰到了记忆表: {compact}')
        self.rowcount = 0
        self._one = None
        self._many = []

        if compact.startswith('CREATE TABLE') or compact.startswith('CREATE INDEX') \
                or compact.startswith('CREATE UNIQUE INDEX'):
            return
        if 'information_schema.columns' in compact:
            self._one = ('timestamp with time zone',)
            return
        if compact.startswith('SELECT 1 FROM chat_log_tombstone'):
            user_id, chat_id, client_msg_id = params
            if self.store.has_tombstone(user_id, chat_id, client_msg_id):
                self._one = (1,)
            return
        if compact.startswith('INSERT INTO chat_log_tombstone'):
            user_id, chat_id, client_msg_id = params
            self.rowcount = self.store.insert_tombstone(
                user_id, chat_id, client_msg_id)
            return
        if compact.startswith('INSERT INTO chat_log'):
            user_id, chat_id, client_msg_id, role, text, subtitle, emotion, kind, extra, has_audio = params[:10]
            created_at = datetime.now(timezone.utc)
            self.rowcount = self.store.insert_row(
                user_id, chat_id, client_msg_id, role, text, subtitle,
                emotion, kind, extra, has_audio, created_at)
            return
        if compact.startswith('SELECT client_msg_id FROM chat_log'):
            server_id, user_id, chat_id = params
            for row in self.store.rows:
                if (row['id'] == server_id
                        and row['user_id'] == user_id
                        and row['chat_id'] == chat_id):
                    self._one = (row['client_msg_id'],)
                    return
            return
        if compact.startswith('SELECT id, client_msg_id'):
            if 'AND id <' in compact:
                user_id, chat_id, before_id, limit = params
                matched = [
                    row for row in self.store.rows
                    if row['user_id'] == user_id
                    and row['chat_id'] == chat_id
                    and row['id'] < before_id
                ]
            else:
                user_id, chat_id, limit = params
                matched = [
                    row for row in self.store.rows
                    if row['user_id'] == user_id and row['chat_id'] == chat_id
                ]
            matched.sort(key=lambda r: r['id'], reverse=True)
            matched = matched[:limit]
            self._many = [
                (r['id'], r['client_msg_id'], r['role'], r['text'],
                 r['subtitle'], r['emotion'], r['kind'], r['extra'],
                 r['has_audio'], r['created_at'])
                for r in matched
            ]
            return
        if compact.startswith('SELECT role, text, subtitle, kind, extra'):
            user_id, chat_id, limit = params
            matched = [
                row for row in self.store.rows
                if row['user_id'] == user_id and row['chat_id'] == chat_id
            ]
            matched.sort(key=lambda r: r['id'], reverse=True)
            matched = matched[:limit]
            self._many = [
                (r['role'], r['text'], r['subtitle'], r['kind'], r['extra'])
                for r in matched
            ]
            return
        if compact.startswith('DELETE FROM chat_log WHERE id='):
            server_id, user_id, chat_id = params
            before = len(self.store.rows)
            self.store.rows = [
                row for row in self.store.rows
                if not (row['id'] == server_id
                        and row['user_id'] == user_id
                        and row['chat_id'] == chat_id)
            ]
            self.rowcount = before - len(self.store.rows)
            return
        if compact.startswith('DELETE FROM chat_log WHERE user_id=%s AND chat_id=%s AND client_msg_id=%s'):
            user_id, chat_id, client_msg_id = params
            before = len(self.store.rows)
            self.store.rows = [
                row for row in self.store.rows
                if not (row['user_id'] == user_id
                        and row['chat_id'] == chat_id
                        and row['client_msg_id'] == client_msg_id)
            ]
            self.rowcount = before - len(self.store.rows)
            return
        if compact.startswith('DELETE FROM chat_log WHERE user_id=%s AND chat_id=%s'):
            user_id, chat_id = params
            before = len(self.store.rows)
            self.store.rows = [
                row for row in self.store.rows
                if not (row['user_id'] == user_id and row['chat_id'] == chat_id)
            ]
            self.rowcount = before - len(self.store.rows)
            return
        if compact.startswith('SELECT COUNT(*) FROM chat_log'):
            user_id, chat_id = params
            n = sum(
                1 for row in self.store.rows
                if row['user_id'] == user_id and row['chat_id'] == chat_id
            )
            self._one = (n,)
            return
        raise AssertionError(f'unhandled SQL in chatlog fake: {compact}')

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)

    def close(self):
        pass


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.commits = 0
        self.rollbacks = 0
        self.closed = False
        self._cursor = FakeCursor(store)

    def cursor(self):
        self._cursor = FakeCursor(self.store)
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def _msg(client_msg_id, text='hello', role='user'):
    return {
        'client_msg_id': client_msg_id,
        'role': role,
        'text': text,
        'subtitle': '',
        'emotion': '',
        'kind': 'text',
        'extra': '',
        'has_audio': False,
    }


class ChatlogDeleteTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatlogStore()
        self.patcher = patch(
            'db_chatlog.get_conn',
            side_effect=lambda: FakeConn(self.store),
        )
        self.patcher.start()
        db_chatlog.init_chatlog_table()

    def tearDown(self):
        self.patcher.stop()

    def ids(self, user_id, chat_id):
        msgs, _ = db_chatlog.get_messages(user_id, chat_id)
        return [m['client_msg_id'] for m in msgs]

    def test_init_creates_tombstone_table(self):
        creates = [sql for sql, _ in self.store.sql if 'CREATE TABLE' in sql]
        self.assertTrue(any('chat_log_tombstone' in sql for sql in creates))
        self.assertTrue(any(
            'PRIMARY KEY (user_id, chat_id, client_msg_id)' in sql
            for sql in creates
        ))

    def test_delete_by_client_msg_id_removes_row(self):
        written = db_chatlog.append_messages(
            'u1', 'gojo', [_msg('delete_test_1', '要删的')])
        self.assertEqual(written, 1)
        self.assertIn('delete_test_1', self.ids('u1', 'gojo'))

        deleted = db_chatlog.delete_message(
            'u1', 'gojo', client_msg_id='delete_test_1')
        self.assertEqual(deleted, 1)
        self.assertNotIn('delete_test_1', self.ids('u1', 'gojo'))

    def test_tombstone_blocks_reappend_of_same_client_msg_id(self):
        db_chatlog.append_messages('u1', 'gojo', [_msg('delete_test_1')])
        db_chatlog.delete_message('u1', 'gojo', client_msg_id='delete_test_1')

        written = db_chatlog.append_messages(
            'u1', 'gojo', [_msg('delete_test_1', '复活?')])
        self.assertEqual(written, 0)
        self.assertEqual(self.ids('u1', 'gojo'), [])
        self.assertTrue(
            self.store.has_tombstone('u1', 'gojo', 'delete_test_1'))

    def test_delete_by_server_id_when_client_msg_id_empty(self):
        written = db_chatlog.append_messages(
            'u1', 'gojo', [_msg('', '旧记录')])
        self.assertEqual(written, 1)
        msgs, _ = db_chatlog.get_messages('u1', 'gojo')
        self.assertEqual(len(msgs), 1)
        server_id = msgs[0]['id']
        self.assertEqual(msgs[0]['client_msg_id'], '')

        deleted = db_chatlog.delete_message(
            'u1', 'gojo', client_msg_id='srv_%s' % server_id, server_id=server_id)
        self.assertEqual(deleted, 1)
        self.assertEqual(self.ids('u1', 'gojo'), [])
        self.assertFalse(
            self.store.has_tombstone('u1', 'gojo', 'srv_%s' % server_id))

    def test_same_client_msg_id_in_other_user_or_chat_untouched(self):
        db_chatlog.append_messages('u1', 'gojo', [_msg('delete_test_1', 'A')])
        db_chatlog.append_messages('u2', 'gojo', [_msg('delete_test_1', 'B')])
        db_chatlog.append_messages('u1', 'geto', [_msg('delete_test_1', 'C')])

        deleted = db_chatlog.delete_message(
            'u1', 'gojo', client_msg_id='delete_test_1')
        self.assertEqual(deleted, 1)

        self.assertEqual(self.ids('u1', 'gojo'), [])
        self.assertEqual(self.ids('u2', 'gojo'), ['delete_test_1'])
        self.assertEqual(self.ids('u1', 'geto'), ['delete_test_1'])

        written_other_user = db_chatlog.append_messages(
            'u2', 'gojo', [_msg('delete_test_1', 'B2')])
        written_other_chat = db_chatlog.append_messages(
            'u1', 'geto', [_msg('delete_test_1', 'C2')])
        self.assertEqual(written_other_user, 0)  # 仍受原幂等约束,但还在
        self.assertEqual(written_other_chat, 0)
        self.assertEqual(self.ids('u2', 'gojo'), ['delete_test_1'])
        self.assertEqual(self.ids('u1', 'geto'), ['delete_test_1'])

        written_blocked = db_chatlog.append_messages(
            'u1', 'gojo', [_msg('delete_test_1', 'A2')])
        self.assertEqual(written_blocked, 0)

    def test_delete_does_not_touch_memory_tables(self):
        db_chatlog.append_messages('u1', 'gojo', [_msg('delete_test_1')])
        self.store.sql.clear()
        db_chatlog.delete_message('u1', 'gojo', client_msg_id='delete_test_1')
        joined = ' '.join(sql for sql, _ in self.store.sql).lower()
        for name in FORBIDDEN_SQL:
            self.assertNotIn(name, joined)

    def test_synthetic_srv_id_is_not_written_to_tombstone(self):
        db_chatlog.append_messages('u1', 'gojo', [_msg('real_id_9', 'x')])
        msgs, _ = db_chatlog.get_messages('u1', 'gojo')
        server_id = msgs[0]['id']
        db_chatlog.delete_message(
            'u1', 'gojo', client_msg_id='srv_%s' % server_id, server_id=server_id)
        self.assertFalse(self.store.has_tombstone('u1', 'gojo', 'srv_%s' % server_id))
        self.assertTrue(self.store.has_tombstone('u1', 'gojo', 'real_id_9'))

    def test_prompt_history_recovers_image_summary_and_reply_linkage(self):
        image_extra = json.dumps({
            'visual_summary': '照片里是一只蓝色马克杯，杯沿有裂纹。',
            'event_meta': {'kind': 'image', 'source_event_id': 'img-1'},
            'reply_to': {'name': '五条悟', 'text': '刚才那个杯子别乱放。'},
        }, ensure_ascii=False)
        reply_extra = json.dumps({
            'reply_to_source_event_id': 'img-1',
            'source_event_id': 'img-1:reply:0',
        }, ensure_ascii=False)
        db_chatlog.append_messages('u1', 'gojo', [
            {**_msg('img-1', '看这个杯子', 'user'), 'kind': 'image',
             'extra': image_extra},
            {**_msg('img-1-r', '割れてるな。', 'gojo'), 'subtitle': '裂了啊。',
             'extra': reply_extra},
        ])

        history = db_chatlog.get_prompt_history('u1', 'gojo', limit=10)
        self.assertEqual([m['role'] for m in history], ['user', 'assistant'])
        self.assertIn('【图片摘要】照片里是一只蓝色马克杯', history[0]['content'])
        self.assertIn('【引用】五条悟: 刚才那个杯子别乱放。', history[0]['content'])
        self.assertIn('割れてるな。', history[1]['content'])


class ChatlogDeleteRouteTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatlogStore()
        self.patcher = patch(
            'db_chatlog.get_conn',
            side_effect=lambda: FakeConn(self.store),
        )
        self.patcher.start()
        db_chatlog.init_chatlog_table()

    def tearDown(self):
        self.patcher.stop()

    def _body(self, response):
        return json.loads(response.body)

    def test_delete_message_endpoint_returns_ok(self):
        db_chatlog.append_messages('u1', 'gojo', [_msg('delete_test_1')])
        response = asyncio.run(route_chatlog.delete_chatlog_message(
            user_id='u1', chat_id='gojo', client_msg_id='delete_test_1'))
        data = self._body(response)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(data['ok'])
        self.assertEqual(data['deleted'], 1)
        self.assertEqual(data['client_msg_id'], 'delete_test_1')

    def test_delete_message_requires_ids(self):
        missing_chat = asyncio.run(route_chatlog.delete_chatlog_message(
            user_id='u1', chat_id=' ', client_msg_id='x'))
        self.assertEqual(missing_chat.status_code, 400)

        missing_target = asyncio.run(route_chatlog.delete_chatlog_message(
            user_id='u1', chat_id='gojo', client_msg_id=''))
        self.assertEqual(missing_target.status_code, 400)

    def test_bulk_clear_endpoint_still_exists(self):
        src = Path(BACKEND, 'route_chatlog.py').read_text(encoding='utf-8')
        self.assertIn("@router.delete('/chatlog/message')", src)
        self.assertIn("@router.delete('/chatlog')", src)
        self.assertIn('db_chatlog.delete_message(', src)
        self.assertNotIn('short_memory', route_chatlog.delete_chatlog_message.__doc__ or '')


class FrontendChatlogDeleteGuardTests(unittest.TestCase):
    def setUp(self):
        self.src = Path(FRONTEND_CHAT).read_text(encoding='utf-8')
        self.memory = Path(MEMORY_PAGE).read_text(encoding='utf-8')

    def test_single_chat_delete_hits_chatlog_message(self):
        self.assertIn("${SERVER_URL}/chatlog/message", self.src)
        self.assertIn('client_msg_id: String(msg.id)', self.src)
        self.assertIn('server_id: (msg as any).serverId', self.src)
        self.assertIn('[chatlog] 单条删除服务器失败:', self.src)
        self.assertIn('云端记录没有删掉，请稍后再试。', self.src)
        self.assertIn('只删除聊天记录，不删除角色记忆、关系账本或认知证据', self.src)

    def test_delete_message_does_not_call_memory_endpoints(self):
        start = self.src.index('const deleteMessage')
        end = self.src.index('const onBubbleLongPress')
        body = self.src[start:end]
        self.assertNotIn('/long_memory/', body)
        self.assertNotIn('/bond_memory/', body)
        self.assertNotIn('/character_memory/', body)

    def test_hippocampus_delete_paths_unchanged(self):
        self.assertIn('${SERVER_URL}/long_memory/${id}', self.memory)
        self.assertIn('${SERVER_URL}/character_memory/${id}', self.memory)
        self.assertIn('${SERVER_URL}/bond_memory/${id}', self.memory)


if __name__ == '__main__':
    unittest.main()
