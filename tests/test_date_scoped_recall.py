"""Date-scoped recall: parse an explicit day, then read that day's facts."""
import os
import sys
import unittest
from datetime import date, datetime, timezone
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import date_expr
import smart_recall
from tests import test_cognitive_deterministic as acceptance


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


class DateUtcWallBoundsTests(unittest.TestCase):
    def test_singapore_midnight_is_the_previous_utc_wall_clock(self):
        self.assertEqual(
            smart_recall._date_utc_wall_bounds(date(2026, 11, 11)),
            (datetime(2026, 11, 10, 16, 0), datetime(2026, 11, 11, 16, 0)))


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'requires offline PostgreSQL fixture')
class RecallByDateTests(unittest.TestCase):
    setUpClass = classmethod(acceptance.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(acceptance.OfflineDatabaseTests.tearDownClass.__func__)
    tearDown = acceptance.OfflineDatabaseTests.tearDown
    sql = acceptance.OfflineDatabaseTests.sql
    source = acceptance.OfflineDatabaseTests.source
    ingest = acceptance.OfflineDatabaseTests.ingest
    run_cycle = acceptance.OfflineDatabaseTests.run_cycle
    report = acceptance.OfflineDatabaseTests.report

    def setUp(self):
        acceptance.OfflineDatabaseTests.setUp(self)
        self.stack.enter_context(patch('smart_recall.get_conn', return_value=self.database))

    def test_singapore_day_includes_the_utc_wall_clock_and_skips_the_gate(self):
        self.report('early', '我喜欢咖啡。')
        self.report('later', '我喜欢茶。')
        early = datetime(2026, 11, 10, 16, 30)
        later = datetime(2026, 11, 10, 18, 0)
        outside = datetime(2026, 11, 11, 16, 30)
        self.sql('UPDATE long_memory SET timestamp=%s WHERE content LIKE %s', (early, '%咖啡%'))
        self.sql('UPDATE long_memory SET timestamp=%s WHERE content LIKE %s', (later, '%茶%'))
        self.sql(
            '''INSERT INTO long_memory (user_id, character_id, content, category, timestamp)
               VALUES ('u', 'c', '没有出处的句子', '其他', %s)''',
            (early,))
        self.database.commit()
        before = self.sql(
            'SELECT id, mention_count FROM long_memory ORDER BY id')
        with patch('builtins.print') as printed:
            rows = smart_recall.recall_by_date('u', 'c', date(2026, 11, 11))
        after = self.sql(
            'SELECT id, mention_count FROM long_memory ORDER BY id')
        self.assertEqual(after, before)
        self.assertEqual(len(rows), 2)
        self.assertIn('咖啡', rows[0]['content'])
        self.assertIn('茶', rows[1]['content'])
        self.assertTrue(all(row['recall_kind'] == 'date_scoped' for row in rows))
        self.assertNotIn('没有出处的句子', [row['content'] for row in rows])
        trace = '\n'.join(
            str(call.args[0]) for call in printed.call_args_list
            if call.args and str(call.args[0]).startswith('[recall_trace]'))
        self.assertIn('date=2026-11-11', trace)
        self.assertIn(f'id={rows[0]["id"]}', trace)
        self.assertNotIn('咖啡', trace)
        self.assertNotIn('没有出处', trace)
        self.sql('UPDATE long_memory SET timestamp=%s WHERE content LIKE %s', (outside, '%咖啡%'))
        self.database.commit()
        kept = smart_recall.recall_by_date('u', 'c', date(2026, 11, 11))
        self.assertEqual(len(kept), 1)
        self.assertIn('茶', kept[0]['content'])


if __name__ == '__main__':
    unittest.main()
