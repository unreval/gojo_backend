"""Versioned recall identity must survive display edits."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gojo_backend'))

from recall_candidates import _candidate_from_row, collapse_candidates


class SemanticDedupTests(unittest.TestCase):
    def _fact(self, row_id, content, source, value):
        return _candidate_from_row('fact', {
            'id': row_id, 'content': content, 'projection_version': 'role_view_v2',
            'source_event_ids': [source], 'semantic_payload': {
                'source_event_id': source, 'source_scope': 'gojo',
                'subject_ref': 'user:u', 'predicate': 'reported_preference',
                'object': '咖啡', 'value': value,
            },
        })

    def test_same_semantics_collapse_after_display_edit(self):
        first = self._fact(1, '她喜欢咖啡。', 'raw', '喜欢')
        second = self._fact(2, '这条的展示文字已改。', 'raw', '喜欢')
        self.assertEqual(len(collapse_candidates([first, second])), 1)

    def test_same_display_does_not_collapse_different_semantics(self):
        first = self._fact(1, '她喜欢咖啡。', 'raw', '喜欢')
        second = self._fact(2, '她喜欢咖啡。', 'raw', '讨厌')
        self.assertEqual(len(collapse_candidates([first, second])), 2)

    def test_told_ids_include_source_table(self):
        a = _candidate_from_row('told', {'id': 1, 'content': '她说过咖啡',
                                         'source_table': 'long_memory'})
        b = _candidate_from_row('told', {'id': 1, 'content': '她说过咖啡',
                                         'source_table': 'bond_memory'})
        self.assertNotEqual(a.candidate_id, b.candidate_id)


if __name__ == '__main__':
    unittest.main()
