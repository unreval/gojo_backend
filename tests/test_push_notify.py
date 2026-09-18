# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import push_notify  # noqa: E402


class FakeHTTPResponse:
    def __init__(self, payload, status=200):
        self.status = status
        self._payload = json.dumps(payload).encode('utf-8')

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class PushTokenStore:
    def __init__(self, tokens):
        self.tokens = list(tokens)
        self.deleted = []
        self.rowcount = 0
        self._many = []

    def cursor(self):
        return self

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        if compact.startswith('SELECT token FROM push_token'):
            self._many = [(token,) for token in self.tokens]
            return
        if compact.startswith('DELETE FROM push_token WHERE token'):
            token = params[0]
            self.deleted.append(token)
            before = len(self.tokens)
            self.tokens = [item for item in self.tokens if item != token]
            self.rowcount = before - len(self.tokens)
            return
        if 'DELETE FROM push_token WHERE user_id' in compact:
            raise AssertionError('must not delete by user_id')

    def fetchall(self):
        return list(self._many)


class PushNotifyTests(unittest.TestCase):
    def test_delete_token_sql_targets_token_not_user(self):
        source = Path(os.path.join(BACKEND, 'push_notify.py')).read_text(encoding='utf-8')
        self.assertIn('DELETE FROM push_token WHERE token=%s', source)
        self.assertNotIn('DELETE FROM push_token WHERE user_id', source)

    def test_device_not_registered_deletes_only_that_token(self):
        tokens = ['ExponentPushToken[aaa111]', 'ExponentPushToken[bbb222]']
        store = PushTokenStore(tokens)
        deleted = []

        def fake_urlopen(req, timeout=10):
            body = json.loads(req.data.decode('utf-8'))
            if body['to'] == tokens[0]:
                return FakeHTTPResponse({
                    'data': {
                        'status': 'error',
                        'details': {'error': 'DeviceNotRegistered'},
                    }
                })
            return FakeHTTPResponse({'data': {'status': 'ok', 'id': '1'}})

        with patch.object(push_notify, 'get_tokens', return_value=list(tokens)), \
             patch.object(push_notify, 'delete_token',
                          side_effect=lambda token: deleted.append(token)), \
             patch.object(push_notify, 'get_conn', return_value=store), \
             patch('urllib.request.urlopen', fake_urlopen):
            push_notify.push_to_user('u', 'title', 'body')
        self.assertEqual(deleted, [tokens[0]])

    def test_generic_push_error_does_not_delete_token(self):
        token = 'ExponentPushToken[ccc333]'
        deleted = []

        def fake_urlopen(req, timeout=10):
            return FakeHTTPResponse({
                'data': {
                    'status': 'error',
                    'details': {'error': 'MessageTooBig'},
                }
            })

        with patch.object(push_notify, 'get_tokens', return_value=[token]), \
             patch.object(push_notify, 'delete_token',
                          side_effect=lambda item: deleted.append(item)), \
             patch('urllib.request.urlopen', fake_urlopen):
            push_notify.push_to_user('u', 'title', 'body')
        self.assertEqual(deleted, [])

    def test_http_error_without_device_flag_keeps_token(self):
        token = 'ExponentPushToken[ddd444]'
        deleted = []

        def fake_urlopen(req, timeout=10):
            raise HTTPError(
                push_notify.EXPO_PUSH_URL, 500, 'server', hdrs={},
                fp=BytesIO(b'{"data":{"status":"error"}}'),
            )

        with patch.object(push_notify, 'get_tokens', return_value=[token]), \
             patch.object(push_notify, 'delete_token',
                          side_effect=lambda item: deleted.append(item)), \
             patch('urllib.request.urlopen', fake_urlopen):
            push_notify.push_to_user('u', 'title', 'body')
        self.assertEqual(deleted, [])

    def test_all_valid_device_tokens_are_pushed(self):
        tokens = ['ExponentPushToken[ok1111]', 'ExponentPushToken[ok2222]']
        sent = []

        def fake_urlopen(req, timeout=10):
            body = json.loads(req.data.decode('utf-8'))
            sent.append(body['to'])
            return FakeHTTPResponse({'data': {'status': 'ok', 'id': 'x'}})

        deleted = []
        with patch.object(push_notify, 'get_tokens', return_value=list(tokens)), \
             patch.object(push_notify, 'delete_token',
                          side_effect=lambda item: deleted.append(item)), \
             patch('urllib.request.urlopen', fake_urlopen):
            push_notify.push_to_user('u', 'title', 'body')
        self.assertEqual(sent, tokens)
        self.assertEqual(deleted, [])

    def test_delete_token_uses_token_equality(self):
        store = PushTokenStore([
            'ExponentPushToken[keep]', 'ExponentPushToken[drop]',
        ])
        with patch.object(push_notify, 'get_conn', return_value=store):
            n = push_notify.delete_token('ExponentPushToken[drop]')
        self.assertEqual(n, 1)
        self.assertEqual(store.tokens, ['ExponentPushToken[keep]'])
        self.assertEqual(store.deleted, ['ExponentPushToken[drop]'])


if __name__ == '__main__':
    unittest.main()
