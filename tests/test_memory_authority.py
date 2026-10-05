"""Real SQL: canonical group evidence, factual readers and unsupported language."""
import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import types
import unittest
from unittest.mock import patch

from tests import test_cognitive_deterministic as acceptance
import cognitive_revision
import user_memory
from memory_authority import AUTHORITY, filter_recall_authority


class MemoryAuthorityTests(unittest.TestCase):
    # Reuse the offline fixture, not its tests. Every case retains fail-on-call
    # guards for the actual cognitive LLM/embedding entry points.
    setUpClass = classmethod(acceptance.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(acceptance.OfflineDatabaseTests.tearDownClass.__func__)
    tearDown = acceptance.OfflineDatabaseTests.tearDown
    sql = acceptance.OfflineDatabaseTests.sql
    source = acceptance.OfflineDatabaseTests.source
    ingest = acceptance.OfflineDatabaseTests.ingest
    run_cycle = acceptance.OfflineDatabaseTests.run_cycle
    report = acceptance.OfflineDatabaseTests.report

    def setUp(self):
        acceptance.OfflineDatabaseTests.setUp(self)
        for target in ('user_memory.get_conn', 'smart_recall.get_conn', 'memory_search.get_conn', 'memory_jobs.get_conn'):
            self.stack.enter_context(patch(target, return_value=self.database))
        self.stack.enter_context(patch('user_memory._bg_embed'))
        self.sql('CREATE TABLE chat_log_tombstone (user_id TEXT,chat_id TEXT,client_msg_id TEXT)')
        self.sql('CREATE TABLE groups (id INTEGER,owner_user_id TEXT)')
        self.sql('CREATE TABLE group_members (group_id INTEGER,member_type TEXT,member_id TEXT)')
        self.sql("INSERT INTO groups VALUES (1,'u'),(2,'other')")
        self.sql("INSERT INTO group_members VALUES (1,'character','c'),(1,'character','d'),(2,'character','c')")
        self.sql("""CREATE TABLE memory_jobs (id SERIAL PRIMARY KEY,kind TEXT,user_id TEXT,
            character_id TEXT,user_text TEXT,assistant_text TEXT,extra_json TEXT,status TEXT,
            source_event_id TEXT,assistant_event_id TEXT,attempts INTEGER DEFAULT 0,
            last_error TEXT,updated_at TIMESTAMP)""")
        self.database.commit()

    def group_source(self, mid, content, *, role='user', sender='u', target=None, gid=1):
        from raw_events import append_group_raw_event
        source_id = append_group_raw_event(self.database.cursor(), group_id=gid,
            message_id=mid, sender_type=role, sender_id=sender, content=content,
            target_character_id=target)
        self.database.commit()
        return source_id

    def process_group(self, mid, *, owner='u', gid=1):
        self.assertTrue(user_memory.extract_and_save_group_memory(owner, '伪造的复制文本',
            '模型声称她住在火星', [{'id': 'forged', 'name': '伪造角色'}],
            source_event_id=f'group:{gid}:message:{mid}', source_chat_id=f'group:{gid}'))
        return self.run_cycle()

    def prompt_memory(self, result):
        import prompt
        pack = types.SimpleNamespace(recall_ready=True, support_ready=True,
            recall_result=result, memory_text='缓存伪造：她住在火星', accounts_text='无账户')
        with patch.object(prompt, 'get_character', return_value={'core_prompt': '角色'}), \
             patch.object(prompt, 'get_first_interaction_days', return_value=1), \
             patch.object(prompt, 'load_canon_lock', return_value=''):
            # Query-selected memory now belongs to the uncached dynamic block.
            return prompt._build_prompt_parts('u', 'c', user_message='咖啡', context_pack=pack)[-1]

    def current_result(self):
        return {'facts': [dict(id=mid,content=text,timestamp=ts,category=cat,authority=AUTHORITY)
            for mid,text,ts,cat in self.sql('SELECT id,content,timestamp,category FROM long_memory')],
            'loose_bonds': [dict(id=mid,content=text,timestamp=ts,authority=AUTHORITY)
            for mid,text,ts in self.sql("SELECT id,content,timestamp FROM bond_memory WHERE kind='between'")],
            'tolds': []}

    def test_group_fake_extractor_output_cannot_mint_fact_bond_or_told(self):
        self.group_source(1, '我喜欢咖啡。')
        invented = {'user_fact': {'content': '她住在火星', 'category': '身份'},
                    'told': {'target': 'c', 'content': '她说过结婚了'},
                    'char_bonds': [{'target': 'c', 'content': '我们结婚了'}]}
        with patch('user_memory.invoke_structured_llm', return_value=invented) as extractor:
            self.process_group(1)
            extractor.assert_not_called()
        self.assertEqual(self.sql('SELECT COUNT(*) FROM bond_memory')[0][0], 0)
        facts = user_memory.get_long_memory('u', 'c')
        self.assertEqual(len(facts), 1)
        self.assertIn('她喜欢咖啡', facts[0][0])
        text = self.prompt_memory(self.current_result())
        self.assertIn('她喜欢咖啡', text)
        self.assertNotIn('火星', text)
        self.assertNotIn('已确认事实', text)

    def test_group_raw_and_job_commit_before_any_reply(self):
        self.group_source(1, '我的职业是「教师」。')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM chat_log')[0][0], 1)
        job = self.sql("""SELECT id,kind,user_id,character_id,user_text,assistant_text,
                         extra_json,attempts,source_event_id,assistant_event_id FROM memory_jobs""")[0]
        from memory_jobs import _run_job
        _run_job(job)
        self.assertEqual(self.sql('SELECT status FROM memory_jobs'), [('done',)])
        self.run_cycle()
        self.assertIn('教师', user_memory.get_long_memory('u', 'c')[0][0])

    def test_copied_transcript_and_members_without_source_stay_untrusted(self):
        self.assertFalse(user_memory.extract_and_save_group_memory(
            'u', '我的职业是「医生」', '群主：我的职业是「医生」', [{'id': 'c'}]))
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_events')[0][0], 0)

    def test_unaddressed_group_pronouns_and_irony_are_pending(self):
        for mid, content in enumerate(('我讨厌你才怪', '我喜欢你', '不要「开玩笑」'), 1):
            self.group_source(mid, content)
            self.process_group(mid)
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_beliefs')[0][0], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM cognitive_events WHERE adjudication->>'status'='pending'")[0][0], 3)

    def test_group_target_and_assistant_speaker_are_canonical(self):
        self.group_source(1, '不要「谈论体重」', target='c')
        self.process_group(1)
        self.group_source(2, '她是火星人，我们结婚了', role='character', sender='c')
        self.process_group(2)
        bonds = user_memory.get_bond_memories('u', 'c')
        self.assertEqual(len(bonds), 2)
        quote = next(text for _, text, _ in bonds if '我实际说过' in text)
        self.assertIn('不证明话中内容或关系', quote)
        self.assertEqual(user_memory.get_bond_memories('u', 'd'), [])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM cognitive_beliefs WHERE statement LIKE '%结婚%'")[0][0], 0)

    def test_group_foreign_owner_or_scope_cannot_ingest(self):
        self.group_source(1, '我喜欢咖啡')
        from cognitive_events import ingest_canonical_turn
        for owner, char, chat in (('other','shared','group:1'),('u','c','group:1'),('u','shared','group:2')):
            with self.subTest(owner=owner,char=char,chat=chat):
                result = ingest_canonical_turn(user_id=owner,character_id=char,
                    source_event_id='group:1:message:1', source_chat_id=chat)
                self.assertEqual(result['status'], 'pending_canonical_source')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_events')[0][0], 0)

    def test_generated_generic_writers_have_no_factual_authority(self):
        self.source('raw', '我喜欢咖啡')
        self.assertTrue(user_memory.save_long_memory('u', '她住在火星', '身份', 'c',
            source_event_refs=[{'source_id': 'raw'}]))
        with patch('smart_recall.link_bond_to_fact', return_value=None):
            self.assertTrue(user_memory.save_bond_memory('u', 'c', 'between', '我们结婚了'))
            self.assertTrue(user_memory.save_bond_memory('u', 'c', 'told', '她告诉我她住在火星'))
        self.assertEqual(self.sql('SELECT DISTINCT authority FROM long_memory'), [('generated_recollection',)])
        self.assertEqual(user_memory.get_long_memory('u','c'), [])
        self.assertEqual(user_memory.get_bond_memories('u','c'), [])
        self.assertNotIn('火星', self.prompt_memory(self.current_result()))

    def test_legacy_merge_resolution_and_invalidation_cannot_modify_canonical_bonds(self):
        self.report('raw', '不要「讨论体重」')
        mid, content, _ = user_memory.get_bond_memories('u', 'c')[0]
        self.assertEqual(user_memory.merge_bond_memories('u', 'c', 'between',
            [content], content + '，这是模型增加的条件'), (False, 0))
        self.assertEqual(user_memory.resolve_bond_memories('u', 'c', 'between',
            [content], reason='completed'), (False, []))
        self.assertEqual(user_memory.invalidate_bond_memories('u', 'c', 'between',
            [mid]), (False, []))
        self.assertEqual(self.sql('SELECT recall_status,content FROM bond_memory'), [('active', content)])

    def test_correction_invalidates_cached_prompt_and_late_generated_write(self):
        self.report('old', '我讨厌咖啡')
        cached = self.current_result()
        self.assertIn('讨厌咖啡', self.prompt_memory(cached))
        self.report('fix', '更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.assertNotIn('讨厌咖啡', self.prompt_memory(cached))
        user_memory.save_long_memory('u', '她讨厌咖啡，永远如此', '喜好','c',source_event_refs=[{'source_id':'old'}])
        self.assertEqual(len(user_memory.get_long_memory('u','c')), 1)
        self.assertIn('喜欢咖啡', user_memory.get_long_memory('u','c')[0][0])
        self.assertEqual(self.sql("SELECT recall_status FROM long_memory WHERE authority_event_id IS NOT NULL ORDER BY id"), [('superseded',),('active',)])

    def test_group_correction_is_scoped_and_retracts_shared_projection(self):
        self.group_source(1,'我讨厌咖啡')
        self.process_group(1)
        before = self.sql('SELECT content,recall_status,authority,authority_event_id FROM long_memory ORDER BY id')
        cached = self.current_result()
        self.group_source(2,'更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.process_group(2)
        facts = user_memory.get_long_memory('u','d')
        self.assertEqual(len(facts),1)
        self.assertIn('喜欢咖啡',facts[0][0])
        self.assertEqual(self.sql('SELECT status FROM cognitive_beliefs ORDER BY id'), [('retracted',),('active',)])
        self.assertNotIn('讨厌咖啡', self.prompt_memory(cached))
        if os.getenv('MEMORY_AUTHORITY_TEST_ARTIFACT'):
            Path(os.environ['MEMORY_AUTHORITY_TEST_ARTIFACT']).write_text(json.dumps({
                'scenario': 'canonical group correction with all cognitive model calls forbidden',
                'before_memory': before,
                'after_memory': self.sql('SELECT content,recall_status,authority,authority_event_id FROM long_memory ORDER BY id'),
                'beliefs': self.sql('SELECT statement,status FROM cognitive_beliefs ORDER BY id'),
                'events': self.sql('SELECT source_event_id,adjudication FROM cognitive_events ORDER BY id'),
                'current_other_character_reader': facts,
                'cached_prompt_after_correction': self.prompt_memory(cached),
                'guard_call_counts': [guard.call_count for guard in self.model_guards],
            }, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

    def test_source_deletion_or_changed_content_removes_memory_from_readers(self):
        self.report('old','我的名字是「小林」')
        cached = self.current_result()
        self.sql("UPDATE chat_log SET text='被修改' WHERE event_id='old'")
        self.database.commit()
        self.assertEqual(user_memory.get_long_memory('u','c'), [])
        self.assertNotIn('小林',self.prompt_memory(cached))
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='old'")
        self.database.commit()
        self.assertEqual(user_memory.get_long_memory('u','c'), [])

    def test_forged_badge_or_memory_content_cannot_pass_current_provenance(self):
        self.report('raw','我喜欢咖啡')
        cached = self.current_result()
        cached['facts'][0]['content']='她住在火星'
        self.assertIn('她喜欢咖啡', filter_recall_authority(cached,'u','c')['facts'][0]['content'])
        self.assertNotIn('火星', str(filter_recall_authority(cached,'u','c')))
        self.sql("UPDATE long_memory SET content='她住在火星'")
        self.database.commit()
        self.assertIn('她喜欢咖啡', user_memory.get_long_memory('u','c')[0][0])

    def test_versioned_semantic_survives_display_edit_and_repeated_ddl(self):
        from memory_authority import MEMORY_AUTHORITY_DDL
        self.report('raw', '我喜欢咖啡')
        original = self.sql("SELECT text FROM chat_log WHERE event_id='raw'")[0][0]
        before = self.sql('SELECT semantic_payload,authority,authority_event_id FROM long_memory')[0]
        self.sql("UPDATE long_memory SET content='过期的显示文字'")
        for _ in range(3):
            for ddl in MEMORY_AUTHORITY_DDL:
                self.sql(ddl)
        self.database.commit()
        self.assertEqual(self.sql("SELECT text FROM chat_log WHERE event_id='raw'")[0][0], original)
        self.assertEqual(self.sql('SELECT semantic_payload,authority,authority_event_id FROM long_memory')[0], before)
        self.assertEqual(self.sql("SELECT content FROM long_memory")[0][0], '过期的显示文字')
        self.assertIn('她喜欢咖啡', user_memory.get_long_memory('u', 'c')[0][0])
        self.assertIn('她喜欢咖啡', self.prompt_memory(self.current_result()))

    def test_cognitive_display_edit_does_not_change_role_visible_fact(self):
        self.report('raw', '我喜欢咖啡')
        metadata = self.sql('SELECT metadata FROM cognitive_questions')[0][0]
        metadata['current_judgment']['content'] = '她住在火星'
        self.sql('UPDATE cognitive_questions SET metadata=%s::jsonb',
                 (json.dumps(metadata, ensure_ascii=False),))
        self.sql("UPDATE cognitive_beliefs SET statement='她住在火星'")
        self.sql("UPDATE cognitive_hypotheses SET statement='她住在火星'")
        self.database.commit()
        from cognitive_reader import build_cognitive_prompt_context, iter_active_cognitive_items
        context = build_cognitive_prompt_context('u', 'c', conn=self.database)
        items = iter_active_cognitive_items('u', 'c', conn=self.database, query='咖啡')
        self.assertIn('她喜欢咖啡', context)
        self.assertNotIn('火星', context)
        self.assertTrue(any('她喜欢咖啡' in item['text'] for item in items))
        self.assertNotIn('火星', str(items))

    def test_legacy_canonical_and_startup_changed_projection_still_read(self):
        self.report('raw', '我喜欢咖啡')
        legacy = '用户明确自述：我喜欢咖啡（仅限这次自述，不推断隐含心理）'
        decision = self.sql('SELECT adjudication FROM cognitive_events')[0][0]
        for operation in (decision, decision['operations']['main']):
            operation['memory_content'] = legacy
            operation.pop('semantic_payload', None)
            operation.pop('projection_version', None)
        self.sql('UPDATE cognitive_events SET adjudication=%s::jsonb',
                 (json.dumps(decision, ensure_ascii=False),))
        self.sql("UPDATE cognitive_beliefs SET statement=%s,metadata=metadata-'semantic_payload'-'projection_version'",
                 (legacy,))
        self.sql("UPDATE long_memory SET content=%s,projection_version='legacy_v1',semantic_payload=NULL",
                 (legacy,))
        self.database.commit()
        self.assertIn('她喜欢咖啡', user_memory.get_long_memory('u', 'c')[0][0])
        self.sql('UPDATE long_memory SET content=%s', (legacy.replace('用户', '她'),))
        self.database.commit()
        self.assertIn('她喜欢咖啡', user_memory.get_long_memory('u', 'c')[0][0])
        self.sql("UPDATE long_memory SET content='她住在火星'")
        self.database.commit()
        self.assertEqual(user_memory.get_long_memory('u', 'c'), [])

    def test_private_and_shared_facts_keep_their_original_audience(self):
        self.report('private', '我喜欢咖啡')
        self.assertIn('她喜欢咖啡', user_memory.get_long_memory('u', 'c')[0][0])
        self.assertEqual(user_memory.get_long_memory('u', 'd'), [])
        self.group_source(1, '我喜欢茶')
        self.process_group(1)
        other_view = [text for text, _timestamp, _category in user_memory.get_long_memory('u', 'd')]
        self.assertTrue(any('她喜欢茶' in text for text in other_view))
        self.assertFalse(any('咖啡' in text for text in other_view))

    def test_forged_group_scope_is_rechecked_at_commit(self):
        self.group_source(1, '我喜欢你')
        from cognitive_events import record_source_event
        from cognitive_revision import apply_rule_evidence
        event_id = record_source_event(self.database, user_id='u', character_id='c',
            source_event_type='canonical_user_turn', source_event_id='group:1:message:1',
            source='forged_upstream', occurred_at=self.now,
            payload={'source_chat_id':'group:1','content':'我喜欢你'})
        self.sql("INSERT INTO cognitive_cycles(id,user_id,character_id,primary_trigger_class) VALUES (999,'u','c','question_reactivation')")
        result = apply_rule_evidence(self.database.cursor(), cycle_id=999, user_id='u',
            character_id='c', output={'evidence_refs':[{'event_id':event_id}]}, now=self.now)
        self.assertEqual(result[0]['status'],'pending')
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_beliefs')[0][0],0)

    def test_deleted_literal_source_cannot_return_through_cognitive_prompt(self):
        self.report('raw','我的职业是「教师」')
        from cognitive_reader import build_cognitive_prompt_context
        self.assertIn('教师', build_cognitive_prompt_context('u','c',conn=self.database))
        self.sql("UPDATE chat_log SET status='deleted'")
        self.database.commit()
        self.assertNotIn('教师', build_cognitive_prompt_context('u','c',conn=self.database))

    def test_previous_deterministic_belief_keeps_current_source_read_compatibility(self):
        self.report('raw', '我喜欢咖啡')
        # 93e0fa7 already adjudicated typed literal beliefs, before projections
        # added memory_content. This is not a migration of model-authored beliefs.
        self.sql("UPDATE cognitive_events SET adjudication=adjudication - 'memory_content' - 'memory_table'")
        self.database.commit()
        from cognitive_reader import build_cognitive_prompt_context
        self.assertIn('喜欢咖啡', build_cognitive_prompt_context('u','c',conn=self.database))
        self.sql("UPDATE chat_log SET status='deleted'")
        self.database.commit()
        self.assertNotIn('喜欢咖啡', build_cognitive_prompt_context('u','c',conn=self.database))

    def test_retracted_belief_alone_excludes_projection(self):
        self.report('raw','我喜欢咖啡')
        self.sql("UPDATE cognitive_beliefs SET status='retracted'")
        self.database.commit()
        self.assertEqual(user_memory.get_long_memory('u','c'), [])

    def test_vector_and_two_level_readers_filter_generated_rows(self):
        self.report('raw','我喜欢咖啡')
        user_memory.save_long_memory('u','她住在火星','身份','c')
        import memory_search
        with patch.object(memory_search,'_VECTOR_READY',True), \
             patch.object(memory_search,'embed',return_value=[1]), \
             patch.object(memory_search,'_to_vec',return_value=[1]), \
             patch.object(memory_search,'_top_k_ids',side_effect=lambda table,q,ids,k:[(i,1) for i in ids]):
            rows=memory_search.search_long_memory('u','c','shared','咖啡')
        self.assertEqual(len(rows),1)
        self.assertIn('喜欢咖啡',rows[0][0])
        import smart_recall
        # Optional subjective channels are orthogonal to factual authority.
        with patch('memory_lifecycle.recall_lifecycle_memories',return_value=[]), \
             patch('memory_lifecycle.recall_sticky_notes',return_value=[]), \
             patch('memory_lifecycle.recall_diary_memories',return_value=[]):
            result=smart_recall.two_level_recall('u','c','咖啡')
        self.assertIsNotNone(result)
        self.assertEqual(len(result['facts']),1)
        self.assertNotIn('火星',self.prompt_memory(result))

    def test_memory_page_keeps_legacy_history_separate_from_recallable_fact(self):
        self.report('raw', '我喜欢咖啡')
        user_memory.save_long_memory('u', '旧记忆仍保留', '其他', 'c')
        user_memory.save_long_memory('u', '另一角色的旧记忆', '其他', 'd')
        user_memory.save_long_memory('other', '另一用户的旧记忆', '其他', 'c')
        import route_memory
        with patch('route_memory.get_conn', return_value=self.database):
            response = asyncio.run(route_memory.list_long_memory('u', 'c'))
        rows = json.loads(response.body)['memories']
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row['recallable'] for row in rows), 1)
        self.assertTrue(any('喜欢咖啡' in row['content'] and row['recallable'] for row in rows))
        self.assertTrue(any(row['content'] == '旧记忆仍保留' and not row['recallable'] for row in rows))
        self.assertTrue(any('喜欢咖啡' in row['content'] and row['content_locked'] for row in rows))
        self.assertTrue(any(row['content'] == '旧记忆仍保留' and not row['content_locked'] for row in rows))

    def test_memory_page_marks_stale_state_ineligible_like_chat_recall(self):
        self.report('state', '我的状态是「很累」')
        self.sql("UPDATE long_memory SET timestamp=CURRENT_TIMESTAMP - INTERVAL '49 hours' "
                 "WHERE category='状态'")
        self.database.commit()
        import route_memory
        with patch('route_memory.get_conn', return_value=self.database):
            response = asyncio.run(route_memory.list_long_memory('u', 'c'))
        rows = json.loads(response.body)['memories']
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]['recallable'])
        self.assertTrue(rows[0]['content_locked'])

    def test_bond_page_keeps_legacy_history_without_exposing_it_to_chat(self):
        self.report('boundary', '不要「讨论体重」')
        with patch('smart_recall.link_bond_to_fact', return_value=None):
            user_memory.save_bond_memory('u', 'c', 'between', '旧共同记忆')
        import route_memory
        with patch('route_memory.get_conn', return_value=self.database):
            response = asyncio.run(route_memory.list_bond_memory(
                'u', 'c', kind='between', include_history=True))
        rows = json.loads(response.body)['memories']
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row['recallable'] for row in rows), 1)
        self.assertTrue(any(row['content'] == '旧共同记忆' and not row['recallable']
                            for row in rows))
        self.assertEqual(len(user_memory.get_bond_memories('u', 'c', 'between')), 1)

    def test_memory_page_cannot_rewrite_canonical_fact_or_bond_content(self):
        self.report('fact', '我喜欢咖啡')
        self.report('boundary', '不要「讨论体重」')
        fact_id, fact_text = self.sql('SELECT id,content FROM long_memory')[0]
        bond_id, bond_text = self.sql('SELECT id,content FROM bond_memory')[0]
        import route_memory
        with patch('route_memory.get_conn', return_value=self.database), \
             patch('route_memory.notify_memory_changed') as changed:
            fact_response = asyncio.run(route_memory.update_long_memory(
                fact_id, {'content': '模型改写的事实', 'category': '喜好'}))
            bond_response = asyncio.run(route_memory.edit_bond_memory(
                bond_id, {'content': '模型改写的共同记忆'}))
        self.assertEqual((fact_response.status_code, bond_response.status_code), (409, 409))
        self.assertEqual(self.sql('SELECT content FROM long_memory WHERE id=%s',
                                  (fact_id,))[0][0], fact_text)
        self.assertEqual(self.sql('SELECT content FROM bond_memory WHERE id=%s',
                                  (bond_id,))[0][0], bond_text)
        changed.assert_not_called()

    def test_explicit_fact_state_refusal_boundary_and_date_range_are_applied(self):
        statements=('我的职业是「教师」','我的状态是「很累」',
            '我拒绝「深夜来电」','不要「讨论体重」',
            '在2026-09-01至2026-09-30，我的居住地是「上海」')
        for i,text in enumerate(statements):
            self.report(str(i),text)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM cognitive_beliefs WHERE status='active'")[0][0],5)
        self.assertEqual(len(user_memory.get_bond_memories('u','c')),2)
        self.assertEqual(len(user_memory.get_long_memory('u','c')),3)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM long_memory WHERE category='状态' AND expires_at IS NOT NULL")[0][0],1)

    def test_explicit_promise_repeat_and_fulfillment_use_existing_prediction_lifecycle(self):
        deadline=(self.now+timedelta(days=2)).date().isoformat()
        self.report('promise',f'我承诺在{deadline}前完成「寄明信片」')
        self.report('repeat',f'我承诺在{deadline}前完成「寄明信片」')
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'), [('pending',)])
        self.report('done',f'我已兑现截至{deadline}的承诺「寄明信片」')
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'), [('fulfilled',)])
        bonds=user_memory.get_bond_memories('u','c')
        self.assertEqual(len(bonds),1)
        self.assertIn('已兑现',bonds[0][1])

    def test_unmatched_fulfillment_and_different_deadline_remain_pending(self):
        deadline=(self.now+timedelta(days=2)).date().isoformat()
        other=(self.now+timedelta(days=3)).date().isoformat()
        self.report('promise',f'我承诺在{deadline}前完成「寄明信片」')
        self.report('wrong',f'我已兑现截至{other}的承诺「寄明信片」')
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'), [('pending',)])
        self.assertEqual(self.sql("SELECT adjudication->>'status' FROM cognitive_events WHERE source_event_id='wrong'"), [('pending',)])

    def test_fulfillment_in_another_chat_cannot_settle_group_commitment(self):
        deadline=(self.now+timedelta(days=2)).date().isoformat()
        self.group_source(1, f'我承诺在{deadline}前完成「寄明信片」', target='c')
        self.process_group(1)
        self.report('private-done', f'我已兑现截至{deadline}的承诺「寄明信片」')
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'), [('pending',)])
        self.assertEqual(self.sql("SELECT adjudication->>'reason' FROM cognitive_events WHERE source_event_id='private-done'"), [('fulfillment_source_scope_mismatch',)])
        self.assertIn('她承诺', user_memory.get_bond_memories('u','c')[0][1])
        self.group_source(2, f'我已兑现截至{deadline}的承诺「寄明信片」', target='c')
        # Commit a later occurrence so it is eligible to settle the prediction.
        self.sql("UPDATE chat_log SET created_at=%s WHERE event_id='group:1:message:2'", (self.now,))
        self.database.commit()
        self.process_group(2)
        self.assertEqual(self.sql('SELECT status FROM cognitive_predictions'), [('fulfilled',)])

    def test_private_actual_assistant_quote_does_not_become_user_fact(self):
        self.source('user','我喜欢咖啡')
        self.source('reply','她是火星人',role='assistant')
        self.assertTrue(user_memory.extract_and_save_memory('u','copied','伪造另一句话','c',
            source_event_id='user',source_event_ids=['user','reply']))
        self.run_cycle()
        import cognitive_worker
        cognitive_worker.run_worker_once(now=self.now+timedelta(seconds=1))
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_beliefs')[0][0],1)
        quote=user_memory.get_bond_memories('u','c')[0][1]
        self.assertIn('她是火星人',quote)
        self.assertNotIn('伪造另一句话',quote)

    def test_repeating_conflict_does_not_silently_resolve_it(self):
        self.report('first','我讨厌咖啡')
        self.report('conflict','我喜欢咖啡')
        self.report('repeat','我喜欢咖啡')
        self.assertEqual(user_memory.get_long_memory('u','c'),[])
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_beliefs')[0][0],1)

    def test_failed_projection_rolls_back_correction_and_retries(self):
        self.report('first','我讨厌咖啡')
        self.source('fix','更正：「我讨厌咖啡」不对，应为「我喜欢咖啡」。')
        self.ingest('fix')
        import cognitive_worker
        with patch('memory_authority.project_canonical_memory',side_effect=RuntimeError('projection_failure')):
            self.assertEqual(cognitive_worker.run_worker_once(now=self.now)['status'],'failed')
        self.assertIn('讨厌咖啡',user_memory.get_long_memory('u','c')[0][0])
        self.run_cycle()
        self.assertIn('喜欢咖啡',user_memory.get_long_memory('u','c')[0][0])


class LiteralGrammarTests(unittest.TestCase):
    def test_invalid_dates_mixed_clauses_and_irony_stay_pending(self):
        for text in ('在2026-02-30，我喜欢咖啡','在2026-10-01至2026-09-01，我喜欢咖啡',
                     '我拒绝「来电」，开玩笑的','不要「体重」才怪','我的职业是「医生」，其实不是',
                     '我的状态是开心吗？','我已兑现承诺','她说我喜欢咖啡'):
            with self.subTest(text=text):
                self.assertEqual(cognitive_revision.parse_cognitive_evidence(text,'u','c')['operation'],'pending')

    def test_old_mutating_authority_routes_are_absent(self):
        import relationship_engine
        for name in ('_route_signal','_apply_extracted_signals','_apply_care','_handle_flirt_signal',
                     '_handle_reciprocal','_maybe_mark_romantic_reappraisal'):
            self.assertFalse(hasattr(relationship_engine,name),name)
        import inspect
        import cognitive_output
        source=inspect.getsource(cognitive_output.persist_slow_loop_output)
        self.assertNotIn('INSERT INTO',source)
        self.assertNotIn('UPDATE cognitive_',source)
        import relationship_backfill
        for name in ('estimate_baseline', 'apply_baseline', 'process_one', '_BACKFILL_SYSTEM_PROMPT'):
            self.assertFalse(hasattr(relationship_backfill, name), name)
