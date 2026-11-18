"""Date-scoped recall: parse an explicit day, then read that day's facts."""
import os
import sys
import unittest
from datetime import date, datetime, timezone

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import date_expr


def _local(year, month, day, hour=12):
    return datetime(year, month, day, hour, 0)


class ParseDateExpressionTests(unittest.TestCase):
    def test_arabic_day_with_hao_or_ri(self):
        today = _local(2026, 11, 18)
        for text, expected in (('11号', date(2026, 11, 11)), ('11日', date(2026, 11, 11))):
            with self.subTest(text=text):
                self.assertEqual(date_expr.parse_date_expression(text, today), expected)

    def test_chinese_day_with_hao_or_ri(self):
        today = _local(2026, 11, 18)
        for text, expected in (
                ('一号', date(2026, 11, 1)),
                ('十一号', date(2026, 11, 11)),
                ('十一日', date(2026, 11, 11)),
                ('二十三号', date(2026, 10, 23)),
                ('三十一日', date(2026, 10, 31))):
            with self.subTest(text=text):
                self.assertEqual(date_expr.parse_date_expression(text, today), expected)

    def test_arabic_month_and_day(self):
        today = _local(2026, 11, 18)
        for text in ('11月11号', '11月11日'):
            with self.subTest(text=text):
                self.assertEqual(
                    date_expr.parse_date_expression(text, today), date(2026, 11, 11))

    def test_chinese_month_and_day(self):
        today = _local(2026, 11, 18)
        for text in ('十一月十一号', '十一月十一日'):
            with self.subTest(text=text):
                self.assertEqual(
                    date_expr.parse_date_expression(text, today), date(2026, 11, 11))

    def test_relative_days(self):
        today = _local(2026, 11, 18)
        for text, expected in (
                ('今天', date(2026, 11, 18)),
                ('昨天', date(2026, 11, 17)),
                ('前天', date(2026, 11, 16)),
                ('大前天', date(2026, 11, 15))):
            with self.subTest(text=text):
                self.assertEqual(date_expr.parse_date_expression(text, today), expected)

    def test_clock_time_is_not_a_day(self):
        today = _local(2026, 11, 18)
        for text in ('十一点', '11点'):
            with self.subTest(text=text):
                self.assertIsNone(date_expr.parse_date_expression(text, today))

    def test_month_name_without_a_day_is_not_a_day(self):
        self.assertIsNone(date_expr.parse_date_expression('十一月', _local(2026, 11, 18)))

    def test_line_number_and_question_words_are_not_days(self):
        today = _local(2026, 11, 18)
        for text in ('3号线', '几号', '多少号'):
            with self.subTest(text=text):
                self.assertIsNone(date_expr.parse_date_expression(text, today))

    def test_text_without_a_day_word_is_none(self):
        self.assertIsNone(date_expr.parse_date_expression('你就是臭手', _local(2026, 11, 18)))

    def test_day_only_uses_the_latest_day_not_after_today(self):
        self.assertEqual(
            date_expr.parse_date_expression('十一号', _local(2026, 11, 18)),
            date(2026, 11, 11))
        self.assertEqual(
            date_expr.parse_date_expression('十一号', _local(2026, 12, 5)),
            date(2026, 11, 11))

    def test_older_than_ninety_days_is_none(self):
        self.assertIsNone(date_expr.parse_date_expression('8月19日', _local(2026, 11, 18)))
        self.assertEqual(
            date_expr.parse_date_expression('8月20日', _local(2026, 11, 18)),
            date(2026, 8, 20))

    def test_future_date_is_none(self):
        parsed = date_expr.parse_date_expression('12月1日', _local(2026, 11, 18))
        self.assertIsNone(parsed)
        self.assertNotEqual(parsed, date(2026, 12, 1))

    def test_first_expression_wins(self):
        self.assertEqual(
            date_expr.parse_date_expression('昨天提到十一号', _local(2026, 11, 18)),
            date(2026, 11, 17))


if __name__ == '__main__':
    unittest.main()
