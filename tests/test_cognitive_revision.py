import inspect
import json
import os
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

sys.modules.setdefault('requests', types.ModuleType('requests'))
sys.modules.setdefault(
    'anthropic',
    types.SimpleNamespace(Anthropic=Mock()),
)

import cognitive_output
import cognitive_reader
import cognitive_revision
import cognitive_worker
import relationship_engine
import relationship_flirt
import relationship_initiative
import relationship_reader
from tests.test_cognitive_slow_loop import committable_output, valid_output


NOW = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc)


def _refs():
    return [{'event_id': 12}, {'event_id': 27}]


def _event_meta():
    return {
        12: {
            'source_event_type': 'relationship_v4_signal',
            'source_event_id': 'a',
            'payload': {},
        },
        27: {
            'source_event_type': 'relationship_v4_signal',
            'source_event_id': 'b',
            'payload': {},
        },
    }


def _update(**overrides):
    item = committable_output()['belief_updates'][0].copy()
    item.update(overrides)
    return item


def _existing(**overrides):
    row = {
        'statement': '用户只有 Gojo 一个长期目标。',
        'confidence': 0.90,
        'status': 'active',
        'metadata': {'scope': 'legacy'},
    }
    row.update(overrides)
    return row


def _state(**overrides):
    state = {
        'warmth': 70.0,
        'intimacy': 70.0,
        'trust': 70.0,
        'attachment': 70.0,
        'commitment': 70.0,
        'passion': 20.0,
        'friction': {},
        'pending_passion': 0,
        'banter_baseline': 'playful',
    }
    state.update(overrides)
    return state


class EventVsBeliefTests(unittest.TestCase):
    def test_single_episode_cannot_become_stable_belief(self):
        update = _update(statement='用户今晚又喝酒了。', confidence=0.88)
        decision = cognitive_output._belief_commit_decision(
            update, _refs(), _event_meta(), hypothesis_id=81,
        )
        self.assertEqual(decision['action'], 'held')
        self.assertEqual(decision['reason'], 'episodic_not_belief')

    def test_quoted_utterance_is_not_a_belief(self):
        update = _update(statement='用户说「我好痛苦」。', confidence=0.88)
        decision = cognitive_output._belief_commit_decision(
            update, _refs(), _event_meta(), hypothesis_id=81,
        )
        self.assertEqual(decision['action'], 'held')
        self.assertEqual(decision['reason'], 'episodic_not_belief')


class BeliefRevisionTests(unittest.TestCase):
    def test_support_only_increases_confidence_gradually(self):
        update = _update(
            statement='用户只有 Gojo 一个长期目标。',
            confidence=0.95,
            evidence_relation='support',
            evidence_strength='strong',
        )
        decision = cognitive_output._belief_commit_decision(
            update, _refs(), _event_meta(), 81,
            existing=_existing(confidence=0.50),
        )
        self.assertEqual(decision['action'], 'committed')
        self.assertAlmostEqual(decision['confidence'], 0.60, places=4)
        self.assertEqual(decision['statement'], '用户只有 Gojo 一个长期目标。')

    def test_contradiction_lowers_confidence_and_can_reopen(self):
        update = _update(
            statement='用户正在形成独立于 Gojo 的专业与个人目标。',
            confidence=0.95,
            evidence_relation='contradiction',
            evidence_strength='strong',
            revision_reason='出现独立目标证据',
        )
        decision = cognitive_output._belief_commit_decision(
            update, _refs(), _event_meta(), 81,
            existing=_existing(confidence=0.90),
        )
        self.assertEqual(decision['action'], 'under_review')
        self.assertAlmostEqual(decision['confidence'], 0.72, places=4)
        self.assertEqual(decision['review_status'], 'under_review')
        self.assertIn('独立', decision['statement'])

    def test_scope_limiter_narrows_statement(self):
        update = _update(
            statement='在讨论如何追求 Gojo 时，用户经常向角色寻求明确行动建议。',
            confidence=0.80,
            evidence_relation='scope_limiter',
            evidence_strength='normal',
            scope='topic',
            independent_contexts=['追Gojo'],
        )
        decision = cognitive_output._belief_commit_decision(
            update, _refs(), _event_meta(), 81,
            existing=_existing(
                statement='用户依赖外部指导，不会自主决策。',
                confidence=0.86,
            ),
        )
        self.assertEqual(decision['action'], 'committed')
        self.assertLess(decision['confidence'], 0.86)
        self.assertIn('追求 Gojo', decision['statement'])
        self.assertNotIn('不会自主决策', decision['statement'])

    def test_single_topic_cannot_become_global_personality_judgment(self):
        update = _update(
            statement='用户缺乏独立思考能力。',
            confidence=0.88,
            scope='general_tendency',
            independent_contexts=['追Gojo'],
        )
        decision = cognitive_output._belief_commit_decision(
            update, _refs(), _event_meta(), 81,
        )
        self.assertEqual(decision['action'], 'held')
        self.assertEqual(decision['reason'], 'scope_too_wide')

    def test_sticky_note_cannot_replace_contradiction_handling(self):
        output = valid_output()
        output['sticky_note_updates'] = [{
            'note_key': 'revision.wrong',
            'content': '用户好像开始有独立目标了，旧判断可能不对。',
            'status': 'active',
            'evidence_refs': [27],
        }]
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError,
            'sticky_note_cannot_replace_revision',
        ):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )

    def test_revision_history_keeps_old_and_new_belief(self):
        meta = cognitive_revision.append_revision_history(
            {'scope': 'legacy'},
            old_statement='用户只有 Gojo 一个长期目标。',
            old_confidence=0.90,
            new_statement='用户正在形成独立目标。',
            new_confidence=0.72,
            relation='contradiction',
            reason='独立目标出现',
            evidence_refs=_refs(),
            cycle_id=9,
        )
        history = meta['revision_history']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['old_statement'], '用户只有 Gojo 一个长期目标。')
        self.assertTrue(history[0]['superseded'])
        self.assertEqual(history[0]['cycle_id'], 9)

    def test_no_new_evidence_cannot_self_increase_confidence(self):
        update = _update(
            confidence=0.95,
            evidence_relation='irrelevant',
        )
        decision = cognitive_output._belief_commit_decision(
            update, _refs(), _event_meta(), 81,
            existing=_existing(confidence=0.80),
        )
        self.assertEqual(decision['action'], 'held')
        self.assertEqual(decision['confidence'], 0.80)

        update2 = _update(confidence=0.95)
        decision2 = cognitive_output._belief_commit_decision(
            update2, _refs(), _event_meta(), 81,
            existing=_existing(confidence=0.80),
        )
        self.assertEqual(decision2['reason'], 'missing_evidence_relation')
        self.assertEqual(decision2['confidence'], 0.80)
        self.assertEqual(
            cognitive_revision.confidence_delta('irrelevant', 'strong'), 0.0,
        )


class CognitiveDoesNotWriteRelationshipStateTests(unittest.TestCase):
    def test_persist_and_revision_helpers_do_not_touch_rel_state(self):
        persist_src = inspect.getsource(cognitive_output.persist_slow_loop_output)
        worker_src = inspect.getsource(cognitive_worker)
        rev_src = inspect.getsource(cognitive_revision)
        for source in (persist_src, worker_src, rev_src):
            self.assertNotIn('UPDATE rel_state', source)
            self.assertNotIn('INSERT INTO rel_state', source)
            self.assertNotIn('apply_passion', source)
            self.assertNotIn('apply_warmth', source)


class PersistRevisionCursor:
    def __init__(self, existing):
        self.existing = existing
        self.executed = []
        self.one = None
        self.many = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        self.one = None
        self.many = []
        if compact.startswith('SELECT belief_key'):
            self.many = [self.existing]
        elif compact.startswith('SELECT id, source_event_type'):
            self.many = [
                (12, 'relationship_v4_signal', 'a', 'observer', {}),
                (27, 'relationship_v4_signal', 'b', 'observer', {}),
            ]
        elif compact.startswith('INSERT INTO cognitive_questions'):
            self.one = (71,)
        elif compact.startswith('INSERT INTO cognitive_hypotheses'):
            self.one = (81,)
        elif compact.startswith('SELECT id FROM cognitive_hypotheses'):
            self.one = (81,)
        elif compact.startswith('SELECT id FROM cognitive_questions'):
            self.one = (71,)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class PersistRevisionTests(unittest.TestCase):
    def test_persist_writes_revision_history_without_passion_jump(self):
        output = committable_output()
        output['belief_updates'][0].update({
            'statement': '用户正在形成独立于 Gojo 的目标。',
            'confidence': 0.95,
            'evidence_relation': 'contradiction',
            'evidence_strength': 'strong',
            'revision_reason': '出现独立目标',
            'scope': 'cross_context',
            'independent_contexts': ['工作', '个人生活'],
        })
        normalized = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={12, 27},
        )
        cursor = PersistRevisionCursor((
            'character.boundary_care_pattern',
            '用户只有 Gojo 一个长期目标。',
            0.90, 'active', json.dumps({'scope': 'legacy'}),
        ))
        decisions = cognitive_output.persist_slow_loop_output(
            cursor, cycle_id=9, user_id='u', character_id='gojo',
            output=normalized, now=NOW,
        )
        self.assertEqual(decisions[0]['action'], 'under_review')
        belief_insert = next(
            params for sql, params in cursor.executed
            if sql.startswith('INSERT INTO cognitive_beliefs')
        )
        blob = json.dumps(belief_insert, ensure_ascii=False, default=str)
        self.assertIn('revision_history', blob)
        self.assertIn('用户只有 Gojo 一个长期目标。', blob)
        self.assertIn('under_review', blob)
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertNotIn('rel_state', sql)


class SharedFrameAndFlirtTests(unittest.TestCase):
    def test_friendship_frame_plus_habitual_playful_does_not_add_pending_passion(self):
        effect = relationship_flirt.interpret_flirt_effect(
            'habitual_flirt', reciprocal=True,
            frame_kind='friends', frame_confidence=0.92,
        )
        self.assertEqual(effect['pending_passion_delta'], 0)
        self.assertFalse(effect['romantic_evidence'])

    def test_this_time_is_not_a_joke_is_frame_break_candidate(self):
        self.assertTrue(
            relationship_flirt.utterance_suggests_frame_break('这次不是开玩笑')
        )
        effect = relationship_flirt.interpret_flirt_effect(
            'playful_flirt', reciprocal=True,
            frame_kind='friends', frame_confidence=0.95,
            frame_break=True, meta_serious=True,
        )
        self.assertTrue(effect['frame_break_candidate'])
        self.assertEqual(effect['interpretation'], 'romantic_probe')

    def test_reciprocal_playful_raises_intimacy_not_romance(self):
        effect = relationship_flirt.interpret_flirt_effect(
            'playful_flirt', reciprocal=True,
            frame_kind='friends', frame_confidence=0.9,
        )
        self.assertGreater(effect['warmth_delta'], 0)
        self.assertGreater(effect['intimacy_delta'], 0)
        self.assertEqual(effect['pending_passion_delta'], 0)

    def test_reciprocal_romantic_probe_can_form_pending_evidence(self):
        effect = relationship_flirt.interpret_flirt_effect(
            'romantic_probe', reciprocal=True,
            frame_kind='friends', frame_confidence=0.9,
        )
        self.assertEqual(effect['pending_passion_delta'], 1)
        self.assertTrue(effect['romantic_evidence'])

    def test_ambiguous_flirt_stays_unresolved(self):
        effect = relationship_flirt.interpret_flirt_effect(
            'ambiguous_flirt', reciprocal=True,
        )
        self.assertTrue(effect['unresolved'])
        self.assertEqual(effect['pending_passion_delta'], 0)
        self.assertFalse(effect['romantic_evidence'])

    def test_habitual_with_others_is_not_strong_romantic_evidence(self):
        effect = relationship_flirt.interpret_flirt_effect(
            'romantic_probe', reciprocal=True,
            habitual_with_others=True, exclusive_to_character=False,
        )
        self.assertEqual(effect['interpretation'], 'habitual_flirt')
        self.assertEqual(effect['pending_passion_delta'], 0)

    def test_exclusive_long_term_flirt_can_weight_romantic_hypothesis(self):
        effect = relationship_flirt.interpret_flirt_effect(
            'romantic_probe', reciprocal=True,
            exclusive_to_character=True,
        )
        self.assertEqual(effect['pending_passion_delta'], 1)
        self.assertTrue(effect['romantic_evidence'])

    def test_friendship_frame_can_be_challenged_by_romantic_evidence(self):
        locked = relationship_flirt.interpret_flirt_effect(
            'playful_flirt', reciprocal=True,
            frame_kind='friends', frame_confidence=0.92,
        )
        challenged = relationship_flirt.interpret_flirt_effect(
            'romantic_admission', reciprocal=True,
            frame_kind='friends', frame_confidence=0.55,
        )
        self.assertEqual(locked['pending_passion_delta'], 0)
        self.assertEqual(challenged['pending_passion_delta'], 1)
        decision = cognitive_output._belief_commit_decision(
            _update(
                belief_key='shared.relationship.frame',
                statement='双方默认仍是朋友，但认真浪漫表态正在挑战该框架。',
                evidence_relation='contradiction',
                evidence_strength='normal',
                frame_kind='frame_shifting',
            ),
            _refs(), _event_meta(), 81,
            existing=_existing(
                statement='我们只做朋友，暧昧玩笑不当真。',
                confidence=0.88,
                metadata={'frame_kind': 'friends', 'scope': 'relationship_context'},
            ),
        )
        self.assertLess(decision['confidence'], 0.88)
        self.assertIn(decision['action'], {'committed', 'under_review'})

    def test_friendship_frame_is_not_a_permanent_romance_lock(self):
        effect = relationship_flirt.interpret_flirt_effect(
            'romantic_probe', reciprocal=True,
            frame_kind='friends', frame_confidence=0.99,
            frame_break=True,
        )
        self.assertEqual(effect['pending_passion_delta'], 1)
        self.assertTrue(effect['frame_break_candidate'])


class CharacterInitiativeTests(unittest.TestCase):
    def test_character_may_flirt_before_love_label(self):
        text = relationship_initiative.initiative_guidance(
            'gojo', _state(passion=20), is_love=False,
        )
        self.assertIn('主动试探', text)
        self.assertIn('不代表账本已经升级', text)

    def test_initiative_does_not_write_relationship_state(self):
        source = inspect.getsource(relationship_initiative)
        self.assertNotIn('apply_passion', source)
        self.assertNotIn('UPDATE rel_state', source)
        self.assertNotIn('pending_passion', source)

    def test_user_response_still_enters_v4_via_process_turn_signals(self):
        source = inspect.getsource(relationship_engine.process_turn)
        self.assertIn('extract_signals', source)
        self.assertIn('_route_signal', source)

    def test_different_policies_allow_different_initiative(self):
        state = _state(passion=20)
        gojo = relationship_initiative.initiative_guidance('gojo', state)
        minato = relationship_initiative.initiative_guidance('minato', state)
        geto = relationship_initiative.initiative_guidance('geto', state)
        self.assertIn('主动试探', gojo)
        self.assertIn('拉开距离', minato)
        self.assertIn('继续观察', geto)
        self.assertNotEqual(gojo, minato)


class SalientEventTests(unittest.TestCase):
    def test_high_salience_can_open_reappraisal_without_passion_jump(self):
        allowed = relationship_flirt.salient_event_allows_reappraisal(
            _state(trust=60, intimacy=55, attachment=55, passion=8),
            salience='high',
        )
        self.assertTrue(allowed)
        source = inspect.getsource(relationship_engine._maybe_mark_romantic_reappraisal)
        self.assertIn('romantic_reappraisal', source)
        self.assertNotIn('apply_passion', source)
        self.assertNotIn('update_pending_passion', source)

    def test_salient_event_does_not_directly_add_large_passion(self):
        effect = relationship_flirt.interpret_flirt_effect(
            'playful_flirt', reciprocal=False,
        )
        self.assertEqual(effect['pending_passion_delta'], 0)
        self.assertNotIn('passion_delta', effect)


class ReaderStructureTests(unittest.TestCase):
    def test_high_attachment_commitment_with_mid_passion_is_not_love_or_colorless(self):
        label = relationship_reader.derive_label(_state(
            attachment=70, commitment=70, passion=20, warmth=70, intimacy=70,
        ))
        self.assertEqual(label['primary'], '深厚的挚友 / 亲情')
        self.assertNotEqual(label['primary'], '爱情')
        self.assertNotIn('不带心动色彩', label['expression_guidance'])
        self.assertIn('尚未确认的心动', label['expression_guidance'])
        self.assertIn('不代表底层关系状态已经升级', label['expression_guidance'])

    def test_reader_partitions_cognition_types(self):
        class Cursor:
            def __init__(self):
                self.sql = ''

            def execute(self, sql, params=None):
                self.sql = ' '.join(sql.split())

            def fetchone(self):
                if self.sql.startswith('SELECT cycle_summary'):
                    return ({}, {}, NOW)
                return None

            def fetchall(self):
                if self.sql.startswith('SELECT question_key'):
                    return [('user.independent.goals.q',
                             '用户是否正在形成独立于 Gojo 的个人目标？',
                             'active', NOW)]
                if self.sql.startswith('SELECT belief_key'):
                    return [
                        ('user.goals', '用户正在形成独立目标。', 0.82,
                         'user_model', NOW, {'review_status': 'stable'}),
                        ('character.practical_care', '角色会在生活上接住用户。',
                         0.80, 'relationship_observation', NOW, {}),
                        ('interaction.advice', '在追 Gojo 话题里用户常求建议。',
                         0.79, 'interaction_pattern', NOW, {}),
                        ('self.jealousy', '我可能已经不只把互叫老婆当玩笑。',
                         0.70, 'self_model', NOW, {}),
                        ('shared.relationship.frame',
                         '当前双方主要以朋友互动，暧昧玩笑通常被视为 playful。',
                         0.84, 'relationship_observation', NOW,
                         {'frame_kind': 'friends', 'review_status': 'stable'}),
                    ]
                if self.sql.startswith('SELECT hypothesis_key'):
                    return [('user.independent.goals.h',
                             '用户可能正在建立自己的目标体系。',
                             'open', 'user_model', 0.58, NOW)]
                if self.sql.startswith('SELECT id, note_key'):
                    return [(1, 'follow.project',
                             '用户在做一个能模拟拥抱的东西，后续可以关注进展。',
                             'active', NOW, NOW)]
                return []

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cursor()

            def close(self):
                pass

        text = cognitive_reader.build_cognitive_prompt_context(
            'u', 'gojo', conn=Conn(),
        )
        self.assertIn('【关于用户的稳定认识】', text)
        self.assertIn('【关于我与用户关系的认识】', text)
        self.assertIn('【反复出现的互动模式】', text)
        self.assertIn('【关于自己的暂时认识】', text)
        self.assertIn('【当前共享的关系框架】', text)
        self.assertIn('【当前仍未解决的问题】', text)
        self.assertIn('【正在观察的理解】', text)
        self.assertIn('【近期需要留意的事】', text)
        self.assertNotIn('较稳定的历史观察', text)

    def test_under_review_belief_is_shown_as_reassessment_not_two_facts(self):
        belief = {
            'statement': '用户正在形成独立目标。',
            'confidence': 0.72,
            'status': 'active',
            'metadata': {
                'review_status': 'under_review',
                'revision_history': [{
                    'old_statement': '用户只有 Gojo 一个长期目标。',
                    'new_statement': '用户正在形成独立目标。',
                }],
            },
        }
        display = cognitive_revision.current_belief_display(belief)
        self.assertIn('正在被重新评估', display)
        self.assertIn('只有 Gojo 一个长期目标', display)
        self.assertFalse(cognitive_revision.is_stable_reader_belief(belief))

        class Cursor:
            def execute(self, sql, params=None):
                self.sql = ' '.join(sql.split())

            def fetchone(self):
                if self.sql.startswith('SELECT cycle_summary'):
                    return ({}, {}, NOW)
                return None

            def fetchall(self):
                if self.sql.startswith('SELECT belief_key'):
                    return [(
                        'user.goals', belief['statement'], 0.72,
                        'user_model', NOW, belief['metadata'],
                    )]
                return []

            def close(self):
                pass

        class Conn:
            def cursor(self):
                return Cursor()

            def close(self):
                pass

        text = cognitive_reader.build_cognitive_prompt_context(
            'u', 'gojo', conn=Conn(),
        )
        self.assertIn('正在被重新评估', text)
        self.assertNotIn('（置信度 0.72，可被新证据修正）', text)


class HypothesisQuestionChainTests(unittest.TestCase):
    def test_hypothesis_still_requires_question_key(self):
        output = valid_output()
        del output['hypothesis_updates'][0]['question_key']
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )


if __name__ == '__main__':
    unittest.main()
