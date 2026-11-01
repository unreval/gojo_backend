"""Legacy recollection bypass: labeled, non-factual, and outside the authority gate."""
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_cognitive_deterministic as acceptance
import user_memory
from memory_authority import AUTHORITY, filter_recall_authority


ROOT = Path(__file__).resolve().parents[1]


def _legacy_row(row_id, content, *, day=12, score=0.2):
    return {
        'id': row_id,
        'content': content,
        'timestamp': datetime(2026, 8, day, 12, 0),
        'category': '喜好',
        'score': score,
        'authority': 'generated_recollection',
    }


class LegacyRecollectionScoreTests(unittest.TestCase):
    def test_score_omits_mention_strength(self):
        import smart_recall
        now = 1_700_000_000.0
        timestamp = datetime.fromtimestamp(now - 10 * 86400)
        content = '海边'
        message = '海边'
        score = smart_recall._score_legacy_recollection(
            content, '喜好', timestamp, message, now)
        expected = (
            smart_recall._relevance_score(content, message)
            * smart_recall._category_weight('喜好')
            * smart_recall._time_decay(timestamp, now)
        )
        self.assertEqual(score, expected)
        self.assertGreater(score, 0)
        self.assertNotEqual(
            score, expected * smart_recall._mention_strength(50))


class LegacyRecollectionPromptTests(unittest.TestCase):
    def test_default_format_omits_fuzzy_block(self):
        from smart_recall import format_recall_for_prompt
        memory_text, bond_text, told_text = format_recall_for_prompt({
            'legacy_recollections': [_legacy_row(7, '八月在旧书店遇见')],
        })
        self.assertEqual((bond_text, told_text), ('', ''))
        self.assertNotIn('模糊印象', memory_text)
        self.assertNotIn('旧书店', memory_text)
        self.assertNotIn('不能说成"你说过"', memory_text)

    def test_include_legacy_locks_nonfactual_wording(self):
        from smart_recall import format_recall_for_prompt
        memory_text, bond_text, told_text = format_recall_for_prompt({
            'legacy_recollections': [_legacy_row(7, '八月在旧书店遇见', day=2)],
        }, include_legacy=True)
        self.assertIn('很久以前的模糊印象', memory_text)
        self.assertIn('不能说成"你说过"', memory_text)
        self.assertIn('[2026-08-02] 八月在旧书店遇见', memory_text)
        joined = f'{memory_text}{bond_text}{told_text}'.strip() or '（还没有关于她的记忆）'
        self.assertNotIn('还没有关于她的记忆', joined)
        self.assertEqual(len((memory_text, bond_text, told_text)), 3)

    def test_chat_paths_opt_in_and_diary_does_not(self):
        prompt = (ROOT / 'gojo_backend' / 'prompt.py').read_text(encoding='utf-8')
        layer = (ROOT / 'gojo_backend' / 'context_layer.py').read_text(encoding='utf-8')
        diary = (ROOT / 'gojo_backend' / 'diary_engine.py').read_text(encoding='utf-8')
        self.assertEqual(prompt.count('include_legacy=True'), 2)
        self.assertEqual(layer.count('include_legacy=True'), 1)
        self.assertNotIn('include_legacy', diary)
        self.assertIn('format_recall_for_prompt(verified)', diary)


class LegacyRecollectionBudgetTests(unittest.TestCase):
    def test_candidate_collapse_preserves_legacy_rows(self):
        from recall_candidates import (
            collapse_candidates, from_recall_result, to_recall_result,
        )
        legacy = [_legacy_row(9, '八月在旧书店遇见')]
        original = {'facts': [], 'loose_bonds': [], 'tolds': [],
                    'legacy_recollections': legacy}
        restored = to_recall_result(
            collapse_candidates(from_recall_result(original)), original)
        self.assertEqual(restored['legacy_recollections'], legacy)
        self.assertEqual(restored['facts'], [])


    def test_default_budget_keeps_three_legacy_rows(self):
        from context_budget import BudgetConfig, ContextBudgetManager
        from context_layer import _items_from_recall, _recall_result_from_items

        legacy = [
            _legacy_row(100 + index, f'八月印象{index}：她提过想去海边看一次日落', day=10 + index)
            for index in range(3)
        ]
        facts = [
            {
                'id': index + 1,
                'content': f'她喜欢咖啡{index}',
                'timestamp': datetime(2026, 10, 1, 12, 0),
                'category': '喜好',
                'score': 1.0,
                'authority': AUTHORITY,
                'bonds': [],
            }
            for index in range(4)
        ]
        result = {
            'facts': facts,
            'loose_bonds': [],
            'tolds': [],
            'legacy_recollections': legacy,
        }
        items = _items_from_recall(result)
        legacy_items = [item for item in items if item.item_type == 'legacy_recollection']
        self.assertEqual(len(legacy_items), 3)
        self.assertEqual({item.channel for item in legacy_items}, {'diary'})
        allocated = ContextBudgetManager(BudgetConfig.for_profile('default')).split(items)
        recall_kept = list(allocated.get('recall') or []) + list(allocated.get('diary') or [])
        trimmed = _recall_result_from_items(recall_kept, result)
        kept_ids = [row['id'] for row in trimmed['legacy_recollections']]
        self.assertEqual(
            kept_ids,
            [row['id'] for row in legacy],
            'diary_kept=%s recall_channel=%s survived=%s'
            % (
                len(allocated.get('diary') or []),
                len(allocated.get('recall') or []),
                kept_ids,
            ),
        )


class LegacyRecollectionSqlTests(unittest.TestCase):
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
        self.stack.enter_context(patch('smart_recall.get_conn', return_value=self.database))
        self.stack.enter_context(patch('user_memory.get_conn', return_value=self.database))
        self.stack.enter_context(patch('user_memory._bg_embed'))

    def insert_legacy(self, content, *, category='喜好', timestamp=None,
                      mention_count=1, recall_status='active', expires_at=None):
        if timestamp is None:
            timestamp = datetime(2026, 8, 12, 12, 0)
        row = self.sql(
            '''INSERT INTO long_memory
               (user_id, character_id, content, category, timestamp,
                mention_count, recall_status, expires_at, authority)
               VALUES ('u','c',%s,%s,%s,%s,%s,%s,'generated_recollection')
               RETURNING id''',
            (content, category, timestamp, mention_count, recall_status, expires_at),
        )
        self.database.commit()
        return row[0][0]

    def recall(self, message):
        import smart_recall
        with patch('memory_lifecycle.recall_lifecycle_memories', return_value=[]), \
                patch('memory_lifecycle.recall_sticky_notes', return_value=[]), \
                patch('memory_lifecycle.recall_diary_memories', return_value=[]):
            result = smart_recall.two_level_recall('u', 'c', message)
        self.assertIsNotNone(result, 'two_level_recall failed closed')
        return result

    def test_generated_recollection_is_not_a_fact(self):
        legacy_id = self.insert_legacy('八月在旧书店遇见')
        self.insert_legacy('已归档的旧印象', recall_status='archived')
        self.insert_legacy('已经过期的旧印象', expires_at=datetime(2026, 1, 1, 0, 0))
        before = self.sql(
            '''SELECT id, authority, projection_version, semantic_payload
               FROM long_memory ORDER BY id''')
        result = self.recall('旧书店')
        legacy = result['legacy_recollections']
        self.assertEqual([row['content'] for row in legacy], ['八月在旧书店遇见'])
        self.assertEqual(legacy[0]['id'], legacy_id)
        self.assertEqual(legacy[0]['authority'], 'generated_recollection')
        self.assertNotIn(legacy_id, [row['id'] for row in result['facts']])
        self.assertNotIn('旧书店', ' '.join(row['content'] for row in result['facts']))
        for key in ('loose_bonds', 'tolds'):
            blob = ' '.join(row.get('content', '') for row in result[key])
            self.assertNotIn('旧书店', blob)
            self.assertNotIn('已归档', blob)
        self.assertEqual(self.sql(
            '''SELECT id, authority, projection_version, semantic_payload
               FROM long_memory ORDER BY id'''), before)

    def test_canonical_fact_is_not_legacy(self):
        self.report('coffee', '我喜欢咖啡。')
        result = self.recall('咖啡')
        self.assertTrue(any('咖啡' in row['content'] for row in result['facts']))
        self.assertEqual(result['legacy_recollections'], [])
        self.assertTrue(all(row.get('authority') == AUTHORITY for row in result['facts']))

    def test_at_most_three_legacy_rows_ignore_mention_count(self):
        inserted = []
        for day, mentions in ((1, 999), (5, 1), (10, 1), (15, 1), (20, 1)):
            inserted.append(self.insert_legacy(
                '海边',
                timestamp=datetime(2026, 8, day, 12, 0),
                mention_count=mentions,
            ))
        result = self.recall('海边')
        kept = [row['id'] for row in result['legacy_recollections']]
        self.assertEqual(len(kept), 3)
        self.assertNotIn(inserted[0], kept)
        self.assertEqual(kept, [inserted[4], inserted[3], inserted[2]])

    def test_status_category_stays_out(self):
        self.insert_legacy('她今天很累', category='状态')
        kept_id = self.insert_legacy('八月在旧书店遇见', category='喜好')
        result = self.recall('她今天很累')
        contents = [row['content'] for row in result['legacy_recollections']]
        self.assertNotIn('她今天很累', contents)
        self.assertEqual(contents, ['八月在旧书店遇见'])
        self.assertEqual(result['legacy_recollections'][0]['id'], kept_id)
        self.assertNotIn('她今天很累', ' '.join(row['content'] for row in result['facts']))

    def test_filter_keeps_legacy_and_drops_forged_fact(self):
        self.report('coffee', '我喜欢咖啡。')
        legacy_id = self.insert_legacy('八月在旧书店遇见')
        result = self.recall('咖啡')
        result['facts'].append({
            'id': legacy_id,
            'content': '伪造：她亲口确认过旧书店',
            'authority': AUTHORITY,
            'timestamp': datetime(2026, 8, 12, 12, 0),
            'category': '其他',
        })
        checked = filter_recall_authority(result, 'u', 'c')
        self.assertTrue(any('咖啡' in row['content'] for row in checked['facts']))
        self.assertFalse(any('伪造' in row.get('content', '') for row in checked['facts']))
        self.assertFalse(any(row.get('id') == legacy_id for row in checked['facts']))
        self.assertEqual(
            [row['content'] for row in checked['legacy_recollections']],
            ['八月在旧书店遇见'],
        )
        self.assertEqual(checked['legacy_recollections'][0]['authority'], 'generated_recollection')

    def test_told_path_excludes_legacy_rows(self):
        self.report('coffee', '我喜欢咖啡。')
        legacy_id = self.insert_legacy('八月在旧书店遇见')
        tolds = user_memory.get_bond_memories('u', 'c', kind='told')
        told_ids = [row[0] for row in tolds]
        told_text = ' '.join(row[1] for row in tolds)
        self.assertTrue(any('咖啡' in text for _, text, _ in tolds))
        self.assertNotIn('旧书店', told_text)
        self.assertNotIn(legacy_id, told_ids)
        self.assertNotIn(-legacy_id, told_ids)
        recalled = self.recall('旧书店')
        self.assertNotIn(
            '旧书店', ' '.join(row.get('content', '') for row in recalled['tolds']))
