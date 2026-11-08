"""Schema probe capability: the operator switch and the unchanged success path."""
import importlib
import json
import os
import types
import unittest
from unittest.mock import patch

import config
import route_chat
from generation_contract import GENERATION_ENVELOPE_SCHEMA


def _response(text):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type='text', text=text)],
        stop_reason='end_turn', usage=None)


ENVELOPE = json.dumps({
    'emotion': '平静',
    'messages': [{'jp': 'そうだね', 'zh': '是啊'}],
}, ensure_ascii=False)


class SchemaSwitchTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop('GENERATION_JSON_SCHEMA', None)
        importlib.reload(config)

    def test_switch_off_never_sends_output_config(self):
        model = 'claude-schema-switch-off'
        route_chat._schema_unavailable_models.discard(model)
        for raw_value in ('0', 'false', 'False'):
            with self.subTest(raw_value=raw_value):
                os.environ['GENERATION_JSON_SCHEMA'] = raw_value
                importlib.reload(config)
                self.assertFalse(config.GENERATION_JSON_SCHEMA)
                with patch.object(route_chat, 'GENERATION_JSON_SCHEMA',
                                   config.GENERATION_JSON_SCHEMA), \
                        patch.object(route_chat, 'get_anthropic') as client_factory:
                    provider = client_factory.return_value.messages.create
                    provider.return_value = _response(ENVELOPE)
                    raw, _ = route_chat._create_json(model, 100, [], [])
                self.assertEqual(raw, ENVELOPE)
                provider.assert_called_once()
                self.assertNotIn('output_config', provider.call_args.kwargs)
                self.assertNotIn(model, route_chat._schema_unavailable_models)

    def test_default_switch_keeps_the_schema_request(self):
        os.environ.pop('GENERATION_JSON_SCHEMA', None)
        importlib.reload(config)
        self.assertTrue(config.GENERATION_JSON_SCHEMA)
        model = 'claude-schema-switch-default'
        route_chat._schema_unavailable_models.discard(model)
        try:
            with patch.object(route_chat, 'GENERATION_JSON_SCHEMA', True), \
                    patch.object(route_chat, 'get_anthropic') as client_factory, \
                    patch('builtins.print') as logged:
                provider = client_factory.return_value.messages.create
                provider.return_value = _response(ENVELOPE)
                raw, _ = route_chat._create_json(model, 100, [], [])
            self.assertEqual(raw, ENVELOPE)
            provider.assert_called_once()
            sent = provider.call_args.kwargs
            self.assertEqual(sent['output_config'], {'format': {
                'type': 'json_schema',
                'schema': GENERATION_ENVELOPE_SCHEMA,
            }})
            self.assertEqual(sent['output_config']['format']['schema'],
                             GENERATION_ENVELOPE_SCHEMA)
            self.assertNotIn(model, route_chat._schema_unavailable_models)
            texts = [call.args[0] for call in logged.call_args_list
                     if call.args and isinstance(call.args[0], str)]
            self.assertFalse(any('schema returned empty' in text for text in texts))
            self.assertFalse(any('schema unavailable' in text for text in texts))
        finally:
            route_chat._schema_unavailable_models.discard(model)


class _Rejected(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class SchemaEmptyProbeTests(unittest.TestCase):
    def setUp(self):
        self.model = 'claude-schema-empty-probe'
        route_chat._schema_unavailable_models.discard(self.model)

    def tearDown(self):
        route_chat._schema_unavailable_models.discard(self.model)

    def test_empty_schema_probe_retries_once_without_schema(self):
        transport_calls = []
        real_transport = route_chat.call_with_transport_retry

        def _spy(call, **kwargs):
            transport_calls.append(1)
            return real_transport(call, **kwargs)

        with patch.object(route_chat, 'get_anthropic') as client_factory, \
                patch.object(route_chat, 'call_with_transport_retry', _spy), \
                patch('builtins.print') as logged:
            provider = client_factory.return_value.messages.create
            provider.side_effect = [_response(' \n '), _response(ENVELOPE)]
            result, _ = route_chat._generate_or_none(
                self.model, 100, [], [], attempts=2, log_tag='test', cache_tag='test',
                generation_trace={'source_event_id': 'schema-empty'})
        self.assertIsNotNone(result)
        self.assertEqual(result['messages'][0]['jp'], 'そうだね')
        self.assertEqual(provider.call_count, 2)
        self.assertIn('output_config', provider.call_args_list[0].kwargs)
        self.assertNotIn('output_config', provider.call_args_list[1].kwargs)
        self.assertEqual(transport_calls, [1])
        self.assertIn(self.model, route_chat._schema_unavailable_models)
        summaries = [json.loads(call.args[0].split(' ', 1)[1])
                     for call in logged.call_args_list
                     if call.args and isinstance(call.args[0], str)
                     and call.args[0].startswith('[generation_result] ')]
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]['status'], 'structured_ok')
        self.assertEqual((summaries[0]['provider_calls'],
                          summaries[0]['semantic_retries']), (2, 0))
        notes = [call.args[0] for call in logged.call_args_list
                 if call.args and isinstance(call.args[0], str)]
        self.assertIn(
            f'[generation] schema returned empty model={self.model}; using prompt contract',
            notes)

    def test_second_call_skips_schema_after_empty_probe(self):
        with patch.object(route_chat, 'get_anthropic') as first_factory:
            first = first_factory.return_value.messages.create
            first.side_effect = [_response(''), _response(ENVELOPE)]
            route_chat._create_json(self.model, 100, [], [])
        with patch.object(route_chat, 'get_anthropic') as second_factory, \
                patch('builtins.print'):
            second = second_factory.return_value.messages.create
            second.return_value = _response(ENVELOPE)
            raw, _ = route_chat._create_json(self.model, 100, [], [])
        self.assertEqual(raw, ENVELOPE)
        second.assert_called_once()
        self.assertNotIn('output_config', second.call_args.kwargs)

    def test_schema_400_still_falls_back_once_and_is_remembered(self):
        with patch.object(route_chat, 'get_anthropic') as client_factory, \
                patch('builtins.print') as logged:
            provider = client_factory.return_value.messages.create
            provider.side_effect = [
                _Rejected('json_schema is not supported', 400),
                _response(ENVELOPE),
                _response(ENVELOPE),
            ]
            first, _ = route_chat._create_json(self.model, 100, [], [])
            second, _ = route_chat._create_json(self.model, 100, [], [])
        self.assertEqual(first, ENVELOPE)
        self.assertEqual(second, ENVELOPE)
        self.assertEqual(provider.call_count, 3)
        self.assertIn('output_config', provider.call_args_list[0].kwargs)
        self.assertNotIn('output_config', provider.call_args_list[1].kwargs)
        self.assertNotIn('output_config', provider.call_args_list[2].kwargs)
        self.assertIn(self.model, route_chat._schema_unavailable_models)
        notes = [call.args[0] for call in logged.call_args_list
                 if call.args and isinstance(call.args[0], str)]
        self.assertTrue(any(
            note.startswith(f'[generation] schema unavailable model={self.model} ')
            and 'error_type=_Rejected' in note for note in notes))

    def test_unrelated_400_still_propagates(self):
        with patch.object(route_chat, 'get_anthropic') as client_factory:
            provider = client_factory.return_value.messages.create
            provider.side_effect = _Rejected('invalid request', 400)
            with self.assertRaises(_Rejected):
                route_chat._create_json(self.model, 100, [], [])
        provider.assert_called_once()
        self.assertNotIn(self.model, route_chat._schema_unavailable_models)

    def test_plain_empty_response_is_not_retried(self):
        route_chat._schema_unavailable_models.add(self.model)
        with patch.object(route_chat, 'get_anthropic') as client_factory, \
                patch('builtins.print') as logged:
            provider = client_factory.return_value.messages.create
            provider.return_value = _response('')
            raw, _ = route_chat._create_json(self.model, 100, [], [])
        self.assertEqual(raw, '')
        provider.assert_called_once()
        self.assertNotIn('output_config', provider.call_args.kwargs)
        notes = [call.args[0] for call in logged.call_args_list
                 if call.args and isinstance(call.args[0], str)]
        self.assertFalse(any('schema returned empty' in note for note in notes))
        self.assertFalse(any('schema unavailable' in note for note in notes))
