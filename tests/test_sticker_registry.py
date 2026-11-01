import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import media_storage  # noqa: E402
import sticker_registry  # noqa: E402


def _write(path, payload):
    with open(path, 'w', encoding='utf-8') as handle:
        if isinstance(payload, str):
            handle.write(payload)
        else:
            json.dump(payload, handle)


class StickerRegistryTests(unittest.TestCase):
    def setUp(self):
        self._prev_path = sticker_registry.MANIFEST_PATH
        self._prev_cache = sticker_registry._CACHE
        self._prev_enabled = os.environ.get('STICKERS_ENABLED')
        os.environ.pop('STICKERS_ENABLED', None)
        self.temp = tempfile.TemporaryDirectory()
        sticker_registry._CACHE = None

    def tearDown(self):
        sticker_registry.MANIFEST_PATH = self._prev_path
        sticker_registry._CACHE = self._prev_cache
        if self._prev_enabled is None:
            os.environ.pop('STICKERS_ENABLED', None)
        else:
            os.environ['STICKERS_ENABLED'] = self._prev_enabled
        self.temp.cleanup()

    def _use(self, payload):
        path = os.path.join(self.temp.name, 'stickers.json')
        _write(path, payload)
        sticker_registry.MANIFEST_PATH = path
        sticker_registry._CACHE = None
        return path

    def test_missing_corrupt_and_empty_disable_without_raising(self):
        sticker_registry.MANIFEST_PATH = os.path.join(self.temp.name, 'missing.json')
        sticker_registry._CACHE = None
        self.assertFalse(sticker_registry.is_enabled())
        self.assertEqual(sticker_registry.catalog_for_prompt(), '')

        self._use('{')
        self.assertFalse(sticker_registry.is_enabled())
        self.assertEqual(sticker_registry.catalog_for_prompt(), '')
        self.assertIsNone(sticker_registry.get('happy_01'))

        self._use({'version': 1, 'prefix': 'stickers/v1/', 'items': []})
        self.assertFalse(sticker_registry.is_enabled())
        self.assertEqual(sticker_registry.catalog_for_prompt(), '')

    def test_duplicate_id_discards_the_whole_manifest(self):
        self._use({
            'version': 1,
            'prefix': 'stickers/v1/',
            'items': [
                {'id': 'happy_01', 'file': 'a.png', 'mime': 'image/png',
                 'emotion': '开心', 'desc': '第一张'},
                {'id': 'happy_01', 'file': 'b.png', 'mime': 'image/png',
                 'emotion': '开心', 'desc': '最后一张'},
            ],
        })
        manifest = sticker_registry.load_manifest()
        self.assertEqual(manifest['items'], [])
        self.assertIsNone(sticker_registry.get('happy_01'))

    def test_illegal_id_is_skipped_and_unknown_id_is_none(self):
        self._use({
            'version': 1,
            'prefix': 'stickers/v1/',
            'items': [
                {'id': 'Happy-01', 'file': 'bad.png', 'emotion': '开心', 'desc': '非法'},
                {'id': 'flat_01', 'file': 'flat_01.png', 'emotion': '无语', 'desc': '面无表情'},
            ],
        })
        self.assertIsNone(sticker_registry.get('Happy-01'))
        item = sticker_registry.get('flat_01')
        self.assertEqual(item['id'], 'flat_01')
        self.assertEqual(sticker_registry.object_key(item), 'stickers/v1/flat_01.png')
        self.assertIsNone(sticker_registry.get('不存在的id'))

    def test_signed_url_without_r2_returns_none(self):
        self._use({
            'version': 1,
            'prefix': 'stickers/v1/',
            'items': [
                {'id': 'happy_01', 'file': 'happy_01.png', 'emotion': '开心', 'desc': '笑着比耶'},
            ],
        })
        with patch.dict(os.environ, {
            'R2_ENDPOINT': '',
            'R2_ACCESS_KEY_ID': '',
            'R2_SECRET_ACCESS_KEY': '',
            'R2_BUCKET': '',
        }, clear=False):
            self.assertFalse(media_storage.is_configured())
            self.assertIsNone(sticker_registry.signed_url('happy_01'))

    def test_catalog_groups_by_emotion_only_when_enabled(self):
        self._use({
            'version': 1,
            'prefix': 'stickers/v1/',
            'items': [
                {'id': 'happy_01', 'file': 'happy_01.png', 'emotion': '开心', 'desc': '笑着比耶'},
                {'id': 'happy_02', 'file': 'happy_02.png', 'emotion': '开心', 'desc': '得意挑眉'},
                {'id': 'flat_01', 'file': 'flat_01.png', 'emotion': '无语', 'desc': '面无表情'},
            ],
        })
        self.assertEqual(sticker_registry.catalog_for_prompt(), '')
        with patch.object(media_storage, 'is_configured', return_value=True):
            self.assertTrue(sticker_registry.is_enabled())
            self.assertEqual(
                sticker_registry.catalog_for_prompt(),
                '开心：happy_01=笑着比耶 / happy_02=得意挑眉\n无语：flat_01=面无表情',
            )
        os.environ['STICKERS_ENABLED'] = 'false'
        with patch.object(media_storage, 'is_configured', return_value=True):
            self.assertFalse(sticker_registry.is_enabled())
            self.assertEqual(sticker_registry.catalog_for_prompt(), '')

    def test_audience_splits_prompt_catalog_from_the_user_list(self):
        self._use({
            'version': 1,
            'prefix': 'stickers/v1/',
            'items': [
                {'id': 'char_01', 'file': 'char_01.png', 'emotion': '开心',
                 'desc': '角色笑', 'for': 'character'},
                {'id': 'user_01', 'file': 'user_01.png', 'emotion': '开心',
                 'desc': '我的笑', 'for': 'user'},
                {'id': 'both_01', 'file': 'both_01.png', 'emotion': '无语',
                 'desc': '两边翻', 'for': 'both'},
                {'id': 'plain_01', 'file': 'plain_01.png', 'emotion': '无语',
                 'desc': '没写for'},
            ],
        })
        with patch.object(media_storage, 'is_configured', return_value=True):
            catalog = sticker_registry.catalog_for_prompt()
        self.assertIn('char_01=角色笑', catalog)
        self.assertIn('both_01=两边翻', catalog)
        self.assertIn('plain_01=没写for', catalog)
        self.assertNotIn('user_01', catalog)
        self.assertNotIn('我的笑', catalog)
        user_ids = [item['id'] for item in sticker_registry.list_for_user()]
        self.assertEqual(user_ids, ['user_01', 'both_01', 'plain_01'])
        self.assertNotIn('char_01', user_ids)
        self.assertTrue(sticker_registry.usable_by('plain_01', 'character'))
        self.assertTrue(sticker_registry.usable_by('plain_01', 'user'))
        self.assertTrue(sticker_registry.usable_by('char_01', 'character'))
        self.assertFalse(sticker_registry.usable_by('char_01', 'user'))
        self.assertFalse(sticker_registry.usable_by('user_01', 'character'))
        self.assertTrue(sticker_registry.usable_by('user_01', 'user'))

    def test_illegal_for_stays_in_the_manifest_as_both(self):
        self._use({
            'version': 1,
            'prefix': 'stickers/v1/',
            'items': [
                {'id': 'weird_01', 'file': 'weird_01.png', 'emotion': '开心',
                 'desc': '乱标', 'for': 'everyone'},
                {'id': 'flat_01', 'file': 'flat_01.png', 'emotion': '无语',
                 'desc': '面无表情'},
            ],
        })
        with patch('builtins.print') as log:
            manifest = sticker_registry.load_manifest()
        self.assertEqual(
            [item['id'] for item in manifest['items']],
            ['weird_01', 'flat_01'],
        )
        self.assertEqual(sticker_registry.get('weird_01')['for'], 'both')
        self.assertTrue(sticker_registry.usable_by('weird_01', 'character'))
        self.assertTrue(sticker_registry.usable_by('weird_01', 'user'))
        printed = ' '.join(
            str(call.args[0]) for call in log.call_args_list if call.args)
        self.assertIn("illegal for='everyone'", printed)
        self.assertIn('id=weird_01', printed)


if __name__ == '__main__':
    unittest.main()
