"""Last-attempt Chinese prose can be translated when the operator switch is on.

No live provider calls. The switch defaults off, and these tests patch it.
"""
import json
import types
import unittest
from unittest.mock import Mock, patch

import config
import route_chat
import utils


JAPANESE = '今日はゆっくり話そう。'
CHINESE = '现在我们慢慢聊吧'
ENGLISH = 'We can talk now'
PRODUCTION_SAMPLES = (
    '冷成这样还点个没食欲的汤，这不是折磨自己吗。下次点个辣的暖暖胃啊。\n\n'
    '行，知道你不跑。离十点还有一会儿，先把汤对付喝了，别空着肚子上课。',
    '……没食欲的汤是什么鬼，那你点它干嘛。冷就该点胡椒多的那种，一口下去整个人都暖了。'
    '\n\nまあいい、好歹喝完。十点见，别迟到。',
)
MACHINE_FIELDS = (
    'schedule_action_intent', 'reminder', 'cancel_reminder',
    'pending_transaction', 'proactive_promise', 'sticker_id',
)


def _response(raw):
    return types.SimpleNamespace(
        content=[types.SimpleNamespace(type='text', text=raw)],
        stop_reason='end_turn', usage=None)


def _payloads(logged, prefix):
    return [json.loads(call.args[0].split(' ', 1)[1]) for call in logged.call_args_list
            if call.args and isinstance(call.args[0], str) and call.args[0].startswith(prefix)]


class TranslateFallbackTests(unittest.TestCase):
    def generate(self, raws, *, enabled=False, translation=JAPANESE, boom=False,
                 reject_fn=None, attempts=2):
        responses = [_response(raw) for raw in raws]
        provider = Mock(side_effect=responses)
        client = types.SimpleNamespace(messages=types.SimpleNamespace(create=provider))

        def fake_translate(text):
            fake_translate.calls.append(text)
            if boom:
                raise RuntimeError('relay down')
            return translation

        fake_translate.calls = []
        with patch.object(route_chat, 'get_anthropic', return_value=client), \
                patch.object(config, 'TRANSLATE_FALLBACK_ENABLED', enabled, create=True), \
                patch.object(route_chat, '_translate_plaintext_fallback',
                             side_effect=fake_translate) as translator, \
                patch('builtins.print') as logged:
            result, state = route_chat._generate_or_none(
                'offline', 100, [], [], attempts=attempts, log_tag='test',
                cache_tag='test', salvage=True,
                generation_trace={'source_event_id': 'test'},
                reject_fn=reject_fn)
        return {
            'result': result,
            'state': state,
            'provider': provider,
            'translator': translator,
            'calls': fake_translate.calls,
            'traces': _payloads(logged, '[generation_trace] {'),
            'summaries': _payloads(logged, '[generation_result] {'),
        }

    def test_switch_defaults_off(self):
        self.assertIs(config.TRANSLATE_FALLBACK_ENABLED, False)

    def test_enabled_translation_becomes_the_visible_reply(self):
        out = self.generate([CHINESE, CHINESE], enabled=True, translation=JAPANESE)
        self.assertIsNone(out['state'])
        self.assertEqual(out['provider'].call_count, 2)
        self.assertEqual(out['translator'].call_count, 1)
        self.assertEqual(out['calls'], [CHINESE])
        self.assertEqual(out['result']['emotion'], '平静')
        self.assertEqual(out['result']['messages'], [{'jp': JAPANESE, 'zh': CHINESE}])
        self.assertNotIn('_acceptance_mode', out['result'])
        for field in MACHINE_FIELDS:
            self.assertNotIn(field, out['result'])
        self.assertEqual(out['summaries'][0]['status'], 'translated_fallback')
        self.assertEqual(out['summaries'][0]['provider_calls'], 3)
        self.assertEqual(out['traces'][-1]['outcome'], 'accepted')
        self.assertEqual(out['traces'][-1]['acceptance_mode'], 'structured')

    def test_switch_off_returns_none_and_does_not_translate(self):
        out = self.generate([CHINESE, CHINESE], enabled=False)
        self.assertIsNone(out['result'])
        self.assertIsNone(out['state'])
        self.assertEqual(out['translator'].call_count, 0)
        self.assertEqual(out['provider'].call_count, 2)
        self.assertEqual(out['traces'][-1]['retry_reason'], 'jp_not_japanese')
        self.assertEqual(out['summaries'][0]['status'], 'generation_failed')
        self.assertEqual(out['summaries'][0]['provider_calls'], 2)

    def test_none_or_exception_from_translator_stays_a_failure(self):
        for translation, boom in ((None, False), (JAPANESE, True)):
            with self.subTest(translation=translation, boom=boom):
                out = self.generate(
                    [CHINESE, CHINESE], enabled=True, translation=translation, boom=boom)
                self.assertIsNone(out['result'])
                self.assertEqual(out['translator'].call_count, 1)
                self.assertEqual(out['summaries'][0]['status'], 'generation_failed')
                self.assertEqual(out['traces'][-1]['retry_reason'], 'jp_not_japanese')
                self.assertEqual(out['traces'][-1]['parse_invalid_reason'], 'jp_not_japanese')

    def test_non_japanese_translation_is_rejected(self):
        out = self.generate([CHINESE, CHINESE], enabled=True, translation='还是中文啊')
        self.assertIsNone(out['result'])
        self.assertEqual(out['translator'].call_count, 1)
        self.assertEqual(out['summaries'][0]['status'], 'generation_failed')
        self.assertEqual(out['traces'][-1]['retry_reason'], 'jp_not_japanese')

    def test_translation_identical_to_source_is_rejected(self):
        out = self.generate([CHINESE, CHINESE], enabled=True, translation=CHINESE)
        self.assertIsNone(out['result'])
        self.assertEqual(out['translator'].call_count, 1)
        self.assertEqual(out['summaries'][0]['status'], 'generation_failed')

    def test_first_attempt_does_not_translate_when_retry_recovers(self):
        recovered = json.dumps(
            {'emotion': '平静', 'messages': [{'jp': 'そうだね', 'zh': '是啊'}]},
            ensure_ascii=False)
        out = self.generate([CHINESE, recovered], enabled=True)
        self.assertEqual(out['translator'].call_count, 0)
        self.assertEqual(out['result']['messages'], [{'jp': 'そうだね', 'zh': '是啊'}])
        self.assertNotEqual(out['summaries'][0]['status'], 'translated_fallback')

    def test_reject_fn_can_still_drop_the_translation(self):
        seen = []

        def reject(parsed):
            seen.append(parsed)
            return 'candidate_rejected'

        out = self.generate([CHINESE, CHINESE], enabled=True, reject_fn=reject)
        self.assertIsNone(out['result'])
        self.assertEqual(out['translator'].call_count, 1)
        self.assertEqual(seen[0]['messages'], [{'jp': JAPANESE, 'zh': CHINESE}])
        for field in MACHINE_FIELDS:
            self.assertNotIn(field, seen[0])
        self.assertEqual(out['summaries'][0]['status'], 'generation_failed')
        self.assertEqual(out['traces'][-1]['outcome'], 'candidate_rejected')

    def test_english_prose_is_not_translated(self):
        out = self.generate([ENGLISH, ENGLISH], enabled=True)
        self.assertIsNone(out['result'])
        self.assertEqual(out['translator'].call_count, 0)
        self.assertEqual(out['summaries'][0]['status'], 'generation_failed')

    def test_japanese_prose_stays_on_the_plaintext_path(self):
        out = self.generate([JAPANESE, JAPANESE], enabled=True)
        self.assertEqual(out['translator'].call_count, 0)
        self.assertEqual(out['result']['messages'], [{'jp': JAPANESE, 'zh': ''}])
        self.assertEqual(out['result']['_acceptance_mode'], 'plaintext')
        self.assertEqual(out['traces'][-1]['acceptance_mode'], 'plaintext')
        self.assertEqual(out['summaries'][0]['status'], 'degraded_plaintext')

    def test_production_samples_keep_the_original_chinese_as_subtitle(self):
        for raw in PRODUCTION_SAMPLES:
            with self.subTest(raw=raw[:12]):
                self.assertFalse(utils.has_japanese_kana(raw))
                out = self.generate([raw, raw], enabled=True, translation=JAPANESE)
                self.assertEqual(out['translator'].call_count, 1)
                self.assertEqual(out['calls'], [raw])
                self.assertEqual(out['result']['messages'], [{'jp': JAPANESE, 'zh': raw}])
                self.assertNotIn('_acceptance_mode', out['result'])

    def test_helper_uses_aux_model_and_swallows_failures(self):
        with patch('ai_client.create_chat', return_value=(JAPANESE, {})) as chat:
            self.assertEqual(route_chat._translate_plaintext_fallback(CHINESE), JAPANESE)
        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs['model'], config.MODEL_JP_AUX)
        self.assertIsNone(kwargs['system'])
        self.assertEqual(kwargs['max_tokens'], 800)
        self.assertEqual(kwargs['max_retries'], 0)
        message = kwargs['messages'][0]
        self.assertEqual(message['role'], 'user')
        self.assertIn(CHINESE, message['content'])
        self.assertIn('只输出日语正文', message['content'])
        self.assertNotIn('system', message)
        with patch('ai_client.create_chat', side_effect=RuntimeError('down')):
            self.assertIsNone(route_chat._translate_plaintext_fallback(CHINESE))
        with patch('ai_client.create_chat', return_value=('还是中文啊', {})):
            self.assertIsNone(route_chat._translate_plaintext_fallback(CHINESE))
        with patch('ai_client.create_chat', return_value=(CHINESE, {})):
            self.assertIsNone(route_chat._translate_plaintext_fallback(CHINESE))
