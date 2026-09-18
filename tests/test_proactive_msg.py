# -*- coding: utf-8 -*-
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import proactive_msg  # noqa: E402


NOW = datetime(2026, 9, 18, 10, 0, tzinfo=timezone.utc)


class ProactiveFake:
    def __init__(self):
        self.rows = []
        self.next_id = 1
        self.executed = []
        self._one = None
        self._many = []

    def cursor(self):
        return self

    def commit(self):
        pass

    def close(self):
        pass

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        self._one = None
        self._many = []
        if compact.startswith('INSERT INTO proactive_msg'):
            if len(params) == 11:
                (character_id, user_id, kind, jp, zh, emotion, audio_b64,
                 created_at, event_id, assistant_turn_id, segment_index) = params
            else:
                (character_id, user_id, kind, jp, zh, emotion, audio_b64,
                 event_id, assistant_turn_id, segment_index) = params
                created_at = NOW
            row = {
                'id': self.next_id,
                'character_id': character_id,
                'user_id': user_id,
                'kind': kind,
                'jp': jp,
                'zh': zh,
                'emotion': emotion,
                'audio_b64': audio_b64,
                'created_at': created_at,
                'is_read': False,
                'event_id': event_id,
                'assistant_turn_id': assistant_turn_id,
                'segment_index': segment_index,
            }
            self.rows.append(row)
            self._one = (row['id'], row['created_at'])
            self.next_id += 1
            return
        if compact.startswith('SELECT id, character_id, kind'):
            user_id = params[0]
            character_id = params[1] if len(params) > 1 else None
            selected = []
            for row in self.rows:
                if row['user_id'] != user_id or row['is_read']:
                    continue
                if character_id and row['character_id'] != character_id:
                    continue
                selected.append((
                    row['id'], row['character_id'], row['kind'], row['jp'],
                    row['zh'], row['emotion'], row['audio_b64'],
                    row['created_at'], row['event_id'],
                    row['assistant_turn_id'], row['segment_index'],
                ))
            self._many = selected

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)


class ProactiveMsgIdentityTests(unittest.TestCase):
    def setUp(self):
        self.store = ProactiveFake()
        self.patcher = patch.object(
            proactive_msg, 'get_conn', return_value=self.store)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_init_adds_identity_columns(self):
        proactive_msg.init_proactive_table()
        sql = '\n'.join(item[0] for item in self.store.executed)
        self.assertIn(
            'ALTER TABLE proactive_msg ADD COLUMN IF NOT EXISTS event_id TEXT',
            sql,
        )
        self.assertIn(
            'ALTER TABLE proactive_msg ADD COLUMN IF NOT EXISTS assistant_turn_id TEXT',
            sql,
        )
        self.assertIn(
            'ALTER TABLE proactive_msg ADD COLUMN IF NOT EXISTS segment_index INTEGER',
            sql,
        )

    def test_get_pending_returns_stored_delayed_reply_ids(self):
        for index in range(3):
            proactive_msg.add_proactive_msg(
                'gojo', 'u', 'delayed_reply',
                f'jp{index}', f'zh{index}',
                event_id=f'delayed_reply:8:{index}',
                assistant_turn_id='delayed_reply:8:0',
                segment_index=index,
            )
        pending = proactive_msg.get_pending('u', 'gojo')
        self.assertEqual(
            [item['event_id'] for item in pending],
            ['delayed_reply:8:0', 'delayed_reply:8:1', 'delayed_reply:8:2'],
        )
        self.assertEqual(
            [item['assistant_turn_id'] for item in pending],
            ['delayed_reply:8:0'] * 3,
        )
        self.assertEqual([item['segment_index'] for item in pending], [0, 1, 2])
        for item in pending:
            self.assertFalse(item['event_id'].startswith('proactive:delayed_reply'))

    def test_legacy_proactive_without_event_id_stays_compatible(self):
        proactive_msg.add_proactive_msg('gojo', 'u', 'report', '任務は終わった。', '任务结束了')
        pending = proactive_msg.get_pending('u', 'gojo')
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['event_id'], 'proactive:report:1')
        self.assertEqual(pending[0]['assistant_turn_id'], 'proactive:report:1')
        self.assertEqual(pending[0]['segment_index'], 0)


if __name__ == '__main__':
    unittest.main()
