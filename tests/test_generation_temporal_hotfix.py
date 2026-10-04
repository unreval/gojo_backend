"""Narrow generation contract and transient-context regressions; no live models."""
from datetime import datetime, timedelta, timezone
from contextlib import ExitStack
import json
import types
import unittest
from unittest.mock import Mock, patch

import route_chat
from generation_contract import GENERATION_ENVELOPE_SCHEMA
import temporal_awareness as temporal
import utils
import context_layer
import context_budget
import prompt


class PlaintextContractTests(unittest.TestCase):
    def generate(self, raw, *, stop_reason='end_turn', **kwargs):
        response = types.SimpleNamespace(
            content=[types.SimpleNamespace(type='text', text=raw)],
            stop_reason=stop_reason, usage=None)
        with patch.object(route_chat.claude_client.messages, 'create', return_value=response) as provider, \
                patch('builtins.print') as logged:
            result, state = route_chat._generate_or_none(
                'offline', 100, [], [], attempts=3, log_tag='test', cache_tag='test',
                salvage=True, generation_trace={'source_event_id': 'test'}, **kwargs)
        traces = [json.loads(c.args[0].split(' ', 1)[1]) for c in logged.call_args_list
                  if c.args and isinstance(c.args[0], str) and c.args[0].startswith('[generation_trace] {')]
        return result, state, provider.call_count, traces

    def test_plain_japanese_chinese_english_degrade_without_fake_translation(self):
        for raw in ('今日はゆっくり話そう', '现在我们慢慢聊吧', 'We can talk now'):
            with self.subTest(raw=raw):
                result, state, calls, traces = self.generate(raw)
                self.assertEqual(calls, 1)
                self.assertIsNone(state)
                self.assertEqual(result['messages'], [{'jp': raw, 'zh': ''}])
                self.assertEqual(result['_acceptance_mode'], 'plaintext')
                for field in ('schedule_action_intent', 'reminder', 'cancel_reminder',
                              'pending_transaction', 'proactive_promise'):
                    self.assertNotIn(field, result)
                self.assertIsNone(utils.normalize_plaintext_reply(raw))
                self.assertEqual(traces[0]['output_contract'], 'salvaged_plaintext')
                self.assertFalse(traces[0]['will_retry'])

    def test_nonverbal_plaintext_salvages_once(self):
        for raw in ('😒', '……', '...'):
            with self.subTest(raw=raw):
                result, state, calls, traces = self.generate(raw)
                self.assertEqual(calls, 1)
                self.assertIsNone(state)
                self.assertEqual(result['messages'], [{'jp': raw, 'zh': raw}])
                self.assertEqual(traces[0]['acceptance_mode'], 'nonverbal')

    def test_plaintext_does_not_call_the_model_again(self):
        response = types.SimpleNamespace(stop_reason='end_turn', usage=None)
        bilingual = '{"messages":[{"jp":"そうだね","zh":"是啊"}]}'
        with patch.object(route_chat, '_create_json', side_effect=[
                ('そうだね', response), (bilingual, response)]) as provider:
            result, state = route_chat._generate_or_none(
                'offline', 100, [], [], attempts=3, log_tag='test', cache_tag='test',
                salvage=True)
        self.assertEqual(provider.call_count, 1)
        self.assertIsNone(state)
        self.assertEqual(result['messages'], [{'jp': 'そうだね', 'zh': ''}])

    def test_invalid_or_protocol_output_fails_without_repeating_generation(self):
        for raw in ('', '?!', 'null', '\x00hello', '{"messages":[',
                    '{"messages":[],"reminder":{"content":"sleep"}}',
                    'hello\nschedule_action_intent: complete',
                    'JP: hello\nZH: 你好', '<thinking>private</thinking>',
                    'hello <<<OFFLINE_CHARACTER_STATES>>> {"inner":"x"}'):
            with self.subTest(raw=raw):
                result, state, calls, traces = self.generate(raw)
                self.assertIsNone(result)
                self.assertIsNone(state)
                self.assertEqual(calls, 1)
                self.assertEqual([t['output_contract'] for t in traces], ['retry_invalid'])

    def test_incomplete_provider_output_and_required_structure_are_not_salvaged(self):
        for reason in ('max_tokens', 'length', 'tool_use', 'pause_turn', 'refusal', 'content_filter'):
            with self.subTest(reason=reason):
                result, _, calls, _ = self.generate('話そう', stop_reason=reason)
                self.assertIsNone(result)
                self.assertEqual(calls, 1)
        result, _, calls, _ = self.generate('話そう', min_messages=3)
        self.assertIsNone(result)
        self.assertEqual(calls, 1)

    def test_structured_success_and_truth_guard_keep_existing_contract(self):
        raw = '{"messages":[{"jp":"そうだね","zh":"是啊"}]}'
        result, _, calls, traces = self.generate(raw)
        self.assertEqual(calls, 1)
        self.assertEqual(result['messages'], [{'jp': 'そうだね', 'zh': '是啊'}])
        self.assertEqual(traces[0]['output_contract'], 'structured_ok')
        reject = Mock(return_value='active_event_completion_claim_without_intent')
        result, _, calls, traces = self.generate(raw, reject_fn=reject)
        self.assertIsNone(result)
        self.assertEqual(calls, 2)
        self.assertEqual(reject.call_count, 2)
        self.assertEqual(traces[0]['outcome'], 'candidate_rejected')

    def test_protocol_rejection_reasons_distinguish_shape_and_fields(self):
        cases = (
            ('{"emotion":"平静"}', 'missing_messages'),
            ('{"messages":[', 'malformed_json'),
            ('{"messages":[{"jp":"こんにちは"}]}', 'invalid_message_field'),
            ('{"messages":[{"jp":"今日は","zh":"今日は"}]}',
             'jp_equals_zh_verbal'),
            ('{"messages":[{"jp":"そうだね","zh":"ねえ、悟って呼んで"}]}',
             'zh_contains_kana'),
            ('{"messages":[{"jp":"そうだね","zh":"「おはよう」"}]}',
             'zh_contains_kana'),
        )
        for raw, reason in cases:
            with self.subTest(reason=reason):
                result, _, calls, traces = self.generate(raw)
                self.assertIsNone(result)
                expected_calls = 1 if reason in ('missing_messages', 'malformed_json') else 2
                self.assertEqual(calls, expected_calls)
                self.assertEqual([t['parse_invalid_reason'] for t in traces],
                                 [reason] * expected_calls)
                self.assertEqual([t['retry_reason'] for t in traces],
                                 [reason, None] if expected_calls == 2 else [None])

    def test_model_cannot_spoof_the_degraded_mode(self):
        raw = json.dumps({
            '_acceptance_mode': 'plaintext',
            'messages': [{'jp': '今日は話せるよ', 'zh': ''}],
        }, ensure_ascii=False)
        result, _, calls, traces = self.generate(raw)
        self.assertIsNone(result)
        self.assertEqual(calls, 2)
        self.assertEqual(traces[0]['parse_invalid_reason'], 'invalid_message_field')

    def test_short_structured_story_gets_only_one_semantic_repair(self):
        response = types.SimpleNamespace(stop_reason='end_turn', usage=None)
        bubble = {'jp': 'そうだね', 'zh': '是啊'}
        short = json.dumps({'messages': [bubble]}, ensure_ascii=False)
        complete = json.dumps({'messages': [bubble] * 3}, ensure_ascii=False)
        with patch.object(route_chat, '_create_json', side_effect=[
                (short, response), (complete, response)]) as provider:
            result, _ = route_chat._generate_or_none(
                'offline', 100, [], [], attempts=5, log_tag='story',
                cache_tag='story', min_messages=3)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(len(result['messages']), 3)


class GenerationEnvelopeTransportTests(unittest.TestCase):
    @staticmethod
    def response(raw='{"emotion":"平静","messages":[{"jp":"そうだね","zh":"是啊"}]}'):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type='text', text=raw)],
            stop_reason='end_turn', usage=None)

    def test_request_uses_the_shared_json_schema(self):
        model = 'claude-structured-contract-test'
        route_chat._schema_unavailable_models.discard(model)
        with patch.object(route_chat.claude_client.messages, 'create',
                          return_value=self.response()) as provider:
            raw, _ = route_chat._create_json(model, 100, [], [])
        self.assertIn('そうだね', raw)
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(provider.call_args.kwargs['output_config']['format']['schema'],
                         GENERATION_ENVELOPE_SCHEMA)
        self.assertEqual(GENERATION_ENVELOPE_SCHEMA['required'], ['emotion', 'messages'])
        self.assertFalse(GENERATION_ENVELOPE_SCHEMA['additionalProperties'])

    def test_unsupported_schema_falls_back_and_caches_capability(self):
        model = 'claude-unsupported-schema-test'
        route_chat._schema_unavailable_models.discard(model)
        try:
            with patch.object(route_chat.claude_client.messages, 'create',
                              side_effect=[TypeError('unexpected keyword argument output_config'),
                                           self.response(), self.response()]) as provider, \
                    patch('builtins.print'):
                route_chat._create_json(model, 100, [], [])
                route_chat._create_json(model, 100, [], [])
            self.assertEqual(provider.call_count, 3)
            self.assertIn('output_config', provider.call_args_list[0].kwargs)
            self.assertNotIn('output_config', provider.call_args_list[1].kwargs)
            self.assertNotIn('output_config', provider.call_args_list[2].kwargs)
        finally:
            route_chat._schema_unavailable_models.discard(model)

    def test_generation_result_counts_schema_fallback_as_two_provider_calls(self):
        model = 'claude-schema-fallback-count-test'
        route_chat._schema_unavailable_models.discard(model)
        try:
            with patch.object(route_chat.claude_client.messages, 'create', side_effect=[
                    TypeError('unexpected keyword argument output_config'),
                    self.response()]) as provider, patch('builtins.print') as logged:
                result, _ = route_chat._generate_or_none(
                    model, 100, [], [], attempts=2, log_tag='test', cache_tag='test',
                    generation_trace={'source_event_id': 'test'})
            summaries = [json.loads(call.args[0].split(' ', 1)[1])
                         for call in logged.call_args_list
                         if call.args and isinstance(call.args[0], str)
                         and call.args[0].startswith('[generation_result] {')]
            self.assertEqual(provider.call_count, 2)
            self.assertIsNotNone(result)
            self.assertEqual(len(summaries), 1)
            self.assertEqual(summaries[0]['status'], 'structured_ok')
            self.assertEqual((summaries[0]['provider_calls'],
                              summaries[0]['semantic_retries']), (2, 0))
        finally:
            route_chat._schema_unavailable_models.discard(model)

    def test_provider_429_never_enters_semantic_retry(self):
        class RateLimited(Exception):
            status_code = 429

        model = 'claude-rate-limited-test'
        route_chat._schema_unavailable_models.discard(model)
        with patch.object(route_chat.claude_client.messages, 'create',
                          side_effect=RateLimited('429')) as provider, \
                patch('builtins.print'):
            result, state = route_chat._generate_or_none(
                model, 100, [], [], attempts=3, log_tag='test', cache_tag='test',
                salvage=True)
        self.assertIsNone(result)
        self.assertIsNone(state)
        self.assertEqual(provider.call_count, 1)



class StructuredBoundaryTests(unittest.TestCase):
    def test_final_plaintext_does_not_satisfy_structured_business_schemas(self):
        from structured_output import parse_structured_output
        from user_memory import _validate_memory_output
        from relationship_signals import _validate_signals_envelope
        for validator in (_validate_memory_output, _validate_signals_envelope):
            self.assertIsNone(utils.normalize_plaintext_reply('现在可以慢慢聊。'))
            self.assertFalse(parse_structured_output(
                '现在可以慢慢聊。', schema_validator=validator).ok)
            self.assertFalse(parse_structured_output('{}', schema_validator=validator).ok)
        for raw, validator in (
                ('{"bond":{"content":42}}', _validate_memory_output),
                ('{"signals":{}}', _validate_signals_envelope)):
            self.assertEqual(parse_structured_output(raw, schema_validator=validator).error_code,
                             'schema_validation_failed')
            self.assertIsNone(utils.normalize_plaintext_reply(raw))

    def test_structured_invalid_json_still_retries_without_final_reply_salvage(self):
        import relationship_signals
        for invalid in ('{"signals": nope}', '{"signals":[]} }',
                        '{"signals":[]} {"signals":[{}]}', '{"signals":{}}',
                        '现在可以慢慢聊。'):
            with self.subTest(invalid=invalid), patch.object(
                    relationship_signals, 'create_chat', side_effect=[
                        (invalid, {'stop_reason': 'end_turn'}),
                        ('```json\n{"signals":[]}\n```', {'stop_reason': 'end_turn'})]) as provider, patch.object(
                    utils, 'normalize_plaintext_reply', side_effect=AssertionError('wrong channel')):
                result = relationship_signals.extract_signals('test', model='offline')
                self.assertIsNone(result['error'])
                self.assertEqual(result['signals'], [])
                self.assertEqual(provider.call_count, 2)

    def test_unique_wrappers_succeed_once_and_invalid_siblings_fail_closed(self):
        from structured_output import invoke_structured_llm, parse_structured_output
        from user_memory import _validate_memory_output
        from relationship_signals import _validate_signals_envelope
        for raw, validator, schema in (
                ('{"bond":null}', _validate_memory_output, 'memory_extractor'),
                ('{"signals":[]}', _validate_signals_envelope, 'relationship_signals')):
            for wrapped in (raw, '```json\n' + raw + '\n```', 'Result: ' + raw + ' Done.'):
                with self.subTest(schema=schema, wrapped=wrapped):
                    provider = Mock(return_value=(wrapped, {'stop_reason': 'end_turn'}))
                    call = invoke_structured_llm(
                        domain=schema, create_chat_fn=provider, model='offline', messages=[],
                        schema_validator=validator, schema_name=schema)
                    self.assertTrue(call.parsed.ok)
                    provider.assert_called_once()
            for malformed in (raw + ' }', raw + ' {"x": nope}', raw + ' {"other":1}'):
                parsed = parse_structured_output(malformed, schema_validator=validator)
                self.assertFalse(parsed.ok)
                self.assertIsNone(parsed.value)
            parsed = parse_structured_output(raw + ' }', schema_validator=validator)
            self.assertEqual((parsed.error_code, parsed.candidate_count,
                              parsed.distinct_candidate_count), ('invalid_json', 1, 0))


class TemporalApplicabilityTests(unittest.TestCase):
    def at(self, hour, minute):
        return datetime(2026, 9, 30, hour, minute, tzinfo=temporal.CN_TZ)

    def snapshot(self, now, assistant, user):
        canonical = {'first_interaction_at': assistant, 'last_interaction_at': user,
                     'last_user_message_at': user, 'last_assistant_message_at': assistant}
        with patch.object(temporal, '_load_row', return_value={
                'last_interaction_at': self.at(5, 18),
                'last_user_message_at': self.at(5, 17)}), \
                patch.object(temporal, '_load_canonical_activity', return_value=canonical):
            return temporal.get_temporal_snapshot('u', 'gojo', now_utc=now)

    def test_morning_sleep_context_is_historical_after_new_user_activity(self):
        snap = self.snapshot(self.at(13, 30), self.at(5, 18), self.at(13, 30))
        self.assertEqual(snap['now_local'], self.at(13, 30))
        self.assertEqual(snap['latest_user_at'], self.at(13, 30))
        self.assertEqual(snap['last_interaction_at'], self.at(13, 30))
        self.assertEqual(snap['elapsed_seconds_since_last_interaction'], 0)
        self.assertEqual(snap['elapsed_since_assistant'], 8 * 3600 + 12 * 60)
        self.assertEqual(snap['conversational_state']['applicability'], 'historical')
        self.assertEqual(snap['conversational_state']['reason'], 'new_activity_after_gap')
        text = temporal.build_prompt_context('u', 'gojo', snap)
        self.assertIn('13:30', text)
        self.assertIn('05:18', text)
        self.assertIn('不证明用户现在仍准备睡觉', text)
        self.assertIn('kind: latest_canonical_assistant_reply', text)
        self.assertIn('elapsed: 8小时12分钟（29520 seconds）', text)

    def test_evening_1724_keeps_early_morning_reply_historical(self):
        snap = self.snapshot(self.at(17, 24), self.at(5, 18), self.at(17, 24))
        text = temporal.build_prompt_context('u', 'gojo', snap)
        self.assertEqual(snap['conversational_state']['applicability'], 'historical')
        self.assertIn('17:24', text)
        self.assertIn('05:18', text)
        self.assertIn('不证明用户现在仍准备睡觉', text)

    def test_seven_minute_insomnia_conversation_keeps_continuity(self):
        snap = self.snapshot(self.at(5, 25), self.at(5, 18), self.at(5, 25))
        self.assertEqual(snap['conversational_state']['applicability'], 'continuing')
        self.assertEqual(snap['conversational_state']['age_seconds'], 7 * 60)
        before_user_commit = self.snapshot(self.at(5, 25), self.at(5, 18), self.at(5, 17))
        self.assertEqual(before_user_commit['conversational_state']['applicability'], 'continuing')

    def test_new_activity_after_gap_and_long_silence_expire_applicability(self):
        snap = self.snapshot(self.at(12, 15), self.at(11, 40), self.at(12, 15))
        self.assertEqual(snap['conversational_state']['applicability'], 'historical')
        snap = self.snapshot(self.at(8, 30), self.at(5, 18), self.at(5, 17))
        self.assertEqual(snap['conversational_state']['applicability'], 'historical')
        self.assertFalse(snap['conversational_state']['newer_user_activity'])

    def test_display_timezone_cannot_change_applicability(self):
        start = datetime(2026, 9, 30, 3, 40, tzinfo=timezone.utc)
        now = start + timedelta(minutes=35)
        for hours in (-5, 0, 8):
            with self.subTest(hours=hours), patch.object(temporal, 'CN_TZ', timezone(timedelta(hours=hours))):
                snap = self.snapshot(now, start, start - timedelta(minutes=1))
                self.assertEqual(snap['conversational_state']['applicability'], 'historical')
                snap = self.snapshot(now, start, now)
                self.assertEqual(snap['conversational_state']['applicability'], 'historical')
                self.assertEqual(snap['timezone_source'], 'legacy_app_config')

    def test_configured_timezone_and_missing_canonical_activity(self):
        zone = timezone(timedelta(hours=-5))
        with patch.object(temporal, 'CN_TZ', zone):
            snap = self.snapshot(self.at(13, 30), self.at(5, 18), self.at(13, 30))
            self.assertEqual(snap['now_local'].utcoffset(), timedelta(hours=-5))
            self.assertTrue(snap['current_timestamp'].endswith('-05:00'))
        with patch.object(temporal, '_load_canonical_activity', return_value=None), \
                patch.object(temporal, '_load_row', return_value={'last_interaction_at': self.at(5, 18)}):
            snap = temporal.get_temporal_snapshot('u', 'gojo', now_utc=self.at(13, 30))
        self.assertIsNone(snap['latest_user_at'])
        self.assertEqual(snap['conversational_state']['applicability'], 'unknown')

    def test_text_voice_hot_history_and_prompt_share_the_frozen_snapshot(self):
        events = [
            {'event_id': 'sleep', 'role': 'assistant', 'content': '晚安，快睡。', 'timestamp': self.at(5, 18)},
            {'event_id': 'return', 'role': 'user', 'content': '(蹭蹭)', 'timestamp': self.at(11, 4)},
            {'event_id': 'current', 'role': 'user', 'content': '现在聊聊', 'timestamp': self.at(13, 30)},
        ]
        texts = []
        for profile in ('text', 'voice'):
            with self.subTest(profile=profile), ExitStack() as stack:
                snap = self.snapshot(self.at(13, 30), self.at(5, 18), self.at(13, 30))
                stack.enter_context(patch.object(context_layer, 'list_rolling_summaries', return_value=[]))
                stack.enter_context(patch.object(context_layer, 'list_pins', return_value=[]))
                stack.enter_context(patch('auto_pin.maybe_auto_pin'))
                pack = context_layer.assemble_from_events(
                    events, user_id='u', character_id='gojo', user_message='现在聊聊',
                    now=snap['now_utc'], temporal_snapshot=snap,
                    config=context_budget.BudgetConfig.for_profile(profile),
                    current_event_id='current', include_recall=False)
                self.assertIn('晚安，快睡。', [m['content'] for m in pack.messages])
                self.assertEqual(events[0]['content'], '晚安，快睡。')
                self.assertIs(pack.temporal_snapshot, snap)
                self.assertIn('11:04', temporal._as_local(snap['historical_context_through']).isoformat())
                # A cached support block and exhausted aux budget cannot change now.
                pack.support_ready = pack.recall_ready = True
                pack.temporal_text = 'STALE-CLOCK-05:18'
                pack.schedule_text = 'STALE-SLEEP-CURRENT'
                for name, value in (
                        ('get_character', {'core_prompt': 'core'}),
                        ('load_canon_lock', ''), ('get_first_interaction_days', 1),
                        ('_accounts_block', '')):
                    stack.enter_context(patch.object(prompt, name, return_value=value))
                stack.enter_context(patch('memory_authority.filter_recall_authority', return_value={}))
                stack.enter_context(patch('smart_recall.format_recall_for_prompt', return_value=('', '', '')))
                schedule_reader = stack.enter_context(patch('db_schedule.format_world_prompt', return_value=('', {
                    'now': self.at(13, 30), 'availability': {'reply_state': 'free'}})))
                stack.enter_context(patch.object(context_layer, 'revalidate_pack_blocks', return_value={
                    key: '' for key in ('cognitive_prompt_text', 'pinned_prompt_text',
                                        'summary_prompt_text', 'episode_prompt_text')}))
                with patch.object(temporal, 'get_temporal_snapshot', side_effect=AssertionError('second clock')):
                    blocks = prompt.build_system_blocks('u', 'gojo', '现在聊聊', context_pack=pack)
                schedule_reader.assert_called_once_with('gojo', 'u', self.at(13, 30))
                text = blocks[-1]['text']
                self.assertIn('13:30', text)
                self.assertIn('conversational_state: historical', text)
                self.assertNotIn('STALE-', text)
                texts.append(text)
        self.assertEqual(texts[0], texts[1])


class CanonicalActivitySQLTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.offline_pg import Connection
        cls.database = Connection()
        cls.database.query('''CREATE TABLE chat_log (
            user_id TEXT, chat_id TEXT, event_id TEXT, client_msg_id TEXT,
            role TEXT, status TEXT, created_at TIMESTAMPTZ)''')
        cls.database.query('''CREATE TABLE chat_log_tombstone (
            user_id TEXT, chat_id TEXT, client_msg_id TEXT)''')

    @classmethod
    def tearDownClass(cls):
        cls.database.shutdown()

    def test_canonical_activity_ignores_other_scopes_deleted_and_tombstoned_rows(self):
        self.database.query('''INSERT INTO chat_log VALUES
            ('u','gojo','a','a','gojo','active','2026-09-30T05:18:00Z'),
            ('u','gojo','u1','u1','user','active','2026-09-30T11:04:00Z'),
            ('u','gojo','u2','u2','user','active','2026-09-30T13:30:00Z'),
            ('other','gojo','other','other','user','active','2026-09-30T19:00:00Z'),
            ('u','other','other2','other2','gojo','active','2026-09-30T19:00:00Z'),
            ('u','gojo','future','future','user','active','2026-09-30T20:00:00Z'),
            ('u','gojo','deleted','deleted','gojo','deleted','2026-09-30T19:00:00Z'),
            ('u','gojo','tomb','tomb','gojo','active','2026-09-30T19:00:00Z')''')
        self.database.query("INSERT INTO chat_log_tombstone VALUES ('u','gojo','tomb')")
        with patch.object(temporal, 'get_conn', return_value=self.database):
            row = temporal._load_canonical_activity(
                'u', 'gojo', datetime(2026, 9, 30, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(row['last_user_message_at'], datetime(2026, 9, 30, 13, 30, tzinfo=timezone.utc))
        self.assertEqual(row['last_assistant_message_at'], datetime(2026, 9, 30, 5, 18, tzinfo=timezone.utc))
        self.assertEqual(self.database.query('SELECT COUNT(*) AS n FROM chat_log')['rows'][0]['n'], 8)


if __name__ == '__main__':
    unittest.main()
