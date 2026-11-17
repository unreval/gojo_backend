"""G8 accepts a perspective that contains 我 without leading with it."""
import copy
import os
import sys
import unittest
from unittest.mock import Mock, patch

BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import candidate_producer as producer
import memory_candidate
from cognitive_output import STICKY_NOTE_EMOTIONS


class PerspectiveGateTests(unittest.TestCase):
    raw_text = '今天买咖啡亏了69元'

    def candidate(self, perspective):
        return {
            'source_event_id': 'source-1',
            'quote': self.raw_text,
            'summary': '她今天买咖啡亏了69元',
            'predicate': 'reported_fact',
            'object': '交易结果',
            'value': '亏了69元',
            'perspective': perspective,
        }

    def validate(self, candidate):
        raw = {'event_id': 'source-1', 'role': 'user', 'content': self.raw_text,
               'metadata': {}}
        cur = Mock()
        cur.fetchone.return_value = ({'content': self.raw_text, 'source_chat_id': 'c'},)
        with patch.object(memory_candidate, 'get_active_events_by_ids', return_value=[raw]), \
                patch.object(memory_candidate, 'cognitive_source_matches_scope', return_value=True):
            return memory_candidate.validate_candidate(cur, 'u', 'c', candidate)

    def test_perspective_with_wo_later_in_the_line_is_kept(self):
        candidate = self.candidate('这账记得比谁都清楚，我可没那记性。')
        original = copy.deepcopy(candidate)
        self.assertEqual(self.validate(candidate), (True, None))
        self.assertEqual(candidate, original)

    def test_perspective_without_wo_is_dropped(self):
        candidate = self.candidate('顺着杆子就往上爬，这脾气倒有点意思。')
        self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
        self.assertNotIn('perspective', candidate)

    def test_perspective_opening_on_her_is_dropped(self):
        candidate = self.candidate('她好像不太高兴，我得小心点')
        self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
        self.assertNotIn('perspective', candidate)

    def test_assertion_inside_a_self_line_is_still_dropped(self):
        candidate = self.candidate('我觉得她心里不舒服')
        self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
        self.assertNotIn('perspective', candidate)

    def test_previously_pinned_perspective_cases_keep_their_outcome(self):
        dropped = (
            '她其实很在意',
            '这件事很重要',
            '我们想认真听她说',
            None,
            '',
            ' \t\n',
            1,
            ['我想听她说'],
            {'text': '我想听她说'},
            '我想' + '听' * 39,
        )
        for perspective in dropped:
            with self.subTest(perspective=perspective):
                candidate = self.candidate(perspective)
                self.assertEqual(self.validate(candidate), (True, 'perspective_dropped'))
                self.assertNotIn('perspective', candidate)
        kept = self.candidate('我想认真听她说')
        self.assertEqual(self.validate(kept), (True, None))
        self.assertEqual(kept['perspective'], '我想认真听她说')


class PerspectivePromptWordingTests(unittest.TestCase):
    def test_flag_off_system_prompt_stays_byte_identical(self):
        expected = producer._SYSTEM % ', '.join(sorted(producer._PREDICATES))
        with patch('characters_data._loader.load_core',
                   side_effect=AssertionError('load_core')):
            self.assertEqual(producer._system_prompt('gojo'), expected)

    def test_flag_on_system_prompt_uses_the_relaxed_perspective_wording(self):
        with patch('config.STICKY_VIEW_ENABLED', True):
            system = producer._system_prompt('gojo')
        self.assertIn(
            '句子里必须出现“我”，但不必放在开头；不能以“我们”开头，也不能以“她”“他”“你”开头。',
            system)
        self.assertNotIn('必须以“我”开头', system)
        self.assertIn('、'.join(sorted(STICKY_NOTE_EMOTIONS)), system)


if __name__ == '__main__':
    unittest.main()
