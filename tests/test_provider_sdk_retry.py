"""Exercise the real installed SDK over an offline HTTP transport, without waits."""
import unittest
from unittest.mock import patch

import anthropic
from anthropic import _base_client

from tests.test_anthropic_client_factory import load_adapter
from provider_error import call_with_transport_retry


# SDK releases use either httpx or httpx2; the injected transport must match.
sdk_http = getattr(_base_client, 'httpx2', None) or _base_client.httpx


class ProviderSDKRetryTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.sdk_sleeps = []
        self.outer_sleeps = []
        self.headers = {}
        self.transport_error = None
        self.adapter = load_adapter()
        http = sdk_http.Client(transport=sdk_http.MockTransport(self.respond))
        self.base = anthropic.Anthropic(
            api_key='offline-test-key', base_url='https://offline.invalid',
            http_client=http)
        self.adapter._anthropic_client = self.base
        self.addCleanup(self.base.close)
        sleep = patch.object(_base_client.time, 'sleep', side_effect=self.sdk_sleeps.append)
        jitter = patch.object(_base_client, 'random', return_value=1.0)
        sleep.start()
        jitter.start()
        self.addCleanup(sleep.stop)
        self.addCleanup(jitter.stop)

    def respond(self, request):
        self.requests.append(request)
        if self.transport_error:
            raise self.transport_error('offline transport failure', request=request)
        return sdk_http.Response(503, headers=self.headers, request=request, json={
            'type': 'error',
            'error': {'type': 'overloaded_error', 'message': 'offline failure'},
        })

    def create(self):
        # Same factory/options as route_chat._provider_create; base defaults must
        # not silently multiply the caller's requested SDK retry budget.
        client = self.adapter.get_anthropic(max_retries=1)
        self.assertEqual(self.base.max_retries, anthropic.DEFAULT_MAX_RETRIES)
        self.assertEqual(client.max_retries, 1)
        self.assertIs(client._client, self.base._client)
        return client.messages.create(model='claude-offline', max_tokens=1,
            messages=[{'role': 'user', 'content': 'offline fixture'}])

    def outer_call(self):
        return call_with_transport_retry(self.create, attempts=2,
            backoff_seconds=0, sleep=self.outer_sleeps.append)

    def assert_retry_headers(self, expected):
        # Recent SDKs expose the internal HTTP attempt count in this header.
        actual = [request.headers.get('x-stainless-retry-count')
                  for request in self.requests]
        if any(value is not None for value in actual):
            self.assertEqual(actual, expected)
        self.assertEqual(len(self.requests), len(expected))

    def test_normal_503_gets_one_sdk_retry(self):
        with self.assertRaises(anthropic.InternalServerError):
            self.create()
        self.assert_retry_headers(['0', '1'])
        self.assertEqual(self.sdk_sleeps, [0.375])

    def test_first_sdk_backoff_is_not_a_fixed_500_milliseconds(self):
        for random_value, expected_delay in ((1.0, 0.375), (0.0, 0.5)):
            with self.subTest(random_value=random_value):
                self.requests.clear()
                self.sdk_sleeps.clear()
                with patch.object(_base_client, 'random', return_value=random_value):
                    with self.assertRaises(anthropic.InternalServerError):
                        self.create()
                self.assert_retry_headers(['0', '1'])
                self.assertEqual(self.sdk_sleeps, [expected_delay])
        self.assertLess(0.375, 0.4332)
        self.assertLess(0.4332, 0.5)

    def test_retry_after_milliseconds_can_make_sdk_retry_faster(self):
        self.headers = {'retry-after-ms': '100'}
        with self.assertRaises(anthropic.InternalServerError):
            self.create()
        self.assert_retry_headers(['0', '1'])
        self.assertEqual(self.sdk_sleeps, [0.1])

    def test_server_header_stops_sdk_retry_but_not_the_outer_status_policy(self):
        self.headers = {'x-should-retry': 'false'}
        with self.assertRaises(anthropic.InternalServerError):
            self.create()
        self.assert_retry_headers(['0'])
        self.assertEqual(self.sdk_sleeps, [])
        self.requests.clear()
        with self.assertRaises(anthropic.InternalServerError):
            self.outer_call()
        self.assert_retry_headers(['0', '0'])
        self.assertEqual(self.sdk_sleeps, [])
        self.assertEqual(self.outer_sleeps, [0])

    def test_two_outer_attempts_times_two_sdk_attempts_make_four_http_requests(self):
        with self.assertRaises(anthropic.InternalServerError):
            self.outer_call()
        self.assert_retry_headers(['0', '1', '0', '1'])
        self.assertEqual(self.sdk_sleeps, [0.375, 0.375])
        self.assertEqual(self.outer_sleeps, [0])

    def test_read_timeout_still_has_sdk_retry_but_no_additional_outer_retry(self):
        self.transport_error = sdk_http.ReadTimeout
        with self.assertRaises(anthropic.APITimeoutError) as caught:
            self.outer_call()
        self.assertIsInstance(caught.exception.__cause__, sdk_http.ReadTimeout)
        self.assert_retry_headers(['0', '1'])
        self.assertEqual(self.sdk_sleeps, [0.375])
        self.assertEqual(self.outer_sleeps, [])

    def test_ambiguous_protocol_failure_is_not_an_outer_connect_retry(self):
        self.transport_error = sdk_http.RemoteProtocolError
        with self.assertRaises(anthropic.APIConnectionError) as caught:
            self.outer_call()
        self.assertIsInstance(caught.exception.__cause__, sdk_http.RemoteProtocolError)
        self.assert_retry_headers(['0', '1'])
        self.assertEqual(self.outer_sleeps, [])

    def test_wrapped_connect_error_can_use_the_outer_retry(self):
        self.transport_error = sdk_http.ConnectError
        with self.assertRaises(anthropic.APIConnectionError) as caught:
            self.outer_call()
        self.assertIsInstance(caught.exception.__cause__, sdk_http.ConnectError)
        self.assert_retry_headers(['0', '1', '0', '1'])
        self.assertEqual(self.outer_sleeps, [0])


if __name__ == '__main__':
    unittest.main()
