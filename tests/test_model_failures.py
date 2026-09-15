import asyncio
import importlib.util
import json
import os
import sys
import types
import unittest
from unittest.mock import Mock, patch


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


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


def load_client():
    return load_source('ai_client', {
        'anthropic': stub('anthropic', Anthropic=Mock()),
        'requests': stub('requests'),
    })


def model_response(text='', stop='end_turn', thinking=False):
    blocks = []
    if thinking:
        blocks.append(types.SimpleNamespace(type='thinking', thinking='PRIVATE_THINKING'))
    if text:
        blocks.append(types.SimpleNamespace(type='text', text=text))
    return types.SimpleNamespace(
        content=blocks, stop_reason=stop, id='test-response-id',
        usage=types.SimpleNamespace(input_tokens=1400, output_tokens=1600 if thinking else 40),
    )


PNG = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/pLvAAAAAElFTkSuQmCC'
REPLY = json.dumps({'emotion': '平静', 'messages': [{
    'jp': '赤い画像が見えるよ。', 'zh': '能看到红色的图片。',
}]}, ensure_ascii=False)


class ImageFailureTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        router = Mock()
        router.post.side_effect = lambda *_args, **_kwargs: lambda function: function
        self.memory = stub('user_memory',
                           save_short_memory=Mock(),
                           save_user_short_memory_once=Mock(return_value=True),
                           get_short_memory=Mock(return_value=[]),
                           update_chat_days=Mock(return_value=3))
        self.jobs = Mock()
        self.state = Mock()
        self.record_turn = Mock()
        self.ai = load_client()
        modules = {
            'anthropic': stub('anthropic', Anthropic=Mock(return_value=self.client)),
            'fastapi': stub('fastapi', APIRouter=Mock(return_value=router)),
            'fastapi.responses': stub('fastapi.responses', JSONResponse=lambda content, status_code=200:
                                      types.SimpleNamespace(body=json.dumps(content).encode(),
                                                            status_code=status_code)),
            'db': stub('db', get_conn=Mock(side_effect=AssertionError('unexpected DB access'))),
            'ai_client': self.ai,
            'tts': stub('tts', tts_to_b64=Mock(return_value='')),
            'prompt': stub('prompt', build_system_blocks=Mock(return_value=[
                {'type': 'text', 'text': '角色设定'}]), log_cache_usage=Mock()),
            'user_memory': self.memory,
            'memory_jobs': stub('memory_jobs', enqueue_private_extraction=self.jobs),
            'temporal_awareness': stub('temporal_awareness',
                                      get_temporal_snapshot=Mock(return_value={}),
                                      record_turn=self.record_turn),
            'characters': stub('characters', get_character=Mock(return_value={'voice_id': None})),
            'tasks': stub('tasks', find_duplicate_task=Mock(),
                          find_and_delete_tasks_by_keyword=Mock(), delete_latest_task=Mock()),
            'task_dedup': stub('task_dedup', find_similar_task=Mock()),
            'relationship_state': stub('relationship_state', save_offline_character_state=self.state),
        }
        module_patch = patch.dict(sys.modules, modules)
        module_patch.start()
        self.addCleanup(module_patch.stop)
        self.route = load_source('route_image', modules)
        log_patch = patch('builtins.print')
        self.log = log_patch.start()
        self.addCleanup(log_patch.stop)

    def send(self, **extra):
        data = {'user_id': 'u', 'character_id': 'gojo', 'image_base64': PNG,
                'media_type': 'image/jpeg', 'text': '看这张图'}
        data.update(extra)
        response = asyncio.run(self.route.chat_image(data))
        return response, json.loads(response.body)

    def assert_no_memory_writes(self):
        self.memory.save_short_memory.assert_not_called()
        self.jobs.assert_not_called()
        self.state.assert_not_called()
        self.record_turn.assert_not_called()

    def assert_generation_failed(self, response, result):
        self.assertEqual(response.status_code, 502)
        self.assertEqual(result['error'], 'generation_failed')
        self.assertTrue(result['generation_failed'])
        self.assertEqual(result['messages'], [])
        self.assert_no_memory_writes()
        self.memory.save_user_short_memory_once.assert_called()

    def test_empty_responses_are_bounded_and_do_not_pretend_image_was_seen(self):
        self.client.messages.create.return_value = model_response()
        response, result = self.send()
        self.assertEqual(self.client.messages.create.call_count, 3)
        self.assert_generation_failed(response, result)
        self.assertNotIn('没能读出图片', json.dumps(result, ensure_ascii=False))

    def test_ellipsis_is_rejected_by_commit_gate(self):
        self.client.messages.create.return_value = model_response(json.dumps({
            'emotion': '平静',
            'messages': [{'jp': '...', 'zh': '...'}],
        }, ensure_ascii=False))
        response, result = self.send()
        self.assertEqual(self.client.messages.create.call_count, 3)
        self.assert_generation_failed(response, result)

    def test_source_event_id_is_forwarded_to_user_memory(self):
        self.client.messages.create.return_value = model_response()
        self.send(source_event_id='img-evt-1')
        self.memory.save_user_short_memory_once.assert_called()
        self.assertEqual(
            self.memory.save_user_short_memory_once.call_args.kwargs['source_event_id'],
            'img-evt-1',
        )

    def test_thinking_only_truncation_retries_with_more_tokens_and_same_image(self):
        self.client.messages.create.side_effect = [
            model_response(stop='max_tokens', thinking=True), model_response(REPLY),
        ]
        _, result = self.send()
        first, second = self.client.messages.create.call_args_list
        self.assertGreater(second.kwargs['max_tokens'], first.kwargs['max_tokens'])
        for call in (first, second):
            source = call.kwargs['messages'][-1]['content'][0]['source']
            self.assertEqual(source, {'type': 'base64', 'media_type': 'image/png', 'data': PNG})
        self.assertNotIn('error', result)
        self.assertEqual(self.memory.save_short_memory.call_count, 1)
        self.memory.save_user_short_memory_once.assert_called()
        self.assertNotIn('PRIVATE_THINKING', str(self.log.call_args_list))
        self.assertIn('max_tokens', str(self.log.call_args_list))

    def test_empty_retry_adds_guidance_without_empty_assistant_turn(self):
        self.client.messages.create.side_effect = [model_response(), model_response(REPLY)]
        _, result = self.send()
        first, second = self.client.messages.create.call_args_list
        self.assertGreater(len(second.kwargs['system']), len(first.kwargs['system']))
        self.assertEqual(first.kwargs['messages'], second.kwargs['messages'])
        self.assertNotIn('error', result)

    def test_truncated_json_is_not_accepted_even_if_a_complete_object_can_be_parsed(self):
        self.client.messages.create.side_effect = [model_response(REPLY, 'max_tokens'),
                                                  model_response(REPLY)]
        self.send()
        self.assertEqual(self.client.messages.create.call_count, 2)
        self.assertEqual(self.memory.save_short_memory.call_count, 1)

    def test_bad_request_and_refusal_stop_without_blind_retries(self):
        error = RuntimeError('invalid image request')
        error.status_code = 400
        self.client.messages.create.side_effect = error
        response, result = self.send()
        self.assertEqual(self.client.messages.create.call_count, 1)
        self.assertFalse(result['retryable'])
        self.assert_generation_failed(response, result)
        self.client.messages.create.reset_mock(side_effect=True)
        self.memory.save_short_memory.reset_mock()
        self.jobs.reset_mock()
        self.state.reset_mock()
        self.record_turn.reset_mock()
        self.client.messages.create.return_value = model_response(stop='refusal')
        response, result = self.send()
        self.assertEqual(self.client.messages.create.call_count, 1)
        self.assert_generation_failed(response, result)

    def test_upload_normalization_accepts_data_url_and_rejects_invalid_bytes(self):
        normalized = self.route._normalize_image({'data': 'data:image/jpeg;base64,' + PNG})
        self.assertEqual(normalized, {'data': PNG, 'media_type': 'image/png'})
        for data in ('not base64!', 'aGVsbG8=', '', 'data:image/png,abc'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.route._normalize_image({'data': data})
        response, _ = self.send(image_base64='not base64!')
        self.assertEqual(response.status_code, 400)
        self.client.messages.create.assert_not_called()
        self.memory.save_user_short_memory_once.assert_not_called()

    def test_video_frames_are_preserved_and_prompt_does_not_request_invention(self):
        self.client.messages.create.return_value = model_response(REPLY)
        self.send(images=[{'data': PNG}, {'data': PNG}], is_video=True)
        sent = self.client.messages.create.call_args.kwargs
        blocks = sent['messages'][-1]['content']
        self.assertEqual(len([block for block in blocks if block['type'] == 'image']), 2)
        self.assertNotIn('脑补中间', str(blocks))
        self.assertIn('不得编造', str(blocks))

    def test_failed_reply_does_not_persist_offline_state(self):
        self.client.messages.create.return_value = model_response(
            '<<<OFFLINE_CHARACTER_STATES>>> {"inner":"guess","intent":"wait"}')
        response, result = self.send()
        self.assert_generation_failed(response, result)

    def test_exhausted_time_budget_stops_requests(self):
        self.route.time = types.SimpleNamespace(monotonic=Mock(side_effect=[0, 46]))
        response, result = self.send()
        self.client.messages.create.assert_not_called()
        self.assert_generation_failed(response, result)


class ObserverFailureTests(unittest.TestCase):
    def setUp(self):
        self.call = Mock()
        self.observer = load_source('relationship_signals', {
            'ai_client': stub('ai_client', create_chat=self.call),
        })
        log_patch = patch('builtins.print')
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_balanced_json_parser_ignores_trailing_metadata_object(self):
        self.call.return_value = ('Result: {"signals":[]}\nmetadata: {"done":true}', {})
        result = self.observer.extract_signals('你好')
        self.assertIsNone(result['error'])
        self.assertEqual(self.call.call_count, 1)

    def test_truncation_retries_without_accepting_partial_evidence(self):
        self.call.side_effect = [('{"signals":[', {'stop_reason': 'max_tokens'}),
                                 ('{"signals":[]}', {'stop_reason': 'end_turn'})]
        result = self.observer.extract_signals('你好')
        self.assertIsNone(result['error'])
        first, second = self.call.call_args_list
        self.assertGreater(second.kwargs['max_tokens'], first.kwargs['max_tokens'])
        self.assertNotIn('relationship_model', second.kwargs['messages'][0]['content'])

    def test_empty_and_invalid_schema_responses_fail_closed(self):
        for raw, expected in [('', 'empty_response'), ('[]', 'json_parse_failed'),
                              ('{"signals":null}', 'signals_schema_invalid')]:
            with self.subTest(raw=raw):
                self.call.reset_mock()
                self.call.return_value = (raw, {})
                result = self.observer.extract_signals('你好')
                self.assertEqual(result['error'], expected)
                self.assertEqual(result['signals'], [])
                self.assertEqual(self.call.call_count, 2)

    def test_refusal_is_not_retried_or_accepted_as_evidence(self):
        self.call.return_value = ('{"signals":[]}', {'stop_reason': 'refusal'})
        result = self.observer.extract_signals('你好')
        self.assertEqual(result['error'], 'model_refused')
        self.assertEqual(self.call.call_count, 1)

    def test_failed_observer_does_not_create_cognitive_event_or_update_relationship(self):
        # Production engine imports are local/pure; replace its entry dependencies.
        with patch.dict(sys.modules, {'ai_client': stub('ai_client', create_chat=self.call),
                                     'db': stub('db', get_conn=Mock())}):
            engine = load_source('relationship_engine', {})
        with patch.object(engine, 'ensure_state_row'), \
             patch.object(engine, 'extract_signals', return_value={
                 'signals': [], 'error': 'empty_response'}), \
             patch.object(engine, '_log_interaction_stats') as stats, \
             patch.object(engine, '_route_signal') as route, \
             patch.object(engine, 'cleanup_hypotheses') as cleanup, \
             patch('cognitive_events.ingest_v4_signals') as ingress:
            result = engine.process_turn('u', 'gojo', '你好')
        self.assertEqual(result['observer_error'], 'empty_response')
        stats.assert_not_called()
        route.assert_not_called()
        cleanup.assert_not_called()
        ingress.assert_not_called()


class ClientMetadataTests(unittest.TestCase):
    def test_anthropic_metadata_preserves_stop_reason_without_thinking_text(self):
        client_module = load_client()
        client = Mock()
        client.messages.create.return_value = model_response(stop='max_tokens', thinking=True)
        with patch.object(client_module, '_get_anthropic', return_value=client):
            text, usage = client_module.create_chat('claude-test', [], max_tokens=400)
        self.assertEqual(text, '')
        self.assertEqual(usage['stop_reason'], 'max_tokens')
        self.assertEqual(usage['content_types'], ['thinking'])
        self.assertNotIn('PRIVATE_THINKING', str(usage))


if __name__ == '__main__':
    unittest.main()
