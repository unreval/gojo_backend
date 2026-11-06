"""Sticky notes projected from candidate time_scope, not keyword classification."""
import hashlib
import json
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))

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

    def project(self, candidate, source_id='evt-1'):
        return project_candidate_sticky(
            self.cursor, user_id='u1', character_id='gojo', event_id=11,
            source_id=source_id, candidate=candidate, now=self.now)

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


if __name__ == '__main__':
    unittest.main()
