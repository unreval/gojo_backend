import logging
import os
import sys
import unittest
from datetime import datetime, timezone


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from smart_recall import _format_expiry, format_recall_for_prompt  # noqa: E402


class FormatExpiryTests(unittest.TestCase):
    def test_none_and_empty_stay_unset(self):
        with self.assertNoLogs('smart_recall', level='WARNING'):
            self.assertEqual(_format_expiry(None), '未设期限')
            self.assertEqual(_format_expiry(''), '未设期限')

    def test_datetime_keeps_existing_clock_display(self):
        exp = datetime(2026, 10, 25, 22, 42, tzinfo=timezone.utc)
        with self.assertNoLogs('smart_recall', level='WARNING'):
            self.assertEqual(_format_expiry(exp), '2026-10-25 22:42')

    def test_iso_string_and_zulu_parse_without_raising(self):
        with self.assertLogs('smart_recall', level='WARNING') as captured:
            self.assertEqual(
                _format_expiry('2026-10-25T22:42:00+00:00', record_id=7),
                '2026-10-25 22:42',
            )
            self.assertEqual(
                _format_expiry('2026-10-25T14:42:00Z', record_id=8),
                '2026-10-25 14:42',
            )
        self.assertIn('id=7', captured.output[0])
        self.assertIn('id=8', captured.output[1])

    def test_garbage_string_is_shown_and_does_not_raise(self):
        with self.assertLogs('smart_recall', level='WARNING') as captured:
            self.assertEqual(
                _format_expiry('not-a-date!!', record_id=9),
                'not-a-date!!',
            )
        self.assertIn('id=9', captured.output[0])

    def test_non_datetime_object_does_not_raise(self):
        text = _format_expiry(object())
        self.assertIsInstance(text, str)
        self.assertTrue(text)
        self.assertLessEqual(len(text), 16)

    def test_string_expiry_still_renders_the_sticky(self):
        with self.assertLogs('smart_recall', level='WARNING') as captured:
            memory_text, bond_text, told_text = format_recall_for_prompt({
                'sticky_notes': [{
                    'id': 11,
                    'content': '记得带伞',
                    'expires_at': '2026-10-25T22:42:00+08:00',
                }, {
                    'id': 12,
                    'content': '垃圾期限也要留下',
                    'expires_at': 'totally-garbage',
                }],
            })
        self.assertIn('便利贴备忘', memory_text)
        self.assertIn('记得带伞', memory_text)
        self.assertIn('[到期 2026-10-25 22:42]', memory_text)
        self.assertIn('垃圾期限也要留下', memory_text)
        self.assertIn('[到期 totally-garbage]', memory_text)
        self.assertEqual(bond_text, '')
        self.assertEqual(told_text, '')
        self.assertTrue(any('id=11' in line for line in captured.output))
        self.assertTrue(any('id=12' in line for line in captured.output))


if __name__ == '__main__':
    logging.basicConfig(level='WARNING')
    unittest.main()
