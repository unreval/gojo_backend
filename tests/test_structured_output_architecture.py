# -*- coding: utf-8 -*-
"""Guard the three structured LLM boundaries against parser drift."""
import ast
import inspect
import os
import sys
import textwrap
import unittest


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cognitive_output
import cognitive_worker
import relationship_signals
import user_memory
import ai_client


def _direct_json_parser_calls(function):
    tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == 'json'
                and func.attr in {'loads', 'load', 'JSONDecoder'}):
            calls.append(func.attr)
        if isinstance(func, ast.Attribute) and func.attr == 'raw_decode':
            calls.append(func.attr)
    return calls


class StructuredOutputArchitectureTests(unittest.TestCase):
    def test_direct_decoders_are_limited_to_persisted_data_helpers(self):
        # Whole-module scan prevents moving a response parser into a helper to
        # bypass the entry-point checks. These exceptions decode stored data.
        allowed = {
            user_memory: {'parse_event_meta', '_source_ids_from_refs'},
            cognitive_output: {'_load_event_metadata', '_json_meta', '_json_refs'},
            cognitive_worker: set(),
            relationship_signals: set(),
            ai_client: set(),
        }
        for module, storage_helpers in allowed.items():
            tree = ast.parse(inspect.getsource(module))
            json_aliases = {'json'}
            decoder_aliases = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    json_aliases.update(alias.asname or alias.name for alias in node.names
                                        if alias.name == 'json')
                elif isinstance(node, ast.ImportFrom) and node.module == 'json':
                    decoder_aliases.update(alias.asname or alias.name for alias in node.names
                                           if alias.name in {'loads', 'load', 'JSONDecoder'})

            def check(node, owner=None):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    owner = node.name
                if isinstance(node, ast.Call):
                    func = node.func
                    decoder = (isinstance(func, ast.Name) and func.id in decoder_aliases)
                    if isinstance(func, ast.Attribute):
                        decoder = decoder or func.attr == 'raw_decode' or (
                            isinstance(func.value, ast.Name)
                            and func.value.id in json_aliases
                            and func.attr in {'loads', 'load', 'JSONDecoder'})
                    if decoder:
                        self.assertIn(owner, storage_helpers,
                                      f'{module.__name__}:{node.lineno} bypasses canonical parser')
                    # Even storage helpers must never decode response.text.
                    if decoder:
                        self.assertFalse(any(isinstance(arg, ast.Attribute)
                                             and arg.attr == 'text'
                                             for arg in ast.walk(node)),
                                         f'{module.__name__}:{node.lineno} decodes response.text')
                for child in ast.iter_child_nodes(node):
                    check(child, owner)

            check(tree)

    def test_memory_extractor_uses_the_shared_wrapper_not_utils_extract_json(self):
        source = inspect.getsource(user_memory.extract_and_save_memory)
        self.assertIn('ingest_canonical_turn', source)
        self.assertNotIn('invoke_structured_llm', source)
        self.assertNotIn('extract_json(', source)
        self.assertEqual(_direct_json_parser_calls(user_memory.extract_and_save_memory), [])

    def test_relationship_observer_uses_the_shared_wrapper_not_a_local_parser(self):
        source = inspect.getsource(relationship_signals.extract_signals)
        module_source = inspect.getsource(relationship_signals)
        self.assertIn('invoke_structured_llm', source)
        self.assertNotIn('json.JSONDecoder', module_source)
        self.assertNotIn('def _extract_relationship_envelope', module_source)
        self.assertEqual(_direct_json_parser_calls(relationship_signals.extract_signals), [])

    def test_slow_loop_uses_the_shared_parser_and_wrapper(self):
        parser_source = inspect.getsource(cognitive_output.parse_slow_loop_output)
        worker_source = inspect.getsource(cognitive_worker.generate_cycle_output)
        self.assertIn('parse_structured_output', parser_source)
        self.assertIn('deterministic_cycle_output', worker_source)
        self.assertNotIn('invoke_structured_llm', worker_source)
        self.assertNotIn('json.JSONDecoder', parser_source)
        self.assertEqual(_direct_json_parser_calls(cognitive_output.parse_slow_loop_output), [])
        self.assertEqual(_direct_json_parser_calls(cognitive_worker.generate_cycle_output), [])


if __name__ == '__main__':
    unittest.main()
