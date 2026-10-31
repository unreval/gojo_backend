"""Image+caption canonical text matches the client bubble.

The client writes the bare caption (or the no-caption placeholder) into
chat_log first. The image route must persist that same string. A real
mismatch still fails closed. Image identity stays on kind / summary markers,
not on a camera emoji in the stored text.
"""
import asyncio
import importlib.util
import json
import os
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

import db_chatlog  # noqa: E402
import raw_events  # noqa: E402
import user_memory  # noqa: E402
from tests.receipt_passthrough import passthrough_generation_receipt  # noqa: E402


CAPTION = '给你看我们学校开的🪷'
VIDEO_CAPTION = '当当'
PNG = (
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8'
    'z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


def _stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


class ImageMarkerTests(unittest.TestCase):
    def test_history_content_marks_a_captioned_image_without_the_camera_emoji(self):
        summary = '校园池塘里开着荷花'
        with_summary = db_chatlog._history_content('user', CAPTION, '', 'image', {
            'visual_summary': summary,
            'event_meta': {'kind': 'image'},
        })
        self.assertTrue(with_summary.startswith('【图片摘要】'))
        self.assertIn(summary, with_summary)
        self.assertNotIn('📷', with_summary.split('\n', 1)[0])

        bare = db_chatlog._history_content('user', CAPTION, '', 'image', {
            'event_meta': {'kind': 'image'},
        })
        self.assertTrue(bare.startswith('【图片】'))
        self.assertIn(CAPTION, bare)

        video = db_chatlog._history_content('user', VIDEO_CAPTION, '', 'video', {})
        self.assertTrue(video.startswith('【视频】'))
        video_summary = db_chatlog._history_content('user', VIDEO_CAPTION, '', 'video', {
            'visual_summary': '一段挥手',
        })
        self.assertTrue(video_summary.startswith('【视频摘要】'))

    def test_assemble_prompt_content_keeps_the_image_summary_marker(self):
        summary = '校园池塘里开着荷花'
        assembled = user_memory.assemble_prompt_content(CAPTION, {
            'kind': 'image',
            'visual_summary': summary,
        })
        self.assertIn(CAPTION, assembled)
        self.assertIn('【图片摘要】' + summary, assembled)

        video = user_memory.assemble_prompt_content(VIDEO_CAPTION, {
            'kind': 'video',
            'visual_summary': '一段挥手',
        })
        self.assertIn('【视频摘要】一段挥手', video)

        # No summary: the stored caption is returned as written. The 【图片】
        # prefix is produced by _history_content from kind, not by this helper.
        self.assertEqual(
            user_memory.assemble_prompt_content(CAPTION, {'kind': 'image'}),
            CAPTION,
        )

    def test_prompt_names_the_history_markers_and_keeps_the_seen_rule(self):
        text = (BACKEND / 'prompt.py').read_text(encoding='utf-8')
        self.assertNotIn('上下文里的"📷"标记', text)
        self.assertIn(
            '上下文里的"【图片】"、"【图片摘要】"、"【视频】"或"【视频摘要】"'
            '标记表示她当时发过一张图，紧跟在它后面的你的回复，就是你【当时亲眼看过那张图】之后说的话。',
            text,
        )
        self.assertIn('绝不允许说"我看不到图片"，你看过。', text)
        self.assertIn('只有当她发来全新图片而你的上下文里确实没有时，才可以说没收到。', text)


class ImageCaptionStoreTests(unittest.TestCase):
    from tests import test_private_source_first as fixture
    setUpClass = classmethod(fixture.PrivateSourceFirstTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.PrivateSourceFirstTests.tearDownClass.__func__)
    sql = fixture.PrivateSourceFirstTests.sql
    tearDown = fixture.PrivateSourceFirstTests.tearDown

    def setUp(self):
        self.fixture.PrivateSourceFirstTests.setUp(self)

    def _prewrite(self, client_msg_id, text, kind):
        written = db_chatlog.append_messages('u', 'gojo', [{
            'client_msg_id': client_msg_id,
            'role': 'user',
            'text': text,
            'kind': kind,
        }])
        self.assertEqual(written, 1)

    def _short_content(self, source_event_id):
        return self.sql(
            'SELECT content FROM short_memory WHERE source_event_id=%s',
            (source_event_id,),
        )

    def test_frontend_caption_then_matching_save_does_not_raise(self):
        self._prewrite('test_caption_1', CAPTION, 'image')
        inserted = user_memory.save_user_short_memory_once(
            'u', CAPTION, 'gojo', source_event_id='test_caption_1',
            event_meta={'kind': 'image', 'source_event_id': 'test_caption_1'},
        )
        self.assertTrue(inserted)
        self.assertEqual(self._short_content('test_caption_1'), [(CAPTION,)])
        stored = self.sql(
            "SELECT text FROM chat_log WHERE client_msg_id='test_caption_1'")
        self.assertEqual(stored, [(CAPTION,)])

    def test_video_caption_is_stored_without_the_clapper_prefix(self):
        self._prewrite('test_caption_video', VIDEO_CAPTION, 'video')
        inserted = user_memory.save_user_short_memory_once(
            'u', VIDEO_CAPTION, 'gojo', source_event_id='test_caption_video',
            event_meta={'kind': 'video', 'source_event_id': 'test_caption_video'},
        )
        self.assertTrue(inserted)
        self.assertEqual(self._short_content('test_caption_video'), [(VIDEO_CAPTION,)])
        self.assertNotIn('🎬', VIDEO_CAPTION)
        stored = self.sql(
            "SELECT text, kind FROM chat_log WHERE client_msg_id='test_caption_video'")
        self.assertEqual(stored, [(VIDEO_CAPTION, 'video')])

    def test_image_without_caption_still_uses_the_placeholder(self):
        placeholder = '📷 [图片]'
        self._prewrite('test_caption_none', placeholder, 'image')
        inserted = user_memory.save_user_short_memory_once(
            'u', placeholder, 'gojo', source_event_id='test_caption_none',
            event_meta={'kind': 'image', 'source_event_id': 'test_caption_none'},
        )
        self.assertTrue(inserted)
        self.assertEqual(self._short_content('test_caption_none'), [(placeholder,)])

    def test_plain_text_save_stores_the_text_unchanged(self):
        inserted = user_memory.save_user_short_memory_once(
            'u', '你好', 'gojo', source_event_id='evt-text-plain')
        self.assertTrue(inserted)
        stored = self.sql(
            "SELECT text, kind FROM chat_log WHERE client_msg_id='evt-text-plain'")
        self.assertEqual(stored, [('你好', 'text')])
        again = user_memory.save_user_short_memory_once(
            'u', '你好', 'gojo', source_event_id='evt-text-plain')
        self.assertFalse(again)
        self.assertEqual(self.sql('SELECT count(*) FROM chat_log')[0][0], 1)

    def test_real_content_mismatch_is_still_rejected(self):
        self._prewrite('mismatch-1', '她说了A', 'text')
        with self.assertRaisesRegex(
                raw_events.SourceValidityError,
                'canonical_source_unavailable_or_mismatched'):
            user_memory.save_user_short_memory_once(
                'u', '她说了B', 'gojo', source_event_id='mismatch-1')
        self.assertEqual(self.sql('SELECT count(*) FROM short_memory')[0][0], 0)
        stored = self.sql(
            "SELECT text FROM chat_log WHERE client_msg_id='mismatch-1'")
        self.assertEqual(stored, [('她说了A',)])

    def test_prefixed_caption_is_still_a_mismatch(self):
        self._prewrite('test_caption_prefix', CAPTION, 'image')
        with self.assertRaises(raw_events.SourceValidityError):
            user_memory.save_user_short_memory_once(
                'u', '📷 ' + CAPTION, 'gojo', source_event_id='test_caption_prefix',
                event_meta={'kind': 'image'})
        self.assertEqual(self.sql('SELECT count(*) FROM short_memory')[0][0], 0)


class ImageRouteCanonicalTextTests(unittest.TestCase):
    def setUp(self):
        self.save_user = Mock(return_value=True)
        self.pending = {}
        self.client = Mock()
        self.client.messages.create.return_value = types.SimpleNamespace(
            content=[], stop_reason='end_turn',
            usage=types.SimpleNamespace(input_tokens=1, output_tokens=1),
            id='img-canonical',
        )
        router = Mock()
        router.post.side_effect = lambda *_args, **_kwargs: lambda function: function

        def check_reply(character_id, user_id, **kwargs):
            self.pending['text'] = kwargs.get('pending_text')
            self.pending['event_meta'] = kwargs.get('event_meta')
            return self.availability

        modules = {
            'fastapi': _stub('fastapi', APIRouter=Mock(return_value=router)),
            'fastapi.responses': _stub(
                'fastapi.responses',
                JSONResponse=lambda content, status_code=200: types.SimpleNamespace(
                    body=json.dumps(content, ensure_ascii=False, default=str).encode(),
                    status_code=status_code,
                    data=content,
                ),
            ),
            'db': _stub('db', get_conn=Mock(side_effect=AssertionError('unexpected DB'))),
            'ai_client': _stub(
                'ai_client',
                get_anthropic=Mock(return_value=self.client),
                extract_text=lambda response, sep='': '',
                response_metadata=lambda response: {
                    'stop_reason': getattr(response, 'stop_reason', 'end_turn'),
                },
            ),
            'tts': _stub('tts', tts_to_b64=Mock(return_value='')),
            'prompt': _stub(
                'prompt',
                build_system_blocks=Mock(return_value=[{'type': 'text', 'text': 's'}]),
                log_cache_usage=Mock(),
            ),
            'user_memory': _stub(
                'user_memory',
                save_short_memory=Mock(),
                save_user_short_memory_once=self.save_user,
                get_short_memory=Mock(return_value=[]),
                get_short_memory_for_prompt=Mock(return_value=[]),
                attach_short_memory_event_meta=Mock(return_value=True),
                update_chat_days=Mock(return_value=3),
            ),
            'memory_jobs': _stub('memory_jobs', enqueue_private_extraction=Mock()),
            'temporal_awareness': _stub(
                'temporal_awareness',
                get_temporal_snapshot=Mock(return_value={}),
                record_turn=Mock(),
                record_user_message=Mock(),
            ),
            'characters': _stub(
                'characters',
                get_character=Mock(return_value={'id': 'gojo', 'voice_id': None}),
            ),
            'db_chat_media': _stub(
                'db_chat_media',
                persist_image=Mock(return_value={
                    'id': 'media-1',
                    'media_kind': 'image',
                    'object_key': 'chat-media/u/gojo/evt/original.png',
                    'mime_type': 'image/png',
                }),
                public_media=lambda record: None if not record else {
                    'id': record['id'],
                    'kind': 'image',
                    'url': 'https://r2.example/signed',
                    'mime_type': record.get('mime_type') or 'image/png',
                },
            ),
            'media_storage': _stub(
                'media_storage',
                is_configured=Mock(return_value=True),
                signed_get_url=Mock(return_value='https://r2.example/signed'),
                MediaStorageError=RuntimeError,
            ),
            'db_generation_receipt': passthrough_generation_receipt(),
            'reply_availability': _stub(
                'reply_availability', check_reply_availability=check_reply),
            'db_schedule': _stub(
                'db_schedule', merge_phone_check_event_meta=Mock(return_value=True)),
            'db_read_receipt': _stub(
                'db_read_receipt', mark_immediate_seen=Mock()),
            'context_layer': _stub(
                'context_layer',
                build_chat_context=Mock(return_value=types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')),
                assemble_fallback_from_messages=Mock(return_value=types.SimpleNamespace(
                    messages=[], failed_closed=False, memory_text='')),
                append_current_user_turn=lambda msgs, content: list(msgs or []) + [
                    {'role': 'user', 'content': content}],
            ),
        }
        self.availability = {'can_reply': True, 'seen': True}
        self.module_patch = patch.dict(sys.modules, modules)
        self.module_patch.start()
        self.addCleanup(self.module_patch.stop)
        spec = importlib.util.spec_from_file_location(
            'route_image_caption_under_test', BACKEND / 'route_image.py')
        self.route = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.route)
        log_patch = patch('builtins.print')
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _send(self, **extra):
        data = {
            'user_id': 'u',
            'character_id': 'gojo',
            'image_base64': PNG,
            'media_type': 'image/png',
            'source_event_id': 'img-canonical-1',
        }
        data.update(extra)
        response = asyncio.run(self.route.chat_image(data))
        return response, json.loads(response.body)

    def _saved_text(self):
        self.save_user.assert_called_once()
        args, kwargs = self.save_user.call_args
        return args[1], kwargs.get('event_meta') or {}

    def test_free_image_with_caption_persists_the_bare_caption(self):
        self._send(text=CAPTION)
        text, meta = self._saved_text()
        self.assertEqual(text, CAPTION)
        self.assertNotEqual(text, '📷 ' + CAPTION)
        self.assertEqual(meta.get('caption'), '📷 ' + CAPTION)
        self.assertEqual(self.pending['text'], '📷 ' + CAPTION)

    def test_busy_seen_image_persists_the_bare_caption(self):
        self.availability = {
            'can_reply': False,
            'seen': True,
            'reply_state': 'soft_busy',
            'activity': {'title': '上课', 'location': '学校', 'end_time': '12:00'},
            'free_at': '12:00',
            'opportunity_id': 4,
            'seen_at': None,
            'next_phone_check_at': None,
            'pending_count': 1,
        }
        _response, body = self._send(text=CAPTION, source_event_id='img-busy-caption')
        self.assertTrue(body['busy'])
        text, meta = self._saved_text()
        self.assertEqual(text, CAPTION)
        self.assertEqual(meta.get('caption'), '📷 ' + CAPTION)
        self.assertEqual(self.pending['text'], '📷 ' + CAPTION)

    def test_free_video_caption_has_no_clapper_prefix(self):
        self._send(
            text=VIDEO_CAPTION,
            is_video=True,
            images=[{'data': PNG, 'media_type': 'image/png'}],
            source_event_id='vid-canonical-1',
        )
        text, meta = self._saved_text()
        self.assertEqual(text, VIDEO_CAPTION)
        self.assertFalse(text.startswith('🎬'))
        self.assertEqual(meta.get('caption'), '🎬 ' + VIDEO_CAPTION)
        self.assertEqual(meta.get('kind'), 'video')

    def test_image_without_caption_persists_the_picture_placeholder(self):
        self._send(text='', source_event_id='img-no-caption')
        text, meta = self._saved_text()
        self.assertEqual(text, '📷 [图片]')
        self.assertEqual(meta.get('caption'), '📷 [图片]')
        self.assertEqual(self.pending['text'], '📷 [图片]')

    def test_video_without_caption_persists_the_video_placeholder(self):
        self._send(
            text='   ',
            is_video=True,
            images=[{'data': PNG, 'media_type': 'image/png'}],
            source_event_id='vid-no-caption',
        )
        text, _meta = self._saved_text()
        self.assertEqual(text, '🎬 [视频]')

    def test_unhandled_exception_fails_the_receipt_and_returns_502(self):
        self.save_user.side_effect = RuntimeError('boom')
        response, body = self._send(text=CAPTION, source_event_id='img-unhandled')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body['error'], 'generation_failed')
        self.assertTrue(body['generation_failed'])
        self.assertEqual(body['messages'], [])
        self.assertEqual(body['source_event_id'], 'img-unhandled')
        self.assertEqual(body['total_days'], 3)
        self.assertTrue(body['retryable'])
        self.assertIn('media', body)
        self.client.messages.create.assert_not_called()
        receipt = sys.modules['db_generation_receipt']
        receipt.fail_generation.assert_called()
        self.assertEqual(
            receipt.fail_generation.call_args.kwargs['last_error'],
            'unhandled:RuntimeError',
        )

    def test_empty_model_output_still_fails_as_generation_failed(self):
        response, body = self._send(text=CAPTION, source_event_id='img-empty-model')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(body['error'], 'generation_failed')
        self.assertIn('media', body)
        receipt = sys.modules['db_generation_receipt']
        errors = [
            call.kwargs.get('last_error')
            for call in receipt.fail_generation.call_args_list
        ]
        self.assertEqual(errors, ['generation_failed'])
