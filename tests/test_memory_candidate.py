"""Deterministic candidate gates with canonical source reads stubbed locally."""
import copy
import os
import sys
import unittest
from unittest.mock import Mock, patch

BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import memory_candidate


class MemoryCandidateGateTests(unittest.TestCase):
    raw_text = '今天买咖啡亏了69元'

    def candidate(self, **overrides):
        candidate = {
            'source_event_id': 'source-1',
            'quote': self.raw_text,
            'summary': '她今天买咖啡亏了69元',
            'predicate': 'reported_fact',
            'object': '交易结果',
            'value': '亏了69元',
        }
        candidate.update(overrides)
        return candidate

    def validate(self, candidate, raw_text=None):
        raw_text = self.raw_text if raw_text is None else raw_text
        raw = {'event_id': 'source-1', 'role': 'user', 'content': raw_text,
               'metadata': {}}
        cur = Mock()
        cur.fetchone.return_value = ({'content': raw_text, 'source_chat_id': 'c'},)
        with patch.object(memory_candidate, 'get_active_events_by_ids', return_value=[raw]), \
                patch.object(memory_candidate, 'cognitive_source_matches_scope', return_value=True):
            result = memory_candidate.validate_candidate(cur, 'u', 'c', candidate)
        for call in cur.execute.call_args_list:
            self.assertTrue(call.args[0].lstrip().upper().startswith('SELECT'))
        return result

    def validate_with_grounding_coverage(self, candidate, raw_text=None):
        """Observe the real gate's coverage without reproducing its algorithm."""
        measured = {}
        code = memory_candidate.validate_candidate.__code__
        previous_trace = sys.gettrace()

        def trace(frame, event, _arg):
            if frame.f_code is not code:
                return None
            if event == 'return' and 'chars' in frame.f_locals:
                measured['coverage'] = (
                    sum(frame.f_locals.get('covered', [])), len(frame.f_locals['chars']))
            return trace

        try:
            sys.settrace(trace)
            result = self.validate(candidate, raw_text=raw_text)
        finally:
            sys.settrace(previous_trace)
        return result, measured.get('coverage')

    def test_matching_quote_and_summary_pass_without_mutation(self):
        candidate = self.candidate()
        original = copy.deepcopy(candidate)
        self.assertEqual(self.validate(candidate), (True, None))
        self.assertEqual(candidate, original)

    def test_interpretation_is_not_grounded_and_cannot_rescue_a_summary(self):
        interpretation = '她其实是在抱怨室友，不是真的想搬家，听起来像昨天亏了100元'
        candidate = self.candidate(interpretation=interpretation)
        original = copy.deepcopy(candidate)
        self.assertEqual(self.validate(candidate), (True, None))
        self.assertEqual(candidate, original)

        laundered = self.candidate(summary='她打算搬去火星和室友分手',
                                   interpretation='她今天买咖啡亏了69元')
        self.assertEqual(self.validate(laundered),
                         (False, 'summary_not_grounded_in_quote'))
        self.assertEqual(laundered['interpretation'], '她今天买咖啡亏了69元')

    def test_absent_quote_is_rejected(self):
        self.assertEqual(self.validate(self.candidate(quote='我今天去打篮球')),
                         (False, 'quote_not_verbatim'))

    def test_one_changed_character_in_quote_is_rejected(self):
        self.assertEqual(self.validate(self.candidate(quote='今天买奶茶亏了69元')),
                         (False, 'quote_not_verbatim'))

    def test_verbatim_quote_does_not_normalize_whitespace_or_width(self):
        for quote in ('今天买咖啡 亏了69元', '今天买咖啡亏了６９元'):
            with self.subTest(quote=quote):
                self.assertEqual(self.validate(self.candidate(quote=quote)),
                                 (False, 'quote_not_verbatim'))

    def test_two_character_quote_is_rejected(self):
        self.assertEqual(self.validate(self.candidate(quote='好的', summary='她答应出门'),
                                       raw_text='我说好的再出门'),
                         (False, 'quote_too_short'))

    def test_quote_length_excludes_all_whitespace(self):
        quote = '好 \t 的\n呀'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary='她答应了'),
                                       raw_text=quote),
                         (False, 'quote_too_short'))

    def test_four_non_whitespace_characters_are_sufficient(self):
        quote = '我\t爱 咖\n啡'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary='她爱咖啡'),
                                       raw_text=quote), (True, None))

    def test_changed_number_is_rejected(self):
        self.assertEqual(self.validate(self.candidate(summary='她今天买咖啡亏了96元')),
                         (False, 'number_value_mismatch'))

    def test_one_matching_number_cannot_mask_an_added_number(self):
        self.assertEqual(self.validate(self.candidate(summary='她亏了69元又亏了96元')),
                         (False, 'number_value_mismatch'))

    def test_summary_cannot_add_a_number_to_a_quote_without_numbers(self):
        quote = '今天交易亏钱了'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary='她亏了96元'),
                                       raw_text=quote),
                         (False, 'number_value_mismatch'))

    def test_chinese_and_arabic_number_values_match(self):
        for quote, summary in (
                ('今天交易亏了六十九元', '她亏了69元'),
                ('今天交易亏了69元', '她亏了六十九元'),
                ('今天交易亏了６９元', '她亏了69元')):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote), (True, None))

    def test_supported_chinese_number_values_remain_distinct(self):
        from utils import _check_number_consistency

        for left, right in (
                ('六十九', '九十六'), ('三十', '十三'), ('二', '八'),
                ('一百', '一千'), ('十', '百'), ('六十九', '六十八')):
            with self.subTest(left=left, right=right):
                self.assertEqual(_check_number_consistency(left, right),
                                 'number_value_mismatch')
                self.assertFalse(memory_candidate._same_number(left, right))

    def test_supported_chinese_number_changes_are_rejected_end_to_end(self):
        for quote, summary in (
                ('今天交易亏了六十九元', '她亏了九十六元'),
                ('今天交易亏了三十元', '她亏了十三元')):
            with self.subTest(quote=quote, summary=summary):
                self.assertFalse(memory_candidate._numbers_match(quote, summary))
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote),
                                 (False, 'number_value_mismatch'))

    def test_indefinite_duration_is_not_a_quantity(self):
        # Shadow batch bdebbb00, candidate 1: 睡了会 -> 睡了一会 adds no number,
        # the 一 is the same indefinite article already exempted in 一下 / 一点.
        for text in ('睡了一会', '等一会儿再说', '一会就好'):
            with self.subTest(text=text):
                self.assertEqual(memory_candidate._number_tokens(text), [])
        quote = '太累了我刚刚又去睡了会'
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary='她太累了，刚刚又去睡了一会'),
                          raw_text=quote), (True, None))

    def test_counted_units_are_still_quantities_after_the_exemption(self):
        for text, expected in (('一会议室的人', ['一']), ('开了三会', ['三']),
                               ('十一会场', ['十一'])):
            with self.subTest(text=text):
                self.assertEqual(memory_candidate._number_tokens(text), expected)

    def test_number_may_be_omitted_from_summary(self):
        self.assertEqual(self.validate(self.candidate(summary='她今天买咖啡亏钱了')),
                         (True, None))

    def test_colloquial_and_large_numbers_match_equivalent_values(self):
        for quote, summary in (
                ('我两点下班', '她2点下班'),
                ('我有两个弟弟', '她有2个弟弟'),
                ('存了三万块', '她存了30000块'),
                ('今天交易亏了一亿元', '她亏了100000000元'),
                ('今天交易亏了三萬元', '她亏了30000元'),
                ('今天交易亏了两億元', '她亏了200000000元')):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote), (True, None))

    def test_colloquial_and_large_number_changes_are_rejected(self):
        for quote, summary in (
                ('今天交易亏了三万元', '她亏了四万元'),
                ('今天交易亏了六十九万元', '她亏了九十六万元'),
                ('今天交易亏了一亿元', '她亏了两亿元'),
                ('我两点下班', '她三点下班')):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote),
                                 (False, 'number_value_mismatch'))

    def test_extended_integer_normalization_is_symmetric(self):
        for left, right in (
                ('两', '二'), ('两百', '200'), ('两千零二', '2002'),
                ('三百二十万', '3200000'), ('六十九万', '690000'),
                ('九十六万', '960000'), ('3万', '30000'),
                ('两亿', '200000000'), ('两億', '二亿')):
            with self.subTest(left=left, right=right):
                self.assertTrue(memory_candidate._same_number(left, right))
                self.assertTrue(memory_candidate._same_number(right, left))

    def test_ambiguous_extended_number_tokens_remain_exact_only(self):
        for token, unsupported_equivalent in (
                ('一万二', '10002'), ('两百二', '202'),
                ('两三万', '230000'), ('两三', '23'), ('三四万', '340000'),
                ('两百零', '200'), ('两百零零三', '203'), ('零两百', '200'),
                ('两千零三百', '2300'), ('两十零三', '23'),
                ('一百二万', '1020000'), ('三万零五', '30005'),
                ('一亿三万', '100030000'), ('一万万', '100000000'),
                ('两十百', '120'), ('两点五万', '25000'),
                ('负两万', '-20000'), ('两兆', '2000000000000')):
            with self.subTest(token=token):
                self.assertTrue(memory_candidate._same_number(token, token))
                self.assertFalse(memory_candidate._same_number(token, unsupported_equivalent))
                self.assertFalse(memory_candidate._same_number(unsupported_equivalent, token))

    def test_extended_number_comparison_does_not_parse_unbounded_python_integers(self):
        huge = '9' * 5000
        self.assertFalse(memory_candidate._same_number('两', huge))
        self.assertFalse(memory_candidate._same_number(huge, '两'))

    def test_grouped_amounts_cannot_match_only_their_digit_fragments(self):
        for quote, summary, expected in (
                ('今天交易亏了69,000元', '她亏了69元', False),
                ('今天支出1元收入0元', '她支出1,000元', False),
                ('今天交易亏了69,000元', '她亏了69000元', True),
                ('今天支出了1000元', '她支出1,000元', True)):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote),
                                 (True, None) if expected else (False, 'number_value_mismatch'))

    def test_sign_or_decimal_change_cannot_match_an_integer_fragment(self):
        for quote, summary in (
                ('今天交易亏了69.5元', '她亏了69.6元'),
                ('今天交易亏了69元', '她亏了6.9元'),
                ('今天账户余额是负69元', '她账户余额是69元'),
                ('今天账户余额是-69元', '她账户余额是69元')):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote),
                                 (False, 'number_value_mismatch'))

    def test_unsupported_number_tokens_require_an_exact_token_match(self):
        # 这里的同值用例靠 token 原样相等通过，未经过 _check_number_consistency。
        for quote, summary, expected in (
                ('今天交易亏了六十九万元', '她亏了六十九万元', (True, None)),
                ('今天交易亏了六十九万元', '她亏了九十六万元',
                 (False, 'number_value_mismatch')),
                ('今天交易亏了1234567元', '她亏了1234567元', (True, None)),
                ('今天交易亏了1234567元', '她亏了7654321元',
                 (False, 'number_value_mismatch'))):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote), expected)

    def test_negation_cannot_be_removed_from_a_short_quote(self):
        quote = '今天没亏'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary='她亏了'),
                                       raw_text=quote),
                         (False, 'negation_polarity_mismatch'))

    def test_negation_cannot_be_added_to_summary(self):
        quote = '今天交易亏钱了'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary='她今天没亏钱'),
                                       raw_text=quote),
                         (False, 'negation_polarity_mismatch'))

    def test_matching_negative_polarity_passes(self):
        quote = '这次交易没亏钱'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary='她这次交易没亏钱'),
                                       raw_text=quote), (True, None))

    def test_rhetorical_denial_matches_a_plain_denial_in_the_summary(self):
        # Shadow batch 89e68370, candidate 3: 哪有钱 and 没钱 deny the same
        # thing, so G5 must not read the faithful summary as a polarity flip.
        quote = '我现在可是穷光蛋哪有钱点星巴克'
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary='她说自己现在是穷光蛋，没钱点星巴克'),
                          raw_text=quote), (True, None))

    def test_interrogative_na_words_are_not_denials(self):
        for text in ('你在哪里上班', '哪个校区上课', '哪天有空'):
            with self.subTest(text=text):
                self.assertFalse(memory_candidate._has_grounding_negation(text))

    def test_leading_answer_particle_does_not_negate_the_following_clause(self):
        # Shadow batch 15347f2e, candidate 1: the 没有 answers "很疼吗", it does
        # not deny 疼. The summary carries no denial and must still match.
        quote = '没有哦，就是有点点疼…要是你在就好了…'
        self.assertFalse(memory_candidate._has_grounding_negation(quote))
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary='她就是有点点疼'),
                          raw_text=quote), (True, None))

    def test_answer_particle_is_kept_when_the_rest_still_denies(self):
        for text in ('没有，不疼', '没有啊，我不去了', '没有哦，没带伞'):
            with self.subTest(text=text):
                self.assertTrue(memory_candidate._has_grounding_negation(text))

    def test_double_negatives_are_not_treated_as_plain_denial(self):
        for quote, summary in (
                ('我今天不得不去上班', '她今天去上班'),
                ('我不是不喜欢咖啡', '她喜欢咖啡')):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote), (True, None))

    def test_grounding_contract_cases_report_actual_covered_positions(self):
        cases = (
            (self.raw_text, '她今天买咖啡亏了69元', (5, 6), True),
            (self.raw_text, '她今天买咖啡亏钱了', (4, 6), True),
            ('今天交易亏了六十九元', '她亏了69元', (2, 3), True),
            ('今天交易亏了69元', '她亏了六十九元', (2, 3), True),
            ('我昨天晚上喝咖啡', '她昨晚喝咖啡', (3, 3), True),
            ('我明天早上喝咖啡', '她明早喝咖啡', (3, 3), True),
            ('我\t爱 咖\n啡', '她爱咖啡', (3, 3), True),
            ('我今天不得不去上班', '她今天去上班', (3, 3), True),
            ('我今天去上班了', '她今天不得不去上班', (3, 6), False),
            ('我今天很累', '在新加坡读书', (0, 6), False),
            ('我今天很累', '讨厌香菜', (0, 4), False),
            ('我今天很累', '喜欢吃辣', (0, 4), False),
            ('我今天很累', '她今天很好', (0, 2), False),
        )
        with patch('ai_client.create_chat') as create, \
                patch('ai_client.get_anthropic') as client, \
                patch('structured_output.invoke_structured_llm') as structured:
            for quote, summary, coverage, accepted in cases:
                with self.subTest(quote=quote, summary=summary):
                    result, actual_coverage = self.validate_with_grounding_coverage(
                        self.candidate(quote=quote, summary=summary), raw_text=quote)
                    self.assertEqual(actual_coverage, coverage)
                    self.assertEqual(result, (True, None) if accepted else
                                     (False, 'summary_not_grounded_in_quote'))
            for model in (create, client, structured):
                model.assert_not_called()

    def test_adding_double_negative_fails_grounding_at_fifty_percent(self):
        quote = '我今天去上班了'
        result, coverage = self.validate_with_grounding_coverage(
            self.candidate(quote=quote, summary='她今天不得不去上班'), raw_text=quote)
        self.assertEqual(coverage, (3, 6))
        self.assertEqual(result, (False, 'summary_not_grounded_in_quote'))

    def test_weekday_and_clock_numbers_remain_grounded_as_complete_words(self):
        quote = '周六调休了周三的课，今天早八到晚九'
        summary = '她周六调休补周三的课，今天课程从早八到晚九'
        result, coverage = self.validate_with_grounding_coverage(
            self.candidate(quote=quote, summary=summary), raw_text=quote)
        self.assertEqual(result, (True, None))
        self.assertEqual(coverage, (13, 17))

    def test_grounded_schedule_cannot_add_a_number_not_in_the_quote(self):
        quote = '周六调休了周三的课，今天早八到晚九'
        summary = '她周六调休补周三的课，今天课程从早八到晚十'
        result, coverage = self.validate_with_grounding_coverage(
            self.candidate(quote=quote, summary=summary), raw_text=quote)
        self.assertEqual(result, (False, 'number_value_mismatch'))
        self.assertIsNone(coverage)

    def test_referent_from_another_sentence_of_the_same_turn_is_grounded(self):
        # Shadow batch 89e68370: one user turn yields several quotes. 那个外卖小哥
        # is introduced in the first sentence and referred to as 他 in the second,
        # so resolving it in the summary is provenanced, not invented.
        turn = ('在等车过打算拐弯进楼的时候那个外卖小哥不走外面偏偏往里面来撞，'
                '一看到他翻的是星巴克瞬间释怀了甚至有点想笑')
        quote = '一看到他翻的是星巴克瞬间释怀了甚至有点想笑'
        result, coverage = self.validate_with_grounding_coverage(
            self.candidate(quote=quote, summary='她看到外卖小哥送的是星巴克后瞬间释怀觉得好笑'),
            raw_text=turn)
        self.assertEqual(result, (True, None))
        self.assertEqual(coverage, (15, 21))

    def test_invented_clause_is_rejected_against_the_whole_turn(self):
        # The widened base must not weaken G9: 对方还说是她的问题 appears nowhere
        # in the turn and still has to fail.
        turn = ('在等车过打算拐弯进楼的时候那个外卖小哥不走外面偏偏往里面来撞，'
                '一看到他翻的是星巴克瞬间释怀了甚至有点想笑')
        quote = '在等车过打算拐弯进楼的时候那个外卖小哥不走外面偏偏往里面来撞'
        self.assertEqual(
            self.validate(self.candidate(
                quote=quote, summary='她等车时拐弯进楼被外卖小哥撞了，对方还说是她的问题'),
                raw_text=turn),
            (False, 'summary_not_grounded_in_quote'))

    def test_grounding_base_is_the_turn_and_never_generated_text(self):
        # Text that is not in the canonical turn cannot ground a summary, even
        # when it would be a plausible reading of it.
        turn = '我今天很累'
        self.assertEqual(
            self.validate(self.candidate(quote=turn, summary='她今天很累需要休息'),
                          raw_text=turn),
            (False, 'summary_not_grounded_in_quote'))

    def test_synonym_rewrite_still_fails_summary_grounding(self):
        quote = '我今天也要打牌，他们过来我家'
        summary = '她今天要在家里和朋友打牌，朋友会来她家'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                       raw_text=quote),
                         (False, 'summary_not_grounded_in_quote'))

    def test_grounding_does_not_require_abstract_object_or_value_in_the_quote(self):
        quote = '我今天很累'
        result, coverage = self.validate_with_grounding_coverage(self.candidate(
            quote=quote, summary='她今天很累', object='身体状态', value='疲惫'),
            raw_text=quote)
        self.assertEqual(coverage, (2, 2))
        self.assertEqual(result, (True, None))

    def test_earlier_gates_keep_their_reason_without_executing_grounding(self):
        for changes, reason in (
                ({'source_event_id': ''}, 'source_not_active_or_changed'),
                ({'quote': '不存在的引用'}, 'quote_not_verbatim'),
                ({'quote': '今天'}, 'quote_too_short'),
                ({'summary': '她亏了96元'}, 'number_value_mismatch'),
                ({'summary': '她没亏69元'}, 'negation_polarity_mismatch'),
                ({'summary': '她昨天亏了69元'}, 'time_word_contradiction'),
                ({'predicate': 'invalid', 'summary': '她今天很好'},
                 'invalid_predicate_or_summary'),
                ({'summary': ''}, 'invalid_predicate_or_summary'),
                ({'summary': '她' * 61}, 'invalid_predicate_or_summary')):
            with self.subTest(reason=reason, changes=changes):
                candidate = self.candidate(perspective='她其实很在意', **changes)
                original = copy.deepcopy(candidate)
                result, coverage = self.validate_with_grounding_coverage(candidate)
                self.assertEqual(result, (False, reason))
                self.assertIsNone(coverage)
                self.assertEqual(candidate, original)

    def test_empty_grounding_residue_still_runs_perspective_validation(self):
        for perspective, dropped in (('我想认真听她说', False), ('她其实很在意', True)):
            with self.subTest(perspective=perspective):
                candidate = self.candidate(summary='她今天69', perspective=perspective)
                result, coverage = self.validate_with_grounding_coverage(candidate)
                self.assertEqual(coverage, (0, 0))
                self.assertEqual(result, (True, 'perspective_dropped' if dropped else None))
                self.assertEqual('perspective' in candidate, not dropped)
                self.assertEqual(candidate['summary'], '她今天69')

    def test_grounding_uses_position_union_and_the_sixty_percent_boundary(self):
        for summary, expected_coverage, accepted in (
                ('她喝咖啡提神', (3, 5), True),
                ('她喝咖啡来提神', (3, 6), False),
                ('她喝咖啡喝咖啡', (6, 6), True)):
            with self.subTest(summary=summary):
                quote = '我喜欢喝咖啡'
                result, coverage = self.validate_with_grounding_coverage(
                    self.candidate(quote=quote, summary=summary), raw_text=quote)
                self.assertEqual(coverage, expected_coverage)
                self.assertEqual(result, (True, None) if accepted else
                                 (False, 'summary_not_grounded_in_quote'))

    def test_yesterday_cannot_replace_today(self):
        self.assertEqual(self.validate(self.candidate(summary='她昨天买咖啡亏了69元')),
                         (False, 'time_word_contradiction'))

    def test_relative_day_is_checked_independently_of_matching_period(self):
        quote = '我今天晚上喝咖啡'
        for day in ('昨天', '明天'):
            with self.subTest(day=day):
                self.assertEqual(self.validate(self.candidate(
                    quote=quote, summary=f'她{day}晚上喝咖啡'), raw_text=quote),
                    (False, 'time_word_contradiction'))

    def test_conflicting_periods_are_rejected(self):
        quote = '我今天上午喝咖啡'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary='她今天下午喝咖啡'),
                                       raw_text=quote),
                         (False, 'time_word_contradiction'))

    def test_compatible_evening_and_night_pass(self):
        quote = '我今天傍晚喝咖啡'
        self.assertEqual(self.validate(self.candidate(quote=quote, summary='她今天晚上喝咖啡'),
                                       raw_text=quote), (True, None))

    def test_relative_day_abbreviations_keep_their_time_of_day(self):
        for quote, summary, expected in (
                ('我昨天上午喝咖啡', '她昨晚喝咖啡', False),
                ('我明天下午喝咖啡', '她明早喝咖啡', False),
                ('我明天早上喝咖啡', '她明晚喝咖啡', False),
                ('我昨天晚上喝咖啡', '她昨晚喝咖啡', True),
                ('我明天早上喝咖啡', '她明早喝咖啡', True)):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote),
                                 (True, None) if expected else (False, 'time_word_contradiction'))

    def test_omitted_time_word_is_allowed(self):
        self.assertEqual(self.validate(self.candidate(summary='她买咖啡亏了69元')),
                         (True, None))

    def test_all_four_existing_predicates_are_allowed(self):
        for predicate in ('reported_preference', 'reported_state',
                          'reported_identity', 'reported_fact'):
            with self.subTest(predicate=predicate):
                self.assertEqual(self.validate(self.candidate(predicate=predicate)), (True, None))

    def test_invalid_predicate_is_rejected(self):
        self.assertEqual(self.validate(self.candidate(predicate='inferred_fact')),
                         (False, 'invalid_predicate_or_summary'))

    def test_empty_or_non_text_summary_is_rejected(self):
        for summary in ('', ' \t\n', None, ['她喜欢咖啡']):
            with self.subTest(summary=summary):
                self.assertEqual(self.validate(self.candidate(summary=summary)),
                                 (False, 'invalid_predicate_or_summary'))

    def test_missing_blank_or_non_text_object_and_value_are_rejected(self):
        for field in ('object', 'value'):
            candidate = self.candidate()
            candidate.pop(field)
            with self.subTest(field=field, value='missing'):
                self.assertEqual(self.validate(candidate),
                                 (False, 'invalid_predicate_or_summary'))
            for value in ('', ' \t\n', None, 69, ['交易结果'], {'text': '亏了69元'}):
                with self.subTest(field=field, value=value):
                    self.assertEqual(self.validate(self.candidate(**{field: value})),
                                     (False, 'invalid_predicate_or_summary'))

    def test_summary_length_boundary_is_sixty_characters(self):
        for length, expected in ((60, (True, None)),
                                 (61, (False, 'invalid_predicate_or_summary'))):
            with self.subTest(length=length):
                self.assertEqual(self.validate(self.candidate(summary='她' * length)), expected)

    def test_safe_character_perspective_is_preserved(self):
        candidate = self.candidate(perspective='我想认真听她说')
        original = copy.deepcopy(candidate)
        self.assertEqual(self.validate(candidate), (True, None))
        self.assertEqual(candidate, original)

    def test_psychological_assertions_drop_only_perspective(self):
        for perspective in ('她其实很在意', '她心里很在意', '她肯定很在意',
                            '她一定很在意', '她在想这件事', '她不敢说真话'):
            with self.subTest(perspective=perspective):
                candidate = self.candidate(perspective=perspective)
                expected = self.candidate()
                self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
                self.assertEqual(candidate, expected)

    def test_unconfirmed_character_perspective_is_dropped(self):
        for perspective in ('这件事很重要', '我们想认真听她说'):
            with self.subTest(perspective=perspective):
                candidate = self.candidate(perspective=perspective)
                self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
                self.assertEqual(candidate, self.candidate())

    def test_character_self_prefix_cannot_mask_psychological_assertion(self):
        for assertion in ('她其实很在意', '她心里很在意', '她肯定很在意',
                          '她一定很在意', '她在想这件事', '她不敢说真话'):
            with self.subTest(assertion=assertion):
                candidate = self.candidate(perspective='我觉得' + assertion)
                self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
                self.assertEqual(candidate, self.candidate())

    def test_invalid_or_oversized_perspective_is_dropped(self):
        for perspective in (None, '', ' \t\n', 1, ['我想听她说'],
                            {'text': '我想听她说'}, '我想' + '听' * 39):
            with self.subTest(perspective=perspective):
                candidate = self.candidate(perspective=perspective)
                self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
                self.assertEqual(candidate, self.candidate())

    def test_forty_character_perspective_is_allowed(self):
        candidate = self.candidate(perspective='我想' + '听' * 38)
        self.assertEqual(self.validate(candidate), (True, None))
        self.assertEqual(len(candidate['perspective']), 40)

    def test_logged_numeral_words_are_not_number_mismatches(self):
        # 线上原文。数字关不再把词内汉字当成数量。整段校验仍由未改的 G9 拒绝：
        # 摘要里有原话没有的内容（恋爱八卦、香水、传感器）。
        cases = (
            ('我感觉我对这个的敏锐度也不高而且也不感兴趣',
             '她觉得自己对恋爱八卦的敏锐度不高，也不太感兴趣'),
            ('之前我买的时候是三百块现在都八百了',
             '她之前花三百块买了一瓶香水，现在价格涨到八百了'),
            ('我做了你的等比身长娃娃……',
             '她想做一个等比身长的娃娃，再加传感器'),
        )
        for quote, summary in cases:
            with self.subTest(quote=quote, summary=summary):
                self.assertTrue(memory_candidate._numbers_match(quote, summary))
                self.assertEqual(
                    memory_candidate._has_grounding_negation(quote),
                    memory_candidate._has_grounding_negation(summary))
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    (False, 'summary_not_grounded_in_quote'))

    def test_logged_question_and_bare_mei_are_not_polarity_mismatches(self):
        # 线上原文。是不是是疑问，没发和没有都是否定。G9 仍拒绝夹带解释的摘要。
        cases = (
            ('是我不重要所以你才能一直这样对我吗',
             '她质问对方是不是因为她不重要才一直这样对她'),
            ('你没发！',
             '她指责对方没有按约定主动发消息，认为对方食言'),
        )
        for quote, summary in cases:
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(
                    memory_candidate._has_grounding_negation(quote),
                    memory_candidate._has_grounding_negation(summary))
                self.assertTrue(memory_candidate._numbers_match(quote, summary))
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    (False, 'summary_not_grounded_in_quote'))

    def test_grounded_question_and_bare_mei_pass_the_full_gate(self):
        cases = (
            ('你有没有吃饭啊', '她吃饭啊'),
            ('你没发消息啊', '她没有发消息'),
            ('村子被淹没了啊', '村子进水了啊'),
            ('等了十分钟吧', '她等了10分钟吧'),
        )
        for quote, summary in cases:
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    (True, None))

    def test_logged_changed_number_is_still_rejected(self):
        quote = '亏了69元'
        summary = '亏了96元'
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary=summary), raw_text=quote),
            (False, 'number_value_mismatch'))

    def test_logged_added_negation_is_still_rejected(self):
        quote = '显得我像个小丑舔狗'
        summary = '她不想再主动了'
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary=summary), raw_text=quote),
            (False, 'negation_polarity_mismatch'))

    def test_dropped_bu_zhidao_stays_a_polarity_mismatch(self):
        # 原话「我不知道」在动词表里，是真否定，不是是不是/没发那种认错。
        # 摘要删掉了这层否定，只留下后半句。G5 比的是两边有没有否定，
        # 不是“剩下的从句是否属实”，所以仍然拒绝。
        quote = '我不知道，我只想要明确的表态'
        summary = '她说自己只想要对方明确的表态'
        self.assertTrue(memory_candidate._has_grounding_negation(quote))
        self.assertFalse(memory_candidate._has_grounding_negation(summary))
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary=summary), raw_text=quote),
            (False, 'negation_polarity_mismatch'))

    def test_indefinite_yi_does_not_swallow_a_real_quantity(self):
        cases = (
            ('我买了一百个苹果', '她买了两百个苹果', (False, 'number_value_mismatch')),
            ('我买了一个苹果呀', '她买了三个苹果呀', (False, 'number_value_mismatch')),
            ('会议开了三十分钟啊', '她开了四十分钟啊', (False, 'number_value_mismatch')),
            ('我喜欢八卦内容', '她喜欢8个内容', (False, 'number_value_mismatch')),
        )
        for quote, summary, expected in cases:
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    expected)

    def test_conversational_mei_openers_pass_the_same_negation_gate(self):
        cases = (
            ('没关系啦我明天考离散数学', '她明天考离散数学'),
            ('没错我买了咖啡', '她买了咖啡'),
            ('没事我自己搞定这个报告', '她自己搞定这个报告'),
        )
        for quote, summary in cases:
            with self.subTest(quote=quote):
                self.assertFalse(memory_candidate._has_grounding_negation(quote))
                self.assertFalse(memory_candidate._has_grounding_negation(summary))
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    (True, None))

    def test_mei_shi_zuo_stays_a_negation_mismatch(self):
        quote = '我今天没事做'
        summary = '她今天做事'
        self.assertTrue(memory_candidate._has_grounding_negation(quote))
        self.assertFalse(memory_candidate._has_grounding_negation(summary))
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary=summary),
                          raw_text=quote),
            (False, 'negation_polarity_mismatch'))

    def test_non_denial_bu_words_and_a_not_a_are_not_denials(self):
        for text in ('不过', '不但', '不仅', '不如', '差不多', '是不是', '有没有',
                     '淹没', '没收', '没关系', '没错', '没事'):
            with self.subTest(text=text):
                self.assertFalse(memory_candidate._has_grounding_negation(text))
        for text in ('没发', '没有', '没收到', '不重要', '不想', '没事做', '没事干',
                     '没事儿'):
            with self.subTest(text=text):
                self.assertTrue(memory_candidate._has_grounding_negation(text))

    def test_logged_leading_report_frame_does_not_fail_a_faithful_summary(self):
        # 说 / 说自己 is the frame the prompt asks for. It is not extra content.
        cases = (
            ('我很聪明的', '她说自己很聪明'),
            ('因为你写了呀，我照着打的，但并不知道是什么意思',
             '她说之前的日语是照着打的，并不知道意思'),
        )
        for quote, summary in cases:
            with self.subTest(quote=quote):
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    (True, None))

    def test_logged_inference_stays_ungrounded_after_the_frame_strip(self):
        cases = (
            ('四点叫我起床', '她被四点叫起床，表示不满'),
            ('七点叫我的', '她提醒对方说好了七点叫她'),
            ('不适合初学者', '她自认是初学者'),
        )
        for quote, summary in cases:
            with self.subTest(quote=quote):
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    (False, 'summary_not_grounded_in_quote'))

    def test_logged_faithful_chinese_summaries_still_pass(self):
        cases = (
            ('你之前的都太难了不适合初学者', '她觉得之前的内容太难，不适合初学者'),
            ('明天早八有课', '她明天早上八点有课'),
            ('书坏了，停止复习', '她说书坏了，所以停止复习'),
            ('你明天早点叫我起床呗', '她希望明天被早点叫起床'),
        )
        for quote, summary in cases:
            with self.subTest(quote=quote):
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    (True, None))

    def test_logged_japanese_denial_passes_when_the_summary_keeps_it(self):
        quote = 'ぼくはつかれたから,ふくしゅうしたくない'
        summary = '她说「ふくしゅうしたくない」，不想复习'
        self.assertTrue(memory_candidate._has_grounding_negation(quote))
        self.assertTrue(memory_candidate._has_grounding_negation(summary))
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary=summary), raw_text=quote),
            (True, None))

    def test_logged_dropped_japanese_denial_is_a_polarity_mismatch(self):
        # ない used to be invisible. The summary that drops it then looked
        # consistent with the quote, which is the polarity error G5 must reject.
        quote = 'ふくしゅうしたくない'
        summary = '她说想复习'
        self.assertTrue(memory_candidate._has_grounding_negation(quote))
        self.assertFalse(memory_candidate._has_grounding_negation(summary))
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary=summary), raw_text=quote),
            (False, 'negation_polarity_mismatch'))

    def test_japanese_denial_forms_are_visible(self):
        for text in ('ない', 'なかった', 'ません', 'ませんでした',
                     'したくない', 'ふくしゅうしたくない'):
            with self.subTest(text=text):
                self.assertTrue(memory_candidate._has_grounding_negation(text))

    def test_bare_nu_is_not_a_denial(self):
        # ぬ as a classical auxiliary, as in 知らぬ, is intentionally not
        # recognized. Everyday words that merely contain ぬ are not denials.
        for text in ('いぬ', 'ぬるい', 'ぬいぐるみ', 'きぬ', '知らぬ'):
            with self.subTest(text=text):
                self.assertFalse(memory_candidate._has_grounding_negation(text))

    def test_japanese_nai_words_are_not_denials(self):
        for text in ('危ない', '少ない', 'もったいない', '汚い', 'きたない',
                     '情けない', 'さりげない', 'つまらない', 'しかたない',
                     'しようがない', 'しょうがない', 'くだらない', 'たまらない',
                     'あんない', 'しつない', 'この道は危ない', '水が少ない',
                     'これはもったいない', '今日はつまらない'):
            with self.subTest(text=text):
                self.assertFalse(memory_candidate._has_grounding_negation(text))
        for text in ('少なくない', '危なくない', 'つまらなくない',
                     'あんないしない', 'しつないじゃない'):
            with self.subTest(text=text):
                self.assertTrue(memory_candidate._has_grounding_negation(text))

    def test_chinese_grounding_is_unchanged_by_the_japanese_list(self):
        cases = (
            ('今天没亏', '她亏了', (False, 'negation_polarity_mismatch')),
            ('这次交易没亏钱', '她这次交易没亏钱', (True, None)),
            ('我很聪明的', '她说自己很聪明', (True, None)),
            ('四点叫我起床', '她被四点叫起床，表示不满',
             (False, 'summary_not_grounded_in_quote')),
        )
        for quote, summary, expected in cases:
            with self.subTest(quote=quote):
                self.assertEqual(
                    self.validate(self.candidate(quote=quote, summary=summary),
                                  raw_text=quote),
                    expected)

    def test_logged_kana_fragment_is_grounded_when_the_summary_quotes_it(self):
        # Production row: a hiragana quote has no CJK ideograph, so the old
        # character class left coverage at zero even when the summary quoted it.
        quote = 'きょはたのし'
        summary = '她说「きょはたのし」，今天很开心'
        self.assertEqual(
            self.validate(self.candidate(quote=quote, summary=summary), raw_text=quote),
            (True, None))

    def test_earlier_gate_failure_precedes_later_failure_without_mutation(self):
        cases = (
            ({'quote': '不存在的引用', 'summary': '', 'predicate': 'invalid'},
             'quote_not_verbatim'),
            ({'quote': '今天', 'summary': '', 'predicate': 'invalid'}, 'quote_too_short'),
            ({'summary': '她昨天没亏96元', 'predicate': 'invalid'}, 'number_value_mismatch'),
            ({'summary': '她昨天没亏69元', 'predicate': 'invalid'}, 'negation_polarity_mismatch'),
            ({'summary': '她昨天亏了69元', 'predicate': 'invalid'}, 'time_word_contradiction'),
            ({'predicate': 'invalid'}, 'invalid_predicate_or_summary'),
        )
        for overrides, reason in cases:
            with self.subTest(reason=reason):
                candidate = self.candidate(perspective='她其实很在意', **overrides)
                original = copy.deepcopy(candidate)
                self.assertEqual(self.validate(candidate), (False, reason))
                self.assertEqual(candidate, original)

    def test_apply_candidate_strips_utterance_observation_before_the_gate(self):
        seen = {}

        def capture(_cur, _user, _character, candidate):
            seen['candidate'] = dict(candidate)
            return False, 'quote_not_verbatim'

        candidate = self.candidate(utterance_mode='practice', context_excerpt='出题')
        with patch.object(memory_candidate, 'validate_candidate', capture):
            result = memory_candidate.apply_candidate(
                Mock(), 'u', 'c', candidate, occurred_at=None,
                event_id=1, operation_id='op')
        self.assertEqual(result['reason'], 'quote_not_verbatim')
        self.assertNotIn('utterance_mode', seen['candidate'])
        self.assertNotIn('context_excerpt', seen['candidate'])
        self.assertEqual(candidate['utterance_mode'], 'practice')
        self.assertEqual(candidate['context_excerpt'], '出题')

    def test_utterance_mode_does_not_change_gate_results(self):
        passing = self.candidate(utterance_mode='practice')
        self.assertEqual(self.validate(passing), (True, None))
        self.assertEqual(self.validate(self.candidate()), (True, None))
        rejected = '她住在火星上'
        self.assertEqual(
            self.validate(self.candidate(quote=rejected, utterance_mode='practice')),
            self.validate(self.candidate(quote=rejected)))

    def test_character_line_used_as_quote_is_not_verbatim(self):
        # The character line never enters raw_content. G2 still compares the
        # quote with the user turn only.
        user = '想和你聊一聊'
        character = '请用这个句型造一个完整的句子'
        self.assertNotIn(character, user)
        self.assertEqual(
            self.validate(self.candidate(quote=character), raw_text=user),
            (False, 'quote_not_verbatim'))

    def test_summary_built_from_the_character_line_fails_coverage(self):
        # G9's denominator is the user turn. Characters that exist only in the
        # preceding character line cannot be covered.
        user = '想和你聊一聊'
        character = '请用这个句型造一个完整的句子'
        self.assertEqual(
            self.validate(self.candidate(
                quote=user, summary='她说' + character, value='聊一聊'),
                raw_text=user),
            (False, 'summary_not_grounded_in_quote'))


if __name__ == '__main__':
    unittest.main()
