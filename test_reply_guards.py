"""Permanent R1/R8 regressions; runs without a database, network, or paid models.

Run from the project root:
    python -m pytest test_reply_guards.py -v
    python -m unittest test_reply_guards -v
"""
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import importlib
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / 'gojo_backend'), str(ROOT / 'tests')]

import utils
import config
from temporal_projection_offline import offline_environment

ZONE = timezone(timedelta(hours=8))
GOOD = {'jp': 'そうだね。', 'zh': '是这样啊。'}
TRANSLATION_CASES = (
    ('time_word_contradiction', {'jp': '朝ごはんまだ食べてないな。', 'zh': '晚饭还没吃呢。'}),
    ('negation_polarity_mismatch', {'jp': 'それは関係ないだろ。', 'zh': '那是有关系的吧。'}),
    ('number_value_mismatch', {'jp': '3時間かかるぞ。', 'zh': '要花5个小时。'}),
)
TEMPORAL_CASES = (
    ('current_clock_phase_conflict', {'jp': 'もう夜だ。', 'zh': '现在是晚上了。'}),
    ('bilingual_numeric_anchor_mismatch', {'jp': '3時に会おう。', 'zh': '5点见吧。'}),
    ('temporal_prompt_marker_echo', {'jp': '【历史消息】前の会話では。', 'zh': '之前的对话里。'}),
)


def envelope(message):
    return {'emotion': '平静', 'messages': [dict(message)]}


def raw(message):
    return json.dumps(envelope(message), ensure_ascii=False)


def snapshot(hour=13, minute=30, day=14):
    now = datetime(2026, 10, day, hour, minute, tzinfo=ZONE)
    return {'now_local': now, 'now_utc': now.astimezone(timezone.utc)}


class TestTranslationConsistency(unittest.TestCase):
    def setUp(self):
        # Preserve the strict-mode detector contract; default policy is tested separately.
        self.blocking = patch.object(config, 'TRANSLATION_CHECKS_BLOCKING', True)
        self.blocking.start()
        self.addCleanup(self.blocking.stop)

    def assert_reason(self, jp, zh, reason=None):
        self.assertEqual(utils.reply_message_rejection_reason({'jp': jp, 'zh': zh}), reason)

    def test_reported_translation_failures_reject(self):
        for reason, message in TRANSLATION_CASES:
            with self.subTest(reason=reason):
                self.assertEqual(utils.reply_message_rejection_reason(message), reason)

    def test_consistent_time_words_pass(self):
        for jp, zh in (
                ('朝から暑いな。', '从早上开始就很热啊。'),
                ('朝ごはんを食べよう。', '一起吃早饭吧。'),
                ('朝ごはんまだ食べてないな。', '早饭还没吃呢。'),
                ('夕方に会おう。', '晚上见吧。'),
                ('今夜は静かだな。', '今晚很安静呢。'),
                ('深夜まで話そう。', '聊到半夜吧。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_longest_time_word_does_not_create_false_night_match(self):
        # 深夜 must not contribute 夜 as a second, broader period.
        self.assert_reason('深夜に会おう。', '傍晚见吧。', 'time_word_contradiction')

    def test_no_time_word_or_only_one_side_passes(self):
        for jp, zh in (
                ('何言ってんだ。', '你在说什么。'),
                ('朝だぞ。起きろ。', '该起床了。'),
                ('食事をしよう。', '一起来吃晚饭吧。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_negation_in_either_direction_rejects(self):
        for jp, zh in (
                ('そんなことはないよ。', '事情就是这样子的。'),
                ('明日は参加しません。', '明天我会参加的哦。'),
                ('それはちゃんと知っている。', '那件事我完全不知道。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh, 'negation_polarity_mismatch')

    def test_matching_negations_and_affirmations_pass(self):
        for jp, zh in (
                ('それは知らないよ。', '那件事我不知道啊。'),
                ('明日は参加しません。', '明天我不会参加的。'),
                ('まだ寝てないんだよ。', '我还没睡觉呢。'),
                ('そういうことだよ。', '事情就是这样子的。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_faithful_casual_negations_are_not_polarity_flips(self):
        # Regression: closed-list negation detectors treated "no pattern
        # matched" as affirmative, so faithful translations whose negation sat
        # outside either list were rejected and /chat/text returned 502.
        for jp, zh in (
                ('まだ寝てないのか？もう遅いぞ。', '还没睡吗？已经很晚了。'),
                ('知らねぇな、そんなこと聞いてないし。', '不知道啊，那种事我也没听说。'),
                ('心配いらねぇって、オレがいるだろ。', '不用担心啦，有我在呢。'),
                ('寝れないんだろ？顔に書いてある。', '睡不着吧？都写在脸上了。'),
                ('今日はもう行かない。疲れたからな。', '今天不去了，因为累了。'),
                ('ごめん、まだ決めてないんだ。', '抱歉，还没决定。'),
                ('そんなつもりじゃなかったんだって。', '我不是那个意思啦。'),
                ('まだわからなくてさ、困ってる。', '还不明白，很困扰。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_lexicalised_negation_morphology_is_not_denial(self):
        # ない-adjectives and 不/没 compounds carry the morphology without
        # negating the clause; counting them flips polarity on its own.
        for jp, zh in (
                ('人が少ないな、今日は。', '今天人很少啊。'),
                ('危ないから気をつけろよ。', '很危险，小心点。'),
                ('そんなのつまらないだろ。', '那种事很无聊吧。'),
                ('少なくとも三人はいるぞ。', '至少有三个人哦。'),
                ('まあ、だいたいそんなもんだ。', '嘛，差不多就是那样。'),
                ('すごいじゃん、やるな。', '真了不起啊，厉害。'),
                ('でも今日は疲れたな。', '不过今天真是累了。'),
                ('他の人には言うなよ。', '别跟别人说哦。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_short_negations_skip_only_uncertain_polarity_check(self):
        for jp, zh in (('ない', '对'), ('それは関係ないだろ。', '是啊。'),
                       ('いいよ。', '这件事我不会做的。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_double_negation_and_rhetorical_question_pass(self):
        for jp, zh in (
                ('できないわけではない。', '这件事还是可以做的。'),
                ('知らないことはない。', '这件事我还是知道的。'),
                ('いい天気じゃないか。', '今天的天气真是很好啊。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_same_numbers_and_complete_cjk_numbers_pass(self):
        for jp, zh in (
                ('3時間かかるぞ。', '要花3个小时。'),
                ('三日後に会おう。', '3天后见吧。'),
                ('十三個あるよ。', '一共有13个哦。'),
                ('二十個あるよ。', '一共有20个哦。'),
                ('二〇二六年に会おう。', '2026年见吧。'),
                ('３個あるよ。', '一共有3个哦。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_unsupported_large_numerals_are_not_truncated_into_digits(self):
        for jp, zh in (
                ('一万円かかるよ。', '需要10000元哦。'),
                ('三万円かかるよ。', '需要30000元哦。'),
                ('3万ほどかかる。', '大概需要30000呢。'),
                ('一万三千円かかるよ。', '需要13000元哦。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_code_subscripts_do_not_become_numeric_translation_anchors(self):
        self.assert_reason('配列の a[0] を見て。', '明天复习 [第三章]。')
        self.assert_reason('a[0] は3個あるよ。', 'a[0] 有5个哦。',
                           'number_value_mismatch')

    def test_negative_reminder_keeps_matching_translation(self):
        self.assert_reason('水も忘れずに持っていこう。', '别忘了带水。')

    def test_different_complete_cjk_numbers_reject(self):
        # Character-by-character extraction would incorrectly share 三.
        self.assert_reason('十三個あるよ。', '一共有3个哦。', 'number_value_mismatch')

    def test_no_numbers_one_sided_numbers_and_shared_value_pass(self):
        for jp, zh in (
                ('すぐ終わる。', '马上就结束。'),
                ('明日な。', '3点见。'),
                ('3個と5個あるよ。', '一共有3个和7个。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_equivalent_clock_notation_does_not_trigger_r8(self):
        for jp, zh in (
                ('午後三時に会おう。', '15点见吧。'),
                ('午後3時に会おう。', '下午3点见吧。'),
                ('3時半に会おう。', '3:30见吧。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_twelve_hour_noon_and_midnight_words_pass(self):
        for jp, zh in (
                ('午前十二時に会おう。', '半夜十二点见吧。'),
                ('午後十二時に会おう。', '中午十二点见吧。'),
                ('午前１２時に会おう。', '半夜十二点见吧。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)

    def test_twelve_hour_clock_preserves_noon_midnight_distinction(self):
        for jp, zh in (
                ('午後12時に会おう。', '下午12点见吧。'),
                ('午前十二時に会おう。', '上午十二点见吧。')):
            with self.subTest(jp=jp):
                self.assert_reason(jp, zh)
        self.assert_reason('午前十二時に会おう。', '中午十二点见吧。',
                           'time_word_contradiction')
        self.assert_reason('午後12時に会おう。', '半夜12点见吧。',
                           'time_word_contradiction')

    def test_negative_permission_is_not_mistaken_for_polarity_flip(self):
        self.assert_reason('今日は来なくてもいい。', '你今天不必过来了。')
        self.assert_reason('今日は来なくても良い。', '你今天不必过来了。')

    def test_existing_identical_verbal_and_kana_checks_preserved(self):
        self.assert_reason('你好啊', '你好啊', 'jp_equals_zh_verbal')
        self.assert_reason('そうだね。', 'ねえ、悟って呼んで。', 'zh_contains_kana')

    def test_existing_invalid_fields_and_nonverbal_replies_preserved(self):
        for message in (None, {}, {'jp': 'こんにちは'},
                        {'jp': 'hello', 'zh': ''},
                        {'jp': 'state: thinking', 'zh': '在想事情。'}):
            with self.subTest(message=message):
                self.assertEqual(utils.reply_message_rejection_reason(message),
                                 'invalid_message_field')
        for text in ('😒', '……', '...'):
            with self.subTest(text=text):
                self.assert_reason(text, text)


class OfflineRouteTests(unittest.TestCase):
    def setUp(self):
        blocking = patch.object(config, 'TRANSLATION_CHECKS_BLOCKING', True)
        blocking.start()
        self.addCleanup(blocking.stop)

    @classmethod
    def setUpClass(cls):
        cls.environment = ExitStack()
        cls.environment.enter_context(offline_environment())
        cls.addClassCleanup(cls.environment.close)
        cls.route = importlib.import_module('route_chat')
        cls.temporal = importlib.import_module('temporal_awareness')


class TestTemporalRejection(OfflineRouteTests):
    def reject(self, message, snap=None):
        return self.route._reject_temporal_candidate(
            envelope(message), snapshot() if snap is None else snap)

    def test_three_temporal_observations_become_rejections(self):
        for reason, message in TEMPORAL_CASES:
            with self.subTest(reason=reason):
                self.assertEqual(self.reject(message), reason)

    def test_clock_assertion_in_either_language_rejects(self):
        for message in (
                {'jp': '今は朝だ。', 'zh': '慢慢说吧。'},
                {'jp': 'ゆっくり話そう。', 'zh': '现在是早上了。'}):
            with self.subTest(message=message):
                self.assertEqual(self.reject(message), 'current_clock_phase_conflict')

    def test_morning_and_late_morning_are_compatible(self):
        for hour, minute, message in (
                (10, 0, {'jp': '今は朝だ。', 'zh': '现在是早上了。'}),
                (6, 30, {'jp': '今は朝だ。', 'zh': '现在是上午了。'})):
            with self.subTest(hour=hour):
                self.assertIsNone(self.reject(message, snapshot(hour, minute)))

    def test_all_prompt_markers_reject_in_either_language_with_whitespace(self):
        for marker in ('【历史消息', '【对话间隔', '【后端当前时间事实'):
            for language in ('jp', 'zh'):
                with self.subTest(marker=marker, language=language):
                    message = dict(GOOD)
                    message[language] = ' \t' + marker + '】内容。'
                    self.assertEqual(self.reject(message, {}),
                                     'temporal_prompt_marker_echo')

    def test_missing_clock_does_not_disable_other_temporal_guards(self):
        self.assertIsNone(self.reject({'jp': '今は朝だ。', 'zh': '现在是早上了。'}, {}))
        self.assertEqual(self.reject(TEMPORAL_CASES[1][1], {}),
                         'bilingual_numeric_anchor_mismatch')
        self.assertEqual(self.reject(TEMPORAL_CASES[2][1], {}),
                         'temporal_prompt_marker_echo')
        for parsed in (None, {}, {'messages': []}):
            with self.subTest(parsed=parsed):
                self.assertIsNone(self.route._reject_temporal_candidate(parsed, {}))

    def test_consistent_and_equivalent_numeric_anchors_pass(self):
        for jp, zh in (
                ('3時に会おう。', '3点见吧。'),
                ('午後三時に会おう。', '15点见吧。'),
                ('3時半に会おう。', '3:30见吧。'),
                ('十三日後に会おう。', '13天后见吧。'),
                ('午前十二時に会おう。', '半夜十二点见吧。'),
                ('午後十二時に会おう。', '中午十二点见吧。')):
            with self.subTest(jp=jp):
                self.assertIsNone(self.reject({'jp': jp, 'zh': zh}))

    def test_real_clock_helper_preserves_history_condition_and_suggestions(self):
        for jp, zh in (
                ('昨日は朝だった。', '昨天现在是早上了。'),
                ('もし今は朝なら起きよう。', '如果现在是早上了，就起床。'),
                ('夜になったら寝よう。', '晚上再睡吧。'),
                ('「今は朝だ」と言った。', '他说“现在是早上了”。'),
                ('今は朝ではない。', '现在不是早上了。')):
            with self.subTest(jp=jp):
                self.assertIsNone(self.reject({'jp': jp, 'zh': zh}))

    def test_cross_midnight_history_does_not_assert_current_morning(self):
        snap = snapshot(1, 41)
        snap['last_assistant_message_at'] = datetime(2026, 10, 13, 23, 38, tzinfo=ZONE)
        self.assertIsNone(self.reject({
            'jp': '昨日の朝は早かったな。今は深夜だ。',
            'zh': '昨天早上很早呢。现在是深夜了。'}, snap))
        self.assertEqual(self.reject({
            'jp': '今は朝だ。', 'zh': '现在是早上了。'}, snap),
            'current_clock_phase_conflict')

    def test_observer_keeps_hard_guard_telemetry_without_numeric_candidate_prefix(self):
        for reason, message in (TEMPORAL_CASES[0], TEMPORAL_CASES[2]):
            with self.subTest(reason=reason), patch('builtins.print') as logged:
                self.route._observe_temporal_candidate(
                    envelope(message), snapshot(), SimpleNamespace(history_time_facts={}), 'evt')
                records = [json.loads(c.args[0].split(' ', 1)[1])
                           for c in logged.call_args_list
                           if c.args and str(c.args[0]).startswith('[temporal_check] {')]
                self.assertTrue(any(record['error_code'] == reason for record in records))


class TestGuardSemanticRetry(OfflineRouteTests):
    def generate(self, messages):
        response = SimpleNamespace(stop_reason='end_turn', usage=None)
        with patch.object(self.route, '_create_json', side_effect=[
                (raw(message), response) for message in messages]) as provider, \
                patch('builtins.print') as logged:
            result, state = self.route._generate_or_none(
                'offline', 100, [{'type': 'text', 'text': '角色设定'}], [],
                attempts=9, log_tag='reply-guards', cache_tag='reply-guards',
                generation_trace={'source_event_id': 'private-source-id'},
                reject_fn=lambda parsed: self.route._reject_temporal_candidate(parsed, snapshot()),
                observe_fn=lambda parsed: self.route._observe_temporal_candidate(
                    parsed, snapshot(), SimpleNamespace(history_time_facts={}), 'evt'))
        traces = [json.loads(c.args[0].split(' ', 1)[1])
                  for c in logged.call_args_list
                  if c.args and str(c.args[0]).startswith('[generation_trace] {')]
        return result, state, provider, traces

    def test_all_six_reasons_retry_to_good_and_preserve_trace_reason(self):
        for reason, message in TRANSLATION_CASES + TEMPORAL_CASES:
            with self.subTest(reason=reason):
                result, state, provider, traces = self.generate([message, GOOD])
                self.assertEqual(provider.call_count, 2)
                self.assertEqual(result['messages'], [GOOD])
                self.assertIsNone(state)
                self.assertEqual(traces[0]['retry_reason'], reason)
                self.assertTrue(traces[0]['will_retry'])
                self.assertEqual(traces[-1]['outcome'], 'accepted')
                self.assertEqual(traces[0]['outcome'],
                                 'parse_invalid' if reason in dict(TRANSLATION_CASES)
                                 else 'candidate_rejected')
                serialized = json.dumps(traces, ensure_ascii=False)
                self.assertNotIn(message['jp'], serialized)
                self.assertNotIn('private-source-id', serialized)
                hint = provider.call_args_list[1].args[2][-1]['text']
                self.assertIn(reason, hint)
                self.assertNotIn(message['jp'], hint)

    def test_all_six_reasons_exhaust_at_two_attempts(self):
        for reason, message in TRANSLATION_CASES + TEMPORAL_CASES:
            with self.subTest(reason=reason):
                result, state, provider, traces = self.generate([message, message, GOOD])
                self.assertIsNone(result)
                self.assertIsNone(state)
                self.assertEqual(provider.call_count, 2)
                self.assertEqual(len(traces), 2)
                self.assertEqual([trace['will_retry'] for trace in traces], [True, False])
                self.assertEqual(traces[0]['retry_reason'], reason)

    def test_protocol_and_temporal_failures_share_one_retry_budget(self):
        result, state, provider, traces = self.generate([
            TRANSLATION_CASES[0][1], TEMPORAL_CASES[0][1], GOOD])
        self.assertIsNone(result)
        self.assertIsNone(state)
        self.assertEqual(provider.call_count, 2)
        self.assertEqual([trace['outcome'] for trace in traces],
                         ['parse_invalid', 'candidate_rejected'])
        self.assertFalse(traces[-1]['will_retry'])



class TestChatGuardCommit(unittest.TestCase):
    """Exercise the real normal-chat reject closure and assistant commit gate."""

    def setUp(self):
        self.environment = ExitStack()
        self.addCleanup(self.environment.close)
        self.environment.enter_context(offline_environment())
        real_temporal = importlib.import_module('temporal_awareness')
        self.fixture = importlib.import_module('test_chat_commit_gate').ChatCommitGateTests
        self.fixture.setUp(self)
        self.environment.enter_context(patch.object(
            sys.modules['config'], 'TRANSLATION_CHECKS_BLOCKING', True))
        # The existing commit fixture replaces the temporal DB adapter. Retain
        # its in-memory snapshot and the real deterministic phase classifier.
        sys.modules['temporal_awareness'].clock_phase = real_temporal.clock_phase
        self.route.get_temporal_snapshot.return_value = snapshot()
        self.environment.enter_context(patch(
            'reply_availability.check_reply_availability', return_value={'can_reply': True}))
        self.environment.enter_context(patch('db_read_receipt.mark_immediate_seen'))
        self.environment.enter_context(patch('auto_pin.maybe_auto_pin'))
        self.environment.enter_context(patch('rolling_summary.enqueue_summary_job'))
        self.environment.enter_context(patch(
            'schedule_transition.validate_generated_schedule_reply', return_value=(None, {})))

    def send(self, *args, **kwargs):
        return self.fixture.send(self, *args, **kwargs)

    def generation_traces(self):
        return self.fixture.generation_traces(self)

    def user_memory_roles(self):
        return self.fixture.user_memory_roles(self)

    def assert_assistant_commit_skipped(self):
        return self.fixture.assert_assistant_commit_skipped(self)

    def assert_generation_failed(self, *args, **kwargs):
        return self.fixture.assert_generation_failed(self, *args, **kwargs)

    def assert_repaired_chat(self, reason, message):
        response, body = self.send([raw(message), raw(GOOD)])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['messages'][0]['jp'], GOOD['jp'])
        self.assertEqual(body['messages'][0]['zh'], GOOD['zh'])
        self.assertEqual(self.route._create_json.call_count, 2)
        self.assertEqual(self.user_memory_roles(), ['user', 'assistant'])
        self.assertEqual(self.save_user_once.call_count, 1)
        self.assertEqual(self.save_short.call_count, 1)
        self.assertEqual(self.generation_traces()[0]['retry_reason'], reason)
        self.client.messages.create.assert_not_called()

    def assert_exhausted_chat(self, reason, message):
        response, body = self.send([raw(message), raw(message), raw(GOOD)])
        self.assert_generation_failed(response, body, attempts=2)
        self.assertEqual(self.generation_traces()[0]['retry_reason'], reason)
        self.client.messages.create.assert_not_called()

    def test_clock_retry_commits_once(self):
        self.assert_repaired_chat(*TEMPORAL_CASES[0])

    def test_numeric_retry_commits_once(self):
        self.assert_repaired_chat(*TEMPORAL_CASES[1])

    def test_marker_retry_commits_once(self):
        self.assert_repaired_chat(*TEMPORAL_CASES[2])

    def test_translation_time_retry_commits_once(self):
        self.assert_repaired_chat(*TRANSLATION_CASES[0])

    def test_translation_negation_retry_commits_once(self):
        self.assert_repaired_chat(*TRANSLATION_CASES[1])

    def test_translation_number_retry_commits_once(self):
        self.assert_repaired_chat(*TRANSLATION_CASES[2])

    def test_clock_exhaustion_never_commits(self):
        self.assert_exhausted_chat(*TEMPORAL_CASES[0])

    def test_numeric_exhaustion_never_commits(self):
        self.assert_exhausted_chat(*TEMPORAL_CASES[1])

    def test_marker_exhaustion_never_commits(self):
        self.assert_exhausted_chat(*TEMPORAL_CASES[2])

    def test_translation_time_exhaustion_never_commits(self):
        self.assert_exhausted_chat(*TRANSLATION_CASES[0])

    def test_translation_negation_exhaustion_never_commits(self):
        self.assert_exhausted_chat(*TRANSLATION_CASES[1])

    def test_translation_number_exhaustion_never_commits(self):
        self.assert_exhausted_chat(*TRANSLATION_CASES[2])

    def test_normal_chat_reuses_snapshot_and_retains_observer(self):
        real_reject = self.route._reject_temporal_candidate
        real_observer = self.route._observe_temporal_candidate
        with patch.object(self.route, '_reject_temporal_candidate', wraps=real_reject) as reject, \
                patch.object(self.route, '_observe_temporal_candidate',
                             wraps=real_observer) as observer:
            response, _ = self.send([raw(TEMPORAL_CASES[0][1]), raw(GOOD)])
        self.assertEqual(response.status_code, 200)
        self.route.get_temporal_snapshot.assert_called_once_with('u', 'gojo')
        self.assertEqual(reject.call_count, 2)
        self.assertEqual(observer.call_count, 2)
        frozen = self.route.get_temporal_snapshot.return_value
        self.assertTrue(all(call.args[1] is frozen for call in reject.call_args_list))
        self.assertTrue(all(call.args[1] is frozen for call in observer.call_args_list))

    def test_default_translation_warnings_commit_original_reply_on_first_attempt(self):
        with patch.object(sys.modules['config'], 'TRANSLATION_CHECKS_BLOCKING', False):
            for index, (reason, message) in enumerate(TRANSLATION_CASES + (TEMPORAL_CASES[1],)):
                with self.subTest(reason=reason):
                    response, body = self.send([raw(message)], source_event_id=f'observe-{index}')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(body['messages'][0]['jp'], message['jp'])
                    self.assertEqual(body['messages'][0]['zh'], message['zh'])
                    self.assertEqual(self.route._create_json.call_count, 1)
        self.assertEqual(self.save_short.call_count, 4)

    def test_default_mode_keeps_calendar_and_schedule_rejections(self):
        with patch.object(sys.modules['config'], 'TRANSLATION_CHECKS_BLOCKING', False), \
                patch.object(self.route, 'find_reply_calendar_conflict',
                             return_value='tomorrow_noon_rewritten_as_today'):
            response, body = self.send([raw(GOOD), raw(GOOD)])
        self.assert_exhausted_result(response, body, 'tomorrow_noon_rewritten_as_today')

    def assert_exhausted_result(self, response, body, reason):
        self.assert_generation_failed(response, body, attempts=2)
        self.assertEqual(self.generation_traces()[0]['retry_reason'], reason)

    def test_default_mode_keeps_schedule_rejection(self):
        with patch.object(sys.modules['config'], 'TRANSLATION_CHECKS_BLOCKING', False), \
                patch.object(self.route, '_reject_schedule_candidate',
                             return_value='active_event_completion_claim_without_intent'):
            response, body = self.send([raw(GOOD), raw(GOOD)])
        self.assert_exhausted_result(response, body, 'active_event_completion_claim_without_intent')


if __name__ == '__main__':
    unittest.main()
