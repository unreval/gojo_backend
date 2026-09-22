import inspect
import json
import os
import sys
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import cognitive_db
import cognitive_config
import cognitive_inspect
import cognitive_output
import cognitive_queue
import cognitive_worker


NOW = datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc)


def valid_output():
    return {
        'cycle_summary': {
            'summary': '用户明确表达长期在意，角色承认关心但尚未对等确认。',
            'salient_change': '角色首次明确承认会想到用户。',
            'uncertainty': '这份关心是否会发展为双向爱情仍未知。',
            'confidence': 'high',
        },
        'question_updates': [{
            'question_key': 'character.care.boundary.question',
            'question_text': '角色的关心是否仍停留在保持边界的照看？',
            'status': 'active',
            'evidence_refs': [27],
        }],
        'belief_updates': [],
        'hypothesis_updates': [{
            'hypothesis_key': 'character.care_without_commitment',
            'statement': '角色存在关心，但尚未形成对等承诺。',
            'hypothesis_type': 'relationship',
            'confidence': 0.62,
            'status': 'supported',
            'question_key': 'character.care.boundary.question',
            'supporting_evidence_refs': [27],
            'contradicting_evidence_refs': [],
        }],
        'new_predictions': [{
            'prediction_key': 'care_signal.repeats.v1',
            'resolver_name': 'current_event_signal_outcome',
            'fulfillment_operator': '>=',
            'fulfillment_value': 1,
            'violation_operator': '<=',
            'violation_value': -1,
            'expires_in_seconds': 86400,
            'question_key': 'character.care.boundary.question',
            'hypothesis_key': 'character.care_without_commitment',
            'metadata': {
                'description': '等待角色后续是否仍明确保持边界。',
                'fulfillment_signals': [{
                    'signal_type': 'character_stance_declared',
                    'actor': 'character',
                    'attributes': {'stance_type': 'boundary_stated'},
                }],
                'violation_signals': [{
                    'signal_type': 'character_reciprocal',
                    'actor': 'character',
                }],
            },
            'evidence_refs': [27],
        }],
        'evidence_refs': [{
            'event_id': 27,
            'reason': '该事件同时包含用户表态和角色明确立场。',
        }],
        'reflection_note': {
            'content': '下次若继续谈在一起，保留关心与边界之间的不确定性。',
            'evidence_refs': [27],
        },
    }


def committable_output():
    output = valid_output()
    output['evidence_refs'] = [
        {'event_id': 12, 'reason': '历史事件显示角色多次保持边界。'},
        {'event_id': 27, 'reason': '本轮事件再次显示角色保持边界。'},
    ]
    output['belief_updates'] = [{
        'belief_key': 'character.boundary_care_pattern',
        'statement': '角色常以保持边界的方式继续照看用户。',
        'confidence': 0.84,
        'status': 'active',
        'belief_type': 'interaction_pattern',
        'from_hypothesis_key': 'character.care_without_commitment',
        'evidence_refs': [12, 27],
    }]
    output['hypothesis_updates'][0]['supporting_evidence_refs'] = [12, 27]
    return output


def sticky_update(note_key, content, *, emotion='无奈', tone='',
                  trigger_snippet='她提到了还没讲完的签证。'):
    update = {
        'note_key': note_key,
        'content': content,
        'emotion': emotion,
        'trigger_snippet': trigger_snippet,
        'status': 'active',
        'expires_in_seconds': 3600,
        'evidence_refs': [27],
    }
    if tone:
        update['tone'] = tone
    return update


_TTL_MISSING = object()


def output_with_sticky_ttl(status='active', ttl=_TTL_MISSING, *,
                           content='这事还挂在心上，过会儿再想。'):
    output = valid_output()
    note = {
        'note_key': 'reply.pending.ttl',
        'content': content,
        'emotion': '认真',
        'status': status,
        'evidence_refs': [27],
    }
    if ttl is not _TTL_MISSING:
        note['expires_in_seconds'] = ttl
    output['sticky_note_updates'] = [note]
    return output


class TransactionConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        pass


class StructuredCommitCursor:
    def __init__(self):
        self.one = None
        self.many = []
        self.executed = []
        self.rowcount = 1

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        self.one = None
        self.many = []
        self.rowcount = 1
        if compact.startswith('SELECT user_id, character_id FROM cognitive_cycles'):
            self.one = ('u', 'gojo')
        elif compact.startswith('SELECT status, input_state_version'):
            self.one = ('running', 4)
        elif compact.startswith('SELECT COUNT(*), COUNT(*) FILTER'):
            self.one = (1, 1)
        elif compact.startswith('SELECT DISTINCT trigger.event_id'):
            self.many = [(27,)]
        elif compact.startswith('SELECT id FROM cognitive_events'):
            self.many = [(12,)]
        elif compact.startswith('INSERT INTO cognitive_questions'):
            self.one = (71,)
        elif compact.startswith('INSERT INTO cognitive_hypotheses'):
            self.one = (81,)
        elif compact.startswith('SELECT COUNT(*) FROM cognitive_cycles'):
            self.one = (1,)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class SnapshotCursor:
    def __init__(self):
        self.many = []
        self.executed = []

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self.executed.append((compact, params))
        if 'FROM cognitive_cycles' in compact:
            self.many = [(
                7, 'succeeded', 'high_weight_evidence', 4, 5,
                json.dumps(valid_output()['cycle_summary'], ensure_ascii=False),
                json.dumps(valid_output()['question_updates'], ensure_ascii=False),
                json.dumps(valid_output()['belief_updates'], ensure_ascii=False),
                '[]',
                json.dumps(valid_output()['hypothesis_updates'], ensure_ascii=False),
                json.dumps(valid_output()['new_predictions'], ensure_ascii=False),
                json.dumps(valid_output()['evidence_refs'], ensure_ascii=False),
                json.dumps(valid_output()['reflection_note'], ensure_ascii=False),
                '[]', '[]',
                'test-model', None, NOW, NOW, NOW,
            )]
        elif 'FROM cognitive_questions' in compact:
            self.many = [(
                'character.care.boundary.question',
                '角色的关心是否仍停留在保持边界的照看？',
                'active', '[]', 7, NOW,
            )]
        elif 'FROM cognitive_beliefs' in compact:
            self.many = [(
                'user.long_term_care', '用户持续表达在意。', 0.9, 'active',
                'user_model', '[]', 81, '{}', 7, NOW,
            )]
        elif 'FROM cognitive_hypotheses' in compact:
            self.many = [(
                'character.care_without_commitment', '角色关心但尚未承诺。',
                'supported', 'relationship', 0.62, '[]', '[]', '[]', 7, NOW,
            )]
        elif 'FROM cognitive_predictions' in compact:
            self.many = [(
                'care_signal.repeats.v1', 'pending',
                'current_event_signal_outcome', '>=', 1.0,
                '<=', -1.0, None, NOW, None, 7, '{}',
            )]
        else:
            self.many = []

    def fetchall(self):
        return list(self.many)

    def close(self):
        pass


class SnapshotConnection:
    def __init__(self):
        self._cursor = SnapshotCursor()

    def cursor(self):
        return self._cursor

    def close(self):
        pass


class SlowLoopValidationTests(unittest.TestCase):
    def test_required_output_is_normalized(self):
        result = cognitive_output.validate_slow_loop_output(
            valid_output(), allowed_event_ids={27},
        )
        self.assertEqual(result['cycle_summary']['confidence'], 'high')
        self.assertEqual(result['question_updates'][0]['evidence_refs'], [27])
        self.assertEqual(
            result['hypothesis_updates'][0]['supporting_evidence_refs'], [27],
        )
        self.assertEqual(result['new_predictions'][0]['fulfillment_value'], 1.0)
        self.assertEqual(result['reflection_note']['evidence_refs'], [27])

    def test_json_fence_is_tolerated_but_reasoning_is_not_saved(self):
        raw = 'preface\n```json\n' + json.dumps(valid_output()) + '\n```'
        parsed = cognitive_output.parse_slow_loop_output(raw)
        self.assertEqual(parsed['cycle_summary']['confidence'], 'high')
        self.assertNotIn('preface', json.dumps(parsed))

    def test_event_outside_cycle_is_rejected(self):
        output = valid_output()
        output['evidence_refs'][0]['event_id'] = 999
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError, 'not_in_cycle',
        ):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )

    def test_update_must_reference_declared_evidence(self):
        output = valid_output()
        output['hypothesis_updates'][0]['supporting_evidence_refs'] = [28]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27, 28},
            )

    def test_confidence_update_must_reference_current_evidence(self):
        output = valid_output()
        output['evidence_refs'].insert(0, {
            'event_id': 12,
            'reason': '历史事件不能单独给本轮 confidence 加分。',
        })
        output['hypothesis_updates'][0]['supporting_evidence_refs'] = [12]
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError,
            'must_reference_current_evidence',
        ):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={12, 27}, current_event_ids={27},
            )

    def test_arbitrary_prediction_resolver_is_rejected(self):
        output = valid_output()
        output['new_predictions'][0]['resolver_name'] = 'rel_state.passion'
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError, 'resolver_invalid',
        ):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )

    def test_optional_sticky_notes_and_diary_require_grounding(self):
        output = valid_output()
        output['sticky_note_updates'] = [sticky_update(
            'reply.pending.topic', '签证那件事她没讲完。之后得再问一句。')]
        output['diary_entries'] = [{
            'diary_key': 'reflection.20260913.topic',
            'content': '今天这轮让我意识到，她并不是随口提起那件事。',
            'reflection_kind': 'event',
            'evidence_refs': [27],
        }]

        result = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27},
        )

        self.assertEqual(result['sticky_note_updates'][0]['status'], 'active')
        self.assertEqual(result['sticky_note_updates'][0]['emotion'], '无奈')
        self.assertEqual(
            result['sticky_note_updates'][0]['trigger_snippet'],
            '她提到了还没讲完的签证。',
        )
        self.assertEqual(result['diary_entries'][0]['reflection_kind'], 'event')

    def test_audit_sticky_is_dropped_natural_sticky_kept(self):
        self.assertFalse(cognitive_output.is_user_facing_sticky_content(
            '用户明天要抽徽章，让我帮她选号码'))
        self.assertTrue(cognitive_output.is_user_facing_sticky_content(
            '她明天要抽徽章，还让我帮她选号。到时候看看。'))
        output = valid_output()
        output['sticky_note_updates'] = [
            sticky_update('user.gacha.audit', '用户明天要抽徽章，让我帮她选号码'),
            sticky_update(
                'user.gacha.personal',
                '她明天要抽徽章，还让我帮她选号。到时候看看。',
            ),
        ]
        result = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27},
        )
        keys = [item['note_key'] for item in result['sticky_note_updates']]
        self.assertEqual(keys, ['user.gacha.personal'])
        self.assertEqual(
            result['question_updates'][0]['question_key'],
            'character.care.boundary.question',
        )
        self.assertEqual(len(result['hypothesis_updates']), 1)

    def test_audit_sticky_does_not_fail_slow_loop(self):
        output = valid_output()
        output['sticky_note_updates'] = [sticky_update(
            'user.gacha.audit', '用户明天要抽徽章，让我帮她选号码')]
        result = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27},
        )
        self.assertEqual(result['sticky_note_updates'], [])
        self.assertTrue(result['question_updates'])
        self.assertTrue(result['hypothesis_updates'])
        self.assertEqual(result['diary_entries'], [])

    def test_sticky_emotion_labels_cover_inner_reactions(self):
        cases = [
            ('心动', '心动', '还特意回来确认一遍。啧，真会让人分心。'),
            ('自嘲', '自嘲', '刚才那句是不是太硬了。算了，我也就这德行。'),
            ('无奈', '嘴硬', '高兴？没有。只是她记得这事，勉强算不错。'),
            ('认真', '认真', '这事不能当玩笑听。下次得接住。'),
            ('警惕', '警惕', '这句不像随口说的。先别急着给答案。'),
        ]
        for index, (emotion, tone, content) in enumerate(cases):
            output = valid_output()
            output['sticky_note_updates'] = [sticky_update(
                f'user.inner.{index}',
                content,
                emotion=emotion,
                tone=tone,
                trigger_snippet='她特意回来确认蛋糕。',
            )]

            result = cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )
            note = result['sticky_note_updates'][0]
            self.assertEqual(note['emotion'], emotion)
            self.assertEqual(note['tone'], tone)
            self.assertEqual(note['trigger_snippet'], '她特意回来确认蛋糕。')
            self.assertEqual(
                note['tag'], cognitive_output.sticky_emotion_tag(emotion),
            )

    def test_sticky_payload_without_emotion_fields_still_validates(self):
        output = valid_output()
        output['sticky_note_updates'] = [{
            'note_key': 'reply.pending.topic',
            'content': '签证那件事她没讲完。之后得再问一句。',
            'status': 'active',
            'expires_in_seconds': 3600,
            'evidence_refs': [27],
        }]
        result = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27},
        )
        note = result['sticky_note_updates'][0]
        self.assertEqual(note['emotion'], '')
        self.assertEqual(note['trigger_snippet'], '')
        self.assertEqual(note['tag'], '·')

    def test_sticky_cannot_use_fast_memory_namespace(self):
        output = valid_output()
        output['sticky_note_updates'] = [sticky_update(
            'memory_lifecycle.exam', '这件事不能忘。',
        )]
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError, 'reserved_namespace',
        ):
            cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )

    def test_inner_voice_with_one_said_is_kept(self):
        output = valid_output()
        output['sticky_note_updates'] = [sticky_update(
            'user.inner.smitten',
            '她说得倒是认真。偏偏喜欢上的还是最不会领这种情的人……先看看她能坚持多久吧。',
            emotion='心动',
            trigger_snippet='因为satoru才知道喜欢和爱是什么意思',
        )]
        result = cognitive_output.validate_slow_loop_output(
            output, allowed_event_ids={27},
        )
        self.assertEqual(result['sticky_note_updates'][0]['emotion'], '心动')

    def test_event_report_summary_sticky_is_dropped(self):
        cases = [
            '她说想买蛋糕，我说随便，后来我们结束了聊天。',
            '她说因为satoru才知道喜欢和爱是什么意思，还说要变强，我告诉她那个人不会领情。',
        ]
        for content in cases:
            output = valid_output()
            output['sticky_note_updates'] = [sticky_update(
                'user.summary.bad',
                content,
                emotion='平静',
            )]
            result = cognitive_output.validate_slow_loop_output(
                output, allowed_event_ids={27},
            )
            self.assertEqual(result['sticky_note_updates'], [])

    def test_self_claim_only_cannot_commit_belief(self):
        update = committable_output()['belief_updates'][0]
        refs = [{'event_id': 1}, {'event_id': 2}]
        metadata = {
            1: {
                'source_event_type': 'character_self_claim',
                'source_event_id': 'a',
                'payload': {'evidence_category': 'character_self_claim'},
            },
            2: {
                'source_event_type': 'character_self_claim',
                'source_event_id': 'b',
                'payload': {'evidence_category': 'character_self_claim'},
            },
        }
        decision = cognitive_output._belief_commit_decision(
            update, refs, metadata, hypothesis_id=81,
        )
        self.assertEqual(decision['action'], 'held')
        self.assertEqual(decision['reason'], 'character_self_claim_only')


class StickyNoteTTLValidationTests(unittest.TestCase):
    def _validate(self, status='active', ttl=_TTL_MISSING, **kwargs):
        return cognitive_output.validate_slow_loop_output(
            output_with_sticky_ttl(status, ttl, **kwargs),
            allowed_event_ids={27},
            current_event_ids={27},
        )

    def test_active_missing_and_null_use_configured_default(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        for ttl in (_TTL_MISSING, None):
            with self.subTest(ttl=ttl):
                note = self._validate('active', ttl)['sticky_note_updates'][0]
                self.assertEqual(note['expires_in_seconds'], config['default'])

    def test_active_exact_minimum_and_maximum_are_valid(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        for ttl in (config['minimum'], config['maximum']):
            with self.subTest(ttl=ttl):
                note = self._validate('active', ttl)['sticky_note_updates'][0]
                self.assertEqual(note['expires_in_seconds'], ttl)

    def test_active_out_of_range_values_are_rejected_without_clamping(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        for ttl in (config['minimum'] - 1, config['maximum'] + 1, 0, -1):
            with self.subTest(ttl=ttl), self.assertRaisesRegex(
                cognitive_output.SlowLoopOutputError, 'ttl_out_of_range',
            ):
                self._validate('active', ttl)

    def test_active_bool_string_and_float_are_rejected(self):
        for ttl in (False, True, '0', '3600', 0.0, 3600.0):
            with self.subTest(ttl=ttl), self.assertRaisesRegex(
                cognitive_output.SlowLoopOutputError, 'ttl_invalid',
            ):
                self._validate('active', ttl)

    def test_inactive_statuses_accept_missing_null_zero_and_legacy_ttl(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        for status in ('completed', 'expired', 'archived'):
            for ttl in (_TTL_MISSING, None, 0, config['minimum'],
                        config['default'], config['maximum']):
                with self.subTest(status=status, ttl=ttl):
                    note = self._validate(status, ttl)['sticky_note_updates'][0]
                    self.assertIsNone(note['expires_in_seconds'])

    def test_inactive_out_of_range_integers_are_rejected(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        for status in ('completed', 'expired', 'archived'):
            for ttl in (-1, config['minimum'] - 1, config['maximum'] + 1):
                if ttl == 0:
                    continue  # A configured minimum of 1 leaves only zero below it.
                with self.subTest(status=status, ttl=ttl), self.assertRaisesRegex(
                    cognitive_output.SlowLoopOutputError, 'ttl_out_of_range',
                ):
                    self._validate(status, ttl)

    def test_inactive_bool_string_and_float_cannot_use_zero_compatibility(self):
        for status in ('completed', 'expired', 'archived'):
            for ttl in (False, True, '0', '3600', 0.0, 3600.0):
                with self.subTest(status=status, ttl=ttl), self.assertRaisesRegex(
                    cognitive_output.SlowLoopOutputError, 'ttl_invalid',
                ):
                    self._validate(status, ttl)

    def test_inactive_compatibility_ttl_is_normalized_and_revalidates(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        for status in ('completed', 'expired', 'archived'):
            for ttl in (0, config['minimum'], config['default'], config['maximum']):
                with self.subTest(status=status, ttl=ttl):
                    diagnostics = []
                    normalized = cognitive_output.validate_slow_loop_output(
                        output_with_sticky_ttl(status, ttl),
                        allowed_event_ids={27}, current_event_ids={27},
                        diagnostics=diagnostics,
                    )
                    self.assertEqual(normalized, self._validate(status, None))
                    self.assertEqual(diagnostics, [{
                        'error_category': ('inactive_zero_normalized' if ttl == 0
                                           else 'inactive_ttl_ignored'),
                        'field_path': 'sticky_note_updates[0].expires_in_seconds',
                        'update_index': 0,
                        'status': status,
                        'ttl_source': 'model',
                        'ttl_type': 'integer',
                        'ttl_value': ttl,
                        'ttl_min': config['minimum'],
                        'ttl_max': config['maximum'],
                    }])
                    second_diagnostics = []
                    revalidated = cognitive_output.validate_slow_loop_output(
                        normalized,
                        allowed_event_ids={27}, current_event_ids={27},
                        diagnostics=second_diagnostics,
                    )
                    self.assertEqual(revalidated, normalized)
                    self.assertEqual(second_diagnostics, [])

    def test_inactive_compatibility_ttl_persists_like_null_without_reactivation(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        for status in ('completed', 'expired', 'archived'):
            persisted = []
            for ttl in (None, 0, config['minimum'], config['default'],
                        config['maximum']):
                normalized = self._validate(status, ttl)
                cursor = StructuredCommitCursor()
                cognitive_output.persist_slow_loop_output(
                    cursor, cycle_id=7, user_id='u', character_id='gojo',
                    output=normalized, now=NOW,
                )
                sql, params = next(
                    item for item in cursor.executed
                    if item[0].startswith('INSERT INTO cognitive_sticky_notes')
                )
                persisted.append(params)
                self.assertEqual(params[4], status)
                self.assertIsNone(params[9])
                self.assertEqual(
                    params[10], NOW if status == 'completed' else None,
                )
                self.assertIn(
                    'completed_at = COALESCE( EXCLUDED.completed_at, '
                    'cognitive_sticky_notes.completed_at)',
                    sql,
                )
            for params in persisted[1:]:
                self.assertEqual(persisted[0], params)

    def test_default_above_maximum_is_a_configuration_error(self):
        with self.assertRaisesRegex(
            cognitive_config.CognitiveConfigError,
            'default_out_of_range',
        ):
            cognitive_config.validate_sticky_note_ttl_config(
                minimum=300, default=601, maximum=600,
            )

    def test_prompt_and_retry_use_effective_ttl_config(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        prompt = cognitive_worker._build_system_prompt()
        self.assertIn(f'default={config["default"]}', prompt)
        self.assertIn(
            f'inclusive range [{config["minimum"]}, {config["maximum"]}]',
            prompt,
        )
        self.assertIn('omit expires_in_seconds or use null', prompt)
        self.assertIn('integer 0 or a positive JSON integer', prompt)
        self.assertIn('the redundant TTL is ignored', prompt)

        secret_body = '这段便利贴正文绝不能出现在诊断日志里。'
        bad = output_with_sticky_ttl(
            'active', config['maximum'] + 1, content=secret_body,
        )
        good = output_with_sticky_ttl('active', config['minimum'])
        call_model = Mock(side_effect=[
            (json.dumps(bad, ensure_ascii=False), {}),
            (json.dumps(good, ensure_ascii=False), {}),
        ])
        with patch('builtins.print') as log:
            result, _usage = cognitive_worker.generate_cycle_output(
                {'cycle_id': 44, 'events': [{'event_id': 27}]},
                create_chat_fn=call_model,
            )
        self.assertEqual(
            result['sticky_note_updates'][0]['expires_in_seconds'],
            config['minimum'],
        )
        retry = call_model.call_args_list[1].kwargs['messages'][-1]['content']
        self.assertIn('sticky_note_updates[0].expires_in_seconds', retry)
        self.assertIn('status=active', retry)
        self.assertIn('ttl_type=integer', retry)
        self.assertIn(str(config['maximum'] + 1), retry)
        self.assertIn(str(config['minimum']), retry)
        self.assertIn(str(config['maximum']), retry)
        logs = '\n'.join(str(item) for item in log.call_args_list)
        self.assertIn('cycle=44', logs)
        self.assertIn('attempt=1', logs)
        self.assertIn('ttl_source=model', logs)
        self.assertIn('error_category=ttl_out_of_range', logs)
        self.assertNotIn(secret_body, logs)

    def test_inactive_zero_worker_diagnostic_does_not_log_content(self):
        secret_body = '这张结束便利贴的正文不能进入日志。'
        call_model = Mock(return_value=(json.dumps(
            output_with_sticky_ttl('archived', 0, content=secret_body),
            ensure_ascii=False,
        ), {}))
        with patch('builtins.print') as log:
            output, _usage = cognitive_worker.generate_cycle_output(
                {'cycle_id': 45, 'events': [{'event_id': 27}]},
                create_chat_fn=call_model,
            )
        self.assertIsNone(
            output['sticky_note_updates'][0]['expires_in_seconds'],
        )
        logs = '\n'.join(str(item) for item in log.call_args_list)
        self.assertIn('cycle=45', logs)
        self.assertIn('status=archived', logs)
        self.assertIn('ttl_value=0', logs)
        self.assertIn('error_category=inactive_zero_normalized', logs)
        self.assertNotIn(secret_body, logs)

    def test_inactive_positive_ttl_succeeds_without_retry_or_content_logging(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        secret_body = '这张结束便利贴的正文不能进入日志。'
        for status in ('completed', 'expired', 'archived'):
            for ttl in (config['minimum'], config['default'], config['maximum']):
                with self.subTest(status=status, ttl=ttl):
                    call_model = Mock(return_value=(json.dumps(
                        output_with_sticky_ttl(status, ttl, content=secret_body),
                        ensure_ascii=False,
                    ), {}))
                    with patch('builtins.print') as log:
                        output, _usage = cognitive_worker.generate_cycle_output(
                            {'cycle_id': 45, 'events': [{'event_id': 27}]},
                            create_chat_fn=call_model,
                        )
                    call_model.assert_called_once()
                    self.assertIsNone(
                        output['sticky_note_updates'][0]['expires_in_seconds'],
                    )
                    logs = '\n'.join(str(item) for item in log.call_args_list)
                    self.assertIn(f'status={status}', logs)
                    self.assertIn(f'ttl_value={ttl}', logs)
                    self.assertIn('error_category=inactive_ttl_ignored', logs)
                    self.assertNotIn(secret_body, logs)

    def test_inactive_retry_accepts_legacy_ttl_using_runtime_bounds(self):
        config = cognitive_config.validate_sticky_note_ttl_config(
            minimum=600, default=900, maximum=1200,
        )
        call_model = Mock(side_effect=[
            (json.dumps(output_with_sticky_ttl('expired', 1201)), {}),
            (json.dumps(output_with_sticky_ttl('expired', 900)), {}),
        ])
        with patch.object(cognitive_config, 'COGNITIVE_STICKY_NOTE_TTL_CONFIG', config), \
             patch('builtins.print'):
            output, _usage = cognitive_worker.generate_cycle_output(
                {'cycle_id': 47, 'events': [{'event_id': 27}]},
                create_chat_fn=call_model,
            )
        self.assertIsNone(output['sticky_note_updates'][0]['expires_in_seconds'])
        self.assertEqual(call_model.call_count, 2)
        prompt = call_model.call_args.kwargs['system']
        retry = call_model.call_args.kwargs['messages'][-1]['content']
        self.assertIn('default=900', prompt)
        for instruction in (prompt, retry):
            self.assertIn('inclusive range [600, 1200]', instruction)
            self.assertIn('omit expires_in_seconds or use null', instruction)
        self.assertIn('status=expired', retry)
        self.assertIn('integer 0 or a non-boolean positive JSON integer', retry)
        self.assertIn('accepted and ignored (normalized to null)', retry)

    def test_configuration_error_is_not_sent_to_the_model_for_retry(self):
        call_model = Mock()
        error = cognitive_config.CognitiveConfigError(
            'sticky_note_ttl_config_invalid:default_out_of_range',
        )
        with patch.object(
            cognitive_worker, 'get_sticky_note_ttl_config', side_effect=error,
        ):
            with self.assertRaises(cognitive_config.CognitiveConfigError):
                cognitive_worker.generate_cycle_output(
                    {'cycle_id': 46, 'events': [{'event_id': 27}]},
                    create_chat_fn=call_model,
                )
        call_model.assert_not_called()


class SlowLoopTransactionTests(unittest.TestCase):
    def test_structured_output_and_trigger_consumption_commit_together(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        result = cognitive_queue.commit_cycle_success(
            7,
            reasoning_context={
                'events': [{'event_id': 27, 'occurred_at': NOW}],
                'temporal': {'queued_at': NOW},
            },
            structured_output=valid_output(),
            worker_model='test-model',
            worker_usage={'input_tokens': 10, 'output_tokens': 20},
            conn=connection,
            now=NOW,
        )
        self.assertEqual(result['output_state_version'], 5)
        self.assertEqual(connection.commits, 1)
        self.assertEqual(connection.rollbacks, 0)
        sql = '\n'.join(item[0] for item in cursor.executed)
        for table in (
            'cognitive_questions', 'cognitive_hypotheses',
            'cognitive_predictions', 'cognitive_cycles',
        ):
            self.assertIn(table, sql)
        self.assertNotIn('INSERT INTO cognitive_beliefs', sql)
        self.assertIn("SET status = 'consumed'", sql)
        self.assertIn('cycle_summary', sql)
        self.assertIn('belief_commit_decisions', sql)
        self.assertIn('evidence_refs', sql)
        cycle_update = next(
            params for statement, params in cursor.executed
            if statement.startswith('UPDATE cognitive_cycles SET status')
        )
        self.assertIn('2026-09-13T08:00:00+00:00', cycle_update[1])

    def test_belief_commit_candidate_passes_gate_with_independent_evidence(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        result = cognitive_queue.commit_cycle_success(
            7,
            reasoning_context={
                'events': [{'event_id': 27, 'occurred_at': NOW}],
                'current_beliefs': [{
                    'evidence_refs': [{'event_id': 12, 'reason': 'earlier'}],
                }],
                'temporal': {'queued_at': NOW},
            },
            structured_output=committable_output(),
            worker_model='test-model',
            conn=connection,
            now=NOW,
        )
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertIn('INSERT INTO cognitive_beliefs', sql)
        self.assertEqual(
            result['output']['belief_commit_decisions'][0]['action'],
            'committed',
        )

    def test_invalid_output_rolls_back_before_consuming_triggers(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        output = valid_output()
        output['hypothesis_updates'][0]['supporting_evidence_refs'] = [404]
        with self.assertRaises(cognitive_output.SlowLoopOutputError):
            cognitive_queue.commit_cycle_success(
                7, structured_output=output, conn=connection, now=NOW,
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertNotIn("SET status = 'consumed'", sql)

    def test_invalid_sticky_ttl_rolls_back_entire_cycle(self):
        cursor = StructuredCommitCursor()
        connection = TransactionConnection(cursor)
        output = output_with_sticky_ttl('active', 0)
        with self.assertRaisesRegex(
            cognitive_output.SlowLoopOutputError, 'ttl_out_of_range',
        ):
            cognitive_queue.commit_cycle_success(
                7, structured_output=output, conn=connection, now=NOW,
            )
        self.assertEqual(connection.commits, 0)
        self.assertEqual(connection.rollbacks, 1)
        sql = '\n'.join(item[0] for item in cursor.executed)
        self.assertNotIn('INSERT INTO cognitive_sticky_notes', sql)
        self.assertNotIn("SET status = 'consumed'", sql)


class SlowLoopWorkerTests(unittest.TestCase):
    def test_invalid_stance_is_corrected_using_the_validator_enum(self):
        from cognitive_predictions import PREDICTION_STANCE_TYPES

        bad = valid_output()
        bad['new_predictions'][0]['metadata']['violation_signals'] = [{
            'signal_type': 'character_stance_declared',
            'actor': 'character',
            'attributes': {'stance_type': 'romantic_rejection'},
        }]
        call_model = Mock(side_effect=[
            (json.dumps(bad), {}),
            (json.dumps(valid_output()), {}),
        ])
        output, _ = cognitive_worker.generate_cycle_output(
            {'events': [{'event_id': 27}]}, create_chat_fn=call_model)
        self.assertEqual(call_model.call_count, 2)
        prompt = call_model.call_args.kwargs['system']
        for stance in PREDICTION_STANCE_TYPES:
            self.assertIn(stance, prompt)
        self.assertIn('violation_signals', prompt)
        self.assertIn('new_predictions may be []', prompt)
        self.assertEqual(output['evidence_refs'][0]['event_id'], 27)
        self.assertNotIn('romantic_rejection', json.dumps(output))

    def test_unsupported_stance_still_rejected_after_retry(self):
        bad = valid_output()
        bad['new_predictions'][0]['metadata']['violation_signals'] = [{
            'signal_type': 'character_stance_declared',
            'actor': 'character',
            'attributes': {'stance_type': 'invented_stance'},
        }]
        with self.assertRaisesRegex(cognitive_output.SlowLoopOutputError,
                                    'violation_signals_0_stance_type_invalid'):
            cognitive_worker.generate_cycle_output(
                {'events': [{'event_id': 27}]},
                create_chat_fn=Mock(return_value=(json.dumps(bad), {})))

    def test_empty_output_retry_does_not_send_empty_assistant_message(self):
        call_model = Mock(side_effect=[('', {}), (json.dumps(valid_output()), {})])
        cognitive_worker.generate_cycle_output(
            {'events': [{'event_id': 27}]}, create_chat_fn=call_model)
        self.assertTrue(all(message['content'].strip()
                            for message in call_model.call_args.kwargs['messages']))

    def test_generate_retries_invalid_json_then_accepts_valid_output(self):
        responses = [
            ('not-json', {}),
            (json.dumps(valid_output(), ensure_ascii=False), {'input_tokens': 1}),
        ]
        call_model = Mock(side_effect=responses)
        output, usage = cognitive_worker.generate_cycle_output(
            {'events': [{'event_id': 27}]}, create_chat_fn=call_model,
        )
        self.assertEqual(call_model.call_count, 2)
        self.assertEqual(output['cycle_summary']['confidence'], 'high')
        self.assertEqual(usage['input_tokens'], 1)

    def test_worker_claims_builds_calls_and_commits(self):
        with patch.object(cognitive_worker, 'maintain_scheduled_reflections'), \
             patch.object(cognitive_worker, 'maintain_pending_cycles'), \
             patch.object(cognitive_worker, 'claim_next_cycle', return_value={
                 'status': 'running', 'cycle_id': 7,
             }), \
             patch.object(cognitive_worker, 'build_reasoning_context', return_value={
                 'events': [{'event_id': 27}],
             }), \
             patch.object(cognitive_worker, 'generate_cycle_output', return_value=(
                 valid_output(), {'input_tokens': 10},
             )), \
             patch.object(cognitive_worker, 'commit_cycle_success', return_value={
                 'status': 'succeeded', 'output_state_version': 1,
             }) as commit:
            result = cognitive_worker.run_worker_once(now=NOW)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(commit.call_args.kwargs['structured_output'], valid_output())

    def test_worker_failure_releases_cycle(self):
        with patch.object(cognitive_worker, 'maintain_scheduled_reflections'), \
             patch.object(cognitive_worker, 'maintain_pending_cycles'), \
             patch.object(cognitive_worker, 'claim_next_cycle', return_value={
                 'status': 'running', 'cycle_id': 7,
             }), \
             patch.object(cognitive_worker, 'build_reasoning_context', return_value={
                 'events': [{'event_id': 27}],
             }), \
             patch.object(
                 cognitive_worker, 'generate_cycle_output',
                 side_effect=cognitive_output.SlowLoopOutputError('bad_schema'),
             ), \
             patch.object(cognitive_worker, 'fail_cycle', return_value={
                 'status': 'failed', 'triggers': [],
             }) as fail:
            result = cognitive_worker.run_worker_once(now=NOW)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('bad_schema', fail.call_args.args[1])

    def test_invalid_ttl_releases_then_corrected_retry_commits_once(self):
        config = cognitive_config.get_sticky_note_ttl_config()
        bad = output_with_sticky_ttl('active', config['maximum'] + 1)
        good = output_with_sticky_ttl('active', config['minimum'])
        call_model = Mock(side_effect=[
            (json.dumps(bad, ensure_ascii=False), {}),
            (json.dumps(bad, ensure_ascii=False), {}),
            (json.dumps(good, ensure_ascii=False), {}),
        ])
        claims = [
            {'status': 'running', 'cycle_id': 7},
            {'status': 'running', 'cycle_id': 8},
        ]

        def context(cycle_id):
            return {'cycle_id': cycle_id, 'events': [{'event_id': 27}]}

        with patch.object(cognitive_worker, 'maintain_scheduled_reflections'), \
             patch.object(cognitive_worker, 'maintain_pending_cycles'), \
             patch.object(cognitive_worker, 'claim_next_cycle', side_effect=claims), \
             patch.object(cognitive_worker, 'build_reasoning_context', side_effect=context), \
             patch.object(cognitive_worker, 'fail_cycle', return_value={
                 'status': 'failed',
                 'triggers': [{'trigger_id': 9, 'status': 'pending'}],
             }) as fail, \
             patch.object(cognitive_worker, 'commit_cycle_success', return_value={
                 'status': 'succeeded', 'output_state_version': 1,
             }) as commit, \
             patch('builtins.print'):
            first = cognitive_worker.run_worker_once(
                create_chat_fn=call_model, now=NOW,
            )
            second = cognitive_worker.run_worker_once(
                create_chat_fn=call_model, now=NOW,
            )

        self.assertEqual(first['status'], 'failed')
        self.assertEqual(first['triggers'][0]['status'], 'pending')
        self.assertEqual(second['status'], 'succeeded')
        self.assertEqual(call_model.call_count, 3)
        fail.assert_called_once()
        self.assertIn('sticky_note_update_0_ttl_out_of_range', fail.call_args.args[1])
        commit.assert_called_once()
        self.assertEqual(commit.call_args.args[0], 8)

    def test_worker_prompt_forbids_response_policy_and_relationship_writes(self):
        prompt = cognitive_worker._SYSTEM_PROMPT
        self.assertIn('Do not write dialogue', prompt)
        self.assertIn('modify relationship scores', prompt)
        self.assertIn('question_updates', prompt)
        self.assertIn('character_self_claim', prompt)
        self.assertIn('belief_updates are commit candidates', prompt)
        self.assertIn('reflection_note is a compact internal note', prompt)
        self.assertIn('FIRST PERSON', prompt)
        self.assertIn('private inner note', prompt)
        self.assertIn('"emotion"', prompt)
        self.assertIn('"trigger_snippet"', prompt)
        self.assertIn('Allowed sticky emotions', prompt)
        self.assertIn('not a recap of the conversation', prompt)
        self.assertIn('not a chat-response emotion', prompt)
        self.assertIn('没有说出口、但心里还挂着的一句话', prompt)
        self.assertIn('她说了什么 / 我说了什么', prompt)
        self.assertIn('prescribe a chat-response emotion', prompt)
        self.assertNotIn('Do not invent an emotion field', prompt)
        self.assertIn('self_disclosure', prompt)
        source = inspect.getsource(cognitive_worker)
        self.assertNotIn('UPDATE rel_state', source)
        self.assertNotIn('INSERT INTO rel_state', source)


class SlowLoopSchemaTests(unittest.TestCase):
    def test_schema_contains_all_structured_outputs_and_beliefs(self):
        ddl = '\n'.join(cognitive_db.ddl_statements())
        for field in (
            'cycle_summary', 'question_updates', 'belief_updates',
            'belief_commit_decisions', 'hypothesis_updates',
            'new_predictions', 'evidence_refs', 'reflection_note',
        ):
            self.assertIn(field, ddl)
        self.assertIn('CREATE TABLE IF NOT EXISTS cognitive_beliefs', ddl)
        self.assertIn('supporting_evidence_refs', ddl)
        self.assertIn('contradicting_evidence_refs', ddl)
        self.assertIn('self_model_evidence', ddl)
        self.assertIn('cognitive_worker_migrations', ddl)
        self.assertIn('CREATE TABLE IF NOT EXISTS cognitive_sticky_notes', ddl)
        self.assertIn(
            "ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb",
            ddl,
        )

    def test_datetime_serialization_failures_are_requeued_once(self):
        source = inspect.getsource(cognitive_db.init_cognitive_tables)
        self.assertIn('slow_worker_v1_datetime_serialization_recovery', source)
        self.assertIn(
            'slow_loop_typeerror:Object_of_type_datetime_is_not_JSON_serializable',
            source,
        )
        self.assertIn("status IN ('pending', 'dead_letter')", source)

    def test_old_count_predictions_are_superseded_once(self):
        source = inspect.getsource(cognitive_db.init_cognitive_tables)
        self.assertIn('semantic_prediction_v2_supersede_count_resolvers', source)
        self.assertIn('superseded_nonsemantic_prediction_v2', source)
        self.assertIn("resolver_name <> 'current_event_signal_outcome'", source)


class SlowLoopInspectionTests(unittest.TestCase):
    def test_snapshot_exposes_conclusions_without_reasoning_context(self):
        connection = SnapshotConnection()
        snapshot = cognitive_inspect.fetch_cognitive_snapshot(
            'u', 'gojo', limit=5, conn=connection,
        )
        self.assertEqual(snapshot['cycles'][0]['cycle_id'], 7)
        self.assertEqual(snapshot['cycles'][0]['cycle_summary']['confidence'], 'high')
        self.assertEqual(snapshot['cycles'][0]['reflection_note']['evidence_refs'], [27])
        self.assertEqual(snapshot['questions'][0]['status'], 'active')
        self.assertEqual(snapshot['beliefs'][0]['status'], 'active')
        self.assertEqual(snapshot['beliefs'][0]['belief_type'], 'user_model')
        self.assertEqual(snapshot['hypotheses'][0]['status'], 'supported')
        self.assertEqual(snapshot['hypotheses'][0]['confidence'], 0.62)
        self.assertEqual(snapshot['predictions'][0]['status'], 'pending')
        self.assertNotIn('reasoning_context', snapshot['cycles'][0])
        self.assertEqual(connection._cursor.executed[0][1], ('u', 'gojo', 5))

    def test_duplicate_diary_entries_are_not_inserted_twice(self):
        output = valid_output()
        output['diary_entries'] = [{
            'diary_key': 'exam.noticed',
            'content': '她把考试说得很轻。',
            'reflection_kind': 'event',
            'evidence_refs': [27],
        }]
        cursor = StructuredCommitCursor()
        cursor.existing_diary = [('exam.noticed', json.dumps([{'event_id': 27}]))]
        original_execute = cursor.execute

        def execute(sql, params=None):
            compact = ' '.join(sql.split())
            original_execute(sql, params)
            if compact.startswith('SELECT diary_key'):
                cursor.many = list(cursor.existing_diary)
            elif compact.startswith('SELECT id, source_event_type'):
                cursor.many = [(
                    27, 'user_message', 'e27', 'chat',
                    {'evidence_category': 'user_statement'},
                )]
            elif compact.startswith('INSERT INTO cognitive_diary_entries'):
                cursor.inserted_diary = getattr(cursor, 'inserted_diary', [])
                cursor.inserted_diary.append(params)

        cursor.execute = execute
        cognitive_output.persist_slow_loop_output(
            cursor, cycle_id=7, user_id='u', character_id='gojo',
            output=output, now=NOW)
        self.assertEqual(getattr(cursor, 'inserted_diary', []), [])

    def test_sticky_metadata_is_written_and_replaced_on_update(self):
        first = cognitive_output.validate_slow_loop_output(
            {
                **valid_output(),
                'sticky_note_updates': [sticky_update(
                    'user.cake.confirm',
                    '连这种事都记着，还特地回来确认。……行吧，多少有点期待。',
                    emotion='心动',
                    trigger_snippet='因为satoru才知道喜欢和爱是什么意思',
                )],
            },
            allowed_event_ids={27},
        )
        second = cognitive_output.validate_slow_loop_output(
            {
                **valid_output(),
                'sticky_note_updates': [sticky_update(
                    'user.cake.confirm',
                    '高兴？没有。只是她记得这事，勉强算不错。',
                    emotion='嘴硬',
                    trigger_snippet='还特地回来确认蛋糕。',
                )],
            },
            allowed_event_ids={27},
        )
        cursor = StructuredCommitCursor()
        cognitive_output.persist_slow_loop_output(
            cursor, cycle_id=7, user_id='u', character_id='gojo',
            output=first, now=NOW)
        cognitive_output.persist_slow_loop_output(
            cursor, cycle_id=8, user_id='u', character_id='gojo',
            output=second, now=NOW)
        sticky_params = [
            params for statement, params in cursor.executed
            if statement.startswith('INSERT INTO cognitive_sticky_notes')
        ]
        self.assertEqual(len(sticky_params), 2)
        first_meta = json.loads(sticky_params[0][-2])
        second_meta = json.loads(sticky_params[1][-2])
        self.assertEqual(sticky_params[0][3], first['sticky_note_updates'][0]['content'])
        self.assertEqual(first_meta['emotion'], '心动')
        self.assertEqual(
            first_meta['trigger_snippet'],
            '因为satoru才知道喜欢和爱是什么意思',
        )
        self.assertEqual(first_meta['tag'], '♡')
        self.assertEqual(
            sticky_params[1][3], second['sticky_note_updates'][0]['content'],
        )
        self.assertEqual(second_meta['emotion'], '嘴硬')
        self.assertEqual(second_meta['trigger_snippet'], '还特地回来确认蛋糕。')
        self.assertEqual(second_meta['tag'], '~')
        sticky_sql = next(
            statement for statement, _params in cursor.executed
            if statement.startswith('INSERT INTO cognitive_sticky_notes')
        )
        self.assertIn('metadata = EXCLUDED.metadata', sticky_sql)

    def test_legacy_sticky_persist_writes_empty_display_metadata(self):
        output = cognitive_output.validate_slow_loop_output(
            {
                **valid_output(),
                'sticky_note_updates': [{
                    'note_key': 'reply.pending.topic',
                    'content': '签证那件事她没讲完。之后得再问一句。',
                    'status': 'active',
                    'expires_in_seconds': 3600,
                    'evidence_refs': [27],
                }],
            },
            allowed_event_ids={27},
        )
        cursor = StructuredCommitCursor()
        cognitive_output.persist_slow_loop_output(
            cursor, cycle_id=7, user_id='u', character_id='gojo',
            output=output, now=NOW)
        sticky_params = next(
            params for statement, params in cursor.executed
            if statement.startswith('INSERT INTO cognitive_sticky_notes')
        )
        metadata = json.loads(sticky_params[-2])
        self.assertEqual(metadata['emotion'], '')
        self.assertEqual(metadata['trigger_snippet'], '')
        self.assertEqual(metadata['tag'], '·')


class MemoryContaminationGuardTests(unittest.TestCase):
    def test_character_self_claims_are_routed_away_from_bond_memory(self):
        with open(os.path.join(BACKEND, 'user_memory.py'), encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('character_self_claim', source)
        self.assertIn('not_bond_memory', source)
        self.assertIn('self_model_evidence', source)
        self.assertIn('bond 改道自我陈述证据', source)
        self.assertIn('_looks_like_character_self_claim(content)', source)


if __name__ == '__main__':
    unittest.main()
