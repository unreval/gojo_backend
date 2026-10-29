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
import candidate_audit
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
                         object='交易结果', value='亏了69元',
                         interpretation='她这是在报亏损，不是想让我算账')
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
        without_reading = {key: value for key, value in candidate.items()
                           if key != 'interpretation'}
        for value in ([], [candidate], [without_reading],
                      [dict(candidate, predicate='invented',
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

    def test_prompt_keeps_the_character_line_out_of_quote_and_summary(self):
        prompt = producer._SYSTEM
        self.assertIn('prior_character_text', prompt)
        self.assertIn('只用于判断用户这句话的性质', prompt)
        self.assertIn('不得引用', prompt)
        self.assertIn('不得写进 quote 或 summary', prompt)
        self.assertIn('不得当成用户事实', prompt)
        self.assertIn('quote 只能从用户的 content 逐字截取', prompt)
        self.assertEqual(prompt.count('%s'), 1)

    def test_model_payload_omits_context_unless_a_character_line_exists(self):
        item = {'source_event_id': 'source-0', 'content': '想和你聊一聊',
                'occurred_at': self.now, 'context_excerpt': None}
        self.assertNotIn('prior_character_text', producer._model_payload(item))
        long_line = '请用这个句型造一个完整的句子' * 40
        self.assertGreater(len(long_line), producer._CONTEXT_EXCERPT_CHARS)
        excerpt = long_line[:producer._CONTEXT_EXCERPT_CHARS]
        payload = producer._model_payload(dict(item, context_excerpt=excerpt))
        self.assertEqual(payload['prior_character_text'], excerpt)
        self.assertEqual(len(payload['prior_character_text']),
                         producer._CONTEXT_EXCERPT_CHARS)
        self.assertEqual(payload['content'], '想和你聊一聊')
        self.assertIsNone(producer._context_excerpt(''))
        self.assertIsNone(producer._context_excerpt(None))
        self.assertEqual(producer._context_excerpt(long_line), excerpt)
        self.assertEqual(
            producer._format_character_bubble('練習だ', '这是练习'),
            '練習だ\n中文：这是练习')
        self.assertEqual(producer._format_character_bubble('練習だ', ''), '練習だ')
        self.assertEqual(producer._format_character_bubble('練習だ', None), '練習だ')
        self.assertIsNone(producer._format_character_bubble('', '译文'))

    def test_echo_context_keys_are_tolerated_without_widening_unknown_fields(self):
        candidate = dict(source_event_id='source-0', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact',
                         object='交易结果', value='亏了69元')
        for key in ('prior_character_text', 'context_excerpt'):
            with self.subTest(key=key):
                echoed = dict(candidate, **{key: '模型回声'})
                producer._validate_candidate_batch([echoed])
                self.assertIsNone(producer.candidate_contract_reason(
                    echoed, {'source-0'}))
        both = dict(candidate, prior_character_text='回声A', context_excerpt='回声B')
        producer._validate_candidate_batch([both])
        self.assertIsNone(producer.candidate_contract_reason(both, {'source-0'}))
        with self.assertRaises(producer._CandidateBatchSchemaError) as raised:
            producer._validate_candidate_batch([dict(candidate, foo='x')])
        self.assertEqual(raised.exception.detail, 'item_0_unknown_field_foo')

    def test_prompt_keeps_japanese_fragments_and_keeps_inference_out_of_summary(self):
        prompt = producer._SYSTEM
        summary, interpretation = prompt.split(
            'interpretation是你对这件事的主观理解', 1)
        self.assertIn('原话是日语时，summary必须原样保留日语关键片段', summary)
        self.assertIn('她说「ふくしゅうしたくない」，不想复习', summary)
        self.assertIn('不准写你的判断、情绪归因或动机推测', summary)
        self.assertIn('表示不满', summary)
        self.assertIn('她自认是初学者', summary)
        self.assertIn('她觉得之前的内容太难，不适合初学者', summary)
        self.assertNotIn('ふくしゅうしたくない', interpretation)
        self.assertNotIn('表示不满', interpretation)
        self.assertIn('不要求客观正确', interpretation)

    def test_prompt_requests_a_subjective_interpretation(self):
        prompt = producer._SYSTEM
        self.assertIn('interpretation是你对这件事的主观理解，必须给出，一句话', prompt)
        self.assertIn('不要求客观正确', prompt)
        self.assertIn('不确定就说不确定，不要硬下结论', prompt)
        self.assertIn('不要复述用户原话，要说你怎么看这件事', prompt)
        self.assertIn('所有字段值必须是合法JSON字符串', prompt)
        self.assertNotIn('interpretation?', prompt)

    def test_interpretation_is_optional_and_only_type_checked(self):
        candidate = dict(source_event_id='source-0', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact',
                         object='交易结果', value='亏了69元',
                         interpretation='她其实是在抱怨室友，不是真的想搬家')
        self.assertIsNone(producer.candidate_contract_reason(candidate, {'source-0'}))
        omitted = {key: value for key, value in candidate.items()
                   if key != 'interpretation'}
        self.assertIsNone(producer.candidate_contract_reason(omitted, {'source-0'}))
        self.assertEqual(producer.candidate_contract_reason(
            dict(candidate, interpretation=12), {'source-0'}),
            'invalid_candidate_fields')

    def test_prompt_asks_for_a_retention_tier_as_an_optional_field(self):
        prompt = producer._SYSTEM
        self.assertIn('persistence?', prompt)
        self.assertIn('persistence只能是durable或transient', prompt)
        self.assertIn('不确定就省略该字段', prompt)
        self.assertIn('这个字段只用于观测，不影响是否记住', prompt)

    def test_prompt_does_not_exclude_transient_facts_from_extraction(self):
        # The tier can only be measured on candidates that are produced. An
        # opening line that asks for 值得长期记住 alone suppresses the transient
        # ones, so the shadow sample would never contain them.
        prompt = producer._SYSTEM
        self.assertIn('也包括当下状态', prompt)
        self.assertIn('不要因为一件事只是当下状态就不提取', prompt)
        self.assertNotIn('提取值得长期记住的自述事实', prompt)
        # Pure filler is still excluded, so the change widens facts only.
        self.assertIn('纯寒暄、语气词、无法确认的内容和重复候选不提取', prompt)

    def test_persistence_never_rejects_a_candidate_whatever_its_value(self):
        # Observation only: an absent, misspelled or non-string tier must not
        # change any gate outcome, so the shadow measurement cannot cost
        # candidates that would otherwise be accepted.
        candidate = dict(source_event_id='source-0', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact',
                         object='交易结果', value='亏了69元')
        for tier in ('durable', 'transient', 'maybe', '', 12, None):
            with self.subTest(persistence=tier):
                self.assertIsNone(producer.candidate_contract_reason(
                    dict(candidate, persistence=tier), {'source-0'}))
                producer._validate_candidate_batch([dict(candidate, persistence=tier)])
        self.assertIsNone(producer.candidate_contract_reason(candidate, {'source-0'}))

    def test_unknown_fields_other_than_persistence_still_fail_the_batch(self):
        candidate = dict(source_event_id='source-0', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact',
                         object='交易结果', value='亏了69元')
        with self.assertRaises(producer._CandidateBatchSchemaError) as raised:
            producer._validate_candidate_batch([dict(candidate, foo='x')])
        self.assertEqual(raised.exception.detail, 'item_0_unknown_field_foo')

    def test_observed_persistence_folds_everything_unknown_to_none(self):
        # The schema does not type check this field, so the audit record must
        # never raise on it. An unhashable value used to raise TypeError inside
        # _process_candidates, which fails the whole batch without skipping the
        # sources, so the batch retried until the daily limit was spent.
        for tier in ('durable', 'transient'):
            with self.subTest(persistence=tier):
                self.assertEqual(
                    producer.observed_persistence({'persistence': tier}), tier)
        for tier in ('maybe', 'Durable', '', 12, 0, True, None,
                     {'a': 1}, ['durable'], ('durable',)):
            with self.subTest(persistence=tier):
                self.assertIsNone(producer.observed_persistence({'persistence': tier}))
        self.assertIsNone(producer.observed_persistence({}))
        self.assertIsNone(producer.observed_persistence(None))

    def test_prompt_records_utterance_mode_without_using_it_to_decide(self):
        prompt = producer._SYSTEM
        self.assertIn('utterance_mode?', prompt)
        self.assertIn('utterance_mode只能是sincere、practice或unsure', prompt)
        self.assertIn('照着打的', prompt)
        self.assertIn('不知道什么意思', prompt)
        self.assertIn('这样说对吗', prompt)
        self.assertIn('ぼく', prompt)
        self.assertIn('判断不了就写unsure，不要硬下结论', prompt)
        self.assertIn('这个字段只用于观测，不影响是否记住', prompt)
        self.assertEqual(prompt.count('%s'), 1)

    def test_utterance_mode_is_optional_and_never_type_checked(self):
        candidate = dict(source_event_id='source-0', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact',
                         object='交易结果', value='亏了69元')
        for mode in ('sincere', 'practice', 'unsure', 'maybe', '', 12, None):
            with self.subTest(utterance_mode=mode):
                self.assertIsNone(producer.candidate_contract_reason(
                    dict(candidate, utterance_mode=mode), {'source-0'}))
                producer._validate_candidate_batch(
                    [dict(candidate, utterance_mode=mode)])
        self.assertIsNone(producer.candidate_contract_reason(candidate, {'source-0'}))

    def test_observed_utterance_mode_folds_everything_unknown_to_none(self):
        for mode in ('sincere', 'practice', 'unsure'):
            with self.subTest(utterance_mode=mode):
                self.assertEqual(
                    producer.observed_utterance_mode({'utterance_mode': mode}), mode)
        for mode in ('maybe', 'Sincere', '', 12, 0, True, None,
                     {'a': 1}, ['practice'], ('unsure',)):
            with self.subTest(utterance_mode=mode):
                self.assertIsNone(producer.observed_utterance_mode(
                    {'utterance_mode': mode}))
        self.assertIsNone(producer.observed_utterance_mode({}))
        self.assertIsNone(producer.observed_utterance_mode(None))

    def test_utterance_mode_does_not_change_the_audited_gate_sequence(self):
        passing = dict(source_event_id='source-0', quote='今天我亏了69元',
                       summary='她今天亏了69元', predicate='reported_fact',
                       object='交易结果', value='亏了69元')
        rejected = dict(passing, quote='她住在火星上')
        self.assertEqual(
            producer.gate_results(True, None, passing),
            producer.gate_results(True, None, dict(passing, utterance_mode='practice')))
        self.assertEqual(
            producer.gate_results(False, 'quote_not_verbatim', rejected),
            producer.gate_results(False, 'quote_not_verbatim',
                                  dict(rejected, utterance_mode='practice')))

    def test_adjudicate_strips_utterance_observation_before_the_gate(self):
        seen = {}

        def capture(_cur, _user, _character, candidate):
            seen['candidate'] = dict(candidate)
            return False, 'quote_not_verbatim'

        cur = Mock()
        cur.fetchone.return_value = ({'content': '今天我亏了69元'}, {}, self.now)
        with patch('memory_candidate.validate_candidate', capture):
            result = producer.adjudicate_and_project(
                cur, user_id='u', character_id='c', event_id=1, occurred_at=self.now,
                candidate=dict(source_event_id='source-0', quote='今天我亏了69元',
                               summary='她今天亏了69元', predicate='reported_fact',
                               object='交易结果', value='亏了69元',
                               utterance_mode='practice', context_excerpt='出题',
                               prior_character_text='回声'))
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(result['reason'], 'quote_not_verbatim')
        self.assertNotIn('utterance_mode', seen['candidate'])
        self.assertNotIn('context_excerpt', seen['candidate'])
        self.assertNotIn('prior_character_text', seen['candidate'])
        self.assertNotIn('interpretation', seen['candidate'])

    def test_persistence_does_not_change_the_audited_gate_sequence(self):
        candidate = dict(source_event_id='source-0', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact',
                         object='交易结果', value='亏了69元')
        self.assertEqual(
            producer.gate_results(True, None, candidate),
            producer.gate_results(True, None, dict(candidate, persistence='transient')))

    def live_projection_candidate(self, **overrides):
        """Drive the shadow=False branch without a database.

        Everything the branch touches outside the producer is replaced: the
        cursor, the gate, the advisory lock helper and the projector. Only the
        candidate dict handed to the projector is under test.
        """
        candidate = dict(source_event_id='source-0', quote='今天我亏了69元',
                         summary='她今天亏了69元', predicate='reported_fact',
                         object='交易结果', value='亏了69元', **overrides)
        inputs = [{'event_id': 11, 'source_event_id': 'source-0',
                   'occurred_at': self.now}]
        lock = Mock(return_value=1)
        with patch.dict(sys.modules, {'cognitive_queue': Mock(
                stable_advisory_lock_key=lock)}), \
                patch.object(producer, 'validate_candidate',
                             return_value=(True, None)), \
                patch.object(producer, 'adjudicate_and_project') as project:
            project.return_value = {'status': 'validated_projection', 'reason': None}
            records = producer._process_candidates(
                Mock(), 'user-1', 'gojo', [candidate], inputs, {}, False)
        project.assert_called_once()
        return project.call_args.kwargs['candidate'], records[0]

    def test_persistence_is_stripped_before_the_live_projection(self):
        # This branch only runs with shadow off, so it is the code that goes
        # live the moment CANDIDATE_PRODUCER_SHADOW is cleared. The observation
        # field must not reach the canonical projector, which never agreed to
        # carry it, and interpretation must keep being stripped beside it.
        projected, record = self.live_projection_candidate(
            persistence='transient', interpretation='她在抱怨')
        self.assertNotIn('persistence', projected)
        self.assertNotIn('interpretation', projected)
        self.assertEqual(projected['quote'], '今天我亏了69元')
        self.assertEqual(projected['predicate'], 'reported_fact')
        # The audit record still reports the tier the model gave.
        self.assertEqual(record['persistence'], 'transient')
        self.assertTrue(record['accepted'])

    def test_utterance_observation_is_stripped_before_the_live_projection(self):
        projected, record = self.live_projection_candidate(
            utterance_mode='practice', context_excerpt='用这个句型造句',
            prior_character_text='回声')
        self.assertNotIn('utterance_mode', projected)
        self.assertNotIn('context_excerpt', projected)
        self.assertNotIn('prior_character_text', projected)
        self.assertNotIn('interpretation', projected)
        self.assertEqual(projected['quote'], '今天我亏了69元')
        self.assertEqual(projected['predicate'], 'reported_fact')
        self.assertEqual(record['utterance_mode'], 'practice')
        # The model's copy of context_excerpt is not the stored residue.
        # The input in this test has no preceding character line.
        self.assertIsNone(record['context_excerpt'])
        self.assertTrue(record['accepted'])
        omitted, omitted_record = self.live_projection_candidate(utterance_mode='maybe')
        self.assertNotIn('utterance_mode', omitted)
        self.assertIsNone(omitted_record['utterance_mode'])
        self.assertTrue(omitted_record['accepted'])

    def test_live_projection_is_unchanged_when_no_tier_is_offered(self):
        projected, record = self.live_projection_candidate()
        self.assertNotIn('persistence', projected)
        self.assertIsNone(record['persistence'])
        self.assertEqual(record['projection_status'], 'validated_projection')

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
        self.stack.enter_context(patch.object(
            candidate_audit, 'CANDIDATE_AUDIT_ENABLED', True))

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
                 'object': '交易结果', 'value': '亏了69元',
                 'interpretation': '她这是在报亏损，不是想让我算账'}
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

    def audit_rows(self):
        cur = self.database.cursor()
        cur.execute('''SELECT user_id, character_id, batch_id, source_event_id,
            candidate_index, quote, summary, interpretation, persistence,
            gate_accepted, accepted, reason, projection_status, projection_reason,
            module_version, shadow, occurred_at, created_at
            FROM candidate_audit_log ORDER BY candidate_index, id''')
        columns = [item[0] for item in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def assert_aware(self, value):
        self.assertIsInstance(value, datetime)
        self.assertIsNotNone(value.tzinfo)
        self.assertIsNotNone(value.utcoffset())

    def non_processing_state(self):
        # candidate_audit_log is write-only observation. It is not authority,
        # so these snapshots keep comparing every authoritative table.
        ignored = {'event_processing_state', 'candidate_audit_log'}
        return {key: value for key, value in self.database_state().items()
                if key not in ignored}

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

    def test_shadow_schedule_grounding_and_rejections_never_write_authority(self):
        ids = self.seed(3, age=300)
        schedule = '周六调休了周三的课，今天早八到晚九'
        cards = '我今天也要打牌，他们过来我家'
        candidates = []
        for source_id, quote, summary in (
                (ids[0], schedule, '她周六调休补周三的课，今天课程从早八到晚九'),
                (ids[1], schedule, '她周六调休补周三的课，今天课程从早八到晚十'),
                (ids[2], cards, '她今天要在家里和朋友打牌，朋友会来她家')):
            self.sql('UPDATE chat_log SET text=%s WHERE event_id=%s', (quote, source_id))
            self.sql('UPDATE cognitive_events SET payload=%s::jsonb WHERE source_event_id=%s',
                     (json.dumps({'content': quote}, ensure_ascii=False), source_id))
            candidates.append(self.candidate(source_id, quote=quote, summary=summary,
                                             object='活动安排', value=quote))
        self.database.commit()
        before = self.non_processing_state()
        with patch.object(producer, 'adjudicate_and_project',
                          side_effect=AssertionError('shadow adjudicated candidate')) as project, \
                patch.object(self.database, 'query', wraps=self.database.query) as queries:
            result = self.run_pair(self.model(candidates))
        project.assert_not_called()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual((result['gates_accepted'], result['accepted'], result['rejected']),
                         (1, 1, 2))
        self.assertEqual(result['reasons'],
                         {'number_value_mismatch': 1, 'summary_not_grounded_in_quote': 1})
        self.assertEqual(self.non_processing_state(), before)
        # Count attempted writes as well as comparing the committed table state.
        for call in queries.call_args_list:
            sql = ' '.join(call.args[0].split()).upper()
            self.assertTrue(sql.startswith((
                'SELECT ', 'INSERT INTO EVENT_PROCESSING_STATE ',
                'UPDATE EVENT_PROCESSING_STATE ', 'BEGIN', 'COMMIT', 'ROLLBACK',
                'CREATE TABLE IF NOT EXISTS CANDIDATE_AUDIT_LOG',
                'CREATE INDEX IF NOT EXISTS IDX_CANDIDATE_AUDIT_',
                'INSERT INTO CANDIDATE_AUDIT_LOG ',
                'ALTER TABLE CANDIDATE_AUDIT_LOG ADD COLUMN IF NOT EXISTS ')), sql)
        logs = self.candidate_logs()
        self.assertEqual(logs[0]['gates']['G9']['status'], 'passed')
        self.assertEqual(logs[1]['gates']['G4']['status'], 'rejected')
        self.assertEqual(logs[1]['gates']['G9']['status'], 'not_evaluated')
        self.assertEqual(logs[2]['gates']['G9']['status'], 'rejected')

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

    def test_interpretation_is_audited_and_omitted_from_projection(self):
        interpretation = '她把这叫做"亏了"，其实是在抱怨室友，不是真的想搬家，听起来像昨天的100元'
        self.seed()
        before = self.non_processing_state()
        result = self.run_pair(self.model([self.candidate(interpretation=interpretation)]))
        self.assertEqual((result['status'], result['accepted'], result['rejected']),
                         ('succeeded', 1, 0))
        logged = self.candidate_logs()[0]
        self.assertEqual(logged['interpretation'], interpretation)
        self.assertEqual(logged['summary'], '她今天亏了69元')
        for gate in ('G4', 'G5', 'G6', 'G7', 'G9'):
            self.assertEqual(logged['gates'][gate]['status'], 'passed', gate)
        self.assertEqual(logged['gates']['G8']['status'], 'not_evaluated')
        self.assertEqual(self.non_processing_state(), before)

        self.seed(prefix='live', user='live')
        live = self.run_pair(self.model([
            self.candidate('live-0', interpretation=interpretation)]),
            user='live', shadow=False)
        self.assertEqual((live['status'], live['accepted']), ('succeeded', 1))
        recalled = self.sql('SELECT content, semantic_payload FROM long_memory WHERE user_id=%s',
                            ('live',))
        self.assertEqual([row[0] for row in recalled], ['她今天亏了69元。'])
        stored = json.dumps({
            'memory': recalled,
            'events': self.sql('''SELECT payload, adjudication FROM cognitive_events
                WHERE user_id=%s''', ('live',)),
        }, ensure_ascii=False, default=str)
        self.assertNotIn(interpretation, stored)
        self.assertNotIn('抱怨室友', stored)
        self.assertNotIn('interpretation', stored)

    def test_unescaped_quote_in_interpretation_uses_the_existing_solo_skip(self):
        ids = self.seed(2, age=300)
        self.set_batch_size(1)
        raw = ('[{"source_event_id":"%s","quote":"今天我亏了69元",'
               '"summary":"她今天亏了69元","interpretation":"她说的是"亏了"",'
               '"predicate":"reported_fact","object":"交易结果","value":"亏了69元"}]'
               % ids[0])
        before = self.non_processing_state()
        failed = self.model(raw=raw)
        for count, seconds in enumerate((0, 31, 92), start=1):
            result = self.run_pair(failed, now=self.now + timedelta(seconds=seconds))
            self.assertEqual((result['status'], result['reason']), ('failed', 'invalid_json'))
            self.assertEqual(self.control_state().get('solo_failures', {}),
                             {} if count == 3 else {ids[0]: count})
        self.assertEqual(self.sql('''SELECT status,last_error FROM event_processing_state
            WHERE event_id=%s AND processor_type=%s''',
            (ids[0], producer._processor('u', 'c'))), [('skipped', 'invalid_json')])
        self.assertEqual(self.candidate_logs(), [])
        self.assertNotIn('她说的是', json.dumps(self.logs, ensure_ascii=False))
        self.assertEqual(self.non_processing_state(), before)
        recovered = self.model(raw='[]')
        self.assertEqual(self.run_pair(recovered,
            now=self.now + timedelta(seconds=1893))['status'], 'succeeded')
        request = json.loads(recovered.call_args.kwargs['messages'][0]['content'])
        self.assertEqual([item['source_event_id'] for item in request], [ids[1]])

    def test_live_mode_projects_only_after_validation_and_is_recallable(self):
        self.seed()
        self.run_pair(shadow=False)
        from memory_authority import authoritative_memory_sql
        gate = authoritative_memory_sql('long_memory')
        self.assertEqual(self.sql(f'SELECT content FROM long_memory WHERE {gate}'),
                         [('她今天亏了69元。',)])
        self.assertTrue(self.sql('SELECT adjudication FROM cognitive_events WHERE source_event_id=%s',
                                 ('source-0',))[0][0])

    def test_candidate_audit_persists_interpretation(self):
        interpretation = '她这是在报亏损，不是想让我算账'
        self.seed()
        result = self.run_pair(self.model([self.candidate(interpretation=interpretation)]))
        self.assertEqual((result['status'], result['accepted']), ('succeeded', 1))
        logged = self.candidate_logs()[0]
        self.assertEqual(logged['interpretation'], interpretation)
        self.assertNotIn('occurred_at', logged)
        rows = self.audit_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len([row for row in self.logs if row.get('kind') == 'batch']), 1)
        row = rows[0]
        self.assertEqual(row['interpretation'], interpretation)
        self.assertEqual(row['summary'], '她今天亏了69元')
        self.assertEqual(row['quote'], '今天我亏了69元')
        self.assertEqual(row['source_event_id'], 'source-0')
        self.assertEqual(row['candidate_index'], 0)
        self.assertEqual((row['user_id'], row['character_id']), ('u', 'c'))
        self.assertTrue(row['gate_accepted'])
        self.assertTrue(row['accepted'])
        self.assertEqual(row['batch_id'], result['batch_id'])
        self.assertEqual(producer.MODULE_VERSION, '1.0')
        self.assertEqual(row['module_version'], '1.0')
        self.assertIs(row['shadow'], True)
        self.assertIsNone(row['persistence'])
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])

    def test_candidate_audit_persists_null_interpretation(self):
        candidate = self.candidate()
        del candidate['interpretation']
        self.seed()
        result = self.run_pair(self.model([candidate]))
        self.assertEqual(result['status'], 'succeeded')
        row = self.audit_rows()[0]
        self.assertIsNone(row['interpretation'])
        self.assertEqual(row['summary'], '她今天亏了69元')
        self.assertTrue(row['gate_accepted'])

    def test_candidate_audit_persists_gate_rejections(self):
        self.seed()
        rejected = self.candidate(quote='她住在火星上', interpretation='她大概在开玩笑')
        missing_time = self.candidate('not-in-this-batch', interpretation='这条来源不在本批')
        result = self.run_pair(self.model([rejected, missing_time]))
        self.assertEqual(result['status'], 'succeeded')
        rows = self.audit_rows()
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]['gate_accepted'])
        self.assertFalse(rows[0]['accepted'])
        self.assertEqual(rows[0]['reason'], 'quote_not_verbatim')
        self.assertEqual(rows[0]['interpretation'], '她大概在开玩笑')
        self.assertEqual(rows[0]['occurred_at'], self.now)
        self.assertFalse(rows[1]['gate_accepted'])
        self.assertEqual(rows[1]['reason'], 'source_not_in_batch')
        self.assertIsNone(rows[1]['occurred_at'])
        self.assert_aware(rows[1]['created_at'])
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])

    def test_candidate_audit_failure_keeps_batch_succeeded(self):
        self.seed()
        with patch('candidate_audit.log_candidate', side_effect=RuntimeError('audit down')):
            result = self.run_pair()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(len(self.candidate_logs()), 1)
        self.assertEqual(self.processing_rows()[0][0], 'succeeded')
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])
        self.assertEqual(self.sql("""SELECT tablename FROM pg_tables
            WHERE schemaname='public' AND tablename='candidate_audit_log'"""), [])

    def test_candidate_audit_disabled_writes_nothing(self):
        self.seed()
        with patch.object(candidate_audit, 'CANDIDATE_AUDIT_ENABLED', False):
            result = self.run_pair()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.candidate_logs()), 1)
        self.assertEqual(self.sql("""SELECT tablename FROM pg_tables
            WHERE schemaname='public' AND tablename='candidate_audit_log'"""), [])

    def test_candidate_audit_timestamps_are_timezone_aware(self):
        self.seed()
        self.run_pair()
        row = self.audit_rows()[0]
        self.assertEqual(row['occurred_at'], self.now)
        self.assert_aware(row['occurred_at'])
        self.assert_aware(row['created_at'])
        self.assertNotEqual(row['created_at'].tzinfo, None)

    def insert_chat(self, text, *, role='gojo', age=30, character='c', user='u',
                    event_id=None, status='active', subtitle=''):
        created = self.now - timedelta(seconds=age)
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,subtitle,created_at,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)''',
            (user, character, event_id or f'{role}-{age}', role, text,
             subtitle, created, status))
        self.database.commit()

    def request_inputs(self, model):
        return json.loads(model.call_args.kwargs['messages'][0]['content'])

    def test_preceding_character_turn_is_attached_and_truncated(self):
        self.seed(1, age=300)
        line = '请用这个句型造一个完整的句子' * 40
        self.insert_chat(line, age=310)
        model = self.model([])
        result = self.run_pair(model)
        self.assertEqual(result['status'], 'succeeded')
        excerpt = line[:400]
        payloads = self.request_inputs(model)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]['prior_character_text'], excerpt)
        self.assertEqual(payloads[0]['content'], '今天我亏了69元，想和你聊一聊。')
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])

    def test_utterance_mode_does_not_change_accept_or_reject(self):
        self.seed(prefix='plain', user='plain')
        passing = self.candidate('plain-0')
        rejected = self.candidate('plain-0', quote='她住在火星上')
        self.logs.clear()
        plain = self.run_pair(self.model([passing, rejected]), user='plain')
        plain_logs = list(self.candidate_logs())
        self.seed()
        self.logs.clear()
        marked = self.run_pair(self.model([
            self.candidate(utterance_mode='practice'),
            self.candidate(quote='她住在火星上', utterance_mode='practice')]))
        marked_logs = list(self.candidate_logs())

        def outcome(logs):
            return [(row['gate_accepted'], row['accepted'], row['reason']) for row in logs]

        self.assertEqual(outcome(plain_logs), outcome(marked_logs))
        self.assertEqual(plain['accepted'], marked['accepted'])
        self.assertTrue(marked_logs[0]['gate_accepted'])
        self.assertFalse(marked_logs[1]['gate_accepted'])
        self.assertEqual(marked_logs[0]['reason'], None)
        self.assertEqual(marked_logs[1]['reason'], 'quote_not_verbatim')
        self.assertEqual(marked_logs[0]['utterance_mode'], 'practice')
        self.assertEqual(marked_logs[1]['utterance_mode'], 'practice')

    def test_illegal_utterance_mode_does_not_fail_the_batch(self):
        self.seed()
        result = self.run_pair(self.model([
            self.candidate(),
            dict(self.candidate(), utterance_mode='maybe'),
            dict(self.candidate(), utterance_mode=12),
            dict(self.candidate(), utterance_mode=None),
            dict(self.candidate(), utterance_mode='sincere'),
        ]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['accepted'], 5)
        self.assertEqual(
            [row['utterance_mode'] for row in self.candidate_logs()],
            [None, None, None, None, 'sincere'])

    def seed_turn(self, text, source_id, *, character_text=None, user='u',
                  character='c', age=300):
        created = self.now - timedelta(seconds=age)
        if character_text is not None:
            self.sql('''INSERT INTO chat_log
                (user_id,chat_id,event_id,role,text,created_at,status)
                VALUES (%s,%s,%s,'gojo',%s,%s,'active')''',
                (user, character, source_id + '-gojo', character_text,
                 created - timedelta(seconds=5)))
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,created_at,status)
            VALUES (%s,%s,%s,'user',%s,%s,'active')''',
            (user, character, source_id, text, created))
        self.sql('''INSERT INTO cognitive_events
            (user_id,character_id,source_event_type,source_event_id,source,
             occurred_at,payload)
            VALUES (%s,%s,'canonical_user_turn',%s,'chat',%s,%s::jsonb)''',
            (user, character, source_id, created,
             json.dumps({'content': text}, ensure_ascii=False)))
        self.database.commit()

    def observation_rows(self):
        cur = self.database.cursor()
        cur.execute('''SELECT source_event_id, utterance_mode, context_excerpt,
            gate_accepted, reason
            FROM candidate_audit_log ORDER BY candidate_index, id''')
        columns = [item[0] for item in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def test_audit_stores_mode_and_context_for_rejected_candidates_too(self):
        line = '用たくない造个句子' * 50
        self.seed_turn('ぼくはつかれたから,ふくしゅうしたくない', 'boku',
                       character_text=line)
        result = self.run_pair(self.model([
            self.candidate('boku', quote='ぼくはつかれたから,ふくしゅうしたくない',
                           summary='她说「ぼくはつかれたから,ふくしゅうしたくない」，不想复习',
                           value='ふくしゅうしたくない', object='练习',
                           utterance_mode='practice'),
            self.candidate('boku', quote='用たくない造个句子',
                           utterance_mode='practice'),
        ]))
        self.assertEqual(result['status'], 'succeeded')
        rows = self.observation_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['context_excerpt'], line[:400])
        self.assertEqual(rows[1]['context_excerpt'], line[:400])
        self.assertEqual(rows[0]['utterance_mode'], 'practice')
        self.assertEqual(rows[1]['utterance_mode'], 'practice')
        self.assertEqual(rows[1]['reason'], 'quote_not_verbatim')
        self.assertFalse(rows[1]['gate_accepted'])
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])

    def test_echoed_context_keys_do_not_fail_the_batch_or_overwrite_audit(self):
        real = '请用这个句型造一个完整的句子'
        self.seed_turn('想和你聊一聊', 'echo', character_text=real)
        result = self.run_pair(self.model([
            dict(self.candidate('echo', quote='想和你聊一聊', summary='她想和你聊一聊',
                                value='聊一聊', object='闲聊', utterance_mode='practice'),
                 prior_character_text='模型编的上下文',
                 context_excerpt='模型编的截断'),
        ]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['accepted'], 1)
        row = self.observation_rows()[0]
        self.assertEqual(row['context_excerpt'], real)
        self.assertNotEqual(row['context_excerpt'], '模型编的截断')
        self.assertEqual(row['utterance_mode'], 'practice')
        self.assertTrue(row['gate_accepted'])

    def test_missing_context_and_omitted_mode_store_null(self):
        self.seed_turn('明天早八有课', 'first')
        result = self.run_pair(self.model([
            self.candidate('first', quote='明天早八有课', summary='她明天早八有课',
                           value='早八有课', object='课表'),
            dict(self.candidate('first', quote='明天早八有课', summary='她明天早八有课',
                                value='早八有课', object='课表'),
                 utterance_mode='maybe'),
        ]))
        self.assertEqual(result['status'], 'succeeded')
        rows = self.observation_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row['context_excerpt'] is None for row in rows))
        self.assertTrue(all(row['utterance_mode'] is None for row in rows))
        self.assertTrue(all(row['gate_accepted'] for row in rows))

    def test_production_quotes_persist_the_supplied_utterance_mode(self):
        # The fixture supplies the label. This checks the pipe, not whether
        # that label is the right reading of the sentence.
        samples = (
            ('boku', 'ぼくはつかれたから,ふくしゅうしたくない',
             '她说「ぼくはつかれたから,ふくしゅうしたくない」，不想复习',
             'ふくしゅうしたくない', 'practice', '用たくない造个句子'),
            ('kyo', 'きょはたのし', '她说「きょはたのし」，今天很开心',
             'きょはたのし', 'practice', '把今天很开心写出来'),
            ('copied', '因为你写了呀，我照着打的，但并不知道是什么意思',
             '她说因为你写了呀，她照着打的，但并不知道是什么意思',
             '照着打的', 'unsure', '你把这句打出来'),
            ('class', '明天早八有课', '她明天早八有课',
             '早八有课', 'sincere', '明天有什么安排'),
        )
        for index, (source_id, text, _summary, _value, _mode, character_text) in enumerate(samples):
            self.seed_turn(text, source_id, character_text=character_text,
                           age=400 - index * 40)
        result = self.run_pair(self.model([
            self.candidate(source_id, quote=text, summary=summary, value=value,
                           object='原话', utterance_mode=mode)
            for source_id, text, summary, value, mode, _character in samples
        ]))
        self.assertEqual(result['status'], 'succeeded')
        rows = self.observation_rows()
        self.assertEqual(
            [(row['source_event_id'], row['utterance_mode'], row['context_excerpt'])
             for row in rows],
            [(source_id, mode, character_text)
             for source_id, _text, _summary, _value, mode, character_text in samples])
        stored = {row['source_event_id']: row for row in rows}
        self.assertTrue(stored['class']['gate_accepted'])
        self.assertEqual(stored['class']['reason'], None)

    def test_first_user_message_has_no_character_context(self):
        self.seed(1, age=300)
        model = self.model([])
        self.run_pair(model)
        payloads = self.request_inputs(model)
        self.assertTrue(payloads)
        self.assertTrue(all('prior_character_text' not in item for item in payloads))

    def test_character_context_ignores_other_chats_later_rows_and_non_gojo_roles(self):
        self.seed(1, age=300)
        self.insert_chat('另一场聊天的出题', character='other', age=310)
        self.insert_chat('这条在用户之后', age=-60)
        self.insert_chat('assistant 不是库里的角色', role='assistant', age=320)
        self.insert_chat('已经删掉的出题', age=305, status='deleted')
        model = self.model([])
        self.run_pair(model)
        payloads = self.request_inputs(model)
        self.assertTrue(all('prior_character_text' not in item for item in payloads))

    def test_whole_character_turn_includes_every_bubble_and_subtitle(self):
        self.seed(1, age=300)
        self.insert_chat('「〜たくない」の練習だ', age=330, event_id='gojo-1',
                         subtitle='用〜たくない练习')
        self.insert_chat('何か文を作ってみろ', age=320, event_id='gojo-2',
                         subtitle='造个句子试试')
        self.insert_chat('ほら、早く', age=310, event_id='gojo-3', subtitle='')
        model = self.model([])
        self.run_pair(model)
        expected = (
            '「〜たくない」の練習だ\n中文：用〜たくない练习\n'
            '何か文を作ってみろ\n中文：造个句子试试\n'
            'ほら、早く'
        )
        payloads = self.request_inputs(model)
        self.assertEqual(payloads[0]['prior_character_text'], expected)

    def test_character_context_keeps_only_the_latest_turn(self):
        self.seed_turn('上一轮的回答', 'earlier', character_text='更早的出题，不要用这一条',
                       age=500)
        created = self.now - timedelta(seconds=300)
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,subtitle,created_at,status)
            VALUES ('u','c','gojo-near-1','gojo',%s,'请造句',%s,'active')''',
            ('请用这个句型造一个完整的句子',
             created - timedelta(seconds=10)))
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,subtitle,created_at,status)
            VALUES ('u','c','gojo-near-2','gojo',%s,'',%s,'active')''',
            ('ほら、早く', created - timedelta(seconds=5)))
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,created_at,status)
            VALUES ('u','c','later','user',%s,%s,'active')''',
            ('想和你聊一聊', created))
        self.sql('''INSERT INTO cognitive_events
            (user_id,character_id,source_event_type,source_event_id,source,
             occurred_at,payload)
            VALUES ('u','c','canonical_user_turn','later','chat',%s,%s::jsonb)''',
            (created, json.dumps({'content': '想和你聊一聊'}, ensure_ascii=False)))
        self.database.commit()
        # Mark the earlier user turn processed so only the later one is pending.
        self.sql('''INSERT INTO event_processing_state
            (event_id,processor_type,processor_version,status,retry_count)
            VALUES (%s,%s,%s,'succeeded',0)''',
            ('earlier', producer._processor('u', 'c'),
             producer._receipt_version(True)))
        self.database.commit()
        model = self.model([self.candidate(
            'later', quote='想和你聊一聊', summary='她想和你聊一聊',
            value='聊一聊', object='闲聊')])
        result = self.run_pair(model)
        self.assertEqual(result['status'], 'succeeded')
        expected = '请用这个句型造一个完整的句子\n中文：请造句\nほら、早く'
        self.assertEqual(self.request_inputs(model)[0]['prior_character_text'], expected)
        self.assertEqual(self.observation_rows()[0]['context_excerpt'], expected)

    def test_same_second_rows_order_by_id(self):
        self.seed(1, age=300)
        same = self.now - timedelta(seconds=310)
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,subtitle,created_at,status)
            VALUES ('u','c','gojo-same-1','gojo','先出题','',%s,'active')''', (same,))
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,subtitle,created_at,status)
            VALUES ('u','c','gojo-same-2','gojo','再催促','',%s,'active')''', (same,))
        self.database.commit()
        model = self.model([])
        self.run_pair(model)
        self.assertEqual(
            self.request_inputs(model)[0]['prior_character_text'],
            '先出题\n再催促')

    def test_quote_taken_from_the_character_line_is_still_rejected(self):
        self.seed(1, age=300)
        line = '请用这个句型造一个完整的句子'
        self.insert_chat(line, age=310)
        result = self.run_pair(self.model([self.candidate(
            'source-0', quote=line)]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['accepted'], 0)
        row = self.audit_rows()[0]
        self.assertEqual(row['reason'], 'quote_not_verbatim')
        self.assertFalse(row['gate_accepted'])
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])

    def test_summary_taken_from_the_character_line_is_still_ungrounded(self):
        self.seed(1, age=300)
        line = '请用这个句型造一个完整的句子'
        self.insert_chat(line, age=310)
        result = self.run_pair(self.model([self.candidate(
            'source-0', quote='想和你聊一聊', summary='她说' + line, value='聊一聊')]))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['accepted'], 0)
        row = self.audit_rows()[0]
        self.assertEqual(row['reason'], 'summary_not_grounded_in_quote')
        self.assertFalse(row['gate_accepted'])
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])


class CandidateValuePolarityTests(unittest.TestCase):
    """Value polarity uses the same denial check as G5.

    validate_candidate is stubbed so these tests reach the quote/value check
    without a database. The check itself is the production function.
    """

    def setUp(self):
        self.now = datetime(2026, 10, 25, tzinfo=timezone.utc)

    def adjudicate(self, quote, value, predicate='reported_fact'):
        cur = Mock()
        cur.fetchone.return_value = ({}, {}, self.now)
        candidate = {
            'source_event_id': 'source-polarity',
            'quote': quote,
            'summary': '她今天提到了这件事',
            'predicate': predicate,
            'object': '原话片段',
            'value': value,
        }
        with patch('memory_candidate.validate_candidate', return_value=(True, None)), \
                patch('cognitive_revision._apply_literal_operation',
                      return_value={'status': 'applied', 'action': 'reported'}) as writer, \
                patch('memory_candidate.apply_candidate',
                      return_value={'status': 'validated_projection'}):
            result = producer.adjudicate_and_project(
                cur, user_id='u', character_id='c', candidate=candidate,
                event_id=1, occurred_at=self.now)
        return result, writer

    def assert_not_polarity_mismatch(self, quote, value, predicate='reported_fact'):
        result, writer = self.adjudicate(quote, value, predicate)
        self.assertNotEqual(result.get('reason'), 'candidate_value_polarity_mismatch')
        self.assertEqual(result['status'], 'validated_projection')
        self.assertTrue(writer.called)

    def assert_polarity_mismatch(self, quote, value, predicate='reported_fact'):
        result, writer = self.adjudicate(quote, value, predicate)
        self.assertEqual((result['status'], result['reason']),
                         ('rejected', 'candidate_value_polarity_mismatch'))
        self.assertFalse(writer.called)

    def test_question_form_does_not_split_a_non_negated_fragment(self):
        # 是不是 / 有没有 / 能不能 / 要不要 里的「不是」「没有」「不能」「不要」
        # 是问句，不是否定。value 取原话里不含否定的片段。
        for quote, value in (
                ('你是不是一直这样对我', '一直这样对我'),
                ('你有没有吃饭', '吃饭'),
                ('你能不能过来', '过来'),
                ('你要不要咖啡', '咖啡')):
            with self.subTest(quote=quote, value=value):
                self.assert_not_polarity_mismatch(quote, value)

    def test_logged_lines_share_polarity_with_the_value_span(self):
        # 线上原话。旧正则看不见「不重要」和「没发」，但看得见问句里的「不是」
        # 以及「没有」。value 和原话在 G5 口径下极性一致时不再误杀。
        for quote, value in (
                ('是我不重要所以你才能一直这样对我吗', '不重要'),
                ('是不是我不重要所以你才能一直这样对我吗', '不重要'),
                ('你没发！', '没发'),
                ('你是不是没发！', '没发'),
                ('你没发！你没有回我', '没发')):
            with self.subTest(quote=quote, value=value):
                self.assert_not_polarity_mismatch(quote, value)

    def test_dropped_or_flipped_denial_is_still_a_mismatch(self):
        for quote, value in (
                ('我不喜欢咖啡这种饮料', '喜欢'),
                ('我不是不喜欢咖啡', '不喜欢'),
                ('是我不重要所以你才能一直这样对我吗', '重要'),
                ('是我不重要所以你才能一直这样对我吗', '一直这样对我'),
                ('你没发！', '发')):
            with self.subTest(quote=quote, value=value):
                self.assert_polarity_mismatch(quote, value)

    def test_conversational_mei_openers_are_not_value_polarity_mismatches(self):
        for quote, value in (
                ('没关系啦我明天考离散数学', '明天考离散数学'),
                ('没事我自己搞定这个报告', '我自己搞定这个报告'),
                ('没错我买了咖啡', '买了咖啡'),
                ('你是不是一直这样对我', '一直这样对我')):
            with self.subTest(quote=quote, value=value):
                self.assert_not_polarity_mismatch(quote, value)

    def test_real_denial_still_makes_the_value_span_a_polarity_mismatch(self):
        self.assert_polarity_mismatch('你没发！', '发')
        self.assert_polarity_mismatch('我不喜欢咖啡这种饮料', '喜欢')
        self.assert_polarity_mismatch(
            '是我不重要所以你才能一直这样对我吗', '重要')
        self.assert_polarity_mismatch('我今天没事做', '今天')

    def test_non_denial_bu_words_are_not_treated_as_negation(self):
        for quote, value in (
                ('不过我今天买了咖啡', '买了咖啡'),
                ('我不但喜欢咖啡', '喜欢'),
                ('我不仅喜欢咖啡', '喜欢'),
                ('不如先喝咖啡', '喝咖啡'),
                ('差不多喜欢咖啡', '喜欢')):
            with self.subTest(quote=quote, value=value):
                self.assert_not_polarity_mismatch(quote, value)

    def test_japanese_false_negation_is_not_a_value_polarity_mismatch(self):
        # Same function as G5. A ない-final word that is not a denial must not
        # split the quote from a value span that omits it.
        self.assert_not_polarity_mismatch('ここは危ない', 'ここは')
        self.assert_not_polarity_mismatch('水が少ない', '水が')
        self.assert_not_polarity_mismatch('これはもったいない', 'これは')
        self.assert_not_polarity_mismatch('今日はつまらない', '今日は')
        self.assert_not_polarity_mismatch('いぬがすき', 'すき')

    def test_japanese_denial_dropped_from_the_value_span_is_a_mismatch(self):
        self.assert_polarity_mismatch('ふくしゅうしたくない', 'ふくしゅう')
        self.assert_not_polarity_mismatch('ふくしゅうしたくない', 'したくない')

    def test_reported_preference_branch_still_rejects_embedded_positive_values(self):
        self.assert_polarity_mismatch(
            '我不喜欢咖啡这种饮料', '喜欢', predicate='reported_preference')
        self.assert_polarity_mismatch(
            '我不讨厌咖啡这种饮料', '讨厌', predicate='reported_preference')
        self.assert_not_polarity_mismatch(
            '我喜欢喝咖啡', '喜欢', predicate='reported_preference')
        self.assert_not_polarity_mismatch(
            '我不喜欢喝咖啡', '不喜欢', predicate='reported_preference')


if __name__ == '__main__':
    unittest.main()
