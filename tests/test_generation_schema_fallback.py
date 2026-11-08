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
