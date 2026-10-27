"""Write-only candidate observation log. It is not a memory authority."""
import ast
import importlib
import os
from pathlib import Path
import re
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


BACKEND = Path(__file__).resolve().parents[1] / 'gojo_backend'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import candidate_audit


NOW = datetime(2026, 10, 20, 4, tzinfo=timezone.utc)
NAIVE = datetime(2026, 10, 20, 4)


def record(**changes):
    value = {
        'kind': 'candidate',
        'user_id': 'u',
        'character_id': 'c',
        'batch_id': 'batch-1',
        'source_event_id': 'source-0',
        'candidate_index': 0,
        'quote': '今天我亏了69元',
        'summary': '她今天亏了69元',
        'interpretation': '她这是在报亏损',
        'persistence': None,
        'gate_accepted': True,
        'accepted': True,
        'reason': None,
        'projection_status': None,
        'projection_reason': None,
        'module_version': '1.0',
        'shadow': True,
    }
    value.update(changes)
    return value


class CandidateAuditGateTests(unittest.TestCase):
    def test_env_gate_defaults_on_and_accepts_explicit_values(self):
        def restore():
            importlib.reload(candidate_audit)
        self.addCleanup(restore)
        cases = (
            (None, True),
            ('0', False),
            ('false', False),
            (' NO ', False),
            ('off', False),
            ('1', True),
            ('true', True),
        )
        for value, expected in cases:
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop('CANDIDATE_AUDIT_ENABLED', None)
                else:
                    os.environ['CANDIDATE_AUDIT_ENABLED'] = value
                importlib.reload(candidate_audit)
                self.assertIs(candidate_audit.CANDIDATE_AUDIT_ENABLED, expected)

    def test_disabled_or_non_candidate_does_not_open_a_connection(self):
        with patch.object(candidate_audit, 'CANDIDATE_AUDIT_ENABLED', False), \
                patch('db.get_conn') as connect:
            self.assertIsNone(candidate_audit.log_candidate(record()))
        connect.assert_not_called()
        with patch.object(candidate_audit, 'CANDIDATE_AUDIT_ENABLED', True), \
                patch('db.get_conn') as connect:
            for kind in ('batch', 'worker_failure', 'source_skipped'):
                self.assertIsNone(candidate_audit.log_candidate(record(kind=kind)))
        connect.assert_not_called()


class CandidateAuditWriteTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value
        self.cur.fetchone.return_value = (7,)
        enabled = patch.object(candidate_audit, 'CANDIDATE_AUDIT_ENABLED', True)
        connect = patch('db.get_conn', return_value=self.conn)
        enabled.start()
        self.connect = connect.start()
        self.addCleanup(enabled.stop)
        self.addCleanup(connect.stop)

    def insert_params(self):
        sql, params = self.cur.execute.call_args.args
        self.assertIn('INSERT INTO candidate_audit_log', sql)
        self.assertNotIn('SELECT', sql.upper())
        return params

    def test_row_keeps_interpretation_and_aware_timestamps(self):
        self.assertEqual(candidate_audit.log_candidate(
            record(), batch_id='batch-from-state', occurred_at=NOW,
            module_version='1.0', shadow=True, created_at=NOW), 7)
        params = self.insert_params()
        self.assertEqual(params[0:4], ('u', 'c', 'batch-from-state', 'source-0'))
        self.assertEqual(params[7], '她这是在报亏损')
        self.assertIs(params[9], True)
        self.assertIs(params[10], True)
        self.assertEqual(params[14], '1.0')
        self.assertIs(params[15], True)
        self.assertEqual(params[16], NOW)
        self.assertEqual(params[17], NOW)
        self.assertIsNotNone(params[16].tzinfo)
        self.assertIsNotNone(params[17].tzinfo)
        self.conn.commit.assert_called_once()
        self.conn.rollback.assert_not_called()
        self.conn.close.assert_called_once()

    def test_missing_interpretation_and_naive_event_time_stay_null(self):
        row = record()
        row.pop('interpretation')
        self.assertEqual(candidate_audit.log_candidate(
            row, occurred_at=NAIVE, created_at=NOW), 7)
        params = self.insert_params()
        self.assertIsNone(params[7])
        self.assertIsNone(params[16])
        self.assertEqual(params[17], NOW)

    def test_rejected_candidate_keeps_gate_reason(self):
        candidate_audit.log_candidate(record(
            gate_accepted=False, accepted=False, reason='quote_not_verbatim',
            interpretation='她大概在开玩笑'))
        params = self.insert_params()
        self.assertIs(params[9], False)
        self.assertIs(params[10], False)
        self.assertEqual(params[11], 'quote_not_verbatim')
        self.assertEqual(params[7], '她大概在开玩笑')
        candidate_audit.log_candidate(record(), shadow=False)
        self.assertIs(self.insert_params()[15], False)

    def test_insert_failure_is_swallowed(self):
        def fail_insert(sql, params=None):
            if 'INSERT' in sql:
                raise RuntimeError('db down')
        self.cur.execute.side_effect = fail_insert
        self.assertIsNone(candidate_audit.log_candidate(record(), occurred_at=NOW))
        self.conn.commit.assert_not_called()
        self.conn.rollback.assert_called_once()
        self.conn.close.assert_called_once()


class CandidateAuditSchemaTests(unittest.TestCase):
    def test_time_columns_are_timestamptz(self):
        table_ddl = candidate_audit.CANDIDATE_AUDIT_DDL[0].upper()
        self.assertEqual(table_ddl.count('TIMESTAMPTZ'), 2)
        stripped = table_ddl.replace('TIMESTAMPTZ', '').replace('CURRENT_TIMESTAMP', '')
        self.assertNotIn('TIMESTAMP', stripped)
        joined = '\n'.join(candidate_audit.CANDIDATE_AUDIT_DDL).upper()
        self.assertIn('IDX_CANDIDATE_AUDIT_USER', joined)
        self.assertIn('SOURCE_EVENT_ID', joined)
        self.assertIn('CREATED_AT DESC', joined)

    def test_init_owns_transaction_and_is_repeatable(self):
        conn = MagicMock()
        with patch('db.get_conn', return_value=conn):
            candidate_audit.init_candidate_audit_table()
            candidate_audit.init_candidate_audit_table()
        self.assertEqual(conn.commit.call_count, 2)
        self.assertEqual(conn.close.call_count, 2)
        statements = [call.args[0] for call in conn.cursor.return_value.execute.call_args_list]
        self.assertEqual(len(statements), 2 * len(candidate_audit.CANDIDATE_AUDIT_DDL))
        self.assertTrue(all('IF NOT EXISTS' in sql for sql in statements))

    def test_borrowed_connection_keeps_callers_transaction(self):
        conn = MagicMock()
        candidate_audit.init_candidate_audit_table(conn)
        conn.commit.assert_not_called()
        conn.rollback.assert_not_called()
        conn.close.assert_not_called()

    def test_production_code_never_reads_the_audit_table(self):
        pattern = re.compile(r'\b(from|join)\s+candidate_audit_log\b', re.I)
        hits = []
        for path in BACKEND.rglob('*.py'):
            text = path.read_text(encoding='utf-8')
            if pattern.search(text):
                hits.append(path.name)
        self.assertEqual(hits, [])
        source = Path(candidate_audit.__file__).read_text(encoding='utf-8')
        self.assertNotIn('SELECT', source.upper())
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or '').split('.')[0])
        self.assertFalse(imported & {
            'smart_recall', 'memory_lifecycle', 'memory_authority',
            'relationship_db', 'db_promise', 'cognitive_revision',
        })


if __name__ == '__main__':
    unittest.main()
