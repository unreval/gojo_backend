"""Offline SQL acceptance tests for the candidate producer's shadow-only boundary."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import importlib
import json
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import Mock, patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gojo_backend'))


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE to the local test-only package')
class CandidateShadowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def setUp(self):
        import cognitive_db
        import raw_events
        from tests.memory_schema import create_memory_tables
        self.shadow = importlib.import_module('memory_candidate_shadow')
        self.database.rollback()
        self.database.query('DROP SCHEMA public CASCADE')
        self.database.query('CREATE SCHEMA public')
        cur = self.database.cursor()
        for ddl in cognitive_db.ddl_statements():
            cur.execute(ddl)
        cur.execute('''CREATE TABLE chat_log (
            id BIGSERIAL PRIMARY KEY, user_id TEXT, chat_id TEXT,
            event_id TEXT, client_msg_id TEXT, role TEXT, text TEXT,
            kind TEXT DEFAULT 'text', extra TEXT DEFAULT '{}',
            created_at TIMESTAMPTZ, subtitle TEXT DEFAULT '',
            status TEXT DEFAULT 'active')''')
        create_memory_tables(cur)
        processing_ddl = [ddl for ddl in raw_events.RAW_EVENT_DDL
                          if 'CREATE TABLE IF NOT EXISTS event_processing_state' in ddl]
        self.assertEqual(len(processing_ddl), 1)
        cur.execute(processing_ddl[0])
        self.database.commit()
        self.now = datetime(2026, 10, 18, 4, tzinfo=timezone.utc)
        self.logs = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('db.get_conn', return_value=self.database))
        self.stack.enter_context(patch('raw_events.get_conn', return_value=self.database))
        self.write_guards = [self.stack.enter_context(patch(target,
            side_effect=AssertionError('SHADOW_ATTEMPTED_MEMORY_WRITE')))
            for target in ('memory_candidate.apply_candidate',
                           'memory_candidate.project_canonical_memory',
                           'memory_authority.project_canonical_memory')]

    def tearDown(self):
        for guard in self.write_guards:
            guard.assert_not_called()

    def sql(self, statement, params=()):
        cur = self.database.cursor()
        cur.execute(statement, params)
        return cur.fetchall()

    def seed(self, count=1, *, age=0, character='c', prefix='source',
             role='user', event_type='canonical_user_turn', status='active',
             adjudication=None, user='u', text='今天我亏了69元，想和你聊一聊。',
             source_chat_id=None, metadata=None):
        ids = [f'{prefix}-{index}' for index in range(count)]
        created = self.now - timedelta(seconds=age)
        for source_id in ids:
            self.sql('''INSERT INTO chat_log
                (user_id,chat_id,event_id,role,text,created_at,status,extra)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
                (user, source_chat_id or character, source_id, role, text,
                 created, status, json.dumps(metadata or {})))
            payload = {'content': text}
            if source_chat_id:
                payload['source_chat_id'] = source_chat_id
            self.sql('''INSERT INTO cognitive_events
                (user_id,character_id,source_event_type,source_event_id,source,
                 occurred_at,created_at,payload,adjudication)
                VALUES (%s,%s,%s,%s,'chat',%s,%s,%s::jsonb,%s::jsonb)''',
                (user, character, event_type, source_id, created, created,
                 json.dumps(payload, ensure_ascii=False),
                 json.dumps({'status': adjudication} if adjudication else {})))
        self.database.commit()
        return ids

    def candidate(self, source_id='source-0', **changes):
        result = dict(source_event_id=source_id, quote='今天我亏了69元',
                      summary='她今天亏了69元', predicate='reported_fact',
                      time_scope='unspecified', perspective=None)
        result.update(changes)
        return result

    def model(self, candidates=None, *, stop_reason='end_turn', output_tokens=100):
        return Mock(return_value=(
            json.dumps(candidates if candidates is not None else [self.candidate()],
                       ensure_ascii=False),
            {'stop_reason': stop_reason, 'output_tokens': output_tokens}))

    def database_state(self):
        tables = self.sql("""SELECT tablename FROM pg_tables
            WHERE schemaname='public' AND tablename <> 'event_processing_state'
            ORDER BY tablename""")
        return {table: self.sql(
            f'SELECT to_jsonb(row) FROM "{table}" row ORDER BY to_jsonb(row)::text')
            for table, in tables}

    def run_shadow(self, model=None, *, user='u', character='c', now=None,
                   external_edit=None, expected_after=None):
        before = self.database_state()
        if external_edit is not None:
            for row, in before['chat_log']:
                if row['event_id'] == external_edit['source_id']:
                    row.update({key: value for key, value in external_edit.items()
                                if key != 'source_id'})
        query = self.database.query

        def processing_only_query(statement, params=()):
            compact = ' '.join(statement.split())
            without_lock_or_conflict = re.sub(r'\b(?:FOR|DO)\s+UPDATE\b', '', compact, flags=re.I)
            commands = re.findall(r'\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)',
                                  without_lock_or_conflict, flags=re.I)
            for _, table in commands:
                self.assertEqual(table.lower(), 'event_processing_state', compact)
            self.assertFalse(re.search(r'\b(CREATE|ALTER|DROP|TRUNCATE)\b', compact, re.I), compact)
            return query(statement, params)

        try:
            with patch.object(self.database, 'query', side_effect=processing_only_query):
                result = self.shadow.run_pair(
                    user, character, now=now or self.now,
                    create_chat_fn=model if model is not None else self.model(),
                    conn=self.database, emit=self.logs.append)
            return result
        finally:
            self.assertEqual(self.database_state(), expected_after() if expected_after else before)

    def candidate_logs(self):
        return [row for row in self.logs if row['kind'] == 'candidate']

    def batch_logs(self):
        return [row for row in self.logs if row['kind'] == 'batch']

    def failure_logs(self):
        return [row for row in self.logs if row['kind'] == 'batch_failure']

    def assert_pending(self):
        self.assertTrue(all(row[0] in (None, 'pending') for row in self.sql(
            "SELECT adjudication->>'status' FROM cognitive_events")))

    def test_eight_recent_user_turns_trigger_one_bounded_main_model_batch(self):
        ids = self.seed(9)
        model = self.model()
        result = self.run_shadow(model)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['input_count'], 8)
        model.assert_called_once()
        from config import MODEL_MAIN
        self.assertEqual(model.call_args.kwargs['model'], MODEL_MAIN)
        self.assertEqual(model.call_args.kwargs['max_tokens'], 2000)
        self.assertEqual(model.call_args.kwargs['max_retries'], 0)
        self.assertEqual(set(self.batch_logs()[0]['input_source_event_ids']), set(ids[:8]))
        self.assertEqual(len(self.candidate_logs()), 1)
        self.assertEqual(self.candidate_logs()[0]['accepted'], True)
        self.assert_pending()

    def test_seven_messages_wait_until_the_oldest_reaches_five_minutes(self):
        self.seed(7, age=299)
        model = self.model()
        self.assertEqual(self.run_shadow(model)['status'], 'waiting')
        model.assert_not_called()
        result = self.run_shadow(model, now=self.now + timedelta(seconds=1))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['input_count'], 7)
        model.assert_called_once()

    def test_single_message_at_exactly_five_minutes_triggers(self):
        self.seed(age=300)
        result = self.run_shadow()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['input_count'], 1)

    def test_truncated_but_parseable_json_is_discarded_and_next_batch_uses_four(self):
        self.seed(8, age=300)
        truncated = self.model(stop_reason='max_tokens', output_tokens=2000)
        failed = self.run_shadow(truncated)
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(self.candidate_logs(), [])
        self.assertEqual(self.failure_logs()[-1]['stage'], 'truncation')
        self.assert_pending()
        retry = self.model()
        recovered = self.run_shadow(retry, now=self.now + timedelta(hours=1))
        self.assertEqual(recovered['status'], 'succeeded')
        self.assertEqual(recovered['input_count'], 4)
        self.assertEqual(retry.call_args.kwargs['max_tokens'], 2000)

    def test_output_token_limit_alone_rejects_a_complete_json_batch(self):
        self.seed(8)
        result = self.run_shadow(self.model(output_tokens=2000))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.candidate_logs(), [])
        self.assertEqual(self.failure_logs()[-1]['stage'], 'truncation')
        self.assert_pending()

    def test_duplicate_candidate_and_replayed_source_emit_one_shadow_record(self):
        self.seed(8)
        candidate = self.candidate()
        model = self.model([candidate, dict(candidate)])
        self.assertEqual(self.run_shadow(model)['status'], 'succeeded')
        first = list(self.candidate_logs())
        self.shadow = importlib.reload(self.shadow)
        retry = self.model()
        self.assertEqual(self.run_shadow(retry)['status'], 'waiting')
        retry.assert_not_called()
        self.assertEqual(len(first), 1)
        self.assertEqual(self.candidate_logs(), first)
        summary = self.batch_logs()[0]
        self.assertEqual(summary['candidate_count'], 1)
        self.assertEqual(summary['raw_candidate_count'], 2)
        self.assertEqual(summary['duplicate_candidate_count'], 1)
        self.assertEqual(summary['passed_count'] + sum(summary['rejection_counts'].values()), 1)

    def test_equal_source_ids_from_different_users_have_independent_receipts(self):
        self.seed(8)
        self.seed(8, user='other')
        first = self.run_shadow()
        second = self.run_shadow(user='other')
        self.assertEqual((first['status'], second['status']), ('succeeded', 'succeeded'))
        self.assertEqual([row['user_id'] for row in self.candidate_logs()], ['u', 'other'])
        self.assertEqual([row['source_event_id'] for row in self.candidate_logs()],
                         ['source-0', 'source-0'])

    def test_new_module_version_can_observe_an_existing_source_again(self):
        self.seed(8)
        self.assertEqual(self.run_shadow()['status'], 'succeeded')
        with patch.object(self.shadow, 'MODULE_VERSION', 'candidate_shadow_test_v2'):
            self.assertEqual(self.run_shadow()['status'], 'succeeded')
        self.assertEqual(len(self.candidate_logs()), 2)
        self.assertEqual(len({row['module_version'] for row in self.candidate_logs()}), 2)

    def test_worker_discovery_is_not_starved_by_two_hundred_completed_pairs(self):
        for index in range(200):
            character = f'completed-{index:03}'
            ids = self.seed(character=character, prefix=character)
            self.sql('''INSERT INTO event_processing_state
                (event_id,processor_type,processor_version,status)
                VALUES (%s,%s,%s,'succeeded')''',
                (ids[0], self.shadow._processor('u', character), self.shadow.MODULE_VERSION))
        self.seed(character='zz-pending', prefix='pending')
        before = self.database_state()
        with patch.object(self.shadow, 'run_pair', return_value={'status': 'waiting'}) as run, \
             patch.object(self.shadow._STOP, 'is_set', return_value=False):
            self.shadow.run_worker_once()
        run.assert_called_once_with('u', 'zz-pending')
        self.assertEqual(self.database_state(), before)

    def test_one_invalid_candidate_does_not_discard_the_valid_candidate(self):
        self.seed(8)
        result = self.run_shadow(self.model([
            self.candidate('source-0', quote='今天我赚了69元'),
            self.candidate('source-1'),
        ]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['candidate_count'], 2)
        self.assertEqual(result['passed_count'], 1)
        logs = self.candidate_logs()
        self.assertEqual([row['accepted'] for row in logs], [False, True])
        self.assertEqual(logs[0]['reason'], 'quote_not_verbatim')
        self.assertEqual(logs[0]['gates']['G1']['status'], 'passed')
        self.assertEqual(logs[0]['gates']['G2']['status'], 'rejected')
        self.assertEqual(logs[0]['gates']['G3']['status'], 'not_evaluated')
        self.assertEqual(self.batch_logs()[-1]['rejection_counts'], {'quote_not_verbatim': 1})

    def test_extra_output_fields_reject_only_that_candidate(self):
        self.seed(8)
        invalid = self.candidate('source-0', category='其他', importance=0.9)
        result = self.run_shadow(self.model([invalid, self.candidate('source-1')]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['passed_count'], 1)
        self.assertEqual([row['accepted'] for row in self.candidate_logs()], [False, True])

    def test_model_failure_preserves_pending_and_cools_down_without_memory_writes(self):
        self.seed(8, adjudication='pending')
        model = Mock(side_effect=RuntimeError('offline_provider_failure'))
        result = self.run_shadow(model)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.failure_logs()[-1]['stage'], 'model_call')
        self.assertEqual(self.candidate_logs(), [])
        self.assert_pending()
        retry = self.model()
        self.assertEqual(self.run_shadow(retry)['status'], 'cooldown')
        retry.assert_not_called()
        self.assertEqual(self.run_shadow(retry, now=self.now + timedelta(hours=1))['status'],
                         'succeeded')

    def test_model_failure_does_not_hold_a_transaction_or_prevent_new_chat_ingress(self):
        from cognitive_events import ingest_canonical_turn
        self.seed(8)
        self.sql('''INSERT INTO chat_log(user_id,chat_id,event_id,role,text,created_at)
            VALUES ('u','c','live-chat','user','我喜欢咖啡',%s)''', (self.now,))
        self.database.commit()
        query = self.database.query
        expected = []

        def model(**kwargs):
            self.assertFalse(self.database.in_transaction)
            # This independently authorized ingress is a separate action from
            # the candidate producer; only its own two evidence tables may change.
            with patch.object(self.database, 'query', side_effect=query):
                before_ingress = self.database_state()
                ingress = ingest_canonical_turn(user_id='u', character_id='c',
                    source_event_id='live-chat', conn=self.database, aggregate=False)
                self.database.commit()
                self.assertEqual(ingress['status'], 'inserted')
                after_ingress = self.database_state()
                for table in before_ingress:
                    if table not in {'cognitive_events', 'cognitive_event_triggers'}:
                        self.assertEqual(after_ingress[table], before_ingress[table])
                expected.append(after_ingress)
            raise RuntimeError('offline_provider_failure_after_chat_ingress')

        result = self.run_shadow(model, expected_after=lambda: expected[0])
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.sql("SELECT source_event_id FROM cognitive_events "
                                 "WHERE source_event_id='live-chat'"), [('live-chat',)])
        self.assertEqual(self.candidate_logs(), [])

    def test_repeated_failures_back_off_and_pause_only_the_failing_user(self):
        self.seed(8)
        failure = Mock(side_effect=RuntimeError('offline_provider_failure'))
        current = self.now
        for delay in (120, 600, 1800, 7200, 7200):
            self.assertEqual(self.run_shadow(failure, now=current)['status'], 'failed')
            model = self.model([])
            self.assertEqual(self.run_shadow(
                model, now=current + timedelta(seconds=delay - 1))['status'], 'cooldown')
            model.assert_not_called()
            current += timedelta(seconds=delay)
        self.assertEqual(failure.call_count, 5)
        recovered = self.run_shadow(now=current)
        self.assertEqual(recovered['status'], 'succeeded')
        self.seed(8, prefix='after-half-open')
        self.assertEqual(self.run_shadow(failure, now=current)['status'], 'failed')
        # Success resets the consecutive counter, so a new failure gets 120s.
        self.assertEqual(self.run_shadow(self.model([]),
            now=current + timedelta(seconds=120))['status'], 'succeeded')
        self.seed(8, user='other', prefix='other-user')
        result = self.run_shadow(self.model([]), user='other', now=current - timedelta(seconds=1))
        self.assertEqual(result['status'], 'succeeded')
        self.assert_pending()

    def test_malformed_json_is_a_parse_failure_and_preserves_pending(self):
        self.seed(8)
        model = Mock(return_value=('[{"source_event_id":', {'stop_reason': 'end_turn'}))
        result = self.run_shadow(model)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.failure_logs()[-1]['stage'], 'parse')
        self.assertEqual(self.candidate_logs(), [])
        self.assert_pending()

    def test_empty_output_still_records_recall_denominator(self):
        ids = self.seed(8)
        result = self.run_shadow(self.model([]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['candidate_count'], 0)
        summary = self.batch_logs()[-1]
        self.assertEqual(summary['input_count'], 8)
        self.assertEqual(set(summary['input_source_event_ids']), set(ids))
        self.assertEqual(summary['passed_count'], 0)

    def test_role_user_and_active_canonical_user_event_are_required(self):
        self.seed(8, age=300, prefix='assistant', role='assistant')
        self.seed(8, age=300, prefix='char-event', event_type='canonical_assistant_turn')
        self.seed(8, age=300, prefix='deleted', status='deleted')
        model = self.model([])
        self.assertEqual(self.run_shadow(model)['status'], 'waiting')
        model.assert_not_called()

    def test_existing_adjudication_does_not_consume_the_shadow_processor_receipt(self):
        self.seed(4, prefix='applied', adjudication='applied')
        self.seed(4, prefix='rejected', adjudication='rejected')
        result = self.run_shadow(self.model([
            self.candidate('applied-0'), self.candidate('rejected-0')]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['input_count'], 8)
        self.assertEqual(result['passed_count'], 2)
        self.assertEqual({row[0] for row in self.sql(
            "SELECT adjudication->>'status' FROM cognitive_events")}, {'applied', 'rejected'})

    def test_group_user_sources_require_the_canonical_audience_and_target(self):
        self.seed(8, age=300, prefix='group:1:message:wrong-audience',
                  source_chat_id='group:1',
                  metadata={'canonical_group': True, 'target_character_id': 'c', 'audience': ['d']})
        model = self.model([])
        self.assertEqual(self.run_shadow(model)['status'], 'waiting')
        model.assert_not_called()
        ids = self.seed(8, prefix='group:2:message:valid', source_chat_id='group:2',
                        metadata={'canonical_group': True, 'target_character_id': 'c',
                                  'audience': ['c', 'd']})
        result = self.run_shadow(self.model([self.candidate(ids[0])]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['passed_count'], 1)
        self.assertEqual(set(self.batch_logs()[-1]['input_source_event_ids']), set(ids))

    def test_source_edited_during_model_call_is_rejected_by_live_g1_read(self):
        self.seed(8)
        query = self.database.query
        changed = {'source_id': 'source-0',
                   'text': '今天我亏了69元，这只是我举的例子。'}

        def model(**kwargs):
            # The fixture models an independent source edit, outside the
            # producer SQL spy. Every other persisted row must stay identical.
            query('UPDATE chat_log SET text=%s WHERE event_id=%s',
                  (changed['text'], changed['source_id']))
            return json.dumps([self.candidate()], ensure_ascii=False), {
                'stop_reason': 'end_turn', 'output_tokens': 100}

        result = self.run_shadow(model, external_edit=changed)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['passed_count'], 0)
        row = self.candidate_logs()[0]
        self.assertEqual(row['reason'], 'source_not_active_or_changed')
        self.assertEqual(row['gates']['G1']['status'], 'rejected')
        self.assertTrue(all(row['gates'][f'G{index}']['status'] == 'not_evaluated'
                            for index in range(2, 10)))

    def test_source_role_changed_to_assistant_during_generation_is_rejected(self):
        self.seed(8)
        query = self.database.query

        def model(**kwargs):
            query("UPDATE chat_log SET role='assistant' WHERE event_id='source-0'")
            return json.dumps([self.candidate()], ensure_ascii=False), {
                'stop_reason': 'end_turn', 'output_tokens': 100}

        result = self.run_shadow(model, external_edit={'source_id': 'source-0', 'role': 'assistant'})
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['passed_count'], 0)
        self.assertEqual(self.candidate_logs()[0]['reason'], 'source_not_active_or_not_user')

    def test_daily_twenty_batch_budget_is_global_and_survives_reload(self):
        for number in range(20):
            character = 'c' if number % 2 == 0 else 'd'
            user = 'u' if number % 4 < 2 else 'v'
            self.seed(8, prefix=f'batch-{number}', user=user, character=character)
            if number == 10:
                self.shadow = importlib.reload(self.shadow)
            result = self.run_shadow(self.model([]), user=user, character=character,
                                     now=self.now + timedelta(seconds=number))
            self.assertEqual(result['status'], 'succeeded', number)
        self.seed(8, prefix='over-quota', user='w', character='d')
        model = self.model([])
        limited = self.run_shadow(model, user='w', character='d', now=self.now + timedelta(minutes=1))
        self.assertEqual(limited['status'], 'daily_limit')
        model.assert_not_called()
        self.assertEqual(len(self.batch_logs()), 20)
        self.assertEqual([row['batch_sequence'] for row in self.batch_logs()], list(range(1, 21)))
        # 16:00 UTC is the next local midnight in the configured UTC+8 zone.
        next_day = self.now.replace(hour=16)
        self.assertEqual(self.run_shadow(model, user='w', character='d', now=next_day)['status'],
                         'succeeded')
        model.assert_called_once()
        self.assertEqual(self.batch_logs()[-1]['batch_sequence'], 1)

    def test_module_version_change_does_not_reset_the_daily_twenty_batch_budget(self):
        for number in range(20):
            self.seed(8, prefix=f'version-budget-{number}')
            result = self.run_shadow(self.model([]), now=self.now + timedelta(seconds=number))
            self.assertEqual(result['status'], 'succeeded', number)
        model = self.model([])
        # A new module version may re-observe sources, but cannot buy another
        # daily provider budget by creating new versioned processing receipts.
        with patch.object(self.shadow, 'MODULE_VERSION', 'candidate_shadow_budget_test_v2'):
            result = self.run_shadow(model, now=self.now + timedelta(minutes=1))
        self.assertEqual(result['status'], 'daily_limit')
        model.assert_not_called()
        self.assertEqual(len(self.batch_logs()), 20)

    def test_uncertain_final_commit_never_emits_a_successful_candidate(self):
        self.seed(8)
        commit = self.database.commit
        calls = []

        def uncertain_commit():
            calls.append(True)
            if len(calls) == 2:
                raise ConnectionError('offline_final_commit_unknown')
            return commit()

        with patch.object(self.database, 'commit', side_effect=uncertain_commit):
            result = self.run_shadow()
        self.assertEqual(result['status'], 'commit_unknown')
        self.assertEqual(self.candidate_logs(), [])
        self.assertFalse(any(row['status'] == 'succeeded' for row in self.batch_logs()))
        self.assert_pending()

    def test_uncertain_final_commit_with_failed_rollback_cannot_report_success(self):
        self.seed(8)
        commit = self.database.commit
        rollback = self.database.rollback
        query = self.database.query
        calls = []

        def uncertain_commit():
            calls.append(True)
            if len(calls) == 2:
                raise ConnectionError('offline_final_commit_unknown')
            return commit()

        def failed_rollback():
            rollback()
            raise ConnectionError('offline_rollback_response_lost')

        def failed_unlock(statement, params=()):
            if 'pg_advisory_unlock' in statement:
                raise ConnectionError('offline_unlock_response_lost')
            return query(statement, params)

        with patch.object(self.database, 'commit', side_effect=uncertain_commit), \
             patch.object(self.database, 'rollback', side_effect=failed_rollback), \
             patch.object(self.database, 'query', side_effect=failed_unlock):
            result = self.run_shadow()
        self.assertEqual(result['status'], 'commit_unknown')
        self.assertEqual(self.candidate_logs(), [])
        self.assertFalse(any(row['status'] == 'succeeded' for row in self.batch_logs()))
        self.assertIn('batch_id', result)

    def test_failed_success_receipt_write_preserves_consecutive_failure_backoff(self):
        self.seed(8)
        failure = Mock(side_effect=RuntimeError('offline_provider_failure'))
        self.assertEqual(self.run_shadow(failure)['status'], 'failed')
        save = self.shadow._save_control
        failed_once = []

        def fail_success_control(cur, control_id, state, **kwargs):
            if state.get('fail_count') == 0 and state.get('inflight') is False and not failed_once:
                failed_once.append(True)
                raise RuntimeError('offline_success_receipt_write_failed')
            return save(cur, control_id, state, **kwargs)

        retry_at = self.now + timedelta(seconds=120)
        with patch.object(self.shadow, '_save_control', side_effect=fail_success_control):
            result = self.run_shadow(now=retry_at)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.candidate_logs(), [])
        model = self.model()
        self.assertEqual(self.run_shadow(model,
            now=retry_at + timedelta(seconds=599))['status'], 'cooldown')
        model.assert_not_called()
        self.assertEqual(self.run_shadow(model,
            now=retry_at + timedelta(seconds=600))['status'], 'succeeded')

    def test_only_accepted_sanitized_perspective_text_is_logged(self):
        self.seed(8)
        safe = '我会记住这件事'
        unsafe = '她其实很在意'
        self.run_shadow(self.model([
            self.candidate(perspective=safe),
            self.candidate('source-1', perspective=unsafe),
            self.candidate('source-2', quote='原文中没有这句话', perspective=unsafe),
        ]))
        accepted, dropped, rejected = self.candidate_logs()
        self.assertTrue(accepted['accepted'])
        self.assertEqual(accepted['perspective'], safe)
        self.assertFalse(accepted['perspective_empty'])
        self.assertTrue(dropped['accepted'])
        self.assertEqual(dropped['reason'], 'perspective_dropped')
        self.assertIsNone(dropped['perspective'])
        self.assertFalse(rejected['accepted'])
        self.assertIsNone(rejected['perspective'])
        self.assertNotIn(unsafe, json.dumps(self.logs, ensure_ascii=False))

    def test_shadow_log_is_json_serializable_and_contains_every_gate(self):
        quote = '今天我亏了69元，下午坐在窗边整理最近买回来的小说和书本，然后给朋友写了一封很长的信。'
        self.seed(8, text=quote)
        self.run_shadow(self.model([self.candidate(quote=quote, perspective='她其实很在意')]))
        row = self.candidate_logs()[0]
        self.assertEqual(row['quote'], quote[:40])
        self.assertEqual(set(row['gates']), {f'G{index}' for index in range(1, 10)})
        self.assertFalse(row['perspective_output_empty'])
        self.assertTrue(row['perspective_empty'])
        self.assertNotIn('她其实很在意', json.dumps(self.logs, ensure_ascii=False))
        for item in self.logs:
            self.assertEqual(json.loads(json.dumps(item, ensure_ascii=False)), item)
            for field in ('module_version', 'batch_id', 'batch_sequence', 'user_id', 'character_id'):
                self.assertIn(field, item)


if __name__ == '__main__':
    unittest.main()
