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

    def test_structured_output_preserves_batch_schema_and_failure_codes(self):
        from structured_output import invoke_structured_llm, StructuredParseResult
        candidate = dict(source_event_id='source-0', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact',
                         object='交易结果', value='亏了69元')
        invalid_shapes = [({}, 'root_not_array'),
                          ([candidate, None], 'item_1_not_object'),
                          ([0], 'item_0_not_object')]
        for field in sorted(producer._REQUIRED):
            invalid_shapes.append(([{key: value for key, value in candidate.items()
                                     if key != field}], f'item_0_missing_{field}'))
        for field in ('extra_field', 'z_field'):
            invalid_shapes.append(([dict(candidate, **{field: True})],
                                   f'item_0_unknown_field_{field}'))
        invalid_shapes.extend((
            ([{}], 'item_0_missing_object'),
            ([dict(candidate, a_field=1, z_field=2)], 'item_0_unknown_field_a_field'),
        ))
        for value, detail in invalid_shapes:
            with self.subTest(detail=detail):
                with self.assertRaises(producer._CandidateBatchSchemaError) as error:
                    producer._validate_candidate_batch(value)
                self.assertEqual(error.exception.detail, detail)
                self.assertEqual(str(error.exception), detail)
        # This validator owns shape only; the existing contract owns content.
        for value in ([], [candidate], [dict(candidate, predicate='invented',
                      source_event_id='outside-batch', quote=None,
                      summary=[], time_scope=123, perspective={})]):
            with self.subTest(value=value):
                self.assertIs(producer._validate_candidate_batch(value), value)
        for text, stop, expected in (
                ('[]', 'end_turn', None), ('[]', 'stop', None),
                ('[]', 'max_tokens', 'truncated_response'),
                ('[]', 'length', 'truncated_response'),
                ('[]', 'refusal', 'incomplete_response'),
                ('[]', 'content_filter', 'incomplete_response'),
                ('[]', 'tool_use', 'incomplete_response'),
                ('[]', 'pause_turn', 'incomplete_response'),
                ('[]', None, 'incomplete_response'),
                ('[]', 'unknown', 'incomplete_response'),
                ('not JSON', 'end_turn', 'invalid_json'),
                ('', 'end_turn', 'invalid_json'),
                ('[] [1]', 'end_turn', 'invalid_json'),
                ('{}', 'end_turn', 'invalid_batch_shape'),
                ('[{}]', 'end_turn', 'invalid_batch_shape')):
            with self.subTest(text=text, stop=stop):
                call = invoke_structured_llm(
                    domain='candidate_producer',
                    create_chat_fn=Mock(return_value=(text, {'stop_reason': stop})),
                    model='offline-model', messages=[],
                    schema_validator=producer._validate_candidate_batch,
                    schema_name='candidate_producer_batch', root_type='array')
                if expected is None:
                    self.assertTrue(call.parsed.ok)
                    self.assertEqual(call.parsed.value, [])
                else:
                    self.assertEqual(producer._parse_error_to_code(call.parsed, stop),
                                     expected)
        for code, expected in (('truncated_response', 'truncated_response'),
                               ('model_refused', 'incomplete_response'),
                               ('schema_validation_failed', 'invalid_batch_shape'),
                               ('root_not_array', 'invalid_batch_shape')):
            with self.subTest(code=code):
                self.assertEqual(producer._parse_error_to_code(
                    StructuredParseResult(error_code=code), 'end_turn'), expected)

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
        from structured_output import invoke_structured_llm
        source_fixture.CandidateSourceTests.setUp(self)
        self.structured_call = self.stack.enter_context(patch(
            'structured_output.invoke_structured_llm', wraps=invoke_structured_llm))
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

    def run_pair(self, model=None, *, now=None, character='c', user='u', shadow=True):
        return producer.run_pair(
            user, character, now=now or self.now,
            create_chat_fn=model if model is not None else self.model(),
            conn=self.database, emit=self.logs.append, enabled=True, shadow=shadow)

    def candidate_logs(self):
        return [row for row in self.logs if row.get('kind') == 'candidate']

    def non_processing_state(self):
        return {key: value for key, value in self.database_state().items()
                if key != 'event_processing_state'}

    def processing_rows(self, *, user='u', shadow=True):
        return self.sql('''SELECT status,retry_count,last_error,consumed_at
            FROM event_processing_state WHERE processor_type=%s
            AND processor_version=%s ORDER BY event_id''',
            (producer._processor(user, 'c'), producer._receipt_version(shadow)))

    def control_state(self, user='u'):
        return producer._load_control(self.database.cursor(),
            'candidate-producer:user:' + producer._digest(user))

    def set_batch_size(self, size, user='u', *, solo_failures=None):
        state = {'batch_size': size}
        if solo_failures is not None:
            state['solo_failures'] = solo_failures
        producer._save_control(self.database.cursor(),
            'candidate-producer:user:' + producer._digest(user), state)
        self.database.commit()

    def test_historical_batch_retries_do_not_skip_first_solo_parse_failure(self):
        ids = self.seed(1, age=300)
        self.set_batch_size(1)
        producer._reserve_sources(self.database.cursor(),
            [{'source_event_id': ids[0]}], producer._processor('u', 'c'),
            producer._receipt_version(True))
        self.sql('UPDATE event_processing_state SET retry_count=48 WHERE event_id=%s',
                 (ids[0],))
        self.database.commit()
        before = self.non_processing_state()
        result = self.run_pair(self.model(raw='not JSON'))
        self.assertEqual((result['status'], result['reason']), ('failed', 'invalid_json'))
        self.assertEqual(self.processing_rows(), [('pending', 49, 'invalid_json', None)])
        self.assertEqual(self.control_state()['solo_failures'], {ids[0]: 1})
        self.assertFalse(any(record.get('kind') == 'source_skipped' for record in self.logs))
        self.assertEqual(self.non_processing_state(), before)

    def test_batch_parse_failure_does_not_increment_existing_solo_failures(self):
        ids = self.seed(2, age=300)
        counts = {ids[0]: 2, ids[1]: 1}
        self.set_batch_size(2, solo_failures=counts)
        before = self.non_processing_state()
        result = self.run_pair(self.model(raw='not JSON'))
        self.assertEqual((result['status'], result['batch_size']), ('failed', 2))
        self.assertEqual(self.control_state()['solo_failures'], counts)
        self.assertEqual(self.processing_rows(), [('pending', 1, 'invalid_json', None)] * 2)
        self.assertEqual(self.non_processing_state(), before)
        self.assertFalse(any(record.get('kind') == 'source_skipped' for record in self.logs))

    def test_success_clears_only_completed_sources_from_solo_failures(self):
        ids = self.seed(3, age=300)
        self.set_batch_size(2, solo_failures={ids[0]: 1, ids[1]: 2, ids[2]: 1})
        before = self.non_processing_state()
        result = self.run_pair(self.model(raw='[]'))
        self.assertEqual((result['status'], result['batch_size']), ('succeeded', 2))
        self.assertEqual(self.control_state()['solo_failures'], {ids[2]: 1})
        self.assertEqual(self.control_state()['batch_size'], 4)
        self.assertEqual([row[:3] for row in self.processing_rows()],
                         [('succeeded', 0, None)] * 2)
        self.assertEqual(self.non_processing_state(), before)

    def test_success_restores_batch_size_by_one_step_up_to_limit(self):
        for size, expected in ((0, 2), (1, 2), (4, 8), (8, 8)):
            user = 'restore-' + str(size)
            with self.subTest(batch_size=size):
                self.seed(age=300, user=user, prefix=user)
                self.set_batch_size(size, user)
                result = self.run_pair(self.model(raw='[]'), user=user)
                self.assertEqual((result['status'], result['batch_size']), ('succeeded', size))
                self.assertEqual(self.control_state(user)['batch_size'], expected)

    def test_parse_failures_reduce_batches_to_one_without_authoritative_writes(self):
        cases = (('invalid_json', '[{"summary":"private malformed "quote" text"}]', 'end_turn'),
                 ('invalid_batch_shape', '[{}]', 'end_turn'),
                 ('incomplete_response', '[]', 'pause_turn'),
                 ('truncated_response', '[]', 'max_tokens'))
        for shadow in (False, True):
            for reason, raw, stop in cases:
                user = reason + str(shadow)
                with self.subTest(reason=reason, shadow=shadow):
                    ids = self.seed(user=user, prefix=user, age=300)
                    before = self.non_processing_state()
                    model = self.model(raw=raw, stop_reason=stop)
                    with patch.object(producer, '_process_candidates') as process, \
                            patch.object(producer, 'adjudicate_and_project') as adjudicate, \
                            patch.object(producer, 'DAILY_LIMIT', 100):
                        for index, size in enumerate((4, 2, 1, 1, 1, 1)):
                            result = self.run_pair(model, user=user, shadow=shadow,
                                now=self.now + timedelta(seconds=index * 1801))
                            self.assertEqual((result['status'], result['reason']),
                                             ('failed', reason))
                            self.assertEqual(result['batch_size'], (8, 4, 2, 1, 1, 1)[index])
                            self.assertEqual(self.control_state(user)['batch_size'], size)
                            self.assertEqual(self.control_state(user).get('solo_failures', {}),
                                {ids[0]: index - 2} if 3 <= index < 5 else {})
                            skipped = self.sql('''SELECT event_id FROM event_processing_state
                                WHERE processor_type=%s AND status='skipped' ''',
                                (producer._processor(user, 'c'),))
                            self.assertEqual(skipped, [(ids[0],)] if index == 5 else [])
                    process.assert_not_called()
                    adjudicate.assert_not_called()
                    self.assertEqual(self.non_processing_state(), before)
                    self.assertEqual(self.candidate_logs(), [])

    def test_single_source_threshold_is_audited_and_later_sources_progress(self):
        ids = self.seed(3, age=300)
        self.set_batch_size(1)
        before = self.non_processing_state()
        failed = self.model(raw='[{"summary":"private malformed "quote" text"}]')
        for count, seconds in enumerate((0, 31, 92), start=1):
            result = self.run_pair(failed, now=self.now + timedelta(seconds=seconds))
            self.assertEqual((result['status'], result['reason']), ('failed', 'invalid_json'))
            row = self.sql('''SELECT status,retry_count,last_error,consumed_at
                FROM event_processing_state WHERE event_id=%s AND processor_type=%s''',
                (ids[0], producer._processor('u', 'c')))[0]
            self.assertEqual(row, ('skipped' if count == 3 else 'pending',
                                  count, 'invalid_json', None))
            self.assertEqual(self.control_state().get('solo_failures', {}),
                             {} if count == 3 else {ids[0]: count})
        skipped = [record for record in self.logs if record.get('kind') == 'source_skipped']
        self.assertEqual(len(skipped), 1)
        audit = skipped[0]
        self.assertEqual((audit['source_event_id'], audit['retry_count'], audit['last_error']),
                         (ids[0], 3, 'invalid_json'))
        self.assertFalse({'content', 'quote', 'summary', 'value'} & audit.keys())
        self.assertNotIn('private malformed', json.dumps(self.logs))
        self.assertNotIn('今天我亏了69元', json.dumps(self.logs, ensure_ascii=False))
        self.assertEqual(self.non_processing_state(), before)
        pending = producer._pending_inputs(self.database.cursor(), 'u', 'c',
                                           producer._receipt_version(True))
        self.assertEqual([item['source_event_id'] for item in pending], ids[1:])
        self.database.rollback()
        recovered = self.model(raw='[]')
        self.assertEqual(self.run_pair(recovered,
            now=self.now + timedelta(seconds=1893))['status'], 'succeeded')
        request = json.loads(recovered.call_args.kwargs['messages'][0]['content'])
        self.assertEqual([item['source_event_id'] for item in request], ids[1:2])
        self.assertEqual(self.control_state()['batch_size'], 2)
        reprocessable = producer._pending_inputs(self.database.cursor(), 'u', 'c',
                                                 'next-processor-version.shadow')
        self.assertEqual([item['source_event_id'] for item in reprocessable], ids)
        self.assertEqual(self.non_processing_state(), before)

    def test_non_parse_errors_never_shrink_or_isolate_sources(self):
        for stage in ('model_call', 'validation', 'persistence'):
            with self.subTest(stage=stage):
                self.seed(1, age=300, user=stage, prefix=stage)
                counts = {stage + '-0': 2}
                self.set_batch_size(4, stage, solo_failures=counts)
                before = self.non_processing_state()
                model = self.model(raw='[]')
                error = producer.BatchOutputError('invalid_json')
                if stage == 'model_call':
                    model.side_effect = error
                with patch.object(producer, '_process_candidates',
                        side_effect=error if stage == 'validation' else None,
                        return_value=[]), patch.object(producer, '_mark_sources_done',
                        side_effect=error if stage == 'persistence' else None):
                    for index in range(3):
                        result = self.run_pair(model, user=stage, shadow=False,
                            now=self.now + timedelta(seconds=index * 1801))
                        self.assertEqual((result['status'], result['stage']), ('failed', stage))
                        self.assertEqual(self.control_state(stage)['batch_size'], 4)
                        self.assertEqual(self.control_state(stage)['solo_failures'], counts)
                self.assertEqual(self.processing_rows(user=stage, shadow=False),
                                 [('pending', 3, 'invalid_json', None)])
                self.assertEqual(self.non_processing_state(), before)
        self.assertFalse(any(record.get('kind') == 'source_skipped' for record in self.logs))

    def test_failed_success_state_save_does_not_restore_batch_size(self):
        ids = self.seed(1, age=300)
        self.set_batch_size(1, solo_failures={ids[0]: 2})
        save = producer._save_control

        def fail_completion(cur, event_id, state):
            if event_id.startswith('candidate-producer:user:') and state.get('fail_count') == 0:
                raise RuntimeError('completion state not saved')
            return save(cur, event_id, state)

        before = self.non_processing_state()
        with patch.object(producer, '_save_control', side_effect=fail_completion):
            result = self.run_pair(self.model(raw='[]'))
        self.assertEqual((result['status'], result['stage']), ('failed', 'persistence'))
        self.assertEqual(self.control_state()['batch_size'], 1)
        self.assertEqual(self.control_state()['fail_count'], 1)
        self.assertEqual(self.control_state()['solo_failures'], {ids[0]: 2})
        self.assertEqual(self.processing_rows(), [('pending', 1, 'RuntimeError', None)])
        self.assertEqual(self.non_processing_state(), before)

    def test_source_skip_audit_follows_commit_and_failed_commit_emits_no_skip(self):
        for fail_commit in (False, True):
            user = 'commit-' + str(fail_commit)
            with self.subTest(fail_commit=fail_commit):
                ids = self.seed(1, age=300, user=user, prefix=user)
                self.set_batch_size(1, user, solo_failures={ids[0]: 2})
                producer._reserve_sources(self.database.cursor(),
                    [{'source_event_id': ids[0]}], producer._processor(user, 'c'),
                    producer._receipt_version(True))
                self.sql('UPDATE event_processing_state SET retry_count=2 WHERE event_id=%s',
                         (ids[0],))
                self.database.commit()
                commit = self.database.commit
                commits, audited = [], []

                def maybe_commit():
                    if fail_commit and len(commits) == 1:
                        raise RuntimeError('failure receipt not committed')
                    commit()
                    commits.append('committed')

                def emit(record):
                    if record['kind'] == 'source_skipped':
                        self.assertEqual(len(commits), 2)
                        audited.append(record)

                def run():
                    return producer.run_pair(user, 'c', now=self.now,
                        create_chat_fn=self.model(raw='not JSON'), conn=self.database,
                        emit=emit, enabled=True, shadow=True)

                before = self.non_processing_state()
                with patch.object(self.database, 'commit', side_effect=maybe_commit):
                    if fail_commit:
                        with self.assertRaises(RuntimeError):
                            run()
                    else:
                        self.assertEqual(run()['status'], 'failed')
                self.assertEqual(len(audited), 0 if fail_commit else 1)
                row = self.processing_rows(user=user)[0]
                self.assertEqual(row[:2], ('pending', 2) if fail_commit else ('skipped', 3))
                self.assertEqual(self.control_state(user)['solo_failures'],
                                 {ids[0]: 2} if fail_commit else {})
                self.assertEqual(self.non_processing_state(), before)

    def assert_parse_failure(self, raw, reason, *, stop_reason='end_turn',
                             user='u', shadow=False):
        self.seed(prefix=user, user=user)
        before = self.non_processing_state()
        model = self.model(raw=raw, stop_reason=stop_reason)
        with patch.object(producer, '_process_candidates',
                          wraps=producer._process_candidates) as process:
            result = self.run_pair(model, user=user, shadow=shadow)
        model.assert_called_once()
        process.assert_not_called()
        self.assertEqual((result['status'], result['stage'], result['reason']),
                         ('failed', 'parse', reason))
        self.assertEqual(result['candidates_produced'], 0)
        self.assertEqual(result['shadow'], shadow)
        rows = self.processing_rows(user=user, shadow=shadow)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row == ('pending', 1, reason, None) for row in rows))
        self.assertEqual(self.non_processing_state(), before)
        self.assertEqual(self.candidate_logs(), [])
        return result

    def assert_parsed_candidates(self, raw):
        self.seed()
        with patch.object(producer, '_process_candidates',
                          wraps=producer._process_candidates) as process:
            result = self.run_pair(self.model(raw=raw))
        process.assert_called_once()
        self.assertEqual(process.call_args.args[3], [self.candidate()])
        self.assertEqual((result['status'], result['candidates_produced'],
                          result['accepted']), ('succeeded', 1, 1))
        self.assertEqual(len(self.processing_rows()), 8)
        self.assertTrue(all(row[0] == 'succeeded' for row in self.processing_rows()))
        self.assertTrue(self.candidate_logs()[0]['accepted'])

    def test_structured_01_pure_array_uses_default_chat_and_original_request(self):
        self.seed()
        model = self.model()
        tagged_model = '[冰咖啡]claude-opus-4-6'
        with patch('config.MODEL_MAIN', tagged_model), \
                patch('ai_client.create_chat', model), \
                patch.object(producer, '_process_candidates',
                             wraps=producer._process_candidates) as process:
            result = producer.run_pair('u', 'c', now=self.now, conn=self.database,
                emit=self.logs.append, enabled=True, shadow=True)
        process.assert_called_once()
        model.assert_called_once()
        request = model.call_args.kwargs
        self.assertEqual(request['model'], tagged_model)
        self.assertEqual(request['max_tokens'], 2000)
        self.assertEqual(request['max_retries'], 0)
        self.assertFalse({'tools', 'tool_choice', 'input_schema'} & request.keys())
        self.assertEqual(request['system'], producer._SYSTEM % ', '.join(
            sorted(producer._PREDICATES)))
        self.assertEqual(len(json.loads(request['messages'][0]['content'])), 8)
        self.structured_call.assert_called_once()
        structured = self.structured_call.call_args.kwargs
        self.assertEqual(structured['domain'], 'candidate_producer')
        self.assertEqual(structured['schema_name'], 'candidate_producer_batch')
        self.assertEqual(structured['root_type'], 'array')
        self.assertIs(structured['schema_validator'], producer._validate_candidate_batch)
        self.assertEqual((result['status'], result['accepted']), ('succeeded', 1))
        self.assertEqual(process.call_args.args[3], [self.candidate()])
        self.seed(prefix='fallback', user='fallback')
        fallback = Mock(return_value=(json.dumps([]), {'finish_reason': 'stop'}))
        self.assertEqual(self.run_pair(fallback, user='fallback')['status'], 'succeeded')
        self.assertTrue(all(row[0] == 'succeeded'
                            for row in self.processing_rows(user='fallback')))

    def test_structured_02_markdown_fence_reaches_existing_candidate_gates(self):
        raw = json.dumps([self.candidate()], ensure_ascii=False)
        self.assert_parsed_candidates('```json\n' + raw + '\n```')

    def test_structured_03_prose_before_and_after_fence_is_extracted(self):
        raw = json.dumps([self.candidate()], ensure_ascii=False)
        self.assert_parsed_candidates('以下是候选：\n```json\n' + raw + '\n```\n以上。')

    def test_structured_04_tilde_and_nested_fences_are_extracted(self):
        raw = json.dumps([self.candidate()], ensure_ascii=False)
        for index, wrapped in enumerate(('~~~\n' + raw + '\n~~~',
                '```markdown\n```json\n' + raw + '\n```\n```')):
            with self.subTest(wrapper=index):
                user = 'wrapper-' + str(index)
                ids = self.seed(prefix=user, user=user)
                content = wrapped.replace('source-0', ids[0])
                result = self.run_pair(self.model(raw=content), user=user)
                self.assertEqual((result['status'], result['accepted']), ('succeeded', 1))
                self.assertTrue(all(row[0] == 'succeeded'
                                    for row in self.processing_rows(user=user)))

    def test_structured_05_empty_array_completes_sources_without_retry(self):
        self.seed()
        model = self.model(raw='[]')
        result = self.run_pair(model)
        self.assertEqual((result['status'], result['candidates_produced']), ('succeeded', 0))
        self.assertEqual(self.candidate_logs(), [])
        self.assertTrue(all(row[0:3] == ('succeeded', 0, None)
                            for row in self.processing_rows()))
        self.assertEqual(self.run_pair(model, now=self.now + timedelta(seconds=300))['status'],
                         'waiting')
        model.assert_called_once()

    def test_structured_06_missing_field_fails_schema_before_processing(self):
        self.assert_parse_failure('[{"source_event_id":"e1","quote":"..."}]',
                                  'invalid_batch_shape')

    def test_structured_07_extra_field_fails_schema_before_processing(self):
        raw = json.dumps([self.candidate(extra_field='unexpected')], ensure_ascii=False)
        self.assert_parse_failure(raw, 'invalid_batch_shape')

    def test_structured_08_object_root_maps_to_invalid_batch_shape(self):
        raw = json.dumps({'candidates': [self.candidate()]}, ensure_ascii=False)
        self.assert_parse_failure(raw, 'invalid_batch_shape')

    def test_structured_09_truncation_stays_pending_for_both_provider_reasons(self):
        for stop in ('max_tokens', 'length'):
            with self.subTest(stop=stop):
                self.assert_parse_failure('[]', 'truncated_response',
                                          stop_reason=stop, user=stop)

    def test_structured_10_refusal_and_incomplete_stops_cannot_consume_sources(self):
        for index, stop in enumerate(('refusal', 'content_filter', 'tool_use',
                                      'pause_turn', None, 'unknown')):
            with self.subTest(stop=stop):
                self.assert_parse_failure('[]', 'incomplete_response',
                                          stop_reason=stop, user=f'stop-{index}')

    def test_structured_11_one_malformed_item_rejects_the_whole_batch(self):
        for index, item in enumerate((None, {'source_event_id': 'e1'})):
            with self.subTest(item=item):
                raw = json.dumps([self.candidate(), item], ensure_ascii=False)
                self.assert_parse_failure(raw, 'invalid_batch_shape', user=f'mixed-{index}')

    def test_structured_12_unknown_predicate_only_rejects_its_candidate(self):
        ids = self.seed()
        candidates = [self.candidate(ids[0], predicate='invented_relationship_judgment'),
                      self.candidate(ids[1])]
        self.assertEqual(producer._validate_candidate_batch(candidates), candidates)
        result = self.run_pair(self.model(candidates))
        self.assertEqual((result['status'], result['candidates_produced'],
                          result['accepted'], result['rejected']), ('succeeded', 2, 1, 1))
        rows = self.candidate_logs()
        self.assertEqual(rows[0]['reason'], 'invalid_predicate_or_summary')
        self.assertFalse(rows[0]['accepted'])
        self.assertTrue(rows[1]['accepted'])
        self.assertTrue(all(row[0] == 'succeeded' for row in self.processing_rows()))

    def test_structured_13_invalid_json_retries_pending_sources_then_recovers(self):
        self.seed()
        failed = self.model(raw='not JSON')
        result = self.run_pair(failed)
        self.assertEqual((result['status'], result['reason']), ('failed', 'invalid_json'))
        self.assertTrue(all(row == ('pending', 1, 'invalid_json', None)
                            for row in self.processing_rows()))
        self.assertEqual(self.run_pair(failed, now=self.now + timedelta(seconds=29))['status'],
                         'cooldown')
        failed.assert_called_once()
        recovered = self.model(raw='[]')
        result = self.run_pair(recovered, now=self.now + timedelta(seconds=31))
        self.assertEqual(result['status'], 'succeeded')
        recovered.assert_called_once()
        self.assertEqual(len(json.loads(recovered.call_args.kwargs['messages'][0]['content'])), 4)
        self.assertEqual([row[0:3] for row in self.processing_rows()],
                         [('succeeded', 1, None)] * 4 + [('pending', 1, 'invalid_json')] * 4)
        self.assertEqual(self.control_state()['batch_size'], producer.BATCH_SIZE)

    def test_structured_14_three_parse_failures_pause_then_recover_after_cooldown(self):
        self.seed()
        failed = self.model(raw='not JSON')
        for count, seconds in enumerate((0, 31, 92), start=1):
            result = self.run_pair(failed, now=self.now + timedelta(seconds=seconds))
            self.assertEqual((result['status'], result['reason']), ('failed', 'invalid_json'))
            expected_counts = [sum(index < size for size in (8, 4, 2)[:count])
                               for index in range(8)]
            self.assertEqual(self.processing_rows(),
                [('pending', retries, 'invalid_json', None) for retries in expected_counts])
        self.assertEqual(failed.call_count, 3)
        self.assertEqual(self.run_pair(failed, now=self.now + timedelta(seconds=1891))['status'],
                         'cooldown')
        self.assertEqual(failed.call_count, 3)
        recovered = self.model(raw='[]')
        result = self.run_pair(recovered, now=self.now + timedelta(seconds=1893))
        self.assertEqual(result['status'], 'succeeded')
        recovered.assert_called_once()
        self.assertEqual([row[0:3] for row in self.processing_rows()],
            [('succeeded', 3, None), ('pending', 3, 'invalid_json')]
            + [('pending', 2, 'invalid_json')] * 2 + [('pending', 1, 'invalid_json')] * 4)
        self.assertEqual(self.control_state()['batch_size'], 2)
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])

    def test_structured_15_schema_failure_is_identical_in_live_and_shadow(self):
        for shadow in (False, True):
            with self.subTest(shadow=shadow):
                result = self.assert_parse_failure('[{}]', 'invalid_batch_shape',
                    user='shadow-' + str(shadow), shadow=shadow)
                self.assertEqual(result['reasons'], {'invalid_batch_shape': 1})
                self.assertEqual(self.logs[-1]['shadow'], shadow)

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
        self.assertTrue(all(row[:2] == ('pending', 0) for row in self.processing_rows()))
        self.assertFalse(any(row.get('kind') == 'source_skipped' for row in self.logs))
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
        self.assertTrue(all(row[:2] == ('succeeded', 0) for row in self.processing_rows()))
        self.assertFalse(any(row.get('kind') == 'source_skipped' for row in self.logs))
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
