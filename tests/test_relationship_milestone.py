"""Relationship milestones: explicit answers only, switch off by default."""
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gojo_backend'))

import cognitive_events
import cognitive_reader
import cognitive_revision
import relationship_semantics
import shared_relation_prompt
from tests.test_explicit_answer_sql import ExplicitAnswerFixture

_EXPRESSION_HASH = 'd9854a1fedf09c242098ccc0c504fb8bfdbbb48f7373d7099506194414e3d1b1'


def _kind(text):
    parts = cognitive_revision._exchange_parts(text)
    return [(part['kind'], part['value']) for part in parts]


class MilestoneGrammarTests(unittest.TestCase):
    def setUp(self):
        self.flag = patch('config.RELATIONSHIP_MILESTONE_ENABLED', True)
        self.flag.start()
        self.addCleanup(self.flag.stop)

    def test_switch_off_leaves_affection_literal_and_nickname_untouched(self):
        with patch('config.RELATIONSHIP_MILESTONE_ENABLED', False):
            self.assertEqual(_kind('我喜欢你'), [('literal', None)])
            self.assertEqual(_kind('俺も好きだ'), [('literal', None)])
            parts = cognitive_revision._exchange_parts('我还是叫你宝宝。你接不接受这个称呼？')
            self.assertEqual(parts[1]['kind'], 'question')
            self.assertEqual(parts[1]['value'], '宝宝')

    def test_affection_and_proposal_are_narrow(self):
        self.assertEqual(_kind('我喜欢你')[0][0], 'question')
        self.assertEqual(_kind('我喜欢你')[0][1]['surface'], 'affection')
        self.assertEqual(_kind('俺も好きだ')[0][1]['surface'], 'affection')
        self.assertEqual(_kind('付き合おう')[0][1]['surface'], 'proposal')
        self.assertEqual(_kind('好啊')[0], ('milestone_reply', {'reply': 'accept', 'text': '好啊'}))
        self.assertEqual(_kind('我也是')[0][0], 'milestone_reply')
        self.assertEqual(_kind('不行')[0][1]['reply'], 'reject')

    def test_quoted_hypothetical_negative_third_person_and_questions_stay_literal(self):
        for text in (
            '「我喜欢你」',
            '如果我喜欢你',
            '我不喜欢你',
            '我喜欢她',
            '你喜欢我吗',
            '好き？',
            'ラーメンが好き',
            '你也答应了',
            '分手',
            '我不喜欢你了',
            '別れよう',
        ):
            with self.subTest(text=text):
                self.assertEqual(_kind(text), [('literal', None)])

    def test_normalize_mutual_feelings_does_not_infer_prose(self):
        self.assertEqual(relationship_semantics.normalize_mutual_feelings('mutual'), 'mutual')
        self.assertEqual(relationship_semantics.normalize_mutual_feelings('unresolved'), 'unresolved')
        self.assertIsNone(relationship_semantics.normalize_mutual_feelings('我喜欢你'))
        self.assertIsNone(relationship_semantics.normalize_mutual_feelings('yes'))
        self.assertEqual(relationship_semantics.MUTUAL_FEELINGS_KEY, 'relationship.mutual_feelings')


class MilestonePromptTests(unittest.TestCase):
    def test_switch_off_prompt_is_byte_identical(self):
        rules = shared_relation_prompt._EXPRESSION_ONLY_RULES
        self.assertEqual(hashlib.sha256(rules.encode('utf-8')).hexdigest(), _EXPRESSION_HASH)
        bare = shared_relation_prompt.build_relation_rules(None, 0, 0)
        self.assertEqual(
            bare,
            shared_relation_prompt._build_meet_line(None, 0, 0) + rules,
        )
        with patch('config.RELATIONSHIP_MILESTONE_ENABLED', False), \
             patch('relationship_reader.build_state_summary', return_value='LEDGER'), \
             patch('cognitive_reader.build_cognitive_prompt_context', return_value='COG'), \
             patch('cognitive_reader.read_relationship_milestones',
                   side_effect=AssertionError('milestone read while switch is off')):
            text = shared_relation_prompt.build_relation_rules(
                2, 3, 4, user_id='u', character_id='c', user_message='今天天气怎么样',
            )
        expected = '\n\n'.join([
            shared_relation_prompt._build_meet_line(2, 3, 4),
            'LEDGER',
            'COG',
            rules,
        ])
        self.assertEqual(text, expected)
        self.assertIn('除非 Cognitive romantic_label=affirmed', text)

    def test_switch_off_reader_does_not_open_a_connection(self):
        with patch('config.RELATIONSHIP_MILESTONE_ENABLED', False), \
             patch('db.get_conn', side_effect=AssertionError('db')):
            result = cognitive_reader.read_relationship_milestones('u', 'c')
        self.assertEqual(result, {
            'mutual_feelings': None,
            'romantic_label': None,
            'pending': [],
            'as_of': {},
        })

    def test_three_states_change_ban_groups_and_the_standing_line(self):
        unspoken = {'mutual_feelings': None, 'romantic_label': None, 'pending': [], 'as_of': {}}
        mutual = {
            'mutual_feelings': 'mutual',
            'romantic_label': None,
            'pending': [],
            'as_of': {'mutual_feelings': '2026-11-24'},
        }
        affirmed = {
            'mutual_feelings': 'mutual',
            'romantic_label': 'affirmed',
            'pending': [{
                'type': 'romantic_proposal',
                'speaker': 'user',
                'date': '2026-11-01',
            }],
            'as_of': {'romantic_label': '2026-11-24', 'mutual_feelings': '2026-11-20'},
        }
        groups = {}
        for name, milestones in (('unspoken', unspoken), ('mutual', mutual), ('affirmed', affirmed)):
            text = shared_relation_prompt.expression_rules_for_milestones(milestones)
            groups[name] = text
            self.assertIn('【★ 句式（比字面词更狡猾）——同样禁用】', text)
            self.assertIn('这句话是否暗示', text)
            self.assertNotIn('除非 Cognitive romantic_label=affirmed', text)
            self.assertNotIn('romantic_label 未 affirmed', text)
        self.assertIn('▹ 心动：', groups['unspoken'])
        self.assertIn('▹ 独占与承诺：', groups['unspoken'])
        self.assertIn('▹ 亲密描写：', groups['unspoken'])
        self.assertNotIn('▹ 心动：', groups['mutual'])
        self.assertIn('▹ 独占与承诺：', groups['mutual'])
        self.assertIn('▹ 亲密描写：', groups['mutual'])
        self.assertNotIn('▹ 心动：', groups['affirmed'])
        self.assertNotIn('▹ 独占与承诺：', groups['affirmed'])
        self.assertNotIn('▹ 亲密描写：', groups['affirmed'])
        self.assertIn('动作仍按通道规则。', groups['affirmed'])
        self.assertEqual(shared_relation_prompt.milestone_status_line(unspoken), '')
        self.assertEqual(
            shared_relation_prompt.milestone_status_line(mutual),
            '你们互相说过喜欢（2026-11-24），还没有确定关系。',
        )
        affirmed_line = shared_relation_prompt.milestone_status_line(affirmed)
        self.assertIn('你们已经确定关系（2026-11-24）。', affirmed_line)
        self.assertIn('她说过想和你在一起，还没有明确回应。', affirmed_line)
        self.assertNotIn('你们互相说过喜欢', affirmed_line)

        def cognitive(user_id, character_id, query=''):
            self.assertEqual(query, '今天天气怎么样')
            return ''

        with patch('config.RELATIONSHIP_MILESTONE_ENABLED', True), \
             patch('relationship_reader.build_state_summary', return_value='LEDGER'), \
             patch('cognitive_reader.build_cognitive_prompt_context', side_effect=cognitive), \
             patch('cognitive_reader.read_relationship_milestones', return_value=mutual):
            rendered = shared_relation_prompt.build_relation_rules(
                2, 3, 4, user_id='u', character_id='c', user_message='今天天气怎么样',
            )
        self.assertIn('你们互相说过喜欢（2026-11-24），还没有确定关系。', rendered)
        self.assertNotIn('▹ 心动：', rendered)
        self.assertIn('▹ 独占与承诺：', rendered)
        self.assertIn('LEDGER', rendered)


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'set COGNITIVE_TEST_PGLITE to the local test-only package')
class RelationshipMilestoneTests(ExplicitAnswerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch('config.RELATIONSHIP_MILESTONE_ENABLED', True))
        self.sql('''CREATE TABLE IF NOT EXISTS chat_log_tombstone (
            user_id TEXT NOT NULL, chat_id TEXT NOT NULL, client_msg_id TEXT NOT NULL,
            deleted_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, chat_id, client_msg_id))''')
        self.database.commit()

    def say(self, sid, text, role='user', reply=None):
        self.turn(sid, text, role, reply=reply, process=False)
        result = cognitive_events.ingest_canonical_turn(
            user_id='u', character_id='c', source_event_id=sid, allow_assistant=True)
        self.assertIn(result['status'], ('inserted', 'duplicate'), result)
        for _ in range(8):
            pending = self.sql(
                "SELECT count(*) FROM cognitive_event_triggers WHERE status IN ('pending','claimed')")[0][0]
            if not pending:
                return
            self.run_cycle()
        self.fail('triggers still pending for ' + sid)

    def fixed(self, key):
        rows = self.sql(
            'SELECT status, metadata FROM cognitive_questions WHERE question_key=%s', (key,))
        if not rows:
            return None
        metadata = rows[0][1]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return {'status': rows[0][0], 'metadata': metadata}

    def value_of(self, key):
        row = self.fixed(key)
        if not row:
            return None
        judgment = (row['metadata'].get('resolution') or row['metadata'].get('current_judgment') or {})
        return judgment.get('value')

    def active_predicates(self):
        rows = self.sql(
            """SELECT metadata->'proposition'->>'predicate', status
               FROM cognitive_questions WHERE status='active'""")
        return rows

    def milestones(self):
        return cognitive_reader.read_relationship_milestones('u', 'c', conn=self.database)

    def semantic(self):
        return cognitive_reader.read_relationship_semantic_state('u', 'c', conn=self.database)

    def test_switch_off_writes_no_new_key(self):
        with patch('config.RELATIONSHIP_MILESTONE_ENABLED', False):
            self.say('u1', '我喜欢你')
            self.say('a1', '好きだ', 'assistant', reply='u1')
            self.assertIsNone(self.fixed('relationship.mutual_feelings'))
            self.assertIsNone(self.fixed('relationship.romantic_label'))
            self.assertFalse(any(
                item[0] in ('mutual_feelings', 'romantic_proposal')
                for item in self.active_predicates()))

    def test_nickname_still_resolves_when_the_switch_is_off(self):
        with patch('config.RELATIONSHIP_MILESTONE_ENABLED', False):
            self.exchange()
            self.assert_answer()
            self.assertIsNone(self.fixed('relationship.mutual_feelings'))
            self.assertIsNone(self.fixed('relationship.romantic_label'))

    def test_nickname_still_resolves_when_the_switch_is_on(self):
        self.exchange(answer='可以')
        self.assert_answer(value='yes')
        self.assertIsNone(self.fixed('relationship.mutual_feelings'))
        self.assertIsNone(self.fixed('relationship.romantic_label'))

    def test_user_affection_then_character_reply_is_mutual(self):
        self.say('u1', '我喜欢你')
        self.say('a1', '俺も好きだ', 'assistant', reply='u1')
        self.assertEqual(self.value_of('relationship.mutual_feelings'), 'mutual')
        state = self.semantic()
        self.assertEqual(state['mutual_feelings']['value'], 'mutual')
        self.assertIsNone(state['romantic_label'])
        self.assertEqual(self.milestones()['mutual_feelings'], 'mutual')

    def test_character_affection_then_user_reply_is_mutual(self):
        self.say('a1', '好きだよ', 'assistant')
        self.say('u1', '我也喜欢你', reply='a1')
        self.assertEqual(self.value_of('relationship.mutual_feelings'), 'mutual')
        self.assertIsNone(self.fixed('relationship.romantic_label'))

    def test_bare_agreement_does_not_make_affection_mutual(self):
        for answer, sid in (('好', 'u-hao'), ('嗯', 'u-en'), ('哈哈', 'u-ha')):
            with self.subTest(answer=answer):
                self.say('a-' + sid, '好きだよ', 'assistant')
                self.say(sid, answer, reply='a-' + sid)
                self.assertIsNone(self.value_of('relationship.mutual_feelings'))
        pending = self.milestones()['pending']
        self.assertTrue(pending)
        self.assertTrue(all(item['type'] == 'mutual_feelings' for item in pending))
        self.assertTrue(all(item['date'] for item in pending))

    def test_proposal_acceptance_affirms_and_sets_mutual(self):
        self.say('a1', '付き合おう', 'assistant')
        self.say('u1', '好啊', reply='a1')
        self.assertEqual(self.value_of('relationship.romantic_label'), 'affirmed')
        self.assertEqual(self.value_of('relationship.mutual_feelings'), 'mutual')
        romantic = self.fixed('relationship.romantic_label')['metadata']
        self.assertEqual(romantic.get('updated_by'), 'explicit_answer_v1')
        self.assertEqual(len(romantic.get('evidence_event_ids') or []), 2)

    def test_proposal_rejection_declines_without_changing_mutual(self):
        self.say('a1', '付き合おう', 'assistant')
        self.say('u1', '不要', reply='a1')
        self.assertEqual(self.value_of('relationship.romantic_label'), 'declined')
        self.assertIsNone(self.fixed('relationship.mutual_feelings'))

    def test_later_acceptance_supersedes_declined_and_keeps_history(self):
        self.say('a1', '付き合おう', 'assistant')
        self.say('u1', '不要', reply='a1')
        self.say('a2', '付き合おう', 'assistant')
        self.say('u2', '好', reply='a2')
        row = self.fixed('relationship.romantic_label')
        self.assertEqual(row['status'], 'resolved')
        self.assertEqual(self.value_of('relationship.romantic_label'), 'affirmed')
        history = row['metadata'].get('lifecycle_history') or []
        self.assertTrue(any(
            (item.get('current_judgment') or {}).get('value') == 'declined' for item in history
        ), history)
        statuses = self.sql(
            """SELECT op.value->>'status' FROM cognitive_events
               CROSS JOIN LATERAL jsonb_each(COALESCE(adjudication->'operations','{}')) op
               WHERE op.key='milestone.romantic_label' ORDER BY cognitive_events.id""")
        self.assertEqual([item[0] for item in statuses], ['superseded', 'applied'])

    def test_character_acceptance_of_a_user_proposal_stays_active(self):
        self.say('u1', '我们在一起吧')
        self.say('a1', 'いいよ', 'assistant', reply='u1')
        self.assertIsNone(self.fixed('relationship.romantic_label'))
        self.assertIn(('romantic_proposal', 'active'), self.active_predicates())

    def test_character_claiming_agreement_does_not_count(self):
        self.say('u1', '我喜欢你')
        self.say('a1', '你也答应了', 'assistant', reply='u1')
        self.assertIsNone(self.value_of('relationship.mutual_feelings'))
        self.assertIn(('mutual_feelings', 'active'), self.active_predicates())

    def test_breakup_language_does_not_retract(self):
        self.say('a1', '付き合おう', 'assistant')
        self.say('u1', '好啊', reply='a1')
        before = self.sql(
            'SELECT status, metadata::text FROM cognitive_questions WHERE question_key=%s ORDER BY question_key',
            ('relationship.romantic_label',))
        self.say('u2', '分手')
        self.say('u3', '我不喜欢你了')
        self.say('a2', '別れよう', 'assistant')
        after = self.sql(
            'SELECT status, metadata::text FROM cognitive_questions WHERE question_key=%s ORDER BY question_key',
            ('relationship.romantic_label',))
        self.assertEqual(before, after)
        self.assertEqual(self.value_of('relationship.mutual_feelings'), 'mutual')

    def test_unrecognized_forms_and_group_chat_write_nothing(self):
        samples = (
            ('q1', '「我喜欢你」', 'user'),
            ('q2', '如果我喜欢你', 'user'),
            ('q3', '我不喜欢你', 'user'),
            ('q4', '我喜欢她', 'user'),
            ('q5', '你喜欢我吗', 'user'),
            ('q6', 'ラーメンが好き', 'assistant'),
        )
        for sid, text, role in samples:
            self.say(sid, text, role)
        self.assertIsNone(self.fixed('relationship.mutual_feelings'))
        self.assertIsNone(self.fixed('relationship.romantic_label'))
        self.assertFalse(any(
            item[0] in ('mutual_feelings', 'romantic_proposal')
            for item in self.active_predicates()))
        event_id = 'group:g1:message:1'
        extra = json.dumps({
            'canonical_group': True,
            'target_character_id': 'c',
            'audience': ['c'],
        })
        self.now_step(event_id, '我喜欢你', extra)
        result = cognitive_events.ingest_canonical_turn(
            user_id='u', character_id='c', source_event_id=event_id,
            source_chat_id='group:g1', allow_assistant=True)
        self.assertIn(result['status'], ('inserted', 'duplicate'), result)
        for _ in range(8):
            pending = self.sql(
                "SELECT count(*) FROM cognitive_event_triggers WHERE status IN ('pending','claimed')")[0][0]
            if not pending:
                break
            self.run_cycle()
        self.assertIsNone(self.fixed('relationship.mutual_feelings'))
        self.assertFalse(any(item[0] in ('mutual_feelings', 'romantic_proposal')
                             for item in self.active_predicates()))

    def now_step(self, event_id, text, extra):
        from datetime import timedelta
        self.now += timedelta(minutes=1)
        self.sql(
            '''INSERT INTO chat_log(user_id,chat_id,event_id,role,text,created_at,extra)
               VALUES (%s,%s,%s,%s,%s,%s,%s)''',
            ('u', 'group:g1', event_id, 'user', text, self.now, extra))
        self.database.commit()

    def test_inactive_source_drops_the_label_and_restores_unspoken_rules(self):
        self.say('a1', '付き合おう', 'assistant')
        self.say('u1', '好啊', reply='a1')
        self.assertEqual(self.semantic()['romantic_label']['value'], 'affirmed')
        self.sql("UPDATE chat_log SET status='inactive' WHERE event_id='u1'")
        self.database.commit()
        state = self.semantic()
        self.assertIsNone(state['romantic_label'])
        self.assertIsNone(state['mutual_feelings'])
        milestones = self.milestones()
        self.assertIsNone(milestones['romantic_label'])
        self.assertIsNone(milestones['mutual_feelings'])
        rules = shared_relation_prompt.expression_rules_for_milestones(milestones)
        line = shared_relation_prompt.milestone_status_line(milestones)
        self.assertEqual(line, '')
        self.assertIn('▹ 心动：', rules)
        self.assertIn('▹ 独占与承诺：', rules)
        self.assertIn('▹ 亲密描写：', rules)

    def test_decline_does_not_block_a_later_mutual_feeling(self):
        self.say('a1', '付き合おう', 'assistant')
        self.say('u1', '不要', reply='a1')
        self.say('a2', '好きだ', 'assistant')
        self.say('u2', '我也喜欢你', reply='a2')
        self.assertEqual(self.value_of('relationship.romantic_label'), 'declined')
        self.assertEqual(self.value_of('relationship.mutual_feelings'), 'mutual')
        state = self.semantic()
        self.assertEqual(state['romantic_label']['value'], 'declined')
        self.assertEqual(state['mutual_feelings']['value'], 'mutual')

    def test_reader_drops_mutual_when_the_source_is_inactive(self):
        self.say('u1', '我喜欢你')
        self.say('a1', '俺も好きだ', 'assistant', reply='u1')
        self.assertEqual(self.semantic()['mutual_feelings']['value'], 'mutual')
        self.sql("UPDATE chat_log SET status='inactive' WHERE event_id='u1'")
        self.database.commit()
        state = self.semantic()
        self.assertIsNone(state['mutual_feelings'])
        self.assertIsNone(state['romantic_label'])
