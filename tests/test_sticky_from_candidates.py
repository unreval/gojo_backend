"""Sticky notes projected from candidate time_scope, not keyword classification."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'gojo_backend'))

import memory_lifecycle  # noqa: E402
from config import CN_TZ  # noqa: E402
from memory_lifecycle import sticky_ttl_from_time_scope  # noqa: E402


def _end_of_local_day(now, days):
    local = now.astimezone(CN_TZ)
    end = (local + timedelta(days=days)).replace(
        hour=23, minute=59, second=59, microsecond=0)
    return int((end - local).total_seconds())


class StickyTtlTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 15, 0, tzinfo=CN_TZ)

    def test_tomorrow_ends_at_the_next_local_day(self):
        self.assertEqual(
            sticky_ttl_from_time_scope('明天', self.now),
            _end_of_local_day(self.now, 1))

    def test_day_after_tomorrow_is_two_days_out(self):
        self.assertEqual(
            sticky_ttl_from_time_scope('后天', self.now),
            _end_of_local_day(self.now, 2))

    def test_next_week_and_weekday_are_eight_days_out(self):
        expected = _end_of_local_day(self.now, 8)
        self.assertEqual(sticky_ttl_from_time_scope('下周', self.now), expected)
        self.assertEqual(sticky_ttl_from_time_scope('周一', self.now), expected)

    def test_tonight_and_afternoon_end_today(self):
        expected = _end_of_local_day(self.now, 0)
        self.assertEqual(sticky_ttl_from_time_scope('今晚', self.now), expected)
        self.assertEqual(sticky_ttl_from_time_scope('下午', self.now), expected)

    def test_later_offset_wins_over_a_same_day_word(self):
        self.assertEqual(
            sticky_ttl_from_time_scope('明天晚上', self.now),
            _end_of_local_day(self.now, 1))

    def test_past_words_do_not_produce_a_ttl(self):
        for text in ('昨天', '前天', '上周', '刚才', '刚刚', '之前', '昨天晚上'):
            with self.subTest(time_scope=text):
                self.assertIsNone(sticky_ttl_from_time_scope(text, self.now))

    def test_empty_and_missing_time_scope_do_not_produce_a_ttl(self):
        self.assertIsNone(sticky_ttl_from_time_scope('', self.now))
        self.assertIsNone(sticky_ttl_from_time_scope('   ', self.now))
        self.assertIsNone(sticky_ttl_from_time_scope(None, self.now))

    def test_ttl_is_clamped_to_the_configured_maximum(self):
        natural = _end_of_local_day(self.now, 1)
        capped = natural // 2
        self.assertGreater(natural, capped)
        self.assertGreater(capped, memory_lifecycle.COGNITIVE_STICKY_NOTE_MIN_TTL_SECONDS)
        with patch.object(memory_lifecycle, 'COGNITIVE_STICKY_NOTE_MAX_TTL_SECONDS', capped):
            self.assertEqual(sticky_ttl_from_time_scope('明天', self.now), capped)

    def test_ttl_is_clamped_to_the_configured_minimum(self):
        late = datetime(2026, 11, 6, 23, 59, 50, tzinfo=CN_TZ)
        raw = _end_of_local_day(late, 0)
        floor = raw + 50
        self.assertLess(raw, floor)
        with patch.object(memory_lifecycle, 'COGNITIVE_STICKY_NOTE_MIN_TTL_SECONDS', floor), \
                patch.object(
                    memory_lifecycle, 'COGNITIVE_STICKY_NOTE_MAX_TTL_SECONDS', floor + 1000):
            self.assertEqual(sticky_ttl_from_time_scope('今晚', late), floor)


if __name__ == '__main__':
    unittest.main()
