"""Hard acceptance: canonical ingress -> queue -> revision -> reader, LLM off."""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gojo_backend'))
import cognitive_revision as revision


class GrammarTests(unittest.TestCase):
    def test_closed_grammar_does_not_classify_irony_keywords(self):
        for text in ('我讨厌你才怪', '她说我喜欢咖啡', '我喜欢咖啡吗？',
                     '我喜欢咖啡，开玩笑的', '如果我喜欢咖啡', '我喜欢咖啡？',
                     '“我喜欢咖啡”', '讨厌你，笨蛋', '真是太喜欢了呵呵'):
            with self.subTest(text=text):
                self.assertEqual(revision.parse_cognitive_evidence(text, 'u', 'c')['operation'], 'pending')
        self.assertEqual(revision.parse_cognitive_evidence('我喜欢咖啡。', 'u', 'c')['operation'], 'report')

    def test_correction_cannot_change_subject_object_or_scope(self):
        for old, new in (('我讨厌咖啡', '她喜欢咖啡'), ('我讨厌咖啡', '我喜欢茶'),
                         ('在2026-09-01，我讨厌咖啡', '在2026-09-02，我喜欢咖啡')):
            text = f'更正：「{old}」不对，应为「{new}」。'
            self.assertEqual(revision.parse_cognitive_evidence(text, 'u', 'c')['operation'], 'pending')


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'set COGNITIVE_TEST_PGLITE to the local test-only package')
class OfflineDatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def setUp(self):
        import cognitive_db
        import cognitive_worker
        self.database.rollback()
        self.database.query('DROP SCHEMA public CASCADE')
        self.database.query('CREATE SCHEMA public')
        cur = self.database.cursor()
        for ddl in cognitive_db.ddl_statements():
            cur.execute(ddl)
        cur.execute('''CREATE TABLE chat_log (id BIGSERIAL PRIMARY KEY, user_id TEXT, chat_id TEXT,
            event_id TEXT, client_msg_id TEXT, role TEXT, text TEXT, kind TEXT DEFAULT 'text',
            extra TEXT DEFAULT '{}', created_at TIMESTAMPTZ, subtitle TEXT DEFAULT '',
            status TEXT DEFAULT 'active', reply_to_event_id TEXT)''')
        self.database.commit()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('db.get_conn', return_value=self.database))
        self.stack.enter_context(patch('raw_events.get_conn', return_value=self.database))
        self.stack.enter_context(patch('context_layer.get_conn', return_value=self.database))
        self.stack.enter_context(patch('context_layer._USE_MEMORY_STORE', False))
        self.stack.enter_context(patch('cognitive_queue.COGNITIVE_COOLDOWN_SECONDS', 0))
        self.stack.enter_context(patch('cognitive_queue.COGNITIVE_DAILY_CYCLE_LIMIT', 100))
        self.model_guards = [self.stack.enter_context(patch(target,
            side_effect=AssertionError('LLM_CALLED_IN_COGNITIVE_PATH'), create=True)) for target in (
                'ai_client.create_chat', 'structured_output.invoke_structured_llm',
                'user_memory.create_chat', 'user_memory.invoke_structured_llm',
                'relationship_engine.extract_signals', 'memory_search.embed')]
        self.now = datetime.now(timezone.utc) + timedelta(seconds=5)
        cognitive_worker._LAST_REFLECTION_SCAN_AT = None

    def tearDown(self):
        for guard in self.model_guards:
            guard.assert_not_called()

    def sql(self, sql, params=()):
        cur = self.database.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def source(self, source_id, text, *, user='u', character='c', role='user'):
        self.now += timedelta(minutes=1)
        self.sql('''INSERT INTO chat_log(user_id,chat_id,event_id,role,text,created_at)
                    VALUES (%s,%s,%s,%s,%s,%s)''', (user, character, source_id, role, text, self.now))
        self.database.commit()

    def ingest(self, source_id):
        from user_memory import extract_and_save_memory
        self.assertTrue(extract_and_save_memory('u', 'untrusted copied text', 'model prose',
            'c', source_event_id=source_id, parsed_override={'cognitive_update': {'value': 'yes'}}))

    def run_cycle(self):
        import cognitive_worker
        self.now += timedelta(seconds=1)
        result = cognitive_worker.run_worker_once(
            create_chat_fn=lambda **_: self.fail('model called'), now=self.now)
        self.assertEqual(result['status'], 'succeeded', result)
        return result

    def report(self, source_id='original', text='我讨厌咖啡。'):
        self.source(source_id, text)
        self.ingest(source_id)
        return self.run_cycle()

    def test_correction_withdraws_history_and_transitive_dependents(self):
        self.report()
        belief_id, bkey, event_id, cycle_id = self.sql('''SELECT id,belief_key,
            (evidence_refs->0->>'event_id')::bigint,created_by_cycle_id FROM cognitive_beliefs''')[0]
        self.sql('''INSERT INTO cognitive_beliefs(user_id,character_id,belief_key,statement,confidence,
            metadata) VALUES ('u','c','dependent','旧推断',0.8,%s::jsonb),
            ('u','c','transitive','再一层推断',0.8,%s::jsonb)''',
            (json.dumps({'basis_belief_keys': [bkey]}), json.dumps({'basis_belief_keys': ['dependent']})))
        self.sql('''INSERT INTO cognitive_sticky_notes(user_id,character_id,note_key,content,metadata)
            VALUES ('u','c','action','基于旧判断的行动',%s::jsonb)''',
            (json.dumps({'action_basis': {'basis_belief_keys': ['transitive']}}),))
        self.sql('''INSERT INTO cognitive_diary_entries(user_id,character_id,diary_key,content,source_event_refs)
            VALUES ('u','c','summary','旧判断摘要',%s::jsonb)''', (json.dumps([{'event_id': event_id}]),))
        self.database.commit()
        before = self.sql('SELECT belief_key,status,statement FROM cognitive_beliefs ORDER BY id')
        result = self.report('correction', '更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        beliefs = self.sql('SELECT belief_key,status,statement FROM cognitive_beliefs ORDER BY id')
        self.assertEqual([b[1] for b in beliefs], ['retracted','retracted','retracted','active'])
        self.assertIn('讨厌咖啡', beliefs[0][2])
        self.assertIn('喜欢咖啡', beliefs[-1][2])
        self.assertEqual(self.sql("SELECT status,last_error_code FROM cognitive_predictions ORDER BY id")[0], ('expired','basis_retracted'))
        self.assertEqual(self.sql("SELECT status,metadata->>'action_basis_valid' FROM cognitive_sticky_notes")[0], ('archived','false'))
        self.assertIsNotNone(self.sql('SELECT invalidated_by_event_id FROM cognitive_cycles WHERE id=%s', (cycle_id,))[0][0])
        self.assertIsNotNone(self.sql('SELECT invalidated_by_event_id FROM cognitive_diary_entries')[0][0])
        from cognitive_reader import fetch_cognitive_reader_state, list_diary_entries
        state = fetch_cognitive_reader_state('u', 'c', conn=self.database)
        self.assertEqual(len(state['beliefs']), 1)
        self.assertEqual(list_diary_entries('u', 'c', conn=self.database), [])
        question = self.sql('SELECT metadata FROM cognitive_questions')[0][0]
        self.assertEqual(question['current_judgment']['value'], '喜欢')
        self.assertEqual(len(question['revision_history']), 1)
        self.assertEqual(result['output']['belief_commit_decisions'][0]['action'], 'corrected')
        artifact = os.getenv('COGNITIVE_TEST_ARTIFACT')
        if artifact:
            Path(artifact).write_text(json.dumps({
                'database': 'Disposable PostgreSQL (PGlite), real SQL',
                'model_calls': sum(guard.call_count for guard in self.model_guards),
                'before_beliefs': before, 'after_beliefs': beliefs,
                'question_after': question,
                'prediction_after': self.sql('SELECT prediction_key,status,last_error_code,metadata FROM cognitive_predictions ORDER BY id'),
                'action_basis_after': self.sql('SELECT note_key,status,metadata FROM cognitive_sticky_notes'),
                'summary_after': self.sql('SELECT diary_key,content,invalidated_by_event_id FROM cognitive_diary_entries'),
            }, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')

    def test_replay_and_idle_time_never_raise_confidence(self):
        self.report()
        before = self.sql('SELECT belief_key,confidence,status FROM cognitive_beliefs')
        self.ingest('original')
        import cognitive_worker
        result = cognitive_worker.run_worker_once(now=self.now + timedelta(days=2))
        self.assertEqual(result['status'], 'idle')
        self.assertEqual(self.sql('SELECT belief_key,confidence,status FROM cognitive_beliefs'), before)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_cycles')[0][0], 1)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 1)
        self.report('repeat', '我讨厌咖啡。')
        self.assertEqual(self.sql('SELECT belief_key,confidence,status FROM cognitive_beliefs'), before)
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions')[0][0], 'fulfilled')
        self.assertEqual(cognitive_worker.run_worker_once(now=self.now + timedelta(seconds=1))['status'], 'idle')

    def test_contradiction_checks_prediction_and_opens_issue(self):
        self.report()
        self.report('new', '我喜欢咖啡。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions')[0][0], 'violated')
        meta = self.sql('SELECT status,metadata FROM cognitive_questions')[0]
        self.assertEqual(meta[0], 'active')
        self.assertIsNone(meta[1]['current_judgment'])
        self.assertEqual(self.sql("SELECT metadata->>'review_status' FROM cognitive_beliefs")[0][0], 'under_review')
        import cognitive_worker
        self.assertEqual(cognitive_worker.run_worker_once(now=self.now + timedelta(seconds=1))['status'], 'idle')

    def test_other_object_and_scope_do_not_settle_prediction(self):
        self.report()
        self.report('other', '我喜欢茶。')
        self.report('dated', '在2026-09-30，我喜欢咖啡。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions ORDER BY id')[0][0], 'pending')
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 3)

    def test_scope_mismatch_and_irony_stay_pending_without_changing_old_belief(self):
        self.report()
        for i, text in enumerate(('更正：「我讨厌咖啡」不对，应为「我喜欢茶」。',
                                  '我讨厌你才怪，开玩笑的',
                                  '更正事件「missing」：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')):
            self.report('unknown' + str(i), text)
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs'), [('active',)])
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_events WHERE adjudication->>'status'='pending'")[0][0], 3)

    def test_source_cannot_cross_user_or_character(self):
        self.report('other-source')
        self.source('foreign', '我讨厌咖啡。', user='other')
        self.report('wrong-source', '更正事件「foreign」：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs'), [('active',)])

    def test_memory_and_relationship_model_candidates_cannot_write_judgments(self):
        self.source('input', '我喜欢咖啡？')
        from cognitive_events import ingest_question_update, ingest_v4_signals
        ingest_question_update(user_id='u', character_id='c',
            update={'type': 'resolution', 'value': 'yes', 'question_key': 'romance'},
            canonical_events=[{'event_id': 'input', 'role': 'user', 'content': '我喜欢咖啡'}])
        ingest_v4_signals(user_id='u', character_id='c', source_event_id='input',
                         signals=[{'signal_type': 'character_reciprocal', 'confidence': 'high'}])
        from relationship_engine import process_turn
        process_turn('u', 'c', 'model copied yes', source_event_id='input')
        self.run_cycle()
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)
        self.assertEqual(self.sql('SELECT status FROM cognitive_questions'), [('active',)])

    def test_deleted_source_cannot_apply_after_queueing(self):
        self.source('deleted', '我喜欢咖啡。')
        self.ingest('deleted')
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='deleted'")
        self.database.commit()
        self.run_cycle()
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)

    def test_due_predictions_expire_once_without_old_memory_recall(self):
        self.report()
        from cognitive_scheduler import enqueue_scheduled_reflection
        result = enqueue_scheduled_reflection('u', 'c', scheduled_for=self.now + timedelta(days=31))
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions')[0][0], 'expired')
        result = enqueue_scheduled_reflection('u', 'c', scheduled_for=self.now + timedelta(days=32))
        self.assertEqual(result['status'], 'idle')

    def test_rejected_source_preserves_the_existing_judgment(self):
        self.report()
        before = self.sql("SELECT metadata->'current_judgment' FROM cognitive_questions")[0][0]
        self.report('bad', '更正事件「foreign」：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.assertEqual(self.sql("SELECT metadata->'current_judgment' FROM cognitive_questions ORDER BY id")[0][0], before)

    def test_ambiguous_sources_need_exact_source_then_retract_shared_basis(self):
        self.report()
        self.report('repeat', '我讨厌咖啡。')
        self.report('ambiguous', '更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs'), [('active',)])
        self.report('precise', '更正事件「repeat」：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs ORDER BY id'), [('retracted',), ('active',)])

    def test_multiple_events_in_one_cycle_follow_source_order(self):
        self.source('first', '我讨厌咖啡。')
        self.ingest('first')
        self.source('second', '更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.ingest('second')
        self.run_cycle()
        # Aggregation can append to the existing cycle or queue the next one;
        # either case must retain causality when all pending work is consumed.
        import cognitive_worker
        result = cognitive_worker.run_worker_once(now=self.now + timedelta(seconds=1))
        self.assertIn(result['status'], {'succeeded', 'idle'})
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs ORDER BY id'), [('retracted',), ('active',)])
        self.assertEqual(self.sql('SELECT invalidated_by_event_id FROM cognitive_cycles ORDER BY id DESC LIMIT 1'), [(None,)])

    def test_transaction_rolls_back_partial_correction_then_retries(self):
        self.report()
        self.source('correction', '更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.ingest('correction')
        import cognitive_worker
        with patch('cognitive_predictions.create_prediction', side_effect=RuntimeError('injected_db_failure')):
            result = cognitive_worker.run_worker_once(now=self.now)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs'), [('active',)])
        self.assertEqual(self.sql("SELECT adjudication FROM cognitive_events WHERE source_event_id='correction'"), [({},)])
        self.run_cycle()
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs ORDER BY id'), [('retracted',), ('active',)])

    def test_model_output_cannot_bypass_the_commit_validator(self):
        self.source('first', '我喜欢咖啡。')
        self.ingest('first')
        from cognitive_queue import claim_next_cycle, build_reasoning_context, commit_cycle_success
        claim = claim_next_cycle(now=self.now)
        context = build_reasoning_context(claim['cycle_id'])
        output = revision.deterministic_cycle_output(context)
        output['question_updates'] = [{'question_key': 'forged', 'question_text': '伪造关系结论',
            'status': 'resolved', 'evidence_refs': [context['events'][0]['event_id']]}]
        with self.assertRaisesRegex(ValueError, 'external_judgment_candidates'):
            commit_cycle_success(claim['cycle_id'], reasoning_context=context,
                                 structured_output=output, now=self.now)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)
        self.assertEqual(self.sql('SELECT status FROM cognitive_event_triggers')[0][0], 'claimed')

    def test_context_reads_only_the_current_event_worklist(self):
        self.report()
        self.source('new', '我喜欢茶。')
        self.ingest('new')
        from cognitive_queue import claim_next_cycle, build_reasoning_context, aggregate_pending_triggers
        aggregate_pending_triggers('u', 'c', now=self.now)
        cycle = claim_next_cycle(now=self.now)
        with patch.object(self.database, 'query', wraps=self.database.query) as queries:
            context = build_reasoning_context(cycle['cycle_id'], conn=self.database)
        sql = '\n'.join(call.args[0] for call in queries.call_args_list)
        self.assertNotIn('FROM cognitive_beliefs', sql)
        self.assertNotIn('FROM cognitive_hypotheses', sql)
        self.assertNotIn('FROM cognitive_diary_entries', sql)
        self.assertEqual([e['source_event_id'] for e in context['events']], ['new'])

    def test_canonical_lookup_failure_is_not_fallback_to_copied_text(self):
        from user_memory import extract_and_save_memory
        from raw_events import SourceValidityError
        with patch('raw_events.get_active_events_by_ids', side_effect=SourceValidityError('offline_failure')):
            with self.assertRaises(SourceValidityError):
                extract_and_save_memory('u', '我喜欢咖啡。', '', 'c', source_event_id='missing')
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 0)

    def test_missing_or_assistant_source_cannot_create_a_user_report(self):
        from user_memory import extract_and_save_memory
        self.assertFalse(extract_and_save_memory('u', '我喜欢咖啡。', '', 'c'))
        self.source('assistant', '我喜欢咖啡。', role='gojo')
        self.assertFalse(extract_and_save_memory('u', '我喜欢咖啡。', '', 'c', source_event_id='assistant'))
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 0)

    def test_other_character_source_is_not_accepted(self):
        self.source('other', '我喜欢咖啡。', character='someone_else')
        from user_memory import extract_and_save_memory
        self.assertFalse(extract_and_save_memory('u', '我喜欢咖啡。', '', 'c', source_event_id='other'))
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 0)

    def test_same_date_correction_does_not_change_other_dates(self):
        self.report('dated1', '在2026-09-01，我讨厌咖啡。')
        self.report('dated2', '在2026-09-02，我讨厌咖啡。')
        self.report('correction', '更正：「在2026-09-01，我讨厌咖啡」不对，应为「在2026-09-01，我喜欢咖啡」。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs ORDER BY id'), [('retracted',), ('active',), ('active',)])

    def test_correction_of_correction_preserves_both_history_rows(self):
        self.report()
        self.report('second', '更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.report('third', '更正：「我喜欢咖啡」不对，应为「我不喜欢咖啡」。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs ORDER BY id'), [('retracted',), ('retracted',), ('active',)])
        self.assertEqual(len(self.sql('SELECT metadata FROM cognitive_questions')[0][0]['revision_history']), 2)

    def test_rolling_summary_and_action_pin_are_superseded(self):
        from context_layer import init_context_layer_tables, save_rolling_summary
        init_context_layer_tables()
        self.report()
        self.sql("""INSERT INTO rolling_summaries(summary_id,user_id,character_id,text,source_event_ids)
                    VALUES ('s','u','c','旧摘要','["original"]')""")
        self.sql("""INSERT INTO pinned_context(pin_id,user_id,character_id,text,source_event_ids)
                    VALUES ('p','u','c','旧决策依据','["original"]')""")
        self.database.commit()
        self.report('correction', '更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.assertEqual(self.sql('SELECT status FROM rolling_summaries'), [('superseded',)])
        self.assertEqual(self.sql('SELECT status FROM pinned_context'), [('superseded',)])
        from cognitive_revision import sources_current_for_derivation
        self.assertFalse(sources_current_for_derivation(self.database.cursor(), 'u', 'c', ['original']))
        self.assertTrue(sources_current_for_derivation(self.database.cursor(), 'u', 'c', ['correction']))
        late = save_rolling_summary('u', 'c', '晚到的旧摘要', ['original'])
        self.assertEqual(late['status'], 'superseded')
        self.assertEqual(self.sql("SELECT count(*) FROM rolling_summaries WHERE status='active'")[0][0], 0)

    def test_sink_cannot_opt_back_into_model_authority(self):
        from cognitive_output import persist_slow_loop_output
        with self.assertRaisesRegex(ValueError, 'model_judgment_persistence_disabled'):
            persist_slow_loop_output(self.database.cursor(), cycle_id=1, user_id='u',
                character_id='c', output={}, now=self.now, deterministic=False)

    def test_old_model_quotas_do_not_drop_or_delay_corrections(self):
        with patch('cognitive_queue.COGNITIVE_DAILY_CYCLE_LIMIT', 1), \
             patch('cognitive_queue.COGNITIVE_COOLDOWN_SECONDS', 86400):
            self.report()
            self.report('correction', '更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs ORDER BY id'), [('retracted',), ('active',)])

    def test_old_model_signals_never_settle_prediction(self):
        self.report()
        from cognitive_events import record_source_event
        from cognitive_predictions import settle_pending_predictions
        event = record_source_event(self.database, user_id='u', character_id='c',
            source_event_type='relationship_v4_signal', source_event_id='legacy',
            source='relationship_engine_v4', occurred_at=self.now,
            payload={'signals': [{'signal_type': 'character_reciprocal'}]})
        self.assertEqual(settle_pending_predictions(self.database, user_id='u', character_id='c',
            event_id=event, occurred_at=self.now + timedelta(seconds=1)), [])
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'), [('pending',)])


if __name__ == '__main__':
    unittest.main()
