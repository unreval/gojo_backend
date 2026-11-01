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


if __name__ == '__main__':
    unittest.main()
