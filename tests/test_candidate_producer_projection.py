"""Candidate adjudication and fact links over disposable, offline PostgreSQL."""
from copy import deepcopy
from datetime import timedelta
import json
import os
import unittest
from unittest.mock import patch

from tests import test_memory_candidate_projection as projection_fixture
from memory_authority import authoritative_memory_sql, project_canonical_memory
import memory_candidate


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE to the local test-only package')
class CandidateProducerProjectionTests(unittest.TestCase):
    setUpClass = classmethod(projection_fixture.CandidateProjectionTests.setUpClass.__func__)
    tearDownClass = classmethod(projection_fixture.CandidateProjectionTests.tearDownClass.__func__)
    setUp = projection_fixture.CandidateProjectionTests.setUp
    tearDown = projection_fixture.CandidateProjectionTests.tearDown
    sql = projection_fixture.CandidateProjectionTests.sql
    source = projection_fixture.CandidateProjectionTests.source
    snapshot = projection_fixture.CandidateProjectionTests.snapshot
    database_state = projection_fixture.CandidateProjectionTests.database_state
    candidate = projection_fixture.CandidateProjectionTests.candidate
    seed = projection_fixture.CandidateProjectionTests.seed
    recalled = projection_fixture.CandidateProjectionTests.recalled

    def produce(self, candidate=None, **options):
        import candidate_producer
        candidate = self.candidate() if candidate is None else candidate
        source_id = candidate['source_event_id']
        rows = self.sql('SELECT id FROM cognitive_events WHERE source_event_id=%s',
                        (source_id,))
        if not rows:
            self.seed(candidate, adjudicated=False, include_claim=False)
            rows = self.sql('SELECT id FROM cognitive_events WHERE source_event_id=%s',
                            (source_id,))
        return candidate_producer.adjudicate_and_project(
            self.database.cursor(), user_id='u', character_id='c', candidate=candidate,
            event_id=rows[0][0], occurred_at=self.now, **options)

    def deterministic_pass(self, event_id):
        import cognitive_revision
        cycle_id = self.sql('''INSERT INTO cognitive_cycles
            (user_id,character_id,status,primary_trigger_class,input_state_version,
             output_state_version,completed_at)
            VALUES ('u','c','succeeded','question_reactivation',0,1,%s) RETURNING id''',
            (self.now,))[0][0]
        decisions = cognitive_revision.apply_rule_evidence(self.database.cursor(),
            cycle_id=cycle_id, user_id='u', character_id='c',
            output={'evidence_refs': [{'event_id': event_id}]}, now=self.now)
        return cycle_id, decisions

    def test_unsupported_canonical_pending_is_supplemented_without_replacing_main_or_cycle(self):
        first = self.candidate()
        seeded = self.seed(first, text='今天我亏了69元，今天我买了咖啡。',
                           adjudicated=False, include_claim=False)
        cycle_id, _ = self.deterministic_pass(seeded['event_id'])
        original = self.sql('SELECT adjudication FROM cognitive_events WHERE id=%s',
                            (seeded['event_id'],))[0][0]
        self.assertEqual(original['status'], 'pending')
        self.assertEqual(original['reason'], 'unsupported_or_implicit_semantics')
        self.assertEqual(original['processed_by_cycle_id'], cycle_id)
        self.assertEqual(self.produce(first)['status'], 'validated_projection')
        supplemented = self.sql('SELECT adjudication FROM cognitive_events WHERE id=%s',
                                (seeded['event_id'],))[0][0]
        self.assertEqual(supplemented['operations']['main'], original['operations']['main'])
        self.assertEqual(supplemented['processed_by_cycle_id'], cycle_id)
        self.assertEqual(len(supplemented['operations']), 2)
        self.assertEqual(self.recalled(), [('她今天亏了69元。',)])
        second = self.candidate(quote='今天我买了咖啡', summary='她今天买了咖啡',
                                object='购买记录', value='买了咖啡')
        self.assertEqual(self.produce(second)['status'], 'validated_projection')
        appended = self.sql('SELECT adjudication FROM cognitive_events WHERE id=%s',
                            (seeded['event_id'],))[0][0]
        self.assertEqual(appended['processed_by_cycle_id'], cycle_id)
        self.assertEqual(appended['operations']['main'], original['operations']['main'])
        self.assertEqual(len(appended['operations']), 3)
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(2,)])
        state = self.database_state()
        self.assertEqual(self.produce(first)['status'], 'skipped')
        self.assertEqual(self.database_state(), state)

    def test_later_deterministic_pass_preserves_producer_adjudication_and_authority(self):
        self.produce()
        before = self.database_state()
        event_id = self.sql("SELECT id FROM cognitive_events WHERE source_event_id='source'")[0][0]
        _, decisions = self.deterministic_pass(event_id)
        self.assertEqual(decisions, [{'event_id': event_id, 'action': 'already_processed'}])
        after = self.database_state()
        for table in before:
            if table != 'cognitive_cycles':
                self.assertEqual(after[table], before[table], table)
        self.assertEqual(self.recalled(), [('她今天亏了69元。',)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs'), [(1,)])
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(1,)])

    def test_unsupported_top_reason_cannot_authorize_applied_or_other_pending_operations(self):
        unsupported = {'status': 'pending', 'action': 'pending',
                       'reason': 'unsupported_or_implicit_semantics'}
        for index, operations in enumerate((
                {'main': unsupported, 'protected': {'status': 'applied', 'action': 'answered'}},
                {'main': unsupported, 'protected': {'status': 'pending', 'action': 'pending',
                    'reason': 'conflicting_report_requires_confirmation'}},
                {}, [], {'main': None})):
            with self.subTest(operations=operations):
                candidate = self.candidate(source_event_id=f'protected-operation-{index}')
                seeded = self.seed(candidate, adjudicated=False, include_claim=False)
                adjudication = {'status': 'pending', 'reason': 'unsupported_or_implicit_semantics',
                                'operations': operations}
                self.sql('UPDATE cognitive_events SET adjudication=%s::jsonb WHERE id=%s',
                         (json.dumps(adjudication), seeded['event_id']))
                state = self.database_state()
                result = self.produce(candidate)
                self.assertEqual((result['status'], result['reason']),
                                 ('skipped', 'adjudication_already_exists'))
                self.assertEqual(self.database_state(), state)

    def test_complete_chain_is_recallable_and_uses_nullable_cycle_foreign_keys(self):
        candidate = self.candidate()
        original = deepcopy(candidate)
        result = self.produce(candidate)
        self.assertEqual(result['status'], 'validated_projection')
        self.assertEqual(result['claim'], projection_fixture.candidate_claim(candidate))
        self.assertEqual(candidate, original)
        self.assertEqual(self.recalled(), [('她今天亏了69元。',)])
        self.assertEqual(self.sql('''SELECT status,belief_type,metadata->>'review_status',
            created_by_cycle_id,updated_by_cycle_id FROM cognitive_beliefs'''),
            [('active', 'user_model', 'stable', None, None)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_questions'), [(1,)])
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(1,)])

    def test_all_supported_predicates_use_the_existing_canonical_writer(self):
        for index, (predicate, quote, summary, object_, value) in enumerate((
                ('reported_preference', '我喜欢喝咖啡', '她喜欢喝咖啡', '咖啡', '喜欢'),
                ('reported_state', '今天我很累需要休息', '她今天很累需要休息', '状态', '很累'),
                ('reported_identity', '我的职业是护士', '她的职业是护士', '职业', '护士'))):
            with self.subTest(predicate=predicate):
                candidate = self.candidate(source_event_id=f'predicate-{index}',
                    predicate=predicate, quote=quote, summary=summary,
                    object=object_, value=value)
                self.assertEqual(self.produce(candidate)['status'], 'validated_projection')

    def test_unsupported_preference_value_rejects_only_this_candidate(self):
        candidate = self.candidate(quote='我最爱喝黑咖啡了', summary='她最爱喝黑咖啡',
            predicate='reported_preference', object='饮品', value='黑咖啡')
        self.seed(candidate, adjudicated=False, include_claim=False)
        before = self.database_state()
        result = self.produce(candidate)
        self.assertEqual((result['status'], result['reason']),
                         ('rejected', 'unsupported_candidate_preference_value'))
        self.assertEqual(self.database_state(), before)
        independent = self.candidate(quote='我最爱喝黑咖啡了', summary='她最爱喝黑咖啡',
            object='饮品记录', value='黑咖啡')
        self.assertEqual(self.produce(independent)['status'], 'validated_projection')

    def test_projection_serializes_on_the_canonical_pair_lock_before_event_locks(self):
        from cognitive_queue import stable_advisory_lock_key
        with patch.object(self.database, 'query', wraps=self.database.query) as queries:
            self.assertEqual(self.produce()['status'], 'validated_projection')
        calls = [(call.args[0], call.args[1] if len(call.args) > 1 else ())
                 for call in queries.call_args_list]
        locks = [index for index, (sql, params) in enumerate(calls)
                 if 'pg_advisory_xact_lock' in sql
                 and params == (stable_advisory_lock_key('u', 'c'),)]
        event_locks = [index for index, (sql, _) in enumerate(calls)
                       if 'FROM cognitive_events' in sql and 'FOR UPDATE' in sql]
        self.assertTrue(locks)
        self.assertLess(locks[0], event_locks[0])

    def test_positive_value_embedded_in_negative_quote_cannot_gain_authority(self):
        for index, (quote, summary, value) in enumerate((
                ('我不喜欢咖啡这种饮料', '她不喜欢咖啡这种饮料', '喜欢'),
                ('我不讨厌咖啡这种饮料', '她不讨厌咖啡这种饮料', '讨厌'))):
            with self.subTest(value=value):
                candidate = self.candidate(source_event_id=f'polarity-{index}',
                    quote=quote, summary=summary, predicate='reported_preference',
                    object='咖啡', value=value)
                self.seed(candidate, adjudicated=False, include_claim=False)
                before = self.database_state()
                result = self.produce(candidate)
                self.assertEqual((result['status'], result['reason']),
                                 ('rejected', 'candidate_value_polarity_mismatch'))
                self.assertEqual(self.database_state(), before)

    def test_stable_operation_replay_is_a_noop_and_two_candidates_share_event(self):
        first = self.candidate()
        self.seed(first, text='今天我亏了69元，今天我买了咖啡。',
                  adjudicated=False, include_claim=False)
        second = self.candidate(quote='今天我买了咖啡', summary='她今天买了咖啡',
                                object='购买记录', value='买了咖啡')
        self.assertEqual(self.produce(first)['status'], 'validated_projection')
        self.assertEqual(self.produce(second)['status'], 'validated_projection')
        state = self.database_state()
        replay = self.produce(first)
        self.assertEqual(replay['status'], 'skipped')
        self.assertEqual(self.database_state(), state)
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(2,)])
        operations = self.sql('SELECT adjudication->\'operations\' FROM cognitive_events')[0][0]
        self.assertEqual(len(operations), 2)
        self.assertTrue(all(key.startswith('candidate.') for key in operations))

    def test_existing_applied_or_conflicting_pending_adjudication_is_untouched(self):
        for index, adjudication in enumerate((
                {'status': 'applied', 'action': 'answered'},
                {'status': 'pending', 'reason': 'conflicting_report_requires_confirmation'},
                {'status': 'superseded'})):
            with self.subTest(adjudication=adjudication):
                candidate = self.candidate(source_event_id=f'existing-{index}')
                seeded = self.seed(candidate, adjudicated=False, include_claim=False)
                self.sql('UPDATE cognitive_events SET adjudication=%s::jsonb WHERE id=%s',
                         (json.dumps(adjudication), seeded['event_id']))
                state = self.database_state()
                self.assertEqual(self.produce(candidate)['status'], 'skipped')
                self.assertEqual(self.database_state(), state)

    def test_incompatible_payload_claim_cannot_be_replaced(self):
        candidate = self.candidate()
        self.seed(candidate, adjudicated=False,
                  claim=projection_fixture.candidate_claim(dict(candidate, value='亏了96元')))
        state = self.database_state()
        result = self.produce(candidate)
        self.assertEqual((result['status'], result['reason']),
                         ('rejected', 'candidate_claim_scope_mismatch'))
        self.assertEqual(self.database_state(), state)

    def test_value_and_time_scope_require_verbatim_source_grounding(self):
        for index, changes in enumerate(({'value': '喜欢篮球'},
                                         {'time_scope': '2026-01-01'},
                                         {'time_scope': []})):
            with self.subTest(changes=changes):
                candidate = self.candidate(source_event_id=f'ungrounded-{index}', **changes)
                self.seed(candidate, adjudicated=False, include_claim=False)
                state = self.database_state()
                self.assertEqual(self.produce(candidate)['status'], 'rejected')
                self.assertEqual(self.database_state(), state)

    def test_conflicting_report_uses_canonical_pending_policy(self):
        first = self.candidate(source_event_id='report-one')
        second = self.candidate(source_event_id='report-two', quote='今天我亏了96元',
                                summary='她今天亏了96元', value='亏了96元')
        self.assertEqual(self.produce(first)['status'], 'validated_projection')
        result = self.produce(second)
        self.assertEqual((result['status'], result['reason']),
                         ('pending', 'conflicting_report_requires_confirmation'))
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(1,)])
        self.assertEqual(self.sql("SELECT metadata->>'review_status' FROM cognitive_beliefs"),
                         [('under_review',)])
        self.assertEqual(self.recalled(), [])

    def test_own_pending_operation_does_not_block_independent_fact_from_same_source(self):
        self.produce(self.candidate(source_event_id='prior-report'))
        conflicting = self.candidate(source_event_id='two-reports',
            quote='今天我亏了96元', summary='她今天亏了96元', value='亏了96元')
        self.seed(conflicting, text='今天我亏了96元，今天我买了咖啡。',
                  adjudicated=False, include_claim=False)
        self.assertEqual(self.produce(conflicting)['status'], 'pending')
        independent = self.candidate(source_event_id='two-reports',
            quote='今天我买了咖啡', summary='她今天买了咖啡', value='买了咖啡', object='购买记录')
        self.assertEqual(self.produce(independent)['status'], 'validated_projection')
        operations = self.sql('''SELECT adjudication->'operations' FROM cognitive_events
            WHERE source_event_id='two-reports' ''')[0][0]
        self.assertEqual(sorted(op['status'] for op in operations.values()), ['applied', 'pending'])
        self.assertEqual(self.recalled(), [('她今天买了咖啡。',)])

    def test_verbatim_relative_time_scope_does_not_require_date_inference(self):
        candidate = self.candidate(quote='昨天我买了咖啡', summary='她昨天买了咖啡',
                                  object='购买记录', value='买了咖啡', time_scope='昨天')
        result = self.produce(candidate)
        self.assertEqual(result['status'], 'validated_projection')
        self.assertEqual(result['claim']['time_scope'], '昨天')

    def test_source_deletion_removes_projected_fact_from_authoritative_recall(self):
        self.produce()
        self.assertEqual(len(self.recalled()), 1)
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='source'")
        self.assertEqual(self.recalled(), [])

    def test_projection_rejection_and_exception_rollback_all_partial_writes(self):
        candidate = self.candidate()
        self.seed(candidate, adjudicated=False, include_claim=False)
        state = self.database_state()
        with patch.object(memory_candidate, 'apply_candidate',
                          return_value={'status': 'rejected', 'reason': 'source_not_active_or_changed'}):
            self.assertEqual(self.produce(candidate)['status'], 'rejected')
        self.assertEqual(self.database_state(), state)
        with patch.object(memory_candidate, 'apply_candidate', side_effect=RuntimeError('offline')):
            with self.assertRaisesRegex(RuntimeError, 'offline'):
                self.produce(candidate)
        self.assertEqual(self.database_state(), state)


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE to the local test-only package')
class CanonicalBondFactLinkTests(unittest.TestCase):
    setUpClass = classmethod(projection_fixture.CandidateProjectionTests.setUpClass.__func__)
    tearDownClass = classmethod(projection_fixture.CandidateProjectionTests.tearDownClass.__func__)
    setUp = projection_fixture.CandidateProjectionTests.setUp
    tearDown = projection_fixture.CandidateProjectionTests.tearDown
    sql = projection_fixture.CandidateProjectionTests.sql
    source = projection_fixture.CandidateProjectionTests.source
    snapshot = projection_fixture.CandidateProjectionTests.snapshot
    database_state = projection_fixture.CandidateProjectionTests.database_state
    candidate = projection_fixture.CandidateProjectionTests.candidate
    seed = projection_fixture.CandidateProjectionTests.seed

    def fact(self, source_id='fact', *, occurred_at=None, text=None):
        candidate = self.candidate(source_event_id=source_id)
        seeded = self.seed(candidate, text=text)
        memory_candidate.apply_candidate(self.database.cursor(), 'u', 'c', candidate,
            event_id=seeded['event_id'], occurred_at=occurred_at or self.now - timedelta(hours=1),
            operation_id='main')
        return self.sql('SELECT id FROM long_memory WHERE authority_event_id=%s',
                        (seeded['event_id'],))[0][0]

    def bond(self, *, source_id='bond', occurred_at=None):
        candidate = self.candidate(source_event_id=source_id, quote='不要随意打电话',
            summary='她不要随意打电话', predicate='explicit_boundary', object='打电话', value='不要')
        seeded = self.seed(candidate)
        project_canonical_memory(self.database.cursor(), user_id='u', character_id='c',
            event_id=seeded['event_id'], source_id=source_id, content=seeded['content'],
            claim=seeded['claim'], belief_key=seeded['belief_key'],
            occurred_at=occurred_at or self.now, operation_id='main')
        return self.sql('SELECT linked_fact_id FROM bond_memory WHERE authority_event_id=%s',
                        (seeded['event_id'],))[0][0]

    def test_new_canonical_bond_links_to_current_fact(self):
        fact_id = self.fact()
        self.assertEqual(self.bond(), fact_id)
        self.assertEqual(self.sql(f'SELECT count(*) FROM bond_memory WHERE '
                                 f'{authoritative_memory_sql("bond_memory")}'), [(1,)])

    def test_no_fact_leaves_link_null_without_affecting_projection(self):
        self.assertIsNone(self.bond())
        self.assertEqual(self.sql('SELECT count(*) FROM bond_memory'), [(1,)])

    def test_badge_only_deleted_and_future_facts_are_not_link_targets(self):
        valid = self.fact('valid')
        removed = self.fact('removed', occurred_at=self.now - timedelta(minutes=5))
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='removed'")
        self.fact('future', occurred_at=self.now + timedelta(days=1))
        self.sql('''INSERT INTO long_memory (user_id,character_id,content,category,
            timestamp,authority) VALUES ('u','c','untrusted','其他',%s,'canonical_evidence_v1')''',
            (self.now,))
        self.assertNotEqual(valid, removed)
        self.assertEqual(self.bond(), valid)

    def test_only_invalid_facts_leave_link_null(self):
        self.fact()
        self.sql("UPDATE long_memory SET expires_at=CURRENT_TIMESTAMP - INTERVAL '1 second'")
        self.assertIsNone(self.bond())

    def test_foreign_user_and_character_facts_are_not_link_targets(self):
        other_user = self.fact('other-user')
        other_character = self.fact('other-character')
        self.sql("UPDATE long_memory SET user_id='other' WHERE id=%s", (other_user,))
        self.sql("UPDATE long_memory SET character_id='other' WHERE id=%s", (other_character,))
        self.assertIsNone(self.bond())

    def test_same_source_fact_is_preferred_over_a_more_recent_fact(self):
        from memory_authority import canonical_semantic_payload
        same_source = self.fact('same-source',
            text='今天我亏了69元。不要随意打电话。')
        self.fact('more-recent', occurred_at=self.now - timedelta(minutes=1))
        event_id, adjudication = self.sql('''SELECT id,adjudication FROM cognitive_events
            WHERE source_event_id='same-source' ''')[0]
        claim = projection_fixture.candidate_claim(self.candidate(
            source_event_id='same-source', quote='不要随意打电话', summary='她不要随意打电话',
            predicate='explicit_boundary', object='打电话', value='不要'))
        semantic = canonical_semantic_payload(user_id='u', character_id='c',
            source_id='same-source', claim=claim)
        belief_key = claim['question_key'] + f'.e{event_id}'
        content = '她不要随意打电话。'
        from role_view import PROJECTION_VERSION
        adjudication['operations']['boundary'] = {
            'status': 'applied', 'action': 'reported', 'belief_key': belief_key,
            'memory_table': 'bond_memory', 'memory_content': content,
            'projection_version': PROJECTION_VERSION, 'semantic_payload': semantic}
        self.sql('''UPDATE cognitive_events SET payload=payload - 'claim',
            adjudication=%s::jsonb WHERE id=%s''', (json.dumps(adjudication), event_id))
        self.sql('''INSERT INTO cognitive_beliefs
            (user_id,character_id,belief_key,statement,confidence,status,belief_type,metadata)
            VALUES ('u','c',%s,%s,0.8,'active','user_model',%s::jsonb)''',
            (belief_key, content, json.dumps({'review_status': 'stable'})))
        project_canonical_memory(self.database.cursor(), user_id='u', character_id='c',
            event_id=event_id, source_id='same-source', content=content, claim=claim,
            belief_key=belief_key, occurred_at=self.now, operation_id='boundary')
        self.assertEqual(self.sql("SELECT linked_fact_id FROM bond_memory"), [(same_source,)])
        self.assertEqual(self.sql(f'SELECT count(*) FROM bond_memory WHERE '
                                 f'{authoritative_memory_sql("bond_memory")}'), [(1,)])

