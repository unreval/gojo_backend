"""Load the pre-batch baseline in memory; never change the checkout."""
from contextlib import contextmanager
from pathlib import Path
import importlib
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASE_REF = 'c22ac18534fdc75d91380ce1632118b326fcc4c2'
PRODUCTION_FILES = ('context_layer', 'user_memory', 'prompt', 'route_chat')
_BASELINE_ACTIVE = False


def generation_source(name):
    relative = 'gojo_backend/' + name + '.py'
    if _BASELINE_ACTIVE:
        return subprocess.check_output(
            ['git', 'show', BASE_REF + ':' + relative], cwd=ROOT).decode('utf-8')
    return (ROOT / relative).read_text(encoding='utf-8')


@contextmanager
def baseline_generation_code():
    global _BASELINE_ACTIVE
    originals = []
    previous = _BASELINE_ACTIVE
    _BASELINE_ACTIVE = True
    try:
        for name in PRODUCTION_FILES:
            target = importlib.import_module(name)
            originals.append((target, dict(target.__dict__)))
            identity = {key: value for key, value in target.__dict__.items()
                        if key.startswith('__')}
            target.__dict__.clear()
            target.__dict__.update(identity)
            exec(compile(generation_source(name), str(ROOT / 'gojo_backend' / (name + '.py')),
                         'exec'), target.__dict__)
        yield
    finally:
        for target, namespace in reversed(originals):
            target.__dict__.clear()
            target.__dict__.update(namespace)
        _BASELINE_ACTIVE = previous


if __name__ == '__main__':
    import sys
    import unittest
    from temporal_projection_offline import offline_environment
    names = sys.argv[1:] or ['test_time_cognition_dedup_audit']
    with offline_environment(), baseline_generation_code():
        suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(name)
                                   for name in names)
        result = unittest.TextTestRunner(verbosity=2).run(suite)
    print('BASELINE ref=%s tests=%s failures=%s errors=%s skipped=%s' % (
        BASE_REF, result.testsRun, len(result.failures), len(result.errors), len(result.skipped)))
    sys.exit(0 if result.wasSuccessful() else 1)
