"""Run projection regressions with fake vendors and fail-on-call boundaries."""
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import json
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'gojo_backend'), str(ROOT / 'tests'), str(ROOT)]


class ExternalCallForbidden(BaseException):
    """Cannot be swallowed by a production fallback's except Exception."""


def denied(*_args, **_kwargs):
    raise ExternalCallForbidden('unexpected DB / LLM / embedding / TTS call')


def module(name, **attributes):
    value = ModuleType(name)
    value.__dict__.update(attributes)
    return value


class FakeRouter:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: lambda function: function


def response(content, status_code=200):
    return SimpleNamespace(body=json.dumps(content, ensure_ascii=False).encode(),
                           status_code=status_code)


@contextmanager
def offline_environment():
    # These are dependency adapters, never connections to a database or vendor.
    fake_client = SimpleNamespace(messages=SimpleNamespace(create=denied, stream=denied))
    fake_client.with_options = lambda **_kwargs: fake_client
    dependencies = {
        'db': module('db', get_conn=denied),
        'psycopg2': module('psycopg2', connect=denied, _connect=denied),
        'anthropic': module('anthropic', Anthropic=lambda **_kwargs: fake_client,
                            AsyncAnthropic=lambda **_kwargs: fake_client),
        'requests': module('requests', get=denied, post=denied, request=denied,
                           Session=lambda: SimpleNamespace(request=denied)),
        'fastapi': module('fastapi', APIRouter=FakeRouter),
        'fastapi.responses': module('fastapi.responses', JSONResponse=response),
    }
    with ExitStack() as stack:
        stack.enter_context(patch.dict(sys.modules, dependencies))
        import run_offline
        run_offline._install_guards()
        import ai_client
        import memory_search
        import tts
        for target, name in ((ai_client, 'create_chat'),
                             (memory_search, 'embed'),
                             (tts, 'tts_to_b64'), (tts, 'transcribe_audio_b64')):
            stack.enter_context(patch.object(target, name, denied))
        yield


if __name__ == '__main__':
    names = sys.argv[1:] or ['test_time_cognition_projection']
    with offline_environment():
        # Existing receipt/budget fixtures do not exercise these background or
        # world-reader adapters. Supply in-memory responses while retaining the
        # lower DB/provider fail-on-call guards.
        with patch('auto_pin.maybe_auto_pin'), \
                patch('rolling_summary.enqueue_summary_job'), \
                patch('schedule_transition.validate_generated_schedule_reply',
                      return_value=(None, {})):
            suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name)
                                       for name in names)
            result = unittest.TextTestRunner(verbosity=1).run(suite)
        print('GUARDED tests=%s passed=%s skipped=%s failures=%s errors=%s' % (
            result.testsRun,
            result.testsRun - len(result.skipped) - len(result.failures) - len(result.errors),
            len(result.skipped), len(result.failures), len(result.errors)))
        sys.exit(0 if result.wasSuccessful() else 1)
