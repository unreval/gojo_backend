"""Derived summary uses the shared parser and propagates only safe diagnostics."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import memory_jobs
import rolling_summary
from structured_output import StructuredOutputError


class RollingSummaryStructuredTests(unittest.TestCase):
    def output(self, text='完成讨论'):
        fields = {key: '' for key, _label in rolling_summary.SUMMARY_FIELDS}
        fields['what_happened'] = text
        return json.dumps(fields, ensure_ascii=False)

    def generate(self, raw):
        with patch('ai_client.create_chat', return_value=(raw, {})) as model:
            text = rolling_summary.generate_real_summary_text(
                [{'event_id': 'e1', 'role': 'user', 'content': 'private source'}])
        model.assert_called_once()
        return text

    def test_one_object_and_shared_wrappers(self):
        raw = self.output()
        fence = chr(96) * 3
        for wrapped in (raw, f'{fence}json\n{raw}\n{fence}', f'说明\n{raw}\n结束', raw + raw):
            with self.subTest(wrapped=wrapped):
                self.assertIn('发生：完成讨论', self.generate(wrapped))

    def test_fail_closed_error_codes(self):
        cases = [
            ('', 'empty_response'),
            ('plain text', 'no_top_level_json_object'),
            ('{"what_happened": bad}', 'invalid_json'),
            ('{"what_happened":', 'incomplete_json'),
            (self.output() + self.output('不同内容'), 'multiple_distinct_json_objects'),
            ('[]', 'schema_validation_failed'),
            ('{"what_happened":"缺失字段"}', 'schema_validation_failed'),
            (self.output(''), 'empty_summary'),
        ]
        for raw, expected in cases:
            with self.subTest(expected=expected), self.assertRaises(StructuredOutputError) as raised:
                self.generate(raw)
            self.assertEqual(raised.exception.code, expected)

    def test_schema_rejects_unknown_fields_and_non_strings(self):
        base = json.loads(self.output())
        for change in ({'belief': 'model judgment'}, {'decisions': []},
                       {'what_happened': None}, {'task_progress': 12}):
            with self.subTest(change=change), self.assertRaises(StructuredOutputError) as raised:
                self.generate(json.dumps({**base, **change}))
            self.assertEqual(raised.exception.code, 'schema_validation_failed')

    def test_job_attempts_keep_safe_parser_code(self):
        secret = 'PRIVATE-USER-CONTENT'
        for attempt in (1, 3):
            row = (1718, 'rolling_summary', 'u', 'c', secret, None, '{}', attempt, 'segment', None)
            with patch('rolling_summary.process_summary_job',
                       side_effect=StructuredOutputError('multiple_distinct_json_objects')), patch.object(
                       memory_jobs, '_set_status') as status, patch('builtins.print') as logged:
                memory_jobs._run_job(row)
            status.assert_called_once_with(
                1718, 'failed' if attempt == 3 else 'pending', 'multiple_distinct_json_objects')
            output = ' '.join(str(call) for call in logged.call_args_list)
            self.assertIn('kind=rolling_summary', output)
            self.assertIn('#1718', output)
            self.assertIn('multiple_distinct_json_objects', output)
            self.assertNotIn(secret, output)
            if attempt == 3:
                self.assertIn('after 3 attempts error=multiple_distinct_json_objects', output)

    def test_provider_exception_text_is_not_logged_or_stored(self):
        secret = 'PRIVATE-PROVIDER-RESPONSE'
        with patch('rolling_summary.process_summary_job', side_effect=RuntimeError(secret)), patch.object(
                memory_jobs, '_set_status') as status, patch('builtins.print') as logged:
            memory_jobs._run_job((1718, 'rolling_summary', 'u', 'c', '', '', '{}', 3))
        status.assert_called_once_with(1718, 'failed', 'job_exception')
        self.assertNotIn(secret, str(logged.call_args_list))

    def test_invalid_summary_never_reaches_projection(self):
        with patch('context_layer.list_rolling_summaries', return_value=[]), patch(
                'context_layer.save_rolling_summary') as save, patch(
                'ai_client.create_chat', return_value=(self.output() + self.output('另一份'), {})):
            with self.assertRaisesRegex(StructuredOutputError, 'multiple_distinct_json_objects'):
                rolling_summary.process_summary_job('u', 'c', {
                    'source_event_ids': ['e1'], 'events': [{'event_id': 'e1', 'content': 'source'}]})
        save.assert_not_called()

    def test_finish_reason_takes_precedence_without_accepting_partial_json(self):
        secret = 'PRIVATE-SUMMARY-TEXT'
        valid = self.output(secret)
        for raw, usage, expected in (
                (valid[:-1], {'stop_reason': 'end_turn'}, 'incomplete_json'),
                (valid[:-1], {'finish_reason': 'stop'}, 'incomplete_json'),
                (valid[:-1], {'finish_reason': 'length'}, 'truncated_response'),
                (valid, {'stop_reason': 'max_tokens'}, 'truncated_response'),
                (valid, {'finish_reason': 'length'}, 'truncated_response')):
            with self.subTest(usage=usage), patch('ai_client.create_chat', return_value=(
                    raw, dict(usage, output_tokens=700, response_id='test-response'))) as model, patch(
                    'builtins.print') as logged:
                with self.assertRaises(StructuredOutputError) as raised:
                    rolling_summary.generate_real_summary_text(
                        [{'role': 'user', 'content': 'PRIVATE-SOURCE'}], attempt=2)
            self.assertEqual(raised.exception.code, expected)
            model.assert_called_once()
            self.assertEqual(model.call_args.kwargs['max_retries'], 0)
            log = ' '.join(str(call) for call in logged.call_args_list)
            for field in ('domain=rolling_summary', 'attempt=2', f'error={expected}',
                          'stop_reason=', 'output_tokens=700', 'chars=', 'candidate_count='):
                self.assertIn(field, log)
            self.assertNotIn(secret, log)
            self.assertNotIn('PRIVATE-SOURCE', log)

    def test_anthropic_transport_retries_are_disabled_for_summary(self):
        client = Mock()
        client.with_options.return_value.messages.create.return_value = SimpleNamespace(
            content=[{'type': 'text', 'text': self.output()}],
            usage=SimpleNamespace(input_tokens=50, output_tokens=40),
            stop_reason='end_turn', id='test-response')
        with patch('ai_client._get_anthropic', return_value=client), patch(
                'config.MODEL_CN_AUX', 'claude-test'):
            text = rolling_summary.generate_real_summary_text([{'content': '合成来源'}])
        self.assertIn('发生：完成讨论', text)
        client.with_options.assert_called_once_with(max_retries=0)
        client.messages.create.assert_not_called()
        client.with_options.return_value.messages.create.assert_called_once()
        kwargs = client.with_options.return_value.messages.create.call_args.kwargs
        self.assertEqual(kwargs['max_tokens'], 1200)
        self.assertIn('每字段只写简短要点', kwargs['messages'][0]['content'])
        self.assertIn('六字段正文合计最多300字', kwargs['messages'][0]['content'])


class RollingSummaryRetryTests(unittest.TestCase):
    """Exercise the real queue claim/status SQL; never start a worker thread."""

    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()
        cls.database.query('''CREATE TABLE memory_jobs (
            id SERIAL PRIMARY KEY, kind TEXT, user_id TEXT, character_id TEXT,
            user_text TEXT, assistant_text TEXT, extra_json TEXT,
            attempts INTEGER DEFAULT 0, source_event_id TEXT, assistant_event_id TEXT,
            status TEXT DEFAULT 'pending', last_error TEXT, updated_at TIMESTAMP)''')
        cls.database.query('''CREATE TABLE chat_log (
            event_id TEXT, role TEXT, text TEXT, status TEXT)''')
        cls.database.query("INSERT INTO chat_log VALUES ('e1','user','PRIVATE-CANONICAL','active')")

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def test_three_attempts_fail_closed_and_preserve_canonical_events(self):
        valid = RollingSummaryStructuredTests().output('PRIVATE-MODEL-TEXT')
        cases = (
            (valid[:-1], {'stop_reason': 'end_turn'}, 'incomplete_json'),
            (valid[:-1], {'finish_reason': 'length'}, 'truncated_response'),
            (valid, {'stop_reason': 'max_tokens'}, 'truncated_response'),
            ('{"what_happened":"PRIVATE-MODEL-TEXT"}', {}, 'schema_validation_failed'),
            (valid + '{"other":"PRIVATE-MODEL-TEXT"}', {}, 'multiple_distinct_json_objects'),
            ('{"what_happened": bad}', {}, 'invalid_json'),
        )
        canonical_before = self.database.query('SELECT * FROM chat_log')['rows']
        for raw, usage, code in cases:
            with self.subTest(code=code):
                self.database.query('DELETE FROM memory_jobs')
                extra = dict(source_event_ids=['e1'], events=[
                    dict(event_id='e1', role='user', content='PRIVATE-CANONICAL')])
                job_id = self.database.query('''INSERT INTO memory_jobs (
                    kind,user_id,character_id,extra_json,source_event_id)
                    VALUES ('rolling_summary','u','c',$1,$2) RETURNING id''',
                    (json.dumps(extra), rolling_summary.segment_hash(['e1'])))['rows'][0]['id']
                with patch.object(memory_jobs, 'get_conn', return_value=self.database), patch(
                        'context_layer.list_rolling_summaries', return_value=[]), patch(
                        'context_layer.save_rolling_summary') as save, patch(
                        'context_layer.invalidate_summary') as invalidate, patch.object(
                        rolling_summary, '_enqueue_episode_index') as episode, patch(
                        'ai_client.create_chat', return_value=(raw, usage)) as model, patch(
                        'builtins.print') as logged:
                    for attempt in range(1, 4):
                        row = memory_jobs._claim_one()
                        self.assertEqual(row[0], job_id)
                        self.assertEqual(row[7], attempt)
                        memory_jobs._run_job(row)
                        self.assertEqual(model.call_count, attempt)
                    self.assertIsNone(memory_jobs._claim_one())
                saved = self.database.query(
                    'SELECT status,attempts,last_error FROM memory_jobs')['rows'][0]
                self.assertEqual(saved, dict(status='failed', attempts=3, last_error=code))
                save.assert_not_called()
                invalidate.assert_not_called()
                episode.assert_not_called()
                self.assertEqual(self.database.query('SELECT * FROM chat_log')['rows'], canonical_before)
                log = str(logged.call_args_list)
                self.assertIn(f'after 3 attempts error={code}', log)
                self.assertNotIn('PRIVATE-CANONICAL', log)
                self.assertNotIn('PRIVATE-MODEL-TEXT', log)
