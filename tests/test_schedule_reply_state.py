import importlib.util
import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


def stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


fake_db = types.ModuleType('db')
fake_db.get_conn = lambda: (_ for _ in ()).throw(
    AssertionError('database should be patched by tests that need it')
)
sys.modules.setdefault('db', fake_db)

import db_schedule  # noqa: E402
import reply_availability  # noqa: E402


def load_schedule_engine():
    modules = {
        'characters': stub('characters', get_character=Mock()),
        'characters_data': stub('characters_data'),
        'characters_data._loader': stub(
            'characters_data._loader', load_core=Mock(return_value={})),
        'character_rhythm': stub(
            'character_rhythm',
            get_rhythm_text=Mock(return_value=''),
            get_sleep_window=Mock(return_value=None),
        ),
    }
    with patch.dict(sys.modules, modules):
        spec = importlib.util.spec_from_file_location(
            'schedule_engine_under_test',
            os.path.join(BACKEND, 'schedule_engine.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


class PhoneCheckStore:
    def __init__(self):
        self.next_id = 1
        self.rows = {}
        self.schedule_rows = []
        self.sql = []


class FakeCursor:
    def __init__(self, store):
        self.store = store
        self._one = None
        self._many = []
        self.rowcount = 0

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.store.sql.append((compact, tuple(params or ())))
        self._one = None
        self._many = []
        self.rowcount = 0
        params = tuple(params or ())

        if compact.startswith('SELECT id, seen, can_reply'):
            user_id, character_id, sched_date, start_time, end_time = params
            row = self.store.rows.get(
                (user_id, character_id, sched_date, start_time, end_time))
            if row:
                self._one = (
                    row['id'], row['seen'], row['can_reply'],
                    row['pending_count'], row['pending_text'],
                    row['event_meta'], row.get('fallback_promise_id'),
                    row.get('next_phone_check_at'), row.get('seen_at'),
                    row.get('resolved_at'), row.get('seen_watermark', 0),
                )
            return

        if compact.startswith('INSERT INTO char_phone_check'):
            (user_id, character_id, schedule_id, sched_date, start_time,
             end_time, activity_title, reply_state, seen, can_reply,
             pending_count, first_source_event_id, last_source_event_id,
             pending_text, event_meta, next_phone_check_at,
             seen_watermark) = params
            row = {
                'id': self.store.next_id,
                'user_id': user_id,
                'character_id': character_id,
                'schedule_id': schedule_id,
                'sched_date': sched_date,
                'start_time': start_time,
                'end_time': end_time,
                'activity_title': activity_title,
                'reply_state': reply_state,
                'seen': seen,
                'can_reply': can_reply,
                'pending_count': pending_count,
                'first_source_event_id': first_source_event_id,
                'last_source_event_id': last_source_event_id,
                'pending_text': pending_text,
                'event_meta': event_meta,
                'fallback_promise_id': None,
                'next_phone_check_at': next_phone_check_at,
                'seen_at': None,
                'resolved_at': None,
                'seen_watermark': seen_watermark or 0,
            }
            self.store.next_id += 1
            self.store.rows[
                (user_id, character_id, sched_date, start_time, end_time)
            ] = row
            self._one = (row['id'],)
            self.rowcount = 1
            return

        if compact.startswith('UPDATE char_phone_check SET pending_count='):
            pending_count, last_source_event_id, pending_text, event_meta, oid = params
            for row in self.store.rows.values():
                if row['id'] == oid:
                    row['pending_count'] = pending_count
                    row['last_source_event_id'] = last_source_event_id
                    row['pending_text'] = pending_text
                    row['event_meta'] = event_meta
                    self.rowcount = 1
                    return
            return

        if compact.startswith('UPDATE char_phone_check SET seen=FALSE'):
            (first_source_event_id, last_source_event_id, pending_text,
             event_meta, next_phone_check_at, oid) = params
            for row in self.store.rows.values():
                if row['id'] == oid:
                    row.update({
                        'seen': False,
                        'can_reply': False,
                        'seen_at': None,
                        'seen_watermark': 0,
                        'resolved_at': None,
                        'pending_count': 1,
                        'first_source_event_id': first_source_event_id,
                        'last_source_event_id': last_source_event_id,
                        'pending_text': pending_text,
                        'event_meta': event_meta,
                        'fallback_promise_id': None,
                        'next_phone_check_at': next_phone_check_at,
                    })
                    self.rowcount = 1
                    return
            return

        if compact.startswith('UPDATE char_phone_check SET next_phone_check_at='):
            next_phone_check_at, oid = params
            for row in self.store.rows.values():
                if row['id'] == oid:
                    row['next_phone_check_at'] = next_phone_check_at
                    self.rowcount = 1
                    return
            return

        if compact.startswith('UPDATE char_phone_check SET seen=TRUE'):
            if 'resolved_at=%s' in compact:
                seen_at, resolved_at, seen_watermark, oid = params
                for row in self.store.rows.values():
                    if row['id'] == oid:
                        row['seen'] = True
                        row['seen_at'] = seen_at
                        row['can_reply'] = True
                        row['next_phone_check_at'] = None
                        row['resolved_at'] = resolved_at
                        row['seen_watermark'] = seen_watermark
                        self.rowcount = 1
                        return
            else:
                seen_at, next_phone_check_at, seen_watermark, oid = params
                for row in self.store.rows.values():
                    if row['id'] == oid:
                        row['seen'] = True
                        row['seen_at'] = seen_at
                        row['can_reply'] = False
                        row['next_phone_check_at'] = next_phone_check_at
                        row['seen_watermark'] = seen_watermark
                        self.rowcount = 1
                        return
            return

        if compact.startswith('UPDATE char_phone_check SET fallback_promise_id='):
            return

        if compact.startswith('SELECT id, start_time, end_time, title'):
            character_id, user_id, sched_date, hhmm, hhmm2 = params
            matched = []
            for r in self.store.schedule_rows:
                if (r['character_id'] == character_id and r['user_id'] == user_id
                        and r['sched_date'] == sched_date
                        and r['start_time'] <= hhmm < r['end_time']):
                    matched.append((
                        r['id'], r['start_time'], r['end_time'], r['title'],
                        r.get('location', ''), r.get('note', ''),
                        r.get('can_reply', False), r.get('reply_state', ''),
                    ))
            self._many = matched
            self._one = matched[0] if matched else None
            return

        raise AssertionError(f'unhandled SQL: {compact}')

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._many)

    def close(self):
        pass


class FakeConn:
    def __init__(self, store):
        self.store = store
        self.commits = 0

    def cursor(self):
        return FakeCursor(self.store)

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class ScheduleReplyStateTests(unittest.TestCase):
    def test_sanitize_preserves_real_soft_90_and_hard_10_minutes(self):
        schedule_engine = load_schedule_engine()
        items = schedule_engine._sanitize([
            {
                'start_time': '09:00',
                'end_time': '10:30',
                'title': '备课',
                'location': '办公室',
                'note': '堆着一摞材料',
                'reply_state': 'soft_busy',
            },
            {
                'start_time': '10:30',
                'end_time': '10:40',
                'title': '洗澡',
                'location': '家',
                'note': '十分钟冲掉一身汗',
                'reply_state': 'hard_busy',
            },
        ], character_id='gojo')

        self.assertEqual([i['reply_state'] for i in items],
                         ['soft_busy', 'hard_busy'])
        self.assertEqual([i['can_reply'] for i in items], [False, False])
        self.assertEqual(schedule_engine._dur(items[0]), 90)
        self.assertEqual(schedule_engine._dur(items[1]), 10)

    def test_ten_messages_do_not_change_existing_next_phone_check_at(self):
        store = PhoneCheckStore()
        now = datetime(2026, 9, 16, 9, 44, tzinfo=timezone.utc)
        activity = {
            'id': 7,
            'start_time': '09:00',
            'end_time': '10:30',
            'title': '备课',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        fixed_check = now + timedelta(minutes=13)  # 09:57
        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=fixed_check), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (fixed_check, False)):
            first = db_schedule.decide_phone_check(
                'gojo', 'u1', now, activity,
                source_event_id='evt-1', pending_text='A')
            locked = first['next_phone_check_at']
            for i in range(2, 11):
                later = now + timedelta(minutes=i)
                decision = db_schedule.decide_phone_check(
                    'gojo', 'u1', later, activity,
                    source_event_id=f'evt-{i}', pending_text=chr(64 + i))
                self.assertEqual(decision['next_phone_check_at'], locked)
                self.assertFalse(decision['seen'])
                self.assertFalse(decision['can_reply'])
                self.assertTrue(decision['reused'])

        row = next(iter(store.rows.values()))
        self.assertEqual(row['pending_count'], 10)
        self.assertEqual(row['next_phone_check_at'], locked)

    def test_defer_generates_second_phone_check(self):
        store = PhoneCheckStore()
        now = datetime(2026, 9, 16, 9, 44, tzinfo=timezone.utc)
        first_check = datetime(2026, 9, 16, 9, 57, tzinfo=timezone.utc)
        second_check = datetime(2026, 9, 16, 10, 19, tzinfo=timezone.utc)
        activity = {
            'id': 7,
            'start_time': '09:00',
            'end_time': '10:30',
            'title': '备课',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        samples = [first_check, second_check]

        def _sample(now_arg, activity_arg, after=None):
            return samples.pop(0)

        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          side_effect=_sample), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2], False)), \
             patch.object(db_schedule.random, 'random', return_value=0.99):
            created = db_schedule.decide_phone_check(
                'gojo', 'u1', now, activity,
                source_event_id='evt-1', pending_text='你好')
            self.assertFalse(created['seen'])
            self.assertEqual(created['next_phone_check_at'], first_check)

            deferred = db_schedule.decide_phone_check(
                'gojo', 'u1', first_check, activity,
                source_event_id='evt-2', pending_text='还在吗')
            self.assertTrue(deferred['seen'])
            self.assertFalse(deferred['can_reply'])
            self.assertTrue(deferred.get('check_consumed'))
            self.assertEqual(deferred['seen_at'], first_check)
            self.assertEqual(deferred['next_phone_check_at'], second_check)

        row = next(iter(store.rows.values()))
        self.assertEqual(row['next_phone_check_at'], second_check)
        self.assertTrue(row['seen'])
        self.assertFalse(row['can_reply'])
        self.assertEqual(row['seen_watermark'], 2)

    def test_new_message_after_defer_does_not_inherit_bundle_seen(self):
        """A/B/C 第一次 check 已 seen+defer；D 必须等下一次 check 才 seen。"""
        store = PhoneCheckStore()
        t0 = datetime(2026, 9, 16, 9, 44, tzinfo=timezone.utc)
        first_check = datetime(2026, 9, 16, 9, 57, tzinfo=timezone.utc)
        second_check = datetime(2026, 9, 16, 10, 19, tzinfo=timezone.utc)
        activity = {
            'id': 7,
            'start_time': '09:00',
            'end_time': '10:30',
            'title': '备课',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        samples = [first_check, second_check]

        def _sample(now_arg, activity_arg, after=None):
            return samples.pop(0)

        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          side_effect=_sample), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=lambda *a, **k: (a[2], False)), \
             patch.object(db_schedule.random, 'random', return_value=0.99):
            db_schedule.decide_phone_check(
                'gojo', 'u1', t0, activity, source_event_id='A', pending_text='A')
            db_schedule.decide_phone_check(
                'gojo', 'u1', t0 + timedelta(minutes=4), activity,
                source_event_id='B', pending_text='B')
            seen_bundle = db_schedule.decide_phone_check(
                'gojo', 'u1', first_check, activity,
                source_event_id='C', pending_text='C')
            late = db_schedule.decide_phone_check(
                'gojo', 'u1', first_check + timedelta(minutes=1), activity,
                source_event_id='D', pending_text='D')

        self.assertTrue(seen_bundle['seen'])
        self.assertFalse(seen_bundle['can_reply'])
        self.assertEqual(seen_bundle['seen_watermark'], 3)
        self.assertFalse(late['seen'])
        self.assertFalse(late['can_reply'])
        self.assertIsNone(late['seen_at'])
        self.assertEqual(late['seen_watermark'], 3)
        self.assertEqual(late['pending_count'], 4)
        self.assertEqual(late['next_phone_check_at'], second_check)

    def test_hard_busy_covering_check_postpones(self):
        store = PhoneCheckStore()
        now = datetime(2026, 9, 16, 9, 50, tzinfo=timezone.utc)
        original = datetime(2026, 9, 16, 9, 55, tzinfo=timezone.utc)
        postponed = datetime(2026, 9, 16, 10, 11, tzinfo=timezone.utc)
        activity = {
            'id': 7,
            'start_time': '09:00',
            'end_time': '10:30',
            'title': '备课',
            'reply_state': 'soft_busy',
            'can_reply': False,
        }
        with patch.object(db_schedule, 'get_conn',
                          side_effect=lambda: FakeConn(store)), \
             patch.object(db_schedule, 'sample_next_phone_check_at',
                          return_value=original), \
             patch.object(db_schedule, 'postpone_past_hard_busy',
                          side_effect=[
                              (original, False),   # on create
                              (postponed, True),   # on consume at due time
                          ]):
            db_schedule.decide_phone_check(
                'gojo', 'u1', now, activity,
                source_event_id='evt-1', pending_text='hi')
            decision = db_schedule.decide_phone_check(
                'gojo', 'u1', original, activity,
                source_event_id='evt-2', pending_text='again')

        self.assertFalse(decision['seen'])
        self.assertFalse(decision['can_reply'])
        self.assertTrue(decision.get('postponed_for_hard_busy'))
        self.assertEqual(decision['next_phone_check_at'], postponed)

    def test_phone_check_fallback_context_includes_visual_summary(self):
        decision = {
            'seen': True,
            'can_reply': False,
            'reply_state': 'soft_busy',
            'opportunity_id': 3,
            'source_event_id': 'img-1',
        }
        activity = {'title': '备课'}
        event_meta = {
            'kind': 'image',
            'visual_summary': '蓝色马克杯，杯沿有裂纹',
            'source_event_id': 'img-1',
        }
        ctx = reply_availability._fallback_context(
            activity, decision, '📷 看这个', event_meta=event_meta)
        self.assertIn('蓝色马克杯', ctx)
        self.assertIn('image', ctx)
        self.assertIn('看到了', ctx)


class BusyImageVisionTests(unittest.TestCase):
    def test_busy_image_runs_vision_before_availability_and_keeps_unseen(self):
        import asyncio
        import importlib.util

        modules = {
            'config': stub(
                'config', ANTHROPIC_KEY='x', EMOTIONS={'平静'}, TTS_PROVIDER='fish',
                DEFAULT_CHARACTER_ID='gojo', MODEL_MAIN='m'),
            'db': stub('db', get_conn=Mock()),
            'utils': stub(
                'utils', ingest_model_output=Mock(), finalize_user_messages=Mock(),
                valid_reply_msg=Mock(), commit_ready_msgs=Mock(),
                extract_json=lambda raw: {'visual_summary': '蓝色马克杯，杯沿有裂纹'}),
            'ai_client': stub(
                'ai_client', extract_text=lambda r: r,
                response_metadata=Mock()),
            'tts': stub('tts', tts_to_b64=Mock(return_value='')),
            'prompt': stub(
                'prompt', build_system_blocks=Mock(return_value=[]),
                log_cache_usage=Mock()),
            'user_memory': stub(
                'user_memory',
                save_short_memory=Mock(),
                save_user_short_memory_once=Mock(return_value=True),
                get_short_memory=Mock(return_value=[]),
                get_short_memory_for_prompt=Mock(return_value=[]),
                attach_short_memory_event_meta=Mock(return_value=True),
                update_chat_days=Mock(return_value=1),
            ),
            'memory_jobs': stub('memory_jobs', enqueue_private_extraction=Mock()),
            'temporal_awareness': stub(
                'temporal_awareness',
                get_temporal_snapshot=Mock(return_value={}),
                record_turn=Mock(),
                record_user_message=Mock(),
            ),
            'characters': stub(
                'characters',
                get_character=Mock(return_value={'id': 'gojo', 'voice_id': 'v'}),
            ),
            'tasks': stub(
                'tasks',
                find_duplicate_task=Mock(),
                find_and_delete_tasks_by_keyword=Mock(),
                delete_latest_task=Mock(),
            ),
            'task_dedup': stub('task_dedup', find_similar_task=Mock()),
            'fastapi': stub('fastapi', APIRouter=lambda: types.SimpleNamespace(
                post=lambda *a, **k: (lambda f: f),
            )),
            'fastapi.responses': stub(
                'fastapi.responses',
                JSONResponse=lambda body, status_code=200: types.SimpleNamespace(
                    body=json.dumps(body, default=str).encode(),
                    status_code=status_code,
                    data=body),
            ),
            'anthropic': stub(
                'anthropic',
                Anthropic=Mock(return_value=Mock(messages=Mock(create=Mock(
                    return_value='{"visual_summary":"蓝色马克杯，杯沿有裂纹"}'
                )))),
            ),
        }

        availability = {
            'can_reply': False,
            'seen': False,
            'reply_state': 'soft_busy',
            'activity': {'title': '备课', 'location': '办公室', 'end_time': '10:30'},
            'free_at': '10:30',
            'opportunity_id': 9,
            'seen_at': None,
            'next_phone_check_at': datetime(2026, 9, 16, 9, 57, tzinfo=timezone.utc),
        }
        captured = {}

        def _check(character_id, user_id, **kwargs):
            captured['event_meta'] = kwargs.get('event_meta')
            captured['pending_text'] = kwargs.get('pending_text')
            return availability

        modules['reply_availability'] = stub(
            'reply_availability', check_reply_availability=_check)

        with patch.dict(sys.modules, modules):
            spec = importlib.util.spec_from_file_location(
                'route_image_under_test',
                os.path.join(BACKEND, 'route_image.py'))
            route_image = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(route_image)

            tiny_png = (
                'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8'
                'z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
            )
            response = asyncio.run(route_image.chat_image({
                'user_id': 'u1',
                'character_id': 'gojo',
                'image_base64': tiny_png,
                'text': '看这个杯子',
                'source_event_id': 'img-busy-1',
            }))

        body = json.loads(response.body)
        self.assertTrue(body['busy'])
        self.assertFalse(body['seen'])
        self.assertIsNone(body.get('seen_at'))
        self.assertIn('蓝色马克杯', body['visual_summary'])
        self.assertNotIn('visual_summary', captured.get('event_meta') or {})
        self.assertNotIn('【图片摘要】', captured.get('pending_text') or '')
        self.assertIn('看这个杯子', captured.get('pending_text') or '')
        self.assertIn('蓝色马克杯', body['event_meta']['visual_summary'])


class ShortMemoryPromptIsolationTests(unittest.TestCase):
    def test_visual_summary_is_assembled_at_prompt_time_not_in_content(self):
        from user_memory import assemble_prompt_content, format_media_short_memory

        raw = '📷 看这个'
        assembled = assemble_prompt_content(raw, {
            'kind': 'image',
            'visual_summary': '蓝色马克杯，杯沿有裂纹',
        })
        self.assertEqual(raw, '📷 看这个')
        self.assertNotIn('【图片摘要】', raw)
        self.assertIn('📷 看这个', assembled)
        self.assertIn('【图片摘要】蓝色马克杯', assembled)

        media = format_media_short_memory(
            '📷 看这个', '蓝色马克杯，杯沿有裂纹', {'kind': 'image'})
        self.assertIn('【图片摘要】蓝色马克杯', media)

    def test_delete_chatlog_does_not_clear_short_memory_source(self):
        src = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        self.assertIn('get_short_memory_for_prompt', src)
        img_src = Path(BACKEND, 'route_image.py').read_text(encoding='utf-8')
        self.assertIn('user_id, display_text, character_id', img_src)
        self.assertNotIn('format_media_short_memory(', img_src)

    def test_route_chat_prompt_prefers_short_memory_not_chatlog(self):
        from pathlib import Path
        src = Path(BACKEND, 'route_chat.py').read_text(encoding='utf-8')
        self.assertIn('get_short_memory_for_prompt', src)
        self.assertNotIn(
            'Prefer durable chat_log because it contains image summaries', src)
        img_src = Path(BACKEND, 'route_image.py').read_text(encoding='utf-8')
        self.assertIn('get_short_memory_for_prompt', img_src)
        self.assertIn('_analyze_visual_summary', img_src)
        self.assertIn('busy/pending：单独做 visual summary', img_src)
        self.assertIn('free / immediate reply：一次 Vision', img_src)


if __name__ == '__main__':
    unittest.main()
