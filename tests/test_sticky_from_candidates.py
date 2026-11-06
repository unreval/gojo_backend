"""Sticky notes projected from candidate time_scope, not keyword classification."""
import hashlib
import json
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))

import candidate_producer as producer  # noqa: E402
import memory_lifecycle  # noqa: E402
from config import CN_TZ  # noqa: E402
from memory_lifecycle import (  # noqa: E402
    MEMORY_LIFECYCLE_SOURCE,
    project_candidate_sticky,
    project_pending_sticky,
    sticky_ttl_from_time_scope,
)


def _end_of_local_day(now, days):
    local = now.astimezone(CN_TZ)
    end = (local + timedelta(days=days)).replace(
        hour=23, minute=59, second=59, microsecond=0)
    return int((end - local).total_seconds())


class StickyTtlTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 15, 0, tzinfo=CN_TZ)

    def test_tomorrow_ends_at_the_next_local_day(self):
        self.assertEqual(
            sticky_ttl_from_time_scope('明天', self.now),
            _end_of_local_day(self.now, 1))

    def test_day_after_tomorrow_is_two_days_out(self):
        self.assertEqual(
            sticky_ttl_from_time_scope('后天', self.now),
            _end_of_local_day(self.now, 2))

    def test_next_week_and_weekday_are_eight_days_out(self):
        expected = _end_of_local_day(self.now, 8)
        self.assertEqual(sticky_ttl_from_time_scope('下周', self.now), expected)
        self.assertEqual(sticky_ttl_from_time_scope('周一', self.now), expected)

    def test_tonight_and_afternoon_end_today(self):
        expected = _end_of_local_day(self.now, 0)
        self.assertEqual(sticky_ttl_from_time_scope('今晚', self.now), expected)
        self.assertEqual(sticky_ttl_from_time_scope('下午', self.now), expected)

    def test_later_offset_wins_over_a_same_day_word(self):
        self.assertEqual(
            sticky_ttl_from_time_scope('明天晚上', self.now),
            _end_of_local_day(self.now, 1))

    def test_past_words_do_not_produce_a_ttl(self):
        for text in ('昨天', '前天', '上周', '刚才', '刚刚', '之前', '昨天晚上'):
            with self.subTest(time_scope=text):
                self.assertIsNone(sticky_ttl_from_time_scope(text, self.now))

    def test_empty_and_missing_time_scope_do_not_produce_a_ttl(self):
        self.assertIsNone(sticky_ttl_from_time_scope('', self.now))
        self.assertIsNone(sticky_ttl_from_time_scope('   ', self.now))
        self.assertIsNone(sticky_ttl_from_time_scope(None, self.now))

    def test_ttl_is_clamped_to_the_configured_maximum(self):
        natural = _end_of_local_day(self.now, 1)
        capped = natural // 2
        self.assertGreater(natural, capped)
        self.assertGreater(capped, memory_lifecycle.COGNITIVE_STICKY_NOTE_MIN_TTL_SECONDS)
        with patch.object(memory_lifecycle, 'COGNITIVE_STICKY_NOTE_MAX_TTL_SECONDS', capped):
            self.assertEqual(sticky_ttl_from_time_scope('明天', self.now), capped)

    def test_ttl_is_clamped_to_the_configured_minimum(self):
        late = datetime(2026, 11, 6, 23, 59, 50, tzinfo=CN_TZ)
        raw = _end_of_local_day(late, 0)
        floor = raw + 50
        self.assertLess(raw, floor)
        with patch.object(memory_lifecycle, 'COGNITIVE_STICKY_NOTE_MIN_TTL_SECONDS', floor), \
                patch.object(
                    memory_lifecycle, 'COGNITIVE_STICKY_NOTE_MAX_TTL_SECONDS', floor + 1000):
            self.assertEqual(sticky_ttl_from_time_scope('今晚', late), floor)


class StickyCursor:
    """Records sticky inserts and enforces the note_key unique constraint."""

    def __init__(self):
        self.rows = []
        self.executed = []
        self._returning = None

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        self._returning = None
        if 'INSERT INTO cognitive_sticky_notes' not in compact:
            return
        (user_id, character_id, note_key, content, source,
         refs, expires_at, metadata) = params
        key = (user_id, character_id, note_key)
        if any(row['key'] == key for row in self.rows):
            if 'ON CONFLICT' in compact and 'DO NOTHING' in compact:
                return
            raise AssertionError('duplicate sticky without ON CONFLICT DO NOTHING')
        self.rows.append({
            'id': len(self.rows) + 1,
            'key': key,
            'note_key': note_key,
            'content': content,
            'source': source,
            'status': 'active',
            'source_event_refs': json.loads(refs),
            'expires_at': expires_at,
            'metadata': json.loads(metadata),
        })
        self._returning = (self.rows[-1]['id'],)

    def fetchone(self):
        row = self._returning
        self._returning = None
        return row


class CandidateStickyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 15, 0, tzinfo=CN_TZ)
        self.cursor = StickyCursor()

    def candidate(self, **overrides):
        value = {
            'utterance_mode': 'sincere',
            'persistence': 'transient',
            'time_scope': '明天',
            'summary': '她明天考试',
            'interpretation': '她其实很紧张',
            'quote': '我明天考试',
        }
        value.update(overrides)
        return value

    def project(self, candidate, source_id='evt-1', source_text='我明天考试'):
        return project_candidate_sticky(
            self.cursor, user_id='u1', character_id='gojo', event_id=11,
            source_id=source_id, candidate=candidate, now=self.now,
            source_text=source_text)

    def test_practice_and_unsure_do_not_write(self):
        for mode in ('practice', 'unsure'):
            with self.subTest(utterance_mode=mode):
                cursor = StickyCursor()
                self.cursor = cursor
                with self.assertLogs('memory_lifecycle', level='DEBUG'):
                    self.assertIsNone(self.project(self.candidate(utterance_mode=mode)))
                self.assertEqual(cursor.rows, [])

    def test_durable_persistence_does_not_write(self):
        with self.assertLogs('memory_lifecycle', level='DEBUG'):
            self.assertIsNone(self.project(self.candidate(persistence='durable')))
        self.assertEqual(self.cursor.rows, [])

    def test_missing_time_scope_does_not_write(self):
        candidate = self.candidate()
        candidate.pop('time_scope')
        with self.assertNoLogs('memory_lifecycle', level='DEBUG'):
            self.assertIsNone(self.project(candidate))
        self.assertIsNone(self.project(self.candidate(time_scope='')))
        self.assertEqual(self.cursor.rows, [])

    def test_summary_longer_than_120_does_not_write(self):
        with self.assertLogs('memory_lifecycle', level='DEBUG'):
            self.assertIsNone(self.project(self.candidate(summary='她' * 121)))
        self.assertEqual(self.cursor.rows, [])

    def test_matching_candidate_writes_one_user_facing_note(self):
        with patch('builtins.print') as printed:
            row_id = self.project(self.candidate(summary='她' * 120, time_scope='明天'))
        self.assertEqual(row_id, 1)
        self.assertEqual(len(self.cursor.rows), 1)
        row = self.cursor.rows[0]
        self.assertEqual(row['source'], MEMORY_LIFECYCLE_SOURCE)
        self.assertEqual(row['source'], 'memory_lifecycle_fast_loop')
        self.assertEqual(row['status'], 'active')
        self.assertEqual(row['content'], '她' * 120)
        self.assertNotIn('紧张', json.dumps(row, ensure_ascii=False, default=str))
        self.assertEqual(row['metadata']['projection_version'], 'sticky_candidate_v1')
        self.assertEqual(row['metadata']['source_text'], '我明天考试')
        self.assertEqual(row['metadata']['time_scope'], '明天')
        self.assertEqual(row['note_key'], 'memory_lifecycle.raw.' + hashlib.sha1(
            b'evt-1').hexdigest()[:20])
        self.assertEqual(row['source_event_refs'], [{
            'event_id': 11, 'source_type': 'raw_event', 'source_id': 'evt-1',
            'user_id': 'u1', 'character_id': 'gojo',
        }])
        ttl = sticky_ttl_from_time_scope('明天', self.now)
        self.assertEqual(row['expires_at'], self.now + timedelta(seconds=ttl))
        sql = self.cursor.executed[-1][0]
        self.assertIn(
            'ON CONFLICT (user_id, character_id, note_key) DO NOTHING', sql)
        message = ' '.join(call.args[0] for call in printed.call_args_list)
        self.assertIn(
            f'[sticky] created from candidate source_event_id=evt-1 '
            f'ttl_seconds={ttl} time_scope=明天',
            message)

    def test_blank_source_text_does_not_write(self):
        for value in ('', None):
            with self.subTest(source_text=value):
                cursor = StickyCursor()
                self.cursor = cursor
                self.assertIsNone(self.project(self.candidate(), source_text=value))
                self.assertEqual(cursor.rows, [])
                self.assertFalse(any(
                    'INSERT INTO cognitive_sticky_notes' in sql
                    for sql, _params in cursor.executed))

    def test_source_text_is_stored_verbatim(self):
        raw = '  我明天要刷段  '
        with patch('builtins.print'):
            row_id = self.project(self.candidate(), source_text=raw)
        self.assertEqual(row_id, 1)
        metadata = self.cursor.rows[0]['metadata']
        self.assertEqual(metadata['source_text'], raw)
        self.assertEqual(metadata['projection_version'], 'sticky_candidate_v1')
        self.assertEqual(metadata['time_scope'], '明天')

    def test_same_source_id_inserts_once(self):
        with patch('builtins.print'):
            first = self.project(self.candidate())
            second = self.project(self.candidate())
        self.assertEqual(first, 1)
        self.assertIsNone(second)
        self.assertEqual(len(self.cursor.rows), 1)

    def test_pending_sticky_and_candidate_sticky_share_one_note_key(self):
        with patch('builtins.print'):
            pending_id = project_pending_sticky(
                self.cursor, user_id='u1', character_id='gojo', event_id=11,
                source_id='evt-1', source_text='明天考试', now=self.now)
            candidate_id = self.project(self.candidate())
        self.assertIsNotNone(pending_id)
        self.assertIsNone(candidate_id)
        self.assertEqual(len(self.cursor.rows), 1)
        self.assertEqual(
            self.cursor.rows[0]['metadata']['projection_version'],
            'sticky_evidence_v1')
        self.assertEqual(
            self.cursor.rows[0]['note_key'],
            'memory_lifecycle.raw.' + hashlib.sha1(b'evt-1').hexdigest()[:20])


class RecordingCursor:
    def __init__(self):
        self.statements = []
        self._returning = None

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.statements.append((compact, params))
        if 'INSERT INTO cognitive_sticky_notes' in compact:
            self._returning = (1,)
        else:
            self._returning = None

    def fetchone(self):
        row = self._returning
        self._returning = None
        return row

    def sql_text(self):
        return '\n'.join(sql for sql, _params in self.statements)


class ProcessCandidateStickyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 15, 0, tzinfo=CN_TZ)

    def drive(self, *, shadow, accepted_gate=True, sticky=None,
              content='我明天要刷段'):
        candidate = {
            'source_event_id': 'source-0',
            'quote': '我明天考试',
            'summary': '她明天考试',
            'predicate': 'reported_fact',
            'object': '考试',
            'value': '考试',
            'interpretation': '她其实很紧张',
            'utterance_mode': 'sincere',
            'persistence': 'transient',
            'time_scope': '明天',
        }
        inputs = [{
            'event_id': 11,
            'source_event_id': 'source-0',
            'occurred_at': self.now,
            'content': content,
        }]
        cursor = RecordingCursor()
        contexts = [
            patch.dict(sys.modules, {'cognitive_queue': Mock(
                stable_advisory_lock_key=Mock(return_value=1))}),
            patch.object(
                producer, 'validate_candidate',
                return_value=(accepted_gate, None if accepted_gate else 'quote_not_verbatim')),
            patch.object(
                producer, 'adjudicate_and_project',
                return_value={'status': 'validated_projection', 'reason': None}),
        ]
        if sticky is not None:
            contexts.append(patch(
                'memory_lifecycle.project_candidate_sticky', side_effect=sticky))
        with ExitStack() as stack:
            for context in contexts:
                stack.enter_context(context)
            project = producer.adjudicate_and_project
            records = producer._process_candidates(
                cursor, 'user-1', 'gojo', [candidate], inputs,
                {'user_id': 'user-1', 'character_id': 'gojo', 'shadow': shadow},
                shadow)
        return cursor, records, project

    def test_shadow_does_not_write_a_sticky(self):
        cursor, records, project = self.drive(shadow=True)
        self.assertTrue(records[0]['accepted'])
        self.assertNotIn('cognitive_sticky_notes', cursor.sql_text())
        self.assertNotIn('SAVEPOINT candidate_sticky', cursor.sql_text())
        project.assert_not_called()

    def test_rejected_candidate_does_not_write_a_sticky(self):
        cursor, records, project = self.drive(shadow=False, accepted_gate=False)
        self.assertFalse(records[0]['accepted'])
        self.assertNotIn('cognitive_sticky_notes', cursor.sql_text())
        project.assert_not_called()

    def test_accepted_live_candidate_writes_the_unstripped_summary(self):
        cursor, records, project = self.drive(shadow=False)
        self.assertTrue(records[0]['accepted'])
        projected = project.call_args.kwargs['candidate']
        self.assertNotIn('persistence', projected)
        self.assertNotIn('utterance_mode', projected)
        self.assertNotIn('interpretation', projected)
        inserts = [
            params for sql, params in cursor.statements
            if 'INSERT INTO cognitive_sticky_notes' in sql
        ]
        self.assertEqual(len(inserts), 1)
        _user, _character, _note_key, content, source, _refs, _expires, metadata = inserts[0]
        self.assertEqual(content, '她明天考试')
        self.assertNotIn('紧张', content)
        self.assertEqual(source, 'memory_lifecycle_fast_loop')
        stored = json.loads(metadata)
        self.assertEqual(stored['projection_version'], 'sticky_candidate_v1')
        self.assertEqual(stored['source_text'], '我明天要刷段')
        self.assertNotEqual(stored['source_text'], '我明天考试')
        self.assertEqual(stored['time_scope'], '明天')
        self.assertNotIn('interpretation', stored)

    def test_sticky_exception_still_returns_the_audit_record(self):
        def boom(*_args, **_kwargs):
            raise RuntimeError('sticky down')

        cursor, records, _project = self.drive(shadow=False, sticky=boom)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record['kind'], 'candidate')
        self.assertTrue(record['accepted'])
        self.assertEqual(record['source_event_id'], 'source-0')
        self.assertEqual(record['summary'], '她明天考试')
        self.assertIn('ROLLBACK TO SAVEPOINT candidate_sticky', cursor.sql_text())
        seen = {}

        def capture(logged, **_kwargs):
            seen['record'] = logged
            return 4

        with patch('candidate_audit.log_candidate', capture):
            producer._audit_candidate_record(
                record, batch_id='batch-1', occurred_at=self.now,
                module_version='1.2', shadow=False)
        self.assertIs(seen['record'], record)
        self.assertTrue(seen['record']['accepted'])


@unittest.skipUnless(
    os.getenv('COGNITIVE_TEST_PGLITE'),
    'set COGNITIVE_TEST_PGLITE to the local test-only package')
class CandidateStickyVisibilityTests(unittest.TestCase):
    """Reader whitelist plus chat_log provenance for both sticky writers."""

    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def setUp(self):
        import cognitive_db
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
            status TEXT DEFAULT 'active', reply_to_event_id TEXT)''')
        self.database.commit()
        self.now = datetime.now(CN_TZ)

    def sql(self, statement, params=()):
        cur = self.database.cursor()
        cur.execute(statement, params)
        return cur.fetchall()

    def insert_chat(self, source_id, text):
        self.sql(
            '''INSERT INTO chat_log(user_id, chat_id, event_id, role, text)
               VALUES (%s, %s, %s, 'user', %s)''',
            ('u1', 'gojo', source_id, text))

    def visible(self):
        from cognitive_reader import list_user_facing_sticky_notes
        return list_user_facing_sticky_notes('u1', 'gojo', conn=self.database)

    def test_candidate_sticky_is_listed_until_the_source_changes(self):
        raw = '我明天要刷段'
        self.insert_chat('evt-raw', raw)
        cur = self.database.cursor()
        with patch('builtins.print'):
            row_id = project_candidate_sticky(
                cur, user_id='u1', character_id='gojo', event_id=11,
                source_id='evt-raw', now=self.now, source_text=raw,
                candidate={
                    'utterance_mode': 'sincere',
                    'persistence': 'transient',
                    'time_scope': '明天',
                    'summary': '她明天要刷段',
                })
        self.assertIsNotNone(row_id)
        self.assertEqual(
            self.sql("SELECT metadata->>'source_text' FROM cognitive_sticky_notes")[0][0],
            raw)
        notes = self.visible()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['id'], row_id)
        self.assertEqual(notes[0]['content'], '她明天要刷段')
        self.assertEqual(notes[0]['metadata']['projection_version'], 'sticky_candidate_v1')
        self.assertEqual(notes[0]['metadata']['source_text'], raw)

        self.sql("UPDATE chat_log SET text=%s WHERE event_id='evt-raw'", ('我明天要刷题',))
        self.assertEqual(self.visible(), [])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_sticky_notes')[0][0], 1)

        self.sql("UPDATE chat_log SET text=%s WHERE event_id='evt-raw'", (raw,))
        self.assertEqual(len(self.visible()), 1)

        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='evt-raw'")
        self.assertEqual(self.visible(), [])

        self.sql("UPDATE chat_log SET status='active' WHERE event_id='evt-raw'")
        restored = self.visible()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['metadata']['source_text'], raw)

    def test_evidence_v1_sticky_stays_visible(self):
        raw = '我明天考试。'
        self.insert_chat('evt-old', raw)
        cur = self.database.cursor()
        row_id = project_pending_sticky(
            cur, user_id='u1', character_id='gojo', event_id=12,
            source_id='evt-old', source_text=raw, now=self.now)
        self.assertIsNotNone(row_id)
        notes = self.visible()
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['id'], row_id)
        self.assertEqual(notes[0]['metadata']['projection_version'], 'sticky_evidence_v1')
        self.assertEqual(notes[0]['metadata']['source_text'], raw)
        self.assertIn(raw, notes[0]['content'])


if __name__ == '__main__':
    unittest.main()
