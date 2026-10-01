"""Receipt commit is server persistence, never delivery or read acknowledgement."""
import asyncio
from contextlib import contextmanager
import json
import unittest
from unittest.mock import Mock, patch

from tests import test_private_source_first as source_first
import db_generation_receipt as receipt
import generation_effects
import user_memory


class ReceiptWindowTests(unittest.TestCase):
    setUpClass = classmethod(source_first.PrivateSourceFirstTests.setUpClass.__func__)
    tearDownClass = classmethod(source_first.PrivateSourceFirstTests.tearDownClass.__func__)
    setUp = source_first.PrivateSourceFirstTests.setUp
    tearDown = source_first.PrivateSourceFirstTests.tearDown
    sql = source_first.PrivateSourceFirstTests.sql
    save = source_first.PrivateSourceFirstTests.save
    drain_jobs = source_first.PrivateSourceFirstTests.drain_jobs
    run_cycle = source_first.PrivateSourceFirstTests.run_cycle

    @contextmanager
    def http(self, reply='了解。'):
        import route_chat
        from starlette.responses import JSONResponse
        with patch.object(route_chat, 'JSONResponse', JSONResponse), patch.object(
                route_chat, 'save_user_short_memory_once', user_memory.save_user_short_memory_once), patch.object(
                route_chat, 'get_character', return_value={'voice_id': 'offline'}), patch.object(
                route_chat, 'get_temporal_snapshot', return_value={}), patch(
                'reply_availability.check_reply_availability', return_value={'can_reply': True}), patch.object(
                route_chat, 'update_chat_days', return_value=1), patch.object(
                route_chat, 'get_short_memory', return_value=[]), patch.object(
                route_chat, '_turn_context', return_value=(None, [])), patch.object(
                route_chat, 'build_system_blocks', return_value=[]), patch.object(
                route_chat, 'log_cache_usage'), patch.object(
                route_chat, '_create_json', return_value=(json.dumps({
                    'messages': [{'jp': reply, 'zh': reply}]}), Mock())) as generate, patch.object(
                route_chat, '_quick_translate', side_effect=AssertionError('translation not expected')), patch.object(
                route_chat, '_commit_offline_state'), patch.object(
                route_chat, '_reject_schedule_candidate', return_value=None), patch.object(
                route_chat, '_commit_schedule_candidate', return_value={'ok': True, 'noop': True}), patch.object(
                receipt, 'GenerationHeartbeat'), patch('tts.tts_to_b64', return_value=''), patch(
                'characters.get_character', return_value={'voice_id': 'offline'}), patch(
                'db_chat_media.get_media_for_source_event', return_value=None):
            yield route_chat, generate

    def request(self, source):
        return {'user_id': 'u', 'character_id': 'c', 'source_event_id': source,
                'text': '我的职业是「教师」。'}

    def assistant_effect(self, source):
        return next(row for row in receipt.list_side_effects('u', 'c', source, 'chat_text')
                    if row['effect'] == 'assistant_short_memory')

    def test_A_abort_before_receipt_commit_keeps_only_user_evidence(self):
        with self.http() as (route, generate), patch.object(receipt, 'CRASH_BEFORE_COMPLETE', True):
            with self.assertRaisesRegex(RuntimeError, 'injected crash before complete'):
                asyncio.run(route.chat_text(self.request('window-a')))
            generate.assert_called_once()
        self.drain_jobs()
        self.run_cycle()
        self.assertEqual(self.sql("SELECT role FROM chat_log"), [('user',)])
        self.assertEqual(self.sql("SELECT role FROM short_memory"), [('user',)])
        self.assertEqual(self.sql("SELECT source_event_type FROM cognitive_events"), [('canonical_user_turn',)])
        self.assertEqual(self.sql("SELECT count(*) FROM chat_generation_side_effect")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM bond_memory")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_beliefs")[0][0], 1)
        self.assertNotEqual(receipt.get_generation('u', 'c', 'window-a', 'chat_text')['status'], 'completed')

    def test_B_disconnect_after_commit_replays_same_response_without_generator(self):
        from generation_side_effect_worker import process_one_side_effect
        with self.http() as (route, generate):
            response = asyncio.run(route.chat_text(self.request('window-b')))
            self.assertEqual(response.status_code, 200)
            stored = receipt.get_generation('u', 'c', 'window-b', 'chat_text')
            self.assertEqual(stored['status'], 'completed')
            async def disconnected_send(message):
                raise ConnectionResetError('client connection lost before response delivery')
            with self.assertRaises(ConnectionResetError):
                asyncio.run(response({'type': 'http'}, None, disconnected_send))
            replay = asyncio.run(route.chat_text(self.request('window-b')))
            self.assertEqual(json.loads(response.body), json.loads(replay.body))
            generate.assert_called_once()
        self.assertTrue(process_one_side_effect(self.assistant_effect('window-b')))
        self.assertFalse(process_one_side_effect(self.assistant_effect('window-b')))
        self.drain_jobs()
        self.run_cycle()
        self.run_cycle()
        self.assertEqual(self.sql("SELECT count(*) FROM chat_log WHERE role='gojo'")[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM memory_jobs WHERE source_event_id='chat_reply:window-b'")[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM bond_memory")[0][0], 1)
        self.assertFalse({'delivered_at', 'received_at', 'read_at'} & stored.keys())

    def test_C_repair_interrupted_assistant_effect_keeps_event_job_projection_once(self):
        from generation_side_effect_worker import process_one_side_effect
        with self.http() as (route, generate):
            response = asyncio.run(route.chat_text(self.request('window-c')))
            self.assertEqual(response.status_code, 200)
            generate.assert_called_once()
        with patch.object(receipt, 'CRASH_AFTER_EFFECT', 'assistant_short_memory'):
            with self.assertRaisesRegex(RuntimeError, 'injected crash after'):
                process_one_side_effect(self.assistant_effect('window-c'))
        self.assertEqual(self.assistant_effect('window-c')['status'], 'processing')
        self.drain_jobs()
        self.run_cycle()
        self.run_cycle()
        tables = ('chat_log', 'short_memory', 'memory_jobs', 'cognitive_events',
                  'cognitive_event_triggers', 'cognitive_beliefs', 'bond_memory', 'memory_source_events')
        before = {table: self.sql(f'SELECT * FROM {table} ORDER BY 1') for table in tables}
        effect_key_sql = ("SELECT user_id,character_id,source_event_id,endpoint,effect "
                          "FROM chat_generation_side_effect WHERE effect='assistant_short_memory'")
        effect_id = self.sql(effect_key_sql)
        self.sql("UPDATE chat_generation_side_effect SET claim_expires_at=NOW()-INTERVAL '1 minute' WHERE effect='assistant_short_memory'")
        self.database.commit()
        payload = receipt.get_generation('u', 'c', 'window-c', 'chat_text')['response_json']
        generation_effects.repair_completed_generation('u', 'c', 'window-c', 'chat_text', payload)
        self.assertTrue(process_one_side_effect(self.assistant_effect('window-c')))
        self.assertFalse(process_one_side_effect(self.assistant_effect('window-c')))
        self.drain_jobs()
        self.assertEqual(before, {table: self.sql(f'SELECT * FROM {table} ORDER BY 1') for table in tables})
        self.assertEqual(self.sql(effect_key_sql), effect_id)
        self.assertEqual(self.assistant_effect('window-c')['attempt_count'], 2)
        self.assertEqual(self.sql("SELECT count(*) FROM bond_memory")[0][0], 1)

    def test_D_nickname_answer_receipt_repair_keeps_single_binding(self):
        from generation_side_effect_worker import process_one_side_effect
        from cognitive_reader import fetch_cognitive_reader_state
        request = self.request('phase-d')
        request['text'] = '我还是叫你宝宝。你接不接受这个称呼？'
        with self.http(reply='yes') as (route, generate):
            self.assertEqual(asyncio.run(route.chat_text(request)).status_code,200)
            generate.assert_called_once()
        with patch.object(receipt,'CRASH_AFTER_EFFECT','assistant_short_memory'):
            with self.assertRaisesRegex(RuntimeError,'injected crash after'):
                process_one_side_effect(self.assistant_effect('phase-d'))
        self.drain_jobs()
        self.run_cycle()
        self.run_cycle()
        question = fetch_cognitive_reader_state('u','c',conn=self.database)['questions'][0]
        self.assertEqual(question['metadata']['resolution']['value'],'yes')
        self.assertEqual(question['metadata']['resolution']['actor'],'character')
        tables=('chat_log','cognitive_events','cognitive_questions','bond_memory','memory_source_events')
        before={table:self.sql(f'SELECT * FROM {table} ORDER BY 1') for table in tables}
        self.sql("UPDATE chat_generation_side_effect SET claim_expires_at=NOW()-INTERVAL '1 minute' WHERE effect='assistant_short_memory'")
        self.database.commit()
        payload=receipt.get_generation('u','c','phase-d','chat_text')['response_json']
        generation_effects.repair_completed_generation('u','c','phase-d','chat_text',payload)
        self.assertTrue(process_one_side_effect(self.assistant_effect('phase-d')))
        self.drain_jobs()
        self.assertEqual(before,{table:self.sql(f'SELECT * FROM {table} ORDER BY 1') for table in tables})
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0],0)
