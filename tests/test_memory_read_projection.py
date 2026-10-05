"""Memory page and chat prompt must show the same role-view projection."""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime
from unittest.mock import patch


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import route_memory  # noqa: E402
import smart_recall  # noqa: E402
from memory_authority import AUTHORITY  # noqa: E402
from role_view import PROJECTION_VERSION  # noqa: E402


class MemoryStore:
    def __init__(self, facts=(), bonds=()):
        self.facts = list(facts)
        self.bonds = list(bonds)
        self.statements = []
        self.rows = []
        self.category = None

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.statements.append((' '.join(sql.split()), params))
        if 'FROM long_memory' in sql and sql.lstrip().startswith('SELECT id, content, category'):
            self.rows = self.facts
        elif 'FROM bond_memory' in sql and sql.lstrip().startswith('SELECT id, content, timestamp'):
            self.rows = self.bonds
        elif sql.startswith('UPDATE long_memory SET category'):
            self.category = params[0]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return ('用户喜欢猫', AUTHORITY)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class MemoryReadProjectionTests(unittest.TestCase):
    def test_page_and_chat_use_same_v2_projection_while_legacy_stays_history(self):
        semantic = {
            'subject_ref': 'user:u', 'observer_ref': 'gojo',
            'source_scope': 'shared', 'predicate': 'reported_preference',
            'object': '猫', 'value': '喜欢',
        }
        now = datetime(2026, 10, 5)
        store = MemoryStore(facts=[
            (1, '用户喜欢旧文案', '喜好', now, True, True,
             'shared', semantic, PROJECTION_VERSION),
            (2, '用户明确自述：喜欢狗', '喜好', now, False, False,
             'gojo', None, 'legacy_v1'),
            (3, '她喜欢鸟', '喜好', now, False, False,
             'gojo', None, 'legacy_v1'),
        ])
        with patch.object(route_memory, 'get_conn', return_value=store):
            response = asyncio.run(route_memory.list_long_memory('u', 'gojo'))
        page_rows = json.loads(response.body)['memories']
        self.assertEqual(page_rows[0]['content'], '她喜欢猫。')
        self.assertEqual(page_rows[1]['content'], '她明确说过：喜欢狗。')
        self.assertEqual(page_rows[2]['content'], '她喜欢鸟')
        self.assertEqual([row['recallable'] for row in page_rows], [True, False, False])
        self.assertEqual(store.facts[0][1], '用户喜欢旧文案')

        prompt_text, _, _ = smart_recall.format_recall_for_prompt({
            'observer_id': 'gojo',
            'facts': [{'id': 1, 'content': store.facts[0][1],
                       'timestamp': now, 'category': '喜好',
                       'authority': AUTHORITY, 'semantic_payload': semantic,
                       'projection_version': PROJECTION_VERSION,
                       'source_character_id': 'shared'}],
        })
        self.assertIn(page_rows[0]['content'], prompt_text)
        self.assertNotIn('用户喜欢旧文案', prompt_text)
        self.assertNotIn('喜欢狗', prompt_text)

    def test_category_only_update_leaves_locked_content_untouched(self):
        store = MemoryStore()
        with patch.object(route_memory, 'get_conn', return_value=store):
            response = asyncio.run(route_memory.update_long_memory(
                1, {'category': '身份'}))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(store.category, '身份')
        self.assertFalse(any('SET content' in sql for sql, _ in store.statements))

    def test_bond_page_and_chat_render_character_self_without_promoting_history(self):
        semantic = {
            'subject_ref': 'character:gojo', 'observer_ref': 'gojo',
            'source_scope': 'gojo', 'predicate': 'explicit_promise',
            'object': '回答她', 'deadline': '明天', 'value': '承诺',
        }
        now = datetime(2026, 10, 5)
        store = MemoryStore(bonds=[
            (7, '角色答应用户', now, True, True, semantic, PROJECTION_VERSION),
            (8, '用户表示有旧约定', now, False, False, None, 'legacy_v1'),
        ])
        with patch.object(route_memory, 'get_conn', return_value=store):
            response = asyncio.run(route_memory.list_bond_memory(
                'u', 'gojo', kind='between', include_history=True))
        page_rows = json.loads(response.body)['memories']
        self.assertEqual(page_rows[0]['content'], '我承诺在明天前完成「回答她」。')
        self.assertEqual(page_rows[1]['content'], '她说有旧约定')
        self.assertEqual([row['recallable'] for row in page_rows], [True, False])

        _, bond_text, _ = smart_recall.format_recall_for_prompt({
            'observer_id': 'gojo', 'loose_bonds': [
                {'id': 7, 'content': store.bonds[0][1], 'timestamp': now,
                 'authority': AUTHORITY, 'semantic_payload': semantic,
                 'projection_version': PROJECTION_VERSION,
                 'source_character_id': 'gojo'}],
        })
        self.assertIn(page_rows[0]['content'], bond_text)
        self.assertNotIn('角色答应用户', bond_text)


if __name__ == '__main__':
    unittest.main()
