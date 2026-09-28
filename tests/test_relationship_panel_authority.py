import inspect
import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


import cognitive_worker  # noqa: E402
import cognitive_output  # noqa: E402
import relationship_engine  # noqa: E402
import relationship_panel  # noqa: E402
import relationship_reader  # noqa: E402
import relationship_state  # noqa: E402
import shared_relation_prompt  # noqa: E402


NOW = datetime(2026, 9, 28, 8, 0, tzinfo=timezone.utc)


def _ledger_state(**overrides):
    state = {
        'warmth': 72.0,
        'friction': {},
        'intimacy': 71.0,
        'trust': 70.0,
        'attachment': 84.0,
        'commitment': 80.0,
        'passion': 35.0,
        'pending_passion': 0,
        'pending_hypothesis': [],
        'banter_baseline': 'reserved',
        'last_updated': NOW,
        'has_state_row': True,
    }
    state.update(overrides)
    return state


def _cognitive_items(*, engagement=True, label='unresolved', openness=True,
                     internal_conflict=None):
    items = {
        'engagement_style': None,
        'romantic_label': {
            'kind': 'question',
            'status': 'active',
            'value': label,
            'content': '是否具有浪漫性质仍未确认。',
            'source_event_ids': ('evt-label',),
            'updated_at': NOW,
        },
        'romantic_openness': None,
        'internal_conflict': None,
    }
    if engagement:
        items['engagement_style'] = {
            'kind': 'belief',
            'status': 'active',
            'statement': '面对关系议题已从立即回避转向愿意延后但正面处理。',
            'confidence': 0.89,
            'source_event_ids': ('evt-engagement-a', 'evt-engagement-b'),
            'updated_at': NOW,
        }
    if openness:
        items['romantic_openness'] = {
            'kind': 'hypothesis',
            'status': 'supported',
            'statement': '角色近期可能不再直接类别性排斥浪漫议题，仍待验证。',
            'confidence': 0.58,
            'source_event_ids': ('evt-open',),
            'updated_at': NOW,
        }
    if internal_conflict:
        items['internal_conflict'] = internal_conflict
    return items


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)
        self.statements = []
        self.params = []

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params.append(params)

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return None

    def close(self):
        pass


class _Connection:
    def __init__(self, cursor):
        self.cursor_value = cursor
        self.commits = 0

    def cursor(self):
        return self.cursor_value

    def commit(self):
        self.commits += 1

    def close(self):
        pass


class RelationshipPanelAuthorityTests(unittest.TestCase):
    def _panel(self, *, state=None, stances=None, cognition=None):
        with patch.object(relationship_panel, 'read_state',
                          return_value=state or _ledger_state()), \
             patch.object(relationship_panel, 'list_declared_stances',
                          return_value=stances or []), \
             patch.object(relationship_panel, 'read_relationship_semantic_state',
                          return_value=cognition or _cognitive_items()):
            return relationship_panel.build_relationship_panel('u', 'gojo')

    def test_panel_keeps_high_ledger_values_and_unresolved_romantic_label_separate(self):
        panel = self._panel()

        self.assertEqual(panel['attachment']['value'], 84.0)
        self.assertEqual(panel['commitment']['value'], 80.0)
        self.assertEqual(panel['engagement_style']['status'], 'belief')
        self.assertEqual(panel['romantic_label']['value'], 'unresolved')
        self.assertEqual(panel['romantic_openness']['status'], 'hypothesis')
        self.assertNotEqual(panel['romantic_label']['value'], 'affirmed')
        self.assertEqual(panel['internal_conflict']['status'], 'unassessed')
        self.assertEqual(panel['direction']['status'], 'unassessed')

    def test_engagement_change_cannot_upgrade_romantic_label(self):
        panel = self._panel(cognition=_cognitive_items(engagement=True, label='unresolved'))

        self.assertIn('愿意延后但正面处理', panel['engagement_style']['value'])
        self.assertEqual(panel['romantic_label']['value'], 'unresolved')

    def test_unresolved_label_is_not_overridden_by_legacy_projection(self):
        panel = self._panel()

        self.assertEqual(panel['romantic_label']['value'], 'unresolved')
        self.assertEqual(panel['legacy_label']['status'], 'legacy_projection')
        rendered = relationship_panel.format_relationship_panel(panel)
        self.assertIn('unresolved', rendered)
        self.assertIn('不能覆盖认知结论', rendered)

    def test_internal_conflict_is_present_only_for_a_cognitive_item(self):
        absent = self._panel()
        present = self._panel(cognition=_cognitive_items(
            internal_conflict={
                'kind': 'hypothesis',
                'status': 'supported',
                'statement': '角色在靠近与回避之间存在可观察的拉扯，仍待验证。',
                'confidence': 0.61,
                'source_event_ids': ('evt-conflict',),
                'updated_at': NOW,
            },
        ))

        self.assertEqual(absent['internal_conflict']['status'], 'unassessed')
        self.assertEqual(present['internal_conflict']['status'], 'present')
        self.assertIn('靠近与回避', present['internal_conflict']['value'])

    def test_panel_does_not_use_offline_state_as_relationship_evidence(self):
        with patch.object(relationship_panel, 'read_state', return_value=_ledger_state()), \
             patch.object(relationship_panel, 'list_declared_stances', return_value=[]), \
             patch.object(relationship_panel, 'read_relationship_semantic_state',
                          return_value=_cognitive_items()), \
             patch.object(relationship_state, 'load_offline_character_state', return_value={
                 'inner': '我已经确认爱情', 'intent': '立刻告白', 'status': '恋爱',
             }):
            first = relationship_panel.build_relationship_panel('u', 'gojo')
        with patch.object(relationship_panel, 'read_state', return_value=_ledger_state()), \
             patch.object(relationship_panel, 'list_declared_stances', return_value=[]), \
             patch.object(relationship_panel, 'read_relationship_semantic_state',
                          return_value=_cognitive_items()), \
             patch.object(relationship_state, 'load_offline_character_state', return_value={
                 'inner': '我根本不喜欢她', 'intent': '再也不见', 'status': '拒绝',
             }):
            second = relationship_panel.build_relationship_panel('u', 'gojo')

        for key in ('romantic_label', 'engagement_style', 'direction'):
            self.assertEqual(first[key], second[key])
        self.assertNotIn('offline', inspect.getsource(relationship_panel).lower())

    def test_panel_and_reader_use_a_read_only_state_path(self):
        with patch.object(relationship_panel, 'read_state', return_value=_ledger_state()), \
             patch.object(relationship_panel, 'list_declared_stances', return_value=[]), \
             patch.object(relationship_panel, 'read_relationship_semantic_state',
                          return_value=_cognitive_items()), \
             patch.object(relationship_state, 'ensure_state_row',
                          side_effect=AssertionError('reader must not initialize rel_state')):
            panel = relationship_panel.build_relationship_panel('u', 'gojo')

        self.assertEqual(panel['romantic_label']['value'], 'unresolved')
        self.assertNotIn('ensure_state_row', inspect.getsource(relationship_panel))
        self.assertNotIn('load_state(', inspect.getsource(relationship_panel))
        reader_source = inspect.getsource(relationship_reader)
        self.assertNotIn('ensure_state_row', reader_source)
        self.assertNotIn('UPDATE rel_state', reader_source)

    def test_full_compact_and_shared_reader_emit_panel_not_confirmed_love(self):
        panel = self._panel()
        quiet_flirt = {
            'flirt_sample_count': 0,
            'desc': '最近没有足够的相关互动',
        }
        with patch.object(relationship_reader, 'build_relationship_panel', return_value=panel), \
             patch.object(relationship_reader, 'compute_tone', return_value=None), \
             patch.object(relationship_reader, 'compute_flirt_response', return_value=quiet_flirt), \
             patch.object(relationship_reader, 'compute_pursue_withdraw',
                          return_value={'pattern': None}), \
             patch.object(relationship_reader, '_temporal_note', return_value=None), \
             patch.object(relationship_reader, 'load_offline_continuity_state',
                          return_value={}), \
             patch.object(relationship_reader, 'initiative_guidance', return_value=''):
            full = relationship_reader.build_state_summary('u', 'gojo')
            compact = relationship_reader.build_state_summary('u', 'gojo', compact=True)
            with patch('cognitive_reader.build_cognitive_prompt_context', return_value=''):
                shared = shared_relation_prompt.build_relation_rules(
                    2, 3, 4, user_id='u', character_id='gojo', user_message='关系如何？',
                )

        for text in (full, compact, shared):
            self.assertIn('关系面板', text)
            self.assertIn('romantic_label：unresolved', text)
            self.assertNotIn('已确认爱情', text)
            self.assertNotIn('不带心动色彩', text)


class RelationshipLegacyBypassTests(unittest.TestCase):
    def test_pending_hypothesis_rejects_panel_semantic_keys(self):
        state = _ledger_state(pending_hypothesis=[])
        save = Mock()
        with patch.object(relationship_state, 'load_state', return_value=state), \
             patch.object(relationship_state, '_save_hypotheses', save):
            result = relationship_state.push_hypothesis_evidence(
                'u', 'gojo', 'relationship.romantic_label', {'signal': 'legacy'},
            )

        self.assertFalse(result)
        save.assert_not_called()

    def test_retreat_boundary_is_not_removed_by_numeric_scores_or_care(self):
        retreat_at = NOW - timedelta(days=2)
        care_at = NOW - timedelta(days=1)
        stances = [
            {'id': 10, 'type': 'retreat_boundary', 'content': '先保持距离',
             'declared_at': retreat_at},
            {'id': 11, 'type': 'care_admission', 'content': '我会照看你',
             'declared_at': care_at},
        ]
        revoke = Mock()
        with patch('relationship_state.list_active_stances', return_value=stances), \
             patch('relationship_state.revoke_stance', revoke), \
             patch('cognitive_reader.read_relationship_semantic_state', return_value={
                 'romantic_label': None,
             }):
            relationship_engine.check_retreat_boundary_superseded('u', 'gojo')

        revoke.assert_not_called()

    def test_retreat_boundary_requires_a_later_explicit_or_source_valid_resolution(self):
        retreat_at = NOW - timedelta(days=2)
        stances = [
            {'id': 10, 'type': 'retreat_boundary', 'content': '先保持距离',
             'declared_at': retreat_at},
            {'id': 11, 'type': 'relationship_confirm', 'content': '我明确改变了表态',
             'declared_at': NOW - timedelta(days=1)},
        ]
        revoke = Mock()
        with patch('relationship_state.list_active_stances', return_value=stances), \
             patch('relationship_state.revoke_stance', revoke), \
             patch('cognitive_reader.read_relationship_semantic_state', return_value={
                 'romantic_label': None,
             }):
            relationship_engine.check_retreat_boundary_superseded('u', 'gojo')

        revoke.assert_called_once()
        self.assertEqual(revoke.call_args.args[2], 10)

    def test_passion_gate_requires_distinct_sessions_as_configured(self):
        def result_for(rows):
            cursor = _Cursor(rows)
            connection = _Connection(cursor)
            with patch.object(relationship_engine, 'get_conn', return_value=connection):
                return relationship_engine._passion_diversity_ok('u', 'gojo')

        same_session = [
            (NOW, 'session-a'),
            (NOW - timedelta(hours=3), 'session-a'),
            (NOW - timedelta(hours=7), 'session-a'),
        ]
        distinct_sessions = [
            (NOW, 'session-b'),
            (NOW - timedelta(hours=3), 'session-a'),
            (NOW - timedelta(hours=7), 'session-a'),
        ]

        self.assertFalse(result_for(same_session))
        self.assertTrue(result_for(distinct_sessions))

    def test_provenance_context_carries_source_event_id_without_new_table(self):
        cursor = _Cursor([])
        connection = _Connection(cursor)
        with patch.object(relationship_state, 'get_conn', return_value=connection):
            with relationship_state.provenance_source_event('evt-source-17'):
                relationship_state._write_provenance(
                    'u', 'gojo', 'warmth', 1.0, 2.0,
                )

        self.assertEqual(connection.commits, 1)
        refs = json.loads(cursor.params[0][9])
        self.assertEqual(refs, [{'source_event_id': 'evt-source-17'}])


class RelationshipCognitiveContractTests(unittest.TestCase):
    def test_worker_documents_the_stable_relationship_semantic_keys(self):
        prompt = cognitive_worker._SYSTEM_PROMPT
        self.assertIn('relationship.engagement_style', prompt)
        self.assertIn('relationship.romantic_label', prompt)
        self.assertIn('relationship.romantic_openness', prompt)
        self.assertIn('unresolved', prompt)
        self.assertNotIn('0.89', prompt)

    def test_semantic_keys_keep_their_independent_cognitive_lifecycles(self):
        cognitive_output._validate_relationship_semantic_contract(
            [{
                'question_key': 'relationship.romantic_label',
                'status': 'active',
                'current_judgment': {
                    'value': 'unresolved', 'content': '仍未确认', 'status': 'current',
                },
            }],
            [{
                'belief_key': 'relationship.engagement_style',
                'belief_type': 'relationship_observation',
            }],
            [{
                'hypothesis_key': 'relationship.romantic_openness',
                'hypothesis_type': 'relationship',
            }],
        )

        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output._validate_relationship_semantic_contract(
                [],
                [{
                    'belief_key': 'relationship.romantic_label',
                    'belief_type': 'relationship_observation',
                }],
                [],
            )
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output._validate_relationship_semantic_contract(
                [{
                    'question_key': 'relationship.romantic_label',
                    'status': 'active',
                    'current_judgment': {
                        'value': 'yes', 'content': '错误窄值', 'status': 'current',
                    },
                }],
                [], [],
            )

    def test_generated_diary_group_and_schedule_paths_do_not_run_relationship_engine(self):
        files = (
            'diary_engine.py', 'diary_scheduler.py', 'schedule_share.py',
            'route_group.py', 'user_memory.py',
        )
        source = {}
        for name in files:
            with open(os.path.join(BACKEND, name), encoding='utf-8') as handle:
                source[name] = handle.read()
            self.assertNotIn('relationship_engine', source[name])
            self.assertNotIn('process_turn(', source[name])

        self.assertIn('主动分享是可选表达行为', source['schedule_share.py'])
        self.assertIn('不构成 relationship evidence', source['schedule_share.py'])
        self.assertIn('不是新的关系证据', source['diary_engine.py'])
        self.assertIn('不是新的关系证据', source['diary_scheduler.py'])
        self.assertIn('不能作为关心或浪漫关系的新证据', source['user_memory.py'])


if __name__ == '__main__':
    unittest.main()
