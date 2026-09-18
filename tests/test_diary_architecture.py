# -*- coding: utf-8 -*-
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

import cognitive_output  # noqa: E402
import diary_engine  # noqa: E402
import memory_lifecycle  # noqa: E402


NOW = datetime(2026, 9, 18, 12, 0, tzinfo=timezone.utc)


class DupDiaryCursor:
    def __init__(self, rows):
        self.rows = rows
        self.inserted = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        if 'FROM cognitive_diary_entries' in compact and compact.startswith('SELECT'):
            self._many = self.rows
        elif compact.startswith('INSERT INTO cognitive_diary_entries'):
            self.inserted.append(params)
            self._many = []
        else:
            self._many = []

    def fetchall(self):
        return list(getattr(self, '_many', []))

    def fetchone(self):
        return None


class DiaryArchitectureTests(unittest.TestCase):
    def test_maybe_write_diary_on_event_is_noop(self):
        self.assertIsNone(
            diary_engine.maybe_write_diary_on_event('gojo', 'u', '大事', '回复'))

    def test_route_chat_no_longer_calls_event_diary_judge(self):
        src = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        self.assertNotIn('maybe_write_diary_on_event', src)
        self.assertIn('diary_entries', src)

    def test_duplicate_important_thought_is_skipped(self):
        existing = [(
            'exam.stress',
            json.dumps([{'event_id': 27, 'reason': '她提到考试'}], ensure_ascii=False),
        )]
        cur = DupDiaryCursor(existing)
        same_key = {
            'diary_key': 'exam.stress',
            'content': '又想起考试',
            'evidence_refs': [27],
        }
        self.assertTrue(cognitive_output._diary_entry_is_duplicate(
            cur, 'u', 'gojo', same_key, [{'event_id': 27}]))
        overlap = {
            'diary_key': 'exam.stress.v2',
            'content': '同一场考试',
            'evidence_refs': [27],
        }
        self.assertTrue(cognitive_output._diary_entry_is_duplicate(
            cur, 'u', 'gojo', overlap, [{'event_id': 27}]))
        fresh = {
            'diary_key': 'other.topic',
            'content': '另一件事',
            'evidence_refs': [99],
        }
        self.assertFalse(cognitive_output._diary_entry_is_duplicate(
            cur, 'u', 'gojo', fresh, [{'event_id': 99}]))

    def test_slow_loop_diary_entries_still_persist(self):
        output = {
            'question_updates': [],
            'belief_updates': [],
            'hypothesis_updates': [],
            'new_predictions': [],
            'sticky_note_updates': [],
            'evidence_refs': [{'event_id': 27, 'reason': '本轮提到考试'}],
            'diary_entries': [{
                'diary_key': 'exam.noticed',
                'content': '她今天把考试说得很轻，但我听得出压力。',
                'reflection_kind': 'event',
                'evidence_refs': [27],
            }],
        }
        cur = DupDiaryCursor([])

        class _Cur(DupDiaryCursor):
            def execute(self, sql, params=None):
                compact = ' '.join(sql.split())
                if compact.startswith('SELECT id, source_event_type'):
                    self._many = [(
                        27, 'user_message', 'e27', 'chat',
                        {'evidence_category': 'user_statement'},
                    )]
                    return
                if compact.startswith('SELECT diary_key'):
                    self._many = []
                    return
                if compact.startswith('INSERT INTO cognitive_diary_entries'):
                    self.inserted.append(params)
                    self._many = []
                    return
                self._many = []

        persist_cur = _Cur([])
        cognitive_output.persist_slow_loop_output(
            persist_cur, cycle_id=3, user_id='u', character_id='gojo',
            output=output, now=NOW)
        self.assertEqual(len(persist_cur.inserted), 1)
        self.assertEqual(persist_cur.inserted[0][2], 'exam.noticed')
        self.assertNotIn('char_diary', persist_cur.inserted[0])

    def test_important_thought_does_not_use_daily_diary_quota(self):
        src = Path(BACKEND, 'cognitive_output.py').read_text(encoding='utf-8')
        self.assertIn('INSERT INTO cognitive_diary_entries', src)
        self.assertNotIn('add_char_diary', src)
        self.assertNotIn('count_char_diaries_since', src)
        daily = Path(BACKEND, 'diary_scheduler.py').read_text(encoding='utf-8')
        self.assertIn('count_char_diaries_since', daily)
        self.assertIn('>= 1', daily)

    def test_daily_diary_quota_skips_second_write(self):
        fake_diary = types.SimpleNamespace(
            count_char_diaries_since=Mock(return_value=1),
            add_char_diary=Mock(return_value=(1, NOW)),
            has_named_self=Mock(return_value=True),
        )
        with patch.object(diary_engine, 'db_diary', fake_diary), \
             patch.object(diary_engine, 'get_character',
                          return_value={'name': '五条'}), \
             patch('ai_client.create_chat') as create_chat:
            result = diary_engine.generate_char_diary('gojo', 'u')
        self.assertIsNone(result)
        create_chat.assert_not_called()
        fake_diary.add_char_diary.assert_not_called()

    def test_daily_diary_reads_day_raw_events_not_last_eight_shorts(self):
        events = [
            {
                'role': 'user', 'content': f'事件{i}',
                'timestamp': NOW.replace(hour=9 + i),
            }
            for i in range(12)
        ]
        notes = {
            'cycles': [{'summary': '今天她提起搬家', 'salient_change': '第一次说想走'}],
            'questions': [{'question_text': '她是不是真的要走？', 'status': 'active'}],
            'settled_predictions': [{
                'status': 'fulfilled', 'statement': '她会再提起考试',
            }],
        }
        with patch('raw_events.list_events_for_local_day', return_value=events), \
             patch('cognitive_reader.list_day_cycle_notes', return_value=notes):
            event_text, cycle_text, n = diary_engine._gather_daily_diary_material(
                'gojo', 'u', NOW)
        self.assertEqual(n, 12)
        self.assertIn('事件0', event_text)
        self.assertIn('事件11', event_text)
        self.assertIn('今天她提起搬家', cycle_text)
        self.assertIn('她是不是真的要走', cycle_text)
        gather_src = Path(BACKEND, 'diary_engine.py').read_text(encoding='utf-8')
        self.assertIn('list_events_for_local_day', gather_src)
        self.assertNotIn('get_short_memory(user_id, 8', gather_src)

    def test_overlapping_daily_and_important_thought_recall_dedups(self):
        shared = '她今天说考试已经过了，我当时却还在担心她没考好。'
        items = [
            {
                'source_type': 'reflection',
                'content': shared,
                'source_event_refs': [{'event_id': '27'}],
            },
            {
                'source_type': 'diary',
                'content': shared,
                'source_event_refs': [{'source_type': 'char_diary', 'source_id': 3}],
            },
        ]
        kept = memory_lifecycle._collapse_overlapping_diary_sources(items)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]['source_type'], 'reflection')

    def test_same_source_diaries_are_not_collapsed_by_cross_source_rule(self):
        items = [
            {'source_type': 'diary', 'content': '考试那天我有点担心。',
             'source_event_refs': [{'source_id': 1}]},
            {'source_type': 'diary', 'content': '考试那天我有点担心。',
             'source_event_refs': [{'source_id': 2}]},
        ]
        kept = memory_lifecycle._collapse_overlapping_diary_sources(items)
        self.assertEqual(len(kept), 2)


if __name__ == '__main__':
    unittest.main()
