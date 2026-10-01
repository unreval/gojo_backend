import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cognitive_events
import cognitive_output
import cognitive_worker
import user_memory
from tests.test_explicit_answer_sql import ExplicitAnswerFixture


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


class PendingResolutionTests(ExplicitAnswerFixture, unittest.TestCase):
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
        self.exchange('明天给你回答。')
        self.turn('a2', '我明确说了yes', 'assistant', reply='q')
        self.assert_answer()
        meta = self.reader()['questions'][0]['metadata']
        self.assertEqual(meta['pending_answer']['status'], 'fulfilled')
        self.assertTrue(meta['lifecycle_history'])
        self.assertEqual({r[0] for r in self.sql('SELECT event_id FROM chat_log')}, {'q','a','a2'})

    def test_e_future_promise_stays_open_and_queues_existing_slow_loop(self):
        self.exchange('明天给你回答。')
        row = self.reader()['questions'][0]
        self.assertEqual(row['status'], 'active')
        self.assertEqual(row['metadata']['pending_answer']['status'], 'pending')
        self.assertEqual(self.answers(), [])
        self.assertTrue(any('仍待回答' in str(b) for b in self.recall()['loose_bonds']))
        self.assertEqual(self.sql("SELECT count(*) FROM cognitive_cycles WHERE status='succeeded'")[0][0], 2)

    def test_stale_slow_loop_cannot_reopen_explicit_resolution_or_predictions(self):
        self.exchange()
        before = self.reader()
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.persist_slow_loop_output(self.database, cycle_id=1, user_id='u',
                character_id='c', output=judgment_output(), now=self.now)
        self.assertEqual(self.reader(), before)
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_predictions')[0][0], 0)

    def test_fabricated_quote_cannot_resolve_question(self):
        self.turn('q', QUESTION)
        self.assertTrue(user_memory.extract_and_save_memory('u', 'copied', 'yes', 'c',
            source_event_id='q', parsed_override={'value': 'yes','evidence_quote':'我拒绝了'}))
        self.assertEqual(self.answers(), [])
        self.assertEqual(self.reader()['questions'][0]['status'], 'active')

    def test_unrelated_assistant_event_cannot_supply_promise(self):
        self.turn('q', QUESTION)
        self.turn('a', '明天给你回答。', 'assistant', reply='unrelated-user')
        self.assertNotIn('pending_answer', self.reader()['questions'][0]['metadata'])
        self.assertEqual(self.answers(), [])

    def test_current_judgment_persists_on_question_not_as_personality_belief(self):
        self.exchange()
        self.assert_answer()
        self.assertEqual(self.reader()['questions'][0]['metadata']['current_judgment']['value'], 'yes')
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)

    def test_current_judgment_cannot_cite_fabricated_evidence(self):
        output = judgment_output()
        output['question_updates'][0]['evidence_refs'] = [999]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27}, current_event_ids={27})

    def test_resolution_keeps_only_validated_delta_recovery_fields(self):
        self.turn('q', QUESTION)
        self.turn('a', 'yes', 'assistant', reply='q', process=False)
        user_memory.extract_and_save_memory('u', '', '', 'c', source_event_id='q',
            source_event_ids=['q','a'], parsed_override={'bond_delta': {'novel':True,
                'question_key':'forged-romance', 'value':'no', 'untrusted_extra':'must not persist'}})
        self.deliver('a')
        decision = self.assert_answer()
        self.assertNotIn('untrusted_extra', str(decision))
        self.assertNotIn('forged-romance', str(self.reader()))

    def test_stale_cycle_cannot_restore_belief_flagged_by_explicit_resolution(self):
        self.exchange(reverse=True)
        self.turn('fix', '更正事件「a」：我刚才回答的不是「宝宝」这个称呼。')
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.persist_slow_loop_output(self.database, cycle_id=1, user_id='u',
                character_id='c', output=judgment_output(), now=self.now)
        self.assertEqual(self.answers(), [])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0], 0)

    def test_clock_tick_alone_cannot_commit_a_current_judgment(self):
        self.exchange('明天给你回答。')
        before = self.reader()['questions']
        self.assertEqual(cognitive_worker.run_worker_once(now=self.now + timedelta(days=2))['status'], 'idle')
        self.assertEqual(self.reader()['questions'], before)
        self.assertEqual(self.answers(), [])


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
