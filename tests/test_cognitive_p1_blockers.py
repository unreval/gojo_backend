"""P1 audit regressions: deterministic boundaries, no live DB or model."""
import os
import sys
import unittest
from contextlib import ExitStack
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend'))
import cognitive_events
import cognitive_output
import cognitive_queue
import cognitive_triggers
import user_memory
from tests import test_cognitive_pending_resolution as pending
from tests import test_cognitive_loop_v1 as queue_tests
from tests import test_memory_critical_delta as memory_tests
from tests.test_explicit_answer_sql import ExplicitAnswerFixture


class QuestionTransitionTests(ExplicitAnswerFixture, unittest.TestCase):
    def test_resolved_cannot_become_active(self):
        self.exchange()
        before = self.reader()['questions']
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.persist_slow_loop_output(self.database, cycle_id=1, user_id='u', character_id='c',
                output=pending.judgment_output(), now=self.now)
        self.assertEqual(self.reader()['questions'], before)

    def test_resolved_cannot_become_dormant(self):
        self.exchange()
        output = pending.judgment_output()
        output['question_updates'][0]['status']='dormant'
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.persist_slow_loop_output(self.database, cycle_id=1, user_id='u', character_id='c', output=output, now=self.now)
        self.assert_answer()

    def test_resolved_output_fulfills_old_pending_answer(self):
        self.exchange('明天给你回答。')
        self.turn('a2', 'yes', 'assistant', reply='q')
        self.assert_answer()
        self.assertEqual(self.reader()['questions'][0]['metadata']['pending_answer']['status'],'fulfilled')

    def test_explicit_resolution_retry_keeps_resolved_and_judgment(self):
        self.exchange('no')
        before = self.reader()['questions']
        self.deliver('a')
        self.assertEqual(self.reader()['questions'],before)
        self.assert_answer(value='no')

    def test_slow_loop_resolved_judgment_is_not_replaced_on_retry(self):
        self.exchange('no')
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.persist_slow_loop_output(self.database, cycle_id=1,user_id='u',character_id='c',output=pending.judgment_output(),now=self.now)
        self.assert_answer(value='no')

    def test_same_question_text_cannot_be_recreated_under_another_key(self):
        self.exchange()
        before = self.reader()['questions']
        cognitive_events.ingest_question_update(user_id='u',character_id='c',update={'question_key':'forged'},
            canonical_events=[{'event_id':'q','role':'user'}])
        self.assertEqual(self.reader()['questions'],before)

    def test_pending_answer_cannot_reopen_slow_loop_resolved(self):
        self.exchange('no')
        before = self.reader()['questions']
        self.turn('later','明天给你回答','assistant',reply='q')
        self.assertEqual(self.reader()['questions'],before)
        self.assert_answer(value='no')

    def test_unimplemented_reopen_operation_is_not_authorized(self):
        with self.assertRaises(ValueError):
            cognitive_events.question_transition_status('resolved', 'active', operation='reopen')


class PromiseRaceCursor(queue_tests.CycleCursor):
    def __init__(self):
        super().__init__(success=True)
        self.active = True
        self.next_day = False
        self.triggers = {}
        self.rowcount = 0

    def execute(self, sql, params=None):
        super().execute(sql, params)
        q = ' '.join(sql.split())
        if q.startswith('INSERT INTO cognitive_event_triggers'):
            key = (params[0], params[3], params[4])
            if key in self.triggers:
                self.one = None
            else:
                self.triggers[key] = 'pending'
                self.one = (91,)
        elif q.startswith('SELECT id, claimed_by_cycle_id, attempt_count'):
            self.many = []
        elif q.startswith('SELECT id FROM cognitive_cycles'):
            self.one = (50,) if self.active else None
        elif q.startswith('SELECT COUNT(*) FROM cognitive_cycles'):
            self.one = (0 if self.next_day else cognitive_queue.COGNITIVE_DAILY_CYCLE_LIMIT,)
        elif q.startswith('UPDATE cognitive_cycles') and "status = 'succeeded'" in q:
            self.active = False
        elif q.startswith('UPDATE cognitive_event_triggers') and "status = 'suppressed'" in q:
            self.rowcount = 0
            if "payload->>'reason' IS DISTINCT FROM 'pending_answer'" not in q:
                for key in self.triggers:
                    if self.triggers[key] == 'pending':
                        self.triggers[key] = 'suppressed'
                        self.rowcount += 1
        elif q.startswith('SELECT completed_at FROM cognitive_cycles'):
            self.one = None
        elif q.startswith('SELECT id, trigger_class, priority, event_id, created_at'):
            self.many = ([(91, 'question_reactivation', 300, 27, pending.NOW)]
                         if "trigger_class = 'question_reactivation'" in q
                         and 'pending' in self.triggers.values() else [])
        elif q.startswith('SELECT COALESCE(MAX(output_state_version)'):
            self.one = (14,)
        elif q.startswith('INSERT INTO cognitive_cycles'):
            self.one = (51,)
        elif q.startswith('UPDATE cognitive_event_triggers') and "SET status = 'claimed'" in q:
            for key in self.triggers:
                if self.triggers[key] == 'pending':
                    self.triggers[key] = 'claimed'


class PromiseDailyLimitTests(unittest.TestCase):
    def test_arrival_during_last_cycle_survives_cleanup_and_is_claimed_next_day_once(self):
        cursor = PromiseRaceCursor()
        conn = queue_tests.TransactionConnection(cursor)
        args = dict(event_id=27, user_id='u', character_id='gojo',
                    trigger_class='question_reactivation', occurrence_key='question:71',
                    payload={'reason': 'pending_answer'})
        self.assertEqual(cognitive_triggers.create_trigger_occurrence(conn, **args), 91)
        self.assertEqual(cognitive_queue.aggregate_pending_triggers(
            'u', 'gojo', conn=conn, now=pending.NOW)['status'], 'active_cycle_exists')
        cognitive_queue.commit_cycle_success(50, conn=conn, now=pending.NOW)
        self.assertEqual(list(cursor.triggers.values()), ['pending'])
        self.assertIsNone(cognitive_triggers.create_trigger_occurrence(conn, **args))
        self.assertEqual(cognitive_queue.aggregate_pending_triggers(
            'u', 'gojo', conn=conn, now=pending.NOW)['status'], 'daily_limit')
        self.assertEqual(list(cursor.triggers.values()), ['pending'])
        cursor.next_day = True
        result = cognitive_queue.aggregate_pending_triggers(
            'u', 'gojo', conn=conn, now=pending.NOW + timedelta(days=1))
        self.assertEqual(result['status'], 'queued')
        self.assertEqual(list(cursor.triggers.values()), ['claimed'])
        self.assertEqual(len(cursor.triggers), 1)


from tests.canonical_memory_fixture import CanonicalMemoryFixture


class OperationEvidenceTests(CanonicalMemoryFixture, unittest.TestCase):
    def validate(self, text, quote, value='yes', kind='explicit_acceptance', **extra):
        item = memory_tests.delta(evidence_quote=quote, value=value, kind=kind,
                                  content='我接受这个称呼' if value == 'yes' else '我不接受这个称呼', **extra)
        events = [memory_tests.EVENTS[0], dict(memory_tests.EVENTS[1], content=text,
                  reply_to_event_id='u1')]
        return user_memory._validated_turn_delta('u', item, events, 'u1')

    def test_negative_substrings_cannot_support_positive(self):
        for negative, positive in [('不接受', '接受'), ('不是', '是'), ('不要', '要'),
                                   ('不愿意', '愿意'), ('不可以', '可以'), ('不同意', '同意')]:
            with self.subTest(text=negative):
                self.assertIsNone(self.validate(negative, positive))

    def test_complete_negative_fragments_are_supported(self):
        for text in ('不接受', '不是', '不要', '不愿意', '不可以', '不同意', 'no', 'いいえ'):
            with self.subTest(text=text):
                self.assertTrue(self.validate(text, text, 'no', 'explicit_rejection'))

    def test_complete_positive_fragments_are_supported(self):
        for text in ('我接受', '是', '要', '愿意', '可以', '同意', 'yes', 'はい'):
            with self.subTest(text=text):
                self.assertTrue(self.validate(text, text))

    def test_ambiguous_reported_conditional_and_double_negative_rejected(self):
        for text, quote in [('不是不接受', '接受'), ('如果你问，我接受', '我接受'),
                            ('他说我接受', '我接受'), ('I did not say yes', 'yes'),
                            ('はいとは言っていない', 'はい'), ('我接受吗？', '我接受'),
                            ('我接受，但也不接受', '我接受'),
                            ('我接受你叫我宝宝才怪', '接受'), ('她接受这个称呼', '接受')]:
            with self.subTest(text=text):
                self.assertIsNone(self.validate(text, quote))

    def test_actor_and_kind_must_match(self):
        self.assertIsNone(self.validate('我接受', '我接受', actor='user'))
        self.assertIsNone(self.validate('不接受', '不接受', 'no', 'explicit_acceptance'))

    def test_reply_linkage_must_match(self):
        events = [memory_tests.EVENTS[0], dict(memory_tests.EVENTS[1],
                  content='yes', reply_to_event_id='wrong')]
        self.assertIsNone(user_memory._validated_turn_delta(
            'u', memory_tests.delta(evidence_quote='yes'), events, 'u1'))


    def test_cognitive_update_cannot_bypass_bond_delta_gate(self):
        for index, candidate in enumerate(('yes','no')):
            events = [dict(memory_tests.EVENTS[0],event_id=f'u{index}'),
                dict(memory_tests.EVENTS[1],event_id=f'a{index}',content='不接受',metadata={},reply_to_event_id=f'u{index}')]
            self.assertTrue(self.ingest_sources(events,primary=f'u{index}',source_ids=[f'u{index}',f'a{index}'],
                model_payload={'cognitive_update':{'value':candidate,'evidence_quote':'接受'}}))
        rows = self.sql("SELECT metadata->'resolution'->>'value' FROM cognitive_questions WHERE status='resolved'")
        self.assertEqual(rows,[('no',),('no',)])
        self.assertEqual(self.sql('SELECT count(*) FROM cognitive_beliefs')[0][0],0)

    def test_ingress_itself_rejects_invalid_polarity(self):
        self.assertTrue(self.ingest_sources([memory_tests.EVENTS[0],dict(memory_tests.EVENTS[1],content='不接受')],
            source_ids=['u1','chat_reply:u1'],model_payload={'value':'yes','evidence_quote':'接受'}))
        self.assertEqual(self.sql("SELECT metadata->'resolution'->>'value' FROM cognitive_questions WHERE status='resolved'"),[('no',)])


    def test_valid_negative_is_persisted_as_no(self):
        self.assertTrue(self.ingest_sources([memory_tests.EVENTS[0],dict(memory_tests.EVENTS[1],content='不接受')],source_ids=['u1','chat_reply:u1']))
        self.assertEqual(self.sql("SELECT metadata->'resolution'->>'value' FROM cognitive_questions WHERE status='resolved'"),[('no',)])
        from cognitive_reader import fetch_cognitive_reader_state
        self.assertIn('不接受',fetch_cognitive_reader_state('u','c',conn=self.database)['questions'][0]['metadata']['resolution']['content'])

    def test_summary_content_cannot_reverse_the_validated_answer(self):
        item = memory_tests.delta(evidence_quote='不接受', value='no', kind='explicit_rejection')
        events = [memory_tests.EVENTS[0], dict(memory_tests.EVENTS[1], content='不接受', reply_to_event_id='u1')]
        self.assertIsNone(user_memory._validated_turn_delta('u', item, events, 'u1'))

    def test_explicit_reply_link_cannot_be_overridden_by_deterministic_event_id(self):
        events = [memory_tests.EVENTS[0], dict(memory_tests.EVENTS[1], reply_to_event_id='wrong')]
        with patch('raw_events.get_active_events_by_ids', return_value=events), \
                patch('raw_events.get_previous_active_user_events', return_value=[]), \
                patch('raw_events.get_previous_active_turn_events', return_value=[]):
            view = user_memory._canonical_turn_sources(
                'u', 'gojo', ['u1', 'chat_reply:u1'], 'u1', 'copied user', 'copied assistant')
        self.assertEqual(len(view['canonical_turn_events']), 1)

    def test_pending_answer_requires_witnessed_promise_not_a_resolution(self):
        for text, accepted in [('明天给你回答', True), ('明天不会给你回答', False),
                               ('I will answer tomorrow.', True), ('明日答える。', True)]:
            with self.subTest(text=text):
                item = memory_tests.delta(type='pending_answer', kind='explicit_promise',
                    content=text, value=None, evidence_quote=text)
                events = [memory_tests.EVENTS[0], dict(memory_tests.EVENTS[1], content=text, reply_to_event_id='u1')]
                self.assertEqual(bool(user_memory._validated_turn_delta('u', item, events, 'u1')), accepted)

    def test_unknown_operation_or_kind_cannot_enter_cognitive_update(self):
        events = [memory_tests.EVENTS[0], dict(memory_tests.EVENTS[1], content='yes', reply_to_event_id='u1')]
        for extra in ({'type': 'reopen'}, {'kind': 'user_fact'}, {'question_text': '', 'question_key': ''}):
            with self.subTest(extra=extra):
                item = memory_tests.delta(evidence_quote='yes', **extra)
                self.assertIsNone(user_memory._validated_turn_delta('u', item, events, 'u1'))


class RuntimeCognitionTests(unittest.TestCase):
    def pack(self, unresolved=False):
        return SimpleNamespace(
            failed_closed=False, memory_text='', relationship_prompt_text='canonical-relationship',
            cognitive_prompt_text=('unresolved-question-marker' if unresolved else 'committed-judgment-marker'),
            expression_rules='', hot_messages=[])

    def assert_contract(self, prompt, unresolved):
        self.assertIn('canonical-relationship', prompt)
        self.assertIn('unresolved-question-marker' if unresolved else 'committed-judgment-marker', prompt)
        self.assertIn('READ → EXPRESS', prompt)
        self.assertIn('不能现场决定 yes/no', prompt)
        for legacy in ('三个可选框架', '拿不准就【不是】', '你可以重新表态', '你自己的 core 判断'):
            self.assertNotIn(legacy, prompt)

    def test_missing_cognition_does_not_reauthorize_a_relationship_decision(self):
        from shared_relation_prompt import format_cognitive_expression_context
        for pack in (None, SimpleNamespace(failed_closed=True)):
            prompt = format_cognitive_expression_context(pack)
            self.assertIn('当前认知不可用', prompt)
            self.assertIn('不能现场决定 yes/no', prompt)

    def test_diary_reads_judgment_and_preserves_unresolved(self):
        import diary_engine
        for unresolved in (False, True):
            with self.subTest(unresolved=unresolved), ExitStack() as stack:
                for name, value in [('get_character', {'name': '五条'}), ('get_bond_memories', []),
                                    ('get_long_memory', []), ('get_first_interaction_days', 2),
                                    ('_gather_daily_diary_material', ('真实事件', '复盘', 1))]:
                    stack.enter_context(patch.object(diary_engine, name, return_value=value))
                stack.enter_context(patch.object(diary_engine, 'db_diary', SimpleNamespace(
                    count_char_diaries_since=Mock(return_value=0), has_named_self=Mock(return_value=True),
                    add_char_diary=Mock(return_value=(1, pending.NOW)))))
                stack.enter_context(patch('context_layer.build_chat_context', return_value=self.pack(unresolved)))
                llm = stack.enter_context(patch('ai_client.create_chat', return_value=('{}', None)))
                diary_engine.generate_char_diary('gojo', 'u', topic='是否接受称呼')
                self.assert_contract(llm.call_args.kwargs['messages'][0]['content'], unresolved)

    def test_promise_reads_relevant_judgment_and_preserves_unresolved(self):
        import proactive_scheduler as scheduler
        for unresolved in (False, True):
            with self.subTest(unresolved=unresolved), ExitStack() as stack:
                for name, value in [('get_character', {'name': '五条'}), ('get_bond_memories', []),
                                    ('get_relations_text', ''), ('get_temporal_snapshot', {}),
                                    ('build_prompt_context', '')]:
                    stack.enter_context(patch.object(scheduler, name, return_value=value))
                load = stack.enter_context(patch('context_layer.load_profile_transcript',
                    return_value=('recent', self.pack(unresolved))))
                llm = stack.enter_context(patch.object(scheduler.claude_client.messages, 'create'))
                stack.enter_context(patch.object(scheduler, 'extract_text', return_value='{}'))
                scheduler.generate_from_promise(dict(id=1, character_id='gojo', user_id='u',
                    context='明天回答称呼问题', origin_text='是否接受称呼', trigger_kind='time'), pending.NOW)
                self.assert_contract(llm.call_args.kwargs['messages'][0]['content'], unresolved)
                self.assertIn('是否接受称呼', load.call_args.kwargs['user_message'])

    def test_diary_reaction_reads_judgment_and_preserves_unresolved(self):
        import diary_scheduler
        for unresolved in (False, True):
            with self.subTest(unresolved=unresolved), ExitStack() as stack:
                stack.enter_context(patch('characters.get_character', return_value={'name': '五条'}))
                stack.enter_context(patch('user_memory.get_bond_memories', return_value=[]))
                stack.enter_context(patch('character_relations.get_relations_text', return_value=''))
                load = stack.enter_context(patch('context_layer.load_profile_transcript',
                    return_value=('recent', self.pack(unresolved))))
                client = stack.enter_context(patch('anthropic.Anthropic'))
                stack.enter_context(patch('ai_client.extract_text', return_value='{}'))
                diary_scheduler._generate_comment_reaction('gojo', '日记内容', '答案呢', 1)
                self.assert_contract(client.return_value.messages.create.call_args.kwargs['messages'][0]['content'], unresolved)
                self.assertIn('答案呢', load.call_args.kwargs['user_message'])

    def test_full_relationship_reader_has_no_independent_judgment_authority(self):
        import relationship_reader as reader
        state = dict.fromkeys(('warmth', 'intimacy', 'trust', 'attachment', 'commitment', 'passion'), 0)
        state['friction'] = {}
        panel = {
            'ledger': {**state, 'has_state_row': True},
            'attachment': {'status': 'observed', 'value': 0},
            'commitment': {'status': 'observed', 'value': 0},
            'engagement_style': {'status': 'unassessed', 'value': 'unassessed'},
            'romantic_label': {'value': 'unresolved'},
            'romantic_openness': {'status': 'unassessed', 'value': 'unassessed'},
            'care': {'active': [], 'historical': []},
            'boundary': {'active': [], 'historical': []},
            'internal_conflict': {'status': 'unassessed', 'value': 'unassessed'},
            'direction': {'status': 'unassessed', 'value': 'unassessed'},
            'legacy_label': {'value': 'legacy:ledger_unassessed'},
        }
        with ExitStack() as stack:
            for name, value in [('build_relationship_panel', panel),
                    ('compute_tone', ''), ('compute_flirt_response', {}),
                    ('compute_pursue_withdraw', {}), ('_temporal_note', ''), ('initiative_guidance', '')]:
                stack.enter_context(patch.object(reader, name, return_value=value))
            stack.enter_context(patch.object(reader, 'load_offline_continuity_state', return_value=None))
            for compact in (False, True):
                text = reader.build_state_summary('u', 'gojo', compact=compact)
                self.assertNotIn('你自己的 core 判断', text)
                self.assertNotIn('你可以重新表态', text)
                self.assertIn('unresolved', text)


if __name__ == '__main__':
    unittest.main()
