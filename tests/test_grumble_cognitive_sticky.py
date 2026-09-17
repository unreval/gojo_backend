import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from cognitive_config import USER_FACING_STICKY_SOURCE  # noqa: E402
import cognitive_reader  # noqa: E402


NOW = datetime(2026, 9, 18, 4, 0, tzinfo=timezone.utc)
PRODUCTION_SKIP = {
    'grumble_engine.py',
    'db_grumble.py',
}
MEMORY_LIFECYCLE_SOURCE = 'memory_lifecycle_fast_loop'


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


def load_source(name, modules):
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            name + '_under_test', os.path.join(BACKEND, name + '.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def production_python_files():
    return [
        path for path in Path(BACKEND).glob('*.py')
        if path.name not in PRODUCTION_SKIP
    ]


def make_row(*, note_id=1, character_id='gojo', note_key='thread.visa',
             content='签证那件事我还挂着。', status='active',
             source=USER_FACING_STICKY_SOURCE, source_event_refs=None,
             created_by_cycle_id=90, updated_by_cycle_id=90,
             expires_at=None, completed_at=None, created_at=None,
             updated_at=None, viewed=False, viewed_at=None,
             user_hidden_at=None, user_visible=True):
    return (
        note_id, character_id, note_key, content, status, source,
        source_event_refs if source_event_refs is not None else [
            {'event_id': 27, 'reason': 'current concern'},
        ],
        created_by_cycle_id, updated_by_cycle_id, expires_at, completed_at,
        created_at or NOW, updated_at or NOW, viewed, viewed_at,
        user_hidden_at, user_visible,
    )


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.one = None
        self.many = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.store.executed.append((compact, tuple(params or ())))
        self.one = None
        self.many = []
        self.rowcount = 0
        if compact.startswith('SELECT id, character_id'):
            self.many = list(self.store.select_rows)
        elif compact.startswith('SELECT COUNT(*)'):
            self.one = (self.store.count,)
        elif compact.startswith('UPDATE') and 'viewed = TRUE' in compact:
            self.rowcount = self.store.marked
        elif compact.startswith('UPDATE') and 'user_hidden_at = CURRENT_TIMESTAMP' in compact:
            self.one = self.store.hide_returning
            self.rowcount = 1 if self.store.hide_returning else 0
        elif compact.startswith('UPDATE') and "status = 'completed'" in compact:
            self.one = self.store.complete_returning
            self.rowcount = 1 if self.store.complete_returning else 0

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class FakeConn:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0
        self.select_rows = []
        self.count = 0
        self.marked = 0
        self.hide_returning = (11, 'active')
        self.complete_returning = (11,)
        self._cursor = FakeCursor(self)

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class LegacyGrumbleRetiredTests(unittest.TestCase):
    def test_maybe_write_grumble_has_zero_production_callers(self):
        hits = []
        for path in production_python_files():
            text = path.read_text(encoding='utf-8')
            if 'maybe_write_grumble' in text:
                hits.append(path.name)
        self.assertEqual(hits, [])

    def test_production_code_does_not_read_or_write_char_grumble(self):
        hits = []
        for path in production_python_files():
            text = path.read_text(encoding='utf-8')
            if 'char_grumble' in text or 'db_grumble' in text or 'init_grumble_table' in text:
                hits.append(path.name)
        self.assertEqual(hits, [])

    def test_legacy_engine_files_are_gone(self):
        self.assertFalse(Path(BACKEND, 'grumble_engine.py').exists())
        self.assertFalse(Path(BACKEND, 'db_grumble.py').exists())

    def test_route_chat_no_longer_starts_grumble_thread(self):
        src = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        self.assertNotIn('grumble_engine', src)
        self.assertNotIn('maybe_write_grumble', src)

    def test_voice_stream_does_not_call_legacy_grumble(self):
        src = Path(BACKEND, 'route_voice_stream.py').read_text(encoding='utf-8')
        self.assertNotIn('grumble_engine', src)
        self.assertNotIn('maybe_write_grumble', src)

    def test_gojo_server_does_not_init_legacy_grumble_table(self):
        src = Path(BACKEND, 'gojo_server.py').read_text(encoding='utf-8')
        self.assertNotIn('init_grumble_table', src)
        self.assertNotIn('db_grumble', src)
        self.assertIn("'grumble_engine': False", src)

    def test_route_grumble_reads_cognitive_sticky_notes_not_char_grumble(self):
        src = Path(BACKEND, 'route_grumble.py').read_text(encoding='utf-8')
        self.assertIn('list_user_facing_sticky_notes', src)
        self.assertIn('USER_FACING_STICKY_SOURCE', src)
        self.assertNotIn('db_grumble', src)
        self.assertNotIn('char_grumble', src)

    def test_frontend_no_longer_attributes_stickies_to_per_turn_grumble_engine(self):
        src = Path(ROOT, 'app', 'grumbles', 'index.tsx').read_text(encoding='utf-8')
        self.assertNotIn('grumble_engine', src)
        self.assertNotIn('/chat/text 结束后后台生成', src)
        self.assertIn('cognitive_sticky_notes', src)


class UserFacingStickyQueryTests(unittest.TestCase):
    def test_user_facing_list_filters_slow_loop_active_unhidden_unexpired(self):
        conn = FakeConn()
        conn.select_rows = [make_row()]
        notes = cognitive_reader.list_user_facing_sticky_notes(
            'u1', None, limit=20, conn=conn,
        )
        sql, params = conn.executed[0]
        self.assertIn('FROM cognitive_sticky_notes', sql)
        self.assertNotIn('char_grumble', sql)
        self.assertIn('source = %s', sql)
        self.assertIn("status = 'active'", sql)
        self.assertIn('user_hidden_at IS NULL', sql)
        self.assertIn('expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP', sql)
        self.assertIn('user_visible = TRUE', sql)
        self.assertNotIn('character_id = %s', sql)
        self.assertEqual(params[0], 'u1')
        self.assertIn(USER_FACING_STICKY_SOURCE, params)
        self.assertNotIn(MEMORY_LIFECYCLE_SOURCE, params)
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['source'], USER_FACING_STICKY_SOURCE)
        self.assertEqual(notes[0]['created_by_cycle_id'], 90)
        self.assertEqual(notes[0]['source_event_refs'][0]['event_id'], 27)
        self.assertFalse(notes[0]['viewed'])

    def test_memory_lifecycle_sticky_is_not_selected_by_user_facing_source(self):
        conn = FakeConn()
        conn.select_rows = []
        notes = cognitive_reader.list_user_facing_sticky_notes(
            'u1', 'gojo', conn=conn,
        )
        sql, params = conn.executed[0]
        self.assertEqual(params, ('u1', 'gojo', USER_FACING_STICKY_SOURCE, 50))
        self.assertIn('character_id = %s', sql)
        self.assertIn('user_visible = TRUE', sql)
        self.assertEqual(notes, [])

    def test_inspection_list_can_include_lifecycle_and_hidden_notes(self):
        conn = FakeConn()
        conn.select_rows = [
            make_row(source=MEMORY_LIFECYCLE_SOURCE, note_id=2,
                     content='临近考试/任务：明天考试。到期后不要当作长期事实。'),
        ]
        notes = cognitive_reader.list_sticky_notes(
            'u1', 'gojo', include_inactive=True, include_hidden=True,
            conn=conn,
        )
        sql, params = conn.executed[0]
        self.assertNotIn('source = %s', sql)
        self.assertNotIn('user_hidden_at IS NULL', sql)
        self.assertNotIn('user_visible = TRUE', sql)
        self.assertEqual(notes[0]['source'], MEMORY_LIFECYCLE_SOURCE)

    def test_unviewed_count_does_not_use_completed_status(self):
        conn = FakeConn()
        conn.count = 3
        n = cognitive_reader.count_unviewed_sticky_notes(
            'u1', None, source=USER_FACING_STICKY_SOURCE, conn=conn,
        )
        sql, params = conn.executed[0]
        self.assertEqual(n, 3)
        self.assertIn('viewed = FALSE', sql)
        self.assertIn('user_hidden_at IS NULL', sql)
        self.assertIn('user_visible = TRUE', sql)
        self.assertNotIn("status = 'completed'", sql)
        self.assertIn(USER_FACING_STICKY_SOURCE, params)

    def test_mark_viewed_only_updates_read_state(self):
        conn = FakeConn()
        conn.marked = 2
        n = cognitive_reader.mark_sticky_notes_viewed(
            'u1', None, source=USER_FACING_STICKY_SOURCE, conn=conn,
        )
        sql, _params = conn.executed[0]
        self.assertEqual(n, 2)
        self.assertEqual(conn.commits, 1)
        self.assertIn('viewed = TRUE', sql)
        self.assertNotIn("status = 'completed'", sql)
        self.assertNotIn('completed_at', sql)
        self.assertNotIn('user_hidden_at = CURRENT_TIMESTAMP', sql)

    def test_hide_does_not_complete_semantic_status(self):
        conn = FakeConn()
        ok = cognitive_reader.hide_sticky_note('u1', 11, conn=conn)
        sql, params = conn.executed[0]
        self.assertTrue(ok)
        self.assertEqual(params, (11, 'u1'))
        self.assertIn('user_hidden_at = CURRENT_TIMESTAMP', sql)
        self.assertNotIn("status = 'completed'", sql)
        self.assertNotIn('completed_at', sql)
        self.assertNotIn('viewed = TRUE', sql)

    def test_complete_sticky_note_still_only_changes_semantic_status(self):
        conn = FakeConn()
        ok = cognitive_reader.complete_sticky_note('u1', 'gojo', 11, conn=conn)
        sql, params = conn.executed[0]
        self.assertTrue(ok)
        self.assertEqual(params, (11, 'u1', 'gojo'))
        self.assertIn("status = 'completed'", sql)
        self.assertNotIn('viewed = TRUE', sql)
        self.assertNotIn('user_hidden_at = CURRENT_TIMESTAMP', sql)


class PersistStickyProvenanceTests(unittest.TestCase):
    def test_persist_sql_records_cycle_and_resets_viewed_on_content_change(self):
        persist_src = Path(BACKEND, 'cognitive_output.py').read_text(encoding='utf-8')
        start = persist_src.index("for note in output.get('sticky_note_updates'")
        end = persist_src.index("for entry in output.get('diary_entries'")
        sticky_block = persist_src[start:end]
        self.assertIn("INSERT INTO cognitive_sticky_notes", sticky_block)
        self.assertIn('created_by_cycle_id', sticky_block)
        self.assertIn('source_event_refs', sticky_block)
        self.assertIn('USER_FACING_STICKY_SOURCE', sticky_block)
        self.assertIn('viewed = CASE', sticky_block)
        self.assertIn('user_hidden_at = CASE', sticky_block)
        self.assertIn('user_visible = CASE', sticky_block)
        self.assertIn("EXCLUDED.content THEN NULL", sticky_block)
        self.assertNotIn('char_grumble', sticky_block)
        hide_case = sticky_block.split('user_hidden_at = CASE', 1)[1].split(
            'user_visible = CASE', 1)[0]
        self.assertNotIn('status', hide_case)

    def test_worker_prompt_forbids_second_inner_monologue_pass(self):
        src = ' '.join(Path(BACKEND, 'cognitive_worker.py').read_text(encoding='utf-8').split())
        self.assertIn('user-facing presentation of Slow Loop working state', src)
        self.assertIn('not a second per-turn roleplay pass', src)
        self.assertIn('Do not invent an emotion field', src)


class RouteGrumbleApiTests(unittest.TestCase):
    def setUp(self):
        router = Mock()
        router.get.side_effect = lambda *_args, **_kwargs: lambda function: function
        router.post.side_effect = lambda *_args, **_kwargs: lambda function: function
        router.delete.side_effect = lambda *_args, **_kwargs: lambda function: function
        self.listed = [
            {
                'id': 11,
                'character_id': 'gojo',
                'note_key': 'thread.visa',
                'content': '签证那件事我还挂着。',
                'status': 'active',
                'source': USER_FACING_STICKY_SOURCE,
                'source_event_refs': [{'event_id': 27, 'reason': 'current'}],
                'created_by_cycle_id': 90,
                'updated_by_cycle_id': 90,
                'created_at': NOW.isoformat(),
                'updated_at': NOW.isoformat(),
                'viewed': False,
            }
        ]
        self.list_kwargs = []
        self.unviewed_kwargs = []
        self.marked_kwargs = []
        self.hidden_ids = []

        def _list(user_id, character_id=None, *, limit=100):
            self.list_kwargs.append((user_id, character_id, limit))
            return list(self.listed)

        def _count(user_id, character_id=None, *, source=None):
            self.unviewed_kwargs.append((user_id, character_id, source))
            return 2

        def _mark(user_id, character_id=None, *, source=None):
            self.marked_kwargs.append((user_id, character_id, source))
            for note in self.listed:
                note['viewed'] = True
            return 1

        def _hide(user_id, note_id):
            self.hidden_ids.append((user_id, note_id))
            return True

        modules = {
            'fastapi': stub('fastapi', APIRouter=Mock(return_value=router)),
            'fastapi.responses': stub(
                'fastapi.responses',
                JSONResponse=lambda content, status_code=200: types.SimpleNamespace(
                    body=json.dumps(content, ensure_ascii=False).encode(),
                    status_code=status_code,
                    payload=content,
                ),
            ),
            'cognitive_config': stub(
                'cognitive_config',
                USER_FACING_STICKY_SOURCE=USER_FACING_STICKY_SOURCE,
            ),
            'cognitive_reader': stub(
                'cognitive_reader',
                count_unviewed_sticky_notes=_count,
                hide_sticky_note=_hide,
                list_user_facing_sticky_notes=_list,
                mark_sticky_notes_viewed=_mark,
            ),
        }
        self.route = load_source('route_grumble', modules)

    def test_get_grumbles_returns_slow_loop_note_with_provenance(self):
        response = asyncio.run(self.route.get_grumbles('u1', None, 100))
        body = json.loads(response.body)
        self.assertEqual(self.list_kwargs, [('u1', None, 100)])
        item = body['grumbles'][0]
        self.assertEqual(item['id'], 11)
        self.assertEqual(item['content'], '签证那件事我还挂着。')
        self.assertEqual(item['source'], USER_FACING_STICKY_SOURCE)
        self.assertEqual(item['created_by_cycle_id'], 90)
        self.assertEqual(item['source_event_refs'][0]['event_id'], 27)
        self.assertNotIn('emotion', item)
        self.assertNotIn('trigger_snippet', item)

    def test_unviewed_count_uses_user_facing_source(self):
        response = asyncio.run(self.route.unviewed_count('u1', None))
        body = json.loads(response.body)
        self.assertEqual(body['count'], 2)
        self.assertEqual(
            self.unviewed_kwargs,
            [('u1', None, USER_FACING_STICKY_SOURCE)],
        )

    def test_mark_viewed_does_not_call_complete(self):
        response = asyncio.run(self.route.mark_viewed({'user_id': 'u1'}))
        body = json.loads(response.body)
        self.assertTrue(body['ok'])
        self.assertEqual(body['marked'], 1)
        self.assertEqual(
            self.marked_kwargs,
            [('u1', None, USER_FACING_STICKY_SOURCE)],
        )
        self.assertTrue(self.listed[0]['viewed'])
        self.assertEqual(self.listed[0]['status'], 'active')

    def test_delete_hides_instead_of_completing(self):
        response = asyncio.run(self.route.del_grumble(11, 'u1'))
        body = json.loads(response.body)
        self.assertTrue(body['ok'])
        self.assertEqual(self.hidden_ids, [('u1', 11)])

    def test_ddl_adds_independent_read_state(self):
        from cognitive_db import COGNITIVE_DDL
        blob = '\n'.join(COGNITIVE_DDL)
        self.assertIn('viewed BOOLEAN NOT NULL DEFAULT FALSE', blob)
        self.assertIn('viewed_at TIMESTAMPTZ', blob)
        self.assertIn('user_hidden_at TIMESTAMPTZ', blob)
        self.assertIn('user_visible BOOLEAN NOT NULL DEFAULT TRUE', blob)
        self.assertIn('ADD COLUMN IF NOT EXISTS viewed', blob)
        self.assertIn('ADD COLUMN IF NOT EXISTS user_visible', blob)

    def test_startup_excludes_pre_presentation_slow_loop_once_without_delete(self):
        source = Path(BACKEND, 'cognitive_db.py').read_text(encoding='utf-8')
        self.assertIn('user_facing_sticky_v1_exclude_pre_presentation', source)
        self.assertIn('user_visible = FALSE', source)
        self.assertIn("source = 'cognitive_slow_loop'", source)
        self.assertNotIn('DELETE FROM cognitive_sticky_notes', source)
        self.assertNotIn("status = 'archived'", source.split(
            'user_facing_sticky_v1_exclude_pre_presentation')[1].split('conn.commit()')[0])


class CrossCharacterGrumbleApiTests(unittest.TestCase):
    def setUp(self):
        router = Mock()
        router.get.side_effect = lambda *_args, **_kwargs: lambda function: function
        router.post.side_effect = lambda *_args, **_kwargs: lambda function: function
        router.delete.side_effect = lambda *_args, **_kwargs: lambda function: function
        self.notes = [
            {
                'id': 11, 'character_id': 'gojo', 'note_key': 'thread.visa',
                'content': '签证还挂着。', 'status': 'active',
                'source': USER_FACING_STICKY_SOURCE,
                'source_event_refs': [{'event_id': 27, 'reason': 'gojo'}],
                'created_by_cycle_id': 90, 'updated_by_cycle_id': 90,
                'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat(),
                'viewed': False,
            },
            {
                'id': 12, 'character_id': 'geto', 'note_key': 'thread.exam',
                'content': '她明天考试这件事我还记着。', 'status': 'active',
                'source': USER_FACING_STICKY_SOURCE,
                'source_event_refs': [{'event_id': 28, 'reason': 'geto'}],
                'created_by_cycle_id': 91, 'updated_by_cycle_id': 91,
                'created_at': NOW.isoformat(), 'updated_at': NOW.isoformat(),
                'viewed': False,
            },
        ]

        def _visible(character_id=None, unviewed_only=False):
            items = list(self.notes)
            if character_id:
                items = [n for n in items if n['character_id'] == character_id]
            if unviewed_only:
                items = [n for n in items if not n['viewed']]
            return items

        def _list(user_id, character_id=None, *, limit=100):
            self.list_kwargs.append((user_id, character_id, limit))
            return _visible(character_id)[:limit]

        def _count(user_id, character_id=None, *, source=None):
            self.unviewed_kwargs.append((user_id, character_id, source))
            return len(_visible(character_id, unviewed_only=True))

        def _mark(user_id, character_id=None, *, source=None):
            self.marked_kwargs.append((user_id, character_id, source))
            marked = 0
            for note in _visible(character_id, unviewed_only=True):
                note['viewed'] = True
                marked += 1
            return marked

        self.list_kwargs = []
        self.unviewed_kwargs = []
        self.marked_kwargs = []
        modules = {
            'fastapi': stub('fastapi', APIRouter=Mock(return_value=router)),
            'fastapi.responses': stub(
                'fastapi.responses',
                JSONResponse=lambda content, status_code=200: types.SimpleNamespace(
                    body=json.dumps(content, ensure_ascii=False).encode(),
                    status_code=status_code,
                    payload=content,
                ),
            ),
            'cognitive_config': stub(
                'cognitive_config',
                USER_FACING_STICKY_SOURCE=USER_FACING_STICKY_SOURCE,
            ),
            'cognitive_reader': stub(
                'cognitive_reader',
                count_unviewed_sticky_notes=_count,
                hide_sticky_note=lambda *_a, **_k: True,
                list_user_facing_sticky_notes=_list,
                mark_sticky_notes_viewed=_mark,
            ),
        }
        self.route = load_source('route_grumble', modules)

    def test_mixed_list_without_character_id_returns_gojo_and_geto(self):
        response = asyncio.run(self.route.get_grumbles('u1', None, 100))
        body = json.loads(response.body)
        ids = [item['character_id'] for item in body['grumbles']]
        self.assertEqual(ids, ['gojo', 'geto'])
        self.assertEqual(self.list_kwargs, [('u1', None, 100)])

    def test_character_filter_returns_only_gojo(self):
        response = asyncio.run(self.route.get_grumbles('u1', 'gojo', 100))
        body = json.loads(response.body)
        self.assertEqual([g['character_id'] for g in body['grumbles']], ['gojo'])
        self.assertEqual(len(body['grumbles']), 1)

    def test_unviewed_count_mixed_and_filtered(self):
        mixed = asyncio.run(self.route.unviewed_count('u1', None))
        gojo = asyncio.run(self.route.unviewed_count('u1', 'gojo'))
        self.assertEqual(json.loads(mixed.body)['count'], 2)
        self.assertEqual(json.loads(gojo.body)['count'], 1)

    def test_mark_viewed_gojo_does_not_mark_geto(self):
        response = asyncio.run(self.route.mark_viewed({
            'user_id': 'u1', 'character_id': 'gojo',
        }))
        body = json.loads(response.body)
        self.assertEqual(body['marked'], 1)
        self.assertTrue(self.notes[0]['viewed'])
        self.assertFalse(self.notes[1]['viewed'])
        mixed = asyncio.run(self.route.unviewed_count('u1', None))
        self.assertEqual(json.loads(mixed.body)['count'], 1)

    def test_mark_viewed_without_character_marks_remaining(self):
        asyncio.run(self.route.mark_viewed({'user_id': 'u1'}))
        self.assertTrue(all(note['viewed'] for note in self.notes))
        mixed = asyncio.run(self.route.unviewed_count('u1', None))
        self.assertEqual(json.loads(mixed.body)['count'], 0)

    def test_reader_sql_omits_character_filter_when_mixed(self):
        conn = FakeConn()
        conn.select_rows = [make_row(), make_row(note_id=12, character_id='geto')]
        cognitive_reader.list_user_facing_sticky_notes('u1', None, conn=conn)
        sql, params = conn.executed[0]
        self.assertNotIn('character_id = %s', sql)
        self.assertEqual(params[0], 'u1')
        conn2 = FakeConn()
        conn2.select_rows = [make_row()]
        cognitive_reader.list_user_facing_sticky_notes('u1', 'gojo', conn=conn2)
        sql2, params2 = conn2.executed[0]
        self.assertIn('character_id = %s', sql2)
        self.assertEqual(params2[1], 'gojo')
        conn3 = FakeConn()
        conn3.count = 1
        cognitive_reader.count_unviewed_sticky_notes(
            'u1', None, source=USER_FACING_STICKY_SOURCE, conn=conn3)
        self.assertNotIn('character_id = %s', conn3.executed[0][0])
        conn4 = FakeConn()
        conn4.marked = 1
        cognitive_reader.mark_sticky_notes_viewed(
            'u1', 'gojo', source=USER_FACING_STICKY_SOURCE, conn=conn4)
        self.assertIn('character_id = %s', conn4.executed[0][0])
        self.assertEqual(conn4.executed[0][1][1], 'gojo')


if __name__ == '__main__':
    unittest.main()
