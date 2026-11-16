"""View stickies keep a G8-accepted perspective off the claim path."""
import asyncio
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))

import candidate_producer as producer
import cognitive_reader
import memory_lifecycle
from cognitive_output import STICKY_NOTE_EMOTIONS, sticky_emotion_tag
from config import CN_TZ
from tests import test_memory_candidate_source as source_fixture


def _base_candidate(**extra):
    candidate = {
        'source_event_id': 'source-0',
        'quote': '今天我亏了69元',
        'summary': '她今天亏了69元',
        'predicate': 'reported_fact',
        'object': '交易结果',
        'value': '亏了69元',
    }
    candidate.update(extra)
    return candidate


class RecordingCursor:
    """Answers the view-note count and records sticky inserts."""

    def __init__(self, count=0):
        self.count = count
        self.rows = []
        self.sqls = []
        self._mode = None
        self._inserted = None

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.sqls.append(compact)
        self._mode = None
        self._inserted = None
        if 'COUNT(*)' in compact and 'sticky_view_v1' in compact:
            self._mode = 'count'
            return
        if 'INSERT INTO cognitive_sticky_notes' not in compact:
            return
        self._mode = 'insert'
        (user_id, character_id, note_key, content, source,
         refs, expires_at, metadata) = params
        key = (user_id, character_id, note_key)
        if any(row['key'] == key for row in self.rows):
            return
        self.rows.append({
            'key': key,
            'content': content,
            'source': source,
            'metadata': json.loads(metadata),
            'expires_at': expires_at,
            'refs': json.loads(refs),
        })
        self._inserted = len(self.rows)

    def fetchone(self):
        if self._mode == 'count':
            return (self.count,)
        if self._mode == 'insert' and self._inserted is not None:
            return (self._inserted,)
        return None


class StickyViewPromptTests(unittest.TestCase):
    def test_flag_off_system_prompt_is_byte_identical(self):
        expected = producer._SYSTEM % ', '.join(sorted(producer._PREDICATES))
        with patch('characters_data._loader.load_core',
                   side_effect=AssertionError('load_core')):
            self.assertEqual(producer._system_prompt('gojo'), expected)
        self.assertFalse(producer._sticky_view_on())

    def test_flag_on_adds_name_enum_and_profile_only(self):
        from characters_data._loader import load_core
        core = load_core('gojo')
        profile = core['core_prompt'].split('【笑声规则', 1)[0][:900]
        enum = '、'.join(sorted(STICKY_NOTE_EMOTIONS))
        with patch('config.STICKY_VIEW_ENABLED', True):
            system = producer._system_prompt('gojo')
        self.assertIn('perspective是五条悟本人读到这句话时心里冒出来的一句话', system)
        self.assertIn('必须以“我”开头，不能以“我们”开头', system)
        self.assertIn('不要写成“你说过”', system)
        self.assertIn('emotion是他写这句话时的情绪，只能是：' + enum, system)
        self.assertIn('只有写了perspective时才写emotion', system)
        self.assertIn('perspective?、emotion?、persistence?', system)
        self.assertIn(producer._VOICE_HEADER, system)
        self.assertIn(profile, system)
        self.assertNotIn('禁止：「あはは」', system)
        self.assertNotIn(producer._PERSPECTIVE_RULE, system)
        restored = system.split('\n' + producer._VOICE_HEADER + '\n', 1)[0]
        restored = restored.replace(
            'perspective?、emotion?、persistence?',
            'perspective?、persistence?', 1)
        start = restored.index('perspective是五条悟')
        end = restored.index('只有写了perspective时才写emotion。') + len(
            '只有写了perspective时才写emotion。')
        restored = restored[:start] + producer._PERSPECTIVE_RULE + restored[end:]
        expected = producer._SYSTEM % ', '.join(sorted(producer._PREDICATES))
        self.assertEqual(restored, expected)

    def test_load_core_failure_skips_the_profile_and_does_not_raise(self):
        with patch('config.STICKY_VIEW_ENABLED', True), \
                patch('characters_data._loader.load_core',
                      side_effect=RuntimeError('missing')):
            system = producer._system_prompt('no-such-role')
        self.assertIn('perspective是no-such-role本人', system)
        self.assertNotIn(producer._VOICE_HEADER, system)
        with patch('config.STICKY_VIEW_ENABLED', True), \
                patch('characters_data._loader.load_core', return_value=None):
            empty = producer._system_prompt('no-such-role')
        self.assertNotIn(producer._VOICE_HEADER, empty)
        self.assertIn('perspective是no-such-role本人', empty)


class StickyViewGateTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, 8, tzinfo=timezone.utc)

    def _process(self, candidate, *, shadow=False, validate=None):
        inputs = [{'event_id': 11, 'source_event_id': 'source-0',
                   'occurred_at': self.now}]
        seen = {}

        def fake_validate(cur, user_id, character_id, checked):
            seen['checked'] = dict(checked)
            if validate is not None:
                return validate(checked)
            return True, None

        lock = Mock(return_value=1)
        with patch.dict(sys.modules, {'cognitive_queue': Mock(
                stable_advisory_lock_key=lock)}), \
                patch.object(producer, 'validate_candidate', fake_validate), \
                patch.object(producer, 'adjudicate_and_project') as project:
            project.return_value = {
                'status': 'validated_projection', 'reason': None}
            records = producer._process_candidates(
                Mock(), 'user-1', 'gojo', [candidate], inputs, {}, shadow)
        return project, seen, records[0]

    def test_bad_emotion_does_not_reject_and_audits_null(self):
        allowed = {'source-0'}
        base = _base_candidate()
        self.assertIsNone(producer.candidate_contract_reason(base, allowed))
        for emotion in (None, 12, '高兴', {'mood': '开心'}):
            item = dict(base)
            if emotion is not None:
                item['emotion'] = emotion
            self.assertIsNone(producer.candidate_contract_reason(item, allowed))
            self.assertIsNone(producer.observed_emotion(item))
        self.assertEqual(producer.observed_emotion({'emotion': '无奈'}), '无奈')
        producer._validate_candidate_batch([dict(base, emotion='无奈')])
        project, seen, record = self._process(dict(base, emotion=12, perspective='我想认真听她说'))
        self.assertNotIn('emotion', seen['checked'])
        self.assertIn('perspective', seen['checked'])
        self.assertIsNone(record['emotion'])
        self.assertEqual(record['perspective'], '我想认真听她说')
        self.assertTrue(record['gate_accepted'])
        self.assertTrue(record['accepted'])
        self.assertIsNone(record['view_version'])

    def test_emotion_and_perspective_do_not_enter_adjudication(self):
        project, seen, record = self._process(
            _base_candidate(emotion='无奈', perspective='我想认真听她说'))
        projected = project.call_args.kwargs['candidate']
        self.assertNotIn('emotion', projected)
        self.assertNotIn('perspective', projected)
        self.assertIn('perspective', seen['checked'])
        self.assertEqual(record['emotion'], '无奈')
        self.assertEqual(projected['quote'], '今天我亏了69元')


class StickyViewWriteTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, 12, tzinfo=CN_TZ)
        self.candidate = _base_candidate(
            utterance_mode='sincere', persistence='transient', time_scope='今天')

    def _write(self, *, perspective, emotion='无奈', quote='今天我亏了69元',
               occurred_at=None, count=0, enabled=True, source_text='今天我亏了69元，想和你聊一聊。'):
        cursor = RecordingCursor(count=count)
        with patch('config.STICKY_VIEW_ENABLED', enabled):
            memory_lifecycle.project_candidate_sticky(
                cursor, user_id='u', character_id='c', event_id=7,
                source_id='source-0', candidate=self.candidate, now=self.now,
                source_text=source_text, perspective=perspective, emotion=emotion,
                quote=quote, occurred_at=self.now if occurred_at is None else occurred_at)
        return cursor

    def test_kept_perspective_writes_view_metadata(self):
        cursor = self._write(perspective='我想认真听她说')
        self.assertEqual(len(cursor.rows), 1)
        row = cursor.rows[0]
        self.assertEqual(row['content'], '我想认真听她说')
        self.assertEqual(row['metadata']['projection_version'], 'sticky_view_v1')
        self.assertEqual(row['metadata']['emotion'], '无奈')
        self.assertEqual(row['metadata']['tag'], sticky_emotion_tag('无奈'))
        self.assertEqual(row['metadata']['trigger_snippet'], '今天我亏了69元')
        self.assertEqual(row['metadata']['view_version'], 'view_v1')
        self.assertTrue(all('long_memory' not in sql and 'cognitive_beliefs' not in sql
                            and 'cognitive_hypotheses' not in sql
                            for sql in cursor.sqls))

    def test_forbidden_and_narrator_wording_fall_back(self):
        for perspective in (
                '我想你说过这件事', '我想你讲过这件事', '我想你提过这件事',
                '我想你告诉过我', '我观察到她今天很难', '我啊，用户没说完'):
            cursor = self._write(perspective=perspective)
            self.assertEqual(cursor.rows[0]['content'], '她今天亏了69元', perspective)
            self.assertEqual(
                cursor.rows[0]['metadata']['projection_version'],
                'sticky_candidate_v1', perspective)
            self.assertNotIn('sticky_view_v1', '\n'.join(cursor.sqls))

    def test_old_source_and_daily_limit_fall_back(self):
        old = self._write(
            perspective='我想认真听她说',
            occurred_at=self.now - timedelta(hours=49))
        self.assertEqual(old.rows[0]['metadata']['projection_version'],
                         'sticky_candidate_v1')
        limited = self._write(perspective='我想认真听她说', count=8)
        self.assertEqual(limited.rows[0]['metadata']['projection_version'],
                         'sticky_candidate_v1')
        self.assertTrue(any('sticky_view_v1' in sql for sql in limited.sqls))

    def test_flag_off_keeps_the_summary_path(self):
        cursor = self._write(perspective='我想认真听她说', enabled=False)
        self.assertEqual(cursor.rows[0]['content'], '她今天亏了69元')
        self.assertEqual(cursor.rows[0]['metadata']['projection_version'],
                         'sticky_candidate_v1')
        self.assertFalse(any('sticky_view_v1' in sql for sql in cursor.sqls))

    def test_empty_source_text_writes_nothing(self):
        cursor = self._write(perspective='我想认真听她说', source_text='')
        self.assertEqual(cursor.rows, [])
        self.assertEqual(cursor.sqls, [])


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'requires disposable local PGlite')
class StickyViewDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(source_fixture.CandidateSourceTests.setUpClass.__func__)
    tearDownClass = classmethod(source_fixture.CandidateSourceTests.tearDownClass.__func__)
    tearDown = source_fixture.CandidateSourceTests.tearDown
    sql = source_fixture.CandidateSourceTests.sql
    source = source_fixture.CandidateSourceTests.source
    snapshot = source_fixture.CandidateSourceTests.snapshot

    def setUp(self):
        source_fixture.CandidateSourceTests.setUp(self)
        self.sql('ALTER TABLE chat_log ADD COLUMN reply_to_event_id TEXT')
        self.database.commit()
        self.now = datetime.now(CN_TZ) - timedelta(hours=1)

    def _inputs(self, text):
        return [{'event_id': 11, 'source_event_id': 'source',
                 'occurred_at': self.now, 'content': text}]

    def _run(self, candidate, *, shadow=False):
        text = self.source()
        self.snapshot(text)
        self.database.commit()
        with patch('config.STICKY_VIEW_ENABLED', True), \
                patch.object(producer, 'adjudicate_and_project',
                             return_value={'status': 'validated_projection',
                                           'reason': None}) as project:
            records = producer._process_candidates(
                self.database.cursor(), 'u', 'c', [candidate],
                self._inputs(text), {}, shadow)
        self.database.commit()
        return project, records[0]

    def test_g8_drop_falls_back_and_kept_perspective_does_not_enter_claims(self):
        dropped = source_fixture.CandidateSourceTests.candidate(
            self, perspective='她其实很在意', utterance_mode='sincere',
            persistence='transient', time_scope='今天')
        project, record = self._run(dropped)
        self.assertNotIn('perspective', project.call_args.kwargs['candidate'])
        self.assertIsNone(record['emotion'])
        rows = self.sql(
            '''SELECT content, metadata->>'projection_version'
               FROM cognitive_sticky_notes''')
        self.assertEqual(rows, [('她今天亏了69元', 'sticky_candidate_v1')])
        for table in ('long_memory', 'cognitive_beliefs', 'cognitive_hypotheses'):
            self.assertEqual(self.sql(f'SELECT COUNT(*) FROM {table}')[0][0], 0)

    def test_kept_perspective_writes_view_and_no_durable_rows(self):
        kept = source_fixture.CandidateSourceTests.candidate(
            self, perspective='我想认真听她说', emotion='无奈',
            utterance_mode='sincere', persistence='transient', time_scope='今天')
        project, record = self._run(kept)
        projected = project.call_args.kwargs['candidate']
        self.assertNotIn('perspective', projected)
        self.assertNotIn('emotion', projected)
        self.assertEqual(record['emotion'], '无奈')
        self.assertEqual(record['view_version'], 'view_v1')
        content, version, emotion, tag, snippet = self.sql(
            '''SELECT content,
                      metadata->>'projection_version' AS projection_version,
                      metadata->>'emotion' AS emotion,
                      metadata->>'tag' AS tag,
                      metadata->>'trigger_snippet' AS snippet
               FROM cognitive_sticky_notes''')[0]
        self.assertEqual(content, '我想认真听她说')
        self.assertEqual(version, 'sticky_view_v1')
        self.assertEqual(emotion, '无奈')
        self.assertEqual(tag, sticky_emotion_tag('无奈'))
        self.assertEqual(snippet, '今天我亏了69元')
        for table in ('long_memory', 'cognitive_beliefs', 'cognitive_hypotheses'):
            self.assertEqual(self.sql(f'SELECT COUNT(*) FROM {table}')[0][0], 0)

    def test_shadow_writes_no_sticky(self):
        candidate = source_fixture.CandidateSourceTests.candidate(
            self, perspective='我想认真听她说', emotion='无奈')
        project, record = self._run(candidate, shadow=True)
        project.assert_not_called()
        self.assertEqual(self.sql('SELECT COUNT(*) FROM cognitive_sticky_notes')[0][0], 0)
        self.assertTrue(record['gate_accepted'])
        self.assertTrue(record['accepted'])

    def _insert_view(self, text='今天我亏了69元，想和你聊一聊。', source_id='source'):
        self.source(text=text, source_id=source_id)
        self.sql(
            '''INSERT INTO cognitive_sticky_notes
               (user_id, character_id, note_key, content, status, source,
                source_event_refs, expires_at, metadata, user_visible)
               VALUES (%s, %s, %s, %s, 'active', %s, %s::jsonb, %s, %s::jsonb, TRUE)''',
            ('u', 'c', 'memory_lifecycle.raw.viewtest', '我想认真听她说',
             memory_lifecycle.MEMORY_LIFECYCLE_SOURCE,
             json.dumps([{'event_id': 1, 'source_type': 'raw_event',
                          'source_id': source_id, 'user_id': 'u',
                          'character_id': 'c'}], ensure_ascii=False),
             self.now + timedelta(days=2),
             json.dumps({
                 'projection_version': 'sticky_view_v1',
                 'source_text': text,
                 'emotion': '无奈',
                 'tag': '..',
                 'trigger_snippet': text[:40],
                 'view_version': 'view_v1',
             }, ensure_ascii=False)))
        self.database.commit()

    def test_include_view_and_chat_switch(self):
        hidden = cognitive_reader._sticky_where('u', 'c', source='memory_lifecycle_fast_loop')
        shown = cognitive_reader._sticky_where(
            'u', 'c', source='memory_lifecycle_fast_loop', include_view=True)
        self.assertIn(
            "metadata->>'projection_version' IN ('sticky_evidence_v1','sticky_candidate_v1')",
            hidden[0])
        self.assertNotIn('sticky_view_v1', hidden[0])
        self.assertIn('sticky_view_v1', shown[0])
        self.assertIn("raw.text = metadata->>'source_text'", shown[0])
        self.assertIn("COALESCE(raw.status, 'active') = 'active'", shown[0])

        self._insert_view()
        self.assertEqual(cognitive_reader.list_user_facing_sticky_notes(
            'u', 'c', conn=self.database), [])
        visible = cognitive_reader.list_user_facing_sticky_notes(
            'u', 'c', conn=self.database, include_view=True)
        self.assertEqual([row['content'] for row in visible], ['我想认真听她说'])
        with patch('config.STICKY_VIEW_IN_CHAT', False):
            self.assertEqual(memory_lifecycle.recall_sticky_notes('u', 'c'), [])
            state = cognitive_reader.fetch_cognitive_reader_state(
                'u', 'c', conn=self.database)
            self.assertEqual(state['sticky_notes'], [])
        with patch('config.STICKY_VIEW_IN_CHAT', True):
            recalled = memory_lifecycle.recall_sticky_notes('u', 'c')
            self.assertEqual([row['content'] for row in recalled], ['我想认真听她说'])
            state = cognitive_reader.fetch_cognitive_reader_state(
                'u', 'c', conn=self.database)
            self.assertEqual(
                [row['content'] for row in state['sticky_notes']],
                ['我想认真听她说'])
        page = json.loads(asyncio.run(
            __import__('route_grumble').get_grumbles('u', 'c', 20)).body)
        self.assertEqual([row['content'] for row in page['grumbles']], ['我想认真听她说'])

    def test_edited_source_hides_the_view_note_and_id_actions_still_work(self):
        self._insert_view()
        note_id = self.sql('SELECT id FROM cognitive_sticky_notes')[0][0]
        self.sql("UPDATE chat_log SET text='改过的原话' WHERE event_id='source'")
        self.database.commit()
        self.assertEqual(cognitive_reader.list_user_facing_sticky_notes(
            'u', 'c', conn=self.database, include_view=True), [])
        self.sql("UPDATE chat_log SET text=%s, status='deleted' WHERE event_id='source'",
                 ('今天我亏了69元，想和你聊一聊。',))
        self.database.commit()
        self.assertEqual(cognitive_reader.list_user_facing_sticky_notes(
            'u', 'c', conn=self.database, include_view=True), [])
        self.sql("UPDATE chat_log SET status='active' WHERE event_id='source'")
        self.database.commit()
        self.assertEqual(len(cognitive_reader.list_user_facing_sticky_notes(
            'u', 'c', conn=self.database, include_view=True)), 1)
        self.assertTrue(cognitive_reader.hide_sticky_note('u', note_id, conn=self.database))
        self.assertEqual(cognitive_reader.list_user_facing_sticky_notes(
            'u', 'c', conn=self.database, include_view=True), [])
        self.sql('UPDATE cognitive_sticky_notes SET user_hidden_at=NULL WHERE id=%s',
                 (note_id,))
        self.database.commit()
        self.assertTrue(cognitive_reader.complete_sticky_note(
            'u', 'c', note_id, conn=self.database))
        self.assertEqual(cognitive_reader.list_user_facing_sticky_notes(
            'u', 'c', conn=self.database, include_view=True), [])


if __name__ == '__main__':
    unittest.main()
