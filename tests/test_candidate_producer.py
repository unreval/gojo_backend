"""Candidate worker regressions over disposable PGlite and fake providers."""
import ast
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import runpy
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))
import candidate_producer as producer
from tests import test_memory_candidate_source as source_fixture


class ProducerPureTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)

    def events(self, count, age=0):
        return [{'occurred_at': self.now - timedelta(seconds=age)}
                for _ in range(count)]

    def test_batch_trigger_uses_count_or_oldest_age_with_exact_boundary(self):
        self.assertTrue(producer.batch_ready(self.events(8), self.now))
        self.assertFalse(producer.batch_ready(self.events(7, 299), self.now))
        self.assertTrue(producer.batch_ready(self.events(7, 300), self.now))
        self.assertTrue(producer.batch_ready(self.events(1, 300), self.now))
        self.assertFalse(producer.batch_ready([], self.now))

    def test_complete_output_must_be_json_array_and_end_turn(self):
        self.assertEqual(producer.parse_candidates('[]', 'end_turn'), [])
        for text, stop in (('[]', 'max_tokens'), ('[]', 'tool_use'),
                           ('not JSON', 'end_turn'), ('{}', 'end_turn')):
            with self.subTest(text=text, stop=stop):
                with self.assertRaises(producer.BatchOutputError):
                    producer.parse_candidates(text, stop)

    def test_configuration_is_disabled_and_shadow_by_default(self):
        for enabled, shadow, expected in (
                (None, None, (False, True)), ('true', 'false', (True, False)),
                ('1', '0', (True, False)), ('false', 'true', (False, True))):
            with self.subTest(enabled=enabled, shadow=shadow), patch.dict(os.environ):
                for key, value in (('CANDIDATE_PRODUCER_ENABLED', enabled),
                                   ('CANDIDATE_PRODUCER_SHADOW', shadow)):
                    if value is None:
                        os.environ.pop(key, None)
                    else:
                        os.environ[key] = value
                config = runpy.run_path(str(ROOT / 'gojo_backend/config.py'))
                self.assertEqual((config['CANDIDATE_PRODUCER_ENABLED'],
                                  config['CANDIDATE_PRODUCER_SHADOW']), expected)

    def test_disabled_entry_points_do_not_open_database_model_or_thread(self):
        model = Mock(side_effect=AssertionError('disabled producer called provider'))
        with patch('config.CANDIDATE_PRODUCER_ENABLED', False), \
                patch('db.get_conn', side_effect=AssertionError('disabled DB')) as db, \
                patch.object(producer, 'get_conn', side_effect=AssertionError('disabled DB'),
                             create=True) as local_db, \
                patch.object(producer.threading, 'Thread') as thread:
            producer.start_candidate_producer()
            producer.run_worker_once(enabled=False, create_chat_fn=model)
            producer.run_pair('u', 'c', enabled=False, create_chat_fn=model)
        db.assert_not_called()
        local_db.assert_not_called()
        model.assert_not_called()
        thread.assert_not_called()

    def test_worker_is_wired_at_existing_process_startup(self):
        tree = ast.parse((ROOT / 'gojo_backend/gojo_server.py').read_text(encoding='utf-8'))
        imports = [node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
                   and node.module == 'candidate_producer']
        self.assertTrue(any(any(alias.name == 'start_candidate_producer'
                                for alias in node.names) for node in imports))
        self.assertTrue(any(isinstance(node, ast.Call)
                            and isinstance(node.func, ast.Name)
                            and node.func.id == 'start_candidate_producer'
                            for node in ast.walk(tree)))

    def test_global_daily_limit_stops_remaining_pairs_from_being_queued(self):
        connection = Mock()
        connection.cursor.return_value.fetchall.return_value = [('u', 'c'), ('other', 'c')]
        with patch('db.get_conn', return_value=connection), \
                patch.object(producer, 'run_pair', return_value={'status': 'daily_limit'}) as run:
            results = producer.run_worker_once(enabled=True, shadow=True)
        self.assertEqual(results, [{'status': 'daily_limit'}])
        run.assert_called_once()

    def test_existing_seven_literal_forms_keep_their_original_semantics(self):
        from cognitive_revision import parse_cognitive_evidence
        cases = (
            ('我喜欢咖啡。', 'report', 'reported_preference', '喜欢'),
            ('我的职业是「老师」。', 'report', 'reported_identity', '老师'),
            ('我拒绝「拍照」。', 'report', 'explicit_refusal', '拒绝'),
            ('不要「拍照」。', 'report', 'explicit_boundary', '不要'),
            ('我承诺在2026-10-21前完成「作业」。', 'report', 'explicit_promise', '承诺'),
            ('我已兑现截至2026-10-21的承诺「作业」。', 'report', 'explicit_promise', '已兑现'),
            ('更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。',
             'correction', 'reported_preference', '喜欢'),
        )
        for text, operation, predicate, value in cases:
            with self.subTest(text=text):
                result = parse_cognitive_evidence(text, 'u', 'c')
                self.assertEqual(result['operation'], operation)
                self.assertEqual(result['claim']['predicate'], predicate)
                self.assertEqual(result['claim']['value'], value)
                self.assertEqual(result['claim']['subject'], 'user:u')

    def test_audit_marks_short_circuited_gates_as_not_evaluated(self):
        gates = producer.gate_results(False, 'quote_not_verbatim', {})
        self.assertEqual(gates['G1']['status'], 'passed')
        self.assertEqual(gates['G2']['status'], 'rejected')
        self.assertTrue(all(gates[key]['status'] == 'not_evaluated'
                            for key in ('G3', 'G4', 'G5', 'G6', 'G7', 'G8', 'G9')))


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'requires disposable local PGlite')
class ProducerDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(source_fixture.CandidateSourceTests.setUpClass.__func__)
    tearDownClass = classmethod(source_fixture.CandidateSourceTests.tearDownClass.__func__)
    tearDown = source_fixture.CandidateSourceTests.tearDown
    sql = source_fixture.CandidateSourceTests.sql
    database_state = source_fixture.CandidateSourceTests.database_state

    def setUp(self):
        source_fixture.CandidateSourceTests.setUp(self)
        import raw_events
        processing = [ddl for ddl in raw_events.RAW_EVENT_DDL
                      if 'CREATE TABLE IF NOT EXISTS event_processing_state' in ddl]
        self.assertEqual(len(processing), 1)
        self.sql(processing[0])
        self.sql('ALTER TABLE chat_log ADD COLUMN reply_to_event_id TEXT')
        self.database.commit()
        self.now = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)
        self.logs = []

    def seed(self, count=8, *, age=0, character='c', prefix='source', role='user',
             event_type='canonical_user_turn', status='active', user='u'):
        ids = [f'{prefix}-{index}' for index in range(count)]
        created = self.now - timedelta(seconds=age)
        text = '今天我亏了69元，想和你聊一聊。'
        for source_id in ids:
            self.sql('''INSERT INTO chat_log
                (user_id,chat_id,event_id,role,text,created_at,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s)''',
                (user, character, source_id, role, text, created, status))
            self.sql('''INSERT INTO cognitive_events
                (user_id,character_id,source_event_type,source_event_id,source,
                 occurred_at,payload)
                VALUES (%s,%s,%s,%s,'chat',%s,%s::jsonb)''',
                (user, character, event_type, source_id, created,
                 json.dumps({'content': text}, ensure_ascii=False)))
        self.database.commit()
        return ids

    def candidate(self, source_id='source-0', **changes):
        value = {'source_event_id': source_id, 'quote': '今天我亏了69元',
                 'summary': '她今天亏了69元', 'predicate': 'reported_fact',
                 'object': '交易结果', 'value': '亏了69元'}
        value.update(changes)
        return value

    def model(self, candidates=None, *, stop_reason='end_turn', raw=None):
        return Mock(return_value=(
            raw if raw is not None else json.dumps(
                [self.candidate()] if candidates is None else candidates, ensure_ascii=False),
            {'stop_reason': stop_reason}))

    def run_pair(self, model=None, *, now=None, character='c', shadow=True):
        return producer.run_pair(
            'u', character, now=now or self.now,
            create_chat_fn=model if model is not None else self.model(),
            conn=self.database, emit=self.logs.append, enabled=True, shadow=shadow)

    def candidate_logs(self):
        return [row for row in self.logs if row.get('kind') == 'candidate']

    def non_processing_state(self):
        return {key: value for key, value in self.database_state().items()
                if key != 'event_processing_state'}

    def test_nine_events_make_one_batch_of_eight_using_unchanged_main_model(self):
        self.seed(9)
        model = self.model()
        tagged_model = '[冰咖啡]claude-opus-4-6'
        with patch('config.MODEL_MAIN', tagged_model):
            self.run_pair(model)
        model.assert_called_once()
        request = model.call_args.kwargs
        self.assertEqual(request['model'], tagged_model)
        self.assertEqual(request['max_tokens'], 2000)
        self.assertEqual(request['max_retries'], 0)
        self.assertEqual(len(self.sql("SELECT * FROM event_processing_state WHERE status='succeeded'")), 8)

    def test_batch_takes_canonical_lock_before_validation_source_locks(self):
        from cognitive_queue import stable_advisory_lock_key
        self.seed()
        with patch.object(self.database, 'query', wraps=self.database.query) as queries:
            self.run_pair(shadow=False)
        calls = [(call.args[0], call.args[1] if len(call.args) > 1 else ())
                 for call in queries.call_args_list]
        locks = [index for index, (sql, params) in enumerate(calls)
                 if 'pg_advisory_xact_lock' in sql
                 and params == (stable_advisory_lock_key('u', 'c'),)]
        reads = [index for index, (sql, _) in enumerate(calls)
                 if 'SELECT payload FROM cognitive_events' in sql and 'FOR SHARE' in sql]
        self.assertTrue(locks)
        self.assertLess(locks[0], reads[0])

    def test_seven_recent_events_wait_but_exactly_five_minutes_triggers(self):
        self.seed(7, age=299)
        model = self.model([])
        self.run_pair(model)
        model.assert_not_called()
        self.run_pair(model, now=self.now + timedelta(seconds=1))
        model.assert_called_once()

    def test_shadow_runs_real_gates_and_changes_only_processing_state(self):
        self.seed()
        before = self.non_processing_state()
        with patch('memory_candidate.apply_candidate',
                   side_effect=AssertionError('shadow projected memory')) as project:
            self.run_pair()
        project.assert_not_called()
        self.assertEqual(self.non_processing_state(), before)
        candidates = self.candidate_logs()
        self.assertEqual(len(candidates), 1)
        self.assertTrue(candidates[0]['accepted'])
        self.assertTrue(candidates[0]['shadow'])
        self.assertTrue(candidates[0]['gates'])

    def test_truncated_output_leaves_events_pending_and_halves_retry_batch(self):
        self.seed()
        model = self.model(stop_reason='max_tokens')
        self.run_pair(model)
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])
        self.assertEqual(self.candidate_logs(), [])
        rows = self.sql("SELECT status,retry_count FROM event_processing_state WHERE status='pending'")
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(count >= 1 for _, count in rows))
        retry_model = self.model([])
        self.run_pair(retry_model, now=self.now + timedelta(seconds=61))
        retry_model.assert_called_once()
        self.assertEqual(len(self.sql("SELECT * FROM event_processing_state WHERE status='succeeded'")), 4)

    def test_malformed_json_and_non_array_do_not_project_or_complete_events(self):
        for index, raw in enumerate(('not JSON', '{}')):
            with self.subTest(raw=raw):
                self.seed(prefix=f'invalid-{index}')
                self.run_pair(self.model(raw=raw), now=self.now + timedelta(seconds=61 * index))
                self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])
                self.assertEqual(self.sql("SELECT count(*) FROM event_processing_state WHERE status='succeeded'"), [(0,)])

    def test_one_invalid_candidate_does_not_discard_seven_valid_candidates(self):
        ids = self.seed()
        candidates = [self.candidate(source_id) for source_id in ids]
        candidates[3]['predicate'] = 'invented_relationship_judgment'
        self.run_pair(self.model(candidates))
        rows = self.candidate_logs()
        self.assertEqual(len(rows), 8)
        self.assertEqual(sum(row['accepted'] for row in rows), 7)
        self.assertEqual([row['source_event_id'] for row in rows if not row['accepted']], [ids[3]])

    def test_replay_of_done_event_never_calls_provider_again(self):
        self.seed()
        model = self.model([])
        self.run_pair(model)
        self.run_pair(model, now=self.now + timedelta(seconds=300))
        model.assert_called_once()

    def test_equal_source_ids_in_distinct_character_scopes_do_not_suppress_each_other(self):
        self.seed()
        self.seed(character='other')
        model = self.model([])
        self.run_pair(model)
        self.run_pair(model, character='other')
        self.assertEqual(model.call_count, 2)
        self.assertEqual(self.sql('''SELECT count(DISTINCT processor_type)
            FROM event_processing_state WHERE event_id='source-0' AND status='succeeded' '''),
            [(2,)])

    def test_committed_reservation_with_unknown_ack_retains_budget_and_cooldown(self):
        self.seed()
        commit = self.database.commit
        model = self.model([])

        def commit_then_disconnect():
            commit()
            raise RuntimeError('ack lost after reservation committed')

        with patch.object(self.database, 'commit', side_effect=commit_then_disconnect) as commits:
            result = self.run_pair(model)
        self.assertEqual(result['status'], 'commit_unknown')
        commits.assert_called_once()
        model.assert_not_called()
        self.run_pair(model, now=self.now + timedelta(seconds=1))
        model.assert_not_called()
        budget = self.sql('''SELECT result_ref FROM event_processing_state
            WHERE event_id='candidate-producer:global-budget' ''')[0][0]
        self.assertEqual(json.loads(budget)['batches'], 1)
        self.run_pair(model, now=self.now + timedelta(seconds=31))
        model.assert_called_once()

    def test_unknown_completion_ack_does_not_repeat_provider_or_spend_budget(self):
        self.seed()
        commit = self.database.commit
        model = self.model([])
        calls = 0

        def commit_then_maybe_disconnect():
            nonlocal calls
            calls += 1
            commit()
            if calls == 2:
                raise RuntimeError('ack lost after completion committed')

        with patch.object(self.database, 'commit', side_effect=commit_then_maybe_disconnect):
            result = self.run_pair(model)
        self.assertEqual(result['status'], 'commit_unknown')
        self.run_pair(model, now=self.now + timedelta(seconds=300))
        model.assert_called_once()
        budget = self.sql('''SELECT result_ref FROM event_processing_state
            WHERE event_id='candidate-producer:global-budget' ''')[0][0]
        self.assertEqual(json.loads(budget)['batches'], 1)

    def test_twenty_batch_daily_budget_is_shared_across_user_characters(self):
        self.seed(160)
        self.seed(8, character='other', prefix='other')
        model = self.model([])
        for _ in range(20):
            self.run_pair(model)
        self.assertEqual(model.call_count, 20)
        self.run_pair(model, character='other')
        self.assertEqual(model.call_count, 20)

    def test_three_provider_failures_pause_user_and_recovery_is_possible(self):
        self.seed()
        model = Mock(side_effect=RuntimeError('offline failure'))
        for seconds in (0, 61, 122):
            self.run_pair(model, now=self.now + timedelta(seconds=seconds))
        self.assertEqual(model.call_count, 3)
        self.run_pair(model, now=self.now + timedelta(seconds=180))
        self.assertEqual(model.call_count, 3)
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])
        recovered = self.model([])
        self.run_pair(recovered, now=self.now + timedelta(seconds=122 + 1801))
        recovered.assert_called_once()

    def test_assistant_raw_role_cannot_become_user_fact(self):
        self.seed()
        response = self.model().return_value

        def changed_source(**_request):
            self.sql("UPDATE chat_log SET role='assistant' WHERE event_id='source-0'")
            self.database.commit()
            return response

        self.run_pair(Mock(side_effect=changed_source))
        rows = self.candidate_logs()
        self.assertTrue(rows)
        self.assertFalse(rows[0]['accepted'])
        self.assertEqual(rows[0]['reason'], 'source_not_active_or_not_user')
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])

    def test_invented_quote_and_foreign_source_id_are_rejected(self):
        self.seed()
        self.run_pair(self.model([
            self.candidate(quote='她住在火星上'),
            self.candidate('not-in-this-batch'),
        ]))
        self.assertFalse(any(row['accepted'] for row in self.candidate_logs()))
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])

    def test_live_mode_projects_only_after_validation_and_is_recallable(self):
        self.seed()
        self.run_pair(shadow=False)
        from memory_authority import authoritative_memory_sql
        gate = authoritative_memory_sql('long_memory')
        self.assertEqual(self.sql(f'SELECT content FROM long_memory WHERE {gate}'),
                         [('她今天亏了69元。',)])
        self.assertTrue(self.sql('SELECT adjudication FROM cognitive_events WHERE source_event_id=%s',
                                 ('source-0',))[0][0])


if __name__ == '__main__':
    unittest.main()
