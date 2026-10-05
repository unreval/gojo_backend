"""Phase D acceptance: canonical sources through actual PostgreSQL and readers."""
from datetime import timedelta
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tests.canonical_memory_fixture import CanonicalMemoryFixture
import cognitive_events
import cognitive_reader
import cognitive_revision
import user_memory
from memory_authority import MEMORY_AUTHORITY_DDL, filter_recall_authority


class ExplicitAnswerFixture(CanonicalMemoryFixture):
    def setUp(self):
        super().setUp()
        for target in ('user_memory.get_conn', 'smart_recall.get_conn', 'memory_search.get_conn',
                       'memory_lifecycle.get_conn'):
            self.stack.enter_context(patch(target, return_value=self.database))
        self.model_guards.append(self.stack.enter_context(patch('user_memory._bg_embed',
            side_effect=AssertionError('embedding called'))))
        for target in ('user_memory.save_bond_memory', 'user_memory.merge_bond_memories',
                       'user_memory.resolve_bond_memories', 'requests.sessions.Session.request'):
            self.model_guards.append(self.stack.enter_context(patch(target,
                side_effect=AssertionError('legacy writer or external HTTP called'))))
        from memory_lifecycle import init_memory_lifecycle_tables
        init_memory_lifecycle_tables(conn=self.database)
        self.sql('''CREATE TABLE char_diary(id SERIAL PRIMARY KEY,user_id TEXT,
            character_id TEXT,content TEXT,emotion TEXT,created_at TIMESTAMPTZ)''')
        self.database.commit()


    def turn(self, sid, text, role='user', *, reply=None, process=True):
        self.source(sid, text, role=role)
        if reply:
            self.sql('UPDATE chat_log SET reply_to_event_id=%s WHERE event_id=%s', (reply, sid))
            self.database.commit()
        if process:
            self.deliver(sid)


    def deliver(self, sid):
        result = cognitive_events.ingest_canonical_turn(user_id='u', character_id='c',
            source_event_id=sid, allow_assistant=True)
        self.assertIn(result['status'], ('inserted', 'duplicate'))
        while self.sql("SELECT count(*) FROM cognitive_event_triggers WHERE status IN ('pending','claimed')")[0][0]:
            self.run_cycle()
        return result


    def exchange(self, answer='yes', *, reverse=False, reply=None, suffix=''):
        self.turn('q' + suffix, '我可以叫你宝宝吗？' if reverse else '我还是叫你宝宝。你接不接受这个称呼？',
                  'assistant' if reverse else 'user')
        self.turn('a' + suffix, answer, 'user' if reverse else 'assistant', reply=reply)


    def answers(self):
        return self.sql("""SELECT source_event_id,op.key,op.value FROM cognitive_events
            CROSS JOIN LATERAL jsonb_each(COALESCE(adjudication->'operations','{}')) op
            WHERE op.value->>'action'='answered' AND op.value->>'status'='applied'
            ORDER BY id,op.key""")


    def reader(self):
        return cognitive_reader.fetch_cognitive_reader_state('u', 'c', conn=self.database)


    def recall(self):
        import smart_recall
        result = smart_recall.two_level_recall('u', 'c', '宝宝称呼回答')
        self.assertIsNotNone(result)
        return result


    def assert_answer(self, actor='character', value='yes'):
        rows = self.answers()
        self.assertEqual(len(rows), 1, rows)
        decision = rows[0][2]
        self.assertEqual((decision['actor'], decision['value']), (actor, value))
        self.assertEqual({d['evidence_role'] for d in decision['dependencies']}, {'question', 'answer'})
        self.assertEqual(len(self.reader()['questions']), 1)
        self.assertEqual(self.reader()['questions'][0]['status'], 'resolved')
        self.assertTrue(any(decision['memory_content'] == row[1]
                            for row in user_memory.get_bond_memories('u', 'c')))
        recalled = self.recall()
        self.assertIn(decision['memory_content'], [b['content'] for b in recalled['loose_bonds']])
        items = cognitive_reader.iter_active_cognitive_items('u', 'c', conn=self.database, query='宝宝')
        self.assertTrue(any(decision['memory_content'] in item['text'] for item in items), items)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)
        return decision


    def snapshot(self):
        return {'raw': self.sql('SELECT event_id,role,text,status,reply_to_event_id FROM chat_log ORDER BY id'),
                'events': self.sql('SELECT id,source_event_id,adjudication FROM cognitive_events ORDER BY id'),
                'questions': self.sql('SELECT question_key,status,metadata FROM cognitive_questions ORDER BY id'),
                'projections': self.sql('SELECT id,authority_event_id,authority_operation_id,content,recall_status FROM bond_memory ORDER BY id'),
                'provenance': self.sql('SELECT * FROM memory_source_events ORDER BY memory_id,source_event_id'),
                'reader': self.reader(), 'recall': self.recall(),
                'guard_calls': [guard.call_count for guard in self.model_guards]}


    def write_evidence(self, name, evidence):
        if os.getenv('PHASE_D_ARTIFACT_DIR'):
            Path(os.environ['PHASE_D_ARTIFACT_DIR'], name + '.json').write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding='utf-8')



class ExplicitAnswerSQLTests(ExplicitAnswerFixture, unittest.TestCase):
    def test_original_character_yes_has_full_chain_and_no_romance(self):
        self.exchange()
        decision = self.assert_answer()
        self.assertIn('我明确答复：接受她称呼我', decision['memory_content'])
        before = self.snapshot()
        cached = self.recall()
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='q'")
        self.database.commit()
        self.assertEqual(self.reader()['questions'], [])
        self.assertNotIn(decision['memory_content'], str(filter_recall_authority(cached, 'u', 'c')))
        self.assertEqual(self.sql("SELECT text FROM chat_log WHERE event_id='a'"), [('yes',)])
        self.write_evidence('original-and-deleted-question', {'before': before, 'after': self.snapshot(),
                            'cached_recall_after': filter_recall_authority(cached, 'u', 'c')})


    def test_reverse_user_yes_is_scoped(self):
        self.exchange(reverse=True)
        self.assertIn('她明确答复：接受我称呼她', self.assert_answer('user')['memory_content'])


    def test_both_directions_are_independent(self):
        self.exchange()
        first = self.answers()[0][2]
        self.exchange(reverse=True, suffix='2')
        self.assertEqual({r[2]['actor'] for r in self.answers()}, {'user', 'character'})
        self.assertEqual(self.answers()[0][2], first)
        self.assertEqual(len(self.reader()['questions']), 2)


    def test_no_is_saved_as_refusal(self):
        self.exchange('no')
        self.assertIn('不接受', self.assert_answer(value='no')['memory_content'])


    def test_chinese_refusal_is_saved(self):
        self.exchange('不接受', reverse=True)
        self.assert_answer('user', 'no')


    def test_no_question_cannot_bind_yes(self):
        self.turn('a', 'yes')
        self.assertEqual(self.answers(), [])
        self.assertEqual(user_memory.get_bond_memories('u', 'c'), [])


    def test_two_competing_questions_cannot_bind_bare_yes(self):
        self.turn('q1', '你接受我叫你宝宝吗？')
        self.turn('q2', '你接受我叫你「小猫」吗？')
        self.turn('a', 'yes', 'assistant')
        self.assertEqual(self.answers(), [])


    def test_unknown_competing_question_cannot_be_ignored(self):
        self.turn('q', '你接受我叫你宝宝吗？你喜欢我吗？')
        self.turn('a', 'yes', 'assistant', reply='q')
        self.assertEqual(self.answers(), [])


    def test_topic_change_stops_implicit_binding(self):
        self.turn('q', '你接受我叫你宝宝吗？')
        self.turn('weather', '今天天气如何？')
        self.turn('a', 'yes', 'assistant')
        self.assertEqual(self.answers(), [])


    def test_reference_checks_owner_character_time_and_respondent(self):
        for sid, owner, character, role in [('foreign-user', 'other', 'c', 'user'),
                ('foreign-chat', 'u', 'other', 'user'), ('wrong-speaker', 'u', 'c', 'assistant')]:
            self.source(sid, '你接受我叫你宝宝吗？', user=owner, character=character, role=role)
            self.turn('a-' + sid, 'yes', 'assistant', reply=sid)
        self.turn('early', 'yes', 'assistant', reply='future', process=False)
        self.turn('future', '你接受我叫你宝宝吗？', process=False)
        self.deliver('early')
        self.assertEqual(self.answers(), [])


    def test_untrusted_copied_answer_is_not_source(self):
        self.turn('q', '你接受我叫你宝宝吗？')
        self.assertTrue(user_memory.extract_and_save_memory('u', 'copied', 'yes', 'c',
            source_event_id='q', source_event_ids=['q', 'missing-reply'],
            parsed_override={'bond_delta': {'value': 'yes', 'novel': True}}))
        self.assertEqual(self.answers(), [])


    def test_answer_processed_before_question_job(self):
        self.turn('q', '你接受我叫你宝宝吗？', process=False)
        self.turn('a', 'yes', 'assistant', reply='q')
        before = self.assert_answer()
        self.deliver('q')
        self.assertEqual(self.assert_answer(), before)


    def test_tomorrow_remains_pending_and_retry_preserves_source_time(self):
        self.exchange('明天给你回答。')
        before = self.reader()['questions'][0]
        self.assertEqual(before['status'], 'active')
        self.assertEqual(before['metadata']['pending_answer']['status'], 'pending')
        self.assertEqual(self.answers(), [])
        self.now += timedelta(days=2)
        self.deliver('a')
        self.assertEqual(self.reader()['questions'][0]['metadata'], before['metadata'])
        self.assertTrue(any('仍待回答' in str(b) for b in self.recall()['loose_bonds']))


    def test_strict_whole_answer_grammar(self):
        for index, answer in enumerate(('他说 yes', 'yes?', '不是不接受',
                                      'yes，才怪', 'yes。开玩笑的。', '「yes」')):
            self.exchange(answer, suffix=str(index))
        self.assertEqual(self.answers(), [])

    def test_nickname_answer_never_settles_unrelated_romance_question(self):
        self.turn('romance','你喜欢我吗？')
        before = self.sql('SELECT question_key,status,metadata FROM cognitive_questions')
        self.exchange(reply='q')
        self.assertEqual(len(self.answers()),1)
        self.assertEqual(self.sql('SELECT question_key,status,metadata FROM cognitive_questions WHERE question_key=%s',
                                 (before[0][0],)),before)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0],0)

    def test_existing_literal_tokens_require_scoped_questions(self):
        cases = [(' YES! ','yes'),('我接受。','yes'),('不同意','no'),
                 ('I agree.','yes'),("I don't accept.",'no'),('はい','yes'),('いいえ','no')]
        for index,(answer,value) in enumerate(cases):
            self.exchange(answer,reply=f'q{index}',suffix=str(index))
            row = next(r[2] for r in self.answers() if r[0] == f'a{index}')
            self.assertEqual(row['value'],value)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0],0)


    def test_same_event_independent_answers_and_retry(self):
        self.turn('q', '你接受我叫你宝宝吗？你接受我叫你「小猫」吗？')
        self.turn('a', '接受你叫我宝宝。不接受你叫我「小猫」。', 'assistant', reply='q')
        self.assertEqual([(r[2]['object'], r[2]['value']) for r in self.answers()], [('宝宝', 'yes'), ('小猫', 'no')])
        before = self.snapshot()
        self.deliver('a')
        after = self.snapshot()
        for key in ('raw', 'events', 'questions', 'projections', 'provenance', 'reader'):
            self.assertEqual(after[key], before[key], key)
        self.assertEqual(self.sql('SELECT count(*) FROM chat_log')[0][0], 2)
        self.assertEqual(self.sql('SELECT count(*) FROM bond_memory')[0][0], 3)


    def test_answer_and_independent_literal_promise_survive(self):
        self.turn('q', '我可以叫你宝宝吗？', 'assistant')
        self.turn('a', 'yes。我承诺在2026-12-01前完成「周六散步」。')
        self.assertEqual(len(self.answers()), 1)
        self.assertEqual(len(self.reader()['questions']), 2)
        self.assertTrue(any('周六散步' in r[1] for r in user_memory.get_bond_memories('u', 'c')))
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 1)
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'), [('pending',)])


    def test_quoted_delimiters_do_not_split_independent_literals(self):
        self.turn('u', '不要「叫我宝宝；也不要打电话」。我的职业是「教师」。')
        self.assertEqual(len(user_memory.get_bond_memories('u', 'c')), 1)
        self.assertEqual(len(user_memory.get_long_memory('u', 'c')), 1)


    def test_source_read_failure_rolls_back_and_retries(self):
        self.turn('q', '你接受我叫你宝宝吗？', process=False)
        self.turn('a', 'yes', 'assistant', reply='q', process=False)
        cognitive_events.ingest_canonical_turn(user_id='u', character_id='c', source_event_id='a', allow_assistant=True)
        import cognitive_worker
        with patch.object(cognitive_revision, '_exchange_source', side_effect=RuntimeError('source unavailable')):
            self.assertEqual(cognitive_worker.run_worker_once(now=self.now)['status'], 'failed')
        self.assertEqual(self.sql('SELECT adjudication FROM cognitive_events'), [({},)])
        self.assertEqual(self.sql('SELECT count(*) FROM bond_memory')[0][0], 0)
        self.run_cycle()
        self.assert_answer()


    def test_deleted_answer_and_cached_cognitive_state_cannot_return(self):
        self.exchange()
        cached = self.reader()
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='a'")
        self.database.commit()
        self.assertEqual(self.reader()['questions'], [])
        self.assertEqual(cognitive_reader.iter_active_cognitive_items('u', 'c', conn=self.database,
                         query='宝宝', _state=cached), [])
        self.assertEqual(user_memory.get_bond_memories('u', 'c'), [])


    def test_user_object_correction_withdraws_only_the_bound_operation(self):
        self.turn('q', '我可以叫你宝宝吗？', 'assistant')
        self.turn('a', 'yes。不要「深夜来电」。')
        before = self.snapshot()
        cached = self.recall()
        self.turn('fix', '更正事件「a」：我刚才回答的不是「宝宝」这个称呼。')
        self.assertEqual(self.answers(), [])
        self.assertTrue(any('深夜来电' in r[1] for r in user_memory.get_bond_memories('u', 'c')))
        self.assertFalse(any('明确答复' in r[1] for r in user_memory.get_bond_memories('u', 'c')))
        self.assertNotIn('明确答复', str(filter_recall_authority(cached, 'u', 'c')))
        self.assertFalse(cognitive_revision.sources_current_for_derivation(self.database.cursor(), 'u', 'c', ['a']))
        self.write_evidence('scoped-correction', {'before': before, 'after': self.snapshot()})


    def test_user_cannot_rewrite_character_answer(self):
        self.exchange()
        before = self.answers()
        self.turn('fix', '更正事件「a」：我刚才回答的不是「宝宝」这个称呼。')
        self.assertEqual(self.answers(), before)

    def test_correction_processed_before_answer_job_cannot_be_revived(self):
        self.turn('q','我可以叫你宝宝吗？','assistant',process=False)
        self.turn('a','yes',process=False)
        self.turn('fix','更正事件「a」：我刚才回答的不是「宝宝」这个称呼。')
        self.assertEqual(self.answers(),[])
        before = self.sql('SELECT source_event_id,adjudication FROM cognitive_events ORDER BY id')
        self.deliver('a')
        self.assertEqual(self.sql('SELECT source_event_id,adjudication FROM cognitive_events ORDER BY id'),before)
        self.assertFalse(any('明确答复' in r[1] for r in user_memory.get_bond_memories('u','c')))

    def test_correcting_older_answer_does_not_withdraw_a_later_answer(self):
        self.exchange(reverse=True,reply='q')
        self.turn('a2','我明确接受你叫我宝宝。',reply='q')
        before = self.reader()['questions']
        self.turn('fix','更正事件「a」：我刚才回答的不是「宝宝」这个称呼。')
        self.assertEqual(self.reader()['questions'],before)
        self.assertEqual(self.answers()[0][0],'a2')

    def test_unrelated_prediction_and_generic_dependent_note_on_same_event(self):
        self.turn('q','我可以叫你宝宝吗？','assistant')
        self.turn('a','yes。我承诺在2026-12-01前完成「周六散步」。')
        eid = self.sql("SELECT id FROM cognitive_events WHERE source_event_id='a'")[0][0]
        self.sql("""INSERT INTO cognitive_sticky_notes(user_id,character_id,note_key,content,source_event_refs)
            VALUES ('u','c','old-note','基于该答复的旧提醒',%s::jsonb)""", (json.dumps([{'event_id':eid}]),))
        self.database.commit()
        self.turn('fix','更正事件「a」：我刚才回答的不是「宝宝」这个称呼。')
        self.assertEqual(self.sql('SELECT status FROM cognitive_sticky_notes'),[('archived',)])
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'),[('pending',)])
        self.assertTrue(any('周六散步' in r[1] for r in user_memory.get_bond_memories('u','c')))


    def test_schema_upgrade_repeat_initialization_and_old_row(self):
        self.turn('old', '我喜欢咖啡。')
        old = user_memory.get_long_memory('u', 'c')
        self.sql("UPDATE cognitive_events SET adjudication=adjudication-'operations'")
        for table in ('long_memory', 'bond_memory'):
            self.sql(f'DROP INDEX idx_{table}_authority_operation')
            self.sql(f'ALTER TABLE {table} DROP COLUMN authority_operation_id')
            self.sql(f'CREATE UNIQUE INDEX idx_{table}_authority_event ON {table}(authority_event_id) WHERE authority_event_id IS NOT NULL')
        for _ in range(2):
            for ddl in MEMORY_AUTHORITY_DDL:
                self.sql(ddl)
        self.database.commit()
        self.assertEqual(user_memory.get_long_memory('u', 'c'), old)
        self.exchange()
        self.assertEqual(len(self.answers()), 1)

    def test_same_text_in_two_real_answers_does_not_increase_feeling_confidence(self):
        self.sql("CREATE TABLE rel_state(user_id TEXT,character_id TEXT,warmth REAL,passion REAL)")
        self.sql("INSERT INTO rel_state VALUES ('u','c',2.5,1.25)")
        self.database.commit()
        self.exchange(reply='q')
        self.turn('a2','yes','assistant',reply='q')
        self.assertEqual(len(self.answers()),1)
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_events WHERE source_event_id IN ('a','a2')")[0][0],2)
        self.assertEqual(self.sql('SELECT * FROM rel_state'),[('u','c',2.5,1.25)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0],0)
        self.assertEqual(len(self.reader()['questions']),1)
        self.assertEqual(self.sql("SELECT count(*) FROM bond_memory WHERE recall_status='superseded'")[0][0],1)

    def test_changed_question_before_answer_cannot_bind(self):
        self.turn('q','你接受我叫你宝宝吗？')
        self.sql("UPDATE chat_log SET text='你接受我叫你「小猫」吗？' WHERE event_id='q'")
        self.database.commit()
        self.turn('a','yes','assistant',reply='q')
        self.assertEqual(self.answers(),[])

    def test_changed_source_or_revoked_adjudication_exits_current_readers(self):
        self.exchange()
        cached = self.recall()
        self.sql("UPDATE cognitive_events SET adjudication=adjudication || '{\"status\":\"superseded\"}'::jsonb WHERE source_event_id='a'")
        self.database.commit()
        self.assertEqual(self.reader()['questions'],[])
        self.assertEqual(filter_recall_authority(cached,'u','c')['loose_bonds'],[])

    def test_cached_vectors_cannot_bypass_deleted_question_dependency(self):
        import memory_search
        import smart_recall
        self.exchange()
        for table in ('long_memory','bond_memory'):
            self.sql(f'ALTER TABLE {table} ADD COLUMN embedding_json TEXT')
        self.sql("UPDATE bond_memory SET embedding_json='[1,0]'")
        self.database.commit()
        for attr,value in (('_VECTOR_READY',True),('_CACHE',{'long_memory':{},'bond_memory':{}}),
                           ('_CACHE_LOADED',{'long_memory':False,'bond_memory':False}),
                           ('_MATRIX',{'long_memory':None,'bond_memory':None})):
            self.stack.enter_context(patch.object(memory_search,attr,value))
        # Precomputed test vectors exercise actual cache loading and ranking;
        # every embedding generation entry point remains fail-on-call.
        before=smart_recall.two_level_recall('u','c','unrelated lexical query',query_embedding=[1,0])
        answer_id=self.sql("SELECT id FROM bond_memory WHERE content LIKE '%明确答复%'")[0][0]
        self.assertIn(answer_id,[r['id'] for r in before['loose_bonds']])
        self.assertIn(answer_id,memory_search._CACHE['bond_memory'])
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='q'")
        self.database.commit()
        after=smart_recall.two_level_recall('u','c','unrelated lexical query',query_embedding=[1,0])
        self.assertIn(answer_id,memory_search._CACHE['bond_memory'])
        self.assertNotIn(answer_id,[r['id'] for r in after['loose_bonds']])
        self.write_evidence('cached-vector-reader',{'before':before,'after':after,
            'retained_cached_vector_id':answer_id,'guard_calls':[g.call_count for g in self.model_guards]})

    def test_worker_replay_is_operation_idempotent(self):
        self.exchange()
        before = self.sql('SELECT * FROM bond_memory ORDER BY id')
        eid,cycle = self.sql("SELECT id,(adjudication->>'processed_by_cycle_id')::int FROM cognitive_events WHERE source_event_id='a'")[0]
        result = cognitive_revision.apply_rule_evidence(self.database.cursor(),cycle_id=cycle,
            user_id='u',character_id='c',output={'evidence_refs':[{'event_id':eid}]},now=self.now)
        self.assertEqual(result[0]['action'],'already_processed')
        self.assertEqual(self.sql('SELECT * FROM bond_memory ORDER BY id'),before)

    def test_implicit_answer_after_defer_uses_context_provenance(self):
        self.exchange('明天给你回答。')
        self.turn('followup','答案呢？')
        self.turn('a2','yes','assistant')
        self.assertEqual(len(self.answers()),1)
        answer = self.answers()[0][2]
        self.assertEqual({d['source_id'] for d in answer['dependencies']},{'q','a','followup','a2'})
        self.sql("UPDATE chat_log SET text='今天天气如何？' WHERE event_id='followup'")
        self.database.commit()
        self.assertFalse(any('明确答复' in r[1] for r in user_memory.get_bond_memories('u','c')))

    def test_binding_exits_summary_episode_cached_pack_and_late_writes(self):
        import context_layer
        import episodic_index
        import prompt
        from context_budget import ContextItem
        for target in ('episodic_index.get_conn',):
            self.stack.enter_context(patch(target,return_value=self.database))
        self.stack.enter_context(patch('episodic_index._USE_MEMORY_STORE',False))
        context_layer.init_context_layer_tables()
        episodic_index.init_episodic_index_tables()
        self.sql('CREATE TABLE chat_log_tombstone(user_id TEXT,chat_id TEXT,client_msg_id TEXT)')
        self.database.commit()
        self.exchange()
        conclusion = self.answers()[0][2]['memory_content']
        context_layer.save_rolling_summary('u','c',conclusion,['a'],summary_id='summary',is_placeholder=False)
        episodic_index.save_episode('u','c',title='宝宝称呼',what_happened=conclusion,outcome=conclusion,
            source_event_ids=['a'],episode_id='episode')
        summary_rows = context_layer.list_rolling_summaries('u','c')
        self.assertEqual(len(context_layer._verified_derived(summary_rows,set(),user_id='u',character_id='c')),1)
        self.assertEqual(len(episodic_index.recall_episodes('u','c','宝宝称呼')),1)
        pack = context_layer.ChatContextPack(support_ready=True,recall_ready=True,recall_result=self.recall(),
            cognitive_prompt_text=conclusion,summary_prompt_text=conclusion,episode_prompt_text=conclusion,
            accounts_text='无账户',items=[ContextItem(item_id='sum',item_type='rolling_summary',text=conclusion,source_event_ids=('a',)),
                ContextItem(item_id='ep',item_type='episodic_memory',text=conclusion,source_event_ids=('a',))])
        def rendered():
            with patch.object(prompt,'get_character',return_value={'core_prompt':'角色'}), \
                    patch.object(prompt,'get_first_interaction_days',return_value=1), \
                    patch.object(prompt,'load_canon_lock',return_value=''):
                return '\n'.join(prompt._build_prompt_parts('u','c',user_message='宝宝',context_pack=pack))
        self.assertIn(conclusion,rendered())
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='q'")
        self.database.commit()
        self.assertEqual(context_layer._verified_derived(summary_rows,set(),user_id='u',character_id='c'),[])
        self.assertEqual(episodic_index.recall_episodes('u','c','宝宝称呼'),[])
        self.assertNotIn(conclusion,rendered())
        late = context_layer.save_rolling_summary('u','c',conclusion,['a'],summary_id='late')
        self.assertEqual(late['status'],'superseded')
        self.write_evidence('derived-and-cached-readers',{'question_deleted':True,
            'summary_after':context_layer._verified_derived(summary_rows,set(),user_id='u',character_id='c'),
            'episode_after':episodic_index.recall_episodes('u','c','宝宝称呼'),
            'cached_prompt_after':rendered(),'late_summary':late})

    def test_merge_outcomes_and_duplicate_labels_cannot_lose_independent_delta(self):
        self.turn('walk','我承诺在2026-12-01前完成「周六散步」。')
        obligation = self.sql("SELECT statement,status,confidence FROM cognitive_beliefs")
        evidence = []
        for index, outcome in enumerate(('rejected','accepted','unrelated')):
            summary = '旧称呼问题摘要' if outcome == 'rejected' else (
                '合并后的称呼问题摘要' if outcome == 'accepted' else '其他往事摘要')
            self.sql("INSERT INTO bond_memory(user_id,character_id,content) VALUES ('u','c',%s)",(summary,))
            self.database.commit()
            self.turn(f'q{index}','你接受我叫你宝宝吗？',process=False)
            self.turn(f'a{index}','yes','assistant',reply=f'q{index}',process=False)
            self.assertTrue(user_memory.extract_and_save_memory('u','ignored','ignored','c',
                source_event_id=f'q{index}',source_event_ids=[f'q{index}',f'a{index}'],
                parsed_override={'bond_delta':{'novel':False,'duplicate':True},'bond_merge':{'outcome':outcome}}))
            self.deliver(f'a{index}')
            answer = next(row[2] for row in self.answers() if row[0] == f'a{index}')
            self.assertIn(answer['memory_content'],[r[1] for r in user_memory.get_bond_memories('u','c')])
            self.assertEqual(self.sql('SELECT statement,status,confidence FROM cognitive_beliefs'),obligation)
            self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'),[('pending',)])
            evidence.append({'summary_outcome_fixture':outcome,'summary_row':self.sql(
                'SELECT content,authority,recall_status FROM bond_memory WHERE content=%s',(summary,)),
                'answer':answer,'reader':self.reader()})
        self.write_evidence('independent-deltas',{'scenarios':evidence,
            'obligation_before_after':obligation,'guard_calls':[g.call_count for g in self.model_guards]})




if __name__ == '__main__':
    unittest.main()
