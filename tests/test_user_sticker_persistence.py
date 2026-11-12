"""REG-013: a user sticker's canonical chat_log row stays kind=sticker.

The text ingress used to persist the same client_msg_id as kind=text. Reload
then showed 「【表情】…」 with no picture. These tests lock the canonical row.
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import db_chatlog  # noqa: E402
import db_generation_receipt as receipt  # noqa: E402
import raw_events  # noqa: E402
import user_memory  # noqa: E402


REPLY = json.dumps({
    'emotion': '平静',
    'messages': [{'jp': 'やあ。', 'zh': '嘿'}],
}, ensure_ascii=False)
PLAIN_TEXT = '你好'
STICKER_TEXT = '【表情】皱眉满脸不认同'
STICKER_DESC = '皱眉满脸不认同'
STICKER_ID = 'frown_01'
UNKNOWN_ID = 'not_in_any_catalog_zz'
TEXT_EXTRA = '{"content_type": "text"}'
SPEECH = [{'jp': 'やあ。', 'zh': '嘿'}]


def _speech(messages):
    return [{'jp': item.get('jp'), 'zh': item.get('zh')} for item in messages]


def _without_event_id(messages):
    copied = []
    for item in messages:
        row = dict(item)
        row.pop('event_id', None)
        copied.append(row)
    return copied


class UserStickerPersistenceTests(unittest.TestCase):
    from tests import test_private_source_first as fixture
    setUpClass = classmethod(fixture.PrivateSourceFirstTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PrivateSourceFirstTests.tearDownClass.__func__)
    sql = fixture.PrivateSourceFirstTests.sql
    tearDown = fixture.PrivateSourceFirstTests.tearDown

    def setUp(self):
        self.fixture.PrivateSourceFirstTests.setUp(self)

    def _row(self, client_msg_id):
        rows = self.sql(
            '''SELECT role, kind, text, extra
               FROM chat_log WHERE client_msg_id=%s''',
            (client_msg_id,),
        )
        self.assertEqual(len(rows), 1, rows)
        role, kind, text, extra = rows[0]
        if not isinstance(extra, dict):
            extra = json.loads(extra or '{}')
        return role, kind, text, extra

    def _send(self, text, source_event_id, sticker=None, character_id='gojo'):
        import route_chat
        captured = {}

        def create_json(model, max_tokens, system_blocks, messages):
            captured['messages'] = messages
            return REPLY, Mock()

        payload = {
            'user_id': 'u',
            'character_id': character_id,
            'text': text,
            'source_event_id': source_event_id,
        }
        if sticker is not None:
            payload['sticker'] = sticker
        catalog = Mock(side_effect=AssertionError('user sticker must not consult the catalog'))
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
                'characters.get_character', return_value={'voice_id': 'offline'}), patch(
                'db_chat_media.get_media_for_source_event', return_value=None), patch(
                'sticker_registry.get', catalog):
            response = route_chat.chat_text(payload)
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 200, body)
        self.assertFalse(catalog.called)
        return body, captured

    def test_append_raw_event_keeps_sticker_kind(self):
        result = raw_events.append_raw_event(
            'u', 'gojo', event_id='whitelist-sticker', role='user',
            content=STICKER_TEXT, content_type='sticker',
            metadata={'sticker_id': STICKER_ID, 'sticker_desc': STICKER_DESC},
        )
        self.assertTrue(result['inserted'])
        role, kind, _text, extra = self._row('whitelist-sticker')
        self.assertEqual(role, 'user')
        self.assertEqual(kind, 'sticker')
        self.assertEqual(extra['sticker_id'], STICKER_ID)

    def test_save_user_short_memory_once_keeps_sticker_kind(self):
        inserted = user_memory.save_user_short_memory_once(
            'u', STICKER_TEXT, 'gojo', source_event_id='memory-sticker',
            event_meta={
                'kind': 'sticker',
                'sticker_id': STICKER_ID,
                'sticker_desc': STICKER_DESC,
            },
        )
        self.assertTrue(inserted)
        _role, kind, text, extra = self._row('memory-sticker')
        self.assertEqual(kind, 'sticker')
        self.assertEqual(text, STICKER_TEXT)
        self.assertEqual(extra['sticker_id'], STICKER_ID)
        self.assertEqual(extra['sticker_desc'], STICKER_DESC)

    def test_chat_text_persists_sticker_kind_and_renders_once(self):
        body, captured = self._send(
            STICKER_TEXT, 'chat-sticker',
            sticker={
                'id': STICKER_ID,
                'desc': STICKER_DESC,
                'url': 'https://signed.example/dead',
            },
        )
        role, kind, text, extra = self._row('chat-sticker')
        self.assertEqual((role, kind, text), ('user', 'sticker', STICKER_TEXT))
        self.assertEqual(extra['sticker_id'], STICKER_ID)
        self.assertEqual(extra['sticker_desc'], STICKER_DESC)
        self.assertNotIn('url', extra)
        self.assertNotIn('signed.example', json.dumps(extra, ensure_ascii=False))
        self.assertEqual(_speech(body['messages']), SPEECH)
        self.assertNotIn('sticker', body)
        self.assertNotIn(STICKER_ID, json.dumps(body['messages'], ensure_ascii=False))
        rendered = db_chatlog._history_content(role, text, '', kind, extra)
        self.assertEqual(rendered, STICKER_TEXT)
        self.assertEqual(rendered.count('【表情】'), 1)
        prompt = json.dumps(captured['messages'], ensure_ascii=False)
        self.assertIn(STICKER_TEXT, prompt)
        self.assertNotIn(STICKER_ID, prompt)

    def test_chat_text_without_sticker_keeps_the_text_row(self):
        self._send(PLAIN_TEXT, 'chat-plain')
        _role, kind, text, extra = self._row('chat-plain')
        self.assertEqual(kind, 'text')
        self.assertEqual(text, PLAIN_TEXT)
        self.assertEqual(extra, {'content_type': 'text'})
        stored = self.sql(
            "SELECT extra FROM chat_log WHERE client_msg_id='chat-plain'")[0][0]
        self.assertEqual(stored, TEXT_EXTRA)

    def test_sticker_does_not_change_generation_messages(self):
        plain_body, plain = self._send(
            STICKER_TEXT, 'gen-plain', character_id='plain-char')
        sticker_body, sticker = self._send(
            STICKER_TEXT, 'gen-sticker', character_id='sticker-char',
            sticker={'id': STICKER_ID, 'desc': STICKER_DESC},
        )
        self.assertEqual(plain['messages'], sticker['messages'])
        self.assertEqual(
            _without_event_id(plain_body['messages']),
            _without_event_id(sticker_body['messages']),
        )
        self.assertEqual(_speech(sticker_body['messages']), SPEECH)
        self.assertNotIn('sticker', sticker_body)
        self.assertNotIn(STICKER_ID, json.dumps(sticker['messages'], ensure_ascii=False))

    def test_unknown_sticker_id_is_stored(self):
        body, _captured = self._send(
            '【表情】随便', 'chat-unknown',
            sticker={'id': UNKNOWN_ID, 'desc': '随便'},
        )
        self.assertEqual(_speech(body['messages']), SPEECH)
        self.assertNotIn('error', body)
        self.assertNotIn('sticker', body)
        _role, kind, text, extra = self._row('chat-unknown')
        self.assertEqual(kind, 'sticker')
        self.assertEqual(text, '【表情】随便')
        self.assertEqual(extra['sticker_id'], UNKNOWN_ID)

    def test_user_sticker_rows_do_not_enter_assistant_cooldown(self):
        msgs = [{
            'client_msg_id': 'asst-sticker',
            'role': 'gojo',
            'text': '',
            'kind': 'sticker',
            'extra': json.dumps({'sticker_id': 'old_asst'}),
        }]
        for index in range(6):
            msgs.append({
                'client_msg_id': f'user-sticker-{index}',
                'role': 'user',
                'text': f'【表情】{index}',
                'kind': 'sticker',
                'extra': json.dumps({'sticker_id': f'user_{index}'}),
            })
        self.assertEqual(db_chatlog.append_messages('u', 'gojo', msgs), 7)
        turns = db_chatlog.recent_assistant_sticker_turns('u', 'gojo', 1)
        self.assertEqual(turns, [{'old_asst'}])
        self.assertNotIn('user_5', {item for marks in turns for item in marks})


class UserStickerFrontendTests(unittest.TestCase):
    def test_chat_text_payload_sends_sticker_id_and_retry_without_url(self):
        src = (ROOT / 'app' / 'chat' / '[id].tsx').read_text(encoding='utf-8')
        start = src.index('axios.post(`${SERVER_URL}/chat/text`')
        end = src.index('});', start)
        block = src[start:end]
        self.assertIn(
            "sticker: opts?.sticker ? { id: opts.sticker.id, desc: opts.sticker.desc || '' } : undefined",
            block,
        )
        self.assertNotIn('url', block)
        self.assertIn('sendText(text, { retry: true, sticker: pending.sticker })', src)
        self.assertIn('sendText(last.text, { retry: true, sticker: last.sticker })', src)
