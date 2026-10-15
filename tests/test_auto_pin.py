# -*- coding: utf-8 -*-
import os
import sys
import unittest
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import auto_pin  # noqa: E402
import context_layer  # noqa: E402
import raw_events  # noqa: E402
from tests import test_cognitive_deterministic as acceptance  # noqa: E402


class AutoPinTests(unittest.TestCase):
    def setUp(self):
        context_layer.use_memory_store(True)

    def tearDown(self):
        context_layer.use_memory_store(False)

    def test_explicit_decision_is_pinned(self):
        rows = auto_pin.maybe_auto_pin(
            'u', 'gojo', '就按这个方案做 Recall v2',
            source_event_ids=['e-decision'])
        self.assertTrue(rows)
        self.assertEqual(rows[0]['pin_type'], 'explicit_decision')
        pins = context_layer.list_pins('u', 'gojo')
        self.assertTrue(any('就按这个方案' in (p.get('text') or '') for p in pins))

    def test_current_task_is_pinned(self):
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '先把 Recall v2 做完再看别的',
            source_event_ids=['e-task'])
        pins = context_layer.list_pins('u', 'gojo')
        self.assertTrue(any(p.get('pin_type') == 'current_task' for p in pins))

    def test_pending_request_do_not_push(self):
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '改完先给我看，不要 push',
            source_event_ids=['e-nopush'])
        pins = context_layer.list_pins('u', 'gojo')
        self.assertTrue(any(p.get('pin_type') == 'pending_request' for p in pins))
        self.assertTrue(any('不要 push' in (p.get('text') or '')
                            or '不要push' in (p.get('text') or '').replace(' ', '')
                            for p in pins))

    def test_allow_push_supersedes_block(self):
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '不要 push', source_event_ids=['e1'])
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '现在 push', source_event_ids=['e2'])
        pins = context_layer.list_pins('u', 'gojo', status=None, limit=20)
        blocked = [
            p for p in context_layer._PINS.values()
            if p['user_id'] == 'u' and 'git-push-block' in p['pin_id']
        ]
        self.assertTrue(any(p.get('status') == 'superseded' for p in blocked))
        active = context_layer.list_pins('u', 'gojo', status='active')
        self.assertTrue(any('现在 push' in (p.get('text') or '') for p in active))

    def test_casual_bath_is_not_pinned(self):
        auto_pin.maybe_auto_pin('u', 'gojo', '我要洗澡', source_event_ids=['e-bath'])
        self.assertEqual(context_layer.list_pins('u', 'gojo'), [])

    def test_repeated_read_does_not_raise_priority(self):
        auto_pin.maybe_auto_pin(
            'u', 'gojo', '就按这个方案', source_event_ids=['e1'])
        first = context_layer.list_pins('u', 'gojo')[0]
        p1 = first['priority']
        context_layer.list_pins('u', 'gojo')
        context_layer.list_pins('u', 'gojo')
        second = context_layer.list_pins('u', 'gojo')[0]
        self.assertEqual(second['priority'], p1)

    def test_default_context_pins_current_canonical_ids_and_ignores_assistant_history(self):
        now = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
        current_text = '就按这个方案'
        events = [
            {'event_id': 'assistant-old', 'role': 'assistant',
             'content': '不要 push', 'timestamp': now - timedelta(minutes=1)},
            {'event_id': 'canonical-1', 'role': 'user',
             'content': current_text, 'timestamp': now},
            {'event_id': 'canonical-2', 'role': 'user',
             'content': '继续', 'timestamp': now},
        ]
        with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
             patch.object(raw_events, 'get_hot_candidate_events', return_value=events), \
             patch.object(context_layer, '_support_items', return_value=([], '')):
            pack = context_layer.build_chat_context(
                'u', 'gojo', user_message=current_text, include_recall=False,
                current_event_id=('canonical-1', 'canonical-2'), now=now)
        self.assertFalse(pack.failed_closed)
        self.assertEqual(pack.recent_event_ids, ['assistant-old'])
        pins = list(context_layer._PINS.values())
        self.assertEqual(len(pins), 1)
        self.assertEqual(pins[0]['pin_type'], 'explicit_decision')
        self.assertEqual(pins[0]['text'], current_text)
        self.assertEqual(set(pins[0]['source_event_ids']),
                         {'canonical-1', 'canonical-2'})

    def test_disabled_context_auto_pin_does_not_call_writer_or_change_pins(self):
        now = datetime(2026, 10, 10, 5, 0, tzinfo=timezone.utc)
        for old_text, new_text in [('现在 push', '不要 push'),
                                   ('不要 push', '现在 push')]:
            with self.subTest(old_text=old_text, new_text=new_text):
                context_layer.reset_memory_store()
                auto_pin.maybe_auto_pin(
                    'u', 'gojo', old_text, source_event_ids=['old-pending'], now=now)
                auto_pin.maybe_auto_pin(
                    'u', 'gojo', new_text, source_event_ids=['later-user'], now=now)
                before = deepcopy(context_layer._PINS)
                events = [
                    {'event_id': 'older-hot', 'role': 'user', 'content': '你好',
                     'timestamp': now - timedelta(minutes=1)},
                    {'event_id': 'old-pending', 'role': 'user', 'content': old_text,
                     'timestamp': now},
                ]
                with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
                     patch.object(raw_events, 'get_hot_candidate_events', return_value=events), \
                     patch.object(context_layer, '_support_items', return_value=([], '')), \
                     patch.object(auto_pin, 'maybe_auto_pin', wraps=auto_pin.maybe_auto_pin) as writer:
                    for _attempt in range(2):
                        pack = context_layer.build_chat_context(
                            'u', 'gojo', user_message=old_text, include_recall=False,
                            current_event_id=('old-pending',), now=now,
                            auto_pin_enabled=False)
                        self.assertEqual(pack.recent_event_ids, ['older-hot'])
                        self.assertEqual(context_layer._PINS, before)
                writer.assert_not_called()


class CommunicationAgreementTests(unittest.TestCase):
    def setUp(self):
        context_layer.use_memory_store(True)

    def tearDown(self):
        context_layer.use_memory_store(False)

    def _write_agreement(self, text, event_id='agreement-1', role='user', **kwargs):
        event = {'event_id': event_id, 'role': role, 'content': text}
        with patch.object(raw_events, 'get_active_events_by_ids', return_value=[event]):
            return auto_pin.maybe_auto_pin(
                'u', 'gojo', text, source_event_ids=[event_id], **kwargs)

    def test_explicit_communication_agreement_has_source_and_original_words(self):
        text = '以后我发🦖就是想你了'
        rows = self._write_agreement(text)
        self.assertEqual(len(rows), 1)
        self.assertIn('communication_agreement', auto_pin.AUTO_PIN_TYPES)
        self.assertEqual(rows[0]['pin_type'], 'communication_agreement')
        self.assertEqual(rows[0]['text'], f'用户约定（原话）：「{text}」')
        self.assertEqual(rows[0]['source_event_ids'], ('agreement-1',))
        self.assertEqual(rows[0]['priority'], auto_pin.PIN_PRIORITY['communication_agreement'])
        self.assertIsNone(rows[0]['expires_at'])

    def test_explicit_phrases_include_bedtime_agreements(self):
        texts = ('以后我发晚安就是想你了', '我们约好睡觉前发🦖表示想你',
                 '约定🦖代表想你', '暗号是🦖', '我们的暗号：🦖代表想你',
                 '记住🦖就是想你了')
        for index, text in enumerate(texts):
            with self.subTest(text=text):
                rows = self._write_agreement(text, event_id=f'phrase-{index}')
                self.assertEqual([row['pin_type'] for row in rows], ['communication_agreement'])
                self.assertEqual(rows[0]['text'], f'用户约定（原话）：「{text}」')

    def test_original_words_are_not_clipped_or_whitespace_normalized(self):
        text = ' \n以后我发🦖就是想你了。\n' + '  晚安  睡觉\n' * 40 + ' '
        row = self._write_agreement(text)[0]
        self.assertGreater(len(text), 180)
        self.assertEqual(row['text'], f'用户约定（原话）：「{text}」')

    def test_character_messages_cannot_trigger_agreements(self):
        for role in ('assistant', 'gojo', 'system'):
            with self.subTest(role=role):
                self.assertEqual(self._write_agreement('以后我发🦖就是想你了', role=role), [])
        self.assertEqual(context_layer.list_pins('u', 'gojo'), [])

    def test_casual_chatter_and_questions_do_not_trigger_agreements(self):
        for text in ('晚安', '今天吃饭了', '我要睡觉了', '暗号是什么？', '以后我发🦖就是想你了？',
                     '约定俗成的说法罢了', '约定已经取消了', '约定已被撤销', '我们约好了吗', '我们约好了么'):
            with self.subTest(text=text):
                self.assertEqual(auto_pin.detect_auto_pins(text), [])

    def test_agreements_require_complete_matching_user_sources(self):
        text = '以后我发🦖就是想你了'
        event = {'event_id': 'e1', 'role': 'user', 'content': text}
        cases = (([], []), (['e1'], []), (['e1', 'missing'], [event]),
                 (['e1'], [dict(event, event_id='wrong')]),
                 (['e1'], [dict(event, content='其它原话')]),
                 (['e1', 'assistant'], [event, dict(event, event_id='assistant', role='assistant')]))
        for ids, sources in cases:
            with self.subTest(ids=ids, sources=sources), \
                    patch.object(raw_events, 'get_active_events_by_ids', return_value=sources):
                self.assertEqual(auto_pin.maybe_auto_pin('u', 'gojo', text, source_event_ids=ids), [])
        with patch.object(raw_events, 'get_active_events_by_ids',
                          side_effect=raw_events.SourceValidityError('db down')):
            self.assertEqual(auto_pin.maybe_auto_pin('u', 'gojo', text, source_event_ids=['e1']), [])
        self.assertEqual(context_layer.list_pins('u', 'gojo'), [])

    def test_matching_user_source_can_share_current_turn_with_another_user_event(self):
        text = '以后我发🦖就是想你了'
        sources = [{'event_id': 'e1', 'role': 'user', 'content': text},
                   {'event_id': 'e2', 'role': 'user', 'content': '继续'}]
        with patch.object(raw_events, 'get_active_events_by_ids', return_value=sources):
            rows = auto_pin.maybe_auto_pin('u', 'gojo', text, source_event_ids=['e1', 'e2'])
            replay = auto_pin.maybe_auto_pin('u', 'gojo', text, source_event_ids=['e2', 'e1'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]['source_event_ids']), {'e1', 'e2'})
        self.assertEqual(rows[0]['pin_id'], replay[0]['pin_id'])
        self.assertEqual(len(context_layer.list_pins('u', 'gojo')), 1)

    def test_distinct_agreements_in_the_same_source_batch_do_not_overwrite_each_other(self):
        first_text, second_text = '以后我发🦖就是想你了', '以后我发🦖就是想抱抱了'
        sources = [{'event_id': 'e1', 'role': 'user', 'content': first_text},
                   {'event_id': 'e2', 'role': 'user', 'content': second_text}]
        with patch.object(raw_events, 'get_active_events_by_ids', return_value=sources):
            first = auto_pin.maybe_auto_pin('u', 'gojo', first_text, source_event_ids=['e1', 'e2'])[0]
            second = auto_pin.maybe_auto_pin('u', 'gojo', second_text, source_event_ids=['e1', 'e2'])[0]
            replay = auto_pin.maybe_auto_pin('u', 'gojo', first_text, source_event_ids=['e2', 'e1'])[0]
        self.assertEqual(first['pin_id'], replay['pin_id'])
        self.assertNotEqual(first['pin_id'], second['pin_id'])
        self.assertEqual({p['text'] for p in context_layer.list_pins('u', 'gojo')},
                         {f'用户约定（原话）：「{first_text}」', f'用户约定（原话）：「{second_text}」'})

    def test_redefined_agreement_and_old_replay_keep_independent_sources(self):
        now = datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)
        old = '以后我发🦖就是想你了'
        new = '以后我发🦖就是想抱抱了'
        first = self._write_agreement(old, 'old', now=now - timedelta(days=1))[0]
        latest = self._write_agreement(new, 'new', now=now)[0]
        replay = self._write_agreement(old, 'old', now=now + timedelta(minutes=1))[0]
        self.assertEqual(first['pin_id'], replay['pin_id'])
        self.assertNotEqual(first['pin_id'], latest['pin_id'])
        pins = context_layer.list_pins('u', 'gojo')
        self.assertEqual({p['text'] for p in pins},
                         {f'用户约定（原话）：「{old}」', f'用户约定（原话）：「{new}」'})
        self.assertEqual(next(p for p in pins if p['pin_id'] == latest['pin_id'])['source_event_ids'], ('new',))

    def test_agreement_ids_keep_source_and_identity_distinctions_for_long_ids(self):
        pin_type = 'communication_agreement'
        long_user, long_character = 'u' * 120, 'c' * 120
        identities = ((long_user, long_character, 'e1'), (long_user, long_character, 'e2'),
                      (long_user + '2', long_character, 'e1'), (long_user, long_character + '2', 'e1'))
        ids = [auto_pin._pin_id(user, character, pin_type, source)
               for user, character, source in identities]
        self.assertEqual(len(set(ids)), 4)
        self.assertTrue(all(len(pid) <= 120 for pid in ids))

    def test_original_seven_types_keep_their_text_and_expiration_rules(self):
        now = datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)
        cases = (('current_task', '先把 Recall v2 做完'), ('explicit_decision', '就按这个方案'),
                 ('pending_request', '不要 push'), ('deadline', '今晚之前做好'),
                 ('explicit_promise', '我回来之后继续'), ('unresolved_question', '还没解决'),
                 ('unresolved_conflict', '互相冲突'))
        with patch.object(raw_events, 'get_active_events_by_ids') as lookup:
            for kind, text in cases:
                with self.subTest(kind=kind):
                    row = auto_pin.maybe_auto_pin('u', 'gojo', text, source_event_ids=[kind], now=now)[0]
                    self.assertEqual(row['pin_type'], kind)
                    self.assertEqual(row['text'], text)
                    expected = now + timedelta(days=14) if kind in ('current_task', 'deadline') else None
                    self.assertEqual(row['expires_at'], expected)
            lookup.assert_not_called()
        for kind, text in (('current_task', '晚安先把 Recall v2 做完'),
                           ('explicit_decision', '睡觉前就按这个方案')):
            self.assertEqual([p['pin_type'] for p in auto_pin.detect_auto_pins(text)], [kind])

    def test_agreement_survives_hot_window_and_source_deletion_keeps_it_in_prompt(self):
        """Persistent agreements survive source deletion in the long-term section."""
        text = '以后我发🦖就是想你了'
        now = datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)
        self._write_agreement(text, now=now)
        events = [{'event_id': f'later-{i}', 'role': 'user', 'content': '普通聊天',
                   'timestamp': now + timedelta(minutes=i)} for i in range(100)]
        for deleted in (set(), {'agreement-1'}):
            with self.subTest(deleted=deleted):
                pack = context_layer.assemble_from_events(
                    events, user_id='u', character_id='gojo', include_recall=False,
                    deleted_ids=deleted, now=now + timedelta(days=30))
                self.assertIn('[communication_agreement]', pack.pinned_prompt_text)
                self.assertIn(f'约定（原话）：「{text}」', pack.pinned_prompt_text)
                self.assertIn('长期有效', pack.pinned_prompt_text)
                self.assertNotIn('agreement-1', pack.recent_event_ids)

    def test_context_ignores_character_agreements_and_disabled_replay(self):
        now = datetime(2026, 10, 15, 5, 0, tzinfo=timezone.utc)
        text = '以后我发🦖就是想你了'
        events = [{'event_id': 'assistant-old', 'role': 'assistant', 'content': text, 'timestamp': now}]
        with patch.object(raw_events, 'deleted_event_ids', return_value=set()), \
                patch.object(raw_events, 'get_hot_candidate_events', return_value=events), \
                patch.object(context_layer, '_support_items', return_value=([], '')):
            context_layer.build_chat_context('u', 'gojo', user_message='你好', include_recall=False, now=now)
            self.assertEqual(context_layer.list_pins('u', 'gojo'), [])
            with patch.object(auto_pin, 'maybe_auto_pin', wraps=auto_pin.maybe_auto_pin) as writer:
                context_layer.build_chat_context(
                    'u', 'gojo', user_message=text, include_recall=False,
                    current_event_id='pending-user', now=now, auto_pin_enabled=False)
                writer.assert_not_called()
            self.assertEqual(context_layer.list_pins('u', 'gojo'), [])

    def test_expanded_verb_patterns_detect_agreements(self):
        for text in ('以后我发🦖代表想你了', '以后我发🦖表示想你了',
                     '以后我发🦖意思是想你了', '记住🦖代表想你了',
                     '记住🦖表示想你', '记住🦖意思是想你了',
                     '🦖的意思就是想你了', '🦖的意思是想你'):
            with self.subTest(text=text):
                pins = auto_pin.detect_auto_pins(text)
                self.assertTrue(any(p['pin_type'] == 'communication_agreement' for p in pins))

    def test_expanded_verbs_write_persistent_pins(self):
        for index, text in enumerate(('以后我发🦖代表想你了', '🦖的意思是想你')):
            with self.subTest(text=text):
                rows = self._write_agreement(text, event_id=f'expanded-{index}')
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].get('persistent'))

    def test_communication_agreement_pins_are_persistent(self):
        rows = self._write_agreement('以后我发🦖就是想你了')
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].get('persistent'))

    def test_non_agreement_pins_are_not_persistent(self):
        cases = ('先把 readme 做完', '就按这个方案', '不要 push', '今晚之前做好',
                 '我回来之后继续', '还没解决', '互相冲突')
        for index, text in enumerate(cases):
            with self.subTest(text=text):
                rows = auto_pin.maybe_auto_pin('u', 'gojo', text, source_event_ids=[f'transient-{index}'])
                self.assertTrue(rows)
                self.assertTrue(all(not row.get('persistent') for row in rows))

    def test_persistent_pin_survives_source_deletion_in_verified_derived(self):
        row = self._write_agreement('以后我发🦖就是想你了')[0]
        self.assertTrue(row.get('persistent'))
        pins = context_layer.list_pins('u', 'gojo')
        verified = context_layer._verified_derived(
            pins, deleted={'agreement-1'}, user_id='u', character_id='gojo')
        self.assertEqual([pin['pin_id'] for pin in verified], [row['pin_id']])
        self.assertEqual(verified[0]['source_event_ids'], ('agreement-1',))

    def test_transient_pin_still_dropped_when_sources_deleted(self):
        row = context_layer.upsert_pin(
            'u', 'gojo', 'test transient pin', pin_type='current_task',
            source_event_ids=('src-1',), persistent=False)
        self.assertEqual(context_layer._verified_derived(
            [row], deleted={'src-1'}, user_id='u', character_id='gojo'), [])

    def test_persistent_pins_formatted_in_dedicated_section(self):
        persistent_item = context_layer.ContextItem(
            item_id='pin:persistent', item_type='pinned', text='长期约定原话',
            source_event_ids=('e1',), priority=85,
            metadata={'pin': {'persistent': True}, 'pin_type': 'communication_agreement'})
        transient_item = context_layer.ContextItem(
            item_id='pin:transient', item_type='pinned', text='当前任务原话',
            source_event_ids=('e2',), priority=88,
            metadata={'pin': {'persistent': False}, 'pin_type': 'current_task'})
        block = context_layer._format_pinned_block([persistent_item, transient_item])
        permanent, working = block.split('【当前重要事项——工作集，不是永久记忆】')
        self.assertIn('长期有效', permanent)
        self.assertIn('一直遵守', permanent)
        self.assertIn(persistent_item.text, permanent)
        self.assertNotIn(transient_item.text, permanent)
        self.assertIn(transient_item.text, working)
        self.assertNotIn(persistent_item.text, working)
        self.assertNotIn('工作集', context_layer._format_pinned_block([persistent_item]))
        self.assertNotIn('长期有效', context_layer._format_pinned_block([transient_item]))
        self.assertEqual(context_layer._format_pinned_block([]), '')

    def test_agreement_matches_with_whitespace_in_source_content(self):
        text = '以后我发🦖就是想你了'
        event = {'event_id': 'e-ws', 'role': 'user', 'content': f'  {text}  \n'}
        with patch.object(raw_events, 'get_active_events_by_ids', return_value=[event]):
            rows = auto_pin.maybe_auto_pin('u', 'gojo', text, source_event_ids=['e-ws'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['pin_type'], 'communication_agreement')
        self.assertEqual(rows[0]['text'], f'用户约定（原话）：「{text}」')

    def test_whitespace_matching_preserves_distinct_source_owned_pin_ids(self):
        texts = ('以后我发🦖就是想你了', '以后我发🦖就是想抱抱了')
        sources = [{'event_id': f'padded-{index}', 'role': 'user', 'content': f'  {text}\n'}
                   for index, text in enumerate(texts)]
        with patch.object(raw_events, 'get_active_events_by_ids', return_value=sources):
            first = auto_pin.maybe_auto_pin('u', 'gojo', texts[0], source_event_ids=['padded-0', 'padded-1'])[0]
            second = auto_pin.maybe_auto_pin('u', 'gojo', texts[1], source_event_ids=['padded-0', 'padded-1'])[0]
            replay = auto_pin.maybe_auto_pin('u', 'gojo', texts[0], source_event_ids=['padded-1', 'padded-0'])[0]
        self.assertNotEqual(first['pin_id'], second['pin_id'])
        self.assertEqual(first['pin_id'], replay['pin_id'])
        self.assertEqual({row['text'] for row in context_layer.list_pins('u', 'gojo')},
                         {f'用户约定（原话）：「{text}」' for text in texts})

    def test_persistent_pin_still_respects_resolved_status(self):
        row = self._write_agreement('以后我发🦖就是想你了')[0]
        context_layer.resolve_pin(row['pin_id'])
        self.assertEqual(context_layer.list_pins('u', 'gojo'), [])
        self.assertEqual(context_layer._pin_items(context_layer.list_pins('u', 'gojo', status=None)), [])

    def test_schema_adds_persistent_default_without_rewriting_existing_pins(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        with patch.object(context_layer, 'get_conn', return_value=connection):
            context_layer.init_context_layer_tables()
        statements = [' '.join(call.args[0].split()) for call in cursor.execute.call_args_list]
        self.assertIn('ALTER TABLE pinned_context ADD COLUMN IF NOT EXISTS persistent BOOLEAN NOT NULL DEFAULT FALSE',
                      statements)
        self.assertFalse(any(statement.startswith('UPDATE pinned_context') for statement in statements))
        connection.commit.assert_called_once()

    def test_database_upsert_and_list_round_trip_persistent_flag(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value
        with patch.object(context_layer, '_USE_MEMORY_STORE', False), \
                patch.object(context_layer, 'get_conn', return_value=connection), \
                patch('cognitive_revision.sources_current_for_derivation', return_value=False) as validate:
            row = context_layer.upsert_pin(
                'u', 'gojo', '长期约定', pin_type='communication_agreement',
                source_event_ids=('source',), pin_id='persistent-db', persistent=True)
            validate.assert_not_called()
            self.assertEqual(row['status'], 'active')
            sql, params = cursor.execute.call_args.args
            self.assertIn('persistent', sql)
            self.assertIn('persistent = EXCLUDED.persistent', sql)
            self.assertEqual(params[-1], True)
            cursor.fetchall.return_value = [(
                row['pin_id'], 'u', 'gojo', row['text'], row['pin_type'], '["source"]',
                row['priority'], row['status'], None, row['created_at'], row['updated_at'], True)]
            pins = context_layer.list_pins('u', 'gojo')
            self.assertIn('updated_at, persistent', ' '.join(cursor.execute.call_args.args[0].split()))
        self.assertEqual(len(pins), 1)
        self.assertTrue(pins[0]['persistent'])
        self.assertEqual(pins[0]['source_event_ids'], ('source',))

    def test_database_transient_write_keeps_provenance_gate(self):
        connection = MagicMock()
        for current in (False, True):
            with self.subTest(current=current), \
                    patch.object(context_layer, '_USE_MEMORY_STORE', False), \
                    patch.object(context_layer, 'get_conn', return_value=connection), \
                    patch('cognitive_revision.sources_current_for_derivation', return_value=current) as validate:
                row = context_layer.upsert_pin('u', 'gojo', '当前任务', source_event_ids=('source',))
                validate.assert_called_once_with(connection.cursor.return_value, 'u', 'gojo', ('source',))
                self.assertFalse(row['persistent'])
                self.assertEqual(row['status'], 'active' if current else 'superseded')

    def test_legacy_and_null_database_rows_default_to_transient(self):
        legacy = ('pin', 'u', 'gojo', '旧任务', 'current_task', '["source"]',
                  80, 'active', None, None, None)
        for row in (legacy, legacy + (None,), legacy + (False,)):
            with self.subTest(row=row):
                self.assertFalse(context_layer._row_pin(row)['persistent'])
        self.assertTrue(context_layer._row_pin(legacy + (True,))['persistent'])

    def test_database_verification_preserves_transient_provenance_filter(self):
        persistent = {'pin_id': 'agreement', 'pin_type': 'communication_agreement',
                      'persistent': True, 'source_event_ids': ('deleted',)}
        transient = {'pin_id': 'task', 'pin_type': 'current_task',
                     'persistent': False, 'source_event_ids': ('deleted',)}
        summary = {'summary_id': 'summary', 'source_event_ids': ('deleted',)}
        with patch.object(context_layer, '_USE_MEMORY_STORE', False), \
                patch('memory_authority.filter_derived_answer_sources', return_value=[]) as validate:
            verified = context_layer._verified_derived(
                [transient, persistent, summary], deleted={'deleted'}, user_id='u', character_id='gojo')
        self.assertEqual(verified, [persistent])
        validate.assert_called_once_with([transient, summary], 'u', 'gojo')

    def test_final_prompt_revalidation_retains_only_persistent_pin_without_sources(self):
        items = [
            context_layer.ContextItem(
                item_id='pin:agreement', item_type='pinned', text='仍然有效的长期约定',
                source_event_ids=('deleted',),
                metadata={'pin': {'persistent': True, 'pin_type': 'communication_agreement'},
                          'pin_type': 'communication_agreement'}),
            context_layer.ContextItem(
                item_id='pin:task', item_type='pinned', text='失效任务',
                source_event_ids=('deleted',), metadata={'pin': {'persistent': False}}),
            context_layer.ContextItem(item_id='summary', item_type='rolling_summary',
                                      text='失效摘要', source_event_ids=('deleted',)),
            context_layer.ContextItem(item_id='episode', item_type='episodic_memory',
                                      text='失效经历', source_event_ids=('deleted',)),
        ]
        pack = context_layer.ChatContextPack(items=items)
        with patch('memory_authority.filter_derived_answer_sources', return_value=[]) as validate, \
                patch('cognitive_reader.build_cognitive_prompt_context', return_value=''):
            blocks = context_layer.revalidate_pack_blocks(pack, 'u', 'gojo', '你好')
        self.assertIn(items[0].text, blocks['pinned_prompt_text'])
        self.assertIn('长期有效', blocks['pinned_prompt_text'])
        self.assertNotIn(items[1].text, blocks['pinned_prompt_text'])
        self.assertEqual(blocks['summary_prompt_text'], '')
        self.assertEqual(blocks['episode_prompt_text'], '')
        candidates, user_id, character_id = validate.call_args.args
        self.assertEqual((user_id, character_id), ('u', 'gojo'))
        self.assertEqual([candidate['item'].item_id for candidate in candidates],
                         ['pin:task', 'summary', 'episode'])


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'set COGNITIVE_TEST_PGLITE to the local test-only package')
class PersistentAgreementDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(acceptance.OfflineDatabaseTests.setUpClass.__func__)
    tearDownClass = classmethod(acceptance.OfflineDatabaseTests.tearDownClass.__func__)
    tearDown = acceptance.OfflineDatabaseTests.tearDown
    sql = acceptance.OfflineDatabaseTests.sql
    source = acceptance.OfflineDatabaseTests.source

    def setUp(self):
        acceptance.OfflineDatabaseTests.setUp(self)
        context_layer.init_context_layer_tables()

    def test_canonical_agreement_survives_deleted_source_through_final_prompt(self):
        text = '以后我发🦖代表想你了'
        self.source('agreement', text)
        row = auto_pin.maybe_auto_pin('u', 'c', text, source_event_ids=['agreement'])[0]
        self.assertTrue(row['persistent'])
        context_layer.upsert_pin('u', 'c', '临时任务', source_event_ids=['agreement'], pin_id='transient')
        pins = context_layer.list_pins('u', 'c')
        self.assertEqual({pin['pin_id']: pin['persistent'] for pin in pins},
                         {row['pin_id']: True, 'transient': False})
        pack = context_layer.ChatContextPack(items=context_layer._pin_items(pins))
        self.sql("DELETE FROM chat_log WHERE event_id='agreement'")
        self.database.commit()
        verified = context_layer._verified_derived(
            pins, deleted={'agreement'}, user_id='u', character_id='c')
        self.assertEqual([pin['pin_id'] for pin in verified], [row['pin_id']])
        with patch('cognitive_reader.build_cognitive_prompt_context', return_value=''):
            blocks = context_layer.revalidate_pack_blocks(pack, 'u', 'c', '你好')
        self.assertIn(text, blocks['pinned_prompt_text'])
        self.assertIn('长期有效', blocks['pinned_prompt_text'])
        self.assertNotIn('临时任务', blocks['pinned_prompt_text'])
        context_layer.resolve_pin(row['pin_id'])
        self.assertFalse(any(pin['pin_id'] == row['pin_id']
                             for pin in context_layer.list_pins('u', 'c')))


if __name__ == '__main__':
    unittest.main()
