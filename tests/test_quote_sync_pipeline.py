"""Offline canonical quote receipts and delayed pairing regressions."""
import importlib.util
import asyncio
import json
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


BACKEND = Path(__file__).resolve().parents[1] / 'gojo_backend'


class Store:
    def __init__(self):
        self.rows = []
        self.tombstones = set()
        self.commits = 0

    def seed(self, client_id, role='gojo', text='已核验原话', user='u', chat='gojo'):
        row = {
            'id': len(self.rows) + 1, 'user': user, 'chat': chat,
            'client': client_id, 'event': client_id, 'role': role, 'text': text,
            'subtitle': '', 'emotion': '', 'kind': 'text', 'extra': '',
            'has_audio': False, 'reply': '', 'status': 'active',
            'ts': datetime(2026, 10, 4, 8, tzinfo=timezone.utc),
        }
        self.rows.append(row)
        return row


class Cursor:
    def __init__(self, store):
        self.store = store
        self.one = None
        self.many = []
        self.rowcount = 0

    def execute(self, sql, args=()):
        args = tuple(args or ())
        flat = ' '.join(sql.split())
        self.one, self.many, self.rowcount = None, [], 0
        if flat.startswith('SELECT 1 FROM chat_log_tombstone'):
            if args in self.store.tombstones:
                self.one = (1,)
            return
        if flat.startswith('SELECT id, client_msg_id, COALESCE'):
            user, chat, *keys = args
            matches = [r for r in self.store.rows if r['user'] == user
                       and r['chat'] == chat and r['status'] == 'active'
                       and (user, chat, r['client']) not in self.store.tombstones
                       and (r['id'] == keys[0] if len(keys) == 1 and isinstance(keys[0], int)
                            else r['event'] in keys or r['client'] in keys)]
            self.many = [(r['id'], r['client'], r['event'], r['role'], r['text'],
                          r['subtitle'], r['ts'], r['extra']) for r in matches]
            return
        if flat.startswith('SELECT id, client_msg_id, event_id, role, text'):
            user, chat, client, event = args
            matches = [r for r in self.store.rows if r['user'] == user
                       and r['chat'] == chat and (r['client'] == client or r['event'] == event)]
            if matches:
                r = matches[-1]
                self.one = (r['id'], r['client'], r['event'], r['role'], r['text'],
                            r['subtitle'], r['emotion'], r['kind'], r['extra'],
                            r['has_audio'], r['reply'], r['status'])
            return
        if flat.startswith('SELECT id, client_msg_id, role, text'):
            user, chat = args[:2]
            rows = [r for r in self.store.rows if r['user'] == user
                    and r['chat'] == chat and r['status'] == 'active'
                    and (user, chat, r['client']) not in self.store.tombstones]
            if len(args) == 5 and isinstance(args[2], str):
                term = args[2].strip('%').lower()
                rows = [r for r in rows if term in r['text'].lower()
                        or term in r['subtitle'].lower()]
            elif len(args) == 4:
                rows = [r for r in rows if r['id'] < args[2]]
            rows = sorted(rows, key=lambda r: r['id'], reverse=True)[:args[-1]]
            self.many = [(r['id'], r['client'], r['role'], r['text'],
                          r['subtitle'], r['emotion'], r['kind'], r['extra'],
                          r['has_audio'], r['ts'], r['event'], r['reply'])
                         for r in rows]
            return
        if flat.startswith('INSERT INTO chat_log'):
            user, chat, client, role, text, subtitle, emotion, kind, extra, audio, event, reply = args[:12]
            if any(r['user'] == user and r['chat'] == chat and
                   (r['client'] == client or r['event'] == event) for r in self.store.rows):
                return
            row = self.store.seed(client, role, text, user, chat)
            row.update(event=event, subtitle=subtitle, emotion=emotion,
                       kind=kind, extra=extra, has_audio=audio, reply=reply)
            self.one, self.rowcount = (row['id'],), 1
            return
        if flat.startswith('UPDATE chat_log SET reply_to_event_id'):
            reply, extra, row_id, user, chat, _expected = args
            matches = [r for r in self.store.rows if r['id'] == row_id
                       and r['user'] == user and r['chat'] == chat
                       and r['status'] == 'active' and r['reply'] in ('', reply)]
            if matches:
                matches[0]['reply'], matches[0]['extra'] = reply, extra
                self.rowcount = 1
            return
        raise AssertionError(f'unexpected SQL: {flat}')

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.many

    def close(self):
        pass


class Conn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return Cursor(self.store)

    def commit(self):
        self.store.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass


def load_chatlog(store):
    database = types.ModuleType('db')
    database.get_conn = lambda: Conn(store)
    spec = importlib.util.spec_from_file_location(
        '_quote_chatlog_under_test', BACKEND / 'db_chatlog.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'db': database}):
        spec.loader.exec_module(module)
    return module


class QuoteReceiptTests(unittest.TestCase):
    def setUp(self):
        self.store = Store()
        self.chatlog = load_chatlog(self.store)

    def quote(self, text='你说的啊'):
        return {
            'client_msg_id': 'new-user', 'role': 'user', 'text': text,
            'extra': json.dumps({'reply_to': {
                'id': 'old-gojo', 'text': '客户端伪造内容', 'role': 'gojo'}},
                ensure_ascii=False),
        }

    def test_quote_before_target_then_retry_enriches_only_metadata(self):
        first = self.chatlog.append_messages_confirmed('u', 'gojo', [self.quote()])[0]
        self.assertEqual((first['status'], first['reference_status']),
                         ('inserted', 'unavailable'))
        self.assertEqual(self.store.rows[0]['text'], '你说的啊')
        self.store.seed('old-gojo')
        second = self.chatlog.append_messages_confirmed('u', 'gojo', [self.quote()])[0]
        self.assertEqual(second['status'], 'metadata_enriched')
        self.assertEqual(self.store.rows[0]['reply'], 'old-gojo')
        self.assertEqual(self.store.rows[0]['text'], '你说的啊')
        self.assertEqual(json.loads(self.store.rows[0]['extra'])['reply_to']['text'],
                         '已核验原话')
        self.assertEqual(self.chatlog.append_messages_confirmed(
            'u', 'gojo', [self.quote()])[0]['status'], 'already_identical')

    def test_target_first_and_semantic_conflict(self):
        self.store.seed('old-gojo')
        first = self.chatlog.append_messages_confirmed('u', 'gojo', [self.quote()])[0]
        self.assertEqual(first['status'], 'inserted')
        self.assertEqual(first['reply_to_event_id'], 'old-gojo')
        changed = self.chatlog.append_messages_confirmed(
            'u', 'gojo', [self.quote('被覆盖的正文')])[0]
        self.assertEqual(changed['status'], 'conflict')
        self.assertEqual(self.store.rows[-1]['text'], '你说的啊')

    def test_tombstone_blocks_reappend_and_deleted_target_is_not_evidence(self):
        self.store.tombstones.add(('u', 'gojo', 'new-user'))
        result = self.chatlog.append_messages_confirmed('u', 'gojo', [self.quote()])[0]
        self.assertEqual(result['status'], 'deleted_rejected')
        self.store.tombstones.clear()
        self.store.seed('old-gojo')
        self.store.tombstones.add(('u', 'gojo', 'old-gojo'))
        result = self.chatlog.append_messages_confirmed('u', 'gojo', [self.quote()])[0]
        self.assertEqual(result['reference_status'], 'unavailable')
        self.assertFalse(self.store.rows[-1]['reply'])

    def test_cross_chat_and_client_preview_cannot_supply_quote(self):
        self.store.seed('old-gojo', chat='other')
        self.assertIsNone(self.chatlog.resolve_reply_reference(
            'u', 'gojo', {'id': 'old-gojo', 'text': '客户端伪造内容'}))
        result = self.chatlog.append_messages_confirmed('u', 'gojo', [self.quote()])[0]
        self.assertEqual(result['reference_status'], 'unavailable')
        self.assertNotIn('客户端伪造内容', self.store.rows[-1]['text'])

    def test_cold_reload_derives_quote_from_active_source_then_hides_deleted_source(self):
        self.store.seed('old-gojo', text='真实原话')
        self.chatlog.append_messages_confirmed('u', 'gojo', [self.quote()])
        saved = self.store.rows[-1]
        # The persisted preview may be stale. Hydration reads the source again.
        stale = json.loads(saved['extra'])
        stale['reply_to']['text'] = '旧缓存伪造文本'
        view = {'role': 'user', 'extra': json.dumps(stale),
                'reply_to_event_id': saved['reply']}
        self.chatlog._attach_reply_display('u', 'gojo', [view])
        preview = json.loads(view['extra'])['reply_to']
        self.assertEqual(preview['text'], '真实原话')
        self.assertEqual(preview['source_event_id'], 'old-gojo')
        self.assertEqual(preview['role'], 'gojo')
        self.assertTrue(preview['ts'])
        self.store.tombstones.add(('u', 'gojo', 'old-gojo'))
        self.chatlog._attach_reply_display('u', 'gojo', [view])
        unavailable = json.loads(view['extra'])
        self.assertNotIn('reply_to', unavailable)
        self.assertTrue(unavailable['reply_unavailable'])

    def test_stale_empty_assistant_subtitle_cannot_erase_repaired_translation(self):
        row = self.store.seed('chat_reply:source:0', text='元の文')
        row['subtitle'] = '已补的中文'
        same_event = {
            'client_msg_id': row['client'], 'event_id': row['event'],
            'role': 'gojo', 'text': row['text'], 'subtitle': '',
        }
        raw = types.ModuleType('raw_events')
        raw.infer_assistant_identity = lambda _role, _event, extra: extra
        with patch.dict(sys.modules, {'raw_events': raw}):
            receipt = self.chatlog.append_messages_confirmed(
                'u', 'gojo', [same_event])[0]
            self.assertEqual(receipt['status'], 'already_identical')
            self.assertEqual(row['subtitle'], '已补的中文')
            changed = dict(same_event, subtitle='另一份中文')
            self.assertEqual(self.chatlog.append_messages_confirmed(
                'u', 'gojo', [changed])[0]['status'], 'conflict')
        self.assertEqual(row['subtitle'], '已补的中文')

    def test_paginated_reload_and_search_hydrate_canonical_quote(self):
        self.store.seed('old-gojo', text='原话在较早分页')
        self.chatlog.append_messages_confirmed('u', 'gojo', [self.quote()])
        self.store.seed('newer', role='user', text='后来的消息')
        with patch.object(self.chatlog, 'hidden_aggregate_event_ids', return_value=set()), \
                patch.object(self.chatlog, '_attach_chat_media'):
            newest, more = self.chatlog.get_messages('u', 'gojo', limit=1)
            self.assertEqual(newest[0]['client_msg_id'], 'newer')
            self.assertTrue(more)
            quoted, _more = self.chatlog.get_messages(
                'u', 'gojo', limit=1, before_id=newest[0]['id'])
        self.assertEqual(quoted[0]['client_msg_id'], 'new-user')
        self.assertEqual(json.loads(quoted[0]['extra'])['reply_to']['text'],
                         '原话在较早分页')

        fastapi = types.ModuleType('fastapi')
        fastapi.APIRouter = lambda: types.SimpleNamespace(
            get=lambda *_a, **_k: lambda fn: fn)
        responses = types.ModuleType('fastapi.responses')
        responses.JSONResponse = lambda body, status_code=200: types.SimpleNamespace(
            body=body, status_code=status_code)
        database = types.ModuleType('db')
        database.get_conn = lambda: Conn(self.store)
        spec = importlib.util.spec_from_file_location(
            '_quote_search_under_test', BACKEND / 'route_chatlog_search.py')
        search = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'db': database, 'db_chatlog': self.chatlog,
                                      'fastapi': fastapi,
                                      'fastapi.responses': responses}), \
                patch.object(self.chatlog, 'filter_visible_messages',
                             side_effect=lambda _u, _c, msgs: msgs):
            spec.loader.exec_module(search)
            result = asyncio.run(search.search_chatlog(
                'u', 'gojo', keyword='你说的啊'))
        self.assertEqual(result.body['count'], 1)
        self.assertEqual(json.loads(result.body['results'][0]['extra'])['reply_to']['text'],
                         '原话在较早分页')


class PendingPairingTests(unittest.TestCase):
    def test_multiline_messages_keep_their_own_quote_ids(self):
        from reply_availability import format_pending_bundle_context
        events = [
            {'event_id': 'u1', 'content': '第一行\n第二行',
             'timestamp': datetime(2026, 10, 4, 8, tzinfo=timezone.utc),
             'verified_reply': {'source_event_id': 'g1', 'name': '角色',
                                'role': 'gojo', 'ts': '2026-10-03T08:00:00Z',
                                'text': '原话一', 'subtitle': ''}},
            {'event_id': 'u2', 'content': '另一条\n也有第二行',
             'timestamp': datetime(2026, 10, 4, 9, tzinfo=timezone.utc),
             'verified_reply': {'source_event_id': 'g2', 'name': '角色',
                                'role': 'gojo', 'ts': '2026-10-03T09:00:00Z',
                                'text': '原话二', 'subtitle': ''}},
        ]
        result = format_pending_bundle_context({'activity_title': '工作'}, events)
        first, second = result.split('【积压消息 source_event_id=u2')
        self.assertIn('source_event_id=u1', first)
        self.assertIn('原话一', first)
        self.assertNotIn('原话二', first)
        self.assertIn('原话二', second)
        self.assertIn('另一条\n也有第二行', second)


class BatchBoundaryTests(unittest.TestCase):
    def test_backend_rejects_101_without_silent_slice(self):
        chatlog = types.ModuleType('db_chatlog')
        chatlog.append_messages_confirmed = Mock()
        fastapi = types.ModuleType('fastapi')
        fastapi.APIRouter = lambda: types.SimpleNamespace(
            get=lambda *_a, **_k: lambda fn: fn,
            post=lambda *_a, **_k: lambda fn: fn,
            delete=lambda *_a, **_k: lambda fn: fn)
        responses = types.ModuleType('fastapi.responses')
        responses.JSONResponse = lambda body, status_code=200: types.SimpleNamespace(
            body=body, status_code=status_code)
        spec = importlib.util.spec_from_file_location(
            '_quote_route_under_test', BACKEND / 'route_chatlog.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'db_chatlog': chatlog, 'fastapi': fastapi,
                                      'fastapi.responses': responses}):
            spec.loader.exec_module(module)
            result = asyncio.run(module.append_chatlog({
                'user_id': 'u', 'chat_id': 'gojo',
                'messages': [{'client_msg_id': str(index)} for index in range(101)],
            }))
        self.assertEqual(result.status_code, 400)
        self.assertEqual(len(result.body['unprocessed_ids']), 101)
        chatlog.append_messages_confirmed.assert_not_called()


if __name__ == '__main__':
    unittest.main()
