# -*- coding: utf-8 -*-
import os
import sys
import unittest
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import communication_convention_backfill as backfill  # noqa: E402
import user_memory  # noqa: E402


class CommunicationConventionBackfillTests(unittest.TestCase):
    def test_run_defaults_to_dry_run_and_never_applies_a_plan(self):
        inspection = {
            'primary_events': [{'event_id': 'evt-1', 'content': '约定好了', 'timestamp': ''}],
            'conventions': [],
        }
        plans = [{
            'event_id': 'evt-1',
            'candidate': {'action': 'set'},
            'decision': {'status': 'would_add'},
        }]
        with patch.object(backfill, 'inspect_scope', return_value=inspection), \
             patch.object(backfill, 'plan_backfill', return_value=plans), \
             patch.object(backfill, 'validate_existing_conventions', return_value=[]), \
             patch.object(backfill, 'retire_unsupported_conventions',
                          return_value={'status': 'no_unsupported_conventions'}), \
             patch.object(backfill, 'apply_plan_item') as apply_item:
            report = backfill.run_backfill(
                'u1', 'gojo', event_ids=['evt-1'])

        self.assertEqual(report['mode'], 'dry_run')
        self.assertEqual(report['plans'], plans)
        self.assertEqual(report['applied'], [])
        apply_item.assert_not_called()

    def test_apply_reuses_dry_run_candidate_and_keeps_gate_enabled(self):
        candidate = {
            'action': 'set',
            'symbol': '😒',
            'user_evidence_quote': '我们约定好了',
        }
        plan = {
            'event_id': 'evt-confirm',
            'candidate': candidate,
            'decision': {'status': 'would_add'},
        }
        captured = {}

        def extract(*args, **kwargs):
            captured['args'] = args
            captured['kwargs'] = kwargs
            kwargs['backfill_result']['decision'] = {'status': 'would_add'}
            return True

        with patch.object(user_memory, 'extract_and_save_memory', side_effect=extract):
            result = backfill.apply_plan_item('u1', 'gojo', plan)

        self.assertEqual(result['status'], 'applied')
        self.assertEqual(captured['args'][0:4], ('u1', '', '', 'gojo'))
        self.assertTrue(captured['kwargs']['convention_only'])
        self.assertFalse(captured['kwargs']['dry_run'])
        self.assertEqual(
            captured['kwargs']['parsed_override'],
            {'communication_convention': candidate},
        )
        self.assertEqual(
            captured['kwargs']['processor_type'],
            backfill.BACKFILL_PROCESSOR_TYPE,
        )

    def test_apply_skips_ambiguous_or_rejected_plan(self):
        with patch.object(user_memory, 'extract_and_save_memory') as extract:
            result = backfill.apply_plan_item(
                'u1', 'gojo', {
                    'event_id': 'evt-ambiguous',
                    'candidate': {'action': 'replace'},
                    'decision': {'status': 'rejected'},
                })

        self.assertEqual(result, {
            'event_id': 'evt-ambiguous',
            'status': 'skipped',
            'reason': 'rejected',
        })
        extract.assert_not_called()

    def test_rejected_dry_run_is_reported_as_a_conflict(self):
        def extract(*_args, **kwargs):
            kwargs['backfill_result'].update({
                'candidate': {'action': 'replace'},
                'decision': {
                    'status': 'rejected',
                    'reason': 'no_exact_active_target',
                },
            })
            return True

        with patch.object(user_memory, 'extract_and_save_memory', side_effect=extract):
            plan = backfill.plan_event(
                'u1', 'gojo', {'event_id': 'evt-ambiguous', 'content': '改成别的'})

        self.assertEqual(plan['status'], 'planned')
        self.assertEqual(plan['classification'], 'conflict')
        self.assertEqual(plan['decision']['reason'], 'no_exact_active_target')

    def test_unsupported_links_do_not_prove_missing_historical_authorization(self):
        old = user_memory._communication_convention_content('🥺', '固定反应')
        memory = {
            'memory_id': 44,
            'content': old,
            'recall_status': 'active',
            'source_event_ids': ['user-complaint', 'assistant-reassign'],
        }
        events = [
            {
                'event_id': 'user-complaint', 'role': 'user',
                'content': '固定反应不是约定，别记这个。',
            },
            {
                'event_id': 'assistant-reassign', 'role': 'assistant',
                'content': '🥺',
            },
        ]
        with patch.object(backfill.raw_events, 'get_active_events_by_ids',
                          return_value=events), \
             patch.object(backfill.raw_events, 'sources_are_active', return_value=True):
            validation = backfill.validate_existing_convention(
                'u1', 'gojo', memory, [(44, old, None)])

        # There may be an older authorization outside these recorded links.
        # Loading every linked row cannot establish that none was omitted.
        self.assertEqual(validation['status'], 'ambiguous')
        self.assertEqual(validation['reason'], 'provenance_completeness_unproven')
        self.assertFalse(validation['canonical_source_complete'])
        with patch.object(user_memory, 'invalidate_bond_memories') as retire:
            dry_result = backfill.retire_unsupported_conventions(
                'u1', 'gojo', [validation], dry_run=True)
        self.assertEqual(dry_result, {
            'status': 'no_unsupported_conventions', 'memory_ids': []})
        retire.assert_not_called()

    def test_public_retirement_rereads_and_does_not_trust_supplied_complete_flag(self):
        validation = {
            'memory_id': 45,
            'status': 'would_retire_unsupported',
            'canonical_source_complete': True,
        }
        old = user_memory._communication_convention_content('🥺', '固定反应')
        memory = {
            'memory_id': 45,
            'content': old,
            'recall_status': 'active',
            'source_event_ids': ['user-complaint', 'assistant-reassign'],
            'canonical_source_complete': True,
        }
        events = [
            {'event_id': 'user-complaint', 'role': 'user',
             'content': '固定反应不是约定，别记这个。'},
            {'event_id': 'assistant-reassign', 'role': 'assistant', 'content': '🥺'},
        ]
        with patch.object(backfill, 'list_convention_memories',
                          return_value=[memory]) as reread, \
             patch.object(backfill.raw_events, 'get_active_events_by_ids',
                          return_value=events) as source_read, \
             patch.object(backfill.raw_events, 'sources_are_active', return_value=True), \
             patch.object(user_memory, 'invalidate_bond_memories') as retire:
            result = backfill.retire_unsupported_conventions(
                'u1', 'gojo', [validation], dry_run=False)

        self.assertEqual(result['status'], 'no_unsupported_conventions')
        self.assertEqual(result['memory_ids'], [])
        self.assertEqual(result['validation'][0]['status'], 'ambiguous')
        reread.assert_called_once_with('u1', 'gojo')
        source_read.assert_called_once_with('u1', 'gojo', memory['source_event_ids'])
        retire.assert_not_called()

    def test_incomplete_canonical_source_set_cannot_retire_a_convention(self):
        old = user_memory._communication_convention_content('🥺', '固定反应')
        memory = {
            'memory_id': 47,
            'content': old,
            'recall_status': 'active',
            'source_event_ids': ['user-complaint', 'assistant-reassign'],
        }
        events = [
            {
                'event_id': 'user-complaint', 'role': 'user',
                'content': '固定反应不是约定，别记这个。',
            },
            {
                'event_id': 'assistant-reassign', 'role': 'assistant',
                'content': '🥺',
            },
        ]
        with patch.object(backfill.raw_events, 'get_active_events_by_ids',
                          return_value=events[:1]):
            validation = backfill.validate_existing_convention(
                'u1', 'gojo', memory, [(47, old, None)])

        self.assertEqual(validation['status'], 'ambiguous')
        self.assertEqual(validation['reason'], 'missing_or_deleted_source')
        with patch.object(user_memory, 'invalidate_bond_memories') as retire:
            result = backfill.retire_unsupported_conventions(
                'u1', 'gojo', [validation], dry_run=False)
        self.assertEqual(result, {'status': 'no_unsupported_conventions', 'memory_ids': []})
        retire.assert_not_called()

    def test_linked_authorization_can_confirm_without_completeness_proof(self):
        old = user_memory._communication_convention_content('🥺', '固定反应')
        memory = {
            'memory_id': 48,
            'content': old,
            'recall_status': 'active',
            'source_event_ids': ['user-complaint', 'assistant-reassign', 'user-confirmed'],
        }
        recorded_events = [
            {
                'event_id': 'user-complaint', 'role': 'user',
                'content': '固定反应不是约定，别记这个。',
            },
            {
                'event_id': 'assistant-reassign', 'role': 'assistant',
                'content': '🥺',
            },
        ]
        complete_events = recorded_events + [{
            'event_id': 'user-confirmed', 'role': 'user',
            'content': '固定反应是我们的约定，以后就用🥺回复。',
        }]
        with patch.object(backfill.raw_events, 'get_active_events_by_ids',
                          return_value=complete_events), \
             patch.object(backfill.raw_events, 'sources_are_active', return_value=True):
            validation = backfill.validate_existing_convention(
                'u1', 'gojo', memory, [(48, old, None)])

        self.assertEqual(validation['status'], 'confirmed')
        self.assertIn('user-confirmed', validation['canonical_source_event_ids'])

    def test_apply_revalidates_before_retirement_write(self):
        inspection = {'primary_events': [], 'conventions': [{'memory_id': 51}]}
        initial = [{
            'memory_id': 51,
            'status': 'would_retire_unsupported',
            'canonical_source_complete': True,
        }]
        reread = [{
            'memory_id': 51,
            'status': 'ambiguous',
            'reason': 'canonical_source_set_incomplete',
        }]
        with patch.object(backfill, 'inspect_scope', return_value=inspection), \
             patch.object(backfill, 'plan_backfill', return_value=[]), \
             patch.object(backfill, 'validate_existing_conventions',
                          side_effect=[initial, reread]), \
             patch.object(backfill, 'list_convention_memories', return_value=[]), \
             patch.object(user_memory, 'invalidate_bond_memories') as retire:
            report = backfill.run_backfill(
                'u1', 'gojo', event_ids=['evt-1'], apply=True,
                retire_unconfirmed=True)

        self.assertEqual(report['retirement_validation'], reread)
        self.assertEqual(report['retirement']['memory_ids'], [])
        retire.assert_not_called()

    def test_retirement_source_read_error_fails_closed(self):
        validation = {
            'memory_id': 51, 'status': 'would_retire_unsupported',
            'canonical_source_complete': True,
        }
        with patch.object(backfill, 'list_convention_memories',
                          side_effect=RuntimeError('database unavailable')), \
             patch.object(user_memory, 'invalidate_bond_memories') as retire:
            result = backfill.retire_unsupported_conventions(
                'u1', 'gojo', [validation], dry_run=False)
        self.assertEqual(result['status'], 'ambiguous')
        self.assertEqual(result['reason'], 'canonical_source_unavailable')
        retire.assert_not_called()

    def test_invalidate_bond_memory_marks_deleted_without_deleting_history_or_sources(self):
        cursor = Mock()
        cursor.fetchall.return_value = [(46, 'legacy convention')]
        connection = Mock()
        connection.cursor.return_value = cursor
        with patch.object(user_memory, 'get_conn', return_value=connection), \
             patch('builtins.print'):
            ok, rows = user_memory.invalidate_bond_memories(
                'u1', 'gojo', 'between', [46], reason='unsupported_convention')

        self.assertTrue(ok)
        self.assertEqual(rows, [(46, 'legacy convention')])
        self.assertEqual(cursor.execute.call_count, 2)
        update_sql = cursor.execute.call_args_list[1].args[0]
        self.assertIn("recall_status = 'deleted'", update_sql)
        self.assertNotIn('DELETE FROM bond_memory', update_sql)
        connection.commit.assert_called_once()


if __name__ == '__main__':
    unittest.main()
