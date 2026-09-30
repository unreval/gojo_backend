"""Structured failures must precede every memory/domain mutation."""
import json
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from tests import test_private_memory_fact_gate as private_tests
from tests.canonical_memory_fixture import CanonicalMemoryFixture
import user_memory
from structured_output import StructuredOutputError


class MemoryStructuredOutputTests(CanonicalMemoryFixture, unittest.TestCase):
    def test_malformed_member_blocks_other_valid_memory_fields(self):
        for bad_field in ({'bond': []}, {'bond_delta': {'novel': 'yes'}},
                          {'told': {'content': 42}},
                          {'bond_merge': {'replaces': [17]}}):
            with self.subTest(bad_field=bad_field):
                payload = private_tests.empty_payload(user_fact={
                    'content': '她喜欢寿司', 'category': '喜好',
                    'evidence_quote': '我喜欢寿司',
                })
                payload.update(bad_field)
                # Extractor output is not parsed or granted authority. The
                # actual source still enters the deterministic pending queue.
                self.assertTrue(self.ingest_sources(
                    [{'event_id': 'u1', 'role': 'user', 'content': '我喜欢寿司'}],
                    copied_assistant='记住了。', model_payload=payload))
                self.assert_no_authority()
                self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 1)
                for guard in self.model_guards:
                    guard.assert_not_called()

    def test_correction_scan_structured_failure_aborts_extraction(self):
        with patch.object(user_memory, 'plan_memory_corrections',
                side_effect=StructuredOutputError('memory_correction_invalid_json')) as old_scan:
            self.assertTrue(self.ingest_sources(
                [{'event_id': 'u1', 'role': 'user', 'content': '其实我喜欢寿司'}],
                copied_assistant='记住了。', model_payload=private_tests.empty_payload(
                    user_fact={'content': '她喜欢寿司', 'category': '喜好'})))
        old_scan.assert_not_called()
        self.assert_no_authority()
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 1)

    def test_correction_scan_does_not_silently_convert_failure_to_none(self):
        """The removed model correction scanner cannot delete any memory."""
        for raw in ('broken', '{"action":"delete","ids":[7]}'):
            with self.subTest(raw=raw), patch('ai_client.create_chat',return_value=(raw,None)) as model:
                self.assertEqual(user_memory.plan_memory_corrections('u','其实我喜欢寿司','gojo'),[])
                model.assert_not_called()

    def test_group_failure_does_not_reactivate_delete_or_save(self):
        for raw in ('broken', json.dumps({
                'user_fact': {'content': '她喜欢寿司', 'category': '喜好'},
                'told': None, 'char_bonds': 'invalid'})):
            with self.subTest(raw=raw), ExitStack() as stack:
                for name, value in (('plan_memory_corrections', [(7, 'old')]),
                                    ('get_long_memory', []), ('_all_character_names', [])):
                    stack.enter_context(patch.object(user_memory, name, return_value=value))
                stack.enter_context(patch('ai_client.create_chat', return_value=(raw, None)))
                writes = [stack.enter_context(patch.object(user_memory, name))
                          for name in ('apply_memory_corrections', 'save_long_memory',
                                       'save_bond_memory')]
                writes.append(stack.enter_context(patch(
                    'memory_lifecycle.reactivate_lifecycle_memories')))
                writes.append(stack.enter_context(patch(
                    'memory_lifecycle.apply_user_fact_lifecycle')))
                self.assertFalse(user_memory.extract_and_save_group_memory(
                    'u', '我喜欢寿司', '聊天', [{'id': 'gojo', 'name': '五条'}]))
                for writer in writes:
                    writer.assert_not_called()


if __name__ == '__main__':
    unittest.main()
