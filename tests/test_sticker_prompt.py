import os
import sys
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import prompt  # noqa: E402
from generation_contract import rejection_reason  # noqa: E402


_VISIBLE = {'emotion': '平静', 'messages': [{'jp': 'やあ', 'zh': '嘿'}]}


class StickerPromptTests(unittest.TestCase):
    def test_output_spec_format_accepts_the_catalog_key(self):
        text = prompt.OUTPUT_SPEC.format(emotion_list='x', sticker_catalog='y')
        self.assertIn('y', text)
        self.assertIn('sticker_id', text)
        self.assertIn('happy_01', text)

    def test_disabled_catalog_omits_the_whole_section(self):
        with patch('sticker_registry.is_enabled', return_value=False):
            text = prompt.render_output_spec('平静')
        self.assertNotIn('表情包', text)
        self.assertNotIn('sticker_id', text)
        self.assertNotIn('sticker_catalog', text)
        self.assertIn('平静', text)

    def test_enabled_catalog_is_inlined(self):
        with patch('sticker_registry.is_enabled', return_value=True), \
                patch('sticker_registry.catalog_for_prompt', return_value='开心：happy_01=笑着比耶'):
            text = prompt.render_output_spec('平静')
        self.assertIn('开心：happy_01=笑着比耶', text)
        self.assertIn('"sticker_id":"happy_01"', text)
        self.assertNotIn('{sticker_catalog}', text)


class StickerProtocolTests(unittest.TestCase):
    def test_sticker_id_does_not_reject_the_reply(self):
        cases = [
            dict(_VISIBLE),
            dict(_VISIBLE, sticker_id=''),
            dict(_VISIBLE, sticker_id='not_in_the_catalog'),
        ]
        for parsed in cases:
            self.assertIsNone(rejection_reason(parsed))


if __name__ == '__main__':
    unittest.main()
