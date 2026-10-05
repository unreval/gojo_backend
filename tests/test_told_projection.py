"""Told projection keeps canonical fact identity and audience scope."""
import os
import sys
import unittest


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

from told_projection import canonical_told_view, told_display  # noqa: E402


class ToldProjectionTests(unittest.TestCase):
    def _row(self, scope='gojo', **semantic):
        return {
            'id': 7, 'content': 'display text is irrelevant',
            'source_character_id': scope, 'projection_version': 'role_view_v2',
            'semantic_payload': {
                'subject_ref': 'user:u', 'predicate': 'reported_identity',
                'object': '职业', 'value': '教师', 'time_scope': 'unspecified',
                'source_scope': scope, 'source_event_id': 'event-7', **semantic,
            },
        }

    def test_private_and_shared_audience(self):
        private = canonical_told_view(self._row(), 'u', 'gojo')
        self.assertEqual(private['source_table'], 'long_memory')
        self.assertEqual(private['audience'], 'gojo')
        self.assertIn('她的职业', told_display(private['content']))
        self.assertIsNone(canonical_told_view(self._row(), 'u', 'geto'))
        self.assertIsNotNone(canonical_told_view(self._row('shared'), 'u', 'gojo'))
        self.assertIsNotNone(canonical_told_view(self._row('shared'), 'u', 'geto'))

    def test_missing_provenance_or_explicit_audience_fails_closed(self):
        self.assertIsNone(canonical_told_view(
            self._row(source_event_id=''), 'u', 'gojo'))
        self.assertIsNone(canonical_told_view(
            self._row(source_scope='geto'), 'u', 'gojo'))
        self.assertIsNone(canonical_told_view(
            self._row('shared', audience=['geto']), 'u', 'gojo'))
        self.assertIsNone(canonical_told_view(
            self._row('shared', audience='notgojo'), 'u', 'gojo'))
        self.assertIsNone(canonical_told_view(
            {**self._row(), 'projection_version': 'legacy_v1'}, 'u', 'gojo'))


if __name__ == '__main__':
    unittest.main()
