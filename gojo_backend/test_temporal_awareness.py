import sys
import types
import unittest
from datetime import datetime, timedelta, timezone


fake_db = types.ModuleType('db')
fake_db.get_conn = lambda: (_ for _ in ()).throw(
    AssertionError('database should not be used by these unit tests')
)
sys.modules.setdefault('db', fake_db)

from temporal_awareness import (  # noqa: E402
    build_calendar_grounding,
    build_memory_context,
    build_prompt_context,
    build_relationship_context,
    classify_gap,
    format_elapsed,
    find_reply_calendar_conflict,
    serialize_snapshot,
)


class TemporalAwarenessTextTests(unittest.TestCase):
    def _snapshot(self, elapsed):
        now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        last = now - timedelta(seconds=elapsed)
        return {
            'has_history': True,
            'user_id': 'u1',
            'character_id': 'gojo',
            'now_utc': now,
            'last_interaction_at': last,
            'last_user_message_at': last - timedelta(minutes=2),
            'last_assistant_message_at': last,
            'first_interaction_at': now - timedelta(days=30),
            'elapsed_seconds_since_last_interaction': elapsed,
            'elapsed_label': format_elapsed(elapsed),
            'gap_bucket': classify_gap(elapsed),
            'longest_gap_seconds': elapsed,
            'interaction_count': 12,
            'last_initiator': 'assistant',
            'last_source': 'chat_text',
        }

    def test_gap_classification_and_labels(self):
        self.assertEqual(classify_gap(30), 'continuous')
        self.assertEqual(classify_gap(26 * 3600), 'overnight')
        self.assertEqual(classify_gap(3 * 86400), 'few_days')
        self.assertEqual(format_elapsed(3 * 86400 + 2 * 3600), '3天2小时')

    def test_prompt_context_carries_elapsed_time_without_linear_emotion(self):
        snap = self._snapshot(3 * 86400 + 2 * 3600)
        text = build_prompt_context('u1', 'gojo', snapshot=snap)
        self.assertIn('3天2小时', text)
        self.assertIn('真实经过时间', text)
        self.assertIn('严禁把时间间隔直接换算成情绪或关系分数', text)

    def test_memory_and_relationship_boundaries(self):
        snap = self._snapshot(26 * 3600)
        memory_text = build_memory_context(snap)
        relationship_text = build_relationship_context(snap)
        self.assertIn('距离上一轮有记录互动', memory_text)
        self.assertIn('不要单独提取', memory_text)
        self.assertIn('时间跨度可以帮助判断等待', relationship_text)
        self.assertIn('不直接改变 warmth/trust/attachment/passion', relationship_text)

    def test_first_contact_context_is_not_false_new_relationship(self):
        now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)
        snap = {
            'has_history': False,
            'user_id': 'u1',
            'character_id': 'gojo',
            'now_utc': now,
            'elapsed_seconds_since_last_interaction': None,
            'elapsed_label': '首次记录',
            'gap_bucket': 'first_contact',
            'interaction_count': 0,
        }
        text = build_prompt_context('u1', 'gojo', snapshot=snap)
        self.assertIn('第一次记录', text)
        self.assertIn('不要凭空断言你们"刚认识"', text)

    def test_evening_treats_today_noon_as_past_and_next_noon_as_tomorrow(self):
        now_utc = datetime(2026, 9, 12, 10, 13, tzinfo=timezone.utc)
        text = build_calendar_grounding(now_utc, '我要这样忍到明天中午吗？')

        self.assertIn('当前：2026-09-12 18:13', text)
        self.assertIn('今天中午：2026-09-12 12:00，已过去6小时13分钟', text)
        self.assertIn('下一次中午：2026-09-13 12:00，还有17小时47分钟', text)
        self.assertIn('“明天中午” = 2026-09-13 12:00', text)
        self.assertIn('绝不是今天中午', text)
        self.assertIn('严禁说“今天中午还有几小时”', text)

    def test_morning_keeps_today_noon_in_the_future(self):
        now_utc = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)
        text = build_calendar_grounding(now_utc, '忍到中午')

        self.assertIn('当前：2026-09-12 09:00', text)
        self.assertIn('今天中午：2026-09-12 12:00，还有3小时', text)
        self.assertIn('下一次中午：2026-09-12 12:00，还有3小时', text)
        self.assertIn('今天 12:00 尚未过去', text)

    def test_evening_rejects_noon_as_an_unlabelled_future_deadline(self):
        now_utc = datetime(2026, 9, 12, 10, 13, tzinfo=timezone.utc)

        self.assertEqual(
            find_reply_calendar_conflict(
                '', 'じゃあ昼まで我慢すれば。あと何時間かだろ。', now_utc),
            'past_noon_used_as_future_deadline',
        )
        self.assertIsNone(
            find_reply_calendar_conflict(
                '', 'じゃあ明日の昼まで我慢すれば。', now_utc)
        )

    def test_reply_cannot_rewrite_tomorrow_noon_as_today(self):
        now_utc = datetime(2026, 9, 12, 10, 13, tzinfo=timezone.utc)

        self.assertEqual(
            find_reply_calendar_conflict(
                '我要这样忍到明天中午吗？',
                '今日の昼だろ。今まだ夕方だぞ。',
                now_utc,
            ),
            'tomorrow_noon_rewritten_as_today',
        )
        self.assertIsNone(
            find_reply_calendar_conflict(
                '我要这样忍到明天中午吗？',
                '今日の昼じゃなくて、明日の昼だ。',
                now_utc,
            )
        )

    def test_morning_allows_today_noon_as_a_future_deadline(self):
        now_utc = datetime(2026, 9, 12, 1, 0, tzinfo=timezone.utc)
        self.assertIsNone(
            find_reply_calendar_conflict(
                '', 'じゃあ昼まで我慢すれば。', now_utc)
        )

    def test_snapshot_serialization_keeps_background_jobs_json_safe(self):
        snap = self._snapshot(3600)
        serialized = serialize_snapshot(snap)
        self.assertIsInstance(serialized['now_utc'], str)
        self.assertIsInstance(serialized['last_interaction_at'], str)
        self.assertEqual(serialized['elapsed_seconds_since_last_interaction'], 3600)


if __name__ == '__main__':
    unittest.main()
