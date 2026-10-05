"""Role labels and old projection adapters never rewrite source messages."""
import unittest

from role_view import render_role_view, role_label


class RoleViewTests(unittest.TestCase):
    def test_same_character_is_i_other_character_has_name(self):
        semantic = {'subject_ref': 'character:gojo', 'source_scope': 'gojo',
                    'predicate': 'quoted_utterance', 'evidence_text': '明天回答她'}
        self.assertIn('我实际说过', render_role_view('', observer_id='gojo', semantic=semantic))
        self.assertIn('五条悟实际说过', render_role_view('', observer_id='geto', semantic=semantic))
        self.assertEqual(role_label('character:gojo', observer_id='geto'), '五条悟')
        self.assertEqual(role_label('user:u', observer_id='geto'), '她')

    def test_legacy_first_person_and_literal_report(self):
        self.assertEqual(render_role_view('我答应过她明天回答。', observer_id='geto',
                                          source_character_id='gojo'), '五条悟答应过她明天回答。')
        self.assertEqual(render_role_view('用户明确自述：我喜欢猫（仅限这次自述，不推断隐含心理）',
                                          observer_id='gojo'), '她喜欢猫。')
        self.assertEqual(render_role_view('她明确自述：我喜欢猫（仅限这次自述，不推断隐含心理）',
                                          observer_id='gojo'), '她喜欢猫。')


if __name__ == '__main__':
    unittest.main()
