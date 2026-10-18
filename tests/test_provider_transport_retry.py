"""Bound relay retries without adding retries for ambiguous I/O failures.

Regression for the 2026-10-18 incident: a 503 from the relay reached
``_generate_or_none`` with ``will_retry=False`` and ``retry_reason=None``, so
the semantic repair attempt was never used and /chat/text answered 502.
These application logs do not establish how many HTTP attempts the SDK made.
"""
import ast
import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))


def load_provider_error():
    spec = importlib.util.spec_from_file_location(
        'provider_error_under_test', ROOT / 'gojo_backend' / 'provider_error.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def error(status=None, name='APIStatusError', cause=None):
    exc = type(name, (Exception,), {})('x')
    if status is not None:
        exc.status_code = status
    if cause is not None:
        exc.__cause__ = cause
    return exc


class RetryClassification(unittest.TestCase):
    def setUp(self):
        self.mod = load_provider_error()

    def test_gateway_failures_are_retryable(self):
        for status in (429, 500, 502, 503, 504):
            with self.subTest(status=status):
                self.assertTrue(self.mod.is_retryable_transport(error(status)))

    def test_request_defects_are_not_retryable(self):
        # An identical retry reproduces these, and 401/403 would burn the key.
        for status in (400, 401, 403, 404, 413, 422):
            with self.subTest(status=status):
                self.assertFalse(self.mod.is_retryable_transport(error(status)))

    def test_unestablished_connection_is_retryable(self):
        for name in ('ConnectError', 'ConnectTimeout'):
            with self.subTest(name=name):
                self.assertTrue(self.mod.is_retryable_transport(error(name=name)))
                self.assertTrue(self.mod.is_retryable_transport(error(
                    name='APIConnectionError', cause=error(name=name))))

    def test_read_timeout_is_not_retryable(self):
        # An extra outer retry could duplicate upstream generation.
        for name in ('APITimeoutError', 'Timeout', 'ReadTimeout', 'WriteTimeout',
                     'ReadError', 'WriteError', 'RemoteProtocolError'):
            with self.subTest(name=name):
                self.assertFalse(self.mod.is_retryable_transport(error(name=name)))
                self.assertFalse(self.mod.is_retryable_transport(error(
                    name='APIConnectionError', cause=error(name=name))))
        self.assertFalse(self.mod.is_retryable_transport(TimeoutError()))

    def test_wrapped_status_is_unwrapped(self):
        wrapped = error(name='APIConnectionError', cause=error(503))
        self.assertTrue(self.mod.is_retryable_transport(wrapped))

    def test_no_status_is_not_retryable(self):
        self.assertFalse(self.mod.is_retryable_transport(error()))
        self.assertFalse(self.mod.is_retryable_transport(None))
        for name in ('APIConnectionError', 'ConnectionError'):
            with self.subTest(name=name):
                self.assertFalse(self.mod.is_retryable_transport(error(name=name)))


class TransportRetryLoop(unittest.TestCase):
    def setUp(self):
        self.mod = load_provider_error()
        self.slept = []

    def run_with(self, outcomes, attempts=3, backoff=0.4):
        calls = []

        def call():
            outcome = outcomes[len(calls)]
            calls.append(outcome)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        class Fixed:
            @staticmethod
            def uniform(low, high):
                return high

        result = self.mod.call_with_transport_retry(
            call, attempts=attempts, backoff_seconds=backoff,
            sleep=self.slept.append, random_=Fixed)
        return result, calls

    def test_retries_until_success(self):
        result, calls = self.run_with([error(503), 'ok'])
        self.assertEqual(result, 'ok')
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(self.slept), 1)

    def test_backoff_grows_between_attempts(self):
        with self.assertRaises(Exception):
            self.run_with([error(503), error(503), error(503)])
        self.assertEqual(len(self.slept), 2)
        self.assertLess(self.slept[0], self.slept[1])

    def test_last_attempt_reraises(self):
        boom = error(503)
        with self.assertRaises(Exception) as caught:
            self.run_with([boom, boom, boom])
        self.assertIs(caught.exception, boom)

    def test_non_retryable_fails_on_first_attempt(self):
        with self.assertRaises(Exception):
            self.run_with([error(400), 'never reached'])
        self.assertEqual(self.slept, [])

    def test_attempts_one_disables_retry(self):
        with self.assertRaises(Exception):
            self.run_with([error(503), 'never reached'], attempts=1)
        self.assertEqual(self.slept, [])

    def test_success_on_first_call_never_sleeps(self):
        result, calls = self.run_with(['ok'])
        self.assertEqual(result, 'ok')
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.slept, [])


class RouteWiring(unittest.TestCase):
    """route_chat imports FastAPI and the provider SDK, so assert structurally."""

    def setUp(self):
        self.tree = ast.parse((ROOT / 'gojo_backend' / 'route_chat.py').read_text(
            encoding='utf-8'))

    def function(self, name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        self.fail(f'{name} not found')

    def test_generation_goes_through_the_retry_wrapper(self):
        called = {node.func.id for node in ast.walk(self.function('_generate_or_none'))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertIn('_create_json_resilient', called)
        self.assertNotIn('_create_json', called)

    def test_provider_unavailable_answers_503(self):
        source = ast.unparse(self.function('_generation_failed_response'))
        self.assertIn('503', source)
        self.assertIn('Retry-After', source)
        self.assertIn('provider_retry_delay_seconds', source)


if __name__ == '__main__':
    unittest.main()
