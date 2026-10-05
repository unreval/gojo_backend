"""Role-facing readers adapt old prose without changing stored evidence."""
import asyncio
import json
import os
import sys
import unittest
from unittest.mock import patch


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cognitive_reader  # noqa: E402
import context_layer  # noqa: E402
import memory_lifecycle  # noqa: E402
import relationship_panel  # noqa: E402
import rolling_summary  # noqa: E402
import route_diary  # noqa: E402
import route_group  # noqa: E402
from context_budget import ContextItem  # noqa: E402


class RoleViewConsumerTests(unittest.TestCase):
    def test_relationship_panel_projects_only_display_values(self):
        stance = {'id': 1, 'type': 'care_admission',
                  'content': '角色告诉用户明天再答复。', 'status': 'active'}
        belief = {'kind': 'belief', 'statement': '用户告诉角色她今天很累。',
                  'source_event_ids': ('event-1',)}
        with patch.object(relationship_panel, 'read_state', return_value={}), patch.object(
                relationship_panel, 'list_declared_stances', return_value=[stance]), patch.object(
                relationship_panel, 'read_relationship_semantic_state', return_value={
                    'engagement_style': belief,
                }):
            panel = relationship_panel.build_relationship_panel('user-1', 'gojo')
        self.assertEqual(stance['content'], '角色告诉用户明天再答复。')
        self.assertEqual(belief['statement'], '用户告诉角色她今天很累。')
        self.assertEqual(panel['care']['active'][0]['content'], '我告诉她明天再答复。')
        self.assertEqual(panel['engagement_style']['value'], '她告诉我她今天很累。')
        self.assertEqual(panel['engagement_style']['source_event_ids'], ('event-1',))

    def test_sticky_ui_and_chat_use_same_view_without_changing_raw_trigger(self):
        raw = '用户表示今天很累。'
        row = (1, 'gojo', 'note-1', raw, 'active', 'cognitive_slow_loop',
               [], None, None, None, None, None, None, False, None, None, True,
               {'trigger_snippet': '用户原话：我很累'})
        public = cognitive_reader._serialize_sticky_row(row)
        self.assertEqual(public['content'], '她说今天很累。')
        self.assertEqual(public['trigger_snippet'], '用户原话：我很累')
        note = {'note_key': 'note-1', 'content': raw, 'status': 'active',
                'source_event_refs': [{'source_type': 'raw_event', 'source_id': 'event-1'}]}
        state = {'beliefs': [], 'questions': [], 'hypotheses': [],
                 'sticky_notes': [note], 'predictions': []}
        with patch('raw_events.get_active_events_by_ids', return_value=[{'event_id': 'event-1'}]):
            items = cognitive_reader.iter_active_cognitive_items(
                'user-1', 'gojo', _state=state)
        self.assertEqual(len(items), 1)
        self.assertIn(public['content'], items[0]['text'])
        self.assertEqual(note['content'], raw)

    def test_cognitive_dedup_uses_identity_not_display_text(self):
        state = {'beliefs': [], 'questions': [], 'hypotheses': [],
                 'sticky_notes': [
                     {'note_key': 'same', 'content': '用户表示累了。', 'status': 'active',
                      'source_event_refs': [{'source_type': 'raw_event', 'source_id': 'event-1'}]},
                     {'note_key': 'same', 'content': '她说累了。', 'status': 'active',
                      'source_event_refs': [{'source_type': 'raw_event', 'source_id': 'event-1'}]},
                 ], 'predictions': []}
        with patch('raw_events.get_active_events_by_ids', return_value=[{'event_id': 'event-1'}]):
            items = cognitive_reader.iter_active_cognitive_items(
                'user-1', 'gojo', _state=state)
        self.assertEqual(len(items), 1)

    def test_derived_history_is_projected_after_source_selection(self):
        item = ContextItem('sum-1', 'rolling_summary', '用户表示今天很累。',
                           source_event_ids=('event-1',))
        rendered = context_layer._format_summary_block([item], 'gojo')
        self.assertIn('她说今天很累。', rendered)
        self.assertEqual(item.text, '用户表示今天很累。')
        self.assertEqual(item.source_event_ids, ('event-1',))

    def test_rolling_summary_inputs_label_raw_roles_without_rewriting_messages(self):
        events = [
            {'role': 'user', 'content': '用户手册放在桌上'},
            {'role': 'assistant', 'content': '我看到了'},
        ]
        prompt = rolling_summary._summary_user(events)
        self.assertIn('她: 用户手册放在桌上', prompt)
        self.assertIn('我: 我看到了', prompt)
        self.assertEqual(events[0]['content'], '用户手册放在桌上')

    def test_group_history_names_current_and_other_characters(self):
        history = [
            {'sender_type': 'user', 'sender_id': 'user-1', 'sender_name': '群主',
             'jp': '', 'zh': '早上好'},
            {'sender_type': 'character', 'sender_id': 'gojo', 'sender_name': '五条悟',
             'jp': 'おはよう', 'zh': '早上好'},
            {'sender_type': 'character', 'sender_id': 'geto', 'sender_name': '夏油杰',
             'jp': 'やあ', 'zh': '你好'},
        ]
        gojo_view = route_group._history_text(history, 'gojo')
        geto_view = route_group._history_text(history, 'geto')
        self.assertIn('她：早上好', gojo_view)
        self.assertIn('我：おはよう', gojo_view)
        self.assertIn('夏油杰：やあ', gojo_view)
        self.assertIn('五条悟：おはよう', geto_view)
        self.assertIn('我：やあ', geto_view)
        self.assertEqual(history[1]['jp'], 'おはよう')

    def test_diary_ui_projects_character_entry_but_keeps_user_comment_raw(self):
        raw = {'id': 7, 'content': '用户表示今天很累，我记下了。',
               'emotion': '平静', 'comments': [
                   {'id': 2, 'content': '用户手册是我写的'}]}
        with patch.object(route_diary.db_diary, 'list_char_diaries', return_value=[raw]):
            response = asyncio.run(route_diary.get_char_diary('gojo', 'user-1'))
        entry = json.loads(response.body)['diaries'][0]
        self.assertEqual(entry['content'], '她说今天很累，我记下了。')
        self.assertEqual(entry['comments'][0]['content'], '用户手册是我写的')
        self.assertEqual(raw['content'], '用户表示今天很累，我记下了。')

    def test_fast_sticky_recall_projects_after_selection(self):
        public = {'id': 3, 'note_key': 'note-3', 'content': '她说今天很累。',
                  'source': memory_lifecycle.MEMORY_LIFECYCLE_SOURCE,
                  'source_event_refs': [{'source_id': 'event-3'}],
                  'expires_at': None, 'updated_at': None}
        with patch.object(cognitive_reader, 'list_user_facing_sticky_notes',
                          return_value=[public]) as shared_reader:
            notes = memory_lifecycle.recall_sticky_notes('user-1', 'gojo')
        self.assertEqual(notes[0]['content'], '她说今天很累。')
        self.assertEqual(notes[0]['source_event_refs'], public['source_event_refs'])
        shared_reader.assert_called_once_with('user-1', 'gojo', limit=6)


if __name__ == '__main__':
    unittest.main()
