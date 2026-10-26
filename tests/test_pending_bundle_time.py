"""Delayed-reply context must keep the gap between send time and now."""
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import reply_availability

CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 26, 6, 3, tzinfo=CN)
SENT = datetime(2026, 10, 25, 22, 46, tzinfo=CN)


class PendingBundleTimeTests(unittest.TestCase):
    def bundle(self, **extra):
        bundle = {
            'id': 4,
            'activity_title': '深夜事务处理',
            'reply_state': 'soft_busy',
            'pending_count': 1,
            'pending_text': '明天记得六点叫我起床去考试！听到了吗！',
        }
        bundle.update(extra)
        return bundle

    def test_elapsed_time_replaces_the_just_now_instruction(self):
        text = reply_availability.format_pending_bundle_context(self.bundle(
            pending_prompt_events=[{
                'text': '明天记得六点叫我起床去考试！听到了吗！',
                'timestamp': SENT,
            }],
        ), now=NOW)
        self.assertNotIn('刚刚一起看到', text)
        self.assertIn('当前时刻：2026-10-26 06:03（中国时区）', text)
        self.assertIn('最后一条积压消息发送于 2026-10-25 22:46', text)
        self.assertIn('距今 7 小时 17 分', text)
        self.assertIn('还没到 / 正好是现在 / 已经过了', text)
        self.assertIn('不要假装做过', text)
        self.assertIn('不要默认事情已经发生', text)
        for rule in (
                '不是主动搭讪',
                '不要逐条复读',
                'messages[]',
                'visual_summary',
        ):
            self.assertIn(rule, text)
        self.assertIn('[发送时间 2026-10-25 22:46:00+08:00]', text)

    def test_missing_send_time_is_not_treated_as_just_now(self):
        text = reply_availability.format_pending_bundle_context(
            self.bundle(), now=NOW)
        self.assertIn('当前时刻：2026-10-26 06:03（中国时区）', text)
        self.assertIn('没有单独的发送时间', text)
        self.assertIn('不要把它当成刚刚发生', text)
        self.assertNotIn('刚刚一起看到', text)


if __name__ == '__main__':
    unittest.main()
