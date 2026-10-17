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

    def test_matching_quote_and_summary_pass_without_mutation(self):
        candidate = self.candidate()
        original = copy.deepcopy(candidate)
        self.assertEqual(self.validate(candidate), (True, None))
        self.assertEqual(candidate, original)

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

    def test_number_may_be_omitted_from_summary(self):
        self.assertEqual(self.validate(self.candidate(summary='她今天买咖啡亏钱了')),
                         (True, None))

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

    def test_double_negatives_are_not_treated_as_plain_denial(self):
        for quote, summary in (
                ('我今天不得不去上班', '她今天去上班'),
                ('我今天去上班了', '她今天不得不去上班'),
                ('我不是不喜欢咖啡', '她喜欢咖啡')):
            with self.subTest(quote=quote, summary=summary):
                self.assertEqual(self.validate(self.candidate(quote=quote, summary=summary),
                                               raw_text=quote), (True, None))

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


if __name__ == '__main__':
    unittest.main()
