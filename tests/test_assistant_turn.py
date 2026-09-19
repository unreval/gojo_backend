import asyncio
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

from tests.test_chatlog_delete import ChatlogStore, FakeConn, _msg  # noqa: E402
from tests.test_raw_event_layer import FakeConn as RawFakeConn, LayerStore  # noqa: E402

import assistant_turn  # noqa: E402
import db_chatlog  # noqa: E402
import generation_effects  # noqa: E402
import raw_events  # noqa: E402
import route_chatlog_search  # noqa: E402
import user_memory  # noqa: E402


def _asst(event_id, text, subtitle='', extra=None):
    payload = _msg(event_id, text, role='gojo')
    payload['subtitle'] = subtitle
    payload['event_id'] = event_id
    if extra is not None:
        payload['extra'] = (
            extra if isinstance(extra, str)
            else json.dumps(extra, ensure_ascii=False)
        )
    return payload


class AssistantIdentityTests(unittest.TestCase):
    def test_current_chat_reply_aggregate_and_segments(self):
        agg = assistant_turn.infer_assistant_identity(
            'gojo', 'chat_reply:abc', {})
        self.assertEqual(agg['assistant_turn_id'], 'chat_reply:abc')
        self.assertTrue(agg['turn_aggregate'])
        self.assertTrue(assistant_turn.is_assistant_aggregate(agg, 'chat_reply:abc'))

        seg0 = assistant_turn.infer_assistant_identity(
            'gojo', 'chat_reply:abc:0', {})
        self.assertEqual(seg0['assistant_turn_id'], 'chat_reply:abc')
        self.assertEqual(seg0['segment_index'], 0)
        self.assertFalse(seg0['turn_aggregate'])
        self.assertTrue(assistant_turn.is_assistant_segment(seg0, 'chat_reply:abc:0'))

        seg1 = assistant_turn.infer_assistant_identity(
            'gojo', 'chat_reply:abc:1', {})
        self.assertEqual(seg1['assistant_turn_id'], 'chat_reply:abc')
        self.assertEqual(seg1['segment_index'], 1)

    def test_current_image_reply_same_schema(self):
        agg = assistant_turn.infer_assistant_identity(
            'assistant', 'image_reply:img', {})
        self.assertEqual(agg['assistant_turn_id'], 'image_reply:img')
        self.assertTrue(agg['turn_aggregate'])
        seg = assistant_turn.infer_assistant_identity(
            'assistant', 'image_reply:img:2', {})
        self.assertEqual(seg['assistant_turn_id'], 'image_reply:img')
        self.assertEqual(seg['segment_index'], 2)
        self.assertFalse(seg['turn_aggregate'])

    def test_legacy_reply_schema_still_works(self):
        voice = assistant_turn.infer_assistant_identity(
            'gojo', 'user-1:reply', {})
        self.assertEqual(voice['assistant_turn_id'], 'user-1:reply')
        self.assertTrue(voice['turn_aggregate'])
        seg = assistant_turn.infer_assistant_identity(
            'gojo', 'user-1:reply:1', {})
        self.assertEqual(seg['assistant_turn_id'], 'user-1:reply')
        self.assertEqual(seg['segment_index'], 1)
        self.assertFalse(seg['turn_aggregate'])

    def test_delayed_and_proactive_ids_are_not_chat_segments(self):
        delayed = assistant_turn.parse_assistant_event_id('delayed_reply:8:0')
        proactive = assistant_turn.parse_assistant_event_id('proactive:report:1')
        self.assertIsNone(delayed)
        self.assertIsNone(proactive)
        inferred = assistant_turn.infer_assistant_identity(
            'gojo', 'delayed_reply:8:0', {})
        self.assertEqual(inferred['assistant_turn_id'], 'delayed_reply:8:0')
        self.assertFalse(assistant_turn.is_assistant_segment(
            inferred, 'delayed_reply:8:0'))
        self.assertFalse(assistant_turn.is_assistant_aggregate(
            inferred, 'delayed_reply:8:0'))

    def test_explicit_metadata_still_wins(self):
        identity = assistant_turn.infer_assistant_identity(
            'gojo', 'delayed_reply:8:1', {
                'assistant_turn_id': 'delayed_reply:8:0',
                'segment_index': 1,
            })
        self.assertEqual(identity['assistant_turn_id'], 'delayed_reply:8:0')
        self.assertEqual(identity['segment_index'], 1)
        self.assertTrue(assistant_turn.is_assistant_segment(
            identity, 'delayed_reply:8:1'))
        self.assertFalse(assistant_turn.is_assistant_aggregate(
            identity, 'delayed_reply:8:1'))

    def test_explicit_turn_aggregate_flag(self):
        identity = assistant_turn.infer_assistant_identity(
            'gojo', 'chat_reply:src', {
                'assistant_turn_id': 'chat_reply:src',
                'turn_aggregate': True,
            })
        self.assertTrue(identity['turn_aggregate'])
        self.assertTrue(assistant_turn.is_assistant_aggregate(
            identity, 'chat_reply:src'))


class AssistantCollapseTests(unittest.TestCase):
    def test_aggregate_plus_segments_keeps_aggregate(self):
        events = [
            {'event_id': 'u1', 'role': 'user', 'content': 'hi'},
            {'event_id': 'chat_reply:src', 'role': 'assistant',
             'content': 'full jp', 'subtitle': ''},
            {'event_id': 'chat_reply:src:0', 'role': 'assistant',
             'content': 'jp0', 'subtitle': 'zh0'},
            {'event_id': 'chat_reply:src:1', 'role': 'assistant',
             'content': 'jp1', 'subtitle': 'zh1'},
        ]
        out = assistant_turn.collapse_assistant_logical_turns(events)
        self.assertEqual([item['event_id'] for item in out], ['u1', 'chat_reply:src'])
        self.assertEqual(out[1]['content'], 'full jp')

    def test_segments_only_join_as_one_turn(self):
        events = [
            {'event_id': 'chat_reply:src:1', 'role': 'assistant',
             'content': '二段', 'subtitle': '二'},
            {'event_id': 'chat_reply:src:0', 'role': 'assistant',
             'content': '一段', 'subtitle': '一'},
        ]
        out = assistant_turn.collapse_assistant_logical_turns(events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['event_id'], 'chat_reply:src')
        self.assertEqual(out[0]['content'], '一段 二段')
        self.assertEqual(out[0]['subtitle'], '一 二')

    def test_partial_delete_joins_active_segments_only(self):
        events = [
            {'event_id': 'chat_reply:src', 'role': 'assistant', 'content': 'A B'},
            {'event_id': 'chat_reply:src:0', 'role': 'assistant', 'content': 'A',
             'status': 'deleted'},
            {'event_id': 'chat_reply:src:1', 'role': 'assistant', 'content': 'B'},
        ]
        out = assistant_turn.collapse_assistant_logical_turns(events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['content'], 'B')
        self.assertNotIn('A B', out[0]['content'])

    def test_all_segments_deleted_omits_the_turn(self):
        events = [
            {'event_id': 'chat_reply:src', 'role': 'assistant', 'content': 'A B'},
            {'event_id': 'chat_reply:src:0', 'role': 'assistant', 'content': 'A',
             'status': 'deleted'},
            {'event_id': 'chat_reply:src:1', 'role': 'assistant', 'content': 'B',
             'status': 'deleted'},
        ]
        out = assistant_turn.collapse_assistant_logical_turns(events)
        self.assertEqual(out, [])

    def test_delayed_explicit_metadata_collapses_internally(self):
        events = [
            {'event_id': 'delayed_reply:8:0', 'role': 'assistant', 'content': 'a',
             'metadata': {'assistant_turn_id': 'delayed_reply:8:0', 'segment_index': 0}},
            {'event_id': 'delayed_reply:8:1', 'role': 'assistant', 'content': 'b',
             'metadata': {'assistant_turn_id': 'delayed_reply:8:0', 'segment_index': 1}},
        ]
        out = assistant_turn.collapse_assistant_logical_turns(events)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['content'], 'a b')


class AssistantVisibleChatlogTests(unittest.TestCase):
    def setUp(self):
        self.store = ChatlogStore()
        self.patchers = [
            patch('db_chatlog.get_conn', side_effect=lambda: FakeConn(self.store)),
            patch('route_chatlog_search.get_conn', side_effect=lambda: FakeConn(self.store)),
            patch(
                'db_chat_media.get_conn',
                side_effect=RuntimeError('media db skipped in chatlog tests'),
            ),
        ]
        for item in self.patchers:
            item.start()

    def tearDown(self):
        for item in self.patchers:
            item.stop()

    def ids(self):
        msgs, _ = db_chatlog.get_messages('u1', 'gojo')
        return [m['client_msg_id'] or m['event_id'] for m in msgs]

    def seed_turn(self, source='src', extra_aggregate=None):
        db_chatlog.append_messages('u1', 'gojo', [
            _asst(
                f'chat_reply:{source}',
                '……それだ。 振り返らずに守れるくらい強くなれ。',
                '', extra_aggregate),
            _asst(f'chat_reply:{source}:0', '……それだ。', '……对了。'),
            _asst(f'chat_reply:{source}:1', '振り返らずに守れるくらい強くなれ。', '强到……'),
        ])

    def test_case1_ui_hides_aggregate_when_segments_exist(self):
        self.seed_turn()
        self.assertEqual(
            self.ids(),
            ['chat_reply:src:0', 'chat_reply:src:1'],
        )
        msgs, _ = db_chatlog.get_messages('u1', 'gojo')
        self.assertEqual([m['subtitle'] for m in msgs], ['……对了。', '强到……'])
        self.assertEqual(db_chatlog.count_messages('u1', 'gojo'), 2)
        remaining = [row['client_msg_id'] for row in self.store.rows]
        self.assertIn('chat_reply:src', remaining)

    def test_case2_aggregate_only_stays_as_fallback(self):
        db_chatlog.append_messages('u1', 'gojo', [
            _asst('chat_reply:src', '完整日语拼接版', ''),
        ])
        self.assertEqual(self.ids(), ['chat_reply:src'])
        self.assertEqual(db_chatlog.count_messages('u1', 'gojo'), 1)

    def test_case3_legacy_rows_without_turn_aggregate_flag(self):
        self.seed_turn(extra_aggregate={})
        extra = json.loads(self.store.rows[0]['extra'] or '{}')
        # append stamps identity; even without caller flag, id schema hides it
        self.assertEqual(self.ids(), ['chat_reply:src:0', 'chat_reply:src:1'])
        self.assertNotIn('chat_reply:src', self.ids())
        self.assertTrue(extra.get('turn_aggregate') or extra.get('assistant_turn_id'))

    def test_case3_historical_row_without_extra(self):
        self.store.insert_row(
            'u1', 'gojo', 'chat_reply:old', 'gojo', '完整日语拼接版',
            '', '', 'text', '', False, event_id='chat_reply:old')
        self.store.insert_row(
            'u1', 'gojo', 'chat_reply:old:0', 'gojo', '一段',
            '一', '', 'text', '', False, event_id='chat_reply:old:0')
        self.store.insert_row(
            'u1', 'gojo', 'chat_reply:old:1', 'gojo', '二段',
            '二', '', 'text', '', False, event_id='chat_reply:old:1')
        self.assertEqual(self.ids(), ['chat_reply:old:0', 'chat_reply:old:1'])

    def test_case6_delayed_and_proactive_stay_visible(self):
        db_chatlog.append_messages('u1', 'gojo', [
            _asst('delayed_reply:8:0', '遅1', '迟1', {
                'assistant_turn_id': 'delayed_reply:8:0',
                'segment_index': 0,
            }),
            _asst('delayed_reply:8:1', '遅2', '迟2', {
                'assistant_turn_id': 'delayed_reply:8:0',
                'segment_index': 1,
            }),
            _asst('proactive:report:1', '報告', '报告', {
                'assistant_turn_id': 'proactive:report:1',
                'segment_index': 0,
            }),
        ])
        self.assertEqual(
            self.ids(),
            ['delayed_reply:8:0', 'delayed_reply:8:1', 'proactive:report:1'],
        )

    def test_case7_pagination_hides_aggregate_across_windows(self):
        db_chatlog.append_messages('u1', 'gojo', [
            _asst('chat_reply:src', '完整日语拼接版', ''),
        ])
        for index in range(8):
            db_chatlog.append_messages('u1', 'gojo', [
                _msg(f'filler-{index}', f'垫{index}', 'user'),
            ])
        db_chatlog.append_messages('u1', 'gojo', [
            _asst('chat_reply:src:0', '一段', '一'),
            _asst('chat_reply:src:1', '二段', '二'),
        ])
        newest, has_more = db_chatlog.get_messages('u1', 'gojo', limit=2)
        self.assertEqual(
            [m['client_msg_id'] for m in newest],
            ['chat_reply:src:0', 'chat_reply:src:1'],
        )
        self.assertTrue(has_more)
        older, _ = db_chatlog.get_messages(
            'u1', 'gojo', limit=20, before_id=newest[0]['id'])
        older_ids = [m['client_msg_id'] for m in older]
        self.assertNotIn('chat_reply:src', older_ids)
        self.assertIn('filler-0', older_ids)

    def test_case8_search_does_not_return_hidden_aggregate(self):
        self.seed_turn()
        response = asyncio.run(route_chatlog_search.search_chatlog(
            user_id='u1', chat_id='gojo', keyword='それ', limit=20))
        body = json.loads(response.body)
        ids = [item['client_msg_id'] for item in body['results']]
        self.assertIn('chat_reply:src:0', ids)
        self.assertNotIn('chat_reply:src', ids)
        self.assertEqual(body['count'], len(body['results']))

    def test_case9_partial_delete_hides_deleted_segment_and_aggregate(self):
        db_chatlog.append_messages('u1', 'gojo', [
            _asst('chat_reply:src', 'A B', ''),
            _asst('chat_reply:src:0', 'A', '甲'),
            _asst('chat_reply:src:1', 'B', '乙'),
        ])
        deleted = db_chatlog.delete_message(
            'u1', 'gojo', client_msg_id='chat_reply:src:0')
        self.assertEqual(deleted, 1)
        self.assertEqual(self.ids(), ['chat_reply:src:1'])
        msgs, _ = db_chatlog.get_messages('u1', 'gojo')
        self.assertEqual([m['text'] for m in msgs], ['B'])
        self.assertEqual(db_chatlog.count_messages('u1', 'gojo'), 1)
        history = db_chatlog.get_prompt_history('u1', 'gojo', limit=10)
        self.assertEqual(len(history), 1)
        self.assertIn('B', history[0]['content'])
        self.assertNotIn('A B', history[0]['content'])
        stored = [row['client_msg_id'] for row in self.store.rows]
        self.assertIn('chat_reply:src', stored)

    def test_case10_all_segments_deleted_does_not_resurrect_aggregate(self):
        db_chatlog.append_messages('u1', 'gojo', [
            _asst('chat_reply:src', 'A B', ''),
            _asst('chat_reply:src:0', 'A', '甲'),
            _asst('chat_reply:src:1', 'B', '乙'),
        ])
        db_chatlog.delete_message('u1', 'gojo', client_msg_id='chat_reply:src:0')
        db_chatlog.delete_message('u1', 'gojo', client_msg_id='chat_reply:src:1')
        self.assertEqual(self.ids(), [])
        self.assertEqual(db_chatlog.count_messages('u1', 'gojo'), 0)
        history = db_chatlog.get_prompt_history('u1', 'gojo', limit=10)
        self.assertEqual(history, [])
        stored = [row['client_msg_id'] for row in self.store.rows]
        self.assertIn('chat_reply:src', stored)
        self.assertEqual(
            [row['status'] for row in self.store.rows
             if row['client_msg_id'] == 'chat_reply:src'][0],
            'active',
        )

    def test_case11_true_aggregate_only_fallback_never_had_segments(self):
        db_chatlog.append_messages('u1', 'gojo', [
            _asst('chat_reply:src', '完整日语拼接版', ''),
        ])
        self.assertEqual(self.ids(), ['chat_reply:src'])
        history = db_chatlog.get_prompt_history('u1', 'gojo', limit=10)
        self.assertEqual(len(history), 1)
        self.assertIn('完整日语拼接版', history[0]['content'])

    def test_prompt_history_uses_one_logical_turn(self):
        db_chatlog.append_messages('u1', 'gojo', [
            _msg('user-1', '你好', 'user'),
            _asst('chat_reply:src', '完整日语拼接版', ''),
            _asst('chat_reply:src:0', '一段', '一'),
            _asst('chat_reply:src:1', '二段', '二'),
        ])
        history = db_chatlog.get_prompt_history('u1', 'gojo', limit=10)
        self.assertEqual([item['role'] for item in history], ['user', 'assistant'])
        self.assertIn('完整日语拼接版', history[1]['content'])
        self.assertNotIn('一段', history[1]['content'])


class AssistantInternalContextTests(unittest.TestCase):
    def setUp(self):
        self.store = LayerStore()
        self.patchers = [
            patch('db.get_conn', side_effect=lambda: RawFakeConn(self.store)),
            patch('db_chatlog.get_conn', side_effect=lambda: RawFakeConn(self.store)),
            patch('raw_events.get_conn', side_effect=lambda: RawFakeConn(self.store)),
            patch('user_memory.get_conn', side_effect=lambda: RawFakeConn(self.store)),
            patch('builtins.print'),
        ]
        for item in self.patchers:
            item.start()
        self.raw_events = raw_events
        self.user_memory = user_memory
        db_chatlog.init_chatlog_table()
        raw_events.init_raw_event_layer()

    def tearDown(self):
        for item in self.patchers:
            item.stop()

    def test_case4_recent_context_collapses_aggregate_and_segments(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='user-9', role='user', content='那句话')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src', role='assistant',
            content='完整日语拼接版',
            metadata={'turn_aggregate': True, 'assistant_turn_id': 'chat_reply:src'})
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src:0', role='assistant',
            content='……それだ。', subtitle='……对了。')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src:1', role='assistant',
            content='振り返らずに守れるくらい強くなれ。', subtitle='强到……')
        recent = self.raw_events.get_recent_events('u', 'gojo', n=40)
        assistant = [item for item in recent if item['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]['event_id'], 'chat_reply:src')
        self.assertEqual(assistant[0]['content'], '完整日语拼接版')
        hot = self.raw_events.get_hot_candidate_events('u', 'gojo', n=40)
        self.assertEqual(
            [item['event_id'] for item in hot if item['role'] == 'assistant'],
            ['chat_reply:src'],
        )
        day = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0)
        local = self.raw_events.list_events_for_local_day('u', 'gojo', day)
        self.assertEqual(
            [item['event_id'] for item in local if item['role'] == 'assistant'],
            ['chat_reply:src'],
        )
        merged = self.user_memory._merge_recent_context('u', 'gojo', 40, 24)
        self.assertEqual(
            [item['event_id'] for item in merged if item['role'] == 'assistant'],
            ['chat_reply:src'],
        )

    def test_case5_segments_only_occupy_one_internal_turn(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:only:0', role='assistant',
            content='一段')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:only:1', role='assistant',
            content='二段')
        recent = self.raw_events.get_recent_events('u', 'gojo', n=40)
        self.assertEqual(len(recent), 1)
        self.assertEqual(recent[0]['event_id'], 'chat_reply:only')
        self.assertEqual(recent[0]['content'], '一段 二段')

    def test_case9_internal_context_uses_remaining_segment_only(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src', role='assistant',
            content='A B',
            metadata={'turn_aggregate': True, 'assistant_turn_id': 'chat_reply:src'})
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src:0', role='assistant',
            content='A')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src:1', role='assistant',
            content='B')
        db_chatlog.delete_message('u', 'gojo', client_msg_id='chat_reply:src:0')
        recent = self.raw_events.get_recent_events('u', 'gojo', n=40)
        assistant = [item for item in recent if item['role'] == 'assistant']
        self.assertEqual(len(assistant), 1)
        self.assertEqual(assistant[0]['content'], 'B')
        self.assertNotIn('A', assistant[0]['content'])
        history = db_chatlog.get_prompt_history('u', 'gojo', limit=10)
        self.assertEqual([item['content'] for item in history], ['B'])
        merged = self.user_memory._merge_recent_context('u', 'gojo', 40, 24)
        self.assertEqual(
            [item['content'] for item in merged if item['role'] == 'assistant'],
            ['B'],
        )

    def test_case10_internal_context_drops_turn_when_all_segments_deleted(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src', role='assistant',
            content='A B',
            metadata={'turn_aggregate': True, 'assistant_turn_id': 'chat_reply:src'})
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src:0', role='assistant',
            content='A')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src:1', role='assistant',
            content='B')
        db_chatlog.delete_message('u', 'gojo', client_msg_id='chat_reply:src:0')
        db_chatlog.delete_message('u', 'gojo', client_msg_id='chat_reply:src:1')
        self.assertEqual(self.raw_events.get_recent_events('u', 'gojo', n=40), [])
        self.assertEqual(db_chatlog.get_prompt_history('u', 'gojo', limit=10), [])
        self.assertEqual(
            self.user_memory._merge_recent_context('u', 'gojo', 40, 24), [])
        remaining = [
            row for row in self.store.chat_log
            if row.get('event_id') == 'chat_reply:src'
        ]
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0].get('status'), 'active')

    def test_case11_internal_keeps_true_aggregate_only(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:onlyagg', role='assistant',
            content='完整日语拼接版',
            metadata={
                'turn_aggregate': True,
                'assistant_turn_id': 'chat_reply:onlyagg',
            })
        recent = self.raw_events.get_recent_events('u', 'gojo', n=40)
        self.assertEqual([item['content'] for item in recent], ['完整日语拼接版'])

    def test_current_schema_list_events_for_assistant_turn(self):
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src', role='assistant',
            content='full')
        self.raw_events.append_raw_event(
            'u', 'gojo', event_id='chat_reply:src:0', role='assistant',
            content='一段')
        segs = self.raw_events.list_events_for_assistant_turn(
            'u', 'gojo', 'chat_reply:src')
        self.assertEqual(
            [item['event_id'] for item in segs],
            ['chat_reply:src', 'chat_reply:src:0'],
        )


class AssistantShortMemoryMetadataTests(unittest.TestCase):
    def test_apply_assistant_short_memory_marks_aggregate(self):
        saved = {}

        def save_short(user_id, role, content, character_id,
                       source_event_id=None, metadata=None, **_kwargs):
            saved.update({
                'user_id': user_id,
                'role': role,
                'content': content,
                'character_id': character_id,
                'source_event_id': source_event_id,
                'metadata': metadata,
            })

        generation_effects.apply_assistant_short_memory({
            'user_id': 'u',
            'character_id': 'gojo',
            'source_event_id': 'src',
            'endpoint': generation_effects.ENDPOINT_CHAT_TEXT,
            'full_jp': '完整日语拼接版',
            'save_short_memory': save_short,
            'payload': {'messages': [{'jp': '一段'}, {'jp': '二段'}]},
        })
        self.assertEqual(saved['source_event_id'], 'chat_reply:src')
        self.assertEqual(saved['content'], '完整日语拼接版')
        self.assertEqual(saved['metadata'], {
            'turn_aggregate': True,
            'assistant_turn_id': 'chat_reply:src',
        })


if __name__ == '__main__':
    unittest.main()
