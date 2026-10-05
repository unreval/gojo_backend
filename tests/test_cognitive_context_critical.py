"""Critical cognition reaches the existing Generator under bounded context."""
import os
import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cognitive_reader
import context_budget
import context_layer
import prompt
import shared_relation_prompt


NOW = datetime(2026, 9, 28, tzinfo=timezone.utc)


def state(**extra):
    return dict(questions=[], beliefs=[], hypotheses=[], sticky_notes=[],
                predictions=[], reflection_note={}, **extra)


def raw_ref(event_id):
    return [{'source_type': 'raw_event', 'source_id': event_id}]


def active_sources(_user_id, _character_id, source_ids, conn=None):
    return [{'event_id': event_id} for event_id in source_ids]


class CriticalContextTests(unittest.TestCase):
    def test_specific_question_authority_wins_critical_slot_over_general_belief(self):
        rows = [context_budget.ContextItem(
            key, 'cognitive_state', key, token_cost=100,
            metadata={'critical_kind': 'judgment', 'authority_priority': authority,
                      'relevance': relevance},
        ) for key, authority, relevance in (
            ('general-belief', 0, 20), ('question-judgment', 1, 2))]
        context_budget.ContextBudgetManager(
            context_budget.BudgetConfig(total_token_budget=500)).allocate(rows)
        self.assertEqual(rows[1].metadata['critical_reservation_reason'], 'reserved')
        self.assertNotEqual(rows[0].metadata['critical_reservation_reason'], 'reserved')

    def test_d_tight_budget_keeps_critical_before_ordinary_memory(self):
        critical = [
            context_budget.ContextItem(
                item_id=key, item_type=kind, text=text, token_cost=45,
                metadata={'critical_kind': critical_kind},
            ) for key, kind, text, critical_kind in (
                ('rel:state', 'relationship_state', '关系性质：友情', 'relationship'),
                ('cog:judgment', 'cognitive_state', '当前判断：我接受宝宝这个称呼', 'judgment'),
                ('cog:question', 'cognitive_state', '未解决问题：是否确认恋爱关系', 'question'),
            )
        ]
        noise = [
            context_budget.ContextItem(
                item_id=f'{kind}:{i}', item_type=kind, text='普通历史内容',
                token_cost=90, priority=99,
            ) for kind in ('recalled_memory', 'rolling_summary', 'episodic_memory')
            for i in range(30)
        ]
        kept = context_budget.ContextBudgetManager(
            context_budget.BudgetConfig(total_token_budget=900),
        ).allocate(noise + critical)
        self.assertTrue({row.item_id for row in critical} <= {row.item_id for row in kept})
        self.assertLess(len(kept), len(noise + critical))
        self.assertLessEqual(sum(row.token_cost for row in kept), 900)

    def test_tiny_budget_records_specific_critical_drop_reason(self):
        item = context_budget.ContextItem(
            'cog:judgment', 'cognitive_state', '完整当前判断', token_cost=90,
            metadata={'critical_kind': 'judgment'},
        )
        context_budget.ContextBudgetManager(
            context_budget.BudgetConfig(total_token_budget=30),
        ).allocate([item])
        self.assertEqual(item.metadata.get('critical_reservation_reason'),
                         'item_exceeds_reserved_budget')

    def test_relevant_current_judgment_not_retired_belief_is_selected(self):
        current = state()
        current['beliefs'] = [
            {'belief_key': 'nickname', 'statement': '我接受她叫我宝宝',
             'confidence': .9, 'status': 'active', 'metadata': {},
             'source_event_refs': raw_ref('nickname-source')},
            {'belief_key': 'old', 'statement': '我不接受她叫我宝宝',
             'confidence': .9, 'status': 'retracted', 'metadata': {}},
            {'belief_key': 'food', 'statement': '她喜欢火锅',
             'confidence': .9, 'status': 'active', 'metadata': {},
             'source_event_refs': raw_ref('food-source')},
        ]
        with patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current), \
                patch('raw_events.get_active_events_by_ids', side_effect=active_sources):
            items = cognitive_reader.iter_active_cognitive_items(
                'u', 'gojo', query='宝宝这个称呼你接受吗', now=NOW)
        body = '\n'.join(row['text'] for row in items)
        self.assertIn('我接受她叫我宝宝', body)
        self.assertNotIn('我不接受', body)
        self.assertNotIn('火锅', body)

    def test_resolved_question_is_current_judgment_not_open_question(self):
        current = state()
        current['questions'] = [{
            'question_key': 'nickname', 'question_text': '接受宝宝这个称呼吗',
            'status': 'resolved', 'metadata': {'resolution': {
                'value': 'yes', 'content': '我明确接受她叫我宝宝',
                'evidence_event_ids': ['e-yes'],
            }},
        }]
        with patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current), \
                patch('raw_events.get_active_events_by_ids', return_value=[{
                    'event_id': 'e-yes', 'user_id': 'u', 'character_id': 'gojo',
                }]):
            items = cognitive_reader.iter_active_cognitive_items(
                'u', 'gojo', query='宝宝接受吗', now=NOW)
        self.assertEqual([row['kind'] for row in items], ['cognitive_judgment'])
        self.assertIn('我明确接受', items[0]['text'])

    def test_expired_sticky_and_duplicate_are_filtered(self):
        current = state()
        current['sticky_notes'] = [
            {'note_key': 'same-note', 'content': '宝宝称呼待跟进', 'status': 'active',
              'expires_at': NOW + timedelta(days=1),
              'source_event_refs': raw_ref('sticky-source')},
            {'note_key': 'same-note', 'content': '宝宝称呼待跟进', 'status': 'active',
              'expires_at': NOW + timedelta(days=1),
              'source_event_refs': raw_ref('sticky-source')},
            {'content': '宝宝旧称呼过期提醒', 'status': 'active',
             'expires_at': NOW - timedelta(seconds=1),
             'source_event_refs': raw_ref('old-sticky-source')},
        ]
        with patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current), \
                patch('raw_events.get_active_events_by_ids', side_effect=active_sources):
            items = cognitive_reader.iter_active_cognitive_items(
                'u', 'gojo', query='宝宝称呼', now=NOW)
        self.assertEqual(len(items), 1)

    def test_f_slow_loop_judgment_reaches_generator_context(self):
        # Model-authored judgments cannot populate the Generator's fact context.
        import cognitive_output
        from tests.test_cognitive_pending_resolution import QuestionConnection, judgment_output
        normalized = cognitive_output.validate_slow_loop_output(
            judgment_output(), allowed_event_ids={27}, current_event_ids={27})
        database = QuestionConnection()
        with self.assertRaisesRegex(cognitive_output.SlowLoopOutputError,
                                    'external_judgment_candidates_not_authoritative'):
            cognitive_output.persist_slow_loop_output(
                database, cycle_id=101, user_id='u', character_id='gojo',
                output=normalized, now=NOW)
        self.assertEqual(database.executed, [])
        current = state()
        current['questions'] = []
        with patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current):
            items = cognitive_reader.iter_active_cognitive_items('u', 'gojo', query='宝宝', now=NOW)
        self.assertFalse(any(row['kind'] == 'cognitive_judgment' for row in items))

    def test_g_unresolved_guard_is_in_generator_expression_rules(self):
        current = state()
        current['questions'] = [{
            'question_key': 'nickname', 'question_text': '宝宝称呼是否接受',
            'status': 'active', 'metadata': {},
            'source_event_refs': raw_ref('question-source'),
        }]
        with patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current), \
                patch('raw_events.get_active_events_by_ids', side_effect=active_sources):
            rows = cognitive_reader.iter_active_cognitive_items(
                'u', 'gojo', query='宝宝称呼', now=NOW)
        self.assertEqual([row['kind'] for row in rows], ['cognitive_question'])
        pack = context_layer.ChatContextPack(
            support_ready=True, recall_ready=True, recall_result={},
            cognitive_prompt_text='\n'.join(row['text'] for row in rows),
            accounts_text='账户上下文',
        )
        with patch.object(prompt, 'get_character', return_value={'core_prompt': '角色设定'}), \
                patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current), \
                patch.object(prompt, 'load_canon_lock', return_value=''), \
                patch.object(prompt, 'get_time_context', return_value=''), \
                patch.object(prompt, 'get_first_interaction_days', return_value=None):
            generator_input = prompt.build_system_prompt(
                'u', 'gojo', user_message='宝宝', context_pack=pack)
        self.assertIn('unresolved', generator_input)
        self.assertIn('不能现场决定 yes/no', generator_input)
        self.assertNotIn('当前已明确的结论：', generator_input)
        self.assertNotIn('当前问题的已形成判断（', generator_input)
        self.assertNotIn('我愿意接受她叫我宝宝', generator_input)
        self.assertNotIn('由你自己读记忆判断', shared_relation_prompt._build_meet_line(2, 3, 4))

    def test_missing_resolution_evidence_is_not_expressed(self):
        current = state()
        current['questions'] = [{
            'question_key': 'nickname', 'question_text': '宝宝称呼',
            'status': 'resolved', 'metadata': {'resolution': {
                'value': 'yes', 'content': '我接受宝宝称呼',
                'evidence_event_ids': ['deleted'],
            }},
        }]
        with patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current), \
                patch('raw_events.get_active_events_by_ids', return_value=[]):
            self.assertEqual(cognitive_reader.iter_active_cognitive_items(
                'u', 'gojo', query='宝宝', now=NOW), [])

    def test_promise_and_pending_prediction_share_existing_working_set(self):
        current = state()
        current['questions'] = [{
            'question_key': 'nickname', 'question_text': '宝宝称呼是否接受',
            'status': 'active', 'metadata': {'pending_answer': {
                'status': 'pending', 'content': '明天回答宝宝称呼的问题',
                'evidence_event_ids': ['promise'],
            }},
            'source_event_refs': raw_ref('question-source'),
        }]
        current['predictions'] = [{
            'status': 'pending', 'expires_at': NOW + timedelta(days=1),
            'metadata': {'description': '用户可能追问宝宝称呼的问题',
                         'evidence_refs': raw_ref('prediction-source')},
        }]
        with patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current), \
                patch('raw_events.get_active_events_by_ids', side_effect=active_sources):
            items = cognitive_reader.iter_active_cognitive_items(
                'u', 'gojo', query='宝宝称呼', now=NOW)
        self.assertEqual({row['kind'] for row in items}, {
            'cognitive_question', 'cognitive_promise', 'cognitive_prediction',
        })

    def test_reader_failure_does_not_restore_legacy_generator_decision(self):
        with patch('relationship_reader.build_state_summary', side_effect=RuntimeError('unavailable')):
            output = shared_relation_prompt.build_relation_rules(
                1, 0, 0, user_id='u', character_id='gojo')
        self.assertIn('READ → EXPRESS', output)
        self.assertNotIn('硬门槛', output)

    def test_short_pending_answer_followup_keeps_same_question_judgment(self):
        current = state()
        current['questions'] = [{
            'question_key': 'nickname', 'question_text': '宝宝称呼是否接受',
            'status': 'active', 'metadata': {
                'pending_answer': {'status': 'pending', 'content': '明天给你回答'},
                'current_judgment': {'status': 'committed', 'value': 'yes',
                                     'content': '我接受她叫我宝宝',
                                     'evidence_event_ids': ['judgment-source']},
            },
            'source_event_refs': raw_ref('question-source'),
        }]
        with patch.object(cognitive_reader, 'fetch_cognitive_reader_state', return_value=current), \
                patch('raw_events.get_active_events_by_ids', side_effect=active_sources):
            for query in ('答案呢', '昨天说今天回答'):
                with self.subTest(query=query):
                    rows = cognitive_reader.iter_active_cognitive_items(
                        'u', 'gojo', query=query, now=NOW)
                    self.assertIn('cognitive_judgment', [row['kind'] for row in rows])
                    self.assertNotIn('cognitive_question', [row['kind'] for row in rows])


if __name__ == '__main__':
    unittest.main()
