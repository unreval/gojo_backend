# -*- coding: utf-8 -*-
import os
import sys
import unittest
from unittest.mock import Mock


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import structured_output


class StructuredOutputParserTests(unittest.TestCase):
    def test_accepts_raw_fenced_and_prose_wrapped_single_object(self):
        raw = '{"signals":[]}'
        fenced = '```json\n{"signals":[]}\n```'
        prose = 'Here is the result: {"signals":[]}. Nothing else.'
        escaped = 'Result: {"text":"a brace: { and a quote: \\\""}'

        results = [
            structured_output.parse_structured_output(value)
            for value in (raw, fenced, prose, escaped)
        ]

        self.assertTrue(all(result.ok for result in results))
        self.assertEqual(results[0].extraction_mode, 'raw')
        self.assertEqual(results[1].extraction_mode, 'markdown_fence')
        self.assertEqual(results[2].extraction_mode, 'balanced_object')
        self.assertEqual(results[3].value['text'], 'a brace: { and a quote: "')

    def test_distinct_valid_objects_fail_closed_but_exact_duplicates_do_not(self):
        duplicate = structured_output.parse_structured_output(
            '{"signals":[]}\n{\n  "signals": []\n}')
        ambiguous = structured_output.parse_structured_output(
            '{"signals":[]}\n{"signals":[{"signal_type":"care"}]}')

        self.assertTrue(duplicate.ok)
        self.assertEqual(duplicate.candidate_count, 2)
        self.assertEqual(duplicate.distinct_candidate_count, 1)
        self.assertFalse(ambiguous.ok)
        self.assertEqual(ambiguous.error_code, 'multiple_distinct_json_objects')
        self.assertEqual(ambiguous.distinct_candidate_count, 2)

    def test_nested_values_and_json_inside_strings_are_not_outer_objects(self):
        array_result = structured_output.parse_structured_output('[{"signals":[]}]')
        string_result = structured_output.parse_structured_output('"{\\"signals\\":[]}"')
        nested_result = structured_output.parse_structured_output(
            '{"metadata":{"signals":[]}}')

        self.assertEqual(array_result.error_code, 'root_not_object')
        self.assertEqual(string_result.error_code, 'no_top_level_json_object')
        self.assertTrue(nested_result.ok)
        self.assertEqual(nested_result.value, {'metadata': {'signals': []}})

    def test_invalid_and_incomplete_json_are_classified_without_repair(self):
        invalid = structured_output.parse_structured_output('{"signals": nope}')
        incomplete = structured_output.parse_structured_output('{"signals": [')

        self.assertEqual(invalid.error_code, 'invalid_json')
        self.assertEqual(incomplete.error_code, 'incomplete_json')

    def test_rejects_duplicate_keys_constants_and_mixed_outer_roots(self):
        for raw in ('{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}',
                    '{"x":1e400}', '{"signals":[]}}', '{"signals":[]} ]'):
            with self.subTest(raw=raw):
                self.assertEqual(structured_output.parse_structured_output(raw).error_code,
                                 'invalid_json')
        result = structured_output.parse_structured_output('{"signals":[]} []')
        self.assertEqual(result.error_code, 'multiple_distinct_json_objects')

    def test_does_not_salvage_valid_object_from_invalid_or_incomplete_envelope(self):
        for raw in ('{"x": {"signals":[]}, broken}',
                    '{"signals":[]} {"unfinished":',
                    '[{"signals":[]}]'):
            with self.subTest(raw=raw):
                self.assertFalse(structured_output.parse_structured_output(raw).ok)

    def test_wrapper_rejects_refusal_or_truncation_before_schema_validation(self):
        for stop, expected in [('refusal', 'model_refused'),
                               ('content_filter', 'model_refused'),
                               ('length', 'truncated_response'),
                               ('max_tokens', 'truncated_response')]:
            with self.subTest(stop=stop):
                validator = Mock()
                call = structured_output.invoke_structured_llm(
                    domain='test', create_chat_fn=lambda **_: ('{"signals":[]}', {
                        'stop_reason': stop}), model='test', messages=[],
                    schema_validator=validator)
                self.assertEqual(call.parsed.error_code, expected)
                self.assertIsNone(call.parsed.value)
                self.assertFalse(call.telemetry['ok'])
                validator.assert_not_called()

    def test_arbitrary_schema_error_text_is_not_logged(self):
        validator = Mock(side_effect=ValueError('private conversation content'))
        result = structured_output.parse_structured_output('{}', schema_validator=validator)
        self.assertEqual(result.error_detail, 'ValueError')
        self.assertNotIn('private conversation content', str(result.telemetry()))

    def test_schema_validator_is_part_of_the_canonical_boundary(self):
        def signals_only(value):
            if not isinstance(value.get('signals'), list):
                error = ValueError('signals_not_array')
                error.code = 'signals_not_array'
                raise error
            return value

        result = structured_output.parse_structured_output(
            '{"signals":null}', schema_validator=signals_only,
            schema_name='relationship_signals')

        self.assertEqual(result.error_code, 'schema_validation_failed')
        self.assertEqual(result.error_detail, 'signals_not_array')
        self.assertEqual(result.schema_name, 'relationship_signals')

    def test_wrapper_attaches_safe_telemetry_without_raw_response(self):
        secret = 'PRIVATE-CONVERSATION-CONTENT-937'

        def call_model(**_kwargs):
            return '{"metadata":"' + secret + '"}', {
                'response_id': 'response-1', 'output_tokens': 12,
            }

        call = structured_output.invoke_structured_llm(
            domain='test', create_chat_fn=call_model, model='test-model',
            messages=[{'role': 'user', 'content': 'ignored'}],
        )
        lines = []
        structured_output.emit_structured_output_telemetry(
            call, attempt=1, logger=lines.append)

        self.assertTrue(call.parsed.ok)
        self.assertEqual(call.usage['structured_output']['response_id'], 'response-1')
        self.assertNotIn(secret, lines[0])


if __name__ == '__main__':
    unittest.main()
