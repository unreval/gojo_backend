"""Run memory regressions with disposable PGlite and external IO disabled."""
from contextlib import redirect_stderr, redirect_stdout
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LOCAL_TEMP = Path('C:/Users/\u5f20\u4e09/AppData/Local/Temp')
TARGETED = [
    'tests/test_candidate_producer.py',
    'tests/test_candidate_producer_projection.py',
    'tests/test_structured_output.py',
    'tests/test_structured_output_architecture.py',
    'tests/test_memory_candidate.py',
    'tests/test_memory_candidate_projection.py',
    'tests/test_memory_candidate_source.py',
    'tests/test_memory_authority.py',
    'tests/test_memory_lifecycle.py',
    'tests/test_cognitive_revision.py',
    'tests/test_cognitive_deterministic.py',
    'tests/test_memory_init_restart.py',
    'tests/test_memory_projection_audit.py',
    'tests/test_role_view.py',
    'tests/test_memory_structured_output.py',
    'tests/test_rolling_summary_structured.py',
    'tests/test_provider_errors.py',
]


class Results:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.errors = []
        self.skipped = []
        self.subtests_passed = 0

    def pytest_runtest_logreport(self, report):
        if hasattr(report, 'context') and report.passed:
            self.subtests_passed += 1
            return
        if report.failed:
            target = self.failed if report.when == 'call' else self.errors
            target.append({'test': report.nodeid, 'phase': report.when,
                           'traceback': report.longreprtext})
        elif report.skipped:
            self.skipped.append({'test': report.nodeid, 'reason': report.longreprtext})
        elif report.when == 'call':
            self.passed.append(report.nodeid)

    def pytest_collectreport(self, report):
        if report.failed:
            self.errors.append({'test': report.nodeid, 'phase': 'collection',
                                'traceback': report.longreprtext})


def run(repo, args):
    dependencies = [
        LOCAL_TEMP / 'gojo_future_schedule_prompt_20261017_evidence/test-packages',
        LOCAL_TEMP / 'gojo-voice-retry-test-deps',
    ]
    dependencies = [path for path in dependencies if (path / 'pytest/__init__.py').is_file()]
    sys.path[:0] = [str(path) for path in dependencies] + [
        str(repo), str(repo / 'tests'), str(repo / 'gojo_backend')]
    os.environ.update({
        'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1',
        'COGNITIVE_WORKER_ENABLED': 'false',
        'CANDIDATE_PRODUCER_ENABLED': 'false',
        'DATABASE_URL': 'postgresql://offline:offline@127.0.0.1:1/offline',
    })
    for key in ('ANTHROPIC_KEY', 'ANTHROPIC_API_KEY', 'DEEPSEEK_KEY',
                'OPENAI_API_KEY', 'GROQ_KEY', 'FISH_KEY'):
        os.environ[key] = 'offline-dummy-key'
    os.environ.pop('COGNITIVE_TEST_PGDATA', None)
    package = os.environ.get('COGNITIVE_TEST_PGLITE') or str(
        LOCAL_TEMP / 'gojo-pglite-test/node_modules/@electric-sql/pglite')
    if not (Path(package) / 'package.json').is_file():
        raise SystemExit('Missing local PGlite package; no packages are downloaded.')
    os.environ['COGNITIVE_TEST_PGLITE'] = package
    os.chdir(repo)
    import run_offline
    run_offline._install_guards()
    import pytest
    selected = args.path or (['tests/'] if args.group == 'full' else TARGETED)
    argv = ['-v', '-p', 'no:cacheprovider', *selected]
    if args.stop_first:
        argv.append('-x')
    results = Results()
    log_path = ROOT / 'review' / (args.label + '-tests.log')
    with log_path.open('w', encoding='utf-8') as log, \
            redirect_stdout(log), redirect_stderr(log):
        print('source_commit=' + (args.baseline or 'working-tree'))
        print('pytest_argv=' + json.dumps(argv))
        print('offline_guard=external-network-and-real-psycopg2-disabled')
        exit_code = int(pytest.main(argv, plugins=[results]))
    report = {
        'source_commit': args.baseline or 'working-tree', 'group': args.group,
        'argv': argv, 'exit_code': exit_code, 'passed': len(results.passed),
        'failures': results.failed, 'errors': results.errors,
        'skipped': results.skipped, 'passed_tests': results.passed,
        'subtests_passed': results.subtests_passed,
        'pglite_package': package, 'pytest_version': pytest.__version__,
        'offline_guard': 'external-network-and-real-psycopg2-disabled',
    }
    (ROOT / 'review' / (args.label + '-tests.json')).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items()
                      if key not in ('failures', 'errors', 'skipped', 'passed_tests')},
                     ensure_ascii=False))
    print(f'failures={len(results.failed)} errors={len(results.errors)} '
          f'skipped={len(results.skipped)} log={log_path}')
    return exit_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('group', choices=('targeted', 'full'))
    parser.add_argument('--label', default='targeted')
    parser.add_argument('--baseline', help='Test a clean Git archive of this commit')
    parser.add_argument('--stop-first', action='store_true')
    parser.add_argument('--path', action='append', help='Override the selected test files')
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if not args.baseline:
        return run(ROOT, args)
    archive = subprocess.run(['git', 'archive', args.baseline], cwd=ROOT,
                             check=True, stdout=subprocess.PIPE).stdout
    with tempfile.TemporaryDirectory(prefix='gojo-memory-baseline-') as directory:
        repo = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(repo, filter='data')
        try:
            return run(repo, args)
        finally:
            os.chdir(ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
