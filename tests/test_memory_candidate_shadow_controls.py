"""Worker isolation, observable failures, and offline shadow log accounting."""
import ast
import importlib.util
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))
import memory_candidate_shadow as shadow


class ShadowControlTests(unittest.TestCase):
    def test_shadow_start_failure_is_reported_without_blocking_existing_worker(self):
        import memory_jobs
        thread = Mock()
        thread.is_alive.return_value = True
        output = io.StringIO()
        with patch.object(memory_jobs, '_THREAD', thread), \
                patch.object(shadow, 'start_candidate_shadow_worker', side_effect=RuntimeError('offline')), \
                patch('sys.stdout', output):
            memory_jobs.start_memory_worker()
        failure = json.loads(output.getvalue())
        self.assertEqual(failure['kind'], 'worker_failure')
        self.assertEqual(failure['stage'], 'startup')
        self.assertEqual(failure['status'], 'failed')

    def test_disabled_worker_does_not_start_thread_or_access_database(self):
        with patch.dict('os.environ', {'MEMORY_CANDIDATE_SHADOW_ENABLED': 'false'}), \
                patch.object(shadow.threading, 'Thread') as thread, \
                patch('db.get_conn') as connect:
            self.assertIsNone(shadow.start_candidate_shadow_worker())
        thread.assert_not_called()
        connect.assert_not_called()

    def test_enabled_start_is_idempotent_and_shutdown_is_explicit(self):
        thread = Mock()
        thread.is_alive.return_value = True
        with patch.dict('os.environ', {'MEMORY_CANDIDATE_SHADOW_ENABLED': 'true'}), \
                patch.object(shadow, '_THREAD', None), \
                patch.object(shadow, '_STOP', Mock()), \
                patch.object(shadow.threading, 'Thread', return_value=thread) as factory:
            self.assertIs(shadow.start_candidate_shadow_worker(), thread)
            self.assertIs(shadow.start_candidate_shadow_worker(), thread)
            factory.assert_called_once()
            thread.start.assert_called_once()
            shadow.stop_candidate_shadow_worker(timeout=0)
            self.assertIs(shadow._THREAD, thread)  # Cannot start a second live worker.
            thread.is_alive.return_value = False
            shadow.stop_candidate_shadow_worker(timeout=0)
            self.assertIsNone(shadow._THREAD)

    def test_one_user_failure_does_not_stop_other_users_or_enter_chat_pipeline(self):
        database = Mock()
        database.cursor.return_value.fetchall.return_value = [('u1', 'c'), ('u2', 'c')]
        with patch('db.get_conn', return_value=database), \
                patch.object(shadow, '_STOP', Mock(is_set=Mock(return_value=False))), \
                patch.object(shadow, 'run_pair', side_effect=[RuntimeError('offline'),
                    {'status': 'succeeded'}]) as run, patch.object(shadow, '_emit') as emit:
            results = shadow.run_worker_once()
        self.assertEqual(run.call_count, 2)
        self.assertEqual(results, [{'status': 'succeeded'}])
        self.assertEqual(emit.call_args.args[0]['status'], 'failed')
        source = (ROOT / 'gojo_backend/memory_candidate_shadow.py').read_text(encoding='utf-8')
        tree = ast.parse(source)
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        self.assertFalse(imports & {'route_chat', 'user_memory', 'cognitive_revision', 'memory_jobs'})
        calls = {node.func.id for node in ast.walk(tree)
                 if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.assertFalse(calls & {'apply_candidate', 'project_canonical_memory'})

    def test_short_circuit_results_do_not_invent_later_gate_successes(self):
        gates = shadow.gate_results(False, 'quote_not_verbatim', {'summary': '她喜欢咖啡'})
        self.assertEqual(gates['G1']['status'], 'passed')
        self.assertEqual(gates['G2']['status'], 'rejected')
        self.assertTrue(all(gates[key]['status'] == 'not_evaluated'
                            for key in ('G3', 'G4', 'G5', 'G6', 'G7', 'G8', 'G9')))
        gates = shadow.gate_results(False, 'invalid_predicate_or_summary', {'summary': None})
        self.assertEqual(gates['G4']['status'], 'not_evaluated')
        self.assertEqual(gates['G7']['status'], 'rejected')
        with self.assertRaisesRegex(ValueError, 'unmapped_candidate_gate_reason'):
            shadow.gate_results(False, 'future_unknown_gate', {})

    def test_jsonl_is_parseable_and_human_labels_are_needed_for_accuracy(self):
        spec = importlib.util.spec_from_file_location('candidate_shadow_analysis',
            ROOT / 'scripts/analyze_candidate_shadow.py')
        analysis = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(analysis)
        common = dict(user_id='u', character_id='c', module_version=shadow.MODULE_VERSION,
                      batch_id='b', batch_sequence=1)
        candidate = dict(common, kind='candidate', record_id='r', source_event_id='s1',
                         source_in_batch=True,
                         accepted=True, perspective_output_empty=True, perspective_empty=True,
                         gates=shadow.gate_results(True, None, {'summary': '她喜欢咖啡'}))
        batch = dict(common, kind='batch', status='succeeded', candidate_count=1,
                     input_source_event_ids=['s1', 's2'])
        out = io.StringIO()
        with patch('sys.stdout', out):
            shadow._emit(candidate)
            shadow._emit(candidate)  # Simulate overlapping rotated log exports.
            shadow._emit(batch)
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        result = analysis.analyze(rows)
        self.assertEqual(result['candidate_records'], 1)
        self.assertEqual(result['successful_input_messages'], 2)
        self.assertIsNone(result['human_metrics'])
        self.assertEqual(result['candidate_log_row_difference'], 0)
        labels = [dict(common, source_event_id='s1', should_remember=True, summary_correct=True),
                  dict(common, source_event_id='s2', should_remember=True)]
        metrics = analysis.analyze(rows, labels)['human_metrics']
        self.assertEqual(metrics['candidate_recall'], 0.5)
        self.assertEqual(metrics['summary_precision'], 1.0)

    def test_hallucinated_future_source_cannot_hide_its_later_valid_record(self):
        candidate = dict(source_event_id='future', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact')
        with patch.object(shadow, 'validate_candidate', return_value=(True, None)):
            bad = shadow._validate_batch(Mock(), 'u', 'c', [candidate],
                [{'source_event_id': 'current'}], {'batch_id': 'first'})[0]
            good = shadow._validate_batch(Mock(), 'u', 'c', [candidate],
                [{'source_event_id': 'future'}], {'batch_id': 'second'})[0]
        self.assertEqual(bad['reason'], 'source_not_in_batch')
        self.assertFalse(bad['source_in_batch'])
        self.assertTrue(good['accepted'])
        self.assertTrue(good['source_in_batch'])
        self.assertNotEqual(bad['record_id'], good['record_id'])
        spec = importlib.util.spec_from_file_location('candidate_shadow_analysis',
            ROOT / 'scripts/analyze_candidate_shadow.py')
        analysis = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(analysis)
        common = dict(log_type='memory_candidate_shadow', user_id='u', character_id='c',
                      module_version=shadow.MODULE_VERSION)
        rows = [dict(common, **bad), dict(common, **good),
                dict(common, kind='batch', batch_id='first', status='succeeded',
                     candidate_count=1, input_source_event_ids=['current']),
                dict(common, kind='batch', batch_id='second', status='succeeded',
                     candidate_count=1, input_source_event_ids=['future'])]
        result = analysis.analyze(rows, [dict(common, source_event_id='future',
                                             should_remember=True, summary_correct=True)])
        self.assertEqual(result['candidate_records'], 2)
        self.assertEqual(result['human_metrics']['passed_recall'], 1.0)
        self.assertEqual(result['human_metrics']['summary_precision'], 1.0)


if __name__ == '__main__':
    unittest.main()
