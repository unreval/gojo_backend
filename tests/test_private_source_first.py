"""Source + durable job acceptance on disposable PostgreSQL; no model authority."""
import asyncio
import json
import unittest
from unittest.mock import Mock, patch

from tests import test_cognitive_deterministic as hard
import db_chatlog
import db_generation_receipt as receipt
import generation_effects
import memory_jobs
import raw_events
import user_memory


class PrivateSourceFirstTests(unittest.TestCase):
    setUpClass = classmethod(hard.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(hard.OfflineDatabaseTests.tearDownClass.__func__)
    tearDown = hard.OfflineDatabaseTests.tearDown
    sql = hard.OfflineDatabaseTests.sql
    run_cycle = hard.OfflineDatabaseTests.run_cycle

    def setUp(self):
        hard.OfflineDatabaseTests.setUp(self)
        for module in (db_chatlog, receipt, memory_jobs, user_memory):
            self.stack.enter_context(patch.object(module, 'get_conn', return_value=self.database))
        self.sql('DROP TABLE chat_log')
        db_chatlog.init_chatlog_table()
        memory_jobs.init_memory_jobs_table()
        receipt.init_generation_receipt_table()
        self.sql("""CREATE TABLE short_memory (
            id SERIAL PRIMARY KEY, user_id TEXT, character_id TEXT, role TEXT,
            content TEXT, source_event_id TEXT, event_meta TEXT DEFAULT '',
            timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP)""")
        self.sql("""CREATE UNIQUE INDEX short_once ON short_memory
            (user_id, character_id, role, source_event_id) WHERE source_event_id IS NOT NULL""")
        self.database.commit()

    def save(self, source='user-1', content='我的职业是「教师」。'):
        return user_memory.save_user_short_memory_once('u', content, 'c', source_event_id=source)

    def drain_jobs(self):
        while True:
            row = memory_jobs._claim_one()
            if not row:
                break
            memory_jobs._run_job(row)
        self.assertEqual(self.sql("SELECT count(*) FROM memory_jobs WHERE status <> 'done'")[0][0], 0)

    def test_raw_and_job_survive_cache_failure(self):
        self.sql('DROP TABLE short_memory')
        self.database.commit()
        self.assertFalse(self.save())
        self.assertEqual(self.sql('SELECT count(*) FROM chat_log')[0][0], 1)
        self.assertEqual(self.sql("SELECT kind,status FROM memory_jobs"), [('canonical_turn', 'pending')])
        self.drain_jobs()
        self.run_cycle()
        self.assertIn('教师', self.sql('SELECT statement FROM cognitive_beliefs')[0][0])

    def test_raw_failure_never_writes_cache_or_evidence(self):
        self.sql("ALTER TABLE chat_log ADD CONSTRAINT reject_raw CHECK (text = '')")
        self.database.commit()
        with self.assertRaises(Exception):
            self.save()
        self.assertEqual(self.sql('SELECT count(*) FROM short_memory')[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM memory_jobs')[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 0)

    def test_job_failure_rolls_back_raw_before_cache(self):
        self.sql("ALTER TABLE memory_jobs ADD CONSTRAINT reject_job CHECK (kind = 'impossible')")
        self.database.commit()
        with self.assertRaises(Exception):
            self.save()
        self.assertEqual(self.sql('SELECT count(*) FROM chat_log')[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM short_memory')[0][0], 0)

    def test_retry_after_job_done_is_exactly_once(self):
        self.assertTrue(self.save())
        self.drain_jobs()
        self.run_cycle()
        self.assertFalse(self.save())
        self.drain_jobs()
        for table in ('chat_log', 'short_memory', 'memory_jobs', 'cognitive_events',
                      'cognitive_event_triggers', 'cognitive_cycles', 'cognitive_beliefs'):
            self.assertEqual(self.sql(f'SELECT count(*) FROM {table}')[0][0], 1, table)

    def test_retry_cannot_change_canonical_content(self):
        self.save()
        with self.assertRaises(raw_events.SourceValidityError):
            self.save(content='我的职业是「医生」。')
        self.assertEqual(self.sql('SELECT text FROM chat_log'), [('我的职业是「教师」。',)])
        self.assertEqual(self.sql('SELECT content FROM short_memory'), [('我的职业是「教师」。',)])

    def test_deleted_source_cannot_be_resurrected_by_retry(self):
        self.save()
        self.sql("UPDATE chat_log SET status='deleted'")
        self.database.commit()
        with self.assertRaises(raw_events.SourceValidityError):
            self.save()
        self.assertEqual(self.sql("SELECT count(*) FROM chat_log WHERE status='active'")[0][0], 0)

    def test_failed_generation_preserves_user_evidence_without_phantom_assistant(self):
        import route_chat
        for failure in ('empty', 'malformed', 'provider'):
            with self.subTest(failure=failure), patch.object(
                    route_chat, 'save_user_short_memory_once', user_memory.save_user_short_memory_once), patch.object(
                    route_chat, 'get_character', return_value={'voice_id': 'offline'}), patch.object(
                    route_chat, 'get_temporal_snapshot', return_value={}), patch(
                    'reply_availability.check_reply_availability', return_value={'can_reply': True}), patch.object(
                    route_chat, 'update_chat_days', return_value=1), patch.object(
                    route_chat, 'get_short_memory', return_value=[]), patch.object(
                    route_chat, '_turn_context', return_value=(None, [])), patch.object(
                    route_chat, 'build_system_blocks', return_value=[]), patch.object(
                    route_chat, 'log_cache_usage'), patch.object(
                    route_chat, '_create_json', side_effect=(
                        RuntimeError('offline provider error') if failure == 'provider' else None),
                    return_value=('' if failure == 'empty' else '{"messages":[', Mock())) as generate, patch.object(
                    receipt, 'GenerationHeartbeat'):
                response = asyncio.run(route_chat.chat_text({
                    'user_id': 'u', 'character_id': 'c',
                    'source_event_id': failure, 'text': '我的职业是「教师」。'}))
                self.assertEqual(response.status_code, 502)
                self.assertEqual(generate.call_count, 3)
        self.drain_jobs()
        self.run_cycle()
        self.assertEqual(self.sql("SELECT count(*) FROM chat_log WHERE role='user'")[0][0], 3)
        self.assertEqual(self.sql("SELECT count(*) FROM chat_log WHERE role <> 'user'")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM short_memory WHERE role='assistant'")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_events WHERE source_event_type='canonical_user_turn'")[0][0], 3)
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_events WHERE source_event_type='canonical_assistant_turn'")[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM chat_generation_side_effect')[0][0], 0)
        self.assertEqual(self.sql('SELECT count(*) FROM bond_memory')[0][0], 0)
        self.assertNotIn('chat_reply:', json.dumps(self.sql('SELECT evidence_refs FROM cognitive_beliefs')))
        self.assertTrue(all(e['role'] == 'user' for e in raw_events.get_recent_events('u', 'c')))

    def test_completed_receipt_repairs_one_assistant_job_without_user_reingress(self):
        self.save()
        self.drain_jobs()
        gate = receipt.resolve_generation('u', 'c', 'user-1', 'chat_text')
        payload = {'messages': [{'jp': '了解。', 'zh': '知道了。'}], '_user_text': '我的职业是「教师」。'}
        effects = receipt.needed_effects('chat_text', payload)
        self.assertNotIn('private_extraction', effects)
        self.assertNotIn('relationship_update', effects)
        self.assertTrue(receipt.complete_generation(
            'u', 'c', 'user-1', 'chat_text', gate['claim_token'], payload, effects=effects))
        ctx = {'user_id': 'u', 'character_id': 'c', 'source_event_id': 'user-1',
               'endpoint': 'chat_text', 'payload': payload}
        for _ in range(2):
            receipt.ensure_completed_generation_effects(
                'u', 'c', 'user-1', 'chat_text', ctx=ctx,
                apply_fn=lambda effect, context: generation_effects.apply_assistant_short_memory(context)
                if effect == 'assistant_short_memory' else {})
        self.drain_jobs()
        self.run_cycle()
        self.assertEqual(self.sql('SELECT source_event_id FROM memory_jobs ORDER BY id'),
                         [('user-1',), ('chat_reply:user-1',)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 2)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_event_triggers')[0][0], 2)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM short_memory WHERE role='assistant'")[0][0], 1)

    def test_http_nonverbal_replay_has_one_user_judgment_and_one_assistant_utterance(self):
        import route_chat
        with patch.object(route_chat, 'save_user_short_memory_once', user_memory.save_user_short_memory_once), patch.object(
                route_chat, 'get_character', return_value={'voice_id': 'offline'}), patch.object(
                route_chat, 'get_temporal_snapshot', return_value={}), patch(
                'reply_availability.check_reply_availability', return_value={'can_reply': True}), patch.object(
                route_chat, 'update_chat_days', return_value=1), patch.object(
                route_chat, 'get_short_memory', return_value=[]), patch.object(
                route_chat, '_turn_context', return_value=(None, [])), patch.object(
                route_chat, 'build_system_blocks', return_value=[]), patch.object(
                route_chat, 'log_cache_usage'), patch.object(
                route_chat, '_create_json', return_value=('🥺...', Mock())) as generate, patch.object(
                route_chat, '_quick_translate', side_effect=AssertionError('nonverbal translation')) as translate, patch.object(
                route_chat, '_commit_offline_state') as state, patch.object(
                route_chat, '_reject_schedule_candidate', return_value=None), patch.object(
                route_chat, '_commit_schedule_candidate', return_value={'ok': True, 'noop': True}), patch.object(
                receipt, 'GenerationHeartbeat'), patch('tts.tts_to_b64', return_value=''), patch(
                'characters.get_character', return_value={'voice_id': 'offline'}), patch(
                'db_chat_media.get_media_for_source_event', return_value=None):
            request = {'user_id': 'u', 'character_id': 'c', 'source_event_id': 'http',
                       'text': '我的职业是「教师」。'}
            for _ in range(2):
                response = asyncio.run(route_chat.chat_text(request))
                self.assertEqual(response.status_code, 200, response.body)
                self.assertEqual(json.loads(response.body)['messages'][0]['jp'], '🥺...')
            generate.assert_called_once()
            translate.assert_not_called()
            self.assertTrue(all(call.args[-1] is None for call in state.call_args_list))
        effects = receipt.list_side_effects('u', 'c', 'http', 'chat_text')
        self.assertTrue(all(row['effect'] not in ('private_extraction', 'relationship_update', 'promise_detector')
                            for row in effects))
        from generation_side_effect_worker import process_one_side_effect
        assistant = next(row for row in effects if row['effect'] == 'assistant_short_memory')
        self.assertTrue(process_one_side_effect(assistant))
        self.drain_jobs()
        self.run_cycle()
        self.run_cycle()
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 2)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_event_triggers')[0][0], 2)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_events WHERE source_event_type='canonical_user_turn'")[0][0], 1)
        self.assertEqual(self.sql("SELECT count(*) FROM chat_log WHERE role='gojo'")[0][0], 1)

    def test_canonical_job_retry_preserves_error_and_applies_once(self):
        self.save()
        row = memory_jobs._claim_one()
        with patch('cognitive_events.ingest_canonical_turn', side_effect=RuntimeError('private secret')):
            memory_jobs._run_job(row)
        self.assertEqual(self.sql('SELECT status,last_error FROM memory_jobs'), [('pending', 'job_exception')])
        self.drain_jobs()
        self.run_cycle()
        self.assertEqual(self.sql('SELECT attempts FROM memory_jobs'), [(2,)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 1)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_event_triggers')[0][0], 1)

    def test_late_summary_cannot_reactivate_deleted_or_superseded_sources(self):
        import context_layer
        import rolling_summary
        context_layer.init_context_layer_tables()
        for status in ('deleted', 'superseded'):
            self.save(source=status)
            self.drain_jobs()
            self.run_cycle()

            def generate(*_args, **_kwargs):
                if status == 'deleted':
                    self.sql("UPDATE chat_log SET status='deleted' WHERE event_id=%s", (status,))
                else:
                    self.sql("""UPDATE cognitive_events
                        SET adjudication=jsonb_set(adjudication,'{status}','"superseded"')
                        WHERE source_event_id=%s""", (status,))
                self.database.commit()
                return '晚到的摘要'

            with patch.object(rolling_summary, 'generate_real_summary_text', side_effect=generate), patch.object(
                    rolling_summary, '_enqueue_episode_index') as episode:
                self.assertTrue(rolling_summary.process_summary_job('u', 'c', {
                    'source_event_ids': [status],
                    'events': [{'event_id': status, 'role': 'user', 'content': '原文'}]}))
            episode.assert_not_called()
            self.assertEqual(self.sql("SELECT count(*) FROM rolling_summaries WHERE status='active'")[0][0], 0)
        self.assertEqual(self.sql("SELECT count(*) FROM rolling_summaries WHERE status='superseded'")[0][0], 2)
