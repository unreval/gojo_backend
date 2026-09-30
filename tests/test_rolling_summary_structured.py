"""Derived summary uses the shared parser and propagates only safe diagnostics."""
import json
import unittest
from unittest.mock import patch

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
