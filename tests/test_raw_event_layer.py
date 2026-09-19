import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import db_chatlog  # noqa: E402
import raw_events  # noqa: E402
import user_memory  # noqa: E402


class LayerStore:
    def __init__(self):
        self.next_id = 1
        self.chat_log = []
        self.tombstones = set()
        self.short_memory = []
        self.annotations = {}
        self.processing = {}
        self.derived = {}
        self.source_map = []
        self.long_memory = []
        self.lifecycle = []
        self.sql = []

    def alloc(self):
        value = self.next_id
        self.next_id += 1
        return value


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
        compact = ' '.join(sql.split())
        params = tuple(params or ())
        self.store.sql.append((compact, params))
        self.rowcount = 0
        self._one = None
        self._many = []
        lower = compact.lower()

        if (compact.startswith('CREATE ') or compact.startswith('ALTER TABLE')
                or compact.startswith('UPDATE chat_log SET event_id')
                or compact.startswith('UPDATE chat_log SET status =')
                or 'information_schema.columns' in compact):
            if 'information_schema.columns' in compact:
                self._one = ('timestamp with time zone',)
            return

        if compact.startswith('SELECT 1 FROM chat_log_tombstone'):
            if len(params) == 3:
                self._one = (1,) if params in self.store.tombstones else None
            elif len(params) == 1:
                event_id = params[0]
                self._one = (1,) if any(
                    t[2] == event_id for t in self.store.tombstones) else None
            return

        if compact.startswith('SELECT client_msg_id FROM chat_log_tombstone'):
            user_id, chat_id = params
            self._many = [
                (cid,) for uid, cid_chat, cid in self.store.tombstones
                if uid == user_id and cid_chat == chat_id
            ]
            return

        if compact.startswith('INSERT INTO chat_log_tombstone'):
            if 'SELECT' in compact:
                return
            user_id, chat_id, client_msg_id = params
            key = (user_id, chat_id, client_msg_id)
            if key not in self.store.tombstones:
                self.store.tombstones.add(key)
                self.rowcount = 1
            return

        if compact.startswith('INSERT INTO chat_log') and 'FROM short_memory' in compact:
            keyed = 0
            for row in self.store.short_memory:
                eid = row.get('source_event_id')
                if not eid:
                    continue
                key = (row['user_id'], row['character_id'], eid)
                if key in self.store.tombstones:
                    continue
                if any(
                    r['user_id'] == row['user_id']
                    and r['chat_id'] == row['character_id']
                    and r.get('client_msg_id') == eid
                    for r in self.store.chat_log
                ):
                    continue
                self.store.chat_log.append({
                    'id': self.store.alloc(),
                    'user_id': row['user_id'],
                    'chat_id': row['character_id'],
                    'client_msg_id': eid,
                    'event_id': eid,
                    'role': 'user' if row['role'] == 'user' else 'gojo',
                    'text': row['content'],
                    'subtitle': '',
                    'emotion': '',
                    'kind': 'text',
                    'extra': row.get('event_meta') or '',
                    'has_audio': False,
                    'created_at': row.get('timestamp') or datetime.now(timezone.utc),
                    'status': 'active',
                    'reply_to_event_id': '',
                })
                keyed += 1
            self.rowcount = keyed
            return

        if compact.startswith('INSERT INTO chat_log'):
            user_id, chat_id, client_msg_id, role, text, subtitle, emotion, kind, extra, has_audio = params[:10]
            event_id = client_msg_id
            created_at = datetime.now(timezone.utc)
            reply_to = ''
            if 'created_at' in compact.lower() and len(params) >= 13:
                created_at = params[10] or created_at
                event_id = params[11] or client_msg_id
                reply_to = params[12] or ''
            elif len(params) >= 12:
                event_id = params[10] or client_msg_id
                reply_to = params[11] or ''
            if client_msg_id and (user_id, chat_id, client_msg_id) in self.store.tombstones:
                self.rowcount = 0
                return
            for row in self.store.chat_log:
                if (row['user_id'] == user_id and row['chat_id'] == chat_id
                        and client_msg_id and row.get('client_msg_id') == client_msg_id):
                    self.rowcount = 0
                    return
                if (row['user_id'] == user_id and row['chat_id'] == chat_id
                        and event_id and row.get('event_id') == event_id):
                    self.rowcount = 0
                    return
            self.store.chat_log.append({
                'id': self.store.alloc(),
                'user_id': user_id,
                'chat_id': chat_id,
                'client_msg_id': client_msg_id,
                'event_id': event_id,
                'role': role,
                'text': text,
                'subtitle': subtitle,
                'emotion': emotion,
                'kind': kind,
                'extra': extra,
                'has_audio': bool(has_audio),
                'created_at': created_at if not isinstance(created_at, str) else datetime.now(timezone.utc),
                'status': 'active',
                'reply_to_event_id': reply_to,
            })
            self.rowcount = 1
            return

        if compact.startswith('UPDATE chat_log') and "status='deleted'" in compact.replace(' ', ''):
            n = 0
            if 'WHERE id=%s' in compact:
                server_id, user_id, chat_id = params
                for row in self.store.chat_log:
                    if (row['id'] == server_id and row['user_id'] == user_id
                            and row['chat_id'] == chat_id and row.get('status') == 'active'):
                        row['status'] = 'deleted'
                        n += 1
            elif 'AND client_msg_id=%s' in compact:
                user_id, chat_id, client_msg_id = params
                for row in self.store.chat_log:
                    if (row['user_id'] == user_id and row['chat_id'] == chat_id
                            and row['client_msg_id'] == client_msg_id
                            and row.get('status') == 'active'):
                        row['status'] = 'deleted'
                        n += 1
            self.rowcount = n
            return

        if compact.startswith('UPDATE chat_log') and 'SET extra=' in compact:
            extra, event_id, event_id2 = params
            for row in self.store.chat_log:
                if row.get('event_id') == event_id or row.get('client_msg_id') == event_id2:
                    if row.get('status', 'active') == 'active':
                        row['extra'] = extra
                        self.rowcount += 1
            return

        if compact.startswith('SELECT extra FROM chat_log'):
            event_id, event_id2 = params
            for row in reversed(self.store.chat_log):
                if row.get('event_id') == event_id or row.get('client_msg_id') == event_id2:
                    self._one = (row.get('extra') or '',)
                    return
            return

        if compact.startswith('SELECT event_id, client_msg_id, role, text, extra, created_at'):
            user_id, chat_id = params
            matched = [
                row for row in self.store.chat_log
                if row['user_id'] == user_id and row['chat_id'] == chat_id
                and row.get('status', 'active') == 'active'
            ]
            matched.sort(key=lambda r: r['id'])
            self._many = [
                (r.get('event_id'), r.get('client_msg_id'), r['role'], r['text'],
                 r.get('extra') or '', r['created_at'])
                for r in matched
            ]
            return

        if compact.startswith('SELECT COALESCE(event_id, client_msg_id)'):
            if len(params) == 2:
                user_id, chat_id = params
                self._many = [
                    (row.get('event_id') or row.get('client_msg_id'),)
                    for row in self.store.chat_log
                    if row['user_id'] == user_id and row['chat_id'] == chat_id
                    and row.get('status') == 'deleted'
                ]
                return
            user_id, chat_id, hours, limit = params
            matched = [
                row for row in self.store.chat_log
                if row['user_id'] == user_id and row['chat_id'] == chat_id
                and row.get('status', 'active') == 'active'
            ]
            matched.sort(key=lambda r: r['id'], reverse=True)
            matched = matched[:limit]
            self._many = [
                (r.get('event_id') or r.get('client_msg_id'), r['role'], r['text'],
                 r['kind'], r['extra'], r['created_at'], r.get('subtitle') or '')
                for r in matched
            ]
            return

        if compact.startswith('SELECT 1 FROM chat_log'):
            if len(params) == 4:
                user_id, chat_id, event_id, event_id2 = params
                self._one = (1,) if any(
                    row['user_id'] == user_id and row['chat_id'] == chat_id
                    and (row.get('event_id') == event_id or row.get('client_msg_id') == event_id2)
                    and row.get('status') == 'deleted'
                    for row in self.store.chat_log
                ) else None
            else:
                event_id, event_id2 = params
                self._one = (1,) if any(
                    (row.get('event_id') == event_id or row.get('client_msg_id') == event_id2)
                    and row.get('status') == 'deleted'
                    for row in self.store.chat_log
                ) else None
            return

        if compact.startswith('SELECT id, client_msg_id'):
            user_id, chat_id, limit = params[:3] if 'AND id <' not in compact else params[:4]
            if 'AND id <' in compact:
                user_id, chat_id, before_id, limit = params
                matched = [
                    row for row in self.store.chat_log
                    if row['user_id'] == user_id and row['chat_id'] == chat_id
                    and row['id'] < before_id and row.get('status') == 'active'
                ]
            else:
                user_id, chat_id, limit = params
                matched = [
                    row for row in self.store.chat_log
                    if row['user_id'] == user_id and row['chat_id'] == chat_id
                    and row.get('status') == 'active'
                ]
            matched.sort(key=lambda r: r['id'], reverse=True)
            matched = matched[:limit]
            self._many = [
                (r['id'], r['client_msg_id'], r['role'], r['text'],
                 r['subtitle'], r['emotion'], r['kind'], r['extra'],
                 r['has_audio'], r['created_at'], r.get('event_id') or '')
                for r in matched
            ]
            return

        if compact.startswith('INSERT INTO short_memory'):
            if 'ON CONFLICT' in compact:
                if ", 'user'," in compact or "VALUES (%s, %s, 'user'" in compact:
                    user_id, character_id, content, event_id, event_meta = params
                    role = 'user'
                else:
                    user_id, character_id, role, content, event_id = params[:5]
                    event_meta = params[5] if len(params) > 5 else ''
                for row in self.store.short_memory:
                    if (row['user_id'] == user_id and row['character_id'] == character_id
                            and row['role'] == role
                            and row.get('source_event_id') == event_id):
                        self._one = None
                        return
                row_id = self.store.alloc()
                self.store.short_memory.append({
                    'id': row_id,
                    'user_id': user_id,
                    'character_id': character_id,
                    'role': role,
                    'content': content,
                    'source_event_id': event_id,
                    'event_meta': event_meta,
                    'timestamp': datetime.now(timezone.utc),
                })
                self._one = (row_id,)
                self.rowcount = 1
                return
            user_id, character_id, role, content, event_id = params[:5]
            event_meta = params[5] if len(params) > 5 else ''
            self.store.short_memory.append({
                'id': self.store.alloc(),
                'user_id': user_id,
                'character_id': character_id,
                'role': role,
                'content': content,
                'source_event_id': event_id,
                'event_meta': event_meta,
                'timestamp': datetime.now(timezone.utc),
            })
            self.rowcount = 1
            return

        if compact.startswith('DELETE FROM short_memory'):
            return

        if compact.startswith('SELECT role, content, timestamp'):
            user_id, character_id, hours, limit = params
            matched = [
                row for row in self.store.short_memory
                if row['user_id'] == user_id and row['character_id'] == character_id
            ]
            matched.sort(key=lambda r: r['id'], reverse=True)
            matched = matched[:limit]
            if 'source_event_id' in compact:
                self._many = [
                    (r['role'], r['content'], r['timestamp'],
                     r.get('source_event_id'), r.get('event_meta') or '')
                    for r in matched
                ]
            else:
                self._many = [(r['role'], r['content'], r['timestamp']) for r in matched]
            return

        if compact.startswith('INSERT INTO event_annotations'):
            event_id, annotation_type, processor_version, payload = params
            key = (event_id, annotation_type, processor_version)
            if key in self.store.annotations:
                self._one = None
                return
            self.store.annotations[key] = payload
            self._one = (event_id,)
            self.rowcount = 1
            return

        if compact.startswith('SELECT status FROM event_processing_state'):
            key = params
            row = self.store.processing.get(key)
            self._one = (row['status'],) if row else None
            return

        if compact.startswith('INSERT INTO event_processing_state'):
            if 'DO UPDATE SET' in compact and 'retry_count' in compact:
                event_id, processor_type, processor_version = params[:3]
                key = (event_id, processor_type, processor_version)
                existing = self.store.processing.get(key)
                if existing and existing.get('status') != 'succeeded':
                    existing['retry_count'] = existing.get('retry_count', 0) + 1
                    existing['status'] = 'processing'
                elif not existing:
                    self.store.processing[key] = {
                        'status': 'processing', 'retry_count': 0,
                    }
                self.rowcount = 1
                return
            event_id, processor_type, processor_version, status = params[:4]
            result_ref = params[4] if len(params) > 4 else None
            last_error = params[5] if len(params) > 5 else None
            key = (event_id, processor_type, processor_version)
            self.store.processing[key] = {
                'status': status,
                'result_ref': result_ref,
                'last_error': last_error,
                'retry_count': self.store.processing.get(key, {}).get('retry_count', 0),
            }
            self.rowcount = 1
            return

        if compact.startswith('SELECT 1 FROM derived_memory_idempotency'):
            key = params
            self._one = (1,) if key in self.store.derived else None
            return

        if compact.startswith('INSERT INTO derived_memory_idempotency'):
            processor_type, processor_version, source_key, memory_type, memory_id = params
            key = (processor_type, processor_version, source_key)
            if key in self.store.derived:
                self.rowcount = 0
                return
            self.store.derived[key] = {
                'memory_type': memory_type, 'memory_id': memory_id,
            }
            self.rowcount = 1
            return

        if compact.startswith('INSERT INTO memory_source_events'):
            memory_type, memory_id, source_event_id = params
            item = (memory_type, memory_id, source_event_id)
            if item in self.store.source_map:
                self.rowcount = 0
                return
            self.store.source_map.append(item)
            self.rowcount = 1
            return

        if compact.startswith('SELECT memory_type, memory_id FROM memory_source_events'):
            event_id = params[0]
            self._many = [
                (mt, mid) for mt, mid, sid in self.store.source_map if sid == event_id
            ]
            return

        if compact.startswith('DELETE FROM memory_source_events'):
            event_id = params[0]
            before = len(self.store.source_map)
            self.store.source_map = [
                item for item in self.store.source_map if item[2] != event_id
            ]
            self.rowcount = before - len(self.store.source_map)
            return

        if compact.startswith('SELECT COUNT(*) FROM memory_source_events'):
            memory_type, memory_id = params
            self._one = (sum(
                1 for mt, mid, _sid in self.store.source_map
                if mt == memory_type and mid == memory_id
            ),)
            return

        if compact.startswith('INSERT INTO long_memory'):
            row_id = self.store.alloc()
            self.store.long_memory.append({
                'id': row_id,
                'user_id': params[0],
                'character_id': params[1],
                'content': params[2],
                'recall_status': 'active',
                'source_event_refs': params[6] if len(params) > 6 else '[]',
            })
            self._one = (row_id,)
            self.rowcount = 1
            return

        if compact.startswith('SELECT content FROM long_memory'):
            self._many = [(row['content'],) for row in self.store.long_memory
                          if row.get('recall_status') == 'active']
            return

        if compact.startswith('SELECT id, source_event_refs FROM long_memory'):
            self._many = [
                (row['id'], row.get('source_event_refs') or '[]')
                for row in self.store.long_memory
                if row.get('recall_status') == 'active'
            ]
            return

        if compact.startswith('UPDATE long_memory'):
            if 'source_event_refs' in compact and 'recall_status' not in compact:
                refs, row_id = params
                for row in self.store.long_memory:
                    if row['id'] == row_id:
                        row['source_event_refs'] = refs
                        self.rowcount = 1
                return
            row_id = params[-1]
            for row in self.store.long_memory:
                if row['id'] == row_id:
                    row['recall_status'] = 'deleted'
                    self.rowcount = 1
            return

        if compact.startswith('UPDATE memory_lifecycle_items'):
            if "status = 'reactivated'" in compact:
                _expires, user_id, character_id, topic_key = params[:4]
                count = 0
                for row in self.store.lifecycle:
                    if (row.get('user_id') == user_id
                            and row.get('character_id') == character_id
                            and row.get('topic_key') == topic_key
                            and row.get('status') in ('archived', 'expired')):
                        row['status'] = 'reactivated'
                        row['decay_state'] = 'active'
                        count += 1
                self.rowcount = count
                return
            row_id = params[-1]
            for row in self.store.lifecycle:
                if row['id'] == row_id:
                    row['status'] = 'archived'
                    row['decay_state'] = 'dormant'
                    self.rowcount = 1
            return

        if compact.startswith('SELECT id FROM memory_jobs'):
            self._one = None
            return

        raise AssertionError(f'unhandled SQL in raw-event fake: {compact}')


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class RawEventLayerTests(unittest.TestCase):
    def setUp(self):
        self.store = LayerStore()
        self.conn = FakeConn(self.store)
        self.patchers = [
            patch('db.get_conn', side_effect=lambda: FakeConn(self.store)),
            patch('db_chatlog.get_conn', side_effect=lambda: FakeConn(self.store)),
            patch('raw_events.get_conn', side_effect=lambda: FakeConn(self.store)),
            patch('user_memory.get_conn', side_effect=lambda: FakeConn(self.store)),
            patch('builtins.print'),
        ]
        for item in self.patchers:
            item.start()
        self.db_chatlog = db_chatlog
        self.raw_events = raw_events
        self.user_memory = user_memory
        db_chatlog.init_chatlog_table()
        raw_events.init_raw_event_layer()

    def tearDown(self):
        for item in self.patchers:
            item.stop()

    def active_events(self, user_id='u', character_id='gojo'):
        return [
            row for row in self.store.chat_log
            if row['user_id'] == user_id and row['chat_id'] == character_id
            and row.get('status') == 'active'
        ]

    def capture_extractor_prompt(self, prior_events, user_text, assistant_text,
                                 existing_bond=''):
        for event_id, role, content in prior_events:
            self.raw_events.append_raw_event(
                'u', 'gojo', event_id=event_id, role=role, content=content)
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='current-user', role='user', content=user_text)
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='current-assistant', role='assistant',
            content=assistant_text)

        payload = json.dumps({
            'user_fact': None,
            'bond': None,
            'told': None,
            'character_self_claim': None,
            'bond_merge': None,
            'bond_resolution': None,
        }, ensure_ascii=False)
        captured = {}

        def fake_chat(*_args, **kwargs):
            captured['prompt'] = kwargs['messages'][0]['content']
            return payload, None

        bonds = [
            (1, existing_bond, datetime.now(timezone.utc))
        ] if existing_bond else []
        with patch.object(self.user_memory, 'plan_memory_corrections', return_value=[]), \
             patch.object(self.user_memory, 'get_long_memory', return_value=[]), \
             patch.object(self.user_memory, 'get_bond_memories', return_value=bonds), \
             patch.object(self.user_memory, '_all_character_names', return_value=set()), \
             patch.object(self.user_memory, 'get_relations_text', return_value=''), \
             patch('ai_client.create_chat', side_effect=fake_chat), \
             patch('characters.get_character', return_value={'name': '五条'}), \
             patch('memory_lifecycle.reactivate_lifecycle_memories', return_value=0), \
             patch('smart_recall.reinforce_mentioned_facts'):
            ok = self.user_memory.extract_and_save_memory(
                'u', user_text, assistant_text, 'gojo',
                source_event_id='current-user',
                source_event_ids=['current-user'],
            )
        self.assertTrue(ok)
        return captured['prompt']

    @staticmethod
    def prompt_section(prompt, heading, next_heading):
        return prompt.split(heading, 1)[1].split(next_heading, 1)[0]

    def test_text_writes_one_canonical_raw_event(self):
        inserted = self.user_memory.save_user_short_memory_once(
            'u', '你好', 'gojo', source_event_id='evt-text-1')
        self.assertTrue(inserted)
        inserted_again = self.user_memory.save_user_short_memory_once(
            'u', '你好', 'gojo', source_event_id='evt-text-1')
        self.assertFalse(inserted_again)
        events = self.active_events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['text'], '你好')
        recent = self.raw_events.get_recent_events('u', 'gojo', n=40)
        self.assertEqual([item['event_id'] for item in recent], ['evt-text-1'])
        short = self.user_memory.get_short_memory('u', 40, 'gojo')
        self.assertEqual(len(short), 1)
        self.assertEqual(short[0][0], 'user')
        self.assertIn('你好', short[0][1])

    def test_voice_same_user_text_is_one_raw_event(self):
        self.user_memory.save_user_short_memory_once(
            'u', '语音一句', 'gojo', source_event_id='voice-1')
        self.user_memory.save_user_short_memory_once(
            'u', '语音一句', 'gojo', source_event_id='voice-1')
        self.assertEqual(len(self.active_events()), 1)
        prompt = self.user_memory.get_short_memory('u', 6, 'gojo')
        user_turns = [content for role, content in prompt if role == 'user']
        self.assertEqual(len(user_turns), 1)
        self.user_memory.save_short_memory(
            'u', 'assistant', 'ん？', 'gojo', source_event_id='voice-1:reply')
        self.assertEqual(len(self.active_events()), 2)

    def test_image_keeps_placeholder_and_annotation(self):
        self.user_memory.save_user_short_memory_once(
            'u', '📷 [图片]', 'gojo', source_event_id='img-1',
            event_meta={'kind': 'image', 'caption': ''},
        )
        first = self.raw_events.attach_event_annotation(
            'img-1', 'vision_summary', '图片里有一只猫')
        second = self.raw_events.attach_event_annotation(
            'img-1', 'vision_summary', '图片里有一只猫')
        self.assertTrue(first)
        self.assertFalse(second)
        event = self.active_events()[0]
        self.assertEqual(event['kind'], 'image')
        self.assertEqual(event['text'], '📷 [图片]')
        self.assertNotIn('图片里有一只猫', event['text'])
        self.assertIn('图片里有一只猫', event['extra'])
        rendered = self.user_memory.get_short_memory_for_prompt('u', 10, 'gojo')
        self.assertEqual(len(rendered), 1)
        self.assertIn('【图片摘要】图片里有一只猫', rendered[0]['content'])
        self.assertIn('📷 [图片]', rendered[0]['content'])

    def test_extractor_recent_context_resolves_reference_without_old_bond(self):
        old_bond = '她答应明天拍谷子照片给我看'
        prompt = self.capture_extractor_prompt(
            [
                ('prior-user-1', 'user', '我今天买了甜点'),
                ('prior-assistant-1', 'assistant', '买了什么？'),
                ('prior-user-2', 'user', '这个蛋糕看起来很好吃'),
                ('prior-assistant-2', 'assistant', '那给我看看。'),
            ],
            '只能明天拍照给您看了',
            '写真で見せるだけ？',
            existing_bond=old_bond,
        )
        bond_section = self.prompt_section(
            prompt, '【已记录的羁绊记忆】', '【最近对话上下文】')
        recent_section = self.prompt_section(
            prompt, '【最近对话上下文】', '【上下文使用边界】')
        self.assertIn('这个蛋糕看起来很好吃', recent_section)
        self.assertIn(old_bond, bond_section)
        self.assertNotIn(old_bond, recent_section)
        self.assertIn('禁止拿它们猜当前 turn 没有明确说出的对象', prompt)

    def test_extractor_forbids_old_bond_antecedent_without_prior_object(self):
        prompt = self.capture_extractor_prompt(
            [
                ('prior-user-1', 'user', '今天有点忙'),
                ('prior-assistant-1', 'assistant', '先忙你的。'),
            ],
            '明天拍照给你看',
            '分かった。',
            existing_bond='她以前答应拍谷子照片给我看',
        )
        recent_section = self.prompt_section(
            prompt, '【最近对话上下文】', '【上下文使用边界】')
        self.assertNotIn('谷子', recent_section)
        self.assertIn('只能泛化（例如“她答应明天拍照片给我看”）或填 null', prompt)
        self.assertIn('禁止因为旧 bond 里有“谷子”', prompt)

    def test_extractor_current_turn_appears_only_in_current_section(self):
        user_text = '明天拍照给你看'
        assistant_text = '写真で見せるだけ？'
        prompt = self.capture_extractor_prompt(
            [
                ('prior-user-1', 'user', '这个蛋糕颜色很漂亮'),
                ('prior-assistant-1', 'assistant', '見せて。'),
            ],
            user_text,
            assistant_text,
        )
        recent_section = self.prompt_section(
            prompt, '【最近对话上下文】', '【上下文使用边界】')
        current_section = self.prompt_section(
            prompt, '【这次对话】', '【四类记忆/证据的定义')
        self.assertNotIn(user_text, recent_section)
        self.assertNotIn(assistant_text, recent_section)
        self.assertEqual(current_section.count(user_text), 1)
        self.assertEqual(current_section.count(assistant_text), 1)

    def test_worker_retry_does_not_duplicate_derived_memory(self):
        event_id = 'evt-worker'
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id=event_id, role='user', content='我明天考试')

        def derive():
            key = self.raw_events.derivation_source_key([event_id])
            if self.raw_events.already_derived(
                    self.raw_events.PROCESSOR_MEMORY_EXTRACTOR,
                    self.raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION, key):
                return False
            if self.raw_events.claim_processor(
                    event_id, self.raw_events.PROCESSOR_MEMORY_EXTRACTOR,
                    self.raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION) == 'already_succeeded':
                return False
            self.user_memory.save_long_memory(
                'u', '她明天考试', '状态', 'gojo',
                source_event_refs=[{'source_id': event_id}])
            self.raw_events.record_derived(
                self.raw_events.PROCESSOR_MEMORY_EXTRACTOR,
                self.raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION, key,
                'long_memory', self.store.long_memory[-1]['id'])
            self.raw_events.finish_processor(
                event_id, self.raw_events.PROCESSOR_MEMORY_EXTRACTOR,
                self.raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION, 'succeeded')
            return True

        self.assertTrue(derive())
        self.assertFalse(derive())
        self.assertEqual(len(self.store.long_memory), 1)
        self.assertEqual(len(self.store.source_map), 1)

    def test_queue_duplicate_delivery_reuses_idempotency(self):
        event_id = 'evt-queue'
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id=event_id, role='user', content='重复投递')
        key = self.raw_events.derivation_source_key([event_id])
        self.raw_events.record_derived(
            self.raw_events.PROCESSOR_MEMORY_EXTRACTOR,
            self.raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION, key)
        self.raw_events.finish_processor(
            event_id, self.raw_events.PROCESSOR_MEMORY_EXTRACTOR,
            self.raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION, 'succeeded')
        self.assertEqual(
            self.raw_events.claim_processor(
                event_id, self.raw_events.PROCESSOR_MEMORY_EXTRACTOR,
                self.raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION),
            'already_succeeded')
        self.assertTrue(self.raw_events.already_derived(
            self.raw_events.PROCESSOR_MEMORY_EXTRACTOR,
            self.raw_events.PROCESSOR_MEMORY_EXTRACTOR_VERSION, key))

    def test_semantic_repetition_keeps_four_raw_events(self):
        texts = ['我好困', '真的好困', '我要困死了', '我要睡觉']
        for index, text in enumerate(texts, 1):
            self.user_memory.save_user_short_memory_once(
                'u', text, 'gojo', source_event_id=f'sleep-{index}')
        events = self.active_events()
        self.assertEqual(len(events), 4)
        self.assertEqual([row['text'] for row in events], texts)
        recent = self.raw_events.get_recent_events('u', 'gojo', n=40)
        self.assertEqual([item['content'] for item in recent], texts)

    def test_technical_retry_same_event_id_is_one_raw_event(self):
        first = self.raw_events.append_raw_event(
            'u', 'gojo', event_id='req-1', role='user', content='同一请求')
        second = self.raw_events.append_raw_event(
            'u', 'gojo', event_id='req-1', role='user', content='同一请求')
        self.assertTrue(first['inserted'])
        self.assertFalse(second['inserted'])
        self.assertEqual(len(self.active_events()), 1)

    def test_delete_then_reconnect_hides_event_from_history_and_prompt(self):
        self.user_memory.save_user_short_memory_once(
            'u', '删掉我', 'gojo', source_event_id='del-1')
        self.assertEqual(len(self.db_chatlog.get_messages('u', 'gojo')[0]), 1)
        deleted = self.db_chatlog.delete_message('u', 'gojo', client_msg_id='del-1')
        self.assertEqual(deleted, 1)
        history, _ = self.db_chatlog.get_messages('u', 'gojo')
        self.assertEqual(history, [])
        self.assertEqual(self.raw_events.get_recent_events('u', 'gojo'), [])
        prompt = self.user_memory.get_short_memory('u', 40, 'gojo')
        self.assertEqual(prompt, [])
        written = self.db_chatlog.append_messages('u', 'gojo', [{
            'client_msg_id': 'del-1',
            'role': 'user',
            'text': '复活?',
            'kind': 'text',
        }])
        self.assertEqual(written, 0)
        self.assertEqual(self.db_chatlog.get_messages('u', 'gojo')[0], [])

    def test_derived_memory_invalidated_when_only_source_deleted(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='src-a', role='user', content='A')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='src-b', role='user', content='B')
        self.user_memory.save_long_memory(
            'u', '只来自 A', '其他', 'gojo',
            source_event_refs=[{'source_id': 'src-a'}])
        self.user_memory.save_long_memory(
            'u', '来自 A 和 B', '其他', 'gojo',
            source_event_refs=[{'source_id': 'src-a'}, {'source_id': 'src-b'}])
        self.db_chatlog.delete_message('u', 'gojo', client_msg_id='src-a')
        self.raw_events.invalidate_memories_for_deleted_event('src-a', 'u', 'gojo')
        statuses = {row['content']: row['recall_status'] for row in self.store.long_memory}
        self.assertEqual(statuses['只来自 A'], 'deleted')
        self.assertEqual(statuses['来自 A 和 B'], 'active')
        leftover = [
            item for item in self.store.source_map if item[2] == 'src-b'
        ]
        self.assertEqual(len(leftover), 1)

    def test_migration_is_idempotent(self):
        self.user_memory.save_user_short_memory_once(
            'u', '旧缓存', 'gojo', source_event_id='old-1')
        first = self.raw_events.init_raw_event_layer()
        second = self.raw_events.init_raw_event_layer()
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(self.active_events()), 1)

    def test_n_limit_still_applies(self):
        for index in range(5):
            self.user_memory.save_user_short_memory_once(
                'u', f'msg-{index}', 'gojo', source_event_id=f'n-{index}')
        rows = self.user_memory.get_short_memory('u', 3, 'gojo')
        self.assertEqual(len(rows), 3)

    def test_proactive_assistant_is_one_canonical_raw_event(self):
        self.user_memory.commit_visible_assistant_message(
            'u', '約束だ', 'gojo',
            event_id='proactive:promise:9',
            kind='proactive',
            subtitle='说好了',
            emotion='平静',
        )
        self.user_memory.commit_visible_assistant_message(
            'u', '約束だ', 'gojo',
            event_id='proactive:promise:9',
            kind='proactive',
        )
        events = [row for row in self.active_events() if row['role'] == 'gojo']
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['client_msg_id'], 'proactive:promise:9')
        recent = self.raw_events.get_recent_events('u', 'gojo', n=40)
        self.assertEqual(
            [item['event_id'] for item in recent if item['role'] == 'assistant'],
            ['proactive:promise:9'],
        )
        short = self.user_memory.get_short_memory('u', 40, 'gojo')
        assistant = [content for role, content in short if role == 'assistant']
        self.assertEqual(len(assistant), 1)
        self.assertIn('約束だ', assistant[0])

    def test_streaming_segments_share_assistant_turn(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='user-1:reply:0', role='assistant', content='一段')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='user-1:reply:1', role='assistant', content='二段')
        segs = self.raw_events.list_events_for_assistant_turn(
            'u', 'gojo', 'user-1:reply')
        self.assertEqual([item['segment_index'] for item in segs], [0, 1])
        self.assertEqual({item['assistant_turn_id'] for item in segs}, {'user-1:reply'})
        self.assertEqual(len(segs), 2)

    def test_deleted_source_commit_race_does_not_write_derived(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='race-1', role='user', content='我叫小明')
        deleted_after_llm = {'value': False}

        def gated_deleted(event_id, user_id=None, character_id=None, conn=None):
            return bool(deleted_after_llm['value'])

        fact = (
            '{"user_fact":{"content":"她叫小明","category":"身份"},'
            '"bond":null,"told":null,"character_self_claim":null,"bond_merge":null}'
        )

        def fake_chat(*args, **kwargs):
            deleted_after_llm['value'] = True
            return fact, None

        with patch.object(self.raw_events, 'is_raw_event_deleted', gated_deleted), \
             patch.object(self.user_memory, 'plan_memory_corrections', return_value=[]), \
             patch.object(self.user_memory, 'get_long_memory', return_value=[]), \
             patch.object(self.user_memory, 'get_bond_memories', return_value=[]), \
             patch.object(self.user_memory, '_all_character_names', return_value=set()), \
             patch.object(self.user_memory, 'get_relations_text', return_value=''), \
             patch('ai_client.create_chat', side_effect=fake_chat), \
             patch('characters.get_character', return_value={'name': '五条'}), \
             patch('memory_lifecycle.reactivate_lifecycle_memories', return_value=0), \
             patch('memory_lifecycle.apply_user_fact_lifecycle') as lifecycle:
            ok = self.user_memory.extract_and_save_memory(
                'u', '我叫小明', 'そうか', 'gojo', source_event_id='race-1')
        self.assertTrue(ok)
        self.assertTrue(deleted_after_llm['value'])
        lifecycle.assert_not_called()
        self.assertEqual(self.store.long_memory, [])
        statuses = [row['status'] for row in self.store.processing.values()]
        self.assertIn('skipped', statuses)
        self.assertNotIn('succeeded', statuses)

    def test_assistant_source_event_id_retry_is_one_cache_row(self):
        self.user_memory.save_short_memory(
            'u', 'assistant', 'ん？', 'gojo', source_event_id='asst-1')
        self.user_memory.save_short_memory(
            'u', 'assistant', 'ん？', 'gojo', source_event_id='asst-1')
        keyed = [
            row for row in self.store.short_memory
            if row['role'] == 'assistant' and row.get('source_event_id') == 'asst-1'
        ]
        self.assertEqual(len(keyed), 1)
        self.user_memory.save_short_memory('u', 'assistant', '旧缓存', 'gojo')
        self.user_memory.save_short_memory('u', 'assistant', '旧缓存', 'gojo')
        legacy = [
            row for row in self.store.short_memory
            if row['role'] == 'assistant' and not row.get('source_event_id')
        ]
        self.assertGreaterEqual(len(legacy), 2)

    def test_pending_proactive_double_submit_is_one_raw_event(self):
        import proactive_msg
        event_id = proactive_msg.canonical_event_id('report', 42)
        self.user_memory.commit_visible_assistant_message(
            'u', '任務終わった', 'gojo', event_id=event_id, kind='proactive')
        payload = {
            'client_msg_id': event_id,
            'role': 'gojo',
            'text': '任務終わった',
            'kind': 'text',
            'extra': json.dumps({
                'assistant_turn_id': event_id,
                'segment_index': 0,
            }, ensure_ascii=False),
        }
        self.db_chatlog.append_messages('u', 'gojo', [payload])
        self.db_chatlog.append_messages('u', 'gojo', [payload])
        events = [
            row for row in self.active_events()
            if row.get('client_msg_id') == event_id
        ]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['role'], 'gojo')

    def test_source_validity_error_does_not_write_derived_memory(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='src-err', role='user', content='我叫小明')

        def boom(*_args, **_kwargs):
            raise self.raw_events.SourceValidityError('db down')

        with patch.object(self.raw_events, 'is_raw_event_deleted', side_effect=boom), \
             patch('ai_client.create_chat') as chat, \
             patch('memory_lifecycle.reactivate_lifecycle_memories') as reactivate, \
             patch('memory_lifecycle.apply_user_fact_lifecycle') as lifecycle:
            ok = self.user_memory.extract_and_save_memory(
                'u', '我叫小明', 'そうか', 'gojo', source_event_id='src-err')
        self.assertFalse(ok)
        chat.assert_not_called()
        reactivate.assert_not_called()
        lifecycle.assert_not_called()
        self.assertEqual(self.store.long_memory, [])
        statuses = [row['status'] for row in self.store.processing.values()]
        self.assertIn('failed', statuses)
        self.assertNotIn('succeeded', statuses)

    def test_lifecycle_reactivation_skipped_when_source_deleted_before_commit(self):
        self.store.lifecycle.append({
            'id': 1,
            'user_id': 'u',
            'character_id': 'gojo',
            'topic_key': 'sleep',
            'status': 'archived',
            'decay_state': 'dormant',
        })
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='life-1', role='user',
            content='我又想起之前失眠那阵子')
        deleted_after_llm = {'value': False}

        def gated_deleted(event_id, user_id=None, character_id=None, conn=None):
            return bool(deleted_after_llm['value'])

        fact = (
            '{"user_fact":{"content":"她最近又失眠","category":"状态"},'
            '"bond":null,"told":null,"character_self_claim":null,"bond_merge":null}'
        )

        def fake_chat(*args, **kwargs):
            deleted_after_llm['value'] = True
            return fact, None

        with patch.object(self.raw_events, 'is_raw_event_deleted', gated_deleted), \
             patch.object(self.user_memory, 'plan_memory_corrections', return_value=[]), \
             patch.object(self.user_memory, 'get_long_memory', return_value=[]), \
             patch.object(self.user_memory, 'get_bond_memories', return_value=[]), \
             patch.object(self.user_memory, '_all_character_names', return_value=set()), \
             patch.object(self.user_memory, 'get_relations_text', return_value=''), \
             patch('ai_client.create_chat', side_effect=fake_chat), \
             patch('characters.get_character', return_value={'name': '五条'}), \
             patch('memory_lifecycle.get_conn', side_effect=lambda: FakeConn(self.store)), \
             patch('memory_lifecycle.apply_user_fact_lifecycle') as lifecycle:
            ok = self.user_memory.extract_and_save_memory(
                'u', '我又想起之前失眠那阵子', 'そうか', 'gojo',
                source_event_id='life-1')
        self.assertTrue(ok)
        lifecycle.assert_not_called()
        self.assertEqual(self.store.long_memory, [])
        self.assertEqual(self.store.lifecycle[0]['status'], 'archived')
        self.assertEqual(self.store.lifecycle[0]['decay_state'], 'dormant')
        statuses = [row['status'] for row in self.store.processing.values()]
        self.assertIn('skipped', statuses)
        self.assertNotIn('succeeded', statuses)

    def test_voice_proactive_request_ids_are_not_minute_buckets(self):
        first = 'voice_proactive:req:aaa'
        second = 'voice_proactive:req:bbb'
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id=first, role='assistant', content='一')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id=second, role='assistant', content='二')
        self.assertEqual(len([
            row for row in self.active_events() if row['role'] == 'gojo'
        ]), 2)
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id=first, role='assistant', content='一-retry')
        self.assertEqual(len([
            row for row in self.active_events() if row['client_msg_id'] == first
        ]), 1)

    def test_chat_proactive_same_operation_retry_is_one_raw_event(self):
        event_id = 'proactive:chat:task:1:2026-09-17:remind'
        first = self.raw_events.append_raw_event(
            'u', 'gojo', event_id=event_id, role='assistant',
            content='该喝水了')
        retry = self.raw_events.append_raw_event(
            'u', 'gojo', event_id=event_id, role='assistant',
            content='该喝水了')
        self.assertTrue(first['inserted'])
        self.assertFalse(retry['inserted'])
        self.assertEqual(len([
            row for row in self.active_events() if row['client_msg_id'] == event_id
        ]), 1)

    def test_chat_proactive_identical_content_distinct_triggers_are_two_events(self):
        text = '该喝水了'
        first = 'proactive:chat:task:1:2026-09-17:remind'
        second = 'proactive:chat:task:2:2026-09-17:remind'
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id=first, role='assistant', content=text)
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id=second, role='assistant', content=text)
        rows = [
            row for row in self.active_events()
            if row['text'] == text and row['role'] == 'gojo'
        ]
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {row['client_msg_id'] for row in rows}, {first, second})

    def test_deleted_event_lookup_db_error_does_not_feed_prompt(self):
        secret = '不该在lookup失败时进prompt'
        self.user_memory.save_user_short_memory_once(
            'u', secret, 'gojo', source_event_id='hidden-lookup-1')
        self.assertTrue(any(secret in (row[1] or '')
                            for row in self.user_memory.get_short_memory('u', 40, 'gojo')))

        def boom(*_args, **_kwargs):
            raise self.raw_events.SourceValidityError('deleted-event db down')

        with patch.object(self.raw_events, 'deleted_event_ids', side_effect=boom):
            prompt = self.user_memory.get_short_memory_for_prompt('u', 40, 'gojo')
            short = self.user_memory.get_short_memory('u', 40, 'gojo')
        blob = json.dumps(prompt, ensure_ascii=False)
        self.assertNotIn(secret, blob)
        self.assertEqual(prompt, [])
        self.assertEqual(short, [])

    def test_prompt_messages_do_not_fallback_when_deleted_lookup_fails(self):
        from pathlib import Path
        chat_src = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        img_src = Path(BACKEND, 'route_image.py').read_text(encoding='utf-8')
        self.assertIn('SourceValidityError', chat_src)
        self.assertIn('SourceValidityError', img_src)
        self.assertNotIn('short_memory prompt fallback', chat_src)
        self.assertNotIn('image short_memory prompt fallback', img_src)
        self.assertIn('source validity unknown', chat_src)
        self.assertIn('source validity unknown', img_src)

    def test_claim_processor_db_error_is_not_claimed(self):
        def boom():
            raise RuntimeError('processing-state db down')

        with patch.object(self.raw_events, 'get_conn', side_effect=boom):
            state = self.raw_events.claim_processor(
                'evt-claim', self.raw_events.PROCESSOR_RELATIONSHIP,
                self.raw_events.PROCESSOR_RELATIONSHIP_VERSION)
        self.assertEqual(state, 'claim_failed')
        self.assertNotEqual(state, 'claimed')

    def test_deleted_event_ids_raises_on_db_error(self):
        def boom():
            raise RuntimeError('tombstone db down')

        with patch.object(self.raw_events, 'get_conn', side_effect=boom):
            with self.assertRaises(self.raw_events.SourceValidityError):
                self.raw_events.deleted_event_ids('u', 'gojo')

    def test_cognitive_ingest_fails_closed_on_source_validity_error(self):
        import cognitive_events

        class GuardConn:
            def cursor(self):
                raise AssertionError('must not write cognitive derived state')

            def commit(self):
                raise AssertionError('must not commit')

            def rollback(self):
                self.rolled = True

            def close(self):
                pass

        def boom(*_args, **_kwargs):
            raise self.raw_events.SourceValidityError('db down')

        with patch.object(self.raw_events, 'is_raw_event_deleted', side_effect=boom):
            result = cognitive_events.ingest_v4_signals(
                user_id='u', character_id='gojo',
                source_event_id='evt-cog',
                signals=[{'brief': 'x'}],
                conn=GuardConn(),
                aggregate=False,
            )
        self.assertEqual(result['status'], 'failed_source_validity')
        self.assertIsNone(result['event_id'])


if __name__ == '__main__':
    unittest.main()
