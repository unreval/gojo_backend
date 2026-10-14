"""A relay label changes provider detection, never the transmitted model name."""
import hashlib
import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


TESTS = os.path.dirname(__file__)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

from test_model_failures import load_client, model_response  # noqa: E402
from provider_error import diagnostics  # noqa: E402


class TaggedModelProviderTests(unittest.TestCase):
    def test_supported_names_dispatch_with_the_original_model(self):
        for model, provider in (
                ('claude-opus-4-6', 'anthropic'),
                ('[冰咖啡]claude-opus-4-6', 'anthropic'),
                ('[x]deepseek-v4-flash', 'deepseek'),
                ('deepseek-v4-flash', 'deepseek'),
                ('anthropic-test', 'anthropic'),
                ('[x]anthropic-test', 'anthropic')):
            with self.subTest(model=model):
                client = load_client()
                with patch.object(client, '_call_anthropic',
                                  return_value=('answer', {'provider': 'anthropic'})) as claude, \
                        patch.object(client, '_call_deepseek',
                                     return_value=('answer', {'provider': 'deepseek'})) as deepseek:
                    result = client.create_chat(model, [], max_tokens=123)
                self.assertEqual(result, ('answer', {'provider': provider}))
                selected, unused = ((claude, deepseek) if provider == 'anthropic'
                                    else (deepseek, claude))
                selected.assert_called_once_with(model, [], None, 123, None)
                unused.assert_not_called()

    def test_unknown_empty_and_malformed_labels_do_not_dispatch(self):
        models = (
            'unknown-model', '[x]unknown-model', '[]claude-x', '[冰咖啡]',
            '', '[a][b]claude-x', '[[x]]claude-x', '[xclaude-x',
            ' CLAUDE-x', '[x]CLAUDE-x', ' [x]claude-x', '[x] claude-x',
        )
        for model in models:
            with self.subTest(model=model):
                client = load_client()
                with patch.object(client, '_call_anthropic') as claude, \
                        patch.object(client, '_call_deepseek') as deepseek:
                    with self.assertRaisesRegex(ValueError, '未知的 model 前缀'):
                        client.create_chat(model, [])
                claude.assert_not_called()
                deepseek.assert_not_called()

    def test_anthropic_sdk_receives_exact_model_and_existing_retry_options(self):
        for model in ('claude-opus-4-6', '[冰咖啡]claude-opus-4-6'):
            for max_retries in (None, 0):
                with self.subTest(model=model, max_retries=max_retries):
                    client = load_client()
                    sdk = Mock()
                    retry_sdk = Mock()
                    sdk.with_options.return_value = retry_sdk
                    sdk.messages.create.return_value = model_response('answer')
                    retry_sdk.messages.create.return_value = model_response('answer')
                    client.anthropic.Anthropic.return_value = sdk
                    messages = [{'role': 'user', 'content': 'hello'}]
                    with patch.object(client, 'ANTHROPIC_KEY', 'fake-anthropic-key'):
                        text, usage = client.create_chat(
                            model, messages, system='system', max_tokens=123,
                            temperature=0.4, max_retries=max_retries)
                    client.anthropic.Anthropic.assert_called_once_with(
                        api_key='fake-anthropic-key')
                    if max_retries is None:
                        sdk.with_options.assert_not_called()
                        sent, unused = sdk, retry_sdk
                    else:
                        sdk.with_options.assert_called_once_with(max_retries=0)
                        sent, unused = retry_sdk, sdk
                    sent.messages.create.assert_called_once_with(
                        model=model, messages=messages, system='system',
                        max_tokens=123, temperature=0.4)
                    unused.messages.create.assert_not_called()
                    self.assertEqual(text, 'answer')
                    self.assertEqual(usage['provider'], 'anthropic')

    def test_deepseek_http_transport_receives_the_exact_tagged_model(self):
        client = load_client()
        response = Mock(status_code=200)
        response.json.return_value = {
            'id': 'response-1',
            'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 12, 'completion_tokens': 4},
        }
        model = '[x]deepseek-v4-flash'
        messages = [{'role': 'user', 'content': 'hello'}]
        with patch.object(client, 'DEEPSEEK_KEY', 'fake-deepseek-key'), \
                patch.object(client, 'DEEPSEEK_BASE_URL', 'https://relay.example/v1/'), \
                patch.object(client.requests, 'post', create=True,
                             return_value=response) as post:
            text, usage = client.create_chat(
                model, messages, system='system', max_tokens=123, temperature=0.4)
        post.assert_called_once_with(
            'https://relay.example/v1/chat/completions',
            headers={'Authorization': 'Bearer fake-deepseek-key',
                     'Content-Type': 'application/json'},
            json={'model': model,
                  'messages': [{'role': 'system', 'content': 'system'}] + messages,
                  'max_tokens': 123, 'temperature': 0.4},
            timeout=90)
        self.assertEqual(text, 'answer')
        self.assertEqual(usage['provider'], 'deepseek')

    def test_diagnostics_infer_provider_without_relaxing_model_redaction(self):
        for model, provider, safe_model in (
                ('claude-opus-4-6', 'anthropic', 'claude-opus-4-6'),
                ('[冰咖啡]claude-opus-4-6', 'anthropic', None),
                ('[x]deepseek-v4-flash', 'deepseek', None),
                ('anthropic-test', 'anthropic', 'anthropic-test'),
                ('[x]anthropic-test', 'anthropic', None),
                ('unknown-model', None, 'unknown-model'),
                ('[]claude-x', None, None),
                ('[冰咖啡]', None, None)):
            with self.subTest(model=model):
                detail = diagnostics(RuntimeError('body'), model=model)
                self.assertEqual(detail['provider'], provider)
                self.assertEqual(detail['model'], safe_model)

    def test_diagnostics_keep_explicit_provider_precedence(self):
        error = RuntimeError('body')
        error.model = '[冰咖啡]claude-opus-4-6'
        error.provider = 'deepseek'
        self.assertEqual(diagnostics(error)['provider'], 'deepseek')
        self.assertEqual(diagnostics(error, provider='anthropic')['provider'], 'anthropic')
        del error.provider
        self.assertEqual(diagnostics(error)['provider'], 'anthropic')

    def test_tagged_error_diagnostics_do_not_echo_credentials(self):
        secret = 'abcdefghijklmnopqrstuvwx'
        error = RuntimeError('Bearer ' + secret)
        error.status_code = 403
        error.body = {'error': {'type': 'permission_error',
                                'message': 'API key ' + secret}}
        error.request = SimpleNamespace(
            url=f'https://user:{secret}@relay.example/v1/messages?api_key={secret}')
        error.response = SimpleNamespace(headers={
            'x-request-id': secret, 'Authorization': 'Bearer ' + secret})
        for model, provider in (
                ('[' + secret + ']claude-opus-4-6', 'anthropic'),
                ('[' + secret + ']deepseek-v4-flash', 'deepseek')):
            with self.subTest(provider=provider):
                detail = diagnostics(error, model=model)
                self.assertEqual(detail, {
                    'provider': provider, 'status': 403, 'code': 'permission_error',
                    'message': 'permission denied', 'model': None,
                    'base_url_host': 'relay.example',
                    'request_id': 'sha256:' + hashlib.sha256(
                        secret.encode('utf-8')).hexdigest()[:16],
                })
                self.assertNotIn(secret, json.dumps(detail))


if __name__ == '__main__':
    unittest.main()
