"""Preserve delayed-reply cooldowns when sharing them with shadow work."""
from pathlib import Path
import sys
import unittest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gojo_backend'))

from delayed_reply_retry import PROVIDER_PAUSE_SECONDS, failure_retry_seconds


class DelayedReplyRetryPolicyTests(unittest.TestCase):
    def test_failure_cooldowns_preserve_delayed_reply_policy(self):
        self.assertEqual([failure_retry_seconds(count) for count in range(1, 5)],
                         [120, 600, 1800, 7200])

    def test_authentication_failure_keeps_fifteen_minute_minimum(self):
        self.assertEqual([failure_retry_seconds(count, auth_failed=True)
                          for count in range(1, 5)], [900, 900, 1800, 7200])

    def test_fifth_and_later_failures_exhaust_the_policy(self):
        for count in (5, 6, 20):
            with self.subTest(count=count):
                self.assertIsNone(failure_retry_seconds(count))
                self.assertIsNone(failure_retry_seconds(count, auth_failed=True))

    def test_provider_circuit_pause_remains_ten_minutes(self):
        self.assertEqual(PROVIDER_PAUSE_SECONDS, 600)


if __name__ == '__main__':
    unittest.main()
