import inspect
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

fake_db = types.ModuleType('db')
fake_db.get_conn = lambda: (_ for _ in ()).throw(
    AssertionError('database should be patched by tests that need it')
)
sys.modules.setdefault('db', fake_db)

import memory_lifecycle  # noqa: E402
from memory_lifecycle import (  # noqa: E402
    build_consolidated_summary,
    classify_memory_lifecycle,
    detect_reactivation_query,
    recall_diary_memories,
    should_consolidate,
)
from smart_recall import format_recall_for_prompt  # noqa: E402


class DiaryCursor:
    def __init__(self):
        self.rows = []
        self.executed = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        if 'FROM cognitive_diary_entries' in compact:
            self.rows = [(
                7,
                'uncertainty.like',
                '我当时觉得自己可能有点在意她，但这只是我的反思。',
                'uncertainty',
                [{'source_type': 'cognitive_event', 'source_id': 27}],
                datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
            )]
        elif 'FROM char_diary' in compact:
            self.rows = [(
                3,
                '今天想起她说明天考试，感觉自己有点记挂。',
                '平静',
                datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc),
            )]
        else:
            self.rows = []

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class DiaryConnection:
    def __init__(self):
        self.cursor_obj = DiaryCursor()

    def cursor(self):
        return self.cursor_obj

    def close(self):
        pass


class MemoryLifecycleTests(unittest.TestCase):
    def test_shower_is_ephemeral_not_durable_long_memory(self):
        cls = classify_memory_lifecycle('我去洗澡了', '她去洗澡了', '状态')

        self.assertEqual(cls['memory_kind'], 'ephemeral')
        self.assertFalse(cls['should_save_long_memory'])
        self.assertEqual(cls['reason'], 'routine_or_current_state')

    def test_sleeping_is_current_state_not_durable_long_memory(self):
        cls = classify_memory_lifecycle('我要睡觉了', '她要睡觉了', '状态')

        self.assertEqual(cls['memory_kind'], 'ephemeral')
        self.assertFalse(cls['should_save_long_memory'])
        self.assertEqual(cls['topic_key'], 'routine.sleep')

    def test_repeated_sleep_issues_consolidate_to_one_summary(self):
        first = classify_memory_lifecycle('今天没睡好', '她今天没睡好', '状态')
        recurring = classify_memory_lifecycle('最近还是睡不好', '她最近还是睡不好', '状态')
        summary = build_consolidated_summary(
            'sleep',
            ['她今天没睡好', '她昨天也没睡好', '她最近还是睡不好'],
        )

        self.assertEqual(first['memory_kind'], 'candidate')
        self.assertEqual(recurring['memory_kind'], 'candidate')
        self.assertTrue(recurring['has_recurrence'])
        self.assertTrue(should_consolidate(3))
        self.assertTrue(should_consolidate(2, has_recurrence=True))
        self.assertEqual(summary['memory_kind'], 'consolidated')
        self.assertIn('反复睡眠不好', summary['content'])
        self.assertNotIn('她今天没睡好；她昨天也没睡好；她最近还是睡不好', summary['content'])

    def test_tomorrow_exam_becomes_temporary_sticky_note(self):
        cls = classify_memory_lifecycle('我明天考试', '她明天考试', '状态')

        self.assertEqual(cls['memory_kind'], 'sticky')
        self.assertTrue(cls['should_write_sticky'])
        self.assertFalse(cls['should_save_long_memory'])
        self.assertEqual(cls['topic_key'], 'goal.exam')
        self.assertGreater(cls['ttl_seconds'], 0)

    def test_falling_in_shower_is_not_dropped_by_routine_filter(self):
        cls = classify_memory_lifecycle('我洗澡的时候摔倒了', '她洗澡时摔倒了', '经历')

        self.assertEqual(cls['memory_kind'], 'episodic')
        self.assertFalse(cls['should_save_long_memory'])
        self.assertEqual(cls['reason'], 'salient_event_not_routine')

    def test_old_memory_reactivation_requires_new_mention(self):
        self.assertEqual(detect_reactivation_query('我又想起之前失眠那阵子'), 'sleep')
        self.assertIsNone(detect_reactivation_query('今天天气还行'))

    def test_diary_recall_returns_source_types_without_relationship_evidence(self):
        original_get_conn = memory_lifecycle.get_conn
        conn = DiaryConnection()
        memory_lifecycle.get_conn = lambda: conn
        try:
            recalled = recall_diary_memories('u1', 'gojo', '考试', limit=5)
        finally:
            memory_lifecycle.get_conn = original_get_conn

        source_types = {item['source_type'] for item in recalled}
        self.assertIn('reflection', source_types)
        self.assertIn('diary', source_types)
        self.assertTrue(all(item.get('source_event_refs') for item in recalled))

    def test_diary_only_content_is_not_relationship_engine_input(self):
        relationship_source = Path(BACKEND, 'relationship_engine.py').read_text(
            encoding='utf-8')

        self.assertNotIn('cognitive_diary_entries', relationship_source)
        self.assertNotIn('char_diary', relationship_source)

    def test_consolidation_summary_keeps_provenance_shape(self):
        source_refs = [
            {'source_type': 'memory_job', 'source_id': 1},
            {'source_type': 'memory_job', 'source_id': 2},
            {'source_type': 'memory_job', 'source_id': 3},
        ]
        lifecycle_item = {
            'content': build_consolidated_summary('sleep', [])['content'],
            'source_event_refs': source_refs,
            'memory_kind': 'consolidated',
        }

        self.assertEqual(lifecycle_item['memory_kind'], 'consolidated')
        self.assertEqual(len(lifecycle_item['source_event_refs']), 3)
        self.assertTrue(all(ref['source_type'] == 'memory_job' for ref in source_refs))

    def test_fast_loop_lifecycle_code_does_not_call_llm(self):
        source = inspect.getsource(memory_lifecycle)

        forbidden = (
            'create_chat(',
            'Anthropic(',
            'claude_client',
            'MODEL_CN_AUX',
            'MODEL_MAIN',
            'messages.create',
        )
        for token in forbidden:
            self.assertNotIn(token, source)

    def test_prompt_formats_lifecycle_and_sticky_as_non_permanent(self):
        memory_text, bond_text, told_text = format_recall_for_prompt({
            'facts': [],
            'loose_bonds': [],
            'tolds': [],
            'lifecycle_memories': [{
                'memory_kind': 'ephemeral',
                'content': '她去洗澡了',
                'updated_at': datetime(2026, 9, 14, tzinfo=timezone.utc),
            }],
            'sticky_notes': [{
                'content': '临近考试：她明天考试。到期后不要当作长期事实。',
                'expires_at': datetime(2026, 9, 15, 23, 59, tzinfo=timezone.utc),
            }],
        })

        self.assertIn('有生命周期，不等于永久事实', memory_text)
        self.assertIn('便利贴备忘', memory_text)
        self.assertIn('不是长期记忆', memory_text)
        self.assertEqual(bond_text, '')
        self.assertEqual(told_text, '')


if __name__ == '__main__':
    unittest.main()
