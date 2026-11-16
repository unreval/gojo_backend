"""User stickers stay out of candidate extraction.

A sticker row is kind='sticker' and its text is the description placeholder.
NULL kind is an old text row and stays eligible. Stickers are not reserved,
so they do not consume the daily batch budget or form a batch by themselves.
"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))
import candidate_producer as producer
from structured_output import invoke_structured_llm as real_invoke
from tests import test_memory_candidate_source as source_fixture


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'requires disposable local PGlite')
class StickerPendingTests(unittest.TestCase):
    setUpClass = classmethod(source_fixture.CandidateSourceTests.setUpClass.__func__)
    tearDownClass = classmethod(source_fixture.CandidateSourceTests.tearDownClass.__func__)
    tearDown = source_fixture.CandidateSourceTests.tearDown
    sql = source_fixture.CandidateSourceTests.sql

    def setUp(self):
        source_fixture.CandidateSourceTests.setUp(self)
        import raw_events
        processing = [ddl for ddl in raw_events.RAW_EVENT_DDL
                      if 'CREATE TABLE IF NOT EXISTS event_processing_state' in ddl]
        self.assertEqual(len(processing), 1)
        self.sql(processing[0])
        self.database.commit()
        self.now = datetime(2026, 11, 16, 4, tzinfo=timezone.utc)
        self.logs = []

    def insert(self, source_id, text, *, kind='text', age=0, user='u', character='c'):
        created = self.now - timedelta(seconds=age)
        self.sql('''INSERT INTO chat_log
            (user_id, chat_id, event_id, role, text, kind, created_at, status)
            VALUES (%s, %s, %s, 'user', %s, %s, %s, 'active')''',
            (user, character, source_id, text, kind, created))
        self.sql('''INSERT INTO cognitive_events
            (user_id, character_id, source_event_type, source_event_id, source,
             occurred_at, payload)
            VALUES (%s, %s, 'canonical_user_turn', %s, 'chat', %s, %s::jsonb)''',
            (user, character, source_id, created,
             json.dumps({'content': text}, ensure_ascii=False)))

    def pending(self, *, user='u', character='c'):
        return producer._pending_inputs(
            self.database.cursor(), user, character,
            producer._receipt_version(True), None)

    def budget(self):
        return producer._load_control(
            self.database.cursor(), 'candidate-producer:global-budget')

    def test_sticker_is_absent_and_text_and_null_kind_stay(self):
        self.insert('text-1', '今天我亏了69元，想和你聊一聊。', kind='text', age=10)
        self.insert('old-1', '昨天我也亏了69元，想和你聊一聊。', kind=None, age=20)
        self.insert('stick-1', '【表情】无语', kind='sticker', age=5)
        ids = [item['source_event_id'] for item in self.pending()]
        self.assertEqual(ids, ['old-1', 'text-1'])
        self.assertNotIn('stick-1', ids)

    def test_stickers_do_not_make_a_young_batch_ready_or_spend_quota(self):
        self.insert('text-1', '今天我亏了69元，想和你聊一聊。', kind='text', age=10)
        for index in range(8):
            self.insert(
                f'stick-{index}', f'【表情】表情{index}', kind='sticker', age=index)
        pending = self.pending()
        self.assertEqual([item['source_event_id'] for item in pending], ['text-1'])
        self.assertFalse(producer.batch_ready(pending, self.now))
        model = Mock(return_value=('[]', {'stop_reason': 'end_turn'}))
        result = producer.run_pair(
            'u', 'c', now=self.now, create_chat_fn=model, conn=self.database,
            emit=self.logs.append, enabled=True, shadow=True)
        self.assertEqual(result['status'], 'waiting')
        self.assertEqual(result['input_count'], 1)
        model.assert_not_called()
        self.assertEqual(self.budget(), {})
        receipts = self.sql(
            '''SELECT event_id FROM event_processing_state
               WHERE event_id LIKE 'stick-%' ''')
        self.assertEqual(receipts, [])

    def test_one_old_text_is_one_batch_beside_stickers(self):
        self.insert('text-1', '今天我亏了69元，想和你聊一聊。', kind='text', age=300)
        self.insert('stick-1', '【表情】无语', kind='sticker', age=400)
        pending = self.pending()
        self.assertEqual([item['source_event_id'] for item in pending], ['text-1'])
        self.assertTrue(producer.batch_ready(pending, self.now))
        model = Mock(return_value=('[]', {'stop_reason': 'end_turn'}))
        with patch('structured_output.invoke_structured_llm', wraps=real_invoke):
            result = producer.run_pair(
                'u', 'c', now=self.now, create_chat_fn=model, conn=self.database,
                emit=self.logs.append, enabled=True, shadow=True)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['batch_size'], 1)
        model.assert_called_once()
        payload = json.loads(model.call_args.kwargs['messages'][0]['content'])
        self.assertEqual([item['source_event_id'] for item in payload], ['text-1'])
        self.assertEqual(self.budget().get('batches'), 1)
        self.assertEqual(self.sql(
            '''SELECT event_id FROM event_processing_state
               WHERE event_id LIKE 'stick-%' '''), [])


if __name__ == '__main__':
    unittest.main()
