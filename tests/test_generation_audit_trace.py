import asyncio
import copy
from contextlib import ExitStack
import json
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import route_chat  # noqa: E402


class GenerationAuditTraceTests(unittest.TestCase):
    def setUp(self):
        self.secret = 'PRIVATE-PROMPT-BODY-DO-NOT-LOG'
        episode = types.SimpleNamespace(
            item_type='episodic_memory',
            item_id='episode:ep-77',
            text=self.secret,
            source_event_ids=('source-1', 'source-2'),
            metadata={'raw': {
                'episode_id': 'ep-77',
                'content': self.secret,
                'score': 0.245,
                'semantic_score': 0.2,
                'lexical_score': 0.045,
                'score_mode': 'semantic_lexical',
            }},
        )
        schedule = types.SimpleNamespace(
            item_type='schedule',
            text=self.secret,
            metadata={
                'event_id': 'sched-9',
                'event_revision': 4,
                'event_status': 'active',
                'phase_id': 'phase-2',
                'phase_status': 'active',
                'phase_kind': 'meal',
                'reply_state': 'soft_busy',
                'title': self.secret,
                'location': self.secret,
                'note': self.secret,
            },
        )
        self.pack = types.SimpleNamespace(
            items=[episode, schedule],
            allocation={
                'hot': 120,
                'pinned': 20,
                'summary': 40,
                'recall': 75,
                'relationship': 15,
                'cognitive': 10,
                'aux': 30,
            },
        )
        self.system_blocks = [
            {'type': 'text', 'text': f'core {self.secret}',
             'cache_control': {'type': 'ephemeral'}},
            {'type': 'text', 'text': f'memory {self.secret}'},
        ]
        self.messages = [
            {'role': 'assistant', 'content': f'history {self.secret}'},
            {'role': 'user', 'content': f'current {self.secret}'},
        ]
        self.trace_context = {
            'source_event_id': 'evt-42',
            'turn_correlation_id': 'evt-42',
            'provider': 'anthropic',
            'context_pack': self.pack,
        }
        self.response = types.SimpleNamespace(
            model='claude-resolved-4-6',
            id='msg-123',
            _request_id='req-456',
            stop_reason='end_turn',
        )

    def _payload(self):
        return route_chat._build_generation_trace_payload(
            model='claude-requested-4-6',
            max_tokens=1500,
            system_blocks=self.system_blocks,
            messages=self.messages,
            attempt=1,
            attempts=3,
            trace_context=self.trace_context,
            response=self.response,
            outcome='accepted',
            will_retry=False,
        )

    def test_trace_records_requested_and_provider_response_metadata(self):
        payload = self._payload()
        self.assertEqual(payload['requested_model'], 'claude-requested-4-6')
        self.assertEqual(payload['resolved_model'], 'claude-resolved-4-6')
        self.assertEqual(payload['response'], {
            'model': 'claude-resolved-4-6',
            'id': 'msg-123',
            'provider_request_id': 'req-456',
            'stop_reason': 'end_turn',
        })
        self.assertEqual(payload['source_event_ref'], route_chat._trace_ref('evt-42'))
        self.assertIsNone(payload['assistant_turn_ref'])
        self.assertNotIn('source_event_id', payload)
        self.assertNotIn('assistant_turn_id', payload)
        self.assertNotIn('turn_correlation_id', payload)

    def test_trace_confirms_current_user_is_last_without_prompt_bodies(self):
        with patch('builtins.print') as logged:
            route_chat._emit_generation_trace(
                model='claude-requested-4-6',
                max_tokens=1500,
                system_blocks=self.system_blocks,
                messages=self.messages,
                attempt=1,
                attempts=3,
                trace_context=self.trace_context,
                response=self.response,
            )
        line = logged.call_args.args[0]
        payload = json.loads(line.split(' ', 1)[1])
        self.assertTrue(payload['current_user_is_last'])
        self.assertEqual(payload['message_count'], 2)
        self.assertEqual(payload['messages'][-1]['role'], 'user')
        self.assertEqual(payload['messages'][-1]['chars'], len(f'current {self.secret}'))
        self.assertEqual(payload['system_block_count'], 2)
        self.assertNotIn(self.secret, line)

    def test_trace_records_episode_scores_and_safe_schedule_metadata_only(self):
        payload = self._payload()
        self.assertEqual(payload['selected_episode_count'], 1)
        self.assertEqual(payload['selected_episodes'], [{
            'episode_ref': route_chat._trace_ref('ep-77'),
            'final_score': 0.245,
            'semantic_score': 0.2,
            'lexical_score': 0.045,
            'score_mode': 'semantic_lexical',
            'source_count': 2,
            'injected_chars': len(self.secret),
        }])
        self.assertEqual(payload['schedule_context'], {
            'injected': True,
            'block_count': 1,
            'chars': len(self.secret),
            'event_ref': route_chat._trace_ref('sched-9'),
            'event_revision': 4,
            'event_status': 'active',
            'phase_ref': route_chat._trace_ref('phase-2'),
            'phase_status': 'active',
            'phase_kind': 'meal',
            'reply_state': 'soft_busy',
        })
        self.assertNotIn('title', payload['schedule_context'])
        self.assertNotIn('location', payload['schedule_context'])
        self.assertNotIn('note', payload['schedule_context'])
        self.assertNotIn(self.secret, json.dumps(payload, ensure_ascii=False))

    def test_trace_preserves_request_objects_and_retry_count(self):
        system_blocks = copy.deepcopy(self.system_blocks)
        messages = copy.deepcopy(self.messages)
        expected_system_blocks = copy.deepcopy(system_blocks)
        expected_messages = copy.deepcopy(messages)
        valid = json.dumps({
            'emotion': '平静',
            'messages': [{'jp': 'そうだね', 'zh': '是啊'}],
        }, ensure_ascii=False)
        create = Mock(side_effect=[('', self.response), (valid, self.response)])
        with patch.object(route_chat, '_create_json', create), \
                patch('builtins.print'):
            result, _state = route_chat._generate_or_none(
                'claude-requested-4-6', 1500, system_blocks, messages,
                attempts=2, log_tag='trace-test', cache_tag='trace-test',
                generation_trace=self.trace_context,
            )
        self.assertEqual(create.call_count, 2)
        self.assertEqual(system_blocks, expected_system_blocks)
        self.assertEqual(messages, expected_messages)
        for call in create.call_args_list:
            self.assertEqual(call.args[2], expected_system_blocks)
            self.assertEqual(call.args[3], expected_messages)
        self.assertEqual(result['messages'][0]['jp'], 'そうだね')

    def _valid_reply(self):
        return json.dumps({
            'emotion': '平静',
            'messages': [{'jp': 'そうだね', 'zh': '是啊'}],
        }, ensure_ascii=False)

    def _trace_payloads(self, logged):
        payloads = []
        for call in logged.call_args_list:
            args = call.args
            if not args or not isinstance(args[0], str):
                continue
            if args[0].startswith('[generation_trace] {'):
                payloads.append(json.loads(args[0].split(' ', 1)[1]))
        return payloads

    def test_attempt_trace_marks_parse_invalid_then_accepted(self):
        create = Mock(side_effect=[
            ('', self.response),
            (self._valid_reply(), self.response),
        ])
        with patch.object(route_chat, '_create_json', create), \
                patch.object(route_chat, 'log_cache_usage'), \
                patch('builtins.print') as logged:
            result, _state = route_chat._generate_or_none(
                'claude-requested-4-6', 1500, self.system_blocks, self.messages,
                attempts=2, log_tag='trace-test', cache_tag='trace-test',
                generation_trace=self.trace_context,
            )

        self.assertEqual(result['messages'][0]['jp'], 'そうだね')
        self.assertEqual([
            (row['attempt'], row['outcome'], row['will_retry'], row['retry_reason'])
            for row in self._trace_payloads(logged)
        ], [
            (1, 'parse_invalid', True, 'parse_invalid'),
            (2, 'accepted', False, None),
        ])

    def test_candidate_rejection_uses_a_controlled_reason_then_retries(self):
        rejection_code = 'active_event_completion_claim_without_intent'
        reject = Mock(side_effect=[rejection_code, None])
        create = Mock(side_effect=[
            (self._valid_reply(), self.response),
            (self._valid_reply(), self.response),
        ])
        with patch.object(route_chat, '_create_json', create), \
                patch.object(route_chat, 'log_cache_usage'), \
                patch('builtins.print') as logged:
            result, _state = route_chat._generate_or_none(
                'claude-requested-4-6', 1500, self.system_blocks, self.messages,
                attempts=2, log_tag='trace-test', cache_tag='trace-test',
                reject_fn=reject, generation_trace=self.trace_context,
            )

        self.assertEqual(create.call_count, 2)
        self.assertEqual(reject.call_count, 2)
        self.assertEqual(result['messages'][0]['jp'], 'そうだね')
        traces = self._trace_payloads(logged)
        self.assertEqual(traces[0]['outcome'], 'candidate_rejected')
        self.assertTrue(traces[0]['will_retry'])
        self.assertEqual(traces[0]['retry_reason'], rejection_code)
        self.assertEqual(traces[1]['outcome'], 'accepted')
        self.assertFalse(traces[1]['will_retry'])

    def test_provider_error_trace_only_includes_error_type(self):
        sensitive_error = 'PROVIDER-ERROR-BODY-DO-NOT-LOG'
        create = Mock(side_effect=[
            TimeoutError(sensitive_error),
            (self._valid_reply(), self.response),
        ])
        with patch.object(route_chat, '_create_json', create), \
                patch.object(route_chat, 'log_cache_usage'), \
                patch('builtins.print') as logged:
            result, _state = route_chat._generate_or_none(
                'claude-requested-4-6', 1500, self.system_blocks, self.messages,
                attempts=2, log_tag='trace-test', cache_tag='trace-test',
                generation_trace=self.trace_context,
            )

        self.assertEqual(result['messages'][0]['jp'], 'そうだね')
        traces = self._trace_payloads(logged)
        self.assertEqual(traces[0]['outcome'], 'provider_error')
        self.assertTrue(traces[0]['will_retry'])
        self.assertEqual(traces[0]['retry_reason'], 'provider_error')
        self.assertEqual(traces[0]['error_type'], 'TimeoutError')
        self.assertEqual(traces[1]['outcome'], 'accepted')
        self.assertNotIn(sensitive_error, json.dumps(traces, ensure_ascii=False))

    def test_trace_builder_or_trace_print_failure_does_not_change_retries(self):
        for failure_kind in ('builder', 'print'):
            with self.subTest(failure_kind=failure_kind):
                create = Mock(side_effect=[
                    ('', self.response),
                    (self._valid_reply(), self.response),
                ])
                with ExitStack() as stack:
                    stack.enter_context(patch.object(route_chat, '_create_json', create))
                    stack.enter_context(patch.object(route_chat, 'log_cache_usage'))
                    if failure_kind == 'builder':
                        stack.enter_context(patch.object(
                            route_chat, '_build_generation_trace_payload',
                            side_effect=RuntimeError('trace builder failed')))
                        stack.enter_context(patch('builtins.print'))
                    else:
                        def fail_trace_print(*args, **_kwargs):
                            if args and isinstance(args[0], str) and args[0].startswith(
                                    '[generation_trace]'):
                                raise OSError('trace sink unavailable')

                        stack.enter_context(patch(
                            'builtins.print', side_effect=fail_trace_print))
                    result, _state = route_chat._generate_or_none(
                        'claude-requested-4-6', 1500,
                        copy.deepcopy(self.system_blocks), copy.deepcopy(self.messages),
                        attempts=2, log_tag='trace-test', cache_tag='trace-test',
                        generation_trace=self.trace_context,
                    )

                self.assertEqual(create.call_count, 2)
                self.assertEqual(result['messages'][0]['jp'], 'そうだね')

    def test_provider_metadata_access_failure_degrades_without_affecting_generation(self):
        class BrokenResponse:
            @property
            def model(self):
                raise RuntimeError('metadata unavailable')

            @property
            def id(self):
                raise RuntimeError('metadata unavailable')

            @property
            def _request_id(self):
                raise RuntimeError('metadata unavailable')

            @property
            def request_id(self):
                raise RuntimeError('metadata unavailable')

            @property
            def stop_reason(self):
                raise RuntimeError('metadata unavailable')

        create = Mock(return_value=(self._valid_reply(), BrokenResponse()))
        with patch.object(route_chat, '_create_json', create), \
                patch.object(route_chat, 'log_cache_usage'), \
                patch('builtins.print') as logged:
            result, _state = route_chat._generate_or_none(
                'claude-requested-4-6', 1500, self.system_blocks, self.messages,
                attempts=1, log_tag='trace-test', cache_tag='trace-test',
                generation_trace=self.trace_context,
            )

        self.assertEqual(create.call_count, 1)
        self.assertEqual(result['messages'][0]['jp'], 'そうだね')
        payload = self._trace_payloads(logged)[0]
        self.assertIsNone(payload['resolved_model'])
        self.assertEqual(payload['response'], {
            'model': None,
            'id': None,
            'provider_request_id': None,
            'stop_reason': None,
        })

    def test_trace_hashes_client_source_and_bounds_untrusted_metadata(self):
        client_source = 'CLIENT-SENSITIVE-SOURCE-EVENT-' * 8
        unsafe_value = 'UNTRUSTED-METADATA-DO-NOT-LOG-' * 8
        trace_context = dict(self.trace_context)
        trace_context.update({
            'source_event_id': client_source,
            'assistant_turn_id': client_source,
            'turn_correlation_id': client_source,
            'provider': unsafe_value,
        })
        system_blocks = copy.deepcopy(self.system_blocks)
        system_blocks[0]['type'] = unsafe_value
        messages = copy.deepcopy(self.messages)
        messages[0]['role'] = unsafe_value
        self.pack.items[0].metadata['raw']['score_mode'] = unsafe_value
        self.pack.items[1].metadata.update({
            'event_status': unsafe_value,
            'phase_status': unsafe_value,
            'phase_kind': unsafe_value,
            'reply_state': unsafe_value,
        })
        response = types.SimpleNamespace(
            model=unsafe_value,
            id=unsafe_value,
            _request_id=unsafe_value,
            stop_reason=unsafe_value,
        )

        first = route_chat._build_generation_trace_payload(
            model=unsafe_value, max_tokens=1500, system_blocks=system_blocks,
            messages=messages, attempt=1, attempts=2,
            trace_context=trace_context, response=response,
            outcome='accepted', will_retry=False,
        )
        second = route_chat._build_generation_trace_payload(
            model=unsafe_value, max_tokens=1500, system_blocks=system_blocks,
            messages=messages, attempt=2, attempts=2,
            trace_context=trace_context, response=response,
            outcome='accepted', will_retry=False,
        )
        rendered = json.dumps([first, second], ensure_ascii=False)

        self.assertEqual(first['source_event_ref'], route_chat._trace_ref(client_source))
        self.assertEqual(first['source_event_ref'], second['source_event_ref'])
        self.assertEqual(len(first['source_event_ref']), 24)
        self.assertEqual(first['assistant_turn_ref'], route_chat._trace_ref(client_source))
        self.assertEqual(first['provider'], 'unknown')
        self.assertEqual(first['system_blocks'][0]['type'], 'unknown')
        self.assertEqual(first['messages'][0]['role'], 'unknown')
        self.assertEqual(first['selected_episodes'][0]['score_mode'], 'unknown')
        self.assertEqual(first['schedule_context']['event_status'], 'unknown')
        self.assertEqual(first['schedule_context']['phase_status'], 'unknown')
        self.assertEqual(first['schedule_context']['phase_kind'], 'unknown')
        self.assertEqual(first['schedule_context']['reply_state'], 'unknown')
        self.assertEqual(first['response']['id'], route_chat._trace_ref(unsafe_value))
        self.assertEqual(first['response']['provider_request_id'],
                         route_chat._trace_ref(unsafe_value))
        self.assertEqual(first['response']['stop_reason'], 'unknown')
        self.assertNotIn(client_source, rendered)
        self.assertNotIn(unsafe_value, rendered)

    def test_turn_link_uses_stable_refs_without_raw_ids(self):
        source_event_id = 'CLIENT-SENSITIVE-SOURCE-EVENT-' * 8
        assistant_turn_id = 'chat_reply:' + source_event_id
        with patch('builtins.print') as logged:
            route_chat._emit_generation_turn_link(source_event_id, assistant_turn_id)

        payload = self._trace_payloads(logged)[0]
        self.assertEqual(payload['event'], 'assistant_turn_link')
        self.assertEqual(payload['source_event_ref'], route_chat._trace_ref(source_event_id))
        self.assertEqual(payload['assistant_turn_ref'],
                         route_chat._trace_ref(assistant_turn_id))
        self.assertEqual(len(payload['source_event_ref']), 24)
        self.assertNotIn(source_event_id, json.dumps(payload, ensure_ascii=False))
        self.assertNotIn(assistant_turn_id, json.dumps(payload, ensure_ascii=False))

    def test_schedule_trace_producer_allowlist_has_no_body_fields(self):
        source = Path(BACKEND, 'context_layer.py').read_text(encoding='utf-8')
        start = source.index("_add('schedule', schedule_text")
        end = source.index('    except Exception as exc:', start)
        producer = source[start:end]
        self.assertIn("'event_id'", producer)
        self.assertIn("'event_revision'", producer)
        self.assertIn("'event_status'", producer)
        self.assertIn("'phase_id'", producer)
        self.assertIn("'phase_status'", producer)
        self.assertIn("'phase_kind'", producer)
        self.assertIn("'reply_state'", producer)
        self.assertNotIn("'title'", producer)
        self.assertNotIn("'location'", producer)
        self.assertNotIn("'note'", producer)

    def _run_chat_with_commit_state(self, completed):
        events = []

        class Heartbeat:
            def __init__(self, *_args, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        class LatencyTrace:
            def __init__(self, *_args, **_kwargs):
                self.marks = {}

            def mark(self, name, value):
                self.marks[name] = value

            def emit(self):
                return None

        receipt = types.ModuleType('db_generation_receipt')
        receipt.ENDPOINT_CHAT_TEXT = 'chat_text'
        receipt.assign_source_event_id = lambda raw: (str(raw or 'assigned'), False)
        receipt.GenerationHeartbeat = Heartbeat
        receipt.hydrate_completed_generation_response = (
            lambda _u, _c, _source, _endpoint, payload=None: payload)

        effects = types.ModuleType('generation_effects')

        def commit_and_run_effects(*_args, **_kwargs):
            events.append('commit')
            return {'completed': completed, 'client_effects': []}

        effects.commit_and_run_effects = commit_and_run_effects
        effects.pending_client_effects = lambda *_args, **_kwargs: []
        effects.client_effects_pending_response = lambda *_args, **_kwargs: {}

        availability = types.ModuleType('reply_availability')
        availability.check_reply_availability = Mock(return_value={'can_reply': True})

        telemetry = types.ModuleType('latency_telemetry')
        telemetry.LatencyTrace = LatencyTrace
        telemetry.bind_trace = lambda _trace: object()
        telemetry.reset_trace = lambda _token: None

        reply = {'emotion': '平静', 'messages': [{'jp': 'そうだね', 'zh': '是啊'}]}
        stamped = [{'jp': 'そうだね', 'zh': '是啊',
                    'event_id': 'chat_reply:evt-link:0', 'segment_index': 0}]

        def emit_link(*_args):
            events.append('link')

        link = Mock(side_effect=emit_link)
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {
                'db_generation_receipt': receipt,
                'generation_effects': effects,
                'reply_availability': availability,
                'latency_telemetry': telemetry,
            }))
            stack.enter_context(patch.object(
                route_chat, 'get_character', return_value={'voice_id': None}))
            stack.enter_context(patch.object(
                route_chat, 'get_temporal_snapshot', return_value={'now_utc': None}))
            stack.enter_context(patch.object(route_chat, 'update_chat_days', return_value=3))
            stack.enter_context(patch.object(route_chat, 'get_short_memory', return_value=[]))
            stack.enter_context(patch.object(
                route_chat, '_turn_context',
                return_value=(types.SimpleNamespace(messages=[]), [])))
            stack.enter_context(patch.object(
                route_chat, '_history_plus_current', side_effect=lambda rows, current: rows + [
                    {'role': 'user', 'content': current}]))
            stack.enter_context(patch.object(
                route_chat, 'build_system_blocks', return_value=[{'type': 'text', 'text': 'safe'}]))
            stack.enter_context(patch.object(route_chat, 'save_user_short_memory_once'))
            stack.enter_context(patch.object(
                route_chat, '_gate_chat_generation',
                return_value=({'claim_token': 'claim', 'source_event_id': 'evt-link'}, None)))
            stack.enter_context(patch.object(route_chat, '_generate_or_none', return_value=(reply, None)))
            stack.enter_context(patch.object(
                route_chat, '_finalize_committed', return_value=('平静', stamped)))
            stack.enter_context(patch.object(
                route_chat, '_commit_schedule_candidate', return_value={'ok': True, 'noop': True}))
            stack.enter_context(patch.object(route_chat, '_commit_offline_state'))
            stack.enter_context(patch.object(
                route_chat, '_stamp_text_reply', return_value=('chat_reply:evt-link', stamped)))
            stack.enter_context(patch.object(route_chat, '_extract_pending_tx', return_value=None))
            stack.enter_context(patch.object(route_chat, '_text_effect_ctx', return_value={}))
            stack.enter_context(patch.object(route_chat, '_emit_generation_turn_link', link))
            stack.enter_context(patch('builtins.print'))
            response = asyncio.run(route_chat.chat_text({
                'user_id': 'u',
                'character_id': 'gojo',
                'text': '你好',
                'source_event_id': 'evt-link',
            }))
        return response, events, link

    def test_assistant_turn_link_only_follows_durable_commit(self):
        for completed in (True, False):
            with self.subTest(completed=completed):
                response, events, link = self._run_chat_with_commit_state(completed)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(events, ['commit', 'link'] if completed else ['commit'])
                if completed:
                    link.assert_called_once_with('evt-link', 'chat_reply:evt-link')
                else:
                    link.assert_not_called()


if __name__ == '__main__':
    unittest.main()
