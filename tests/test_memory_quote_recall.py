"""Assistant speech stays audited without becoming a recallable bond memory."""
import asyncio
from datetime import timedelta
import json
import inspect
import os
import unittest
from unittest.mock import patch

from tests.canonical_memory_fixture import CanonicalMemoryFixture
import cognitive_events
import user_memory
from memory_authority import canonical_semantic_payload, project_canonical_memory
from role_view import PROJECTION_VERSION, render_role_view


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'requires offline PostgreSQL fixture')
class MemoryQuoteRecallTests(CanonicalMemoryFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch('user_memory.get_conn', return_value=self.database))
        self.stack.enter_context(patch('route_memory.get_conn', return_value=self.database))

    def turn(self, source_id, text, role='user'):
        self.source(source_id, text, role=role)
        result = cognitive_events.ingest_canonical_turn(
            user_id='u', character_id='c', source_event_id=source_id, allow_assistant=True)
        self.assertEqual(result['status'], 'inserted')
        while self.sql("SELECT count(*) FROM cognitive_event_triggers WHERE status IN ('pending','claimed')")[0][0]:
            self.run_cycle()
        return self.sql('SELECT id,adjudication,occurred_at FROM cognitive_events '
                        'WHERE source_event_id=%s', (source_id,))[0]

    def legacy_quote(self):
        """Retain a pre-fix row and its original, valid projection adjudication."""
        text = '我会认真答复。'
        event_id, decision, occurred_at = self.turn('old-quote', text, 'assistant')
        projection = project_canonical_memory(
            self.database.cursor(), user_id='u', character_id='c', event_id=event_id,
            source_id='old-quote', content='', occurred_at=occurred_at,
            semantic={'subject_ref': 'character:c', 'predicate': 'quoted_utterance',
                      'evidence_text': text})
        decision.update(projection)
        decision['operations']['main'].update(projection)
        self.sql('UPDATE cognitive_events SET adjudication=%s::jsonb WHERE id=%s',
                 (json.dumps(decision, ensure_ascii=False), event_id))
        self.database.commit()
        return self.sql("SELECT id,content,semantic_payload FROM bond_memory "
                        "WHERE semantic_payload->>'predicate'='quoted_utterance'")[0]

    def test_assistant_quote_keeps_exact_audit_without_projection(self):
        text = '她是火星人。'
        _, decision, _ = self.turn('assistant', text, 'assistant')
        operation = decision['operations']['main']
        semantic = canonical_semantic_payload(
            user_id='u', character_id='c', source_id='assistant',
            semantic={'subject_ref': 'character:c', 'predicate': 'quoted_utterance',
                      'evidence_text': text})
        self.assertEqual(operation, {
            'status': 'applied', 'action': 'quoted',
            'memory_content': render_role_view('', observer_id='c',
                source_character_id='c', semantic=semantic),
            'projection_version': PROJECTION_VERSION, 'semantic_payload': semantic,
        })
        self.assertEqual((decision['status'], decision['action']), ('applied', 'quoted'))
        self.assertEqual(self.sql('SELECT text FROM chat_log'), [(text,)])
        for table in ('bond_memory', 'long_memory', 'memory_source_events', 'cognitive_beliefs'):
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table}'), [(0,)], table)

    def test_assistant_exchange_keeps_utterance_operation_identity(self):
        _, decision, _ = self.turn('question', '我可以叫你宝宝吗？', 'assistant')
        self.assertEqual(decision['operations']['utterance']['action'], 'quoted')
        self.assertEqual(decision['operations']['utterance']['status'], 'applied')
        self.assertNotIn('memory_table', decision['operations']['utterance'])
        self.assertEqual(self.sql("SELECT count(*) FROM bond_memory WHERE "
                                 "semantic_payload->>'predicate'='quoted_utterance'"), [(0,)])

    def test_old_quote_is_filtered_for_both_bond_readers_without_deleting_history(self):
        quote = self.legacy_quote()
        self.turn('boundary', '不要「讨论体重」')
        for kind in ('between', None):
            with self.subTest(kind=kind):
                rows = user_memory.get_bond_memories('u', 'c', kind=kind)
                self.assertEqual(len(rows), 1)
                self.assertIn('讨论体重', rows[0][1])
                self.assertNotIn(quote[0], [row[0] for row in rows])
        self.assertEqual(self.sql("SELECT id,content,semantic_payload FROM bond_memory "
                                 "WHERE id=%s", (quote[0],)), [quote])

    def test_page_retains_old_quote_only_as_nonrecallable_history(self):
        import route_memory
        quote = self.legacy_quote()
        self.turn('boundary', '不要「讨论体重」')
        normal = json.loads(asyncio.run(route_memory.list_bond_memory(
            'u', 'c', kind='between', include_history=False)).body)['memories']
        self.assertEqual(len(normal), 1)
        history = json.loads(asyncio.run(route_memory.list_bond_memory(
            'u', 'c', kind='between', include_history=True)).body)['memories']
        self.assertEqual(len(history), 2)
        old = next(row for row in history if row['id'] == quote[0])
        self.assertFalse(old['recallable'])
        self.assertTrue(old['content_locked'])
        self.assertIn('我实际说过', old['content'])
        self.assertEqual(sum(row['recallable'] for row in history), 1)

    def test_prompt_fallback_bond_block_excludes_old_quote(self):
        import context_layer
        import prompt
        self.legacy_quote()
        self.turn('boundary', '不要「讨论体重」')
        pack = context_layer.ChatContextPack(support_ready=True, recall_ready=False)
        with patch.object(prompt, 'get_character', return_value={'core_prompt': '角色'}), \
             patch.object(prompt, 'get_first_interaction_days', return_value=1), \
             patch.object(prompt, 'load_canon_lock', return_value=''), \
             patch.object(prompt, '_accounts_block', return_value=''), \
             patch('memory_search.is_vector_ready', return_value=False), \
             patch('smart_recall.two_level_recall', return_value=None), \
             patch('db_schedule.format_world_prompt', return_value=('', {})):
            text = prompt._build_prompt_parts('u', 'c', user_message='体重', context_pack=pack)[-1]
        self.assertIn('讨论体重', text)
        self.assertNotIn('我实际说过', text)

    def prompt_text(self, pack):
        import prompt
        with patch.object(prompt, 'get_character', return_value={'core_prompt': '角色'}), \
                patch.object(prompt, 'get_first_interaction_days', return_value=1), \
                patch.object(prompt, 'load_canon_lock', return_value=''), \
                patch.object(prompt, '_accounts_block', return_value=''), \
                patch('memory_search.is_vector_ready', return_value=False), \
                patch('db_schedule.format_world_prompt', return_value=('', {})):
            return prompt._build_prompt_parts(
                'u', 'c', user_message='我会认真答复，也不要讨论体重', context_pack=pack)[-1]

    def test_cached_pack_filters_loose_and_attached_quotes_at_prompt_boundary(self):
        import context_layer
        quote = self.legacy_quote()
        self.turn('fact', '我的职业是「教师」')
        self.turn('boundary', '不要「讨论体重」')
        fact_id = self.sql('SELECT id FROM long_memory')[0][0]
        boundary_id = self.sql("SELECT id FROM bond_memory WHERE "
                               "semantic_payload->>'predicate'='explicit_boundary'")[0][0]
        pack = context_layer.ChatContextPack(
            support_ready=True, recall_ready=True, recall_result={
                'facts': [{'id': fact_id, 'timestamp': self.now, 'category': '身份',
                           'bonds': [{'id': quote[0], 'timestamp': self.now}]}],
                'loose_bonds': [{'id': quote[0], 'timestamp': self.now},
                                {'id': boundary_id, 'timestamp': self.now}], 'tolds': []})
        text = self.prompt_text(pack)
        self.assertIn('教师', text)
        self.assertIn('讨论体重', text)
        self.assertNotIn('我实际说过', text)
        self.assertEqual(self.sql('SELECT count(*) FROM bond_memory'), [(2,)])

    def test_two_level_recall_quote_is_filtered_before_prompt_injection(self):
        import context_layer
        import smart_recall
        quote = self.legacy_quote()
        self.turn('boundary', '不要「讨论体重」')
        self.stack.enter_context(patch('smart_recall.get_conn', return_value=self.database))
        for reader in ('recall_lifecycle_memories', 'recall_sticky_notes', 'recall_diary_memories'):
            self.stack.enter_context(patch('memory_lifecycle.' + reader, return_value=[]))
        candidates = smart_recall.two_level_recall('u', 'c', '我会认真答复，也不要讨论体重')
        self.assertIn(quote[0], [row['id'] for row in candidates['loose_bonds']])
        with patch.object(smart_recall, 'two_level_recall', wraps=smart_recall.two_level_recall) as recall:
            text = self.prompt_text(context_layer.ChatContextPack(support_ready=True, recall_ready=False))
        recall.assert_called_once()
        self.assertIn('讨论体重', text)
        self.assertNotIn('我实际说过', text)

    def test_diary_prompt_excludes_old_quote_with_and_without_cached_pack(self):
        import context_layer
        import diary_engine
        quote = self.legacy_quote()
        self.turn('boundary', '不要「讨论体重」')
        pack = context_layer.ChatContextPack(recall_ready=True, recall_result={
            'facts': [], 'loose_bonds': [{'id': quote[0]}], 'tolds': []})
        for cached in (pack, None):
            with self.subTest(cached=cached is not None), \
                    patch.object(diary_engine, 'get_character', return_value={'name': '角色'}), \
                    patch.object(diary_engine, '_gather_daily_diary_material', return_value=('今天聊过', '', 1)), \
                    patch.object(diary_engine, 'get_first_interaction_days', return_value=1), \
                    patch('context_layer.build_chat_context', return_value=cached), \
                    patch('db_diary.count_char_diaries_since', return_value=0), \
                    patch('db_diary.add_char_diary', return_value=(1, self.now)), \
                    patch('db_diary.has_named_self', return_value=True), \
                    patch('ai_client.create_chat', return_value=('{"content":"今天聊过。","emotion":"平静"}', {})) as fake_model:
                self.assertEqual(diary_engine.generate_char_diary('c', 'u'), (1, '今天聊过。', '平静'))
            fake_model.assert_called_once()
            text = fake_model.call_args.kwargs['messages'][0]['content']
            self.assertIn('讨论体重', text)
            self.assertNotIn('我实际说过', text)

    def test_vector_bond_reader_excludes_old_quote_with_stub_embeddings(self):
        import memory_search
        self.legacy_quote()
        self.turn('boundary', '不要「讨论体重」')
        with patch.object(memory_search, 'get_conn', return_value=self.database), \
                patch.object(memory_search, '_VECTOR_READY', True), \
                patch.object(memory_search, 'embed', return_value=[1.0, 0.0]), \
                patch.object(memory_search, '_to_vec', side_effect=lambda value: value), \
                patch.object(memory_search, '_top_k_ids', side_effect=lambda table, vector, ids, limit:
                             [(row_id, 1.0) for row_id in ids]):
            rows = memory_search.search_bond_memory('u', 'c', 'between', '讨论体重')
        self.assertEqual(len(rows), 1)
        self.assertIn('讨论体重', rows[0][1])
        self.assertNotIn('我实际说过', rows[0][1])

    def test_recall_queries_use_identical_exclusion_text(self):
        import memory_authority
        import memory_search
        import route_memory
        condition = "AND COALESCE(semantic_payload->>'predicate','') <> 'quoted_utterance'"
        for function, count in ((user_memory.get_bond_memories, 2),
                                (memory_authority.filter_recall_authority, 1),
                                (memory_search.search_bond_memory, 1),
                                (route_memory.list_bond_memory, 1)):
            self.assertEqual(inspect.getsource(function).count(condition), count, function.__name__)

    def test_declared_stance_uses_applied_audit_without_bond_projection(self):
        import relationship_state
        text = '我会认真答复。'
        self.turn('stance', text, 'assistant')
        self.sql('''CREATE TABLE rel_declared_stance (
            id SERIAL PRIMARY KEY,user_id TEXT,character_id TEXT,stance_type TEXT,
            content TEXT,source_event_ref TEXT,status TEXT DEFAULT 'active',
            declared_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            revoked_at TIMESTAMPTZ,revoke_reason TEXT)''')
        self.sql('''INSERT INTO rel_declared_stance
            (user_id,character_id,stance_type,content,source_event_ref)
            VALUES ('u','c','care_admission',%s,'stance')''', (text,))
        with patch('relationship_state.get_conn', return_value=self.database):
            self.assertEqual([row['content'] for row in
                              relationship_state.list_active_stances('u', 'c')], [text])
            self.assertEqual(self.sql('SELECT count(*) FROM bond_memory'), [(0,)])
            self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='stance'")
            self.assertEqual(relationship_state.list_active_stances('u', 'c'), [])

    def test_seven_user_fact_forms_still_project_to_long_memory(self):
        forms = ('我喜欢咖啡', '我不喜欢茶', '我讨厌甜食', '我的名字是「小夏」',
                 '我的职业是「教师」', '我的状态是「很累」',
                 '在2026-09-01至2026-09-30，我的居住地是「上海」')
        for index, text in enumerate(forms):
            with self.subTest(text=text):
                event_id, decision, _ = self.turn(f'fact-{index}', text)
                self.assertEqual(decision['status'], 'applied')
                self.assertEqual(decision['memory_table'], 'long_memory')
                self.assertEqual(self.sql('SELECT count(*) FROM long_memory '
                                         'WHERE authority_event_id=%s', (event_id,)), [(1,)])
        self.assertEqual(len(user_memory.get_long_memory('u', 'c')), 7)
        self.assertEqual(self.sql('SELECT count(*) FROM bond_memory'), [(0,)])

    def test_all_seven_literal_grammar_patterns_keep_existing_destinations(self):
        deadline = (self.now + timedelta(days=2)).date().isoformat()
        forms = (
            ('我喜欢咖啡', 'long_memory'), ('我的职业是「教师」', 'long_memory'),
            ('我拒绝「深夜来电」', 'bond_memory'), ('不要「讨论体重」', 'bond_memory'),
            (f'我承诺在{deadline}前完成「寄明信片」', 'bond_memory'),
            (f'我已兑现截至{deadline}的承诺「寄明信片」', 'bond_memory'),
            ('更正：「我喜欢咖啡」不对，应为「我讨厌咖啡」。', 'long_memory'),
        )
        for index, (text, table) in enumerate(forms):
            with self.subTest(text=text):
                event_id, decision, _ = self.turn(f'grammar-{index}', text)
                self.assertEqual(decision['status'], 'applied')
                self.assertEqual(decision['memory_table'], table)
                self.assertEqual(self.sql(f'SELECT count(*) FROM {table} '
                                         'WHERE authority_event_id=%s', (event_id,)), [(1,)])
        self.assertEqual(len(user_memory.get_long_memory('u', 'c')), 2)
        self.assertEqual(len(user_memory.get_bond_memories('u', 'c', 'between')), 3)
        self.assertIn(('fulfilled',), self.sql('SELECT status FROM cognitive_predictions'))


if __name__ == '__main__':
    unittest.main()
