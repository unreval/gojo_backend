"""Regression tests for the opt-in shadow classification audit."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


BACKEND = str(Path(__file__).resolve().parents[1] / 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import shadow_audit


NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


class ShadowAuditGateTests(unittest.TestCase):
    def test_env_gate_defaults_off_and_accepts_explicit_values(self):
        original = shadow_audit.SHADOW_AUDIT_ENABLED
        self.addCleanup(setattr, shadow_audit, 'SHADOW_AUDIT_ENABLED', original)
        for value in (None, '0', 'false', ' NO ', 'off', '1', 'true'):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop('SHADOW_AUDIT_ENABLED', None)
                else:
                    os.environ['SHADOW_AUDIT_ENABLED'] = value
                importlib.reload(shadow_audit)
                self.assertEqual(
                    shadow_audit.SHADOW_AUDIT_ENABLED,
                    value in ('1', 'true'),
                )

    def test_disabled_has_no_classifier_or_database_work(self):
        with patch.object(shadow_audit, 'SHADOW_AUDIT_ENABLED', False), \
             patch.object(shadow_audit, 'get_conn') as connect, \
             patch('memory_lifecycle.classify_memory_lifecycle') as classify, \
             patch('auto_pin.detect_auto_pins') as detect:
            self.assertIsNone(shadow_audit.shadow_log_turn('u', 'c', '明天有考试'))
        connect.assert_not_called()
        classify.assert_not_called()
        detect.assert_not_called()


class ShadowAuditWriteTests(unittest.TestCase):
    def setUp(self):
        self.conn = MagicMock()
        self.cur = self.conn.cursor.return_value
        self.cur.fetchone.return_value = (42,)
        enabled = patch.object(shadow_audit, 'SHADOW_AUDIT_ENABLED', True)
        connect = patch.object(shadow_audit, 'get_conn', return_value=self.conn)
        enabled.start()
        self.connect = connect.start()
        self.addCleanup(enabled.stop)
        self.addCleanup(connect.stop)

    def log(self, text='明天有考试', **kwargs):
        return shadow_audit.shadow_log_turn('u1', 'c1', text, now=NOW, **kwargs)

    def test_sticky_row_has_decisions_hash_and_source_ids(self):
        text = '明天有考试'
        self.assertEqual(self.log('  ' + text + '  ', source_event_ids=('evt_1', '', 2)), 42)
        self.cur.execute.assert_called_once()
        sql, row = self.cur.execute.call_args.args
        self.assertIn('INSERT INTO shadow_audit_log', sql)
        self.assertEqual((row['user_id'], row['character_id']), ('u1', 'c1'))
        self.assertEqual(json.loads(row['source_event_ids']), ['evt_1', '2'])
        self.assertEqual(row['user_text_hash'], hashlib.sha256(text.encode('utf-8')).hexdigest())
        self.assertEqual(row['text_length'], len(text))
        self.assertTrue(row['would_write_sticky'])
        self.assertTrue(row['would_record_lifecycle'])
        self.assertFalse(row['would_save_long_memory'])
        self.assertEqual(row['memory_kind'], 'sticky')
        self.assertEqual(row['reason'], 'temporary_goal_or_deadline')
        self.assertEqual(row['created_at'], NOW)
        self.assertEqual(json.loads(row['lifecycle_classification'])['memory_kind'], 'sticky')
        self.conn.commit.assert_called_once()
        self.conn.rollback.assert_not_called()
        self.cur.close.assert_called_once()
        self.conn.close.assert_called_once()

    def test_routine_and_long_fact_decisions(self):
        for text, kind, save, record in (
            ('我去洗澡了', 'ephemeral', False, True),
            ('我喜欢咖啡', 'long_fact', True, False),
        ):
            with self.subTest(text=text):
                self.log(text)
                row = self.cur.execute.call_args.args[1]
                self.assertEqual(row['memory_kind'], kind)
                self.assertFalse(row['would_write_sticky'])
                self.assertEqual(row['would_save_long_memory'], save)
                self.assertEqual(row['would_record_lifecycle'], record)

    def test_auto_pin_logs_metadata_without_user_text_or_match_span(self):
        for text, expected in (
            ('不要 push，私密标记 SHADOW-PRIVATE-123', {
                'pin_type': 'pending_request', 'topic': 'git-push-block',
            }),
            ('现在 push，私密标记 SHADOW-PRIVATE-123', {
                'pin_type': 'pending_request', 'topic': 'git-push', 'action': 'allow-push',
            }),
        ):
            with self.subTest(text=text):
                self.log(text)
                row = self.cur.execute.call_args.args[1]
                self.assertTrue(row['would_auto_pin'])
                self.assertEqual(json.loads(row['auto_pin_detections']), [expected])
                self.assertNotIn('SHADOW-PRIVATE-123', json.dumps(row, default=str))
                self.assertNotIn(text, json.dumps(row, ensure_ascii=False, default=str))

    def test_calls_canonical_classifiers_with_turn_time(self):
        with patch('memory_lifecycle.classify_memory_lifecycle', return_value={}) as classify, \
             patch('auto_pin.detect_auto_pins', return_value=[]) as detect:
            self.log('  内容  ')
        classify.assert_called_once_with('内容', now=NOW)
        detect.assert_called_once_with('内容')

    def test_empty_text_skips_database(self):
        for text in ('', '  ', None):
            self.assertIsNone(self.log(text))
        self.connect.assert_not_called()

    def test_insert_failure_rolls_back_and_releases_resources(self):
        self.cur.execute.side_effect = RuntimeError('db down')
        with self.assertRaisesRegex(RuntimeError, 'db down'):
            self.log()
        self.conn.commit.assert_not_called()
        self.conn.rollback.assert_called_once()
        self.cur.close.assert_called_once()
        self.conn.close.assert_called_once()

    def test_cursor_failure_closes_owned_connection(self):
        self.conn.cursor.side_effect = RuntimeError('cursor unavailable')
        with self.assertRaisesRegex(RuntimeError, 'cursor unavailable'):
            self.log()
        self.conn.rollback.assert_called_once()
        self.conn.close.assert_called_once()

    def test_classification_failure_does_not_open_connection(self):
        with patch('memory_lifecycle.classify_memory_lifecycle', side_effect=RuntimeError('classifier down')):
            with self.assertRaisesRegex(RuntimeError, 'classifier down'):
                self.log()
        self.connect.assert_not_called()


class ShadowAuditSchemaTests(unittest.TestCase):
    def test_init_owns_transaction_and_is_repeatable(self):
        conn = MagicMock()
        with patch.object(shadow_audit, 'get_conn', return_value=conn):
            shadow_audit.init_shadow_audit_table()
            shadow_audit.init_shadow_audit_table()
        self.assertEqual(conn.commit.call_count, 2)
        self.assertEqual(conn.close.call_count, 2)
        statements = [call.args[0] for call in conn.cursor.return_value.execute.call_args_list]
        self.assertEqual(len(statements), 2 * len(shadow_audit.SHADOW_AUDIT_DDL))
        self.assertTrue(all('IF NOT EXISTS' in sql for sql in statements))
        self.assertIn('ON shadow_audit_log', statements[1])

    def test_borrowed_connection_keeps_callers_transaction(self):
        conn = MagicMock()
        shadow_audit.init_shadow_audit_table(conn)
        conn.commit.assert_not_called()
        conn.rollback.assert_not_called()
        conn.close.assert_not_called()
        conn.cursor.return_value.close.assert_called_once()

    def test_ddl_failure_rolls_back_owned_connection(self):
        conn = MagicMock()
        conn.cursor.return_value.execute.side_effect = RuntimeError('DDL denied')
        with patch.object(shadow_audit, 'get_conn', return_value=conn):
            with self.assertRaisesRegex(RuntimeError, 'DDL denied'):
                shadow_audit.init_shadow_audit_table()
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        conn.cursor.return_value.close.assert_called_once()
        conn.close.assert_called_once()


class ShadowAuditContextTests(unittest.TestCase):
    def setUp(self):
        import context_layer
        self.context = context_layer
        self.old_store = context_layer._USE_MEMORY_STORE
        context_layer.use_memory_store(True)
        self.addCleanup(context_layer.use_memory_store, self.old_store)

    def assemble(self, **kwargs):
        return self.context.assemble_from_events(
            [], user_id='u1', character_id='c1', user_message='明天有考试',
            current_event_id=('evt_1', 'evt_2'), now=NOW,
            include_recall=False, include_support=False, **kwargs,
        )

    def test_hook_is_independent_of_auto_pin_gate(self):
        with patch.object(shadow_audit, 'SHADOW_AUDIT_ENABLED', True), \
             patch.object(shadow_audit, 'shadow_log_turn') as log, \
             patch('auto_pin.maybe_auto_pin') as auto_pin:
            self.assemble(auto_pin_enabled=False)
        auto_pin.assert_not_called()
        self.assertEqual(log.call_count, 1)
        args, kwargs = log.call_args
        self.assertEqual(args, ('u1', 'c1', '明天有考试'))
        self.assertEqual(set(kwargs['source_event_ids']), {'evt_1', 'evt_2'})
        self.assertEqual(kwargs['now'], NOW)

    def test_disabled_hook_does_not_log(self):
        with patch.object(shadow_audit, 'SHADOW_AUDIT_ENABLED', False), \
             patch.object(shadow_audit, 'shadow_log_turn') as log:
            self.assemble(auto_pin_enabled=False)
        log.assert_not_called()

    def test_audit_database_failure_leaves_context_unchanged(self):
        with patch.object(shadow_audit, 'SHADOW_AUDIT_ENABLED', False):
            expected = self.assemble(auto_pin_enabled=False)
        conn = MagicMock()
        conn.cursor.return_value.execute.side_effect = RuntimeError('db down')
        with patch.object(shadow_audit, 'SHADOW_AUDIT_ENABLED', True), \
             patch.object(shadow_audit, 'get_conn', return_value=conn):
            actual = self.assemble(auto_pin_enabled=False)
        self.assertEqual(actual, expected)
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()

    def test_auto_pin_failure_does_not_prevent_audit(self):
        with patch.object(shadow_audit, 'SHADOW_AUDIT_ENABLED', True), \
             patch.object(shadow_audit, 'shadow_log_turn') as log, \
             patch('auto_pin.maybe_auto_pin', side_effect=RuntimeError('pin down')):
            self.assemble()
        log.assert_called_once()


class TextHashTests(unittest.TestCase):
    def test_sha256_is_deterministic_for_unicode_and_empty_text(self):
        for text in ('hello', '明天有考试', '', None):
            with self.subTest(text=text):
                self.assertEqual(
                    shadow_audit._text_hash(text),
                    hashlib.sha256((text or '').encode('utf-8')).hexdigest(),
                )


if __name__ == '__main__':
    unittest.main()
