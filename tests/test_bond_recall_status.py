# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import db_bond  # noqa: E402
import smart_recall  # noqa: E402
import user_memory  # noqa: E402


NOW = datetime(2026, 9, 18, 20, 0)
OLD_GACHA_BOND = (
    '我和她约好抽谷子，每池两发各选一个，我抽不中扣零花钱并穿女仆装，'
    '她抽不中拍我想吃的甜品照片给我看'
)
GACHA_FACT = '她2026-09-18抽谷子抽到了两个系列'
RAIN_BOND = '如果明天没下雨就出去'


class BondRecallStore:
    def __init__(self):
        self.facts = []
        self.bonds = []
        self.executed = []
        self.commits = 0
        self._many = []
        self._one = None
        self.rowcount = 0

    def cursor(self):
        return self

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def close(self):
        pass

    def fetchall(self):
        return list(self._many)

    def fetchone(self):
        return self._one

    def _active(self, row):
        status = row.get('recall_status') or 'active'
        if status != 'active':
            return False
        expires = row.get('expires_at')
        return expires is None or expires > NOW

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        params = tuple(params or ())
        self.executed.append((compact, params))
        self._many = []
        self._one = None
        self.rowcount = 0
        if compact.startswith('SELECT id, user_id, character_id'):
            wanted = params[0] if params else None
            for row in self.bonds:
                if row['id'] == wanted:
                    self._one = (
                        row['id'], row['user_id'], row['character_id'],
                        row['kind'], row['content'],
                        row.get('recall_status') or 'active',
                    )
                    return
            return
        if compact.startswith('SELECT id, content FROM long_memory'):
            self._many = [(row[0], row[1]) for row in self.facts]
            return
        if 'FROM long_memory' in compact:
            self._many = list(self.facts)
            return
        if compact.startswith('SELECT content FROM bond_memory'):
            user_id, character_id, kind = params[:3]
            self._many = [
                (row['content'],)
                for row in self.bonds
                if row['user_id'] == user_id
                and row['character_id'] == character_id
                and row['kind'] == kind
                and self._active(row)
            ]
            return
        if 'FROM memory_source_events' in compact:
            return
        if compact.startswith('UPDATE bond_memory') and 'recall_status' in compact:
            ids = params[0] if params else []
            if not isinstance(ids, (list, tuple)):
                ids = [ids]
            n = 0
            for row in self.bonds:
                if row['id'] in ids:
                    row['recall_status'] = 'superseded'
                    n += 1
            self.rowcount = n
            return
        if compact.startswith('INSERT INTO bond_memory'):
            new_id = max([row['id'] for row in self.bonds] or [0]) + 1
            content = params[3]
            self.bonds.append({
                'id': new_id,
                'user_id': params[0],
                'character_id': params[1],
                'kind': params[2],
                'content': content,
                'timestamp': NOW,
                'linked_fact_id': None,
                'recall_status': 'active',
                'expires_at': None,
            })
            self._one = (new_id,)
            return
        if 'FROM bond_memory' not in compact:
            return
        user_id, character_id = params[0], params[1]
        rows = [
            row for row in self.bonds
            if row['user_id'] == user_id and row['character_id'] == character_id
        ]
        if 'COALESCE(recall_status' in compact:
            rows = [row for row in rows if self._active(row)]
        if "kind = 'told'" in compact:
            rows = [row for row in rows if row['kind'] == 'told']
            self._many = [
                (row['id'], row['content'], row['timestamp']) for row in rows
            ]
            return
        rows = [row for row in rows if row['kind'] == 'between']
        if 'linked_fact_id IN' in compact:
            fact_ids = set(params[2:])
            rows = [
                row for row in rows
                if row.get('linked_fact_id') in fact_ids
            ]
            rows.sort(key=lambda item: item['timestamp'] or NOW, reverse=True)
            self._many = [
                (row['id'], row['content'], row['timestamp'], row.get('linked_fact_id'))
                for row in rows
            ]
            return
        if 'linked_fact_id IS NULL' in compact:
            rows = [
                row for row in rows
                if not row.get('linked_fact_id')
            ]
            rows.sort(key=lambda item: item['timestamp'] or NOW, reverse=True)
            limit = params[-1] if params else 12
            rows = rows[:int(limit)]
            self._many = [
                (row['id'], row['content'], row['timestamp']) for row in rows
            ]
            return
        self._many = [(row['id'], row['content']) for row in rows]


class BondRecallStatusTests(unittest.TestCase):
    def setUp(self):
        self.store = BondRecallStore()
        self.patchers = [
            patch.object(smart_recall, 'get_conn', return_value=self.store),
            patch.object(user_memory, 'get_conn', return_value=self.store),
            patch('memory_lifecycle.recall_lifecycle_memories', return_value=[]),
            patch('memory_lifecycle.recall_sticky_notes', return_value=[]),
            patch('memory_lifecycle.recall_diary_memories', return_value=[]),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)

    def _fact(self, fid=10, content=GACHA_FACT):
        return (fid, content, NOW, '经历', 1, None, False, 'long_fact', 1.0, [])

    def _bond(self, **kwargs):
        row = {
            'id': 1842,
            'user_id': 'u1',
            'character_id': 'gojo',
            'kind': 'between',
            'content': OLD_GACHA_BOND,
            'timestamp': NOW - timedelta(days=2),
            'linked_fact_id': None,
            'recall_status': 'active',
            'expires_at': None,
        }
        row.update(kwargs)
        return row

    def test_superseded_linked_bond_is_not_recalled(self):
        self.store.facts = [self._fact()]
        self.store.bonds = [self._bond(
            id=1842, linked_fact_id=10, recall_status='superseded')]
        result = smart_recall.two_level_recall('u1', 'gojo', '抽谷子抽到了')
        self.assertEqual(result['facts'][0]['content'], GACHA_FACT)
        self.assertEqual(result['facts'][0]['bonds'], [])

    def test_superseded_loose_bond_is_not_recalled(self):
        self.store.bonds = [self._bond(id=1842, recall_status='superseded')]
        result = smart_recall.two_level_recall('u1', 'gojo', '抽谷子抽到了')
        self.assertEqual(result['loose_bonds'], [])
        sql = '\n'.join(item[0] for item in self.store.executed)
        self.assertIn("COALESCE(recall_status, 'active') = 'active'", sql)

    def test_superseded_told_is_not_recalled(self):
        self.store.bonds = [self._bond(
            id=9, kind='told', content='她说过抽不中要拍甜品照片',
            recall_status='superseded')]
        result = smart_recall.two_level_recall('u1', 'gojo', '抽谷子')
        self.assertEqual(result['tolds'], [])

    def test_unrelated_loose_bonds_are_not_padded_to_k(self):
        for index in range(6):
            self.store.bonds.append(self._bond(
                id=100 + index,
                content=f'我和她约好下周去看海{index}号岛',
            ))
        result = smart_recall.two_level_recall('u1', 'gojo', '今天午餐吃什么')
        self.assertEqual(result['loose_bonds'], [])

    def test_expired_loose_bond_is_not_recalled(self):
        self.store.bonds = [self._bond(
            id=3, content='抽谷子还没抽',
            expires_at=NOW - timedelta(hours=1),
        )]
        result = smart_recall.two_level_recall('u1', 'gojo', '抽谷子')
        self.assertEqual(result['loose_bonds'], [])

    def test_resolved_gacha_bond_leaves_recall_and_prompt(self):
        self.store.facts = [self._fact()]
        self.store.bonds = [
            self._bond(id=1842, recall_status='superseded'),
            self._bond(
                id=20, content='我和她去吃过拉面',
                timestamp=NOW - timedelta(days=10),
            ),
        ]
        result = smart_recall.two_level_recall('u1', 'gojo', '抽谷子抽到了两个系列')
        loose_text = ' '.join(item['content'] for item in result['loose_bonds'])
        linked = [
            item['content']
            for fact in result['facts']
            for item in fact.get('bonds', [])
        ]
        self.assertIn(GACHA_FACT, [fact['content'] for fact in result['facts']])
        self.assertNotIn(OLD_GACHA_BOND, loose_text)
        self.assertNotIn(OLD_GACHA_BOND, linked)
        memory_text, bond_text, _told = smart_recall.format_recall_for_prompt(result)
        combined = memory_text + bond_text
        self.assertIn('记忆证据优先级', combined)
        self.assertIn('当前用户消息 / 当前直接事件', combined)
        self.assertIn(GACHA_FACT, combined)
        self.assertNotIn('甜品照片', combined)
        self.assertNotIn('女仆装', combined)
        self.assertIn('不得继续把该条件的未来结果当作 pending', combined)

    def test_bond_resolution_supersedes_old_row_without_delete(self):
        self.store.bonds = [self._bond(id=1842)]
        ok, rows = user_memory.resolve_bond_memories(
            'u1', 'gojo', 'between',
            [OLD_GACHA_BOND],
            new_content=None,
            reason='completed',
        )
        self.assertTrue(ok)
        self.assertEqual(rows[0][0], 1842)
        self.assertEqual(self.store.bonds[0]['recall_status'], 'superseded')
        self.assertEqual(self.store.bonds[0]['content'], OLD_GACHA_BOND)
        ids = [row['id'] for row in self.store.bonds]
        self.assertEqual(ids, [1842])

    def test_rain_plan_is_superseded_and_not_future_plan(self):
        self.store.bonds = [self._bond(id=7, content=RAIN_BOND)]
        ok, _rows = user_memory.resolve_bond_memories(
            'u1', 'gojo', 'between',
            [RAIN_BOND],
            reason='cancelled',
        )
        self.assertTrue(ok)
        result = smart_recall.two_level_recall('u1', 'gojo', '已经下雨了')
        self.assertEqual(result['loose_bonds'], [])
        memory_text, bond_text, _ = smart_recall.format_recall_for_prompt(result)
        self.assertNotIn('没下雨就出去', memory_text + bond_text)

    def test_resolution_skips_restating_pending_as_new_active_bond(self):
        self.store.bonds = [self._bond(id=1842)]
        user_memory.resolve_bond_memories(
            'u1', 'gojo', 'between',
            [OLD_GACHA_BOND],
            new_content=OLD_GACHA_BOND,
            reason='completed',
        )
        active = [
            row for row in self.store.bonds
            if (row.get('recall_status') or 'active') == 'active'
        ]
        self.assertEqual(active, [])

    def test_extractor_prompt_forbids_merge_for_outcomes(self):
        source = Path(BACKEND, 'user_memory.py').read_text(encoding='utf-8')
        self.assertIn('bond_resolution', source)
        self.assertIn('旧事件状态变化', source)
        self.assertIn('禁止用 bond_merge 把旧的"待发生条件"继续保留成 active', source)
        self.assertIn("recall_status = 'superseded'", source)
        self.assertNotIn('DELETE FROM bond_memory WHERE id = %s', source.split(
            'def resolve_bond_memories')[1].split('def get_bond_memories')[0])

    def test_startup_has_no_incident_hardcode(self):
        source = Path(BACKEND, 'db_bond.py').read_text(encoding='utf-8')
        server = Path(BACKEND, 'gojo_server.py').read_text(encoding='utf-8')
        blob = source + '\n' + server
        self.assertNotIn('1842', blob)
        self.assertNotIn('user_mofpiyd7442ia7', blob)
        self.assertNotIn('抽谷', blob)
        self.assertNotIn('same-theme active bond', blob)
        self.assertNotIn('retire_resolved', blob)
        self.assertIn(db_bond.BOND_EXPIRES_AT_DDL, source)
        self.assertIn('expires_at TIMESTAMPTZ', source)


class BondExtractorResolutionTests(unittest.TestCase):
    def test_extract_uses_bond_resolution_and_does_not_resave_pending(self):
        store = BondRecallStore()
        store.bonds = [{
            'id': 1842,
            'user_id': 'u1',
            'character_id': 'gojo',
            'kind': 'between',
            'content': OLD_GACHA_BOND,
            'timestamp': NOW,
            'linked_fact_id': None,
            'recall_status': 'active',
            'expires_at': None,
        }]
        payload = json.dumps({
            'user_fact': None,
            'bond': {'content': OLD_GACHA_BOND},
            'told': None,
            'character_self_claim': None,
            'bond_merge': None,
            'bond_resolution': {
                'replaces': [OLD_GACHA_BOND],
                'content': None,
                'reason': 'completed',
            },
        }, ensure_ascii=False)
        with patch.object(user_memory, 'plan_memory_corrections', return_value=[]), \
             patch.object(user_memory, 'get_long_memory', return_value=[]), \
             patch.object(user_memory, 'get_bond_memories',
                          return_value=[(1842, OLD_GACHA_BOND, NOW)]), \
             patch.object(user_memory, '_all_character_names', return_value=[]), \
             patch.object(user_memory, 'get_relations_text', return_value=''), \
             patch.object(user_memory, 'get_conn', return_value=store), \
             patch('ai_client.create_chat', return_value=(payload, None)), \
             patch('characters.get_character', return_value={'name': '五条'}), \
             patch('memory_lifecycle.reactivate_lifecycle_memories', return_value=0), \
             patch('smart_recall.reinforce_mentioned_facts'):
            ok = user_memory.extract_and_save_memory(
                'u1', '我抽到了两个系列', '抽到了啊', 'gojo')
        self.assertTrue(ok)
        self.assertEqual(store.bonds[0]['recall_status'], 'superseded')
        active = [
            row for row in store.bonds
            if (row.get('recall_status') or 'active') == 'active'
        ]
        self.assertEqual(active, [])


OLD_BOND_COLUMNS = {
    'id', 'user_id', 'character_id', 'kind', 'content', 'timestamp',
}


class BondSchemaStore:
    """Legacy bond_memory table without expires_at."""

    def __init__(self):
        self.columns = set(OLD_BOND_COLUMNS)
        self.executed = []
        self.commits = 0
        self._many = []

    def cursor(self):
        return self

    def commit(self):
        self.commits += 1

    def close(self):
        pass

    def fetchall(self):
        return list(self._many)

    def fetchone(self):
        return None

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append(compact)
        self._many = []
        if compact.startswith('CREATE TABLE IF NOT EXISTS bond_memory'):
            self.columns.update(OLD_BOND_COLUMNS)
            return
        if 'ALTER TABLE bond_memory ADD COLUMN IF NOT EXISTS' in compact:
            rest = compact.split('ADD COLUMN IF NOT EXISTS', 1)[1].strip()
            name = rest.split()[0]
            self.columns.add(name)
            return
        if 'FROM bond_memory' in compact and 'expires_at' in compact:
            if 'expires_at' not in self.columns:
                raise RuntimeError('column "expires_at" does not exist')
            return
        if compact.startswith('SELECT') and 'expires_at' in compact and (
                'FROM bond_memory' in compact):
            if 'expires_at' not in self.columns:
                raise RuntimeError('column "expires_at" does not exist')


class BondExpiresAtMigrationTests(unittest.TestCase):
    def test_init_adds_expires_at_timestamptz_to_legacy_schema(self):
        store = BondSchemaStore()
        self.assertNotIn('expires_at', store.columns)
        with patch.object(db_bond, 'get_conn', return_value=store):
            db_bond.init_bond_table()
        self.assertIn('expires_at', store.columns)
        sql = '\n'.join(store.executed)
        self.assertIn(
            'ALTER TABLE bond_memory ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ',
            sql,
        )
        self.assertNotIn('expires_at TIMESTAMP', sql.replace('TIMESTAMPTZ', ''))

    def test_recall_expires_at_filter_does_not_fail_after_init(self):
        store = BondSchemaStore()
        recall_sql = (
            "SELECT id, content, timestamp FROM bond_memory "
            "WHERE user_id = %s AND character_id = %s AND kind = 'between' "
            f"AND {smart_recall.ACTIVE_BOND_SQL}"
        )
        with self.assertRaisesRegex(RuntimeError, 'expires_at'):
            store.execute(recall_sql, ('u1', 'gojo'))

        with patch.object(db_bond, 'get_conn', return_value=store):
            db_bond.init_bond_table()

        store.execute(recall_sql, ('u1', 'gojo'))

        with patch.object(smart_recall, 'get_conn', return_value=store), \
             patch('memory_lifecycle.recall_lifecycle_memories', return_value=[]), \
             patch('memory_lifecycle.recall_sticky_notes', return_value=[]), \
             patch('memory_lifecycle.recall_diary_memories', return_value=[]):
            after = smart_recall.two_level_recall('u1', 'gojo', 'hello')
        self.assertIsNotNone(after)
        self.assertEqual(after['loose_bonds'], [])
        self.assertEqual(after['tolds'], [])
        bond_sql = [
            item for item in store.executed
            if 'FROM bond_memory' in item and 'expires_at' in item
        ]
        self.assertTrue(bond_sql)


if __name__ == '__main__':
    unittest.main()
