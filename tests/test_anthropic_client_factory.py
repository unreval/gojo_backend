"""R6 factory configuration and shared transport contracts, without API calls."""
import ast
from concurrent.futures import ThreadPoolExecutor
import importlib.util
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import anthropic


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))


def load_adapter(timeout=None):
    config = types.ModuleType('config')
    config.ANTHROPIC_KEY = 'offline-test-key'
    config.DEEPSEEK_KEY = ''
    config.DEEPSEEK_BASE_URL = 'https://offline.invalid'
    with patch.dict(os.environ), patch.dict(sys.modules, {'config': config}):
        os.environ.pop('ANTHROPIC_TIMEOUT_SECONDS', None)
        if timeout is not None:
            os.environ['ANTHROPIC_TIMEOUT_SECONDS'] = timeout
        spec = importlib.util.spec_from_file_location(
            'r6_adapter_under_test', ROOT / 'gojo_backend' / 'ai_client.py')
        adapter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
    return adapter


class AnthropicClientFactoryTests(unittest.TestCase):
    def test_defaults_and_overrides_share_sync_transport(self):
        adapter = load_adapter()
        base = adapter._get_anthropic()
        self.addCleanup(base.close)
        default = adapter.get_anthropic()
        chat = adapter.get_anthropic(max_retries=1)
        image = adapter.get_anthropic(max_retries=0, timeout=7.5)
        zero = adapter.get_anthropic(timeout=0)
        self.assertIs(adapter._get_anthropic(), base)
        for client in (default, chat, image, zero):
            self.assertIs(client._client, base._client)
        self.assertEqual((default.max_retries, default.timeout), (2, 120.0))
        self.assertEqual((chat.max_retries, chat.timeout), (1, 120.0))
        self.assertEqual((image.max_retries, image.timeout), (0, 7.5))
        self.assertEqual(zero.timeout, 0)
        self.assertEqual(base.max_retries, 2)
        self.assertNotEqual(base.timeout, 120.0)

    def test_environment_default_applies_to_both_factories(self):
        adapter = load_adapter('43.25')
        sync = adapter.get_anthropic()
        asynchronous = adapter.get_async_anthropic()
        self.addCleanup(sync.close)
        import asyncio
        self.addCleanup(lambda: asyncio.run(asynchronous.close()))
        self.assertEqual(sync.timeout, 43.25)
        self.assertEqual(asynchronous.timeout, 43.25)
        self.assertEqual(adapter.get_anthropic(timeout=9).timeout, 9)
        self.assertEqual(adapter.get_async_anthropic(timeout=8).timeout, 8)

    def test_async_options_reuse_transport_without_mutating_base(self):
        adapter = load_adapter()
        default = adapter.get_async_anthropic()
        custom = adapter.get_async_anthropic(max_retries=0, timeout=5)
        base = adapter._async_client
        import asyncio
        self.addCleanup(lambda: asyncio.run(base.close()))
        self.assertIs(default._client, base._client)
        self.assertIs(custom._client, base._client)
        self.assertEqual((default.max_retries, default.timeout), (2, 120.0))
        self.assertEqual((custom.max_retries, custom.timeout), (0, 5))
        self.assertEqual(base.max_retries, 2)
        self.assertNotEqual(base.timeout, 120.0)

    def test_async_initialization_constructs_once_under_contention(self):
        adapter = load_adapter()
        base = Mock()
        base.with_options.return_value = object()
        ready = threading.Barrier(8)

        def acquire(_index):
            ready.wait(timeout=10)
            return adapter.get_async_anthropic()

        with patch.object(anthropic, 'AsyncAnthropic', return_value=base) as constructor:
            with ThreadPoolExecutor(max_workers=8) as pool:
                clients = list(pool.map(acquire, range(8)))
        constructor.assert_called_once_with(api_key='offline-test-key')
        self.assertEqual(base.with_options.call_count, 8)
        self.assertTrue(all(client is clients[0] for client in clients))

    def test_import_is_lazy_for_both_sdk_clients(self):
        with patch.object(anthropic, 'Anthropic') as sync, \
                patch.object(anthropic, 'AsyncAnthropic') as asynchronous:
            load_adapter()
        sync.assert_not_called()
        asynchronous.assert_not_called()

    def test_sdk_constructors_are_confined_to_adapter(self):
        constructors = []
        for path in sorted((ROOT / 'gojo_backend').rglob('*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id == 'anthropic'
                        and node.func.attr in {'Anthropic', 'AsyncAnthropic'}):
                    constructors.append((path.name, node.func.attr))
        self.assertCountEqual(constructors, [
            ('ai_client.py', 'Anthropic'), ('ai_client.py', 'AsyncAnthropic')])


if __name__ == '__main__':
    unittest.main()
