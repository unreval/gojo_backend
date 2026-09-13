import inspect
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cognitive_config
import cognitive_db
import cognitive_events
import cognitive_predictions
import cognitive_queue
import cognitive_reactivation
import cognitive_replay
import cognitive_scheduler
import cognitive_triggers


NOW = datetime(2026, 9, 12, 8, 0, tzinfo=timezone.utc)


class TransactionConnection:
    def __init__(self, cursor=None):
        self._cursor = cursor or Mock()
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


class AggregateCursor:
    def __init__(self):
        self.one = None
        self.many = []
        self.executed = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        self.one = None
        self.many = []
        if compact.startswith('SELECT id, claimed_by_cycle_id, attempt_count'):
            self.many = []
        elif compact.startswith('SELECT id FROM cognitive_cycles'):
            self.one = None
        elif compact.startswith('SELECT COUNT(*) FROM cognitive_cycles'):
            self.one = (0,)
        elif compact.startswith('SELECT completed_at FROM cognitive_cycles'):
            self.one = None
        elif compact.startswith('SELECT id, trigger_class, priority, event_id, created_at'):
            if "trigger_class = 'question_reactivation'" in compact:
                self.many = [
                    (12, 'question_reactivation', 300, 1, NOW),
                ]
            else:
                self.many = [
                    (11, 'prediction_error', 400, 1, NOW),
                    (13, 'high_weight_evidence', 200, 2, NOW),
                ]
        elif compact.startswith('SELECT COALESCE(MAX(output_state_version)'):
            self.one = (4,)
        elif compact.startswith('INSERT INTO cognitive_cycles'):
            self.one = (90,)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class RecoverCursor:
    def __init__(self):
        self.many = []
        self.executed = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        if compact.startswith('SELECT id, claimed_by_cycle_id, attempt_count'):
            self.many = [(1, 50, 1), (2, 50, cognitive_config.COGNITIVE_MAX_RETRY)]

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class CycleCursor:
    def __init__(self, *, success, valid_claims=True):
        self.success = success
        self.valid_claims = valid_claims
        self.one = None
        self.many = []
        self.executed = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        self.one = None
        self.many = []
        if compact.startswith('SELECT user_id, character_id FROM cognitive_cycles'):
            self.one = ('u', 'gojo')
        elif compact.startswith('SELECT status, input_state_version'):
            self.one = ('running', 13)
        elif compact.startswith('SELECT status FROM cognitive_cycles'):
            self.one = ('running',)
        elif compact.startswith('SELECT COUNT(*), COUNT(*) FILTER'):
            self.one = (2, 2 if self.valid_claims else 1)
        elif compact.startswith('SELECT COUNT(*) FROM cognitive_cycles'):
            self.one = (1,)
        elif compact.startswith('SELECT trigger.id, trigger.attempt_count'):
            self.many = [(1, 1), (2, cognitive_config.COGNITIVE_MAX_RETRY)]

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class PredictionCursor:
    def __init__(self, prediction):
        self.prediction = prediction
        self.description = []
        self.rows = []
        self.executed = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        if compact.startswith('SELECT id, user_id, character_id, resolver_name'):
            columns = list(self.prediction)
            self.description = [(name,) for name in columns]
            self.rows = [tuple(self.prediction[name] for name in columns)]
        else:
            self.rows = []

    def fetchall(self):
        return list(self.rows)

    def close(self):
        pass


class SchedulerCursor:
    def __init__(self, recent_success=None):
        self.recent_success = recent_success
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((' '.join(sql.split()), params))

    def fetchone(self):
        return self.recent_success

    def close(self):
        pass


class CognitiveSchemaTests(unittest.TestCase):
    def test_all_seven_tables_are_initialized(self):
        ddl = '\n'.join(cognitive_db.ddl_statements()).lower()
        for table in (
            'cognitive_events',
            'cognitive_event_triggers',
            'cognitive_cycles',
            'cognitive_cycle_trigger_events',
            'cognitive_questions',
            'cognitive_hypotheses',
            'cognitive_predictions',
        ):
            self.assertIn(f'create table if not exists {table}', ddl)

    def test_source_event_identity_is_unique(self):
        ddl = '\n'.join(cognitive_db.ddl_statements())
        self.assertIn(
            'UNIQUE (user_id, character_id, source_event_type, source_event_id)',
            ddl,
        )

    def test_one_active_cycle_partial_index(self):
        ddl = ' '.join('\n'.join(cognitive_db.ddl_statements()).split())
        self.assertIn(
            "WHERE status IN ('queued', 'running')",
            ddl,
        )

    def test_database_enforces_cycle_and_trigger_lifecycle_shapes(self):
        ddl = '\n'.join(cognitive_db.ddl_statements())
        self.assertIn('ck_cognitive_cycle_output_version', ddl)
        self.assertIn('ck_cognitive_trigger_claim_fields', ddl)
        self.assertIn('ck_cognitive_trigger_consumed_fields', ddl)

    def test_claim_and_consumption_fields_are_separate(self):
        ddl = '\n'.join(cognitive_db.ddl_statements())
        self.assertIn('claimed_by_cycle_id', ddl)
        self.assertIn('consumed_cycle_id', ddl)
        self.assertIn("'pending', 'claimed', 'consumed'", ddl)

    def test_embeddings_are_text_and_no_pgvector_is_used(self):
        ddl = '\n'.join(cognitive_db.ddl_statements()).lower()
        self.assertIn('embedding_json text', ddl)
        self.assertNotIn('vector(', ddl)
        self.assertNotIn('pgvector', ddl)


class TriggerIngressTests(unittest.TestCase):
    def test_priority_order(self):
        priorities = cognitive_config.TRIGGER_PRIORITIES
        self.assertGreater(priorities['prediction_error'], priorities['question_reactivation'])
        self.assertGreater(priorities['question_reactivation'], priorities['high_weight_evidence'])
        self.assertGreater(priorities['high_weight_evidence'], priorities['scheduled_reflection'])

    def test_same_event_can_emit_multiple_v4_signal_occurrences(self):
        signals = [
            {'signal_type': 'small_care', 'actor': 'user', 'confidence': 'high',
             'brief': 'a', 'attributes': {}},
            {'signal_type': 'promise_kept', 'actor': 'user', 'confidence': 'high',
             'brief': 'b', 'attributes': {}},
        ]
        specs = cognitive_triggers.high_weight_trigger_specs(signals, threshold=0.8)
        self.assertEqual(len(specs), 2)
        self.assertEqual({item['occurrence_key'] for item in specs}, {'signal:0', 'signal:1'})

    def test_v4_signal_object_and_confidence_weight_are_reused(self):
        signal = {
            'signal_type': 'genuine_care',
            'actor': 'user',
            'confidence': 'medium',
            'brief': 'remembered a detail',
            'attributes': {'topic': 'work'},
        }
        specs = cognitive_triggers.high_weight_trigger_specs([signal], threshold=0.6)
        self.assertIs(specs[0]['payload']['signal'], signal)
        self.assertEqual(
            specs[0]['payload']['confidence_weight'],
            cognitive_replay.CONFIDENCE_MULTIPLIER['medium'],
        )

    def test_duplicate_source_event_skips_all_side_effects(self):
        connection = TransactionConnection()
        with patch.object(cognitive_events, 'record_source_event', return_value=None), \
             patch.object(cognitive_events, '_embed_v4_evidence') as embed, \
             patch.object(cognitive_events, 'settle_pending_predictions') as settle, \
             patch.object(cognitive_events, 'reactivate_dormant_questions') as reactivate, \
             patch.object(cognitive_events, 'create_trigger_occurrence') as create_trigger:
            result = cognitive_events.ingest_v4_signals(
                user_id='u', character_id='gojo', source_event_id='same',
                signals=[{'brief': 'fact'}], conn=connection,
            )
        self.assertEqual(result['status'], 'duplicate')
        settle.assert_not_called()
        reactivate.assert_not_called()
        create_trigger.assert_not_called()
        embed.assert_not_called()

    def test_fast_maintenance_completes_before_slow_limit_result(self):
        connection = TransactionConnection()
        call_order = []
        with patch.object(cognitive_events, 'record_source_event', return_value=7), \
             patch.object(cognitive_events, 'high_weight_trigger_specs', return_value=[]), \
             patch.object(cognitive_events, 'settle_pending_predictions',
                          side_effect=lambda *a, **k: call_order.append('settle') or []), \
             patch.object(cognitive_events, 'reactivate_dormant_questions',
                          side_effect=lambda *a, **k: call_order.append('reactivate') or []), \
             patch.object(cognitive_queue, 'aggregate_pending_triggers',
                          side_effect=lambda *a, **k: call_order.append('limit') or
                          {'status': 'daily_limit'}):
            result = cognitive_events.ingest_v4_signals(
                user_id='u', character_id='gojo', source_event_id='event-7',
                signals=[], conn=connection,
            )
        self.assertEqual(call_order, ['settle', 'reactivate', 'limit'])
        self.assertEqual(result['cycle']['status'], 'daily_limit')
        self.assertGreaterEqual(connection.commits, 1)

    def test_cooldown_also_runs_after_fast_maintenance(self):
        connection = TransactionConnection()
        call_order = []
        with patch.object(cognitive_events, 'record_source_event', return_value=8), \
             patch.object(cognitive_events, 'high_weight_trigger_specs', return_value=[]), \
             patch.object(cognitive_events, 'settle_pending_predictions',
                          side_effect=lambda *a, **k: call_order.append('settle') or []), \
             patch.object(cognitive_events, 'reactivate_dormant_questions',
                          side_effect=lambda *a, **k: call_order.append('reactivate') or []), \
             patch.object(cognitive_queue, 'aggregate_pending_triggers',
                          side_effect=lambda *a, **k: call_order.append('cooldown') or
                          {'status': 'cooldown'}):
            result = cognitive_events.ingest_v4_signals(
                user_id='u', character_id='gojo', source_event_id='event-8',
                signals=[], conn=connection,
            )
        self.assertEqual(call_order, ['settle', 'reactivate', 'cooldown'])
        self.assertEqual(result['cycle']['status'], 'cooldown')


class PredictionTests(unittest.TestCase):
    def _prediction(self, **overrides):
        prediction = {
            'resolver_name': 'messages_since_prediction_created',
            'fulfillment_operator': '>=',
            'fulfillment_value': 3,
            'violation_operator': '<',
            'violation_value': 1,
            'expires_at': NOW + timedelta(days=1),
        }
        prediction.update(overrides)
        return prediction

    def test_static_resolver_whitelist_is_exact(self):
        self.assertEqual(
            set(cognitive_predictions.registered_resolvers()),
            set(cognitive_config.PREDICTION_RESOLVER_WHITELIST),
        )

    def test_dynamic_resolvers_and_arbitrary_fields_are_rejected(self):
        for name in ('relationship_state.warmth', 'relationship_model', 'os.system'):
            with self.assertRaises(ValueError):
                cognitive_predictions.validate_prediction_rule(name, '>=', 1)

    def test_prediction_fulfilled(self):
        result = cognitive_predictions.evaluate_prediction(
            self._prediction(), {'messages_since_prediction_created': 3}, now=NOW,
        )
        self.assertEqual(result['status'], 'fulfilled')

    def test_prediction_violated(self):
        result = cognitive_predictions.evaluate_prediction(
            self._prediction(), {'messages_since_prediction_created': 0}, now=NOW,
        )
        self.assertEqual(result['status'], 'violated')

    def test_prediction_expired_without_interpretation(self):
        result = cognitive_predictions.evaluate_prediction(
            self._prediction(expires_at=NOW - timedelta(seconds=1)),
            {'messages_since_prediction_created': 99},
            now=NOW,
        )
        self.assertEqual(result, {'status': 'expired', 'observed_value': None})

    def test_violated_settlement_creates_only_prediction_error_trigger(self):
        prediction = {
            'id': 8,
            'user_id': 'u',
            'character_id': 'gojo',
            'resolver_name': 'messages_since_prediction_created',
            'fulfillment_operator': '>=',
            'fulfillment_value': 3,
            'violation_operator': '<',
            'violation_value': 1,
            'expires_at': NOW + timedelta(days=1),
            'created_at': NOW - timedelta(hours=1),
        }
        cursor = PredictionCursor(prediction)
        connection = TransactionConnection(cursor)
        with patch.dict(
            cognitive_predictions._RESOLVERS,
            {'messages_since_prediction_created': lambda *args: 0},
            clear=True,
        ), patch.object(cognitive_predictions, 'create_trigger_occurrence') as create:
            settled = cognitive_predictions.settle_pending_predictions(
                connection,
                user_id='u', character_id='gojo', event_id=22, occurred_at=NOW,
            )
        self.assertEqual(settled[0]['status'], 'violated')
        self.assertEqual(create.call_args.kwargs['trigger_class'], 'prediction_error')

    def test_no_eval_exec_or_dynamic_import_is_used(self):
        source = inspect.getsource(cognitive_predictions)
        self.assertNotIn('eval(', source)
        self.assertNotIn('exec(', source)
        self.assertNotIn('importlib', source)


class ReactivationTests(unittest.TestCase):
    def test_cosine_top_n_and_descending_rank(self):
        questions = [
            {'id': 1, 'status': 'dormant', 'embedding_json': '[1, 0]',
             'question_key': 'a', 'question_text': 'A'},
            {'id': 2, 'status': 'dormant', 'embedding_json': '[0.8, 0.2]',
             'question_key': 'b', 'question_text': 'B'},
            {'id': 3, 'status': 'dormant', 'embedding_json': '[0, 1]',
             'question_key': 'c', 'question_text': 'C'},
        ]
        ranked = cognitive_reactivation.rank_dormant_questions(
            [1, 0], questions, threshold=0.5, top_n=2,
        )
        self.assertEqual([item['question_id'] for item in ranked], [1, 2])
        self.assertGreaterEqual(ranked[0]['similarity'], ranked[1]['similarity'])

    def test_relationship_numbers_do_not_affect_ranking(self):
        base = [
            {'id': 1, 'status': 'dormant', 'embedding_json': [1, 0]},
            {'id': 2, 'status': 'dormant', 'embedding_json': [0, 1]},
        ]
        noisy = [
            {**base[0], 'warmth': 0, 'trust': 0, 'friction': 999},
            {**base[1], 'warmth': 100, 'trust': 100, 'friction': 0},
        ]
        first = cognitive_reactivation.rank_dormant_questions(
            [1, 0], base, threshold=-1, top_n=2,
        )
        second = cognitive_reactivation.rank_dormant_questions(
            [1, 0], noisy, threshold=-1, top_n=2,
        )
        self.assertEqual(
            [item['question_id'] for item in first],
            [item['question_id'] for item in second],
        )

    def test_reactivation_language_is_only_possibly_related(self):
        source = inspect.getsource(cognitive_reactivation)
        self.assertIn("'relation': 'possibly_related'", source)
        self.assertNotIn("SET status = 'resolved'", source)


class QueueLifecycleTests(unittest.TestCase):
    def test_stable_advisory_key(self):
        key = cognitive_queue.stable_advisory_lock_key('u', 'gojo')
        self.assertEqual(key, cognitive_queue.stable_advisory_lock_key('u', 'gojo'))
        self.assertNotEqual(key, cognitive_queue.stable_advisory_lock_key('v', 'gojo'))

    def test_multiple_triggers_aggregate_into_one_cycle_and_keep_secondaries(self):
        cursor = AggregateCursor()
        connection = TransactionConnection(cursor)
        result = cognitive_queue.aggregate_pending_triggers(
            'u', 'gojo', conn=connection, now=NOW,
        )
        self.assertEqual(result['cycle_id'], 90)
        self.assertEqual(result['trigger_ids'], [11, 12, 13])
        self.assertEqual(result['primary_trigger_class'], 'prediction_error')
        joins = [entry for entry in cursor.executed
                 if entry[0].startswith('INSERT INTO cognitive_cycle_trigger_events')]
        self.assertEqual(len(joins), 3)
        self.assertEqual([entry[1][2] for entry in joins], [True, False, False])

    def test_claim_is_not_consume(self):
        source = inspect.getsource(cognitive_queue.aggregate_pending_triggers)
        self.assertIn("SET status = 'claimed'", source)
        self.assertNotIn("SET status = 'consumed'", source)
        self.assertNotIn('consumed_cycle_id = %s', source)

    def test_success_advances_version_and_consumes_in_same_transaction(self):
        cursor = CycleCursor(success=True)
        connection = TransactionConnection(cursor)
        result = cognitive_queue.commit_cycle_success(5, conn=connection, now=NOW)
        self.assertEqual(result['output_state_version'], 14)
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertIn("status = 'succeeded'", sql)
        self.assertIn("status = 'consumed'", sql)
        self.assertIn('consumed_cycle_id = %s', sql)
        self.assertEqual(connection.commits, 1)

    def test_failed_cycle_does_not_advance_and_retries_then_dead_letters(self):
        cursor = CycleCursor(success=False)
        connection = TransactionConnection(cursor)
        result = cognitive_queue.fail_cycle(5, 'future_worker_failed', conn=connection, now=NOW)
        self.assertIsNone(result['output_state_version'])
        self.assertEqual(
            [item['status'] for item in result['triggers']],
            ['pending', 'dead_letter'],
        )
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertIn('output_state_version = NULL', sql)
        self.assertNotIn("status = 'consumed'", sql)

    def test_success_rejects_an_expired_or_missing_claim(self):
        cursor = CycleCursor(success=True, valid_claims=False)
        connection = TransactionConnection(cursor)
        with self.assertRaisesRegex(ValueError, 'missing or expired'):
            cognitive_queue.commit_cycle_success(5, conn=connection, now=NOW)
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)

    def test_lease_expiry_recovers_or_dead_letters_by_attempt_count(self):
        cursor = RecoverCursor()
        connection = TransactionConnection(cursor)
        result = cognitive_queue.recover_expired_claims(
            'u', 'gojo', conn=connection, now=NOW,
        )
        self.assertEqual(result['recovered'], 2)
        updates = [params for sql, params in cursor.executed
                   if sql.startswith('UPDATE cognitive_event_triggers')]
        self.assertEqual([params[0] for params in updates], ['pending', 'dead_letter'])

    def test_only_success_function_sets_non_null_consumed_cycle(self):
        queue_source = inspect.getsource(cognitive_queue)
        success_source = inspect.getsource(cognitive_queue.commit_cycle_success)
        fail_source = inspect.getsource(cognitive_queue.fail_cycle)
        self.assertIn('consumed_cycle_id = %s', success_source)
        self.assertNotIn('consumed_cycle_id = %s', fail_source)
        self.assertIn('FOR UPDATE SKIP LOCKED', queue_source)
        self.assertIn('pg_advisory_xact_lock', queue_source)

    def test_reasoning_context_contains_facts_and_no_response_guidance(self):
        source = inspect.getsource(cognitive_queue.build_reasoning_context)
        for field in (
            "'cycle_id'", "'input_state_version'", "'events'", "'triggers'",
            "'settled_predictions'", "'reactivated_questions'", "'temporal'",
        ):
            self.assertIn(field, source)
        for phrase in ('应该冷淡', '应该生气', '收短回复', '可以暧昧'):
            self.assertNotIn(phrase, source)

    def test_question_trigger_cap_is_applied_during_aggregation(self):
        source = inspect.getsource(cognitive_queue.aggregate_pending_triggers)
        self.assertIn('COGNITIVE_MAX_QUESTIONS_PER_CYCLE', source)


class SchedulerTests(unittest.TestCase):
    def test_scheduler_creates_event_and_normal_trigger_but_no_cycle(self):
        cursor = SchedulerCursor(recent_success=None)
        connection = TransactionConnection(cursor)
        with patch.object(cognitive_scheduler, 'record_source_event', return_value=41), \
             patch.object(cognitive_scheduler, 'create_trigger_occurrence', return_value=51) as create:
            result = cognitive_scheduler.enqueue_scheduled_reflection(
                'u', 'gojo', scheduled_for=NOW, conn=connection,
            )
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(create.call_args.kwargs['trigger_class'], 'scheduled_reflection')
        source = inspect.getsource(cognitive_scheduler.enqueue_scheduled_reflection)
        self.assertNotIn('aggregate_pending_triggers', source)
        self.assertNotIn('INSERT INTO cognitive_cycles', source)
        queue_source = inspect.getsource(cognitive_queue.aggregate_pending_triggers)
        self.assertIn('cognitive_event_triggers', queue_source)

    def test_recent_success_suppresses_scheduled_occurrence(self):
        cursor = SchedulerCursor(recent_success=(9,))
        connection = TransactionConnection(cursor)
        with patch.object(cognitive_scheduler, 'record_source_event', return_value=41), \
             patch.object(cognitive_scheduler, 'create_trigger_occurrence', return_value=51) as create:
            result = cognitive_scheduler.enqueue_scheduled_reflection(
                'u', 'gojo', scheduled_for=NOW, conn=connection,
            )
        self.assertEqual(result['status'], 'suppressed')
        self.assertEqual(create.call_args.kwargs['suppressed_reason'], 'recent_success')

    def test_scheduled_source_id_is_idempotent_per_bucket(self):
        first = cognitive_scheduler.scheduled_source_event_id('u', 'gojo', NOW)
        second = cognitive_scheduler.scheduled_source_event_id(
            'u', 'gojo', NOW + timedelta(minutes=1),
        )
        self.assertEqual(first, second)


class ReplayAndBoundaryTests(unittest.TestCase):
    def test_replay_outputs_all_required_metrics(self):
        records = [
            {
                'source_event_id': '1', 'user_id': 'u', 'character_id': 'gojo',
                'source_event_type': 'relationship_v4_signal',
                'confidence': 'high', 'occurred_at': NOW,
            },
            {
                'source_event_id': '2', 'user_id': 'u', 'character_id': 'gojo',
                'source_event_type': 'scheduled_reflection',
                'occurred_at': NOW + timedelta(hours=1),
            },
        ]
        metrics = cognitive_replay.run_replay(records, cooldown_minutes=5)
        required = {
            'events_per_day_user', 'trigger_occurrences_per_day_user',
            'trigger_occurrences_by_class', 'cycles_per_day_user',
            'trigger_to_cycle_aggregation_ratio', 'events_per_cycle',
            'trigger_to_cycle_delay_seconds', 'daily_limit_hit_rate',
            'scheduled_suppression_rate',
            'top_1_percent_burst_users_cycle_frequency',
            'estimated_future_cognitive_tokens',
        }
        self.assertTrue(required.issubset(metrics))
        self.assertEqual(set(metrics['events_per_cycle']), {'p50', 'p90', 'p95'})
        self.assertEqual(
            set(metrics['estimated_future_cognitive_tokens']),
            {'p50', 'p95', 'p99', 'burst'},
        )

    def test_parameter_sweep_is_exactly_one_hundred(self):
        sweep = cognitive_replay.run_parameter_sweep([])
        self.assertEqual(len(sweep), 100)
        combinations = {
            tuple(sorted(item['parameters'].items())) for item in sweep
        }
        self.assertEqual(len(combinations), 100)

    def test_deterministic_modules_do_not_write_relationship_state_or_call_models(self):
        forbidden_writes = ('UPDATE rel_state', 'INSERT INTO rel_state')
        forbidden_models = ('create_chat(', 'anthropic.', 'openai.')
        for filename in os.listdir(BACKEND):
            if not filename.startswith('cognitive_') or not filename.endswith('.py'):
                continue
            with open(os.path.join(BACKEND, filename), encoding='utf-8') as handle:
                source = handle.read()
            for forbidden in forbidden_writes + forbidden_models:
                if filename == 'cognitive_worker.py' and forbidden in forbidden_models:
                    continue
                self.assertNotIn(forbidden, source, filename)

    def test_only_slow_worker_may_call_a_model(self):
        model_callers = []
        for filename in os.listdir(BACKEND):
            if not filename.startswith('cognitive_') or not filename.endswith('.py'):
                continue
            with open(os.path.join(BACKEND, filename), encoding='utf-8') as handle:
                if 'from ai_client import create_chat' in handle.read():
                    model_callers.append(filename)
        self.assertEqual(model_callers, ['cognitive_worker.py'])

    def test_wrong_deterministic_response_guidance_is_absent(self):
        self.assertFalse(os.path.exists(
            os.path.join(BACKEND, 'relationship_cognitive_loop.py'),
        ))
        forbidden = (
            '_describe_inner', '_describe_intent', '_describe_moodshift',
            '收短回复', '应该冷淡', '可以有轻微暧昧',
        )
        for filename in os.listdir(BACKEND):
            if filename.startswith('cognitive_') and filename.endswith('.py'):
                with open(os.path.join(BACKEND, filename), encoding='utf-8') as handle:
                    source = handle.read()
                for phrase in forbidden:
                    self.assertNotIn(phrase, source, filename)


if __name__ == '__main__':
    unittest.main()
