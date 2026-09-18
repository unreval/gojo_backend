import importlib.util
import os
import sys
import types
import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from unittest.mock import Mock, patch


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

fake_db = types.ModuleType('db')
fake_db.get_conn = Mock(side_effect=AssertionError('unexpected database access'))
sys.modules.setdefault('db', fake_db)

import cognitive_reader
import memory_lifecycle
import shared_relation_prompt
import smart_recall
from cognitive_config import COGNITIVE_DIARY_RECALL_ENTRY_CHARS


NOW = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
DIARY_TEXT = '考试那天，我当时觉得她在强撑，这只是我的猜测。'
REFLECTION_TEXT = '考试结束后，我当时担心她没考过。'
UNRELATED_TEXT = '今天的甜品很好吃，想再买一份。'
FACT_TEXT = '她明确说考试已经通过，成绩很好。'


class RecallDatabase:
    def __init__(self):
        self.diaries = [
            (3, DIARY_TEXT, '担心', NOW),
            (4, UNRELATED_TEXT, '平静', NOW),
        ]
        self.reflections = [
            (7, 'exam.reflection', REFLECTION_TEXT, 'event',
             [{'source_type': 'cognitive_event', 'source_id': 27}], NOW),
        ]
        self.facts = [(10, FACT_TEXT, NOW, '经历', 1, None, False, 'long_fact', 1.0)]
        self.executed = []
        self.commits = 0
        self.closed = 0

    def cursor(self):
        database = self

        class Cursor:
            rows = []

            def execute(self, sql, params=None):
                compact = ' '.join(sql.split())
                database.executed.append((compact, params))
                if not compact.startswith('SELECT'):
                    raise AssertionError('recall must be read-only: ' + compact)
                if 'FROM cognitive_diary_entries' in compact:
                    self.rows = database.reflections
                elif 'FROM char_diary' in compact:
                    self.rows = database.diaries
                elif 'FROM long_memory' in compact:
                    self.rows = database.facts
                else:
                    self.rows = []

            def fetchall(self):
                return list(self.rows)

            def fetchone(self):
                return None

            def close(self):
                pass

        return Cursor()

    def commit(self):
        self.commits += 1
        raise AssertionError('recall must not commit')

    def close(self):
        self.closed += 1


class DiaryRecallTests(unittest.TestCase):
    def setUp(self):
        self.database = RecallDatabase()
        for module in (memory_lifecycle, smart_recall, sys.modules['db']):
            patcher = patch.object(module, 'get_conn', return_value=self.database)
            patcher.start()
            self.addCleanup(patcher.stop)

    def build_chat_blocks(self, message):
        # Execute the production prompt builder/recall; stub only external I/O.
        stubs = {
            'characters': {
                'get_character': Mock(return_value={'core_prompt': '角色设定'}),
                'retrieve_character_memory': Mock(return_value=[]),
            },
            'characters_data._loader': {
                'load_canon_lock': Mock(return_value=''),
                'load_core': Mock(return_value=None),
            },
            'user_memory': {
                'get_long_memory': Mock(return_value=[]),
                'get_recent_openings': Mock(return_value=[]),
                'get_last_assistant_reply': Mock(return_value=''),
                'get_bond_memories': Mock(return_value=[]),
                'get_first_interaction_days': Mock(return_value=20),
            },
            'route_period': {'get_period_context': Mock(return_value='')},
            'memory_search': {'is_vector_ready': Mock(return_value=False)},
            'relationship_reader': {'build_state_summary': Mock(return_value='当前关系账本')},
            'temporal_awareness': {
                'build_prompt_context': Mock(return_value=''),
                'build_calendar_grounding': Mock(return_value=''),
            },
            'db_schedule': {'get_current_activity': Mock(return_value=None)},
            'diary_engine': {'build_diary_hint': Mock(return_value='')},
        }
        modules = {}
        for name, attributes in stubs.items():
            module = types.ModuleType(name)
            module.__dict__.update(attributes)
            modules[name] = module
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, modules))
            spec = importlib.util.spec_from_file_location(
                'diary_prompt_under_test', os.path.join(BACKEND, 'prompt.py'))
            prompt = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(prompt)
            stack.enter_context(patch.object(prompt, '_accounts_block', return_value=''))
            rules = stack.enter_context(patch.object(
                prompt, 'build_relation_rules', wraps=shared_relation_prompt.build_relation_rules))
            blocks = prompt.build_system_blocks('u1', 'gojo', user_message=message)
            return '\n'.join(block['text'] for block in blocks), rules.call_args

    def test_diaries_reach_actual_chat_prompt_with_entry_provenance(self):
        text, _ = self.build_chat_blocks('考试那天，你怎么想？')
        for expected in (DIARY_TEXT, REFLECTION_TEXT, 'source_type=diary',
                         'source_type=reflection', 'char_diary:3',
                         'cognitive_diary_entries:7', 'cognitive_event'):
            self.assertIn(expected, text)
        self.assertEqual(text.count(DIARY_TEXT), 1)
        self.assertEqual(text.count(REFLECTION_TEXT), 1)
        result = smart_recall.two_level_recall('u1', 'gojo', '考试')
        reflection = next(item for item in result['diary_memories']
                          if item['source_type'] == 'reflection')
        self.assertEqual(reflection['source_ref']['source_id'], 7)
        self.assertEqual(reflection['source_event_refs'][0]['source_id'], 27)

    def test_irrelevant_and_empty_queries_inject_no_diary(self):
        text, _ = self.build_chat_blocks('考试')
        self.assertNotIn(UNRELATED_TEXT, text)
        for query in ('', '你好', '你当时怎么想的？', '今天的天气怎么样？'):
            with self.subTest(query=query):
                text, _ = self.build_chat_blocks(query)
                self.assertNotIn(DIARY_TEXT, text)
                self.assertNotIn(REFLECTION_TEXT, text)
                self.assertNotIn(UNRELATED_TEXT, text)
                self.assertNotIn('正文引用：', text)

    def test_facts_remain_authoritative_when_diary_conflicts(self):
        text, rules = self.build_chat_blocks('考试已经通过了，当时没有在强撑。')
        self.assertIn(FACT_TEXT, text)
        self.assertIn(REFLECTION_TEXT, text)
        self.assertLess(text.index(FACT_TEXT), text.index(REFLECTION_TEXT))
        for constraint in ('过去的主观反思，不是客观事实证据',
                           '当前用户消息 / 当前直接事件 > 较新的明确事实',
                           '与当前事实冲突时，以当前直接证据为准',
                           '日记只代表当时的主观理解，不得覆盖事实',
                           '禁止 self-proof'):
            self.assertIn(constraint, text)
        self.assertEqual(rules.args[1:], (0, 1))

    def test_diary_only_recall_cannot_write_or_count_as_relationship_evidence(self):
        self.database.facts = []
        for _ in range(2):
            text, rules = self.build_chat_blocks('考试')
            self.assertIn(DIARY_TEXT, text)
            self.assertEqual(rules.args[1:], (0, 0))
        self.assertEqual(self.database.commits, 0)
        self.assertTrue(all(sql.startswith('SELECT') for sql, _ in self.database.executed))
        self.assertFalse(any('cognitive_events' in sql or 'rel_state' in sql
                             or 'relationship_model' in sql
                             for sql, _ in self.database.executed))

    def test_separate_budget_caps_both_sources_and_excerpt_size(self):
        self.database.diaries = [(i, DIARY_TEXT, '', NOW) for i in range(10)]
        items = memory_lifecycle.recall_diary_memories('u1', 'gojo', '考试', limit=100)
        self.assertEqual(len(items), 3)
        self.assertEqual(len(memory_lifecycle.recall_diary_memories(
            'u1', 'gojo', '考试', limit=1)), 1)
        self.assertEqual(memory_lifecycle.recall_diary_memories(
            'u1', 'gojo', '考试', limit=0), [])
        self.database.diaries = [(3, '开头。' * 500 + DIARY_TEXT + '后记。' * 500, '', NOW)]
        items = memory_lifecycle.recall_diary_memories('u1', 'gojo', '考试')
        diary = next(item for item in items if item['source_type'] == 'diary')
        self.assertLessEqual(len(diary['content']), COGNITIVE_DIARY_RECALL_ENTRY_CHARS)
        self.assertIn('考试', diary['content'])
        self.assertTrue(diary['excerpt_truncated'])
        self.assertEqual(diary['source_ref']['source_id'], 3)

    def test_query_filters_before_candidate_limit_and_scopes_owner(self):
        memory_lifecycle.recall_diary_memories('u1', 'gojo', '考试')
        for sql, params in self.database.executed:
            self.assertEqual(params[:2], ('u1', 'gojo'))
            self.assertIn('考试', params[2])
            self.assertLess(sql.index('strpos(lower(content)'), sql.index('LIMIT'))

    def test_diary_failure_preserves_other_memories(self):
        with patch.object(memory_lifecycle, 'recall_diary_memories',
                          side_effect=RuntimeError('diary unavailable')):
            text, _ = self.build_chat_blocks('考试')
        self.assertIn(FACT_TEXT, text)
        self.assertNotIn(DIARY_TEXT, text)

    def test_cognitive_reader_does_not_inject_unfiltered_diaries(self):
        text = cognitive_reader.build_cognitive_prompt_context(
            'u1', 'gojo', conn=self.database)
        self.assertEqual(text, '')
        self.assertFalse(any('FROM cognitive_diary_entries' in sql
                             for sql, _ in self.database.executed))

    def test_diary_entry_is_not_valid_evidence_for_hypothesis_update(self):
        import cognitive_output
        from tests.test_cognitive_slow_loop import valid_output

        output = valid_output()
        output['evidence_refs'][0]['event_id'] = 7
        output['hypothesis_updates'][0]['supporting_evidence_refs'] = [7]
        output['question_updates'][0]['evidence_refs'] = [7]
        output['new_predictions'][0]['evidence_refs'] = [7]
        output['reflection_note']['evidence_refs'] = [7]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.validate_slow_loop_output(output, allowed_event_ids=set())

    def test_latin_keyword_must_match_a_whole_word(self):
        self.database.diaries = [(3, 'I remember an example of kindness.', '', NOW)]
        self.database.reflections = []
        self.assertEqual(memory_lifecycle.recall_diary_memories(
            'u1', 'gojo', 'exam'), [])

    def test_overlapping_daily_diary_and_important_thought_are_collapsed(self):
        shared = '考试那天我还以为她在强撑，这只是当时的猜测。'
        self.database.diaries = [(3, shared, '担心', NOW)]
        self.database.reflections = [(
            7, 'exam.reflection', shared, 'event',
            [{'source_type': 'cognitive_event', 'source_id': 27}], NOW,
        )]
        items = memory_lifecycle.recall_diary_memories('u1', 'gojo', '考试')
        self.assertEqual(len(items), 1)

    def test_observer_prompt_excludes_recalled_diary_and_rejects_self_proof(self):
        client = types.ModuleType('ai_client')
        client.create_chat = Mock(return_value=('{"signals": []}', {}))
        with patch.dict(sys.modules, {'ai_client': client}):
            spec = importlib.util.spec_from_file_location(
                'diary_observer_under_test', os.path.join(BACKEND, 'relationship_signals.py'))
            observer = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(observer)
            observer.extract_signals('考试', '我只记得当时担心你。')
        sent = client.create_chat.call_args.kwargs
        self.assertIn('旧日记被再次提及不增加 hypothesis/confidence', sent['system'])
        self.assertIn('必须有本轮新的独立外部证据', sent['system'])
        self.assertNotIn(DIARY_TEXT, str(sent))
        self.assertNotIn(REFLECTION_TEXT, str(sent))


if __name__ == '__main__':
    unittest.main()
