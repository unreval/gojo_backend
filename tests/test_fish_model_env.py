"""Fish model header follows FISH_MODEL. requests.post is patched; no live API."""
import importlib
import os
import unittest
from unittest.mock import MagicMock, patch

import config
import tts


SAMPLE = '今天天气不错，我们出去走走吧。'
WARNING = '[tts] WARNING unknown fish model, Fish will fall back to s2.1-pro'


def _reload():
    importlib.reload(config)
    importlib.reload(tts)


def _logged(reload_modules):
    lines = []

    def _capture(*args, **_kwargs):
        if args:
            lines.append(args[0])

    with patch('builtins.print', side_effect=_capture):
        reload_modules()
    return lines


class FishModelEnvTests(unittest.TestCase):
    def setUp(self):
        self._previous = os.environ.get('FISH_MODEL')

    def tearDown(self):
        if self._previous is None:
            os.environ.pop('FISH_MODEL', None)
        else:
            os.environ['FISH_MODEL'] = self._previous
        _reload()

    def _post(self):
        response = MagicMock()
        response.status_code = 200
        response.iter_content.return_value = [b'audio']
        response.text = ''
        with patch('requests.post', return_value=response) as post:
            audio = tts.fish_tts(SAMPLE)
        self.assertEqual(audio, b'audio')
        self.assertEqual(post.call_count, 1)
        args, kwargs = post.call_args
        self.assertEqual(args[0], 'https://api.fish.audio/v1/tts')
        self.assertEqual(kwargs['headers']['model'], tts.FISH_MODEL)
        return kwargs['headers'], kwargs['json']

    def test_unset_fish_model_uses_s21_pro(self):
        os.environ.pop('FISH_MODEL', None)
        lines = _logged(_reload)
        self.assertIn('[tts] fish model=s2.1-pro', lines)
        self.assertNotIn(WARNING, lines)
        headers, _body = self._post()
        self.assertEqual(headers['model'], 's2.1-pro')

    def test_drama3_changes_only_model_header(self):
        os.environ.pop('FISH_MODEL', None)
        _reload()
        default_headers, default_body = self._post()
        self.assertEqual(default_headers['model'], 's2.1-pro')

        os.environ['FISH_MODEL'] = 'drama-3'
        lines = _logged(_reload)
        self.assertIn('[tts] fish model=drama-3', lines)
        self.assertNotIn(WARNING, lines)
        headers, body = self._post()
        self.assertEqual(headers['model'], 'drama-3')
        self.assertEqual(set(body), set(default_body))
        for key in default_body:
            self.assertEqual(body[key], default_body[key], key)
        self.assertEqual(body, default_body)
        other_headers = {key: value for key, value in headers.items() if key != 'model'}
        default_other = {
            key: value for key, value in default_headers.items() if key != 'model'
        }
        self.assertEqual(other_headers, default_other)

    def test_blank_fish_model_falls_back_to_s21_pro(self):
        os.environ['FISH_MODEL'] = '  '
        lines = _logged(_reload)
        self.assertEqual(tts.FISH_MODEL, 's2.1-pro')
        self.assertIn('[tts] fish model=s2.1-pro', lines)
        self.assertNotIn(WARNING, lines)
        headers, _body = self._post()
        self.assertEqual(headers['model'], 's2.1-pro')

    def test_unknown_fish_model_is_forwarded_with_warning(self):
        os.environ['FISH_MODEL'] = 'not-a-fish-model'
        lines = _logged(_reload)
        self.assertEqual(tts.FISH_MODEL, 'not-a-fish-model')
        self.assertIn('[tts] fish model=not-a-fish-model', lines)
        self.assertIn(WARNING, lines)
        headers, _body = self._post()
        self.assertEqual(headers['model'], 'not-a-fish-model')


if __name__ == '__main__':
    unittest.main()
