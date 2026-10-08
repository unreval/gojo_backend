"""Worker-off ingress, lease recovery, and bounded deterministic processing."""
from contextlib import ExitStack
from datetime import timedelta
import os
import unittest
from unittest.mock import Mock, call, patch

from tests import test_cognitive_deterministic as acceptance
import cognitive_config
import cognitive_events
import cognitive_queue as queue
import cognitive_worker as worker


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'set COGNITIVE_TEST_PGLITE to the local test-only package')
class WorkerControlsSQLTests(unittest.TestCase):
    setUpClass = classmethod(acceptance.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(acceptance.OfflineDatabaseTests.tearDownClass.__func__)
    tearDown = acceptance.OfflineDatabaseTests.tearDown
    sql = acceptance.OfflineDatabaseTests.sql
    source = acceptance.OfflineDatabaseTests.source

    def setUp(self):
        acceptance.OfflineDatabaseTests.setUp(self)
        self.stack.enter_context(patch.object(worker, '_LAST_REFLECTION_SCAN_AT', self.now))
        self.stack.enter_context(patch.object(queue, 'COGNITIVE_CLAIM_LEASE_SECONDS', 300))

    def ingest_turn(self, source_id, *, user='u', character='c'):
        self.source(source_id, '我喜欢咖啡。', user=user, character=character)
        with patch.object(queue, '_utc_now', side_effect=lambda now=None: now or self.now):
            return cognitive_events.ingest_canonical_turn(
                user_id=user, character_id=character, source_event_id=source_id)

    def set_controls(self, maximum, interval=0):
        for module in (queue, worker):
            self.stack.enter_context(patch.object(module, 'COGNITIVE_MAX_CYCLES_PER_RUN', maximum, create=True))
            self.stack.enter_context(patch.object(module, 'COGNITIVE_MIN_SECONDS_BETWEEN_CYCLES', interval, create=True))

    def snapshot(self):
        return (
            self.sql('SELECT id,status,started_at,completed_at,failure_code FROM cognitive_cycles ORDER BY id'),
            self.sql('''SELECT id,status,attempt_count,claimed_by_cycle_id,claimed_at,
                              claim_expires_at,consumed_cycle_id,consumed_at,last_error_code
                       FROM cognitive_event_triggers ORDER BY id'''),
        )

    def test_disabled_ingress_never_claims_or_exhausts_new_evidence(self):
        with patch.object(queue, 'COGNITIVE_WORKER_ENABLED', False, create=True), \
             patch.object(cognitive_events, 'COGNITIVE_WORKER_ENABLED', False, create=True), \
             patch.object(worker, 'COGNITIVE_WORKER_ENABLED', False):
            for index in range(4):
                self.now += timedelta(seconds=301)
                self.assertEqual(self.ingest_turn('disabled-' + str(index))['status'], 'inserted')
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_cycles'), [(0,)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events'), [(4,)])
        rows = self.sql('''SELECT trigger_class,status,attempt_count,claimed_by_cycle_id,
                                 claimed_at,claim_expires_at,consumed_cycle_id,last_error_code
                          FROM cognitive_event_triggers ORDER BY id''')
        self.assertEqual(rows, [('question_reactivation', 'pending', 0, None, None, None, None, None)] * 4)

    def test_enabled_question_reactivation_completes_and_consumes(self):
        ingested = self.ingest_turn('enabled')
        cycle_id, trigger_id = ingested['cycle']['cycle_id'], ingested['trigger_id']
        self.assertEqual(self.sql('SELECT status,started_at FROM cognitive_cycles WHERE id=%s', (cycle_id,)),
                         [('queued', None)])
        self.assertEqual(self.sql('''SELECT trigger_class,status,attempt_count,claimed_by_cycle_id
                                    FROM cognitive_event_triggers WHERE id=%s''', (trigger_id,)),
                         [('question_reactivation', 'claimed', 1, cycle_id)])
        result = worker.run_worker_once(now=self.now + timedelta(seconds=1))
        self.assertEqual(result['status'], 'succeeded', result)
        cycle = self.sql('''SELECT status,worker_model,worker_usage,input_state_version,
                                  output_state_version,started_at,completed_at
                           FROM cognitive_cycles WHERE id=%s''', (cycle_id,))[0]
        self.assertEqual(cycle[:3], ('succeeded', 'deterministic_evidence_policy_v1',
                                    {'model_calls': 0, 'input_tokens': 0, 'output_tokens': 0}))
        self.assertEqual(cycle[4], cycle[3] + 1)
        self.assertIsNotNone(cycle[5])
        self.assertIsNotNone(cycle[6])
        trigger = self.sql('''SELECT status,attempt_count,consumed_cycle_id,consumed_at,
                                    claimed_by_cycle_id,claimed_at,claim_expires_at,last_error_code
                             FROM cognitive_event_triggers WHERE id=%s''', (trigger_id,))[0]
        self.assertEqual(trigger[:3], ('consumed', 1, cycle_id))
        self.assertIsNotNone(trigger[3])
        self.assertEqual(trigger[4:], (None, None, None, None))
        self.assertEqual(self.sql("SELECT adjudication->>'status' FROM cognitive_events"), [('applied',)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs'), [(1,)])

    def test_process_limit_and_monotonic_interval_preserve_unprocessed_cycles(self):
        for index in range(3):
            self.ingest_turn('queued-' + str(index), user='user-' + str(index))
        self.set_controls(2, 10)
        with patch.object(worker, 'monotonic', return_value=100.0) as clock:
            self.assertEqual(worker.run_worker_once(now=self.now)['status'], 'succeeded')
            after_first = self.snapshot()
            clock.return_value = 109.5
            waiting = worker.run_worker_once(now=self.now + timedelta(seconds=1))
            self.assertEqual(waiting['status'], 'cycle_interval_wait')
            self.assertAlmostEqual(waiting['retry_after'], 0.5)
            self.assertEqual(self.snapshot(), after_first)
            clock.return_value = 110.0
            self.assertEqual(worker.run_worker_once(now=self.now + timedelta(seconds=2))['status'], 'succeeded')
            capped = self.snapshot()
            with patch('builtins.print') as logs:
                for tick in (120.0, 130.0):
                    clock.return_value = tick
                    limited = worker.run_worker_once(now=self.now + timedelta(seconds=3))
                    self.assertEqual(limited['status'], 'run_limit_reached')
                    self.assertEqual((limited['processed_cycles'], limited['limit']), (2, 2))
                    self.assertEqual(self.snapshot(), capped)
                self.assertEqual(logs.call_count, 1)
        self.assertEqual(self.sql('SELECT status FROM cognitive_cycles ORDER BY id'),
                         [('succeeded',), ('succeeded',), ('queued',)])
        self.assertEqual(self.sql('SELECT status,attempt_count FROM cognitive_event_triggers ORDER BY id'),
                         [('consumed', 1), ('consumed', 1), ('claimed', 1)])

    def test_controls_defer_ingress_and_do_not_preclaim_pending_pairs(self):
        self.set_controls(1)
        for index in range(3):
            result = self.ingest_turn('pending-' + str(index), user='user-' + str(index))
            self.assertEqual(result['cycle']['status'], 'worker_maintenance_required')
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_cycles'), [(0,)])
        self.assertEqual(self.sql('SELECT status,attempt_count FROM cognitive_event_triggers ORDER BY id'),
                         [('pending', 0)] * 3)
        self.assertEqual(worker.run_worker_once(now=self.now)['status'], 'succeeded')
        self.assertEqual(self.sql('SELECT status FROM cognitive_cycles'), [('succeeded',)])
        self.assertEqual(self.sql('SELECT status,attempt_count FROM cognitive_event_triggers ORDER BY id'),
                         [('consumed', 1), ('pending', 0), ('pending', 0)])
        capped = self.snapshot()
        self.assertEqual(worker.run_worker_once(now=self.now)['status'], 'run_limit_reached')
        self.assertEqual(self.snapshot(), capped)

    def test_failed_cycle_counts_toward_process_limit(self):
        for index in range(2):
            self.ingest_turn('failed-' + str(index), user='user-' + str(index))
        self.set_controls(1)
        with patch.object(worker, 'generate_cycle_output', side_effect=RuntimeError('injected_failure')):
            result = worker.run_worker_once(now=self.now)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(worker._PROCESSED_CYCLES, 1)
        failed = self.snapshot()
        self.assertEqual(worker.run_worker_once(now=self.now)['status'], 'run_limit_reached')
        self.assertEqual(self.snapshot(), failed)
        self.assertEqual(self.sql('SELECT status FROM cognitive_cycles ORDER BY id'), [('failed',), ('queued',)])
        self.assertEqual(self.sql('SELECT status,attempt_count FROM cognitive_event_triggers ORDER BY id'),
                         [('pending', 1), ('claimed', 1)])

    def test_disabled_ingress_recovers_old_leases_without_reclaim_or_refund(self):
        seeded = []
        for character, attempts in (('gojo', 2), ('geto', 3)):
            ingested = self.ingest_turn('old-' + character, character=character)
            seeded.append((character, attempts, ingested['cycle']['cycle_id'], ingested['trigger_id']))
            self.sql('UPDATE cognitive_event_triggers SET attempt_count=%s WHERE id=%s',
                     (attempts, ingested['trigger_id']))
        self.database.commit()
        self.now += timedelta(seconds=301)
        with patch.object(queue, 'COGNITIVE_WORKER_ENABLED', False, create=True), \
             patch.object(cognitive_events, 'COGNITIVE_WORKER_ENABLED', False, create=True), \
             patch.object(worker, 'COGNITIVE_WORKER_ENABLED', False):
            for character, attempts, cycle_id, trigger_id in seeded:
                self.ingest_turn('new-' + character, character=character)
                self.assertEqual(self.sql('''SELECT status,failure_code,started_at
                                            FROM cognitive_cycles WHERE id=%s''', (cycle_id,)),
                                 [('failed', 'claim_lease_expired', None)])
                expected = 'dead_letter' if attempts == 3 else 'pending'
                self.assertEqual(self.sql('''SELECT status,attempt_count,claimed_by_cycle_id,claimed_at,
                                                   claim_expires_at,last_error_code
                                            FROM cognitive_event_triggers WHERE id=%s''', (trigger_id,)),
                                 [(expected, attempts, None, None, None, 'claim_lease_expired')])
                if character == 'gojo':
                    other_trigger = seeded[1][3]
                    self.assertEqual(self.sql('SELECT status,attempt_count FROM cognitive_event_triggers WHERE id=%s',
                                              (other_trigger,)), [('claimed', 3)])
                self.ingest_turn('again-' + character, character=character)
                self.assertEqual(self.sql('SELECT status,attempt_count FROM cognitive_event_triggers WHERE id=%s',
                                          (trigger_id,)), [(expected, attempts)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_cycles'), [(2,)])
        self.assertEqual(self.sql('''SELECT status,attempt_count FROM cognitive_event_triggers
                                    WHERE last_error_code IS NULL ORDER BY id'''), [('pending', 0)] * 4)

    def test_disabled_duplicate_ingress_recovers_a_running_cycle(self):
        ingested = self.ingest_turn('duplicate')
        cycle_id, trigger_id = ingested['cycle']['cycle_id'], ingested['trigger_id']
        self.assertTrue(queue.mark_cycle_running(cycle_id, conn=self.database, now=self.now))
        self.now += timedelta(seconds=301)
        with patch.object(queue, 'COGNITIVE_WORKER_ENABLED', False, create=True), \
             patch.object(cognitive_events, 'COGNITIVE_WORKER_ENABLED', False, create=True), \
             patch.object(worker, 'COGNITIVE_WORKER_ENABLED', False), \
             patch.object(queue, '_utc_now', side_effect=lambda now=None: now or self.now):
            result = cognitive_events.ingest_canonical_turn(
                user_id='u', character_id='c', source_event_id='duplicate')
        self.assertEqual(result['status'], 'duplicate')
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_cycles'), [(1,)])
        cycle = self.sql('SELECT status,failure_code,started_at FROM cognitive_cycles WHERE id=%s', (cycle_id,))[0]
        self.assertEqual(cycle[:2], ('failed', 'claim_lease_expired'))
        self.assertIsNotNone(cycle[2])
        self.assertEqual(self.sql('''SELECT status,attempt_count,claimed_by_cycle_id,claimed_at,
                                           claim_expires_at,last_error_code
                                    FROM cognitive_event_triggers WHERE id=%s''', (trigger_id,)),
                         [('pending', 1, None, None, None, 'claim_lease_expired')])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_event_triggers'), [(1,)])

    def test_disabled_direct_aggregation_recovers_without_reclaiming(self):
        ingested = self.ingest_turn('direct')
        cycle_id, trigger_id = ingested['cycle']['cycle_id'], ingested['trigger_id']
        with patch.object(queue, 'COGNITIVE_WORKER_ENABLED', False, create=True):
            result = queue.aggregate_pending_triggers('u', 'c', conn=self.database,
                                                     now=self.now + timedelta(seconds=301))
        self.assertEqual(result['status'], 'disabled')
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_cycles'), [(1,)])
        self.assertEqual(self.sql('SELECT status,failure_code FROM cognitive_cycles WHERE id=%s', (cycle_id,)),
                         [('failed', 'claim_lease_expired')])
        self.assertEqual(self.sql('''SELECT status,attempt_count,claimed_by_cycle_id,claim_expires_at
                                    FROM cognitive_event_triggers WHERE id=%s''', (trigger_id,)),
                         [('pending', 1, None, None)])


class WorkerControlUnitTests(unittest.TestCase):
    def test_loop_waits_for_guards_and_keeps_running_until_stopped(self):
        stop = Mock()
        stop.is_set.side_effect = [False, False, False, True]
        guarded_results = [
            {'status': 'run_limit_reached', 'retry_after': 5.0},
            {'status': 'cycle_interval_wait', 'retry_after': 2.5},
            {'status': 'run_limit_reached', 'retry_after': 5.0},
        ]
        with patch.object(worker, '_STOP', stop), \
             patch.object(worker, 'run_worker_once', side_effect=guarded_results) as run_once, \
             patch.object(worker, 'monotonic', return_value=100.0):
            worker._loop()
        self.assertEqual(run_once.call_count, 3)
        self.assertEqual(stop.wait.call_args_list, [call(5.0), call(2.5), call(5.0)])
        self.assertEqual(stop.is_set.call_count, 4)

    def test_control_integer_settings_default_and_clamp_to_zero(self):
        for name in ('COGNITIVE_MAX_CYCLES_PER_RUN', 'COGNITIVE_MIN_SECONDS_BETWEEN_CYCLES'):
            for value in (None, '-1', 'invalid'):
                with self.subTest(setting=name, value=value), patch.dict(os.environ):
                    if value is None:
                        os.environ.pop(name, None)
                    else:
                        os.environ[name] = value
                    self.assertEqual(cognitive_config._int_env(name, 0), 0)

    def test_disabled_single_step_has_no_maintenance_or_claim(self):
        with patch.object(worker, 'COGNITIVE_WORKER_ENABLED', False), \
             patch.object(worker, 'maintain_scheduled_reflections') as reflections, \
             patch.object(worker, 'maintain_pending_cycles') as maintenance, \
             patch.object(worker, 'claim_next_cycle') as claim:
            self.assertEqual(worker.run_worker_once(), {'status': 'disabled'})
            reflections.assert_not_called()
            maintenance.assert_not_called()
            claim.assert_not_called()

    def test_thread_restart_does_not_reset_process_budget(self):
        fake_thread = Mock()
        fake_thread.is_alive.return_value = True
        with ExitStack() as stack:
            stack.enter_context(patch.object(worker, 'COGNITIVE_WORKER_ENABLED', True))
            stack.enter_context(patch.object(worker, '_THREAD', None))
            stack.enter_context(patch.object(worker, '_STOP', Mock()))
            stack.enter_context(patch.object(worker, '_PROCESSED_CYCLES', 2))
            stack.enter_context(patch.object(worker, '_NEXT_CYCLE_AT', 123.0))
            stack.enter_context(patch.object(worker, '_RUN_LIMIT_LOGGED', True))
            constructor = stack.enter_context(patch.object(worker.threading, 'Thread', return_value=fake_thread))
            stack.enter_context(patch('builtins.print'))
            worker.start_cognitive_worker()
            worker.stop_cognitive_worker()
            worker.start_cognitive_worker()
            self.assertEqual(constructor.call_count, 2)
            self.assertEqual(fake_thread.start.call_count, 2)
            self.assertEqual((worker._PROCESSED_CYCLES, worker._NEXT_CYCLE_AT, worker._RUN_LIMIT_LOGGED),
                             (2, 123.0, True))


if __name__ == '__main__':
    unittest.main()
