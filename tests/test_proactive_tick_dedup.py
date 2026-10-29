"""Scheduler dedup follows the trigger bucket, not the shared context prefix."""
from datetime import datetime, timedelta, timezone
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import proactive_scheduler


ZONE = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 29, 10, 5, tzinfo=ZONE)
SHARED_PREFIX = '角色答应在特定时间联系。原话:「'


def _due(promise_id, trigger_at, quote):
    return {
        'id': promise_id,
        'character_id': 'gojo',
        'user_id': 'test-user',
        'trigger_kind': 'once',
        'context': SHARED_PREFIX + quote + '」',
        'origin_text': quote,
        'trigger_at': trigger_at,
    }


class ProactiveTickDedupTests(unittest.TestCase):
    def tick(self, due):
        with patch.object(proactive_scheduler, '_now', return_value=NOW), \
                patch.object(proactive_scheduler.db_promise, 'get_due_promises',
                             return_value=due), \
                patch.object(proactive_scheduler, 'generate_from_promise',
                             return_value=(1, 'jp')) as generate, \
                patch.object(proactive_scheduler.db_promise, 'mark_fired') as fired:
            proactive_scheduler._tick()
        return generate, fired

    def test_same_trigger_sends_one_message(self):
        seven = datetime(2026, 10, 29, 7, 0, tzinfo=ZONE)
        generate, fired = self.tick([
            _due(61, seven, '七点叫你'),
            _due(62, seven, '七点叫你'),
        ])
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args[0][0]['id'], 61)
        fired.assert_called_once()
        self.assertEqual(fired.call_args[0][0], 62)

    def test_seven_and_ten_both_send(self):
        seven = datetime(2026, 10, 29, 7, 0, tzinfo=ZONE)
        ten = datetime(2026, 10, 29, 10, 0, tzinfo=ZONE)
        generate, fired = self.tick([
            _due(53, seven, '七点叫你'),
            _due(52, ten, '十点了我叫你'),
        ])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            [call.args[0]['id'] for call in generate.call_args_list],
            [53, 52])
        fired.assert_not_called()

    def test_same_trigger_different_users_both_send(self):
        seven = datetime(2026, 10, 29, 7, 0, tzinfo=ZONE)
        other = _due(71, seven, '七点叫你')
        other['user_id'] = 'other-user'
        generate, fired = self.tick([
            _due(61, seven, '七点叫你'),
            other,
        ])
        self.assertEqual(generate.call_count, 2)
        self.assertEqual(
            [call.args[0]['user_id'] for call in generate.call_args_list],
            ['test-user', 'other-user'])
        fired.assert_not_called()


if __name__ == '__main__':
    unittest.main()
