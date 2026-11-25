"""Reply think stays off the wire, and the gojo core keeps one contact fact."""
from contextlib import redirect_stdout
import copy
import io
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import config  # noqa: E402
import prompt  # noqa: E402
import route_chat  # noqa: E402
import route_image  # noqa: E402
import route_voice_stream  # noqa: E402
from characters_data._loader import load_core  # noqa: E402
from generation_contract import (  # noqa: E402
    GENERATION_ENVELOPE_SCHEMA, degraded_reply, generation_schema, rejection_reason,
)
from tests.test_persona_material_20261116 import KEPT_TAIL  # noqa: E402
from utils import classify_reply_content, ingest_model_output  # noqa: E402


THINK_TEXT = '她其实在表白，不是在聊荞麦面'
SPEECH = [{'jp': 'そうなんだ', 'zh': '这样啊'}]
PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4z8AAAAMBAQDJ/'
    'pLvAAAAAElFTkSuQmCC'
)
ANCHOR = (
    '不要输出 <<<OFFLINE_CHARACTER_STATES>>> 或任何内部状态 JSON。那些不是当前协议，用户永远不该看到。'
)


def _reply(think=None, think_first=True):
    payload = {
        'emotion': '平静',
        'messages': [{'jp': 'そうなんだ', 'zh': '这样啊'}],
    }
    if think is None:
        return json.dumps(payload, ensure_ascii=False)
    if think_first:
        ordered = {'think': think, **payload}
    else:
        ordered = {**payload, 'think': think}
    return json.dumps(ordered, ensure_ascii=False)


def _render_before(emotion_list):
    """The pre-change render_output_spec, kept here as the switch-off oracle."""
    catalog = ''
    try:
        import sticker_registry
        if sticker_registry.is_enabled():
            catalog = sticker_registry.catalog_for_prompt() or ''
    except Exception as exc:
        print(f'[prompt] sticker catalog skipped:{type(exc).__name__}')
        catalog = ''
    text = prompt.OUTPUT_SPEC.format(emotion_list=emotion_list, sticker_catalog=catalog)
    if not catalog:
        text = prompt._strip_sticker_section(text)
    return text


def _provider_response(text):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type='text', text=text)],
        stop_reason='end_turn',
        usage=None,
    )


class ReplyThinkSwitchTests(unittest.TestCase):
    def test_switch_off_render_matches_the_previous_spec(self):
        with patch.object(config, 'REPLY_THINK_ENABLED', False):
            rendered = prompt.render_output_spec('平静')
        self.assertEqual(rendered, _render_before('平静'))
        self.assertNotIn('think', rendered)
        self.assertNotIn(prompt.THINK_SPEC, rendered)

    def test_switch_off_schema_is_the_original_object(self):
        with patch.object(config, 'REPLY_THINK_ENABLED', False):
            self.assertIs(generation_schema(), GENERATION_ENVELOPE_SCHEMA)

    def test_switch_on_inserts_think_spec_on_the_next_line(self):
        with patch.object(config, 'REPLY_THINK_ENABLED', True):
            rendered = prompt.render_output_spec('平静')
        self.assertNotIn('{{', rendered)
        self.assertIn(prompt.THINK_SPEC, rendered)
        lines = rendered.splitlines()
        anchor_at = lines.index(ANCHOR)
        spec_lines = prompt.THINK_SPEC.splitlines()
        self.assertEqual(lines[anchor_at + 1:anchor_at + 1 + len(spec_lines)], spec_lines)

    def test_switch_on_schema_requires_think_and_leaves_the_original_alone(self):
        before = copy.deepcopy(GENERATION_ENVELOPE_SCHEMA)
        with patch.object(config, 'REPLY_THINK_ENABLED', True):
            schema = generation_schema()
        self.assertEqual(next(iter(schema['properties'])), 'think')
        self.assertEqual(schema['properties']['think'], {'type': 'string'})
        self.assertEqual(schema['required'], ['think', 'emotion', 'messages'])
        self.assertIs(schema['additionalProperties'], False)
        self.assertIsNot(schema, GENERATION_ENVELOPE_SCHEMA)
        self.assertIsNot(
            schema['properties']['emotion'],
            GENERATION_ENVELOPE_SCHEMA['properties']['emotion'])
        schema['properties']['emotion']['type'] = 'number'
        self.assertEqual(GENERATION_ENVELOPE_SCHEMA, before)
        schema['properties']['emotion']['type'] = 'string'

    def test_missing_anchor_logs_and_does_not_insert(self):
        captured = io.StringIO()
        with patch.object(config, 'REPLY_THINK_ENABLED', True), \
                patch.object(prompt, 'OUTPUT_SPEC', 'head {emotion_list} tail {sticker_catalog}'), \
                redirect_stdout(captured):
            rendered = prompt.render_output_spec('平静')
        self.assertIn('[prompt] think spec anchor missing', captured.getvalue())
        self.assertNotIn(prompt.THINK_SPEC, rendered)
        self.assertIn('head 平静', rendered)


class ReplyThinkIngestTests(unittest.TestCase):
    def test_think_before_messages_is_removed_and_logged(self):
        raw = _reply(THINK_TEXT, think_first=True)
        captured = io.StringIO()
        with redirect_stdout(captured):
            _visible, parsed, _state = ingest_model_output(raw)
        self.assertNotIn('think', parsed)
        self.assertEqual(parsed['messages'], SPEECH)
        self.assertEqual(parsed['emotion'], '平静')
        self.assertIn('[think] first=true', captured.getvalue())
        self.assertIn(f'chars={len(THINK_TEXT)}', captured.getvalue())
        self.assertIn(f'text={THINK_TEXT}', captured.getvalue())

    def test_think_after_messages_logs_first_false(self):
        raw = _reply(THINK_TEXT, think_first=False)
        captured = io.StringIO()
        with redirect_stdout(captured):
            _visible, parsed, _state = ingest_model_output(raw)
        self.assertNotIn('think', parsed)
        self.assertEqual(parsed['messages'], SPEECH)
        self.assertIn('[think] first=false', captured.getvalue())

    def test_think_without_messages_is_missing_messages(self):
        raw = '{"think":"只有想法","emotion":"平静"}'
        _visible, parsed, _state = ingest_model_output(raw)
        self.assertEqual(rejection_reason(parsed, raw), 'missing_messages')

    def test_broken_json_with_think_is_not_spoken(self):
        raw = '{"think":"她在试探荞麦面","emotion":"平静","messages":[{"jp":"うん","zh":"嗯"}]'
        self.assertEqual(classify_reply_content(raw), 'invalid')
        self.assertIsNone(degraded_reply(raw))


class ReplyThinkBudgetTests(unittest.TestCase):
    def test_headroom_follows_the_switch(self):
        model = 'claude-reply-think-budget'
        route_chat._schema_unavailable_models.discard(model)
        cases = (
            (False, 400, _reply()),
            (True, 400 + route_chat.THINK_TOKEN_HEADROOM, _reply(THINK_TEXT)),
        )
        try:
            for enabled, expected, raw in cases:
                with self.subTest(enabled=enabled):
                    with patch.object(config, 'REPLY_THINK_ENABLED', enabled), \
                            patch.object(route_chat, 'get_anthropic') as factory, \
                            patch('builtins.print'):
                        provider = factory.return_value.messages.create
                        provider.return_value = _provider_response(raw)
                        result, _state = route_chat._generate_or_none(
                            model, 400, [], [], attempts=1,
                            log_tag='test', cache_tag='test')
                    self.assertEqual(provider.call_args.kwargs['max_tokens'], expected)
                    self.assertEqual(result['messages'], SPEECH)
                    self.assertNotIn('think', result)
                    route_chat._schema_unavailable_models.discard(model)
        finally:
            route_chat._schema_unavailable_models.discard(model)


class ReplyThinkDeliveryTests(unittest.TestCase):
    from tests import test_private_source_first as fixture
    setUpClass = classmethod(fixture.PrivateSourceFirstTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PrivateSourceFirstTests.tearDownClass.__func__)
    sql = fixture.PrivateSourceFirstTests.sql
    tearDown = fixture.PrivateSourceFirstTests.tearDown

    def setUp(self):
        self.fixture.PrivateSourceFirstTests.setUp(self)

    def _assistant_rows(self):
        # chat_log stores the assistant speaker as the character id, not "assistant".
        return self.sql("SELECT role, text FROM chat_log WHERE role <> 'user'")

    def _flush_assistant_effect(self, source_event_id, endpoint):
        """The request skips assistant_short_memory; the worker writes that row."""
        import db_generation_receipt as receipt
        from generation_side_effect_worker import process_one_side_effect
        effects = receipt.list_side_effects('u', 'gojo', source_event_id, endpoint)
        assistant = next(row for row in effects if row['effect'] == 'assistant_short_memory')
        stored = json.dumps(assistant, ensure_ascii=False, default=str)
        self.assertNotIn(THINK_TEXT, stored)
        self.assertTrue(process_one_side_effect(assistant))

    def _assert_think_stays_private(self, body, source_event_id, endpoint):
        dumped = json.dumps(body, ensure_ascii=False)
        self.assertNotIn(THINK_TEXT, dumped)
        self.assertNotIn('"think"', dumped)
        self._flush_assistant_effect(source_event_id, endpoint)
        rows = self._assistant_rows()
        self.assertGreaterEqual(len(rows), 1, rows)
        for role, text in rows:
            self.assertEqual(role, 'gojo')
            self.assertNotIn(THINK_TEXT, text or '')
            self.assertIn('そうなんだ', text or '')

    def test_chat_text_does_not_return_or_store_think(self):
        import db_generation_receipt as receipt
        import user_memory

        raw = _reply(THINK_TEXT)

        def create_json(*_args, **_kwargs):
            return raw, Mock()

        payload = {
            'user_id': 'u',
            'character_id': 'gojo',
            'text': '我的荞麦面永远吃不完',
            'source_event_id': 'think-text-1',
        }
        with patch.object(
                route_chat, 'save_user_short_memory_once',
                user_memory.save_user_short_memory_once), patch.object(
                route_chat, 'get_character', return_value={'voice_id': 'offline'}), patch.object(
                route_chat, 'get_temporal_snapshot', return_value={}), patch(
                'reply_availability.check_reply_availability',
                return_value={'can_reply': True}), patch.object(
                route_chat, 'update_chat_days', return_value=1), patch.object(
                route_chat, 'get_short_memory', return_value=[]), patch.object(
                route_chat, '_turn_context', return_value=(None, [])), patch.object(
                route_chat, 'build_system_blocks', return_value=[]), patch.object(
                route_chat, 'log_cache_usage'), patch.object(
                route_chat, '_create_json', side_effect=create_json), patch.object(
                route_chat, '_provider_create', side_effect=AssertionError('model')), patch.object(
                route_chat, '_commit_offline_state'), patch.object(
                route_chat, '_reject_schedule_candidate', return_value=None), patch.object(
                route_chat, '_commit_schedule_candidate',
                return_value={'ok': True, 'noop': True}), patch.object(
                receipt, 'GenerationHeartbeat'), patch(
                'tts.tts_to_b64', return_value=''), patch(
                'characters.get_character', return_value={'voice_id': 'offline'}):
            response = route_chat.chat_text(payload)
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(len(body['messages']), 1)
        self.assertTrue(body['messages'][0]['jp'].startswith('そうなんだ'))
        self.assertEqual(body['messages'][0]['zh'], '这样啊')
        self._assert_think_stays_private(body, 'think-text-1', 'chat_text')

    def test_image_reply_does_not_return_or_store_think(self):
        import db_generation_receipt as receipt

        raw = _reply(THINK_TEXT)
        client = Mock()
        client.messages.create.return_value = _provider_response(raw)
        payload = {
            'user_id': 'u',
            'character_id': 'gojo',
            'text': '看这个',
            'image_base64': PNG,
            'source_event_id': 'think-image-1',
        }
        with patch.object(route_image, 'get_anthropic', return_value=client), patch.object(
                route_image, '_persist_chat_image',
                return_value={'id': 'media-1', 'url': 'https://example/m'}), patch.object(
                route_image, 'get_character', return_value={'voice_id': 'offline'}), patch.object(
                route_image, 'get_temporal_snapshot', return_value={}), patch(
                'reply_availability.check_reply_availability',
                return_value={'can_reply': True, 'seen': True}), patch(
                'db_read_receipt.mark_immediate_seen', return_value=None), patch.object(
                route_image, 'update_chat_days', return_value=1), patch.object(
                route_image, '_turn_context', return_value=(None, [])), patch.object(
                route_image, 'build_system_blocks', return_value=[]), patch.object(
                route_image, 'log_cache_usage'), patch(
                'schedule_transition.validate_generated_schedule_reply',
                return_value=(None, None)), patch(
                'schedule_transition.commit_generated_schedule_intent',
                return_value={'ok': True, 'noop': True}), patch.object(
                receipt, 'GenerationHeartbeat'), patch(
                'tts.tts_to_b64', return_value=''):
            response = route_image.chat_image(payload)
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200, body)
        self.assertEqual(len(body['messages']), 1)
        self.assertTrue(body['messages'][0]['jp'].startswith('そうなんだ'))
        self.assertEqual(body['messages'][0]['zh'], '这样啊')
        self._assert_think_stays_private(body, 'think-image-1', 'chat_image')


class ReplyThinkVoiceLineTests(unittest.TestCase):
    def test_voice_line_parser_drops_think_lines(self):
        self.assertIsNone(route_voice_stream._parse_line('THINK: 她在试探'))
        self.assertIsNone(route_voice_stream._parse_line('think: …'))


class ReplyThinkPersonaTests(unittest.TestCase):
    def test_core_prompt_states_how_they_met_as_fact(self):
        core = load_core('gojo')['core_prompt']
        profile_end = '这些是关于你的事实，不是每次都要提的话题。别人没问到就不用主动说。'
        self.assertIn('【你和她——事实】', core)
        self.assertLess(core.index(profile_end), core.index('【你和她——事实】'))
        self.assertLess(core.index('【你和她——事实】'), core.index('【语言风格】'))
        self.assertIn('你是意外有了这个联系渠道，才和她联系上的。', core)
        self.assertIn('她不是你那个世界里的人：咒术高专、咒术界、你认识的人里都没有她。你们原本互不认识。', core)
        self.assertIn('这些是事实。你怎么看这件事，由你自己决定。', core)
        self.assertIn('【身份认知', core[:300])
        self.assertTrue(core.endswith(KEPT_TAIL))
