"""Offline coverage of the existing temporal_awareness time gate."""
import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _load_temporal():
    config = types.ModuleType('config')
    config.CN_TZ = timezone(timedelta(hours=8))
    database = types.ModuleType('db')
    database.get_conn = lambda: None
    source = Path(__file__).resolve().parents[1] / 'gojo_backend' / 'temporal_awareness.py'
    spec = importlib.util.spec_from_file_location('_clock24_under_test', source)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {'config': config, 'db': database}):
        spec.loader.exec_module(module)
    return module


class Clock24IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporal = _load_temporal()
        cls.shanghai = timezone(timedelta(hours=8))

    def clock(self, local):
        utc = local.astimezone(timezone.utc)
        return self.temporal.Clock24.from_snapshot({'now_utc': utc}, self.shanghai)

    def test_every_minute_of_day_uses_the_same_snapshot(self):
        day = datetime(2026, 10, 4, tzinfo=self.shanghai)
        for index in range(24 * 60):
            local = day + timedelta(minutes=index)
            clock = self.clock(local)
            with self.subTest(minute=index):
                self.assertEqual(clock.now_local.hour * 60 + clock.now_local.minute, index)
                self.assertIsNone(self.temporal.find_current_clock_conflict(
                    f'现在是{local:%H:%M}', clock))
                wrong = local.replace(hour=(local.hour + 12) % 24)
                self.assertEqual(self.temporal.find_current_clock_conflict(
                    f'现在是{wrong:%H:%M}', clock), 'current_clock_mismatch')

    def test_cross_midnight_month_year_and_leap_day(self):
        clock = self.clock(datetime(2026, 12, 31, 23, 59, tzinfo=self.shanghai))
        self.assertEqual(clock.utc_after(minutes=2).astimezone(self.shanghai).isoformat(),
                         '2027-01-01T00:01:00+08:00')
        leap = self.clock(datetime(2028, 2, 29, 0, 1, tzinfo=self.shanghai))
        self.assertEqual(leap.parse_expression('今天 00:01')['date'].isoformat(),
                         '2028-02-29')
        self.assertEqual(leap.parse_expression('昨天 23:59')['date'].isoformat(),
                         '2028-02-28')

    def test_generation_crossing_midnight_fails_before_commit(self):
        start = datetime(2026, 10, 4, 23, 59, 59, tzinfo=self.shanghai)
        self.assertEqual(self.temporal.find_commit_clock_conflict(
            '', '现在是23:59', {'now_utc': start.astimezone(timezone.utc)}, 2),
            'time_snapshot_stale')
        same_day = datetime(2026, 10, 4, 17, 20, tzinfo=self.shanghai)
        self.assertEqual(self.temporal.find_commit_clock_conflict(
            '', '现在是17:20', {'now_utc': same_day.astimezone(timezone.utc)}, 180),
            'current_clock_mismatch')

    def test_ambiguous_five_and_explicit_dayparts(self):
        clock = self.clock(datetime(2026, 10, 4, 17, 20, tzinfo=self.shanghai))
        self.assertEqual(clock.parse_expression('五点')['hours'], (5, 17))
        self.assertTrue(clock.parse_expression('五点')['date_unspecified'])
        self.assertEqual(clock.parse_expression('凌晨五点')['hours'], (5,))
        self.assertEqual(clock.parse_expression('下午五点')['hours'], (17,))
        self.assertEqual(clock.parse_expression('下午5:20')['hours'], (17,))
        self.assertEqual(clock.parse_expression('下午5:20')['minute'], 20)
        self.assertEqual(clock.parse_expression('午後五時半')['minute'], 30)
        self.assertEqual(clock.parse_expression('现在是25:99')['error'],
                         'invalid_clock_expression')

    def test_current_claims_and_normal_expressions(self):
        clock = self.clock(datetime(2026, 10, 4, 17, 20, tzinfo=self.shanghai))
        self.assertEqual(self.temporal.find_current_clock_conflict(
            '现在是凌晨五点', clock), 'current_daypart_mismatch')
        self.assertEqual(self.temporal.find_current_clock_conflict(
            '现在是明天', clock), 'current_date_mismatch')
        self.assertEqual(self.temporal.find_current_clock_conflict(
            '现在是25:99', clock), 'invalid_clock_expression')
        for ordinary in ('我想午睡，下午五点叫我', '今晚补觉', '你说现在是凌晨五点',
                         '假如现在是凌晨五点', '现在不是凌晨五点', '昨晚九点吃了饭'):
            with self.subTest(text=ordinary):
                self.assertIsNone(self.temporal.find_current_clock_conflict(
                    ordinary, clock))

    def test_evening_now_does_not_rewrite_quoted_early_morning_history(self):
        clock = self.clock(datetime(2026, 10, 4, 17, 24, tzinfo=self.shanghai))
        self.assertEqual(self.temporal.find_current_clock_conflict(
            '现在是17:24', clock), None)
        self.assertEqual(self.temporal.find_current_clock_conflict(
            '现在是凌晨五点', clock), 'current_daypart_mismatch')
        for historical in ('你早上说“现在是凌晨五点”',
                           '引用你刚才说的“现在是凌晨五点”',
                           '我今天午睡过，现在聊一会儿'):
            with self.subTest(historical=historical):
                self.assertIsNone(self.temporal.find_current_clock_conflict(
                    historical, clock))

    def test_old_noon_rule_remains_in_the_output_gate(self):
        current = datetime(2026, 10, 4, 15, 0, tzinfo=self.shanghai)
        self.assertEqual(self.temporal.find_reply_calendar_conflict(
            '明天中午', '今天中午见', now_utc=current),
            'tomorrow_noon_rewritten_as_today')
        self.assertIsNone(self.temporal.find_reply_calendar_conflict(
            '今天中午已经过去了', '是的，今天中午已经过去了',
            now_utc=current))

    def test_same_utc_can_have_multiple_display_zones(self):
        utc = datetime(2026, 10, 4, 7, 30, tzinfo=timezone.utc)
        shanghai = self.temporal.Clock24.from_snapshot({'now_utc': utc}, self.shanghai)
        tokyo = self.temporal.Clock24.from_snapshot(
            {'now_utc': utc}, timezone(timedelta(hours=9)))
        self.assertEqual(shanghai.now_utc, tokyo.now_utc)
        self.assertEqual((shanghai.now_local.hour, tokyo.now_local.hour), (15, 16))

    def test_dst_gap_and_fold_are_not_guessed(self):
        try:
            eastern = ZoneInfo('America/New_York')
        except ZoneInfoNotFoundError:
            self.skipTest('IANA tzdata unavailable')
        clock = self.temporal.Clock24.from_snapshot(
            {'now_utc': datetime(2026, 3, 8, 6, tzinfo=timezone.utc)}, eastern)
        self.assertEqual(clock.local_candidates(datetime(2026, 3, 8).date(), 2, 30), [])
        self.assertEqual(len(clock.local_candidates(
            datetime(2026, 11, 1).date(), 1, 30)), 2)


if __name__ == '__main__':
    unittest.main()
