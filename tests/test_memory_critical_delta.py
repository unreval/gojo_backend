"""Critical merge rejection regressions; all model/DB boundaries are local fakes."""
import json
import os
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import patch

BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import raw_events
import user_memory


OLD = '她叫我宝宝，问我接不接受'
NEW = '她叫我宝宝，追问我是否接受，我明确说了yes'
DELTA = '我明确接受她叫我“宝宝”。'
EVENTS = [
    {'event_id': 'u1', 'role': 'user', 'content': '我还是叫你宝宝。你接不接受这个称呼？'},
    {'event_id': 'chat_reply:u1', 'role': 'assistant',
     'content': 'yes，接受你叫我宝宝。',
     'metadata': {'assistant_turn_id': 'chat_reply:u1', 'reply_to_event_id': 'u1'}},
]


def delta(**overrides):
    value = {
        'kind': 'explicit_acceptance', 'novel': True, 'content': DELTA,
        'question_text': '是否接受她叫我宝宝', 'question_key': 'nickname.acceptance',
        'value': 'yes', 'replaces': [OLD],
        'evidence_quote': 'yes，接受你叫我宝宝。',
        'evidence_event_ids': ['u1', 'chat_reply:u1'],
    }
    value.update(overrides)
    return value


class CriticalBondDeltaTests(unittest.TestCase):
    def extract(self, item, *, old=OLD, events=None, sources_active=True,
                repair=None, question_state=None, merge_result=(False, 0),
                cognitive_update=None, merge_targets=None):
        events = EVENTS if events is None else events
        candidate = {
            'content': NEW, 'replaces': merge_targets or [old],
            'evidence_quote': '我还是叫你宝宝',
            'evidence_event_ids': ['u1'],
        }
        payload = {'bond': dict(candidate), 'bond_merge': candidate,
                   'bond_delta': item, 'cognitive_update': cognitive_update}
        with ExitStack() as stack:
            for name, result in (
                ('plan_memory_corrections', []), ('get_long_memory', []),
                ('get_bond_memories', [(10, old, None)]),
                ('get_short_memory_for_prompt', []), ('_all_character_names', []),
                ('get_relations_text', ''),
            ):
                stack.enter_context(patch.object(user_memory, name, return_value=result))
            stack.enter_context(patch('characters.get_character', return_value={'name': '五条'}))
            outputs = [payload, {'bond_delta': repair}]
            stack.enter_context(patch('ai_client.create_chat', side_effect=[
                (json.dumps(value, ensure_ascii=False), None) for value in outputs]))
            for name, result in (
                ('sources_are_active', sources_active), ('already_derived', False),
                ('claim_processor', 'claimed'), ('get_active_events_by_ids', events),
                ('get_previous_active_user_events', []),
            ):
                stack.enter_context(patch.object(raw_events, name, return_value=result))
            stack.enter_context(patch.object(raw_events, 'finish_processor'))
            stack.enter_context(patch.object(raw_events, 'record_derived'))
            stack.enter_context(patch('smart_recall.reinforce_mentioned_facts'))
            stack.enter_context(patch('memory_lifecycle.reactivate_lifecycle_memories', return_value=0))
            stack.enter_context(patch('cognitive_events.question_extraction_state', return_value=question_state or []))
            lifecycle = stack.enter_context(patch('cognitive_events.ingest_question_update', return_value={'status': 'inserted'}))
            merge = stack.enter_context(patch.object(user_memory, 'merge_bond_memories', return_value=merge_result))
            save = stack.enter_context(patch.object(user_memory, 'save_bond_memory', return_value=True))
            resolve = stack.enter_context(patch.object(user_memory, 'resolve_bond_memories', return_value=(True, [(10, old)])))
            ok = user_memory.extract_and_save_memory(
                'user', EVENTS[0]['content'], EVENTS[1]['content'], 'gojo',
                source_event_id='u1', source_event_ids=['u1', 'chat_reply:u1'])
        return ok, merge, save, resolve, lifecycle

    def test_A_merge_reject_saves_only_explicit_acceptance_delta(self):
        ok, merge, save, resolve, lifecycle = self.extract(delta())
        self.assertTrue(ok)
        merge.assert_called_once()
        self.assertEqual(save.call_count, 1)
        self.assertEqual(save.call_args.args[3], DELTA.rstrip('。'))
        self.assertEqual(save.call_args.kwargs['source_event_ids'], ['u1', 'chat_reply:u1'])
        self.assertNotEqual(save.call_args.args[3], NEW)
        self.assertEqual(resolve.call_args.args[3], [OLD])
        self.assertEqual(lifecycle.call_args.kwargs['update']['value'], 'yes')

    def test_B_same_event_duplicate_is_not_appended(self):
        ok, _merge, save, resolve, lifecycle = self.extract(
            delta(content='她叫我宝宝', novel=False), old='她叫我宝宝')
        self.assertTrue(ok)
        save.assert_not_called()
        resolve.assert_not_called()
        lifecycle.assert_not_called()

    def test_model_novel_flag_cannot_bypass_exact_duplicate(self):
        _ok, _merge, save, resolve, _lifecycle = self.extract(delta(), old=DELTA)
        save.assert_not_called()
        resolve.assert_not_called()

    def test_no_canonical_assistant_evidence_cannot_save_character_yes(self):
        _ok, _merge, save, resolve, _lifecycle = self.extract(delta(), events=EVENTS[:1])
        save.assert_not_called()
        resolve.assert_not_called()

    def test_forged_event_or_quote_is_rejected(self):
        for item in (delta(evidence_event_ids=['u1', 'fake']), delta(evidence_quote='绝对没有说过')):
            with self.subTest(item=item):
                _ok, _merge, save, resolve, _lifecycle = self.extract(item)
                save.assert_not_called()
                resolve.assert_not_called()

    def test_deleted_source_does_not_commit_delta(self):
        _ok, _merge, save, resolve, _lifecycle = self.extract(delta(), sources_active=False)
        save.assert_not_called()
        resolve.assert_not_called()

    def test_user_explicit_answer_uses_user_canonical_source(self):
        user_event = {'event_id': 'u1', 'role': 'user', 'content': '我明确接受你叫我宝宝。'}
        item = delta(actor='user', content='她明确接受我叫她宝宝',
                     evidence_quote=user_event['content'], evidence_event_ids=['u1'])
        ok, _merge, save, _resolve, lifecycle = self.extract(item, events=[user_event])
        self.assertTrue(ok)
        self.assertEqual(save.call_args.kwargs['source_event_ids'], ['u1'])
        self.assertEqual(lifecycle.call_args.kwargs['update']['actor'], 'user')

    def test_explicit_promise_does_not_require_an_answer_value(self):
        events = [EVENTS[0], dict(EVENTS[1], content='明天给你回答。')]
        item = delta(kind='explicit_promise', value=None, content='明天给你回答',
                     evidence_quote='明天给你回答。')
        ok, _merge, save, resolve, lifecycle = self.extract(item, events=events)
        self.assertTrue(ok)
        self.assertEqual(lifecycle.call_args.kwargs['update']['type'], 'pending_answer')
        save.assert_called_once()
        resolve.assert_not_called()

    def test_literal_yes_can_resolve_a_scoped_question_without_restatement(self):
        events = [EVENTS[0], dict(EVENTS[1], content='yes')]
        ok, _merge, save, _resolve, _lifecycle = self.extract(delta(evidence_quote='yes'), events=events)
        self.assertTrue(ok)
        save.assert_called_once()

    def test_literal_no_cannot_support_an_acceptance_delta(self):
        events = [EVENTS[0], dict(EVENTS[1], content='no')]
        _ok, _merge, save, resolve, _lifecycle = self.extract(delta(evidence_quote='no'), events=events)
        save.assert_not_called()
        resolve.assert_not_called()

    def test_missing_delta_after_merge_reject_gets_bounded_schema_repair(self):
        ok, _merge, save, _resolve, _lifecycle = self.extract(None, repair=delta())
        self.assertTrue(ok)
        self.assertEqual(save.call_args.args[3], DELTA.rstrip('。'))
        self.assertEqual(save.call_count, 1)

    def test_successful_additive_merge_cannot_erase_independent_yes(self):
        ok, _merge, save, resolve, _lifecycle = self.extract(delta(), merge_result=(True, 1))
        self.assertTrue(ok)
        self.assertEqual(save.call_args.args[3], DELTA.rstrip('。'))
        self.assertEqual(resolve.call_args.args[3], [NEW])

    def test_unrelated_successful_merge_is_not_retired_by_acceptance(self):
        ok, _merge, save, resolve, _lifecycle = self.extract(
            delta(), merge_result=(True, 1), merge_targets=['她答应周六一起散步'])
        self.assertTrue(ok)
        save.assert_called_once()
        self.assertEqual(resolve.call_args.args[3], [OLD])

    def test_terminal_and_unrelated_fragments_are_not_destructively_merged(self):
        ok, merge, save, resolve, _lifecycle = self.extract(
            delta(), merge_result=(True, 2), merge_targets=[OLD, '她答应周六一起散步'])
        self.assertTrue(ok)
        merge.assert_not_called()
        save.assert_called_once()
        self.assertEqual(resolve.call_args.args[3], [OLD])

    def test_replay_uses_committed_delta_if_extractor_now_calls_it_duplicate(self):
        ok, _merge, save, resolve, _lifecycle = self.extract(
            delta(novel=False), question_state=[{'status': 'resolved', 'metadata': {
                'resolution': {'bond_delta': delta()},
            }}])
        self.assertTrue(ok)
        save.assert_called_once()
        resolve.assert_called_once()

    def test_future_answer_extraction_reaches_existing_question_ingress(self):
        events = [EVENTS[0], dict(EVENTS[1], content='明天给你回答。')]
        pending = {'type': 'pending_answer', 'question_text': '是否接受宝宝称呼',
                   'content': '明天给你回答', 'evidence_quote': '明天给你回答。',
                   'evidence_event_ids': ['u1', 'chat_reply:u1']}
        ok, _merge, _save, _resolve, lifecycle = self.extract(
            None, events=events, cognitive_update=pending)
        self.assertTrue(ok)
        self.assertEqual(lifecycle.call_args.kwargs['update']['type'], 'pending_answer')

    def test_rejected_merge_preserves_old_row_and_persists_delta_provenance(self):
        from tests.test_bond_recall_status import BondRecallStore, NOW
        store = BondRecallStore()
        store.bonds = [{'id': 10, 'user_id': 'user', 'character_id': 'gojo',
                        'kind': 'between', 'content': OLD, 'recall_status': 'active',
                        'timestamp': NOW}]
        canonical = [EVENTS[0], dict(EVENTS[1], reply_to_event_id='u1')]
        with patch.object(user_memory, 'get_conn', return_value=store), \
                patch.object(user_memory, '_merge_retains_target_signal', return_value=False), \
                patch.object(user_memory, '_bg_embed'), \
                patch('smart_recall.link_bond_to_fact', return_value=None), \
                patch('raw_events.sources_are_active', return_value=True), \
                patch('cognitive_events.ingest_question_update', return_value={'status': 'inserted'}):
            self.assertEqual(user_memory.merge_bond_memories(
                'user', 'gojo', 'between', [OLD], NEW), (False, 0))
            user_memory._apply_extracted_bond_delta(
                'user', 'gojo', delta(), canonical, 'u1', [(10, OLD, NOW)],
                merge_rejected=True)
        self.assertEqual(store.bonds[0]['content'], OLD)
        self.assertEqual(store.bonds[0]['recall_status'], 'superseded')
        active = [row for row in store.bonds if row['recall_status'] == 'active']
        self.assertEqual([row['content'] for row in active], [DELTA.rstrip('。')])
        links = [params for sql, params in store.executed if sql.startswith('INSERT INTO memory_source_events')]
        self.assertEqual({p[1] for p in links}, {'u1', 'chat_reply:u1'})


if __name__ == '__main__':
    unittest.main()
