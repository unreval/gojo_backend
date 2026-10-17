"""Real PostgreSQL source validation, with writes and model calls forbidden."""
import ast
from copy import deepcopy
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gojo_backend'))
import memory_candidate


class CandidateArchitectureTests(unittest.TestCase):
    def test_candidate_has_no_model_or_alternate_memory_writer_dependency(self):
        tree = ast.parse(Path(memory_candidate.__file__).read_text(encoding='utf-8'))
        imports = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.add((node.module or '').split('.')[0])
                if node.module == 'memory_authority':
                    self.assertEqual({alias.name for alias in node.names},
                                     {'project_canonical_memory'})
        self.assertFalse(imports & {
            'ai_client', 'structured_output', 'anthropic', 'openai',
            'user_memory', 'cognitive_worker',
            'memory_jobs', 'memory_search',
        })


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'),
                     'set COGNITIVE_TEST_PGLITE to the local test-only package')
class CandidateSourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def setUp(self):
        import cognitive_db
        from tests.memory_schema import create_memory_tables
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
        self.database.commit()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch('db.get_conn', return_value=self.database))
        self.stack.enter_context(patch('raw_events.get_conn', return_value=self.database))
        self.model_guards = [self.stack.enter_context(patch(target,
            side_effect=AssertionError('MODEL_CALLED_IN_CANDIDATE_VALIDATION')))
            for target in (
                'ai_client.create_chat', 'ai_client._call_anthropic',
                'ai_client._call_deepseek', 'structured_output.invoke_structured_llm',
                'user_memory.invoke_structured_llm',
                'memory_search.embed',
            )]
        self.now = datetime(2026, 10, 18, 12, tzinfo=timezone.utc)

    def tearDown(self):
        for guard in self.model_guards:
            guard.assert_not_called()

    def sql(self, sql, params=()):
        cur = self.database.cursor()
        cur.execute(sql, params)
        return cur.fetchall()

    def source(self, *, text='今天我亏了69元，想和你聊一聊。', role='user',
               user='u', character='c', source_id='source', status='active'):
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,created_at,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s)''',
            (user, character, source_id, role, text, self.now, status))
        return text

    def snapshot(self, text, *, user='u', character='c', source_id='source',
                 event_type='canonical_user_turn', source_chat_id=None):
        payload = {'content': text}
        if source_chat_id is not None:
            payload['source_chat_id'] = source_chat_id
        self.sql('''INSERT INTO cognitive_events
            (user_id,character_id,source_event_type,source_event_id,source,
             occurred_at,payload) VALUES (%s,%s,%s,%s,'chat',%s,%s::jsonb)''',
            (user, character, event_type, source_id, self.now,
             json.dumps(payload, ensure_ascii=False)))

    def candidate(self, **changes):
        return dict(source_event_id='source', quote='今天我亏了69元',
                    summary='她今天亏了69元', predicate='reported_fact',
                    object='交易结果', value='亏了69元', **changes)

    def database_state(self):
        tables = self.sql("""SELECT tablename FROM pg_tables
            WHERE schemaname='public' ORDER BY tablename""")
        return {table: self.sql(
            f'SELECT to_jsonb(row) FROM "{table}" row ORDER BY to_jsonb(row)::text')
            for table, in tables}

    def validate(self, candidate=None, *, user='u', character='c'):
        before = self.database_state()
        query = self.database.query

        def read_only_query(sql, params=()):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT '), sql)
            return query(sql, params)

        try:
            with patch.object(self.database, 'query', side_effect=read_only_query):
                return memory_candidate.validate_candidate(
                    self.database.cursor(), user, character,
                    candidate if candidate is not None else self.candidate())
        finally:
            self.assertEqual(self.database_state(), before)

    def test_current_canonical_user_source_passes_without_any_database_writes(self):
        self.snapshot(self.source())
        self.assertEqual(self.validate(), (True, None))

    def test_source_reads_hold_locks_on_the_callers_connection(self):
        self.snapshot(self.source())
        from raw_events import get_active_events_by_ids
        with patch.object(memory_candidate, 'get_active_events_by_ids',
                          wraps=get_active_events_by_ids) as source_read, \
             patch.object(self.database, 'query', wraps=self.database.query) as queries:
            self.assertEqual(self.validate(), (True, None))
        source_read.assert_called_once_with(
            'u', 'c', ['source'], conn=self.database, lock=True)
        snapshot_sql = [call.args[0] for call in queries.call_args_list
                        if 'SELECT payload FROM cognitive_events'
                        in ' '.join(call.args[0].split())]
        self.assertEqual(len(snapshot_sql), 1)
        self.assertIn('FOR SHARE', snapshot_sql[0])

    def test_edited_body_is_rejected_even_when_verbatim_quote_survives(self):
        self.snapshot(self.source())
        self.sql("UPDATE chat_log SET text=%s WHERE event_id='source'",
                 ('今天我亏了69元，这只是我举的例子。',))
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_missing_cognitive_snapshot_is_rejected(self):
        self.source()
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_deleted_source_is_rejected(self):
        self.snapshot(self.source(status='deleted'))
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_removed_source_is_rejected(self):
        self.snapshot('今天我亏了69元')
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_foreign_raw_owner_is_rejected(self):
        self.snapshot(self.source(user='other'))
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_foreign_raw_character_scope_is_rejected(self):
        self.snapshot(self.source(character='other'))
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_foreign_snapshot_owner_is_rejected(self):
        self.snapshot(self.source(), user='other')
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_foreign_snapshot_character_scope_is_rejected(self):
        self.snapshot(self.source(), character='other')
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_assistant_snapshot_cannot_substitute_for_user_snapshot(self):
        self.snapshot(self.source(), event_type='canonical_assistant_turn')
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_assistant_raw_role_is_rejected_despite_a_user_snapshot(self):
        self.snapshot(self.source(role='assistant'))
        self.assertEqual(self.validate(), (False, 'source_not_active_or_not_user'))

    def test_group_scope_cannot_be_treated_as_private_scope(self):
        self.snapshot(self.source(character='group:1'), character='group:1')
        self.assertEqual(self.validate(character='group:1'),
                         (False, 'source_not_active_or_changed'))

    def test_missing_snapshot_content_is_rejected(self):
        self.snapshot(self.source())
        self.sql("UPDATE cognitive_events SET payload='{}'::jsonb")
        self.assertEqual(self.validate(), (False, 'source_not_active_or_changed'))

    def test_candidate_supplied_snapshot_cannot_hide_a_changed_source(self):
        self.snapshot(self.source())
        edited = '今天我亏了69元，这只是我举的例子。'
        self.sql("UPDATE chat_log SET text=%s WHERE event_id='source'", (edited,))
        candidate = self.candidate(source_content=edited, snapshot={'content': edited})
        self.assertEqual(self.validate(candidate),
                         (False, 'source_not_active_or_changed'))

    def test_source_rejection_precedes_other_gates_and_preserves_candidate(self):
        self.source()
        candidate = self.candidate(perspective='她其实很在意')
        candidate.update(quote=None, summary=[], predicate='invented')
        before = deepcopy(candidate)
        self.assertEqual(self.validate(candidate),
                         (False, 'source_not_active_or_changed'))
        self.assertEqual(candidate, before)

    def test_group_scope_comes_from_canonical_snapshot_and_audience(self):
        source_id = 'group:1:message:source'
        text = self.source(character='group:1', source_id=source_id)
        self.snapshot(text, source_id=source_id, source_chat_id='group:1')
        metadata = {'canonical_group': True, 'target_character_id': 'c',
                    'audience': ['c', 'd']}
        self.sql('UPDATE chat_log SET extra=%s', (json.dumps(metadata),))
        candidate = self.candidate(source_chat_id='group:forged')
        candidate['source_event_id'] = source_id
        self.assertEqual(self.validate(candidate), (True, None))
        metadata['target_character_id'] = 'd'
        self.sql('UPDATE chat_log SET extra=%s', (json.dumps(metadata),))
        self.assertEqual(self.validate(candidate),
                         (False, 'source_not_active_or_changed'))

    def test_unsafe_perspective_drop_has_no_database_side_effect(self):
        self.snapshot(self.source())
        candidate = self.candidate(perspective='她其实很在意')
        self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
        self.assertNotIn('perspective', candidate)

    def test_rejected_quote_has_no_database_side_effect(self):
        self.snapshot(self.source())
        candidate = self.candidate()
        candidate['quote'] = '今天我赚了69元'
        self.assertEqual(self.validate(candidate), (False, 'quote_not_verbatim'))

    def test_raw_lookup_database_exception_is_not_reported_as_validation_success(self):
        from raw_events import SourceValidityError
        self.snapshot(self.source())
        query = self.database.query

        def failing_query(sql, params=()):
            if 'FROM chat_log' in sql:
                raise RuntimeError('injected_raw_lookup_failure')
            return query(sql, params)

        with patch.object(self.database, 'query', side_effect=failing_query):
            with self.assertRaisesRegex(SourceValidityError, 'injected_raw_lookup_failure'):
                self.validate()

    def test_snapshot_database_exception_is_not_reported_as_validation_success(self):
        self.snapshot(self.source())
        query = self.database.query

        def failing_query(sql, params=()):
            if 'SELECT payload FROM cognitive_events' in ' '.join(sql.split()):
                raise RuntimeError('injected_snapshot_failure')
            return query(sql, params)

        with patch.object(self.database, 'query', side_effect=failing_query):
            with self.assertRaisesRegex(RuntimeError, 'injected_snapshot_failure'):
                self.validate()


if __name__ == '__main__':
    unittest.main()
