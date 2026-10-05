"""Provider failures keep useful diagnostics without exposing credentials."""
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import ai_client  # noqa: E402
import memory_jobs  # noqa: E402
import rolling_summary  # noqa: E402
import route_chat  # noqa: E402
import structured_output  # noqa: E402
from provider_error import ProviderHTTPError, diagnostics, retry_delay_seconds  # noqa: E402


class PermissionDeniedError(Exception):
    status_code = 403


class ProviderErrorTests(unittest.TestCase):
    def test_only_transient_provider_failures_get_a_retry_delay(self):
        self.assertIsNone(retry_delay_seconds(ProviderHTTPError(
            403, provider='anthropic', model='claude-test')))
        self.assertEqual(retry_delay_seconds(ProviderHTTPError(
            429, provider='deepseek', model='deepseek-test')), 120)
        self.assertEqual(retry_delay_seconds(ProviderHTTPError(
            503, provider='deepseek', model='deepseek-test')), 60)
        try:
            raise RuntimeError('safe transport failure') from TimeoutError()
        except RuntimeError as error:
            self.assertEqual(retry_delay_seconds(error), 60)

    def test_error_message_never_echoes_unknown_credentials_or_user_text(self):
        secret = 'abcdefghijklmnopqrstuvwx'  # fake 24-character credential
        error = ProviderHTTPError(
            403, provider='deepseek', model='deepseek-test',
            code=secret, message=f'用户说了私事，credential {secret}')
        detail = diagnostics(error)
        self.assertEqual(detail['message'], 'permission denied')
        self.assertIsNone(detail['code'])
        self.assertNotIn(secret, str(detail))
        self.assertNotIn('私事', str(detail))

    def test_untrusted_request_id_is_hashed_before_logging(self):
        credential = 'abcdefghijklmnopqrstuvwx'
        error = ProviderHTTPError(
            403, provider='deepseek', model='deepseek-test',
            request_id=credential)
        detail = diagnostics(error)
        self.assertTrue(detail['request_id'].startswith('sha256:'))
        self.assertNotIn(credential, str(detail))

    def test_failed_auth_job_is_not_reenqueued_for_same_source(self):
        cursor = Mock()
        cursor.fetchone.return_value = (42,)
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(memory_jobs, 'get_conn', return_value=connection):
            job_id = memory_jobs._enqueue(
                'rolling_summary', 'u', 'gojo', None, None, '{}',
                source_event_id='segment-1')
        self.assertEqual(job_id, 42)
        self.assertEqual(len(cursor.execute.call_args_list), 1)
        self.assertIn("last_error = 'provider_auth_failed'",
                      cursor.execute.call_args.args[0])
        self.assertIn("kind = 'rolling_summary'",
                      cursor.execute.call_args.args[0])

    def test_failed_auth_memory_summary_is_not_reenqueued(self):
        events = [{'event_id': 'a', 'role': 'user', 'content': 'hello'}]
        key = rolling_summary.segment_hash(['a'])
        rolling_summary.use_memory_store(True)
        rolling_summary.reset_memory_jobs()
        try:
            rolling_summary._MEMORY_JOBS.append({
                'id': 7, 'user_id': 'u', 'character_id': 'gojo',
                'source_event_id': key, 'status': 'failed',
                'last_error': 'provider_auth_failed'})
            with patch.object(rolling_summary, 'should_enqueue_summary', return_value=True):
                job_id = rolling_summary.enqueue_summary_job('u', 'gojo', events)
            self.assertEqual(job_id, 7)
            self.assertEqual(len(rolling_summary.list_memory_jobs()), 1)
            with patch.object(rolling_summary, 'should_enqueue_summary', return_value=True):
                other_user_job = rolling_summary.enqueue_summary_job(
                    'other-user', 'gojo', events)
            self.assertNotEqual(other_user_job, 7)
            self.assertEqual(len(rolling_summary.list_memory_jobs()), 2)
        finally:
            rolling_summary.reset_memory_jobs()
            rolling_summary.use_memory_store(False)

    def test_worker_fails_fast_for_401_and_403_and_retries_transient_errors(self):
        row = (1718, 'rolling_summary', 'u', 'c', '', '', '{}', 1)
        for status in (401, 403):
            error = ProviderHTTPError(
                status, provider='deepseek', model='deepseek-test',
                base_url='https://relay.example/v1', code='auth_error',
                message='Credential rejected')
            with self.subTest(status=status), patch(
                    'rolling_summary.process_summary_job', side_effect=error), patch.object(
                    memory_jobs, '_set_status') as set_status:
                memory_jobs._run_job(row)
            set_status.assert_called_once_with(1718, 'failed', 'provider_auth_failed')
        with patch('rolling_summary.process_summary_job',
                   side_effect=PermissionDeniedError('secret body')), patch.object(
                   memory_jobs, '_set_status') as set_status, patch('builtins.print') as logged:
            memory_jobs._run_job(row)
        set_status.assert_called_once_with(1718, 'failed', 'provider_auth_failed')
        self.assertNotIn('secret body', str(logged.call_args_list))
        for error in (
                ProviderHTTPError(429, provider='deepseek', model='deepseek-test'),
                ProviderHTTPError(503, provider='deepseek', model='deepseek-test'),
                TimeoutError('timeout')):
            with self.subTest(error=type(error).__name__), patch(
                    'rolling_summary.process_summary_job', side_effect=error), patch.object(
                    memory_jobs, '_set_status') as set_status:
                memory_jobs._run_job(row)
            set_status.assert_called_once_with(
                1718, 'pending', 'job_exception', retry_delay_seconds(error))

    @unittest.skipUnless(os.environ.get('COGNITIVE_TEST_PGLITE'), 'isolated PGlite unavailable')
    def test_summary_job_retry_deadline_survives_a_new_worker_claim(self):
        from tests.offline_pg import Connection

        database = Connection()
        try:
            database.query('''CREATE TABLE memory_jobs (
                id SERIAL PRIMARY KEY, kind TEXT, user_id TEXT, character_id TEXT,
                user_text TEXT, assistant_text TEXT, extra_json TEXT,
                attempts INTEGER DEFAULT 0, source_event_id TEXT, assistant_event_id TEXT,
                status TEXT DEFAULT 'pending', last_error TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
            database.query('''INSERT INTO memory_jobs
                (kind, user_id, character_id, extra_json)
                VALUES ('rolling_summary', 'u', 'gojo', '{}')''')
            with patch.object(memory_jobs, 'get_conn', return_value=database):
                memory_jobs._set_status(1, 'pending', 'job_exception', 60)
                self.assertIsNone(memory_jobs._claim_one())
                database.query('''UPDATE memory_jobs
                    SET updated_at = CURRENT_TIMESTAMP - INTERVAL '1 second'
                    WHERE id = 1''')
                claimed = memory_jobs._claim_one()
            self.assertEqual(claimed[0], 1)
            self.assertEqual(claimed[7], 1)
        finally:
            database.shutdown()

    def test_generation_429_stops_after_one_candidate_and_reports_delay(self):
        error_out = {}
        error = ProviderHTTPError(429, provider='anthropic', model='claude-test')
        with patch.object(route_chat, '_create_json', side_effect=error) as create, patch(
                'builtins.print'):
            result, state = route_chat._generate_or_none(
                'claude-test', 100, [], [{'role': 'user', 'content': 'hello'}],
                attempts=2, log_tag='chat:test', cache_tag='chat:test',
                error_out=error_out)
        self.assertIsNone(result)
        self.assertIsNone(state)
        create.assert_called_once()
        self.assertEqual(error_out['provider_retry_delay_seconds'], 120)

    def test_deepseek_error_exposes_safe_metadata_and_never_raw_body(self):
        response = Mock(status_code=403)
        response.json.return_value = {'error': {
            'code': 'permission_denied',
            'message': 'Bearer sk-PRIVATE-CREDENTIAL cannot use this model',
        }}
        response.headers = {'x-request-id': 'req_123'}
        response.text = 'Bearer sk-PRIVATE-CREDENTIAL raw error body'
        with patch.object(ai_client, 'DEEPSEEK_KEY', 'dummy'), patch.object(
                ai_client, 'DEEPSEEK_BASE_URL', 'https://relay.example/v1'), patch.object(
                ai_client.requests, 'post', return_value=response):
            with self.assertRaises(ProviderHTTPError) as raised:
                ai_client._call_deepseek('deepseek-test', [], None, 100, None)
        detail = diagnostics(raised.exception)
        self.assertEqual(detail['status'], 403)
        self.assertEqual(detail['code'], 'permission_denied')
        self.assertEqual(detail['model'], 'deepseek-test')
        self.assertEqual(detail['base_url_host'], 'relay.example')
        self.assertEqual(detail['request_id'], 'req_123')
        self.assertNotIn('PRIVATE-CREDENTIAL', str(raised.exception))
        self.assertNotIn('PRIVATE-CREDENTIAL', str(detail))

    def test_deepseek_non_json_error_does_not_expose_response_text(self):
        response = Mock(status_code=503)
        response.json.side_effect = ValueError('invalid JSON')
        response.headers = {}
        response.text = 'PRIVATE-PROVIDER-RESPONSE'
        with patch.object(ai_client, 'DEEPSEEK_KEY', 'dummy'), patch.object(
                ai_client.requests, 'post', return_value=response):
            with self.assertRaises(ProviderHTTPError) as raised:
                ai_client._call_deepseek('deepseek-test', [], None, 100, None)
        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn('PRIVATE-PROVIDER-RESPONSE', str(raised.exception))
        self.assertNotIn('PRIVATE-PROVIDER-RESPONSE', str(diagnostics(raised.exception)))

    def test_anthropic_metadata_uses_request_host_and_redacts_message(self):
        error = PermissionDeniedError('Bearer sk-PRIVATE-CREDENTIAL')
        error.body = {'error': {
            'type': 'permission_error',
            'message': 'API key sk-PRIVATE-CREDENTIAL cannot use this model',
        }}
        error.request = SimpleNamespace(url='https://relay.example/v1/messages')
        error.request_id = 'req_456'
        detail = diagnostics(error, model='claude-test', provider='anthropic')
        self.assertEqual(detail['status'], 403)
        self.assertEqual(detail['code'], 'permission_error')
        self.assertEqual(detail['model'], 'claude-test')
        self.assertEqual(detail['base_url_host'], 'relay.example')
        self.assertEqual(detail['request_id'], 'req_456')
        self.assertNotIn('PRIVATE-CREDENTIAL', str(detail))

    def test_structured_output_logs_safe_provider_fields(self):
        error = PermissionDeniedError('Bearer sk-PRIVATE-CREDENTIAL')
        error.body = {'error': {'type': 'permission_error',
                                'message': 'Bearer sk-PRIVATE-CREDENTIAL denied'}}
        error.request = SimpleNamespace(url='https://relay.example/v1/messages')
        error.request_id = 'req_789'
        with patch('builtins.print') as logged, self.assertRaises(PermissionDeniedError):
            structured_output.invoke_structured_llm(
                domain='rolling_summary', create_chat_fn=Mock(side_effect=error),
                model='claude-test', messages=[])
        output = str(logged.call_args_list)
        for expected in ('status_code=403', 'provider_error_code=permission_error',
                         'base_url_host=relay.example', 'request_id=req_789'):
            self.assertIn(expected, output)
        self.assertNotIn('PRIVATE-CREDENTIAL', output)

    def test_generation_trace_includes_safe_provider_fields(self):
        error = PermissionDeniedError('Bearer sk-PRIVATE-CREDENTIAL')
        error.body = {'error': {'type': 'permission_error',
                                'message': 'Bearer sk-PRIVATE-CREDENTIAL denied'}}
        error.request = SimpleNamespace(url='https://relay.example/v1/messages')
        error.request_id = 'req_999'
        payload = route_chat._build_generation_trace_payload(
            model='claude-test', max_tokens=100, system_blocks=[], messages=[],
            attempt=1, attempts=2, trace_context={'provider': 'anthropic'},
            error=error, outcome='provider_error')
        detail = payload['provider_error']
        self.assertEqual(detail['status'], 403)
        self.assertEqual(detail['code'], 'permission_error')
        self.assertEqual(detail['base_url_host'], 'relay.example')
        self.assertEqual(detail['request_id'], 'req_999')
        self.assertFalse(payload['will_retry'])
        self.assertNotIn('PRIVATE-CREDENTIAL', str(payload))

    def test_generation_403_fails_once_without_response_or_semantic_retry(self):
        error = PermissionDeniedError('Bearer sk-PRIVATE-CREDENTIAL')
        error.body = {'error': {'type': 'permission_error',
                                'message': 'Bearer sk-PRIVATE-CREDENTIAL denied'}}
        error.request = SimpleNamespace(url='https://relay.example/v1/messages')
        error_out = {}
        with patch.object(route_chat, '_create_json', side_effect=error) as create, patch(
                'builtins.print') as logged:
            result, state = route_chat._generate_or_none(
                'claude-test', 100, [], [{'role': 'user', 'content': 'hello'}],
                attempts=2, log_tag='chat:test', cache_tag='chat:test',
                generation_trace={'provider': 'anthropic'}, error_out=error_out)
        self.assertIsNone(result)
        self.assertIsNone(state)
        create.assert_called_once()
        self.assertTrue(error_out['provider_auth_failed'])
        output = str(logged.call_args_list)
        self.assertIn('generation_failed', output)
        self.assertIn('status_code=403', output)
        self.assertNotIn('PRIVATE-CREDENTIAL', output)


if __name__ == '__main__':
    unittest.main()
