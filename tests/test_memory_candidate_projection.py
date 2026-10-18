"""Candidate projection through the real writer and real recall authority SQL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import unittest
from unittest.mock import patch

from tests import test_memory_candidate_source as source_fixture
import cognitive_revision
import memory_candidate
from memory_authority import authoritative_memory_sql, project_canonical_memory
from role_view import PROJECTION_VERSION, render_role_view
import user_memory


def candidate_claim(candidate):
    scope = {'subject': 'user:u', 'predicate': candidate['predicate'],
             'object': candidate['object'],
             'time_scope': candidate.get('time_scope', 'unspecified'),
             'source': 'canonical_user_turn'}
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True, ensure_ascii=False)
                            .encode('utf-8')).hexdigest()[:24]
    return dict(scope, value=candidate['value'], question_key='report.' + digest,
                text=candidate['quote'], display_summary=candidate['summary'])


class DisplaySummaryViewTests(unittest.TestCase):
    def test_summary_uses_existing_observer_rules_and_preserves_quoted_first_person(self):
        semantic = {'subject_ref': 'user:u', 'source_scope': 'gojo',
                    'predicate': 'reported_preference', 'object': '咖啡', 'value': '喜欢',
                    'display_summary': '我记得她说过「我喜欢咖啡」'}
        self.assertEqual(render_role_view('', observer_id='gojo', semantic=semantic),
                         '我记得她说过「我喜欢咖啡」。')
        self.assertEqual(render_role_view('', observer_id='geto', semantic=semantic),
                         '五条悟记得她说过「我喜欢咖啡」。')

    def test_absent_empty_or_non_text_summary_keeps_compositional_rendering(self):
        semantic = {'subject_ref': 'user:u', 'source_scope': 'gojo',
                    'predicate': 'reported_preference', 'object': '咖啡', 'value': '喜欢'}
        self.assertEqual(render_role_view('', observer_id='gojo', semantic=semantic),
                         '她喜欢咖啡。')
        for summary in ('', ' \t\n', None, 0, [], {}):
            with self.subTest(summary=summary):
                self.assertEqual(render_role_view('', observer_id='gojo',
                    semantic=dict(semantic, display_summary=summary)), '她喜欢咖啡。')


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE to the local test-only package')
class CandidateProjectionTests(unittest.TestCase):
    setUpClass = classmethod(source_fixture.CandidateSourceTests.setUpClass.__func__)
    tearDownClass = classmethod(source_fixture.CandidateSourceTests.tearDownClass.__func__)
    tearDown = source_fixture.CandidateSourceTests.tearDown
    sql = source_fixture.CandidateSourceTests.sql
    source = source_fixture.CandidateSourceTests.source
    snapshot = source_fixture.CandidateSourceTests.snapshot
    database_state = source_fixture.CandidateSourceTests.database_state

    def setUp(self):
        source_fixture.CandidateSourceTests.setUp(self)
        self.sql('ALTER TABLE chat_log ADD COLUMN reply_to_event_id TEXT')
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.stack.enter_context(patch('user_memory.get_conn', return_value=self.database))

    def candidate(self, **changes):
        candidate = {'source_event_id': 'source', 'quote': '今天我亏了69元',
                     'summary': '她今天亏了69元', 'predicate': 'reported_fact',
                     'object': '交易结果', 'value': '亏了69元'}
        candidate.update(changes)
        return candidate

    def seed(self, candidate=None, *, claim=None, text=None, adjudicated=True,
             include_claim=True, operation_id='main', legacy=False):
        """Fixture owns pre-existing adjudication; apply_candidate never creates it."""
        candidate = self.candidate() if candidate is None else candidate
        claim = candidate_claim(candidate) if claim is None else claim
        source_id = candidate['source_event_id']
        raw_text = text if text is not None else candidate['quote'] + '，想和你聊一聊。'
        self.source(text=raw_text, source_id=source_id)
        self.snapshot(raw_text, source_id=source_id)
        event_id = self.sql('SELECT id FROM cognitive_events WHERE source_event_id=%s',
                            (source_id,))[0][0]
        payload = {'content': raw_text}
        if include_claim:
            payload['claim'] = claim
        semantic = {'source_event_id': source_id, 'source_scope': 'c', 'observer_ref': 'c',
                    'subject_ref': claim['subject'], 'predicate': claim['predicate'],
                    'object': claim['object'], 'value': claim['value'],
                    'time_scope': claim['time_scope'], 'evidence_text': claim['text']}
        for key in ('display_summary', 'deadline'):
            if key in claim:
                semantic[key] = claim[key]
        table = ('bond_memory' if claim['predicate'] in (
            'explicit_boundary', 'explicit_refusal', 'explicit_promise',
            'reported_fulfillment') else 'long_memory')
        content = render_role_view('', observer_id='c', source_character_id='c', semantic=semantic)
        belief_key = claim['question_key'] + f'.e{event_id}'
        decision = {'status': 'applied', 'action': 'reported', 'belief_key': belief_key,
                    'memory_table': table, 'memory_content': content,
                    'projection_version': PROJECTION_VERSION, 'semantic_payload': semantic}
        adjudication = {'status': 'applied', 'operations': {operation_id: decision}}
        if legacy:
            decision.pop('semantic_payload')
            decision['projection_version'] = 'legacy_v1'
        if not adjudicated:
            adjudication = {}
        self.sql('UPDATE cognitive_events SET payload=%s::jsonb, adjudication=%s::jsonb WHERE id=%s',
                 (json.dumps(payload, ensure_ascii=False),
                  json.dumps(adjudication, ensure_ascii=False), event_id))
        if adjudicated:
            self.sql('''INSERT INTO cognitive_beliefs
                (user_id,character_id,belief_key,statement,confidence,status,belief_type,metadata)
                VALUES ('u','c',%s,%s,0.8,'active','user_model',%s::jsonb)''',
                (belief_key, content, json.dumps({'review_status': 'stable',
                    'semantic_payload': semantic, 'projection_version': PROJECTION_VERSION})))
        return {'event_id': event_id, 'operation_id': operation_id, 'claim': claim,
                'semantic': semantic, 'belief_key': belief_key, 'content': content,
                'payload': payload, 'adjudication': adjudication, 'table': table}

    def apply(self, candidate, seeded, **overrides):
        options = {'occurred_at': self.now, 'event_id': seeded['event_id'],
                   'operation_id': seeded['operation_id']}
        options.update(overrides)
        original = deepcopy(candidate)
        authority_before = {table: rows for table, rows in self.database_state().items()
                            if table not in ('long_memory', 'bond_memory', 'memory_source_events')}
        result = memory_candidate.apply_candidate(
            self.database.cursor(), 'u', 'c', candidate, **options)
        self.assertEqual(candidate, original)
        authority_after = {table: rows for table, rows in self.database_state().items()
                           if table not in ('long_memory', 'bond_memory', 'memory_source_events')}
        self.assertEqual(authority_after, authority_before)
        return result

    def recalled(self, table='long_memory'):
        return self.sql(f'SELECT content FROM {table} WHERE {authoritative_memory_sql(table)}')

    def test_natural_summary_projects_with_exact_quote_and_is_immediately_recallable(self):
        candidate = self.candidate()
        seeded = self.seed(candidate)
        result = self.apply(candidate, seeded)
        self.assertEqual(result['status'], 'validated_projection')
        self.assertEqual(result['claim'], seeded['claim'])
        self.assertEqual(result['semantic_payload'], seeded['semantic'])
        self.assertEqual(result['memory_content'], '她今天亏了69元。')
        self.assertNotEqual(result['claim']['text'], candidate['summary'])
        self.assertFalse(result['perspective_dropped'])
        self.assertEqual(self.recalled(), [('她今天亏了69元。',)])
        self.assertEqual(user_memory.get_long_memory('u', 'c')[0][0], '她今天亏了69元。')
        self.assertEqual(self.sql('SELECT authority FROM long_memory'),
                         [('canonical_evidence_v1',)])

    def test_category_expiry_and_dependencies_follow_the_existing_projector(self):
        for index, (predicate, category) in enumerate((
                ('reported_preference', '喜好'), ('reported_state', '状态'),
                ('reported_identity', '身份'), ('reported_fact', '其他'))):
            with self.subTest(predicate=predicate):
                source_id = f'source-{index}'
                candidate = self.candidate(source_event_id=source_id, predicate=predicate)
                seeded = self.seed(candidate)
                self.apply(candidate, seeded)
                row = self.sql('''SELECT id,category,expires_at,source_event_refs,
                    authority_belief_key FROM long_memory WHERE authority_event_id=%s''',
                    (seeded['event_id'],))[0]
                self.assertEqual(row[1], category)
                self.assertEqual(row[2], self.now + timedelta(hours=48)
                                 if predicate == 'reported_state' else None)
                self.assertEqual(row[3], [{'source_id': source_id}])
                self.assertEqual(row[4], seeded['belief_key'])
                self.assertEqual(self.sql('''SELECT memory_type,source_event_id
                    FROM memory_source_events WHERE memory_id=%s''', (row[0],)),
                    [('long_memory', source_id)])
        self.assertEqual(len(self.recalled()), 4)

    def test_replaying_event_and_operation_does_not_duplicate_projection_or_links(self):
        candidate = self.candidate()
        seeded = self.seed(candidate, operation_id='candidate-1')
        first = self.apply(candidate, seeded)
        second = self.apply(candidate, seeded)
        self.assertEqual(first, second)
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(1,)])
        self.assertEqual(self.sql('SELECT count(*) FROM memory_source_events'), [(1,)])
        self.assertEqual(self.recalled(), [('她今天亏了69元。',)])

    def test_unadjudicated_projection_cannot_create_recall_authority(self):
        candidate = self.candidate()
        seeded = self.seed(candidate, adjudicated=False)
        result = self.apply(candidate, seeded)
        self.assertEqual(result['status'], 'validated_projection')
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(1,)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs'), [(0,)])
        self.assertEqual(self.sql('SELECT adjudication FROM cognitive_events'), [({},)])
        self.assertEqual(self.recalled(), [])
        self.assertEqual(user_memory.get_long_memory('u', 'c'), [])

    def test_missing_adjudication_operation_rejects_without_projection_or_links(self):
        candidate = self.candidate()
        seeded = self.seed(candidate, operation_id='other-op')
        before = self.database_state()
        with patch.object(memory_candidate, 'project_canonical_memory',
                          side_effect=AssertionError('unexpected projection')) as project:
            result = self.apply(candidate, seeded, operation_id='main')
        self.assertEqual(result, {
            'status': 'rejected', 'reason': 'adjudication_operation_missing'})
        project.assert_not_called()
        self.assertEqual(self.sql('SELECT count(*) FROM long_memory'), [(0,)])
        self.assertEqual(self.sql('SELECT count(*) FROM memory_source_events'), [(0,)])
        self.assertEqual(self.database_state(), before)

    def test_source_edit_rejects_cached_projection_even_when_quote_survives(self):
        candidate = self.candidate()
        seeded = self.seed(candidate)
        self.apply(candidate, seeded)
        self.assertEqual(len(self.recalled()), 1)
        self.sql("UPDATE chat_log SET text=replace(text, '想', '不')")
        self.assertIn(candidate['quote'], self.sql('SELECT text FROM chat_log')[0][0])
        self.assertEqual(self.recalled(), [])
        self.assertEqual(user_memory.get_long_memory('u', 'c'), [])

    def test_each_semantic_field_tampered_only_on_projection_loses_authority(self):
        candidate = self.candidate()
        seeded = self.seed(candidate)
        self.apply(candidate, seeded)
        for field in seeded['semantic']:
            with self.subTest(field=field):
                changed = dict(seeded['semantic'], **{field: 'tampered'})
                self.sql('UPDATE long_memory SET semantic_payload=%s::jsonb',
                         (json.dumps(changed, ensure_ascii=False),))
                self.assertEqual(self.recalled(), [])
                self.sql('UPDATE long_memory SET semantic_payload=%s::jsonb',
                         (json.dumps(seeded['semantic'], ensure_ascii=False),))
                self.assertEqual(len(self.recalled()), 1)

    def test_each_semantic_field_tampered_only_on_adjudication_loses_authority(self):
        candidate = self.candidate()
        seeded = self.seed(candidate)
        self.apply(candidate, seeded)
        for field in seeded['semantic']:
            with self.subTest(field=field):
                changed = deepcopy(seeded['adjudication'])
                changed['operations']['main']['semantic_payload'][field] = 'tampered'
                self.sql('UPDATE cognitive_events SET adjudication=%s::jsonb',
                         (json.dumps(changed, ensure_ascii=False),))
                self.assertEqual(self.recalled(), [])
                self.sql('UPDATE cognitive_events SET adjudication=%s::jsonb',
                         (json.dumps(seeded['adjudication'], ensure_ascii=False),))
                self.assertEqual(len(self.recalled()), 1)

    def test_each_existing_claim_authority_field_tampered_only_on_payload_loses_authority(self):
        candidate = self.candidate()
        seeded = self.seed(candidate)
        self.apply(candidate, seeded)
        for field in ('predicate', 'object', 'value', 'time_scope', 'text'):
            with self.subTest(field=field):
                changed = deepcopy(seeded['payload'])
                changed['claim'][field] = 'tampered'
                self.sql('UPDATE cognitive_events SET payload=%s::jsonb',
                         (json.dumps(changed, ensure_ascii=False),))
                self.assertEqual(self.recalled(), [])
                self.sql('UPDATE cognitive_events SET payload=%s::jsonb',
                         (json.dumps(seeded['payload'], ensure_ascii=False),))
                self.assertEqual(len(self.recalled()), 1)

    def test_no_claim_path_requires_evidence_to_be_verbatim_even_when_semantics_match(self):
        candidate = self.candidate()
        seeded = self.seed(candidate, include_claim=False)
        self.apply(candidate, seeded)
        self.assertEqual(len(self.recalled()), 1)
        forged = dict(seeded['semantic'], evidence_text='不存在的原文引用')
        decision = deepcopy(seeded['adjudication'])
        decision['operations']['main']['semantic_payload'] = forged
        self.sql('UPDATE long_memory SET semantic_payload=%s::jsonb',
                 (json.dumps(forged, ensure_ascii=False),))
        self.sql('UPDATE cognitive_events SET adjudication=%s::jsonb',
                 (json.dumps(decision, ensure_ascii=False),))
        self.assertEqual(self.recalled(), [])
        self.assertEqual(user_memory.get_long_memory('u', 'c'), [])

    def test_every_perspective_is_dropped_and_logged_without_persisting_its_content(self):
        for index, perspective in enumerate(('我想认真听她说', '她其实很在意')):
            with self.subTest(perspective=perspective):
                candidate = self.candidate(source_event_id=f'perspective-{index}',
                                           perspective=perspective)
                seeded = self.seed(candidate)
                with self.assertLogs('memory_candidate', level='INFO') as logged:
                    result = self.apply(candidate, seeded)
                self.assertTrue(result['perspective_dropped'])
                self.assertIn('perspective_dropped', '\n'.join(logged.output))
                self.assertIn(candidate['source_event_id'], '\n'.join(logged.output))
                self.assertNotIn(perspective, '\n'.join(logged.output))
                persisted = json.dumps(self.database_state(), ensure_ascii=False, default=str)
                self.assertNotIn(perspective, persisted)
                self.assertNotIn(perspective, json.dumps(result, ensure_ascii=False))
                self.assertNotIn('perspective', result['semantic_payload'])
                self.assertNotIn('perspective', result['claim'])
        self.assertEqual(len(self.recalled()), 2)

    def test_rejected_candidates_produce_no_writes_and_preserve_input(self):
        seeded = self.seed()
        for changes, reason in (
                ({'quote': '原文里没有这句话'}, 'quote_not_verbatim'),
                ({'summary': '她今天亏了96元'}, 'number_value_mismatch'),
                ({'object': ''}, 'invalid_predicate_or_summary'),
                ({'value': None}, 'invalid_predicate_or_summary')):
            with self.subTest(reason=reason, changes=changes):
                candidate = self.candidate(perspective='她其实很在意', **changes)
                before = self.database_state()
                self.assertEqual(self.apply(candidate, seeded),
                                 {'status': 'rejected', 'reason': reason})
                self.assertEqual(self.database_state(), before)

    def test_foreign_event_id_cannot_be_used_for_projection(self):
        candidate = self.candidate()
        seeded = self.seed(candidate)
        other = self.seed(self.candidate(source_event_id='another-source'))
        before = self.database_state()
        result = self.apply(candidate, seeded, event_id=other['event_id'])
        self.assertEqual(result, {'status': 'rejected', 'reason': 'source_not_active_or_changed'})
        self.assertEqual(self.database_state(), before)

    def test_database_writer_exception_propagates_instead_of_reporting_success(self):
        candidate = self.candidate()
        seeded = self.seed(candidate)
        before = self.database_state()
        query = self.database.query

        def fail_insert(sql, params=()):
            if sql.lstrip().startswith('INSERT INTO long_memory'):
                raise RuntimeError('injected_projection_failure')
            return query(sql, params)

        with patch.object(self.database, 'query', side_effect=fail_insert):
            with self.assertRaisesRegex(RuntimeError, 'injected_projection_failure'):
                self.apply(candidate, seeded)
        self.assertEqual(self.database_state(), before)

    def test_legacy_projection_remains_recallable_without_new_summary_fields(self):
        claim = cognitive_revision.parse_explicit_report('我喜欢咖啡', 'u', 'c')
        candidate = self.candidate(quote='我喜欢咖啡')
        seeded = self.seed(candidate, claim=claim, text='我喜欢咖啡', legacy=True)
        self.sql('''INSERT INTO long_memory (user_id,character_id,content,category,authority,
            authority_event_id,authority_belief_key,authority_operation_id,projection_version)
            VALUES ('u','c',%s,'喜好','canonical_evidence_v1',%s,%s,'main','legacy_v1')''',
            (seeded['content'], seeded['event_id'], seeded['belief_key']))
        self.assertEqual(self.recalled(), [('她喜欢咖啡。',)])
        self.assertEqual(user_memory.get_long_memory('u', 'c')[0][0], '她喜欢咖啡。')

    def test_seven_existing_literal_forms_keep_their_exact_rendered_projection(self):
        examples = (
            ('我喜欢咖啡', '她喜欢咖啡。'),
            ('我的职业是「教师」', '她的职业是「教师」。'),
            ('我拒绝「一起出门」', '她拒绝「一起出门」。'),
            ('不要「谈论体重」', '她明确说过：不要「谈论体重」。'),
            ('我承诺在2026-10-20前完成「作业」', '她承诺在2026-10-20前完成「作业」。'),
            ('我已兑现截至2026-10-20的承诺「作业」',
             '她说自己已兑现截至2026-10-20的承诺「作业」。'),
            ('在2026-10-18，我喜欢茶', '在2026-10-18，她喜欢茶。'),
        )
        for index, (text, expected) in enumerate(examples):
            with self.subTest(text=text):
                claim = cognitive_revision.parse_explicit_report(text, 'u', 'c')
                self.assertIsNotNone(claim)
                candidate = self.candidate(source_event_id=f'literal-{index}', quote=text)
                seeded = self.seed(candidate, claim=claim, text=text)
                result = project_canonical_memory(
                    self.database.cursor(), user_id='u', character_id='c',
                    event_id=seeded['event_id'], source_id=candidate['source_event_id'],
                    content='ignored display fallback', claim=claim,
                    belief_key=seeded['belief_key'], occurred_at=self.now)
                self.assertEqual(result['memory_content'], expected)
                self.assertNotIn('display_summary', result['semantic_payload'])
                self.assertIn((expected,), self.recalled(seeded['table']))


if __name__ == '__main__':
    unittest.main()
