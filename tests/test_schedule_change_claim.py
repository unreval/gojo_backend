"""Schedule change claims must use canonical intent, without live IO/models."""
import os
import sys
import json
import types
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
for path in (os.path.join(ROOT, 'gojo_backend'), os.path.dirname(__file__)):
    if path not in sys.path:
        sys.path.insert(0, path)

import db_schedule
from schedule_transition import validate_generated_schedule_reply
from schedule_contract import change_claim_conflict, normalize_action_intent


NOW = datetime(2026, 10, 18, 18, tzinfo=timezone(timedelta(hours=8)))
CLAIM = {'messages': [{'jp': '君のために予定を後ろにずらしたよ。',
                       'zh': '给你往后挪了。'}]}
REASON = 'schedule_change_claim_without_intent'
ACTIVE = {'event': {'id': 7, 'status': 'active', 'revision': 3}, 'now': NOW}
PLANNED = [{'id': 47, 'status': 'planned', 'planned_start_at': NOW + timedelta(hours=1)}]
NONACTIVE_WORLDS = (None, {}, {'event': None},
                    {'event': {'status': 'planned'}},
                    {'event': {'status': 'completed'}},
                    {'event': {'status': 'cancelled'}})


class ScheduleChangeClaimContractTests(unittest.TestCase):
    def test_change_wording_requires_intent(self):
        for text in ('给你往后挪了', '我把会议挪了', '推迟了', '延后了', '提前了',
                     '取消了', '时间空出来了', '时间腾出来了', '改到七点了',
                     '换到八点了', '换地方了', '今晚不去了',
                     '我把会议提前到晚饭之前了', '予定を後ろにずらしたよ。',
                     '会議をキャンセルした。'):
            with self.subTest(text=text):
                self.assertEqual(change_claim_conflict([{'zh': text}], ACTIVE, [], None), REASON)

    def test_questions_conditions_denials_and_past_are_not_claims(self):
        for text in ('要不要我挪一下？', '能挪吗', '能不能取消', '取消可以吗',
                     '往后挪一下?', '如果你想聊天，我就取消会议', '你愿意的话我挪一下',
                     '昨天我取消了会议', '之前我把会议往后挪了',
                     '我没有取消会议', '我不会取消会议', '不要取消会议',
                     '我会提前告诉你', '昨日は会議をキャンセルした。',
                     'もしよければ場所を変えたらどう？'):
            with self.subTest(text=text):
                self.assertIsNone(change_claim_conflict([{'zh': text}], ACTIVE, [], None))

    def test_reported_declarative_sentences_require_intent_for_future_plans(self):
        for text in (
                '我把明天的安排挪到后天了。',
                '不知道为什么，我把明天的安排取消了。',
                '总之我怎么说呢，今天的行程我提前了。',
                '那么，我把它改到周五了。',
                '我什么都没做，不过我把它取消了。'):
            for world in NONACTIVE_WORLDS:
                with self.subTest(text=text, world=world):
                    self.assertEqual(change_claim_conflict(
                        [{'zh': text}], world, PLANNED, None), REASON)

    def test_lexical_me_before_a_change_claim_is_not_a_question_particle(self):
        for prefix in ('不知道为什么', '我怎么说呢', '这么一来', '那么', '不论它多么重要'):
            text = prefix + '，我把明天的安排取消了。'
            with self.subTest(text=text):
                self.assertEqual(change_claim_conflict(
                    [{'zh': text}], None, PLANNED, None), REASON)

    def test_real_questions_remain_nonclaims_with_future_plans(self):
        for text in ('为什么取消了会议？', '你想怎么取消会议？',
                     '你说什么安排取消了？', '你要不要我挪一下？',
                     '这事儿怎么安排比较好？', '有什么需要我改到周五的吗？',
                     '你想把会议改到什么时候？', '怎么把会议往后挪？',
                     '能不能改到周五', '是否取消了会议',
                     '改到周五吗', '改到周五么', '可以取消会议么',
                     '会议取消了么，我还没收到通知'):
            for world in NONACTIVE_WORLDS:
                with self.subTest(text=text, world=world):
                    self.assertIsNone(change_claim_conflict(
                        [{'zh': text}], world, PLANNED, None))

    def test_denials_conditions_and_past_remain_nonclaims_with_future_plans(self):
        for text in ('我没有取消。', '我不会取消明天的安排。',
                     '不要取消明天的安排。', '如果你想聊天，我就取消会议。',
                     '要是你有空，我就取消会议。', '上周我取消了会议。',
                     '你愿意的话我挪一下。', '昨天我把它取消了。',
                     '之前我把会议往后挪了。'):
            with self.subTest(text=text):
                self.assertIsNone(change_claim_conflict(
                    [{'zh': text}], None, PLANNED, None))

    def test_change_claim_rule_is_deterministic_without_database_or_model_calls(self):
        with patch.object(db_schedule, 'get_conn') as connect, \
                patch('ai_client.create_chat') as create, \
                patch('ai_client.get_anthropic') as client, \
                patch('structured_output.invoke_structured_llm') as structured:
            for _ in range(2):
                self.assertEqual(change_claim_conflict(
                    [{'zh': '我把明天的安排取消了。'}], None, PLANNED, None), REASON)
            for dependency in (connect, create, client, structured):
                dependency.assert_not_called()

    def test_question_or_past_sentence_does_not_hide_another_claim(self):
        for text in ('要不要一起吃饭？我已经把会议取消了。',
                     '昨天我取消了会议。今晚的安排也给你往后挪了。',
                     '我还没有吃饭，已经把会议取消了。'):
            with self.subTest(text=text):
                self.assertEqual(change_claim_conflict([{'zh': text}], ACTIVE, [], None), REASON)

    def test_empty_or_past_only_schedule_does_not_activate_guard(self):
        for world in (None, {}, {'event': None}, {'event': {'status': 'completed'}},
                      {'event': {'status': 'cancelled'}}, {'last_completed_event': {'id': 7}}):
            self.assertIsNone(change_claim_conflict(CLAIM['messages'], world, [], None))

    def test_four_valid_change_intents_allow_claim(self):
        for raw in (
                {'type': 'cancel', 'event_id': 7, 'expected_revision': 3},
                {'type': 'relocate', 'event_id': 7, 'expected_revision': 3,
                 'planned_place': {'area_description': '书店'}},
                {'type': 'insert', 'event': {'title': '陪她聊天', 'duration_minutes': 30}},
                {'type': 'extend', 'event_id': 7, 'expected_revision': 3, 'extend_minutes': 30}):
            with self.subTest(action=raw['type']):
                intent = normalize_action_intent(raw)
                self.assertIsNotNone(intent)
                self.assertIsNone(change_claim_conflict(CLAIM['messages'], ACTIVE, [], intent))

    def test_invalid_or_completion_intent_does_not_allow_change(self):
        for raw in (None, {'type': 'cancel'}, {'type': 'unknown'},
                    {'type': 'complete', 'event_id': 7, 'expected_revision': 3}):
            self.assertEqual(change_claim_conflict(
                CLAIM['messages'], ACTIVE, [], normalize_action_intent(raw)), REASON)


class ScheduleChangeClaimIntegrationTests(unittest.TestCase):
    def test_future_event_change_without_intent_is_rejected(self):
        world = {'event': None, 'now': NOW}
        conn = Mock()
        conn.cursor.return_value.fetchall.return_value = [
            (47, 3, NOW + timedelta(hours=1), NOW + timedelta(hours=2),
             '晚饭', '', 'flexible', 'meal', 'free')]
        with patch.object(db_schedule, 'get_current_world_state', return_value=world), \
                patch.object(db_schedule, 'get_conn', return_value=conn):
            reason, actual_world = validate_generated_schedule_reply(
                'gojo', 'u', CLAIM, now=NOW)
        self.assertEqual(reason, REASON)
        self.assertIs(actual_world, world)
        conn.commit.assert_not_called()
        conn.cursor.return_value.close.assert_called_once()
        conn.close.assert_called_once()
        sql, params = conn.cursor.return_value.execute.call_args.args
        self.assertIn('status=%s AND planned_start_at > %s', sql)
        self.assertEqual(params, ('gojo', 'u', 'planned', NOW, NOW.date()))

    def test_active_event_change_without_intent_is_rejected(self):
        world = {'event': {'id': 7, 'status': 'active', 'revision': 3}, 'now': NOW}
        with patch.object(db_schedule, 'get_current_world_state', return_value=world):
            reason, actual_world = validate_generated_schedule_reply(
                'gojo', 'u', CLAIM, now=NOW)
        self.assertEqual(reason, REASON)
        self.assertIs(actual_world, world)

    def test_empty_future_read_allows_ordinary_reply(self):
        conn = Mock()
        conn.cursor.return_value.fetchall.return_value = []
        with patch.object(db_schedule, 'get_current_world_state', return_value={'now': NOW}), \
                patch.object(db_schedule, 'get_conn', return_value=conn):
            reason, _ = validate_generated_schedule_reply('gojo', 'u', CLAIM, now=NOW)
        self.assertIsNone(reason)
        conn.commit.assert_not_called()

    def test_completion_conflict_keeps_priority_and_avoids_planned_read(self):
        with patch.object(db_schedule, 'get_current_world_state', return_value=ACTIVE), \
                patch.object(db_schedule, 'get_conn') as connect:
            reason, _ = validate_generated_schedule_reply(
                'gojo', 'u', {'messages': [{'zh': '会议已经结束了，后面安排也取消了。'}]})
        self.assertEqual(reason, 'active_event_completion_claim_without_intent')
        connect.assert_not_called()

    def test_unavailable_planned_reader_keeps_chat_available_and_closes_connection(self):
        conn = Mock()
        conn.cursor.return_value.execute.side_effect = RuntimeError('offline reader unavailable')
        with patch.object(db_schedule, 'get_current_world_state', return_value={'now': NOW}), \
                patch.object(db_schedule, 'get_conn', return_value=conn), \
                patch('builtins.print') as logged:
            reason, _ = validate_generated_schedule_reply('gojo', 'u', {'messages': [{'zh': '知道了'}]})
        self.assertIsNone(reason)
        logged.assert_called_once()
        conn.cursor.return_value.close.assert_called_once()
        conn.close.assert_called_once()


class ChangeClaimRetryTests(unittest.TestCase):
    def generate(self, candidates):
        import route_chat
        response = types.SimpleNamespace(stop_reason='end_turn', usage=None)
        with patch.object(route_chat, '_create_json', side_effect=[
                (json.dumps(candidate, ensure_ascii=False), response) for candidate in candidates]) as provider, \
                patch.object(db_schedule, 'get_current_world_state', return_value=ACTIVE), \
                patch('builtins.print') as logged:
            result, _ = route_chat._generate_or_none(
                'offline', 100, [], [], attempts=5, log_tag='test', cache_tag='test',
                reject_fn=lambda parsed: validate_generated_schedule_reply('gojo', 'u', parsed)[0],
                generation_trace={'source_event_id': 'test'})
        traces = [json.loads(c.args[0].split(' ', 1)[1]) for c in logged.call_args_list
                  if c.args and isinstance(c.args[0], str) and c.args[0].startswith('[generation_trace] {')]
        return result, provider.call_count, traces

    def test_change_claim_uses_existing_single_semantic_retry(self):
        good = {'messages': [{'jp': '今なら話せるよ。', 'zh': '现在可以聊。'}]}
        result, calls, traces = self.generate([CLAIM, good])
        self.assertEqual(result['messages'], good['messages'])
        self.assertEqual(calls, 2)
        self.assertEqual(traces[0]['retry_reason'], REASON)
        self.assertTrue(traces[0]['will_retry'])
        self.assertFalse(traces[1]['will_retry'])

    def test_repeated_claim_is_rejected_after_shared_budget(self):
        result, calls, traces = self.generate([CLAIM, CLAIM])
        self.assertIsNone(result)
        self.assertEqual(calls, 2)
        self.assertFalse(traces[-1]['will_retry'])

    def test_protocol_repair_and_change_guard_share_total_budget(self):
        invalid = {'messages': [{'jp': '今日は疲れたよ', 'zh': '今日は疲れたよ'}]}
        result, calls, traces = self.generate([invalid, CLAIM])
        self.assertIsNone(result)
        self.assertEqual(calls, 2)
        self.assertEqual(traces[0]['outcome'], 'parse_invalid')
        self.assertEqual(traces[-1]['outcome'], 'candidate_rejected')
        self.assertFalse(traces[-1]['will_retry'])


class DelayedChangeClaimTests(unittest.TestCase):
    from tests.test_delayed_reply import DelayedReplyTests as _Fixture
    setUp = _Fixture.setUp

    def test_delayed_claim_rejects_before_commit_delivery_and_retains_retry(self):
        import delayed_reply
        from tests.test_delayed_reply import HelpersStub, _synthetic_claim
        helpers = HelpersStub(bubbles=CLAIM['messages'])
        bundle = dict(_synthetic_claim(8), reply_state='soft_busy')
        with patch('characters.get_character', return_value={'name': '五条'}), \
                patch('user_memory.save_user_short_memory_once', return_value=True), \
                patch('temporal_awareness.get_temporal_snapshot', return_value={}), \
                patch('prompt.build_system_blocks', return_value=[]), \
                patch.object(db_schedule, 'get_current_world_state', return_value=ACTIVE), \
                patch('schedule_transition.commit_generated_schedule_intent') as transition, \
                patch('user_memory.commit_visible_assistant_message') as commit, \
                patch('proactive_msg.add_proactive_msg') as deliver, \
                patch.object(db_schedule, 'iter_due_phone_checks', return_value=[8]), \
                patch.object(db_schedule, 'abort_delayed_reply', return_value=1) as abort, \
                patch.object(db_schedule, 'complete_delayed_reply') as complete, \
                patch.object(db_schedule, 'log_phone_check_action'):
            result = delayed_reply.process_due_phone_checks(
                NOW, evaluate_fn=lambda *_: {'action': 'reply', 'claimed': bundle},
                generate_fn=lambda claimed: delayed_reply.generate_delayed_chat_reply(claimed, helpers=helpers))
        self.assertEqual(result, [{'id': 8, 'action': 'failed', 'ok': False, 'reason': REASON}])
        self.assertEqual(helpers.generate_calls, 1)
        self.assertEqual(helpers.offline_calls, 0)
        abort.assert_called_once_with(8, 'token', retry_at=None, reason=REASON, now=NOW)
        for effect in (transition, commit, deliver, complete):
            effect.assert_not_called()


if __name__ == '__main__':
    unittest.main()
