"""Run schedule regressions with disposable PGlite and external IO disabled."""
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
    'tests/test_schedule_insert_time.py',
    'tests/test_insert_intent_prompt.py',
    'tests/test_schedule_reflow_transition.py',
    'tests/test_schedule_world_contract.py',
]


class Results:
    def __init__(self):
        self.passed = []
        self.failed = []
        self.errors = []
        self.skipped = []
        self.collected = []
        self.subtests_passed = 0
        self.testsfailed = None

    def pytest_collection_finish(self, session):
        self.collected = [item.nodeid for item in session.items]

    def pytest_sessionfinish(self, session, exitstatus):
        self.testsfailed = session.testsfailed

    def pytest_runtest_logreport(self, report):
        is_subtest = hasattr(report, 'context')
        if is_subtest and report.passed:
            self.subtests_passed += 1
            return
        if report.failed:
            target = self.failed if report.when == 'call' else self.errors
            target.append({'test': report.nodeid, 'phase': report.when,
                           'subtest': is_subtest,
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
        'GIT_OPTIONAL_LOCKS': '0',
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
        print('git_history=' + os.environ.get('GIT_DIR', 'working-tree'))
        exit_code = int(pytest.main(argv, plugins=[results]))
    failed_nodes = sorted({row['test'] for row in results.failed})
    error_nodes = sorted({row['test'] for row in results.errors})
    clean_passed_nodes = sorted(set(results.passed) - set(failed_nodes) - set(error_nodes))
    report = {
        'source_commit': args.baseline or 'working-tree', 'group': args.group,
        'argv': argv, 'exit_code': exit_code,
        'collected': len(results.collected), 'collected_tests': results.collected,
        'passed': len(clean_passed_nodes), 'passed_call_records': len(results.passed),
        'failure_records': len(results.failed), 'failed_node_count': len(failed_nodes),
        'failed_nodes': failed_nodes, 'error_records': len(results.errors),
        'error_node_count': len(error_nodes), 'pytest_testsfailed': results.testsfailed,
        'failures': results.failed, 'errors': results.errors,
        'skipped': results.skipped, 'passed_tests': clean_passed_nodes,
        'subtests_passed': results.subtests_passed,
        'pglite_package': package, 'pytest_version': pytest.__version__,
        'offline_guard': 'external-network-and-real-psycopg2-disabled',
        'git_history': os.environ.get('GIT_DIR', 'working-tree'),
    }
    (ROOT / 'review' / (args.label + '-tests.json')).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    large = ('failures', 'errors', 'skipped', 'passed_tests', 'collected_tests', 'failed_nodes')
    print(json.dumps({key: value for key, value in report.items() if key not in large},
                     ensure_ascii=False))
    print(f'failure_records={len(results.failed)} error_records={len(results.errors)} '
          f'skipped_records={len(results.skipped)} log={log_path}')
    return exit_code


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('group', choices=('targeted', 'full'))
    parser.add_argument('--label', default='schedule-targeted')
    parser.add_argument('--baseline', help='Test a clean Git archive of this commit')
    parser.add_argument('--stop-first', action='store_true')
    parser.add_argument('--path', action='append', help='Override the selected test files')
    args = parser.parse_args()
    sys.dont_write_bytecode = True
    if not args.baseline:
        return run(ROOT, args)
    archive = subprocess.run(['git', 'archive', args.baseline], cwd=ROOT,
                             check=True, stdout=subprocess.PIPE).stdout
    git_dir = subprocess.check_output(
        ['git', 'rev-parse', '--absolute-git-dir'], cwd=ROOT, text=True).strip()
    os.environ['GIT_DIR'] = git_dir
    with tempfile.TemporaryDirectory(prefix='gojo-schedule-baseline-') as directory:
        repo = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
            bundle.extractall(repo, filter='data')
        os.environ['GIT_WORK_TREE'] = str(repo)
        try:
            return run(repo, args)
        finally:
            os.chdir(ROOT)


if __name__ == '__main__':
    raise SystemExit(main())
