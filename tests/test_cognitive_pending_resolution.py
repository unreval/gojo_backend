import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cognitive_events
import cognitive_output
import cognitive_worker


NOW = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
QUESTION = '你接受我叫你宝宝吗？'
EVENTS = [
    {'event_id': 'user-1', 'role': 'user', 'content': QUESTION},
    {'event_id': 'assistant-1', 'role': 'assistant', 'content': '我明确说了yes',
     'extra': {'reply_to_event_id': 'user-1'}},
]


class QuestionConnection:
    """Small SQL boundary fake; no network, LLM or background worker."""

    def __init__(self, question=None):
        self.question = question
        self.executed = []
        self.result = None
        self.commits = 0
        self.rollbacks = 0
        self.resolved_keys = []
        self.results = []
        self.event_metadata = [(27, 'question_pending_answer:nickname.acceptance',
                                'user-1', 'memory_extraction',
                                {'evidence_event_ids': ['user-1', 'assistant-1']})]

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        self.result = None
        self.results = []
        if compact.startswith('SELECT id, question_key, question_text, status, metadata'):
            self.result = self.question
        elif compact.startswith('INSERT INTO cognitive_questions'):
            self.result = (71,)
        elif compact.startswith('SELECT question_key FROM cognitive_questions'):
            self.results = [(key,) for key in self.resolved_keys]
        elif compact.startswith('SELECT id, source_event_type, source_event_id, source, payload'):
            self.results = self.event_metadata

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.results

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class PendingResolutionTests(unittest.TestCase):
    def _ingest(self, update, *, events=None, conn=None):
        database = conn or QuestionConnection()
        with patch('raw_events.sources_are_active', return_value=True), \
                patch.object(cognitive_events, 'record_source_event', return_value=27), \
                patch.object(cognitive_events, 'create_trigger_occurrence', return_value=91) as trigger, \
                patch('cognitive_queue.aggregate_pending_triggers',
                      return_value={'status': 'queued', 'cycle_id': 101}) as aggregate:
            result = cognitive_events.ingest_question_update(
                user_id='u', character_id='gojo', update=update,
                canonical_events=events or EVENTS, occurred_at=NOW, conn=database,
            )
            self.last_trigger = trigger.call_args
        return result, database, aggregate

    def test_c_explicit_yes_resolves_question_and_preserves_history(self):
        old = (71, 'nickname.acceptance', QUESTION, 'active',
               {'pending_answer': {'status': 'pending', 'content': '明天回答'}},
               [{'event_id': 12, 'source_id': 'older-user'}])
        result, conn, _ = self._ingest({
            'type': 'resolution', 'question_key': 'nickname.acceptance',
            'question_text': QUESTION, 'content': '我明确接受她叫我宝宝。',
            'value': 'yes', 'evidence_quote': '我明确说了yes',
            'evidence_event_ids': ['user-1', 'assistant-1'],
        }, conn=QuestionConnection(old))
        self.assertEqual(result['question_status'], 'resolved')
        insert = next(params for sql, params in conn.executed
                      if sql.startswith('INSERT INTO cognitive_questions'))
        metadata = json.loads(insert[5])
        self.assertEqual(metadata['resolution']['value'], 'yes')
        self.assertEqual(metadata['pending_answer']['status'], 'fulfilled')
        self.assertTrue(metadata['lifecycle_history'])
        refs = json.loads(insert[6])
        self.assertEqual(refs[0]['event_id'], 12)
        self.assertEqual(refs[-1]['event_id'], 27)
        self.assertTrue(any('UPDATE cognitive_hypotheses' in sql for sql, _ in conn.executed))
        self.assertTrue(any('UPDATE cognitive_predictions' in sql for sql, _ in conn.executed))
        self.assertTrue(any('UPDATE cognitive_sticky_notes' in sql for sql, _ in conn.executed))
        belief_sql, belief_params = next((sql, params) for sql, params in conn.executed
                                        if sql.startswith('UPDATE cognitive_beliefs'))
        self.assertIn('committed_from_hypothesis_id IN', belief_sql)
        self.assertIn("metadata->>'question_key' = %s", belief_sql)
        self.assertEqual(json.loads(belief_params[0])['review_status'], 'under_review')
        self.assertEqual(belief_params[4:], ('nickname.acceptance', 'u', 'gojo', 71))

    def test_e_future_promise_stays_open_and_queues_existing_slow_loop(self):
        events = [EVENTS[0], dict(EVENTS[1], content='明天给你回答。')]
        result, conn, aggregate = self._ingest({
            'type': 'pending_answer', 'question_text': QUESTION,
            'content': '明天给你回答。', 'evidence_quote': '明天给你回答。',
            'evidence_event_ids': ['user-1', 'assistant-1'],
        }, events=events)
        self.assertEqual(result['question_status'], 'active')
        self.assertEqual(result['trigger_id'], 91)
        self.assertEqual(result['cycle']['cycle_id'], 101)
        aggregate.assert_called_once()
        self.assertEqual(self.last_trigger.kwargs['trigger_class'], 'question_reactivation')
        self.assertEqual(self.last_trigger.kwargs['payload']['reason'], 'pending_answer')
        insert = next(params for sql, params in conn.executed
                      if sql.startswith('INSERT INTO cognitive_questions'))
        self.assertEqual(json.loads(insert[5])['pending_answer']['status'], 'pending')
        # The production worker drains this queue without any new chat event.
        with patch.object(cognitive_worker, 'maintain_scheduled_reflections'), \
                patch.object(cognitive_worker, 'maintain_pending_cycles') as maintenance, \
                patch.object(cognitive_worker, 'claim_next_cycle',
                             return_value={'status': 'running', 'cycle_id': 101}), \
                patch.object(cognitive_worker, 'build_reasoning_context',
                             return_value={'current_questions': [{'question_text': QUESTION}]}), \
                patch.object(cognitive_worker, 'generate_cycle_output',
                             return_value=({'cycle_summary': {'summary': '继续考虑该问题'}}, {})) as slow_loop, \
                patch.object(cognitive_worker, 'commit_cycle_success',
                             return_value={'status': 'succeeded'}):
            self.assertEqual(cognitive_worker.run_worker_once(now=NOW)['status'], 'succeeded')
            maintenance.assert_called_once()
            slow_loop.assert_called_once()
            self.assertEqual(slow_loop.call_args.args[0]['current_questions'][0]['question_text'], QUESTION)

    def test_stale_slow_loop_cannot_reopen_explicit_resolution_or_predictions(self):
        conn = QuestionConnection()
        conn.resolved_keys = ['nickname.acceptance']
        output = {
            'evidence_refs': [{'event_id': 27}],
            'question_updates': [{'question_key': 'nickname.acceptance',
                                  'question_text': QUESTION, 'status': 'active',
                                  'evidence_refs': [27]}],
            'hypothesis_updates': [{'question_key': 'nickname.acceptance',
                                    'hypothesis_key': 'nickname.uncertain'}],
            'new_predictions': [{'question_key': 'nickname.acceptance'}],
            'belief_updates': [],
            'sticky_note_updates': [{'note_key': 'reminder',
                                     'question_key': 'nickname.acceptance'}],
        }
        cognitive_output.persist_slow_loop_output(
            conn, cycle_id=1, user_id='u', character_id='gojo', output=output, now=NOW,
        )
        question_sql = next(sql for sql, _ in conn.executed
                            if sql.startswith('INSERT INTO cognitive_questions'))
        row = next(params for sql, params in conn.executed
                   if sql.startswith('INSERT INTO cognitive_questions'))
        # Both writers now use the shared transition gate before persistence.
        self.assertEqual(row[4], 'resolved')
        self.assertIn('cognitive_questions.source_event_refs || EXCLUDED.source_event_refs', question_sql)
        for table in ('cognitive_predictions', 'cognitive_hypotheses', 'cognitive_sticky_notes'):
            self.assertFalse(any(sql.startswith('INSERT INTO ' + table) for sql, _ in conn.executed))

    def test_fabricated_quote_cannot_resolve_question(self):
        with self.assertRaises(ValueError):
            self._ingest({
                'type': 'resolution', 'question_text': QUESTION,
                'content': '接受', 'value': 'yes', 'evidence_quote': '我拒绝了',
                'evidence_event_ids': ['user-1', 'assistant-1'],
            })

    def test_unrelated_assistant_event_cannot_supply_promise(self):
        with self.assertRaises(ValueError):
            self._ingest({
                'type': 'pending_answer', 'question_text': QUESTION,
                'content': '明天回答', 'evidence_quote': '明天回答',
                'evidence_event_ids': ['user-1', 'assistant-1'],
            }, events=[EVENTS[0], dict(EVENTS[1], content='明天回答',
                                     extra={'reply_to_event_id': 'unrelated-user'})])

    def test_current_judgment_persists_on_question_not_as_personality_belief(self):
        output = judgment_output()
        normalized = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27}, current_event_ids={27})
        conn = QuestionConnection()
        cognitive_output.persist_slow_loop_output(
            conn, cycle_id=101, user_id='u', character_id='gojo',
            output=normalized, now=NOW,
        )
        insert = next(params for sql, params in conn.executed
                      if sql.startswith('INSERT INTO cognitive_questions'))
        judgment = json.loads(insert[5])['current_judgment']
        self.assertEqual(judgment['value'], 'yes')
        self.assertEqual(judgment['status'], 'committed')
        self.assertEqual(judgment['cycle_id'], 101)
        self.assertEqual(judgment['evidence_event_ids'], ['user-1', 'assistant-1'])
        self.assertEqual(judgment['evidence_refs'][0]['event_id'], 27)
        self.assertFalse(any(sql.startswith('INSERT INTO cognitive_beliefs') for sql, _ in conn.executed))

    def test_current_judgment_cannot_cite_fabricated_evidence(self):
        output = judgment_output()
        output['question_updates'][0]['evidence_refs'] = [999]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27}, current_event_ids={27})

    def test_resolution_keeps_only_validated_delta_recovery_fields(self):
        result, conn, _ = self._ingest({
            'type': 'resolution', 'kind': 'explicit_acceptance', 'novel': True,
            'question_key': 'nickname.acceptance', 'question_text': QUESTION,
            'content': '我明确接受她叫我宝宝。', 'value': 'yes',
            'evidence_quote': '我明确说了yes',
            'evidence_event_ids': ['user-1', 'assistant-1'],
            'replaces': ['她叫我宝宝，问我接不接受'],
            'untrusted_extra': 'must not persist',
        })
        insert = next(params for sql, params in conn.executed
                      if sql.startswith('INSERT INTO cognitive_questions'))
        recovery = json.loads(insert[5])['resolution']['bond_delta']
        self.assertEqual(recovery['question_key'], result['question_key'])
        self.assertEqual(recovery['value'], 'yes')
        self.assertNotIn('untrusted_extra', recovery)

    def test_stale_cycle_cannot_restore_belief_flagged_by_explicit_resolution(self):
        output = judgment_output()
        output['question_updates'] = []
        output['belief_updates'] = [{'belief_key': 'nickname.old_uncertainty',
                                     'evidence_refs': [27]}]
        old_beliefs = {'nickname.old_uncertainty': (
            '仍不确定是否接受这个称呼', .8, 'active',
            {'review_status': 'under_review',
             'review_reason': 'question_explicitly_resolved', 'resolution_event_id': 28},
        )}
        with patch.object(cognitive_output, '_load_keyed_map', side_effect=[{}, old_beliefs]), \
                patch.object(cognitive_output, '_belief_commit_decision') as decide:
            cognitive_output.persist_slow_loop_output(
                QuestionConnection(), cycle_id=101, user_id='u', character_id='gojo',
                output=output, now=NOW,
            )
        decide.assert_not_called()

    def test_clock_tick_alone_cannot_commit_a_current_judgment(self):
        conn = QuestionConnection()
        conn.event_metadata = [(27, 'scheduled_reflection', 'reflection:time',
                                'cognitive_scheduler', {})]
        normalized = cognitive_output.validate_slow_loop_output(
            judgment_output(), allowed_event_ids={27}, current_event_ids={27})
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.persist_slow_loop_output(
                conn, cycle_id=101, user_id='u', character_id='gojo',
                output=normalized, now=NOW,
            )


def judgment_output():
    """A narrow Slow Loop answer, deliberately not a stable belief candidate."""
    return {
        'cycle_summary': {'summary': '已经形成这个称呼问题的当前答案。',
                          'salient_change': '接受称呼', 'uncertainty': '',
                          'confidence': 'high'},
        'question_updates': [{
            'question_key': 'nickname.acceptance', 'question_text': QUESTION,
            'status': 'active', 'evidence_refs': [27],
            'current_judgment': {'value': 'yes', 'content': '我愿意接受她叫我宝宝。',
                                 'status': 'committed'},
        }],
        'belief_updates': [], 'hypothesis_updates': [], 'new_predictions': [],
        'evidence_refs': [{'event_id': 27, 'reason': '提问和延期回答承诺的原始对话。'}],
        'reflection_note': {'content': '', 'evidence_refs': []},
    }


if __name__ == '__main__':
    unittest.main()
