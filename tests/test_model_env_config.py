"""Direct Anthropic calls use environment-configured model names verbatim."""
import ast
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


TESTS = os.path.dirname(__file__)
if TESTS not in sys.path:
    sys.path.insert(0, TESTS)

from test_model_failures import BACKEND, load_source  # noqa: E402


EXPECTED_MODELS = {
    'route_group': ['MODEL_JP_AUX', 'MODEL_JP_AUX', 'MODEL_MID'],
    'trending_engine': ['MODEL_JP_AUX', 'MODEL_JP_AUX'],
    'route_story': ['MODEL_MID'],
    'route_memory': ['MODEL_JP_AUX'],
}


def message_model_nodes(source):
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)
             and node.func.attr == 'create'
             and isinstance(node.func.value, ast.Attribute)
             and node.func.value.attr == 'messages']
    return [next((kw.value for kw in call.keywords if kw.arg == 'model'), None)
            for call in sorted(calls, key=lambda node: node.lineno)]


class ModelEnvConfigTests(unittest.TestCase):
    def test_direct_calls_use_config_model_names(self):
        for name, expected in EXPECTED_MODELS.items():
            with self.subTest(module=name):
                source = (Path(BACKEND) / (name + '.py')).read_text(encoding='utf-8')
                models = message_model_nodes(source)
                self.assertEqual(len(models), len(expected))
                for model in models:
                    self.assertIsInstance(model, ast.Name)
                self.assertEqual([model.id for model in models], expected)
                imported = {
                    alias.name
                    for node in ast.parse(source).body
                    if isinstance(node, ast.ImportFrom)
                    and node.module == 'config' and node.level == 0
                    for alias in node.names
                    if alias.asname in (None, alias.name)
                }
                self.assertTrue(set(expected).issubset(imported))

    def test_checker_identifies_a_literal_model(self):
        models = message_model_nodes("client.messages.create(model='literal-model')")
        self.assertEqual(len(models), 1)
        self.assertIsInstance(models[0], ast.Constant)
        self.assertEqual(models[0].value, 'literal-model')

    def test_unset_environment_preserves_default_models(self):
        with patch.dict(os.environ):
            os.environ.pop('MODEL_JP_AUX', None)
            os.environ.pop('MODEL_MID', None)
            config = load_source('config', {})
        self.assertEqual(config.MODEL_JP_AUX, 'claude-haiku-4-5-20251001')
        self.assertEqual(config.MODEL_MID, 'claude-sonnet-4-6')

    def test_environment_model_names_are_preserved_verbatim(self):
        for model in ('[冰咖啡]claude-opus-4-6', ' [冰咖啡]claude-opus-4-6 '):
            with self.subTest(model=model):
                with patch.dict(os.environ, MODEL_JP_AUX=model, MODEL_MID=model):
                    config = load_source('config', {})
                self.assertEqual(config.MODEL_JP_AUX, model)
                self.assertEqual(config.MODEL_MID, model)


if __name__ == '__main__':
    unittest.main()
