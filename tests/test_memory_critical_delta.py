"""Critical memory regressions through real canonical SQL, with LLMs disabled."""
import json
import os
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import patch

BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import raw_events
import user_memory


OLD = '她叫我宝宝，问我接不接受'
NEW = '她叫我宝宝，追问我是否接受，我明确说了yes'
DELTA = '我明确接受她叫我“宝宝”。'
EVENTS = [
    {'event_id': 'u1', 'role': 'user', 'content': '我还是叫你宝宝。你接不接受这个称呼？'},
    {'event_id': 'chat_reply:u1', 'role': 'assistant',
     'content': 'yes，接受你叫我宝宝。',
     'metadata': {'assistant_turn_id': 'chat_reply:u1', 'reply_to_event_id': 'u1'}},
]


def delta(**overrides):
    value = {
        'kind': 'explicit_acceptance', 'novel': True, 'content': DELTA,
        'question_text': '是否接受她叫我宝宝', 'question_key': 'nickname.acceptance',
        'value': 'yes', 'replaces': [OLD],
        'evidence_quote': 'yes，接受你叫我宝宝。',
        'evidence_event_ids': ['u1', 'chat_reply:u1'],
    }
    value.update(overrides)
    return value


from tests.canonical_memory_fixture import CanonicalMemoryFixture


class CriticalBondDeltaTests(CanonicalMemoryFixture, unittest.TestCase):

    def extract(self, item, *, old=OLD, events=None, sources_active=True,
                repair=None, question_state=None, merge_result=(False, 0),
                cognitive_update=None, merge_targets=None, repair_raw=None,
                pending_corrections=None, payload_extra=None):
        events = EVENTS if events is None else events
        # Materialize the summary result independently; it has no authority over
        # the canonical answer, regardless of the former merge outcome.
        summaries = [NEW if merge_result[0] else old, *(merge_targets or [])]
        for summary in dict.fromkeys(summaries):
            if not self.sql('SELECT id FROM bond_memory WHERE content=%s', (summary,)):
                self.sql("INSERT INTO bond_memory(user_id,character_id,content) VALUES ('u','c',%s)", (summary,))
        self.database.commit()
        payload = {'bond_delta': item, 'cognitive_update': cognitive_update,
                   'bond': {'content': NEW, 'replaces': merge_targets or [old]}}
        payload.update(payload_extra or {})
        # Observe compatibility writers without replacing their behavior. The
        # canonical ingress and deterministic worker below execute actual SQL.
        with ExitStack() as stack:
            watched = [stack.enter_context(patch.object(module, name, wraps=getattr(module, name)))
                       for module, name in (
                           (user_memory, 'merge_bond_memories'),
                           (user_memory, 'save_bond_memory'),
                           (user_memory, 'resolve_bond_memories'),
                           (__import__('cognitive_events'), 'ingest_question_update'))]
            ok = self.ingest_sources(
                events, source_ids=['u1', 'chat_reply:u1'],
                copied_user=EVENTS[0]['content'], copied_assistant=EVENTS[1]['content'],
                model_payload=repair_raw if repair_raw is not None else payload,
                deleted=[] if sources_active else [e['event_id'] for e in events])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)
        for writer in watched:
            writer.assert_not_called()
        return ok, *watched

    def assert_delta(self, value='yes', actor='character'):
        rows = self.sql("""SELECT metadata FROM cognitive_questions
            WHERE status='resolved' AND metadata->>'updated_by'='explicit_answer_v1'""")
        self.assertEqual(len(rows),1)
        judgment = rows[0][0]['resolution']
        self.assertEqual((judgment['value'],judgment['actor']),(value,actor))
        from cognitive_reader import fetch_cognitive_reader_state
        state = fetch_cognitive_reader_state('u','c',conn=self.database)
        self.assertTrue(any(q['metadata'].get('resolution') == judgment for q in state['questions']))
        with patch.object(user_memory,'get_conn',return_value=self.database):
            self.assertIn(judgment['content'], [r[1] for r in user_memory.get_bond_memories('u','c')])
        ids = self.sql("""SELECT m.source_event_id FROM memory_source_events m JOIN bond_memory b
            ON b.id=m.memory_id AND m.memory_type='bond_memory' WHERE b.content=%s""", (judgment['content'],))
        self.assertEqual({r[0] for r in ids},set(judgment['evidence_event_ids']))
        return judgment

    def test_A_merge_reject_saves_only_explicit_acceptance_delta(self):
        ok, *_ = self.extract(delta())
        self.assertTrue(ok)
        judgment = self.assert_delta()
        self.assertEqual(set(judgment['evidence_event_ids']), {'u1','chat_reply:u1'})
        self.assertIn('角色被用户称作「宝宝」', judgment['content'])
        self.assertEqual(self.sql('SELECT content,recall_status FROM bond_memory WHERE authority_event_id IS NULL'), [(OLD,'active')])


    def test_B_same_event_duplicate_is_not_appended(self):
        self.extract(delta(content='她叫我宝宝',novel=False),old='她叫我宝宝')
        before = self.sql('SELECT id,content,authority_event_id,authority_operation_id FROM bond_memory ORDER BY id')
        self.extract(delta(content='她叫我宝宝',novel=False),old='她叫我宝宝')
        self.assertEqual(self.sql('SELECT id,content,authority_event_id,authority_operation_id FROM bond_memory ORDER BY id'),before)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0],2)
        self.assert_delta()

    def test_model_novel_flag_cannot_bypass_exact_duplicate(self):
        self.extract(delta(),old=DELTA)
        before = self.sql('SELECT id,adjudication FROM cognitive_events ORDER BY id')
        self.extract(delta(novel=True),old=DELTA)
        self.assertEqual(self.sql('SELECT id,adjudication FROM cognitive_events ORDER BY id'),before)
        self.assert_delta()

    def test_no_canonical_assistant_evidence_cannot_save_character_yes(self):
        _ok, _merge, save, resolve, _lifecycle = self.extract(delta(), events=EVENTS[:1])
        save.assert_not_called()
        resolve.assert_not_called()

    def test_forged_event_or_quote_is_rejected(self):
        for item in (delta(evidence_event_ids=['u1','fake']),delta(evidence_quote='绝对没有说过')):
            self.extract(item)
            judgment = self.assert_delta()
            self.assertNotIn('fake',str(judgment))
            self.assertNotIn('绝对没有说过',str(judgment))

    def test_deleted_source_does_not_commit_delta(self):
        _ok, _merge, save, resolve, _lifecycle = self.extract(delta(), sources_active=False)
        save.assert_not_called()
        resolve.assert_not_called()

    def test_user_explicit_answer_uses_user_canonical_source(self):
        self.extract(delta(actor='user'),events=[{'event_id':'u1','role':'user','content':'我明确接受你叫我宝宝。'}])
        judgment = self.assert_delta(actor='user')
        self.assertEqual(judgment['evidence_event_ids'],['u1'])

    def test_explicit_promise_does_not_require_an_answer_value(self):
        self.extract(delta(kind='explicit_promise',value=None),events=[EVENTS[0],dict(EVENTS[1],content='明天给你回答。')])
        from cognitive_reader import fetch_cognitive_reader_state
        row = fetch_cognitive_reader_state('u','c',conn=self.database)['questions'][0]
        self.assertEqual(row['status'],'active')
        self.assertEqual(row['metadata']['pending_answer']['status'],'pending')
        self.assertNotIn('resolution',row['metadata'])
        with patch.object(user_memory,'get_conn',return_value=self.database):
            self.assertTrue(any('仍待回答' in r[1] for r in user_memory.get_bond_memories('u','c')))

    def test_literal_yes_can_resolve_a_scoped_question_without_restatement(self):
        self.extract(delta(evidence_quote='yes'),events=[EVENTS[0],dict(EVENTS[1],content='yes')])
        self.assert_delta()

    def test_literal_no_cannot_support_an_acceptance_delta(self):
        self.extract(delta(evidence_quote='no'),events=[EVENTS[0],dict(EVENTS[1],content='no')])
        self.assert_delta(value='no')

    def test_missing_delta_is_extracted_before_writes_and_used_after_merge_reject(self):
        self.extract(None)
        self.assert_delta()
        for guard in self.model_guards:
            guard.assert_not_called()


    def test_delta_parse_or_schema_failure_precedes_every_domain_write(self):
        # The deleted extractor cannot interpret any of these outputs or invoke
        # direct writers. Canonical assistant text remains a quoted utterance.
        for raw in ('not JSON', '{"bond_delta": []}',
                    '{"bond_delta": null} {"bond_delta": {"novel": true}}'):
            with self.subTest(raw=raw):
                ok, merge, save, resolve, lifecycle = self.extract(
                    None, repair_raw=raw, pending_corrections=[(7, 'old fact')],
                    payload_extra={'user_fact': {'content': '她叫我宝宝'}})
                self.assertTrue(ok)
                for writer in (merge, save, resolve, lifecycle):
                    writer.assert_not_called()
                self.assertEqual(self.sql('SELECT count(*) FROM long_memory')[0][0], 0)
                self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)
                for guard in self.model_guards:
                    guard.assert_not_called()

    def test_successful_additive_merge_cannot_erase_independent_yes(self):
        self.extract(delta(novel=False),merge_result=(True,1))
        self.assert_delta()
        self.assertEqual(self.sql('SELECT content,recall_status FROM bond_memory WHERE authority_event_id IS NULL'),[(NEW,'active')])

    def test_unrelated_successful_merge_is_not_retired_by_acceptance(self):
        self.extract(delta(),merge_result=(True,1),merge_targets=['她答应周六一起散步'])
        self.assert_delta()
        self.assertEqual(self.sql("SELECT recall_status FROM bond_memory WHERE content='她答应周六一起散步'"),[('active',)])

    def test_terminal_and_unrelated_fragments_are_not_destructively_merged(self):
        self.sql("INSERT INTO bond_memory(user_id,character_id,content,recall_status) VALUES ('u','c','她答应周六一起散步','completed')")
        self.database.commit()
        self.extract(delta(),merge_result=(True,2),merge_targets=[OLD,'她答应周六一起散步'])
        self.assert_delta()
        self.assertEqual(self.sql("SELECT recall_status FROM bond_memory WHERE content='她答应周六一起散步'"),[('completed',)])
        self.assertEqual(self.sql('SELECT recall_status FROM bond_memory WHERE content=%s',(OLD,)),[('active',)])

    def test_replay_uses_committed_delta_if_extractor_now_calls_it_duplicate(self):
        import db_generation_receipt as receipt
        self.stack.enter_context(patch.object(receipt, 'get_conn', return_value=self.database))
        receipt.init_generation_receipt_table()
        events = [dict(EVENTS[0], content='我接受「宝宝这个称呼」。'), EVENTS[1]]
        self.assertTrue(self.ingest_sources(events, source_ids=['u1', 'chat_reply:u1']))
        gate = receipt.resolve_generation('u', 'c', 'u1', 'chat_text')
        payload = {'messages': [{'jp': EVENTS[1]['content'], 'zh': '接受这个称呼。'}]}
        self.assertTrue(receipt.complete_generation(
            'u', 'c', 'u1', 'chat_text', gate['claim_token'], payload, effects=[]))
        tables = ('cognitive_events', 'cognitive_event_triggers', 'cognitive_cycles',
                  'cognitive_questions', 'cognitive_beliefs', 'bond_memory', 'memory_source_events')
        before = {table: self.sql(f'SELECT * FROM {table} ORDER BY 1') for table in tables}
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_events WHERE adjudication->>'status'='applied'")[0][0], 2)
        for _ in range(2):
            diagnostic = {}
            self.assertTrue(user_memory.extract_and_save_memory(
                'u', 'ignored copied text', 'ignored model reply', 'c',
                source_event_id='u1', source_event_ids=['u1', 'chat_reply:u1'],
                parsed_override={'bond_delta': delta(novel=False)}, backfill_result=diagnostic))
            self.assertEqual(diagnostic['reason'], 'canonical_judgment_already_committed')
            self.assertFalse(diagnostic['judgment_restored'])
            self.assertEqual(receipt.resolve_generation('u', 'c', 'u1', 'chat_text')['action'], 'replay')
            self.assertEqual(receipt.get_generation('u', 'c', 'u1', 'chat_text')['response_json']['messages'],
                             payload['messages'])
        self.assertEqual(before, {table: self.sql(f'SELECT * FROM {table} ORDER BY 1') for table in tables})

    def test_historical_model_candidate_without_canonical_source_is_not_restored(self):
        from cognitive_events import record_source_event
        record_source_event(self.database, user_id='u', character_id='c',
            source_event_type='relationship_signals', source_event_id='u1',
            source='legacy_model', occurred_at=self.now,
            payload={'bond_delta': delta(), 'current_judgment': {'status': 'committed', 'value': 'yes'}})
        self.database.commit()
        diagnostic = {}
        self.assertFalse(user_memory.extract_and_save_memory(
            'u', EVENTS[0]['content'], EVENTS[1]['content'], 'c', source_event_id='u1',
            parsed_override={'bond_delta': delta()}, backfill_result=diagnostic))
        self.assertEqual(diagnostic['reason'], 'canonical_source_unavailable')
        self.assertFalse(diagnostic['judgment_restored'])
        self.assert_no_authority()

    def test_duplicate_without_valid_historical_adjudication_explains_non_restoration(self):
        from cognitive_events import ingest_canonical_turn
        self.source('u1', '我接受「宝宝这个称呼」。')
        ingest_canonical_turn(user_id='u', character_id='c', source_event_id='u1')
        for decision in ({}, {'status': 'applied', 'processed_by_cycle_id': 99999}):
            with self.subTest(decision=decision):
                self.sql('UPDATE cognitive_events SET adjudication=%s::jsonb', (json.dumps(decision),))
                self.database.commit()
                diagnostic = {}
                self.assertTrue(user_memory.extract_and_save_memory(
                    'u', '', '', 'c', source_event_id='u1',
                    parsed_override={'bond_delta': delta()}, backfill_result=diagnostic))
                self.assertEqual(diagnostic['status'], 'duplicate')
                self.assertEqual(diagnostic['reason'], 'historical_adjudication_missing')
                self.assertFalse(diagnostic['judgment_restored'])
                self.assert_no_authority()

    def test_historical_recovery_cannot_revive_deleted_or_retracted_source(self):
        self.ingest_sources([dict(EVENTS[0], content='我接受「宝宝这个称呼」。')])
        from memory_authority import authoritative_memory_sql
        for status in ('deleted', 'retracted'):
            with self.subTest(status=status):
                self.sql('UPDATE chat_log SET status=%s', (status,))
                self.database.commit()
                diagnostic = {}
                self.assertFalse(user_memory.extract_and_save_memory(
                    'u', '', '', 'c', source_event_id='u1', backfill_result=diagnostic))
                self.assertEqual(diagnostic['reason'], 'canonical_source_unavailable')
                self.assertEqual(self.sql('SELECT status FROM chat_log'), [(status,)])
                self.assertEqual(self.sql('SELECT count(*) FROM bond_memory WHERE ' +
                                         authoritative_memory_sql('bond_memory'))[0][0], 0)

    def test_historical_source_read_error_is_not_success_or_duplicate(self):
        self.source('u1', '我接受「宝宝这个称呼」。')
        self.sql('ALTER TABLE chat_log RENAME COLUMN text TO unavailable_text')
        self.database.commit()
        diagnostic = {}
        with self.assertRaises(raw_events.SourceValidityError):
            user_memory.extract_and_save_memory(
                'u', '', '', 'c', source_event_id='u1', backfill_result=diagnostic)
        self.assertEqual(diagnostic['status'], 'not_restored')
        self.assertEqual(diagnostic['reason'], 'canonical_ingress_error')
        self.assertFalse(diagnostic['judgment_restored'])
        self.assert_no_authority()

    def test_historical_superseded_judgment_is_not_reapplied(self):
        self.ingest_sources([dict(EVENTS[0], content='我喜欢咖啡。')])
        self.ingest_sources([{'event_id': 'correction', 'role': 'user',
                              'content': '更正：「我喜欢咖啡」不对，应为「我讨厌咖啡」。'}],
                            primary='correction')
        before = self.sql('SELECT * FROM cognitive_beliefs ORDER BY id')
        diagnostic = {}
        self.assertTrue(user_memory.extract_and_save_memory(
            'u', '', '', 'c', source_event_id='u1', parsed_override={'bond_delta': delta()},
            backfill_result=diagnostic))
        self.assertEqual(diagnostic['reason'], 'historical_adjudication_superseded')
        self.assertFalse(diagnostic['judgment_restored'])
        self.assertEqual(self.sql('SELECT * FROM cognitive_beliefs ORDER BY id'), before)
        self.assertIn('讨厌咖啡', self.sql("SELECT statement FROM cognitive_beliefs WHERE status='active'")[0][0])

    def test_future_answer_extraction_reaches_existing_question_ingress(self):
        self.extract(None,events=[EVENTS[0],dict(EVENTS[1],content='明天给你回答。')])
        rows = self.sql("SELECT status,metadata->'pending_answer'->>'status' FROM cognitive_questions")
        self.assertEqual(rows,[('active','pending')])
        self.assertGreaterEqual(self.sql("SELECT count(*) FROM cognitive_cycles WHERE status='succeeded'")[0][0],1)

    def test_rejected_merge_preserves_old_row_and_persists_delta_provenance(self):
        self.extract(delta())
        judgment = self.assert_delta()
        self.assertEqual(set(judgment['evidence_event_ids']),{'u1','chat_reply:u1'})
        self.assertEqual(self.sql('SELECT content FROM bond_memory WHERE authority_event_id IS NULL'),[(OLD,)])
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_questions WHERE status='resolved'")[0][0],1)


if __name__ == '__main__':
    unittest.main()
