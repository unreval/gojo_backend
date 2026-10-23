"""Generation translation diagnostics must not veto visible replies by default."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch
import json
import unittest

import config
import route_chat
import utils


CASES = (
    ('negation_polarity_mismatch', '今日は全部うまくできたよ。', '今天这些事情没有做好。'),
    ('number_value_mismatch', 'りんごを三つ買ったよ。', '我买了五个苹果。'),
    ('time_word_contradiction', '朝に散歩しよう。', '我们晚上去散步吧。'),
    ('bilingual_numeric_anchor_mismatch', '午前十一時に帰るよ。', '23:00回家就好了。'),
    ('zh_contains_kana', 'そうだね。', 'ねえ、悟って呼んで。'),
)
SNAPSHOT = {'now_utc': datetime(2026, 10, 23, 6, tzinfo=timezone.utc)}


class TranslationPolicyTests(unittest.TestCase):
    def generate(self, messages, blocking=False):
        raw = json.dumps({'emotion': '平静', 'messages': messages}, ensure_ascii=False)
        with patch.object(config, 'TRANSLATION_CHECKS_BLOCKING', blocking, create=True), \
                patch.object(route_chat, '_create_json', return_value=(
                    raw, SimpleNamespace(stop_reason='end_turn', usage=None))) as provider, \
                patch('builtins.print') as logged:
            result, _ = route_chat._generate_or_none(
                'offline', 100, [], [], attempts=2, log_tag='test', cache_tag='test',
                reject_fn=lambda parsed: route_chat._reject_temporal_candidate(parsed, SNAPSHOT))
            finalized = route_chat._finalize_committed(result)
        return result, finalized, provider.call_count, logged

    def test_each_translation_issue_accepts_first_candidate_and_final_commit(self):
        for reason, jp, zh in CASES:
            with self.subTest(reason=reason):
                result, (_, messages), calls, _ = self.generate([{'jp': jp, 'zh': zh}])
                self.assertIsNotNone(result)
                self.assertEqual(messages, [{'jp': jp, 'zh': zh}])
                self.assertEqual(calls, 1)

    def test_blocking_switch_restores_each_existing_rejection_reason(self):
        for reason, jp, zh in CASES:
            with self.subTest(reason=reason), patch.object(
                    config, 'TRANSLATION_CHECKS_BLOCKING', True, create=True):
                candidate = {'messages': [{'jp': jp, 'zh': zh}]}
                code = (route_chat._protocol_rejection_reason(candidate)
                        or route_chat._reject_temporal_candidate(candidate, SNAPSHOT))
                self.assertEqual(code, reason)
                if reason == 'bilingual_numeric_anchor_mismatch':
                    self.assertEqual(route_chat._reject_temporal_candidate(candidate, SNAPSHOT), reason)
                result, _, _, _ = self.generate(candidate['messages'], blocking=True)
                self.assertIsNone(result)

    def test_warning_has_reason_bubble_and_short_markers_without_candidate_text(self):
        secret = 'PRIVATE-CANDIDATE-TEXT-DO-NOT-LOG'
        messages = [{'jp': '今日はゆっくり話そう。', 'zh': '今天我们慢慢聊吧。'},
                    {'jp': 'あと三分間だよ。' + secret, 'zh': '还有五分钟就好了。' + secret}]
        _, _, _, logged = self.generate(messages)
        warnings = [json.loads(call.args[0].split(' ', 1)[1])
                    for call in logged.call_args_list
                    if call.args and str(call.args[0]).startswith('[translation_check] ')]
        self.assertTrue(warnings)
        self.assertIn('bilingual_numeric_anchor_mismatch', {row['reason'] for row in warnings})
        for row in warnings:
            self.assertEqual(row['message_index'], 2)
            self.assertFalse(row['blocking'])
            self.assertTrue(row['markers'])
            self.assertTrue(all(len(marker) <= 8 for marker in row['markers']))
            self.assertEqual(set(row), {'reason', 'message_index', 'markers', 'blocking'})
            self.assertNotIn(secret, json.dumps(row))

    def test_numeric_warning_does_not_skip_later_prompt_marker_hard_rejection(self):
        candidate = {'messages': [
            {'jp': 'あと三分間だよ。', 'zh': '还有五分钟就好了。'},
            {'jp': '【历史消息；发送时间未知】秘密', 'zh': '历史资料'}]}
        for blocking in (False, True):
            with self.subTest(blocking=blocking), patch.object(
                    config, 'TRANSLATION_CHECKS_BLOCKING', blocking, create=True):
                self.assertEqual(route_chat._reject_temporal_candidate(candidate, SNAPSHOT),
                                 'bilingual_numeric_anchor_mismatch' if blocking
                                 else 'temporal_prompt_marker_echo')

    def test_hard_protocol_failure_in_later_bubble_is_never_masked(self):
        cases = ((None, 'malformed_json'), ({}, 'missing_messages'),
                 ({'messages': [{'jp': 'こんにちは'}]}, 'invalid_message_field'),
                 ({'messages': [{'jp': 'こんにちは', 'zh': 'こんにちは'}]}, 'jp_equals_zh_verbal'))
        for blocking in (False, True):
            for candidate, expected in cases:
                with self.subTest(blocking=blocking, reason=expected), patch.object(
                        config, 'TRANSLATION_CHECKS_BLOCKING', blocking, create=True):
                    self.assertEqual(route_chat._protocol_rejection_reason(candidate), expected)
            candidate = {'messages': [{'jp': CASES[0][1], 'zh': CASES[0][2]},
                                      {'jp': 'こんにちは'}]}
            if not blocking:
                with patch.object(config, 'TRANSLATION_CHECKS_BLOCKING', False, create=True):
                    self.assertEqual(route_chat._protocol_rejection_reason(candidate), 'invalid_message_field')

    def test_kana_heavy_zh_is_logged_and_committed_unless_blocking(self):
        secret = 'PRIVATE-CANDIDATE-TEXT-DO-NOT-LOG'
        samples = (
            ('そうだね。', 'ねえ、悟って呼んで。' + secret),
            ('そうだね', '「おはよう」'),
        )
        for jp, zh in samples:
            with self.subTest(zh=zh):
                result, (_, messages), calls, logged = self.generate([{'jp': jp, 'zh': zh}])
                self.assertIsNotNone(result)
                self.assertEqual(calls, 1)
                self.assertEqual(messages[0]['zh'], zh)
                self.assertTrue(utils.commit_ready_msgs(messages))
                warnings = [json.loads(call.args[0].split(' ', 1)[1])
                            for call in logged.call_args_list
                            if call.args and str(call.args[0]).startswith('[translation_check] ')]
                self.assertGreaterEqual(len(warnings), 1)
                self.assertTrue(all(row['reason'] == 'zh_contains_kana' for row in warnings))
                for row in warnings:
                    self.assertEqual(row['message_index'], 1)
                    self.assertFalse(row['blocking'])
                    self.assertTrue(row['markers'])
                    self.assertTrue(all(len(marker) <= 8 for marker in row['markers']))
                    self.assertEqual(set(row), {'reason', 'message_index', 'markers', 'blocking'})
                encoded = json.dumps(warnings, ensure_ascii=False)
                self.assertNotIn(secret, encoded)
                self.assertNotIn(zh, encoded)
        for jp, zh in samples:
            with self.subTest(blocking=True, zh=zh), patch.object(
                    config, 'TRANSLATION_CHECKS_BLOCKING', True, create=True):
                self.assertEqual(utils.reply_message_rejection_reason({'jp': jp, 'zh': zh}),
                                 'zh_contains_kana')
                self.assertFalse(utils.commit_ready_msgs([{'jp': jp, 'zh': zh}]))
                self.assertIsNone(self.generate([{'jp': jp, 'zh': zh}], blocking=True)[0])

    def test_kana_observation_does_not_mask_a_later_hard_failure(self):
        candidate = {'messages': [
            {'jp': 'そうだね。', 'zh': 'ねえ、悟って呼んで。'},
            {'jp': 'こんにちは'},
        ]}
        for blocking, expected in ((False, 'invalid_message_field'), (True, 'zh_contains_kana')):
            with self.subTest(blocking=blocking), patch.object(
                    config, 'TRANSLATION_CHECKS_BLOCKING', blocking, create=True), \
                    patch('builtins.print') as logged:
                self.assertEqual(route_chat._protocol_rejection_reason(candidate), expected)
                warnings = [json.loads(call.args[0].split(' ', 1)[1])
                            for call in logged.call_args_list
                            if call.args and str(call.args[0]).startswith('[translation_check] ')]
                self.assertEqual(warnings[0]['reason'], 'zh_contains_kana')
                self.assertIs(warnings[0]['blocking'], blocking)

    def test_short_japanese_quote_inside_chinese_is_not_a_kana_warning(self):
        for zh in ('他说「おはよう」，我听懂了。', '他说「おはよう」。'):
            for blocking in (False, True):
                with self.subTest(zh=zh, blocking=blocking), patch.object(
                        config, 'TRANSLATION_CHECKS_BLOCKING', blocking, create=True), \
                        patch('builtins.print') as logged:
                    self.assertIsNone(route_chat._protocol_rejection_reason(
                        {'messages': [{'jp': 'そうだね', 'zh': zh}]}))
                    self.assertFalse(any(
                        call.args and 'zh_contains_kana' in str(call.args[0])
                        for call in logged.call_args_list))

    def test_clock_conflict_remains_hard_in_both_modes(self):
        for blocking in (False, True):
            with self.subTest(blocking=blocking), patch.object(
                    config, 'TRANSLATION_CHECKS_BLOCKING', blocking, create=True):
                self.assertEqual(route_chat._reject_temporal_candidate(
                    {'messages': [{'jp': '今は深夜だ。', 'zh': '现在是深夜了。'}]}, SNAPSHOT),
                    'current_clock_phase_conflict')

    def test_final_message_commit_uses_same_policy_and_trace_codes_are_known(self):
        for blocking in (False, True):
            with patch.object(config, 'TRANSLATION_CHECKS_BLOCKING', blocking, create=True):
                self.assertEqual(utils.commit_ready_msgs([
                    {'jp': CASES[0][1], 'zh': CASES[0][2]}]), not blocking)
        for reason, _, _ in CASES:
            self.assertIn(reason, route_chat._TRACE_RETRY_REASONS)

    def test_yen_units_are_not_days_and_match_japanese_currency(self):
        for text in ('五千日元', '五千日圆', '5000日元', '５０００日圆'):
            with self.subTest(text=text):
                self.assertEqual(route_chat._numeric_anchors(text), [('currency', 5000)])
                self.assertIsNone(route_chat._find_bilingual_numeric_conflict(
                    {'messages': [{'jp': '五千円だよ。', 'zh': text}]}))
        self.assertEqual(route_chat._numeric_anchors('五日'), [('days', 5)])

    def test_five_non_denial_phrases_do_not_emit_false_negation_warnings(self):
        for jp, zh in (
                ('それ、けっこういいと思うよ。', '我觉得那个还不错呢。'),
                ('大丈夫だから、安心してよ。', '我没事的，你放心吧。'),
                ('ぜんぜん平気だよ、ほんとに。', '这真的没关系，我懂的。'),
                ('それは大丈夫、任せてよ。', '这个没问题，交给我。'),
                ('どういたしまして、当然だよ。', '不客气啊，应该的。')):
            for blocking in (False, True):
                with self.subTest(jp=jp, blocking=blocking), patch.object(
                        config, 'TRANSLATION_CHECKS_BLOCKING', blocking), \
                        patch('builtins.print') as logged:
                    self.assertGreaterEqual(len(jp.strip()), 6)
                    self.assertGreaterEqual(len(zh.strip()), 6)
                    self.assertIsNone(utils._check_negation_consistency(jp, zh))
                    self.assertIsNone(route_chat._protocol_rejection_reason(
                        {'messages': [{'jp': jp, 'zh': zh}]}))
                    logged.assert_not_called()

    def test_real_negation_and_unlisted_negative_words_remain_detectable(self):
        for jp, zh in (
                ('今日は行かないつもりだよ。', '我今天打算去的。'),
                ('ちゃんと持ってきてよ、それ。', '那个你不用带过来了。'),
                ('そのやり方なら大丈夫だよ。', '这样做我觉得不行。')):
            for blocking in (False, True):
                with self.subTest(jp=jp, blocking=blocking), patch.object(
                        config, 'TRANSLATION_CHECKS_BLOCKING', blocking), \
                        patch('builtins.print') as logged:
                    self.assertGreaterEqual(len(jp.strip()), 6)
                    self.assertGreaterEqual(len(zh.strip()), 6)
                    self.assertEqual(utils._check_negation_consistency(jp, zh),
                                     'negation_polarity_mismatch')
                    reason = route_chat._protocol_rejection_reason(
                        {'messages': [{'jp': jp, 'zh': zh}]})
                    self.assertEqual(reason, 'negation_polarity_mismatch' if blocking else None)
                    warnings = [json.loads(call.args[0].split(' ', 1)[1])
                                for call in logged.call_args_list]
                    self.assertEqual(warnings[0]['reason'], 'negation_polarity_mismatch')

    def test_env_switch_defaults_false_and_accepts_true(self):
        import os
        from pathlib import Path
        source = Path(config.__file__).read_text(encoding='utf-8')
        for value, expected in ((None, False), ('false', False), ('true', True), ('1', True)):
            with self.subTest(value=value), patch.dict(os.environ, clear=False):
                os.environ.pop('TRANSLATION_CHECKS_BLOCKING', None)
                if value is not None:
                    os.environ['TRANSLATION_CHECKS_BLOCKING'] = value
                namespace = {}
                exec(compile(source, config.__file__, 'exec'), namespace)
                self.assertIs(namespace['TRANSLATION_CHECKS_BLOCKING'], expected)
