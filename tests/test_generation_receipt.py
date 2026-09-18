import asyncio
import importlib.util
import json
import os
import sys
import threading
import time
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
FRONTEND_CHAT = os.path.join(ROOT, 'app', 'chat', '[id].tsx')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
if os.path.dirname(__file__) not in sys.path:
    sys.path.insert(0, os.path.dirname(__file__))

import db_generation_receipt as receipt  # noqa: E402


class _PassthroughHeartbeat:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        return self

    def stop(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


_TTS_PATCH = None


def setUpModule():
    import tts as _tts
    global _TTS_PATCH
    _TTS_PATCH = patch.object(_tts, 'tts_to_b64', Mock(return_value='audio'))
    _TTS_PATCH.start()


def tearDownModule():
    from generation_side_effect_worker import stop_generation_side_effect_worker
    stop_generation_side_effect_worker()
    receipt.stop_all_generation_heartbeats()
    try:
        _TTS_PATCH.stop()
    except Exception:
        pass


PNG_B64 = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ'
    '/pLvAAAAAElFTkSuQmCC'
)


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


def chat_reply(jp, zh, emotion='平静', extra=None, messages=None):
    body = {
        'emotion': emotion,
        'messages': messages or [{'jp': jp, 'zh': zh}],
    }
    if extra:
        body.update(extra)
    return json.dumps(body, ensure_ascii=False)


class ReceiptStore:
    def __init__(self):
        self.lock = threading.Lock()
        self.receipts = {}
        self.effects = {}
        self.tasks = []
        self.jobs = []
        self.promises = []
        self.turn_once = set()
        self.cancels = set()
        self.next_id = 1
        self.next_task = 1
        self.next_job = 1
        self.next_promise = 1


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self.rowcount = 0
        self._one = None
        self._many = []

    def close(self):
        pass

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)

    def execute(self, sql, params=None):
        with self.store.lock:
            self._execute_unlocked(sql, params)

    def _execute_unlocked(self, sql, params=None):
        compact = ' '.join(sql.split())
        params = tuple(params or ())
        self.rowcount = 0
        self._one = None
        self._many = []
        now = datetime.now(timezone.utc)

        if compact.startswith('CREATE TABLE') or compact.startswith('CREATE INDEX') \
                or compact.startswith('CREATE UNIQUE') or compact.startswith('ALTER TABLE') \
                or compact.startswith('SAVEPOINT') or compact.startswith('RELEASE') \
                or compact.startswith('ROLLBACK TO'):
            return

        if compact.startswith("UPDATE chat_generation_side_effect SET status='completed'") \
                and 'WHERE status IS NULL' in compact:
            return

        if 'INSERT INTO chat_generation_receipt' in compact:
            key = (params[0], params[1], params[2], params[3])
            token = params[4]
            lease = int(params[5])
            row = self.store.receipts.get(key)
            can_take = False
            if row is None:
                can_take = True
            elif row['status'] == 'failed':
                can_take = True
            elif row['status'] == 'processing' and row['claim_expires_at'] < now:
                can_take = True
            if can_take:
                rec = {
                    'id': row['id'] if row else self.store.next_id,
                    'status': 'processing',
                    'claim_token': token,
                    'response_json': None,
                    'claim_expires_at': now + timedelta(seconds=lease),
                    'last_error': None,
                    'user_id': params[0],
                    'character_id': params[1],
                    'source_event_id': params[2],
                    'endpoint': params[3],
                }
                if row is None:
                    self.store.next_id += 1
                self.store.receipts[key] = rec
                self._one = (
                    rec['id'], rec['status'], rec['claim_token'],
                    rec['response_json'], rec['claim_expires_at'],
                )
                self.rowcount = 1
            return

        if 'FROM chat_generation_receipt' in compact and compact.startswith('SELECT'):
            key = (params[0], params[1], params[2], params[3])
            rec = self.store.receipts.get(key)
            if rec:
                self._one = (
                    rec['id'], rec['status'], rec['claim_token'],
                    rec['response_json'], rec['claim_expires_at'],
                    rec.get('last_error'),
                )
            return

        if compact.startswith('UPDATE chat_generation_receipt') \
                and 'SET claim_expires_at' in compact \
                and "status='completed'" not in compact \
                and "status='failed'" not in compact:
            lease, user_id, character_id, source_event_id, endpoint, token = params
            key = (user_id, character_id, source_event_id, endpoint)
            rec = self.store.receipts.get(key)
            if rec and rec.get('claim_token') == token and rec.get('status') == 'processing':
                rec['claim_expires_at'] = now + timedelta(seconds=int(lease))
                self._one = (rec['claim_expires_at'],)
                self.rowcount = 1
            return

        if compact.startswith('UPDATE chat_generation_receipt') and "status='completed'" in compact:
            encoded, user_id, character_id, source_event_id, endpoint, token = params
            key = (user_id, character_id, source_event_id, endpoint)
            rec = self.store.receipts.get(key)
            if rec and rec['claim_token'] == token and rec['status'] == 'processing':
                payload = encoded
                if isinstance(payload, str):
                    try:
                        payload = json.loads(payload)
                    except Exception:
                        pass
                rec['status'] = 'completed'
                rec['response_json'] = payload
                self.rowcount = 1
            return

        if compact.startswith('UPDATE chat_generation_receipt') and "status='failed'" in compact:
            last_error, user_id, character_id, source_event_id, endpoint, token = params
            key = (user_id, character_id, source_event_id, endpoint)
            rec = self.store.receipts.get(key)
            if rec and rec['claim_token'] == token and rec['status'] == 'processing':
                rec['status'] = 'failed'
                rec['last_error'] = last_error
                self.rowcount = 1
            return

        if compact.startswith('DELETE FROM chat_generation_receipt'):
            user_id, character_id, source_event_id, endpoint, token = params
            key = (user_id, character_id, source_event_id, endpoint)
            rec = self.store.receipts.get(key)
            if rec and rec['claim_token'] == token and rec['status'] == 'processing':
                del self.store.receipts[key]
                self.rowcount = 1
            return

        if 'INSERT INTO chat_generation_side_effect' in compact:
            key = (params[0], params[1], params[2], params[3], params[4])
            encoded = params[5] if len(params) > 5 else None
            payload = encoded
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    pass
            row = self.store.effects.get(key)
            if row is None:
                self.store.effects[key] = {
                    'effect': params[4],
                    'status': 'pending',
                    'claim_token': None,
                    'last_error': None,
                    'attempt_count': 0,
                    'claim_expires_at': None,
                    'payload_json': payload,
                    'result_json': None,
                    'created_at': now,
                    'user_id': params[0],
                    'character_id': params[1],
                    'source_event_id': params[2],
                    'endpoint': params[3],
                }
                self._one = (params[4],)
                self.rowcount = 1
            elif payload is not None and row.get('payload_json') is None:
                row['payload_json'] = payload
            return

        if compact.startswith('UPDATE chat_generation_side_effect') \
                and "SET status='processing'" in compact:
            token, lease, user_id, character_id, source_event_id, endpoint, effect = params
            key = (user_id, character_id, source_event_id, endpoint, effect)
            row = self.store.effects.get(key)
            can_take = False
            if row and row['status'] == 'pending':
                can_take = True
            elif row and row['status'] == 'failed':
                expires = row.get('claim_expires_at')
                can_take = expires is None or expires < now
            elif row and row['status'] == 'processing' and row.get('claim_expires_at') \
                    and row['claim_expires_at'] < now:
                can_take = True
            if can_take:
                row['status'] = 'processing'
                row['claim_token'] = token
                row['attempt_count'] = int(row.get('attempt_count') or 0) + 1
                row['claim_expires_at'] = now + timedelta(seconds=int(lease))
                row['last_error'] = None
                self._one = (effect, 'processing')
                self.rowcount = 1
            return

        if compact.startswith('UPDATE chat_generation_side_effect') \
                and "SET status='completed'" in compact \
                and 'claim_token=%s' in compact:
            if len(params) >= 7:
                encoded, user_id, character_id, source_event_id, endpoint, effect, token = params[:7]
            else:
                user_id, character_id, source_event_id, endpoint, effect, token = params
                encoded = None
            key = (user_id, character_id, source_event_id, endpoint, effect)
            row = self.store.effects.get(key)
            if row and row.get('claim_token') == token and row.get('status') == 'processing':
                row['status'] = 'completed'
                if encoded is not None:
                    result = encoded
                    if isinstance(result, str):
                        try:
                            result = json.loads(result)
                        except Exception:
                            pass
                    row['result_json'] = result
                self.rowcount = 1
            return

        if compact.startswith('UPDATE chat_generation_side_effect') \
                and "SET status='failed'" in compact:
            last_error, user_id, character_id, source_event_id, endpoint, effect, token = params
            key = (user_id, character_id, source_event_id, endpoint, effect)
            row = self.store.effects.get(key)
            if row and row.get('claim_token') == token and row.get('status') == 'processing':
                row['status'] = 'failed'
                row['last_error'] = last_error
                row['claim_expires_at'] = now - timedelta(seconds=1)
                self.rowcount = 1
            return

        if compact.startswith('SELECT user_id, character_id, source_event_id, endpoint, effect'):
            max_attempts, limit = params
            due = []
            for key, row in self.store.effects.items():
                status = row.get('status')
                expires = row.get('claim_expires_at')
                attempts = int(row.get('attempt_count') or 0)
                is_due = False
                if status == 'pending':
                    is_due = True
                elif status == 'processing' and expires and expires < now:
                    is_due = True
                elif status == 'failed' and attempts < int(max_attempts) and (
                        expires is None or expires < now):
                    is_due = True
                if is_due:
                    item = dict(row)
                    item.setdefault('user_id', key[0])
                    item.setdefault('character_id', key[1])
                    item.setdefault('source_event_id', key[2])
                    item.setdefault('endpoint', key[3])
                    due.append(item)
            due.sort(key=lambda item: item.get('created_at') or now)
            self._many = [
                (
                    item.get('user_id'), item.get('character_id'),
                    item.get('source_event_id'), item.get('endpoint'),
                    item.get('effect'), item.get('payload_json'),
                    item.get('result_json'), item.get('status'),
                    item.get('attempt_count') or 0,
                )
                for item in due[:int(limit)]
            ]
            return

        if compact.startswith('SELECT effect, status, claim_token') \
                and 'FROM chat_generation_side_effect' in compact:
            user_id, character_id, source_event_id, endpoint = params
            self._many = []
            for key, row in self.store.effects.items():
                if key[:4] == (user_id, character_id, source_event_id, endpoint):
                    self._many.append((
                        row['effect'], row['status'], row.get('claim_token'),
                        row.get('last_error'), row.get('attempt_count') or 0,
                        row.get('payload_json'), row.get('result_json'),
                        row.get('claim_expires_at'),
                    ))
            return

        if compact.startswith('SELECT effect, status FROM chat_generation_side_effect'):
            user_id, character_id, source_event_id, endpoint, effect = params
            key = (user_id, character_id, source_event_id, endpoint, effect)
            row = self.store.effects.get(key)
            if row:
                self._one = (row['effect'], row['status'])
            return

        if 'INSERT INTO tasks' in compact:
            occurrence = params[-1] if len(params) >= 7 else None
            if occurrence:
                for task in self.store.tasks:
                    if task.get('occurrence_key') == occurrence:
                        self._one = (task['id'],)
                        return
            task_id = self.store.next_task
            self.store.next_task += 1
            self.store.tasks.append({
                'id': task_id, 'params': params, 'occurrence_key': occurrence,
                'user_id': params[0] if params else None,
                'title': params[1] if len(params) > 1 else '',
                'completed': False,
                'created_at': now,
                'notification_id': None,
            })
            self._one = (task_id,)
            self.rowcount = 1
            return

        if compact.startswith('SELECT id FROM tasks WHERE occurrence_key'):
            for task in self.store.tasks:
                if task.get('occurrence_key') == params[0]:
                    self._one = (task['id'],)
                    return
            return

        if 'INSERT INTO proactive_promise' in compact:
            occurrence = params[-1] if len(params) >= 8 else None
            if occurrence:
                for item in self.store.promises:
                    if item.get('occurrence_key') == occurrence:
                        self._one = (item['id'],)
                        return
            pid = self.store.next_promise
            self.store.next_promise += 1
            self.store.promises.append({
                'id': pid, 'params': params, 'occurrence_key': occurrence,
            })
            self._one = (pid,)
            self.rowcount = 1
            return

        if compact.startswith('SELECT id FROM proactive_promise WHERE occurrence_key'):
            for item in self.store.promises:
                if item.get('occurrence_key') == params[0]:
                    self._one = (item['id'],)
                    return
            return

        if 'INSERT INTO temporal_turn_once' in compact:
            key = tuple(params[:3])
            if key in self.store.turn_once:
                return
            self.store.turn_once.add(key)
            self._one = (params[2],)
            self.rowcount = 1
            return

        if 'INSERT INTO task_cancel_occurrence' in compact:
            key = params[0]
            if key in self.store.cancels:
                return
            self.store.cancels.add(key)
            self._one = (key,)
            self.rowcount = 1
            return

        if compact.startswith('SELECT id, notification_id FROM tasks'):
            user_id = params[0]
            keyword = None
            if 'ILIKE' in compact and len(params) > 1:
                keyword = str(params[1]).replace('%', '')
            rows = []
            for task in self.store.tasks:
                owner = task.get('user_id')
                if owner is None and task.get('params'):
                    owner = task['params'][0]
                if owner != user_id:
                    continue
                if task.get('completed'):
                    continue
                if keyword:
                    title = str(task.get('title') or (task.get('params') or [None, ''])[1] or '')
                    if keyword.lower() not in title.lower():
                        continue
                rows.append(task)
            rows.sort(key=lambda item: item.get('id') or 0, reverse=True)
            if 'LIMIT 1' in compact:
                rows = rows[:1]
            self._many = [(item['id'], item.get('notification_id')) for item in rows]
            self._one = self._many[0] if self._many else None
            return

        if compact.startswith('DELETE FROM tasks'):
            task_id = params[0]
            user_id = params[1] if len(params) > 1 else None
            kept = []
            deleted = None
            for task in self.store.tasks:
                owner = task.get('user_id')
                if owner is None and task.get('params'):
                    owner = task['params'][0]
                if task['id'] == task_id and (user_id is None or owner == user_id):
                    deleted = task
                    continue
                kept.append(task)
            self.store.tasks = kept
            if deleted:
                self._one = (deleted['id'], deleted.get('notification_id'))
                self.rowcount = 1
            return

        if 'INSERT INTO memory_jobs' in compact or 'FROM memory_jobs' in compact:
            source = params[-2] if 'RETURNING' in compact else (params[3] if len(params) > 3 else None)
            if compact.startswith('SELECT') and source:
                for job in self.store.jobs:
                    if job.get('source_event_id') == source and job.get('status') in (
                            'pending', 'running', 'done'):
                        self._one = (job['id'],)
                        return
                return
            job_id = self.store.next_job
            self.store.next_job += 1
            self.store.jobs.append({
                'id': job_id,
                'status': 'pending',
                'source_event_id': params[-2] if len(params) >= 2 else None,
            })
            self._one = (job_id,)
            self.rowcount = 1


class FakeConn:
    def __init__(self, store):
        self.store = store

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


class ClaimSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.store = ReceiptStore()
        self.patchers = [
            patch.object(receipt, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(receipt, 'DEFAULT_WAIT_SECONDS', 0.4),
            patch.object(receipt, 'DEFAULT_POLL_SECONDS', 0.05),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)

    def test_first_claim_owns_insert(self):
        first = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        self.assertTrue(first['owned'])
        self.assertEqual(first['status'], 'processing')
        self.assertTrue(first['claim_token'])

    def test_completed_replay_does_not_reclaim(self):
        first = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        receipt.complete_generation(
            'u', 'gojo', 'e1', 'chat_text', first['claim_token'],
            {'emotion': '平静', 'messages': [{'jp': 'a', 'zh': 'b', 'audio_b64': 'HUGE'}]})
        second = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        self.assertFalse(second['owned'])
        self.assertEqual(second['status'], 'completed')
        self.assertNotIn('audio_b64', json.dumps(second['response_json']))

    def test_processing_lease_blocks_second_owner(self):
        first = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text', lease_seconds=240)
        second = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        self.assertTrue(first['owned'])
        self.assertFalse(second['owned'])
        self.assertEqual(second['status'], 'processing')
        resolve = receipt.resolve_generation(
            'u', 'gojo', 'e1', 'chat_text', wait_seconds=0.2, poll_seconds=0.05)
        self.assertEqual(resolve['action'], 'in_progress')

    def test_stale_processing_lease_can_reclaim(self):
        first = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        key = ('u', 'gojo', 'e1', 'chat_text')
        self.store.receipts[key]['claim_expires_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=5))
        second = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        self.assertTrue(second['owned'])
        self.assertNotEqual(second['claim_token'], first['claim_token'])

    def test_failed_can_reclaim(self):
        first = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        receipt.fail_generation(
            'u', 'gojo', 'e1', 'chat_text', first['claim_token'], 'boom')
        second = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        self.assertTrue(second['owned'])

    def test_side_effect_claim_lifecycle(self):
        first = receipt.claim_side_effect('u', 'gojo', 'e1', 'chat_text', 'reminder')
        self.assertTrue(first['owned'])
        second = receipt.claim_side_effect('u', 'gojo', 'e1', 'chat_text', 'reminder')
        self.assertFalse(second['owned'])
        self.assertEqual(second['status'], 'processing')
        receipt.complete_side_effect(
            'u', 'gojo', 'e1', 'chat_text', 'reminder', first['claim_token'])
        third = receipt.claim_side_effect('u', 'gojo', 'e1', 'chat_text', 'reminder')
        self.assertFalse(third['owned'])
        self.assertEqual(third['status'], 'completed')

    def test_complete_inserts_pending_effects(self):
        first = receipt.claim_generation('u', 'gojo', 'e1', 'chat_text')
        ok = receipt.complete_generation(
            'u', 'gojo', 'e1', 'chat_text', first['claim_token'],
            {'_user_text': 'hi', 'messages': [{'jp': 'a', 'zh': 'b'}]},
            effects=['private_extraction', 'relationship_update'])
        self.assertTrue(ok)
        rows = receipt.list_side_effects('u', 'gojo', 'e1', 'chat_text')
        names = sorted(r['effect'] for r in rows)
        self.assertEqual(names, ['private_extraction', 'relationship_update'])
        self.assertTrue(all(r['status'] == 'pending' for r in rows))

    def test_legacy_source_event_id_is_assigned(self):
        source, legacy = receipt.assign_source_event_id('')
        self.assertTrue(legacy)
        self.assertTrue(source.startswith('legacy:'))

    def test_stamp_ids(self):
        msgs = receipt.stamp_assistant_messages(
            [{'jp': 'a', 'zh': '1'}, {'jp': 'b', 'zh': '2'}, {'jp': 'c', 'zh': '3'}],
            'chat_reply:src')
        self.assertEqual([m['event_id'] for m in msgs], [
            'chat_reply:src:0', 'chat_reply:src:1', 'chat_reply:src:2'])
        self.assertEqual(receipt.assistant_turn_id_for('chat_text', 'src'), 'chat_reply:src')
        self.assertEqual(receipt.assistant_turn_id_for('chat_image', 'src'), 'image_reply:src')


class RouteIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.store = ReceiptStore()
        self.client = Mock()
        router = Mock()
        router.post.side_effect = lambda *_args, **_kwargs: lambda function: function
        self.jobs = Mock()
        self.promise_detect = Mock()
        self.applied_rel = []
        self.promises_by_key = {}

        def _add_promise(character_id, user_id, trigger_kind, context,
                         trigger_at=None, trigger_time=None, origin_text='',
                         occurrence_key=None):
            if occurrence_key and occurrence_key in self.promises_by_key:
                return self.promises_by_key[occurrence_key]
            pid = self.store.next_promise
            self.store.next_promise += 1
            self.store.promises.append({
                'id': pid, 'occurrence_key': occurrence_key, 'context': context,
            })
            if occurrence_key:
                self.promises_by_key[occurrence_key] = pid
            return pid

        def process_turn(**kwargs):
            sid = kwargs.get('source_event_id')
            if sid in self.applied_rel:
                return {
                    'signals_extracted': 0, 'signals_applied': 0,
                    'skipped': 'already_processed', 'applied': [],
                }
            self.applied_rel.append(sid)
            return {
                'signals_extracted': 1, 'signals_applied': 1, 'applied': [],
            }

        self.add_promise = Mock(side_effect=_add_promise)
        self.process_turn = Mock(side_effect=process_turn)
        self.rel = self.process_turn
        self.tts = Mock(return_value='audio')
        self.save_short = Mock()
        self.save_user = Mock(return_value=True)
        self.record_turn = Mock()
        self.record_cycle = Mock()

        def _create(model, max_tokens, system_blocks, messages):
            raw = self.raws.pop(0) if self.raws else ''
            return raw, Mock()

        self.raws = []
        modules = {
            'anthropic': stub('anthropic', Anthropic=Mock(return_value=self.client)),
            'fastapi': stub('fastapi', APIRouter=Mock(return_value=router)),
            'fastapi.responses': stub(
                'fastapi.responses',
                JSONResponse=lambda content, status_code=200: types.SimpleNamespace(
                    body=json.dumps(content, ensure_ascii=False, default=str).encode(),
                    status_code=status_code,
                ),
            ),
            'config': stub(
                'config',
                ANTHROPIC_KEY='',
                EMOTIONS=['平静', '调皮', '疑惑'],
                TTS_PROVIDER='fish',
                DEFAULT_CHARACTER_ID='gojo',
                MODEL_MAIN='claude-test',
                MODEL_JP_AUX='claude-haiku-test',
            ),
            'db': stub('db', get_conn=lambda: FakeConn(self.store)),
            'ai_client': stub('ai_client', extract_text=lambda response, sep='': ''),
            'tts': stub('tts', tts_to_b64=self.tts, transcribe_audio_b64=Mock()),
            'prompt': stub(
                'prompt',
                build_system_blocks=Mock(return_value=[{'type': 'text', 'text': '角色设定'}]),
                log_cache_usage=Mock(),
            ),
            'user_memory': stub(
                'user_memory',
                save_short_memory=self.save_short,
                save_user_short_memory_once=self.save_user,
                get_short_memory=Mock(return_value=[]),
                get_short_memory_for_prompt=Mock(return_value=[]),
                update_chat_days=Mock(return_value=3),
                SHORT_MEMORY_MAX=20,
            ),
            'memory_jobs': stub('memory_jobs', enqueue_private_extraction=self.jobs),
            'temporal_awareness': stub(
                'temporal_awareness',
                get_temporal_snapshot=Mock(return_value={'now_utc': None}),
                find_reply_calendar_conflict=Mock(return_value=None),
                record_turn=self.record_turn,
                record_user_message=Mock(),
                record_assistant_message=Mock(),
            ),
            'characters': stub(
                'characters',
                get_character=Mock(return_value={'voice_id': 'v1', 'core_prompt': 'core'}),
            ),
            'tasks': stub(
                'tasks',
                find_duplicate_task=Mock(return_value=None),
                find_and_delete_tasks_by_keyword=Mock(return_value=[]),
                delete_latest_task=Mock(return_value=[]),
                find_open_tasks_by_keyword=self._find_open_tasks_by_keyword,
                find_latest_open_task=self._find_latest_open_task,
                delete_tasks_by_ids=self._delete_tasks_by_ids,
            ),
            'task_dedup': stub('task_dedup', find_similar_task=Mock(return_value=None)),
            'relationship_state': stub(
                'relationship_state', save_offline_character_state=Mock()),
            'promise_detector': stub('promise_detector', detect_and_save=self.promise_detect),
            'db_promise': stub('db_promise', add_promise=self.add_promise),
            'relationship_engine': stub('relationship_engine', process_turn=self.process_turn),
            'behavior_evidence': stub(
                'behavior_evidence', record_reply_cycle=self.record_cycle),
            'context_layer': stub(
                'context_layer',
                build_chat_context=Mock(return_value=types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')),
                assemble_fallback_from_messages=Mock(return_value=types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')),
                append_current_user_turn=lambda msgs, content: list(msgs or []) + [
                    {'role': 'user', 'content': content}],
            ),
            'raw_events': stub(
                'raw_events',
                SourceValidityError=type('SourceValidityError', (Exception,), {})),
            'db_chat_media': stub(
                'db_chat_media',
                get_media_for_source_event=Mock(return_value=None),
                public_media=Mock(return_value=None),
            ),
            'reply_availability': stub(
                'reply_availability',
                check_reply_availability=Mock(return_value={'can_reply': True}),
            ),
        }
        self.receipt_patchers = [
            patch.object(receipt, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(receipt, 'DEFAULT_WAIT_SECONDS', 0.5),
            patch.object(receipt, 'DEFAULT_POLL_SECONDS', 0.05),
        ]
        for item in self.receipt_patchers:
            item.start()
            self.addCleanup(item.stop)
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.route = load_source('route_chat', modules)

        def _rel_start(user_id, character_id, user_text, full_jp, char, short_memories,
                       temporal_snapshot=None, source_event_id=None, pack=None):
            self.process_turn(
                user_id=user_id,
                character_id=character_id,
                user_message=user_text,
                character_reply=full_jp,
                source_event_id=source_event_id,
            )

        self.route._start_relationship_update = _rel_start
        self.route._create_json = Mock(side_effect=_create)
        self._hb_patch = patch.object(receipt, 'GenerationHeartbeat', _PassthroughHeartbeat)
        self._hb_patch.start()
        self.addCleanup(self._hb_patch.stop)

        def _apply_rel(ctx):
            _rel_start(
                ctx.get('user_id'), ctx.get('character_id'),
                ctx.get('user_text') or '', ctx.get('full_jp') or '',
                ctx.get('char') or {}, ctx.get('short_memories') or [],
                ctx.get('temporal_snapshot'), ctx.get('source_event_id'),
                pack=ctx.get('pack'))
            return {}

        def _apply_pd(ctx):
            payload = ctx.get('payload') or {}
            msgs = ctx.get('msgs') or payload.get('messages') or []
            reply_zh = ' '.join(
                str((m or {}).get('zh') or '') for m in msgs if (m or {}).get('zh'))
            if reply_zh:
                self.promise_detect(
                    ctx.get('character_id'), ctx.get('user_id'),
                    ctx.get('user_text') or '', reply_zh)
            return {}

        for target, fn in (
            ('generation_effects.apply_relationship_update', _apply_rel),
            ('generation_effects.apply_promise_detector', _apply_pd),
        ):
            item = patch(target, side_effect=fn)
            item.start()
            self.addCleanup(item.stop)
        try:
            import tts as _real_tts
            tts_lock = patch.object(_real_tts, 'tts_to_b64', self.tts)
            tts_lock.start()
            self.addCleanup(tts_lock.stop)
        except Exception:
            pass
        try:
            import relationship_engine as _rel_eng
            rel_lock = patch.object(_rel_eng, 'process_turn', self.process_turn)
            rel_lock.start()
            self.addCleanup(rel_lock.stop)
        except Exception:
            pass
        log_patch = patch('builtins.print')
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.addCleanup(self._reset_crash_flags)

    def _find_open_tasks_by_keyword(self, user_id, keyword, latest_only=True):
        rows = []
        for task in self.store.tasks:
            owner = task.get('user_id')
            if owner is None and task.get('params'):
                owner = task['params'][0]
            if owner != user_id or task.get('completed'):
                continue
            title = str(task.get('title') or (task.get('params') or [None, ''])[1] or '')
            if str(keyword or '').lower() not in title.lower():
                continue
            rows.append((task['id'], task.get('notification_id')))
        rows.sort(key=lambda item: item[0], reverse=True)
        return rows[:1] if latest_only else rows

    def _find_latest_open_task(self, user_id):
        rows = []
        for task in self.store.tasks:
            owner = task.get('user_id')
            if owner is None and task.get('params'):
                owner = task['params'][0]
            if owner != user_id or task.get('completed'):
                continue
            rows.append((task['id'], task.get('notification_id')))
        rows.sort(key=lambda item: item[0], reverse=True)
        return rows[:1]

    def _delete_tasks_by_ids(self, user_id, task_ids):
        ids = {int(tid) for tid in (task_ids or []) if tid is not None}
        deleted = []
        kept = []
        for task in self.store.tasks:
            if task['id'] in ids:
                deleted.append((task['id'], task.get('notification_id')))
            else:
                kept.append(task)
        self.store.tasks = kept
        return deleted

    def _reset_crash_flags(self):
        receipt.CRASH_BEFORE_COMPLETE = False
        receipt.CRASH_BEFORE_EFFECT = None
        receipt.CRASH_AFTER_EFFECT = None

    def _drain(self):
        from generation_side_effect_worker import process_due_side_effects
        for _ in range(24):
            ran = process_due_side_effects(limit=20)
            if not ran:
                break

    def send(self, raws, source_event_id='evt-1', extra=None, drain=True):
        self.raws = list(raws)
        payload = {
            'user_id': 'u',
            'character_id': 'gojo',
            'text': '你好',
            'source_event_id': source_event_id,
        }
        if extra:
            payload.update(extra)
        response = asyncio.run(self.route.chat_text(payload))
        if drain and getattr(response, 'status_code', None) == 200:
            self._drain()
        return response, json.loads(response.body)

    def test_sequential_same_source_llm_once(self):
        raw = chat_reply('そうだね', '是啊')
        first, body1 = self.send([raw], 'dup-1')
        time.sleep(0.05)
        second, body2 = self.send([raw], 'dup-1')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(body1['assistant_turn_id'], 'chat_reply:dup-1')
        self.assertEqual(body2['assistant_turn_id'], body1['assistant_turn_id'])
        self.assertEqual(body1['messages'][0]['event_id'], 'chat_reply:dup-1:0')
        self.assertEqual(body2['messages'][0]['jp'], body1['messages'][0]['jp'])
        self.assertEqual(self.jobs.call_count, 1)
        self.assertEqual(self.rel.call_count, 1)
        self.assertEqual(self.promise_detect.call_count, 1)
        self.assertEqual(self.save_short.call_count, 1)
        self.assertEqual(self.record_turn.call_count, 1)

    def test_http_loss_replays_completed(self):
        raw = chat_reply('そうだね', '是啊')
        first, body1 = self.send([raw], 'lost-1')
        self.assertEqual(first.status_code, 200)
        second, body2 = self.send([chat_reply('違うよ', '不同')], 'lost-1')
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(body2['messages'][0]['jp'], body1['messages'][0]['jp'])
        self.assertEqual(body2['assistant_turn_id'], 'chat_reply:lost-1')

    def test_failed_then_retry_regenerates(self):
        first, body1 = self.send(['', '', ''], 'fail-1')
        self.assertEqual(first.status_code, 502)
        second, body2 = self.send([chat_reply('そうだね', '是啊')], 'fail-1')
        self.assertEqual(second.status_code, 200)
        self.assertGreaterEqual(self.route._create_json.call_count, 2)
        self.assertEqual(body2['messages'][0]['jp'], 'そうだね。')

    def test_processing_second_request_does_not_generate(self):
        owned = receipt.claim_generation('u', 'gojo', 'busy-1', 'chat_text', lease_seconds=240)
        self.assertTrue(owned['owned'])
        response, body = self.send([chat_reply('x', 'x')], 'busy-1')
        self.assertEqual(response.status_code, 202)
        self.assertTrue(body['generation_in_progress'])
        self.assertEqual(body['source_event_id'], 'busy-1')
        self.assertTrue(body['retryable'])
        self.assertEqual(self.route._create_json.call_count, 0)

    def test_stale_lease_reclaim_generates(self):
        owned = receipt.claim_generation('u', 'gojo', 'stale-1', 'chat_text')
        key = ('u', 'gojo', 'stale-1', 'chat_text')
        self.store.receipts[key]['claim_expires_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=1))
        response, body = self.send([chat_reply('そうだね', '是啊')], 'stale-1')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['messages'][0]['jp'], 'そうだね。')
        self.assertNotEqual(owned['claim_token'], body.get('assistant_turn_id'))

    def test_concurrent_only_one_owner(self):
        raw = chat_reply('そうだね', '是啊')
        barrier = threading.Barrier(2)
        results = []

        def _create(model, max_tokens, system_blocks, messages):
            time.sleep(0.15)
            return raw, Mock()

        self.route._create_json = Mock(side_effect=_create)

        def worker():
            barrier.wait()
            payload = {
                'user_id': 'u', 'character_id': 'gojo',
                'text': '你好', 'source_event_id': 'conc-1',
            }
            response = asyncio.run(self.route.chat_text(payload))
            results.append((response.status_code, json.loads(response.body)))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for item in threads:
            item.start()
        for item in threads:
            item.join()
        self.assertEqual(self.route._create_json.call_count, 1)
        statuses = sorted(code for code, _ in results)
        self.assertEqual(statuses[0] in (200, 202), True)
        self.assertTrue(any(code == 200 for code, _ in results))
        bodies = [body for code, body in results if code == 200]
        self.assertTrue(bodies)
        self.assertTrue(all(b['messages'][0]['jp'] == 'そうだね。' for b in bodies))

    def test_reminder_and_promise_once(self):
        extra = {
            'reminder': {
                'date': '2026-09-20', 'time': '10:00',
                'content': '喝水', 'notification': '喝水',
            },
            'proactive_promise': {
                'trigger_kind': 'daily',
                'trigger_time': '09:00',
                'context': '提醒喝水',
            },
        }
        raw = chat_reply('わかった', '知道了', extra=extra)
        self.send([raw], 'side-1')
        time.sleep(0.05)
        self.send([raw], 'side-1')
        self.assertEqual(len(self.store.tasks), 1)
        self.assertEqual(self.add_promise.call_count, 1)
        self.assertEqual(self.promise_detect.call_count, 1)


    def test_reminder_first_response_has_task_id(self):
        extra = {
            'reminder': {
                'date': '2026-09-20', 'time': '10:00',
                'content': 'drink water', 'notification': 'drink water',
            },
        }
        raw = chat_reply('okay', 'ok', extra=extra)
        response, body = self.send([raw], 'rem-first', drain=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['reminder']['task_id'], self.store.tasks[0]['id'])
        self.assertFalse(body['reminder']['duplicate'])
        row = self.store.effects[('u', 'gojo', 'rem-first', 'chat_text', 'reminder')]
        self.assertEqual(row['status'], 'completed')

    def test_reminder_transient_failure_returns_pending_without_schedulable_reminder(self):
        extra = {
            'reminder': {
                'date': '2026-09-20', 'time': '10:00',
                'content': 'drink water', 'notification': 'drink water',
            },
        }
        raw = chat_reply('okay', 'ok', extra=extra)
        with patch('generation_effects.apply_reminder', side_effect=RuntimeError('transient')):
            response, body = self.send([raw], 'rem-pending', drain=False)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(body['generation_in_progress'])
        self.assertTrue(body['reminder_pending'])
        self.assertNotIn('reminder', body)
        row = self.store.effects[('u', 'gojo', 'rem-pending', 'chat_text', 'reminder')]
        self.assertEqual(row['status'], 'failed')
        retry, retry_body = self.send([chat_reply('different', 'different')], 'rem-pending', drain=False)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(retry_body['reminder']['task_id'], self.store.tasks[0]['id'])
        self.assertEqual(self.route._create_json.call_count, 1)

    def test_three_bubbles_stable_ids(self):
        raw = chat_reply('x', 'x', messages=[
            {'jp': '今日はいい天気だね。', 'zh': '今天天气不错。'},
            {'jp': '夕方になったら戻るよ。', 'zh': '傍晚再回来。'},
            {'jp': '水も忘れずに持っていこう。', 'zh': '别忘了带水。'},
        ])
        _, body = self.send([raw], 'bub-3')
        ids = [m['event_id'] for m in body['messages']]
        self.assertEqual(ids, [
            'chat_reply:bub-3:0',
            'chat_reply:bub-3:1',
            'chat_reply:bub-3:2',
        ])
        self.assertEqual(body['assistant_turn_id'], 'chat_reply:bub-3')


    def _expire_effect(self, source_event_id, effect, endpoint='chat_text'):
        key = ('u', 'gojo', source_event_id, endpoint, effect)
        row = self.store.effects.get(key)
        if row:
            row['claim_expires_at'] = datetime.now(timezone.utc) - timedelta(seconds=5)

    def test_crash_before_complete_has_no_assistant_effects(self):
        raw = chat_reply('そうだね', '是啊', extra={
            'reminder': {
                'date': '2026-09-20', 'time': '10:00',
                'content': '喝水', 'notification': '喝水',
            },
            'proactive_promise': {
                'trigger_kind': 'daily',
                'trigger_time': '09:00',
                'context': '提醒喝水',
            },
        })
        receipt.CRASH_BEFORE_COMPLETE = True
        with self.assertRaises(RuntimeError):
            self.send([raw], 'pre-complete')
        receipt.CRASH_BEFORE_COMPLETE = False
        self.assertEqual(self.jobs.call_count, 0)
        self.assertEqual(self.rel.call_count, 0)
        self.assertEqual(self.save_short.call_count, 0)
        self.assertEqual(self.record_turn.call_count, 0)
        self.assertEqual(len(self.store.tasks), 0)
        self.assertEqual(self.add_promise.call_count, 0)
        rec = self.store.receipts.get(('u', 'gojo', 'pre-complete', 'chat_text'))
        self.assertEqual(rec['status'], 'processing')

    def test_crash_before_private_extraction_then_replay_once(self):
        raw = chat_reply('そうだね', '是啊')
        receipt.CRASH_BEFORE_EFFECT = 'private_extraction'
        with self.assertRaises(RuntimeError):
            self.send([raw], 'crash-pe')
        receipt.CRASH_BEFORE_EFFECT = None
        self.assertEqual(self.jobs.call_count, 0)
        second, _body = self.send([chat_reply('違うよ', '不同')], 'crash-pe')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(self.jobs.call_count, 1)

    def test_crash_before_relationship_then_replay_once(self):
        raw = chat_reply('そうだね', '是啊')
        receipt.CRASH_BEFORE_EFFECT = 'relationship_update'
        with self.assertRaises(RuntimeError):
            self.send([raw], 'crash-rel')
        receipt.CRASH_BEFORE_EFFECT = None
        self.assertEqual(self.rel.call_count, 0)
        second, _body = self.send([raw], 'crash-rel')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.rel.call_count, 1)
        self.assertEqual(self.applied_rel, ['crash-rel'])
        self.assertEqual(self.route._create_json.call_count, 1)

    def test_relationship_applied_then_crash_before_effect_completed(self):
        raw = chat_reply('そうだね', '是啊')
        receipt.CRASH_AFTER_EFFECT = 'relationship_update'
        with self.assertRaises(RuntimeError):
            self.send([raw], 'crash-rel-after')
        receipt.CRASH_AFTER_EFFECT = None
        self.assertEqual(self.applied_rel, ['crash-rel-after'])
        self._expire_effect('crash-rel-after', 'relationship_update')
        second, _body = self.send([raw], 'crash-rel-after')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.rel.call_count, 2)
        self.assertEqual(self.applied_rel, ['crash-rel-after'])
        row = self.store.effects[('u', 'gojo', 'crash-rel-after', 'chat_text', 'relationship_update')]
        self.assertEqual(row['status'], 'completed')

    def test_reminder_create_then_crash_before_effect_completed(self):
        extra = {
            'reminder': {
                'date': '2026-09-20', 'time': '10:00',
                'content': 'drink water', 'notification': 'drink water',
            },
        }
        raw = chat_reply('okay', 'ok', extra=extra)
        receipt.CRASH_AFTER_EFFECT = 'reminder'
        with self.assertRaises(RuntimeError):
            self.send([raw], 'crash-rem', drain=False)
        receipt.CRASH_AFTER_EFFECT = None
        self.assertEqual(len(self.store.tasks), 1)
        self._expire_effect('crash-rem', 'reminder')
        self._drain()
        self.assertEqual(len(self.store.tasks), 1)
        self.assertEqual(self.route._create_json.call_count, 1)
        row = self.store.effects[('u', 'gojo', 'crash-rem', 'chat_text', 'reminder')]
        self.assertEqual(row['status'], 'completed')
        self.assertEqual(row['result_json']['task_id'], self.store.tasks[0]['id'])

    def test_proactive_promise_create_then_crash_before_effect_completed(self):
        extra = {
            'proactive_promise': {
                'trigger_kind': 'daily',
                'trigger_time': '09:00',
                'context': '提醒喝水',
            },
        }
        raw = chat_reply('わかった', '知道了', extra=extra)
        receipt.CRASH_AFTER_EFFECT = 'proactive_promise'
        with self.assertRaises(RuntimeError):
            self.send([raw], 'crash-pp')
        receipt.CRASH_AFTER_EFFECT = None
        self.assertEqual(len(self.store.promises), 1)
        self._expire_effect('crash-pp', 'proactive_promise')
        self.send([raw], 'crash-pp')
        self.assertEqual(len(self.store.promises), 1)
        self.assertEqual(self.add_promise.call_count, 2)
        self.assertEqual(self.route._create_json.call_count, 1)

    def test_completed_replay_repairs_pending_without_llm(self):
        raw = chat_reply('そうだね', '是啊')
        first, _body1 = self.send([raw], 'repair-1')
        self.assertEqual(first.status_code, 200)
        key = ('u', 'gojo', 'repair-1', 'chat_text', 'private_extraction')
        self.store.effects[key]['status'] = 'pending'
        self.store.effects[key]['claim_token'] = None
        self.jobs.reset_mock()
        second, _body2 = self.send([chat_reply('違うよ', '不同')], 'repair-1')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(self.jobs.call_count, 1)

    def test_http_returns_before_relationship_worker_repairs(self):
        raw = chat_reply('そうだね', '是啊')
        first, _body = self.send([raw], 'async-rel', drain=False)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.jobs.call_count, 0)
        self.assertEqual(self.rel.call_count, 0)
        self.assertEqual(self.promise_detect.call_count, 0)
        private_row = self.store.effects[('u', 'gojo', 'async-rel', 'chat_text', 'private_extraction')]
        self.assertEqual(private_row['status'], 'pending')
        row = self.store.effects[('u', 'gojo', 'async-rel', 'chat_text', 'relationship_update')]
        self.assertEqual(row['status'], 'pending')
        self._drain()
        self.assertEqual(self.jobs.call_count, 1)
        self.assertEqual(self.rel.call_count, 1)
        self.assertEqual(self.promise_detect.call_count, 1)
        self.assertEqual(
            self.store.effects[('u', 'gojo', 'async-rel', 'chat_text', 'relationship_update')]['status'],
            'completed')
        self.assertEqual(self.route._create_json.call_count, 1)

    def test_heartbeat_blocks_reclaim_while_llm_runs(self):
        self._hb_patch.stop()
        raw = chat_reply('そうだね', '是啊')
        started = threading.Event()

        def _create(model, max_tokens, system_blocks, messages):
            started.set()
            time.sleep(2.0)
            return raw, Mock()

        self.route._create_json = Mock(side_effect=_create)
        lease_patch = patch.object(receipt, 'DEFAULT_LEASE_SECONDS', 1)
        hb_patch = patch.object(receipt, 'DEFAULT_HEARTBEAT_INTERVAL_SECONDS', 0.2)
        lease_patch.start()
        hb_patch.start()
        self.addCleanup(lease_patch.stop)
        self.addCleanup(hb_patch.stop)
        self.addCleanup(self._hb_patch.start)
        finished = []

        def owner():
            response = asyncio.run(self.route.chat_text({
                'user_id': 'u', 'character_id': 'gojo',
                'text': '你好', 'source_event_id': 'hb-long',
            }))
            finished.append((response.status_code, json.loads(response.body)))

        thread = threading.Thread(target=owner)
        thread.start()
        self.assertTrue(started.wait(timeout=8))
        time.sleep(1.3)
        second, body = self.send([chat_reply('違うよ', '不同')], 'hb-long', drain=False)
        thread.join(timeout=5)
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(second.status_code, 202)
        self.assertTrue(body['generation_in_progress'])
        self.assertTrue(finished)
        self.assertEqual(finished[0][0], 200)

    def test_reminder_http_loss_replays_same_task_id(self):
        extra = {
            'reminder': {
                'date': '2026-09-20', 'time': '10:00',
                'content': '喝水', 'notification': '喝水',
            },
        }
        raw = chat_reply('わかった', '知道了', extra=extra)
        first, body1 = self.send([raw], 'rem-lost')
        second, body2 = self.send([chat_reply('違うよ', '不同')], 'rem-lost')
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(body1['reminder']['task_id'], body2['reminder']['task_id'])
        self.assertEqual(len(self.store.tasks), 1)
        rec = self.store.receipts[('u', 'gojo', 'rem-lost', 'chat_text')]
        self.assertNotIn('task_id', json.dumps(rec['response_json']))

    def test_promise_http_loss_replays_same_promise_id(self):
        extra = {
            'proactive_promise': {
                'trigger_kind': 'daily',
                'trigger_time': '09:00',
                'context': '提醒喝水',
            },
        }
        raw = chat_reply('わかった', '知道了', extra=extra)
        first, body1 = self.send([raw], 'pp-lost')
        second, body2 = self.send([chat_reply('違うよ', '不同')], 'pp-lost')
        self.assertEqual(self.add_promise.call_count, 1)
        self.assertEqual(body1['saved_promise']['id'], body2['saved_promise']['id'])
        self.assertEqual(body1['saved_promise']['id'], self.store.promises[0]['id'])


    def test_cancel_first_response_returns_notification_id(self):
        now = datetime.now(timezone.utc)
        self.store.tasks.append({
            'id': 10, 'user_id': 'u', 'title': 'old reminder', 'completed': False,
            'created_at': now, 'notification_id': 'notif-10',
        })
        raw = chat_reply('okay', 'ok', extra={'cancel_reminder': {'latest': True}})
        response, body = self.send([raw], 'cancel-first', drain=False)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            body['cancelled_tasks'],
            [{'task_id': 10, 'notification_id': 'notif-10'}])
        self.assertEqual(self.store.tasks, [])
        row = self.store.effects[('u', 'gojo', 'cancel-first', 'chat_text', 'cancel_reminder')]
        self.assertEqual(row['status'], 'completed')

    def test_cancel_http_loss_replays_same_cancelled_tasks(self):
        now = datetime.now(timezone.utc)
        self.store.tasks.append({
            'id': 10, 'user_id': 'u', 'title': 'old reminder', 'completed': False,
            'created_at': now, 'notification_id': 'notif-10',
        })
        raw = chat_reply('okay', 'ok', extra={'cancel_reminder': {'latest': True}})
        first, body1 = self.send([raw], 'cancel-lost', drain=False)
        second, body2 = self.send([chat_reply('different', 'different')], 'cancel-lost', drain=False)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(body1['cancelled_tasks'], body2['cancelled_tasks'])
        self.assertEqual(body2['cancelled_tasks'][0]['notification_id'], 'notif-10')

    def test_cancel_transient_failure_returns_pending_without_fake_success(self):
        now = datetime.now(timezone.utc)
        self.store.tasks.append({
            'id': 10, 'user_id': 'u', 'title': 'old reminder', 'completed': False,
            'created_at': now, 'notification_id': 'notif-10',
        })
        raw = chat_reply('okay', 'ok', extra={'cancel_reminder': {'latest': True}})
        with patch('generation_effects.apply_cancel_reminder', side_effect=RuntimeError('transient')):
            response, body = self.send([raw], 'cancel-pending', drain=False)
        self.assertEqual(response.status_code, 202)
        self.assertTrue(body['generation_in_progress'])
        self.assertTrue(body['cancel_reminder_pending'])
        self.assertNotIn('cancelled_tasks', body)
        self.assertEqual([task['id'] for task in self.store.tasks], [10])
        retry, retry_body = self.send([chat_reply('different', 'different')], 'cancel-pending', drain=False)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(
            retry_body['cancelled_tasks'],
            [{'task_id': 10, 'notification_id': 'notif-10'}])
        self.assertEqual(self.route._create_json.call_count, 1)

    def test_cancel_latest_freezes_original_task(self):
        now = datetime.now(timezone.utc)
        self.store.tasks.append({
            'id': 10, 'user_id': 'u', 'title': '旧提醒', 'completed': False,
            'created_at': now, 'notification_id': None,
        })
        raw = chat_reply('わかった', '知道了', extra={'cancel_reminder': {'latest': True}})
        receipt.CRASH_BEFORE_EFFECT = 'cancel_reminder'
        with self.assertRaises(RuntimeError):
            self.send([raw], 'cancel-freeze')
        receipt.CRASH_BEFORE_EFFECT = None
        payload = self.store.effects[
            ('u', 'gojo', 'cancel-freeze', 'chat_text', 'cancel_reminder')
        ].get('payload_json') or {}
        self.assertEqual(payload.get('target_task_ids'), [10])
        self.store.tasks.append({
            'id': 11, 'user_id': 'u', 'title': '新提醒', 'completed': False,
            'created_at': now, 'notification_id': None,
        })
        self._drain()
        self.assertEqual(sorted(task['id'] for task in self.store.tasks), [11])
        self.assertEqual(self.route._create_json.call_count, 1)

    def test_cancel_crash_after_delete_does_not_remove_later_task(self):
        now = datetime.now(timezone.utc)
        self.store.tasks.append({
            'id': 10, 'user_id': 'u', 'title': 'old reminder', 'completed': False,
            'created_at': now, 'notification_id': 'notif-10',
        })
        raw = chat_reply('okay', 'ok', extra={'cancel_reminder': {'latest': True}})
        receipt.CRASH_AFTER_EFFECT = 'cancel_reminder'
        with self.assertRaises(RuntimeError):
            self.send([raw], 'cancel-after', drain=False)
        receipt.CRASH_AFTER_EFFECT = None
        self.assertEqual([task['id'] for task in self.store.tasks], [])
        self.store.tasks.append({
            'id': 11, 'user_id': 'u', 'title': 'new reminder', 'completed': False,
            'created_at': now, 'notification_id': 'notif-11',
        })
        key = ('u', 'gojo', 'cancel-after', 'chat_text', 'cancel_reminder')
        self.store.effects[key]['claim_expires_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=5))
        self._drain()
        self.assertEqual([task['id'] for task in self.store.tasks], [11])
        self.assertEqual(self.store.effects[key]['status'], 'completed')
        self.assertEqual(
            self.store.effects[key]['result_json']['cancelled_tasks'],
            [{'task_id': 10, 'notification_id': 'notif-10'}])

    def test_worker_does_not_call_llm(self):
        raw = chat_reply('そうだね', '是啊')
        first, _body = self.send([raw], 'no-llm', drain=False)
        self.assertEqual(first.status_code, 200)
        calls = self.route._create_json.call_count
        self._drain()
        self.assertEqual(self.route._create_json.call_count, calls)


class ImageRouteIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.store = ReceiptStore()
        self.client = Mock()
        router = Mock()
        router.post.side_effect = lambda *_args, **_kwargs: lambda function: function
        self.jobs = Mock()
        self.persist_calls = []
        self.media_row = {
            'id': 'media-1',
            'media_kind': 'image',
            'object_key': 'chat-media/u/gojo/img-1/original.png',
            'mime_type': 'image/png',
            'source_event_id': 'img-1',
        }

        def persist_image(user_id, chat_id, source_event_id, raw, mime_type='image/png',
                          media_kind='image'):
            self.persist_calls.append(source_event_id)
            rec = dict(self.media_row)
            rec['source_event_id'] = source_event_id
            rec['id'] = 'media-' + source_event_id
            return rec

        def public_media(record):
            if not record:
                return None
            return {
                'id': record['id'],
                'kind': record.get('media_kind') or 'image',
                'url': 'https://r2.example/' + str(len(self.persist_calls)),
                'mime_type': record.get('mime_type') or 'image/png',
            }

        def get_media_for_source_event(user_id, chat_id, event_id):
            rec = dict(self.media_row)
            rec['source_event_id'] = event_id
            rec['id'] = 'media-' + event_id
            return rec

        good = json.dumps({
            'emotion': '平静',
            'messages': [
                {'jp': 'この写真すごくきれいだね。', 'zh': '这张照片好漂亮。'},
                {'jp': 'どこで撮ったのか教えてよ。', 'zh': '在哪里拍的告诉我。'},
            ],
            'visual_summary': '红点',
        }, ensure_ascii=False)
        self.client.messages.create.return_value = types.SimpleNamespace(
            content=[types.SimpleNamespace(text=good)],
            stop_reason='end_turn',
            usage=types.SimpleNamespace(),
        )
        modules = {
            'anthropic': stub('anthropic', Anthropic=Mock(return_value=self.client)),
            'fastapi': stub('fastapi', APIRouter=Mock(return_value=router)),
            'fastapi.responses': stub(
                'fastapi.responses',
                JSONResponse=lambda content, status_code=200: types.SimpleNamespace(
                    body=json.dumps(content, ensure_ascii=False, default=str).encode(),
                    status_code=status_code,
                ),
            ),
            'config': stub(
                'config', ANTHROPIC_KEY='', EMOTIONS=['平静'], TTS_PROVIDER='fish',
                DEFAULT_CHARACTER_ID='gojo', MODEL_MAIN='claude-test'),
            'db': stub('db', get_conn=lambda: FakeConn(self.store)),
            'ai_client': stub(
                'ai_client',
                extract_text=lambda response, sep='': response.content[0].text,
                response_metadata=lambda response: {
                    'stop_reason': getattr(response, 'stop_reason', 'end_turn'),
                },
            ),
            'tts': stub('tts', tts_to_b64=Mock(return_value='audio')),
            'prompt': stub(
                'prompt',
                build_system_blocks=Mock(return_value=[{'type': 'text', 'text': 's'}]),
                log_cache_usage=Mock(),
            ),
            'user_memory': stub(
                'user_memory',
                save_short_memory=Mock(),
                save_user_short_memory_once=Mock(return_value=True),
                get_short_memory=Mock(return_value=[]),
                get_short_memory_for_prompt=Mock(return_value=[]),
                attach_short_memory_event_meta=Mock(),
                update_chat_days=Mock(return_value=1),
            ),
            'memory_jobs': stub('memory_jobs', enqueue_private_extraction=self.jobs),
            'temporal_awareness': stub(
                'temporal_awareness',
                get_temporal_snapshot=Mock(return_value={}),
                record_turn=Mock(),
                record_user_message=Mock(),
            ),
            'characters': stub(
                'characters', get_character=Mock(return_value={'voice_id': None})),
            'tasks': stub(
                'tasks',
                find_duplicate_task=Mock(return_value=None),
                find_and_delete_tasks_by_keyword=Mock(return_value=[]),
                delete_latest_task=Mock(return_value=[]),
                find_open_tasks_by_keyword=self._find_open_tasks_by_keyword,
                find_latest_open_task=self._find_latest_open_task,
                delete_tasks_by_ids=self._delete_tasks_by_ids,
            ),
            'task_dedup': stub('task_dedup', find_similar_task=Mock(return_value=None)),
            'relationship_state': stub(
                'relationship_state', save_offline_character_state=Mock()),
            'promise_detector': stub('promise_detector', detect_and_save=Mock()),
            'db_promise': stub('db_promise', add_promise=Mock(return_value=1)),
            'relationship_engine': stub(
                'relationship_engine', process_turn=Mock(return_value={'applied': []})),
            'behavior_evidence': stub(
                'behavior_evidence', record_reply_cycle=Mock()),
            'db_chat_media': stub(
                'db_chat_media',
                persist_image=persist_image,
                public_media=public_media,
                get_media_for_source_event=get_media_for_source_event,
            ),
            'media_storage': stub(
                'media_storage',
                is_configured=Mock(return_value=True),
                signed_get_url=Mock(return_value='https://r2.example/fresh'),
                MediaStorageError=RuntimeError,
            ),
            'reply_availability': stub(
                'reply_availability',
                check_reply_availability=Mock(return_value={'can_reply': True}),
            ),
            'context_layer': stub(
                'context_layer',
                append_current_user_turn=lambda msgs, content: list(msgs or []) + [
                    {'role': 'user', 'content': content}],
            ),
        }
        self.receipt_patchers = [
            patch.object(receipt, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(receipt, 'DEFAULT_WAIT_SECONDS', 0.4),
            patch.object(receipt, 'DEFAULT_POLL_SECONDS', 0.05),
        ]
        for item in self.receipt_patchers:
            item.start()
            self.addCleanup(item.stop)
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.route = load_source('route_image', modules)
        hb_patch = patch.object(receipt, 'GenerationHeartbeat', _PassthroughHeartbeat)
        hb_patch.start()
        self.addCleanup(hb_patch.stop)
        rel_patch = patch(
            'generation_effects.apply_relationship_update',
            return_value={})
        pd_patch = patch(
            'generation_effects.apply_promise_detector',
            return_value={})
        rel_patch.start()
        pd_patch.start()
        self.addCleanup(rel_patch.stop)
        self.addCleanup(pd_patch.stop)
        log_patch = patch('builtins.print')
        log_patch.start()
        self.addCleanup(log_patch.stop)
        self.addCleanup(self._reset_crash_flags)

    def _find_open_tasks_by_keyword(self, user_id, keyword, latest_only=True):
        return []

    def _find_latest_open_task(self, user_id):
        return []

    def _delete_tasks_by_ids(self, user_id, task_ids):
        ids = {int(tid) for tid in (task_ids or []) if tid is not None}
        deleted = []
        kept = []
        for task in self.store.tasks:
            if task['id'] in ids:
                deleted.append((task['id'], task.get('notification_id')))
            else:
                kept.append(task)
        self.store.tasks = kept
        return deleted

    def _reset_crash_flags(self):
        receipt.CRASH_BEFORE_COMPLETE = False
        receipt.CRASH_BEFORE_EFFECT = None
        receipt.CRASH_AFTER_EFFECT = None

    def _drain(self):
        from generation_side_effect_worker import process_due_side_effects
        for _ in range(24):
            ran = process_due_side_effects(limit=20)
            if not ran:
                break

    def send(self, source_event_id='img-1', drain=True, **extra):
        data = {
            'user_id': 'u',
            'character_id': 'gojo',
            'image_base64': PNG_B64,
            'text': '看',
            'source_event_id': source_event_id,
        }
        data.update(extra)
        response = asyncio.run(self.route.chat_image(data))
        if drain and getattr(response, 'status_code', None) == 200:
            self._drain()
        return response, json.loads(response.body)

    def test_image_retry_one_object_and_one_llm(self):
        first, body1 = self.send('img-same')
        second, body2 = self.send('img-same')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(len(self.persist_calls), 2)
        self.assertEqual(self.client.messages.create.call_count, 1)
        self.assertEqual(self.jobs.call_count, 1)
        self.assertEqual(body1['assistant_turn_id'], 'image_reply:img-same')
        self.assertEqual(
            [m['event_id'] for m in body1['messages']],
            ['image_reply:img-same:0', 'image_reply:img-same:1'])
        self.assertEqual(body2['media']['id'], body1['media']['id'])
        self.assertTrue(str(body2['media']['url']).startswith('https://r2.example/'))
        self.assertEqual(body2['messages'][0]['jp'], body1['messages'][0]['jp'])

    def test_r2_success_generation_failed_keeps_media_and_retries(self):
        self.client.messages.create.side_effect = RuntimeError('llm down')
        first, body1 = self.send('img-fail')
        self.assertEqual(first.status_code, 502)
        self.assertEqual(body1['media']['id'], 'media-img-fail')
        self.assertEqual(len(self.persist_calls), 1)
        good = json.dumps({
            'emotion': '平静',
            'messages': [{'jp': 'わかったよ。', 'zh': '知道了。'}],
        }, ensure_ascii=False)
        self.client.messages.create.side_effect = None
        self.client.messages.create.return_value = types.SimpleNamespace(
            content=[types.SimpleNamespace(text=good)],
            stop_reason='end_turn',
            usage=types.SimpleNamespace(),
        )
        second, body2 = self.send('img-fail')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(body2['messages'][0]['jp'], 'わかったよ。')
        self.assertEqual(body2['media']['id'], 'media-img-fail')

    def test_image_crash_before_extraction_then_replay_once(self):
        receipt.CRASH_BEFORE_EFFECT = 'private_extraction'
        with self.assertRaises(RuntimeError):
            self.send('img-crash-pe')
        receipt.CRASH_BEFORE_EFFECT = None
        self.assertEqual(self.jobs.call_count, 0)
        second, body = self.send('img-crash-pe')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.client.messages.create.call_count, 1)
        self.assertEqual(self.jobs.call_count, 1)
        self.assertEqual(body['assistant_turn_id'], 'image_reply:img-crash-pe')

    def test_image_crash_before_complete_no_assistant_effects(self):
        receipt.CRASH_BEFORE_COMPLETE = True
        with self.assertRaises(RuntimeError):
            self.send('img-pre-complete')
        receipt.CRASH_BEFORE_COMPLETE = False
        self.assertEqual(self.jobs.call_count, 0)
        rec = self.store.receipts.get(('u', 'gojo', 'img-pre-complete', 'chat_image'))
        self.assertEqual(rec['status'], 'processing')

    def test_image_reminder_crash_after_create_stays_once(self):
        good = json.dumps({
            'emotion': '平静',
            'messages': [{'jp': 'わかったよ。', 'zh': '知道了。'}],
            'reminder': {
                'date': '2026-09-20', 'time': '10:00',
                'content': '喝水', 'notification': '喝水',
            },
        }, ensure_ascii=False)
        self.client.messages.create.return_value = types.SimpleNamespace(
            content=[types.SimpleNamespace(text=good)],
            stop_reason='end_turn',
            usage=types.SimpleNamespace(),
        )
        receipt.CRASH_AFTER_EFFECT = 'reminder'
        with self.assertRaises(RuntimeError):
            self.send('img-crash-rem')
        receipt.CRASH_AFTER_EFFECT = None
        self.assertEqual(len(self.store.tasks), 1)
        key = ('u', 'gojo', 'img-crash-rem', 'chat_image', 'reminder')
        self.store.effects[key]['claim_expires_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=5))
        self.send('img-crash-rem')
        self.assertEqual(len(self.store.tasks), 1)
        self.assertEqual(self.client.messages.create.call_count, 1)

    def test_image_completed_replay_repairs_pending(self):
        first, _body = self.send('img-repair')
        self.assertEqual(first.status_code, 200)
        key = ('u', 'gojo', 'img-repair', 'chat_image', 'private_extraction')
        self.store.effects[key]['status'] = 'pending'
        self.store.effects[key]['claim_token'] = None
        self.jobs.reset_mock()
        second, _body2 = self.send('img-repair')
        self.assertEqual(second.status_code, 200)
        self.assertEqual(self.client.messages.create.call_count, 1)
        self.assertEqual(self.jobs.call_count, 1)

    def test_image_worker_repairs_without_user_retry(self):
        first, _body = self.send('img-worker', drain=False)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(self.client.messages.create.call_count, 1)
        key = ('u', 'gojo', 'img-worker', 'chat_image', 'relationship_update')
        self.assertEqual(self.store.effects[key]['status'], 'pending')
        self._drain()
        self.assertEqual(self.store.effects[key]['status'], 'completed')
        self.assertEqual(self.client.messages.create.call_count, 1)


class RelationshipGateTests(unittest.TestCase):
    def test_process_turn_has_event_gate(self):
        src = Path(BACKEND, 'relationship_engine.py').read_text(encoding='utf-8')
        self.assertIn("skipped='already_processed'", src)
        chat = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        self.assertIn("relationship_update", chat)
        self.assertIn('commit_and_run_effects', chat)
        self.assertNotIn('_once_effect', chat)
        self.assertIn('source={source_event_id', chat)
        self.assertIn('[rel_update] start', chat)
        effects = Path(BACKEND, 'generation_effects.py').read_text(encoding='utf-8')
        self.assertIn('apply_relationship_update', effects)
        self.assertNotIn('daemon=True', effects)


class MemoryEnqueueOnceTests(unittest.TestCase):
    def test_done_jobs_are_not_requeued(self):
        src = Path(BACKEND, 'memory_jobs.py').read_text(encoding='utf-8')
        self.assertIn("status IN ('pending', 'running', 'done')", src)


class FrontendStableIdTests(unittest.TestCase):
    def setUp(self):
        self.src = Path(FRONTEND_CHAT).read_text(encoding='utf-8')

    def test_append_segments_prefers_backend_ids(self):
        self.assertIn('function assistantSegmentId', self.src)
        self.assertIn('seg?.event_id', self.src)
        self.assertIn('assistantTurnId', self.src)
        self.assertIn('generation_in_progress', self.src)
        self.assertIn('仍在生成', self.src)
        self.assertIn('list.some(item => item.id === msgId)', self.src)
        self.assertNotIn("appendSegments(segments, `${Date.now()}`", self.src)

    def test_duplicate_consume_keeps_one_bubble(self):
        def assistant_segment_id(seg, assistant_turn_id, index):
            event_id = str((seg or {}).get('event_id') or '').strip()
            if event_id:
                return event_id
            turn_id = str(assistant_turn_id or '').strip()
            if turn_id:
                return f'{turn_id}:{index}'
            return f'legacy_{index}'

        segments = [
            {'jp': 'a', 'zh': '1', 'event_id': 'chat_reply:x:0'},
            {'jp': 'b', 'zh': '2', 'event_id': 'chat_reply:x:1'},
        ]
        ids = []
        for _consume in range(2):
            for index, seg in enumerate(segments):
                msg_id = assistant_segment_id(seg, 'chat_reply:x', index)
                if msg_id not in ids:
                    ids.append(msg_id)
        self.assertEqual(ids, ['chat_reply:x:0', 'chat_reply:x:1'])

    def test_in_progress_keeps_source_event_id(self):
        self.assertIn('lastFailedSendRef.current.sourceEventId === keepId', self.src)
        self.assertIn('{ retry: true }', self.src)
        self.assertIn('setGenerationInProgress(true)', self.src)


    def test_reminder_missing_task_id_does_not_schedule(self):
        self.assertIn("if (!data.reminder.task_id)", self.src)
        self.assertIn('[reminder] skip local schedule: missing task_id', self.src)
        self.assertIn('await scheduleReminder(data.reminder)', self.src)


class HeartbeatAndWorkerTests(unittest.TestCase):
    def setUp(self):
        self.store = ReceiptStore()
        self.patchers = [
            patch.object(receipt, 'get_conn', side_effect=lambda: FakeConn(self.store)),
            patch.object(receipt, 'DEFAULT_LEASE_SECONDS', 1),
            patch.object(receipt, 'DEFAULT_EFFECT_LEASE_SECONDS', 1),
            patch.object(receipt, 'DEFAULT_HEARTBEAT_INTERVAL_SECONDS', 0.2),
            patch.dict(sys.modules, {
                'db_chat_media': stub(
                    'db_chat_media',
                    get_media_for_source_event=Mock(return_value=None),
                    public_media=Mock(return_value=None),
                ),
                'characters': stub('characters', get_character=Mock(return_value={})),
                'tts': stub('tts', tts_to_b64=Mock(return_value='audio')),
            }),
        ]
        for item in self.patchers:
            item.start()
            self.addCleanup(item.stop)
        self.addCleanup(self._reset)

    def _reset(self):
        receipt.CRASH_BEFORE_COMPLETE = False
        receipt.CRASH_BEFORE_EFFECT = None
        receipt.CRASH_AFTER_EFFECT = None
        from generation_side_effect_worker import stop_generation_side_effect_worker
        stop_generation_side_effect_worker()

    def _claim(self, source='evt-1', lease=1):
        return receipt.claim_generation(
            'u', 'gojo', source, receipt.ENDPOINT_CHAT_TEXT, lease_seconds=lease)

    def _complete(self, source, payload=None, effects=None, effect_payloads=None, lease=2):
        claim = self._claim(source, lease=lease)
        self.assertTrue(claim.get('owned'))
        receipt.complete_generation(
            'u', 'gojo', source, receipt.ENDPOINT_CHAT_TEXT,
            claim['claim_token'],
            payload or {'ok': True},
            effects=effects,
            effect_payloads=effect_payloads)
        return claim

    def test_renew_requires_current_token(self):
        first = self._claim('renew-token', lease=2)
        self.assertTrue(first.get('owned'))
        token = first['claim_token']
        ok = receipt.renew_generation_lease(
            'u', 'gojo', 'renew-token', receipt.ENDPOINT_CHAT_TEXT, token, lease_seconds=2)
        self.assertTrue(ok)
        bad = receipt.renew_generation_lease(
            'u', 'gojo', 'renew-token', receipt.ENDPOINT_CHAT_TEXT,
            '00000000-0000-0000-0000-000000000000', lease_seconds=2)
        self.assertFalse(bad)
        rec = self.store.receipts[('u', 'gojo', 'renew-token', 'chat_text')]
        self.assertEqual(rec['claim_token'], token)

    def test_heartbeat_keeps_lease_so_second_cannot_reclaim(self):
        first = self._claim('hb-keep', lease=1)
        token = first['claim_token']
        hb = receipt.GenerationHeartbeat(
            'u', 'gojo', 'hb-keep', receipt.ENDPOINT_CHAT_TEXT, token,
            interval_seconds=0.15, lease_seconds=1)
        hb.start()
        try:
            time.sleep(1.3)
            second = self._claim('hb-keep', lease=1)
            self.assertFalse(second.get('owned'))
            self.assertEqual(second.get('status'), 'processing')
        finally:
            hb.stop()

    def test_heartbeat_stop_allows_reclaim_after_expiry(self):
        first = self._claim('hb-crash', lease=1)
        token = first['claim_token']
        hb = receipt.GenerationHeartbeat(
            'u', 'gojo', 'hb-crash', receipt.ENDPOINT_CHAT_TEXT, token,
            interval_seconds=0.15, lease_seconds=1)
        hb.start()
        time.sleep(0.25)
        hb.stop()
        time.sleep(1.15)
        second = self._claim('hb-crash', lease=1)
        self.assertTrue(second.get('owned'))
        self.assertNotEqual(second.get('claim_token'), token)

    def test_worker_repairs_pending_without_user_retry(self):
        from generation_side_effect_worker import process_due_side_effects
        self._complete(
            'w-pending',
            payload={'ok': True},
            effects=['relationship_update'],
            effect_payloads={'relationship_update': {'source_event_id': 'w-pending'}})
        applied = []

        def _apply(kind, ctx):
            applied.append(kind)
            return {'applied': True}

        ran = process_due_side_effects(limit=20, apply_fn=_apply)
        self.assertTrue(ran)
        self.assertEqual(applied, ['relationship_update'])
        row = self.store.effects[
            ('u', 'gojo', 'w-pending', 'chat_text', 'relationship_update')]
        self.assertEqual(row['status'], 'completed')

    def test_worker_retries_transient_failure(self):
        from generation_side_effect_worker import process_one_side_effect, process_due_side_effects
        self._complete(
            'w-fail', payload={'ok': True}, effects=['behavior_evidence'])
        calls = {'n': 0}

        def _apply(kind, ctx):
            calls['n'] += 1
            if calls['n'] == 1:
                raise RuntimeError('transient')
            return {'ok': True}

        first = process_one_side_effect(
            self.store.effects[('u', 'gojo', 'w-fail', 'chat_text', 'behavior_evidence')],
            apply_fn=_apply)
        self.assertFalse(first)
        row = self.store.effects[('u', 'gojo', 'w-fail', 'chat_text', 'behavior_evidence')]
        self.assertEqual(row['status'], 'failed')
        row['claim_expires_at'] = datetime.now(timezone.utc) - timedelta(seconds=2)
        process_due_side_effects(limit=10, apply_fn=_apply)
        self.assertEqual(calls['n'], 2)
        self.assertEqual(
            self.store.effects[('u', 'gojo', 'w-fail', 'chat_text', 'behavior_evidence')]['status'],
            'completed')

    def test_two_workers_apply_once(self):
        from generation_side_effect_worker import process_due_side_effects
        self._complete(
            'w-race', payload={'ok': True}, effects=['assistant_short_memory'])
        applied = []

        def _apply(kind, ctx):
            applied.append(kind)
            time.sleep(0.05)
            return {'ok': True}

        t1 = threading.Thread(target=lambda: process_due_side_effects(limit=5, apply_fn=_apply))
        t2 = threading.Thread(target=lambda: process_due_side_effects(limit=5, apply_fn=_apply))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(applied, ['assistant_short_memory'])
        self.assertEqual(
            self.store.effects[('u', 'gojo', 'w-race', 'chat_text', 'assistant_short_memory')]['status'],
            'completed')

    def test_stale_processing_is_reclaimed(self):
        from generation_side_effect_worker import process_due_side_effects
        self._complete('w-stale', payload={'ok': True}, effects=['record_turn'])
        key = ('u', 'gojo', 'w-stale', 'chat_text', 'record_turn')
        claimed = receipt.claim_side_effect(
            'u', 'gojo', 'w-stale', receipt.ENDPOINT_CHAT_TEXT, 'record_turn')
        self.assertTrue(claimed.get('owned'))
        self.store.effects[key]['claim_expires_at'] = (
            datetime.now(timezone.utc) - timedelta(seconds=2))
        applied = []

        def _apply(kind, ctx):
            applied.append(kind)
            return {'ok': True}

        process_due_side_effects(limit=10, apply_fn=_apply)
        self.assertEqual(applied, ['record_turn'])
        self.assertEqual(self.store.effects[key]['status'], 'completed')

    def test_start_is_single_thread_per_process(self):
        from generation_side_effect_worker import (
            start_generation_side_effect_worker,
            stop_generation_side_effect_worker,
        )
        loops = []

        def fake_loop():
            loops.append(1)
            time.sleep(0.2)

        with patch('generation_side_effect_worker._loop', side_effect=fake_loop):
            start_generation_side_effect_worker()
            start_generation_side_effect_worker()
            time.sleep(0.05)
            stop_generation_side_effect_worker()
        self.assertEqual(len(loops), 1)

    def test_hydrate_merges_result_json_without_mutating_receipt(self):
        self._complete(
            'hydrate-1',
            payload={'messages': [{'jp': 'a', 'zh': '1'}], 'emotion': '平静'},
            effects=['reminder'],
            effect_payloads={
                'reminder': {'date': '2026-09-20', 'time': '10:00', 'content': '喝水'},
            })
        owned = receipt.claim_side_effect(
            'u', 'gojo', 'hydrate-1', receipt.ENDPOINT_CHAT_TEXT, 'reminder')
        self.assertTrue(owned.get('owned'))
        receipt.complete_side_effect(
            'u', 'gojo', 'hydrate-1', receipt.ENDPOINT_CHAT_TEXT, 'reminder',
            owned['claim_token'], result_json={'task_id': 77, 'duplicate': False})
        rec = self.store.receipts[('u', 'gojo', 'hydrate-1', 'chat_text')]
        body = receipt.hydrate_completed_generation_response(
            'u', 'gojo', 'hydrate-1', receipt.ENDPOINT_CHAT_TEXT)
        self.assertEqual(body['reminder']['task_id'], 77)
        self.assertNotIn('task_id', json.dumps(rec['response_json']))


class InitAndSchemaTests(unittest.TestCase):
    def test_server_inits_table(self):
        src = Path(BACKEND, 'gojo_server.py').read_text(encoding='utf-8')
        self.assertIn('init_generation_receipt_table', src)
        self.assertIn('start_generation_side_effect_worker', src)

    def test_schema_sql(self):
        src = Path(BACKEND, 'db_generation_receipt.py').read_text(encoding='utf-8')
        self.assertIn('CREATE TABLE IF NOT EXISTS chat_generation_receipt', src)
        self.assertIn('UNIQUE (user_id, character_id, source_event_id, endpoint)', src)
        self.assertIn('chat_generation_side_effect', src)
        self.assertIn("status TEXT NOT NULL DEFAULT 'pending'", src)
        self.assertIn('claim_expires_at', src)
        self.assertIn('payload_json', src)
        self.assertIn('result_json', src)
        self.assertIn('renew_generation_lease', src)
        self.assertIn('hydrate_completed_generation_response', src)
        self.assertIn('ensure_completed_generation_effects', src)
        self.assertIn('[chat:idempotency] legacy request without source_event_id', src)
        self.assertIn("ENDPOINT_CHAT_TEXT = 'chat_text'", src)
        self.assertIn("ENDPOINT_CHAT_IMAGE = 'chat_image'", src)
        self.assertNotIn('threading.Lock', src)
        self.assertNotIn('try_once_side_effect', src)

    def test_effects_freeze_cancel_targets(self):
        src = Path(BACKEND, 'generation_effects.py').read_text(encoding='utf-8')
        self.assertIn('resolve_cancel_targets', src)
        self.assertIn('target_task_ids', src)
        self.assertNotIn('_claim_cancel_occurrence', src)
        self.assertIn('SYNC_SKIP_EFFECTS', src)


if __name__ == '__main__':
    unittest.main()
