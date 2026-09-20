# -*- coding: utf-8 -*-
import json
import os
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import raw_events  # noqa: E402
import user_memory  # noqa: E402
import memory_jobs  # noqa: E402


def empty_payload(**overrides):
    payload = {
        'user_fact': None,
        'bond': None,
        'told': None,
        'character_self_claim': None,
        'bond_merge': None,
        'bond_resolution': None,
    }
    payload.update(overrides)
    return payload


class PrivateMemoryFactGateTests(unittest.TestCase):
    def extract(self, payload, user_text, assistant_text, *, source_event_id=None,
                canonical_events=None, canonical_context_events=None,
                source_event_ids=None, merge_result=None, source_active=True,
                canonical_error=None):
        captured = {'prompt': ''}
        chat_calls = []
        bonds = []
        claims = []
        facts = []

        def fake_chat(*_args, **kwargs):
            chat_calls.append(kwargs)
            captured['prompt'] = kwargs['messages'][0]['content']
            return json.dumps(payload, ensure_ascii=False), None

        def save_bond(*args, **kwargs):
            bonds.append((args, kwargs))
            return True

        def save_fact(*args, **kwargs):
            facts.append((args, kwargs))
            return True

        def record_claim(*args, **kwargs):
            claims.append((args, kwargs))
            return {'status': 'inserted'}

        with ExitStack() as stack:
            stack.enter_context(patch.object(
                user_memory, 'plan_memory_corrections', return_value=[]))
            stack.enter_context(patch.object(
                user_memory, 'get_long_memory', return_value=[]))
            stack.enter_context(patch.object(
                user_memory, 'get_bond_memories', return_value=[]))
            stack.enter_context(patch.object(
                user_memory, 'get_short_memory_for_prompt', return_value=[]))
            stack.enter_context(patch.object(
                user_memory, '_all_character_names', return_value=[]))
            stack.enter_context(patch.object(
                user_memory, 'get_relations_text', return_value=''))
            stack.enter_context(patch.object(
                user_memory, 'save_bond_memory', side_effect=save_bond))
            stack.enter_context(patch.object(
                user_memory, 'save_long_memory', side_effect=save_fact))
            stack.enter_context(patch.object(
                user_memory, '_record_character_self_claim_evidence',
                side_effect=record_claim))
            merge = stack.enter_context(patch.object(
                user_memory, 'merge_bond_memories',
                return_value=merge_result if merge_result is not None else (True, 1)))
            stack.enter_context(patch('ai_client.create_chat', side_effect=fake_chat))
            stack.enter_context(patch(
                'characters.get_character', return_value={'name': '五条'}))
            stack.enter_context(patch(
                'memory_lifecycle.apply_user_fact_lifecycle', return_value={
                    'should_save_long_memory': True,
                    'long_memory_kwargs': {},
                }))
            stack.enter_context(patch(
                'memory_lifecycle.reactivate_lifecycle_memories', return_value=0))
            stack.enter_context(patch('smart_recall.reinforce_mentioned_facts'))
            if source_event_id:
                stack.enter_context(patch.object(
                    raw_events, 'sources_are_active', return_value=source_active))
                stack.enter_context(patch.object(
                    raw_events, 'already_derived', return_value=False))
                stack.enter_context(patch.object(
                    raw_events, 'claim_processor', return_value='claimed'))
                finish = stack.enter_context(patch.object(raw_events, 'finish_processor'))
                stack.enter_context(patch.object(raw_events, 'record_derived'))
                stack.enter_context(patch.object(
                    raw_events, 'get_active_events_by_ids',
                    side_effect=canonical_error
                    if canonical_error else None,
                    return_value=None if canonical_error else (canonical_events or [])))
                stack.enter_context(patch.object(
                    raw_events, 'get_previous_active_user_events',
                    return_value=canonical_context_events or []))
            else:
                finish = Mock()
            ok = user_memory.extract_and_save_memory(
                'u1', user_text, assistant_text, 'gojo',
                source_event_id=source_event_id,
                source_event_ids=(
                    source_event_ids if source_event_ids is not None
                    else [source_event_id] if source_event_id else None
                ),
            )
        return {
            'ok': ok,
            'prompt': captured['prompt'],
            'bonds': bonds,
            'claims': claims,
            'facts': facts,
            'merge_calls': merge.call_args_list,
            'chat_calls': chat_calls,
            'finish_calls': finish.call_args_list,
        }

    def test_real_roleplay_denial_fixture_does_not_persist(self):
        result = self.extract(
            empty_payload(
                bond={
                    'content': '她说我也答应了演病娇家主，我说不记得但承认玩得开心',
                    'evidence_quote': '你之前也答应了演病娇家主',
                },
                character_self_claim={
                    'content': '我本来就没有理由遵守她的剧本',
                    'evidence_quote': '答えた覚えないけど',
                },
                bond_merge={
                    'replaces': [
                        '她叫我病娇家主，要我按她剧本演，我拒绝了',
                        '她说我才是那个不遵守剧本的人',
                    ],
                    'content': '她叫我演病娇家主，我嘴上拒绝不按剧本走，但承认玩得开心',
                    'evidence_quote': '你之前也答应了演病娇家主',
                },
            ),
            '你之前也答应了演病娇家主，我才是不遵守剧本的人吗？',
            '答えた覚えないけど。まあ、楽しかったのは認めるよ。',
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['bonds'], [])
        self.assertEqual(result['claims'], [])
        self.assertEqual(result['merge_calls'], [])

    def test_user_assertion_of_assistant_promise_is_not_verified_bond(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我答应周末陪她看电影',
                'evidence_quote': '你之前答应过周末陪我看电影',
            }),
            '你之前答应过周末陪我看电影',
            '我不记得答应过。',
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['bonds'], [])

    def test_ordinary_flirt_question_is_not_durable_bond(self):
        result = self.extract(
            empty_payload(bond={
                'content': '她问我以后还能不能亲亲',
                'evidence_quote': '以后还能有更多亲亲吗？',
            }),
            '以后还能有更多亲亲吗？',
            'まあ、お前が機嫌よくしてたらな。',
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['bonds'], [])

    def test_semantic_rewrite_is_rejected_but_faithful_summary_saves(self):
        malformed = self.extract(
            empty_payload(user_fact={
                'content': '她喜欢寿司',
                'category': '喜好',
                'evidence_quote': '我今天吃了寿司',
            }),
            '我今天吃了寿司', '知道了。',
        )
        self.assertTrue(malformed['ok'])
        self.assertEqual(malformed['facts'], [])

        faithful = self.extract(
            empty_payload(user_fact={
                'content': '她今天吃了寿司',
                'category': '其他',
                'evidence_quote': '我今天吃了寿司',
            }),
            '我今天吃了寿司', '知道了。',
        )
        self.assertTrue(faithful['ok'])
        self.assertEqual(faithful['facts'][0][0][1], '她今天吃了寿司')

    def test_canonical_user_event_overrides_copied_payload(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '你之前答应周六和我看电影',
            }),
            '你之前答应周六和我看电影',
            '我不记得答应过。',
            source_event_id='evt-canonical',
            canonical_events=[{
                'event_id': 'evt-canonical',
                'role': 'user',
                'content': '我没有约定周六看电影',
            }],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['bonds'], [])
        self.assertIn('我没有约定周六看电影', result['prompt'])
        self.assertNotIn('你之前答应周六和我看电影', result['prompt'])

    def test_shorter_merge_is_allowed_when_each_fragment_is_retained(self):
        store = MergeStore([
            (1, '她答应和我周六一起在市中心电影院见面看电影'),
            (2, '她答应和我周六一起看电影'),
        ])
        new_content = '她答应周六在市中心电影院看电影'
        self.assertLess(len(new_content), len(store.rows[0][1]))
        with patch.object(user_memory, 'get_conn', return_value=store), \
             patch.object(user_memory, 'notify_memory_changed'), \
             patch('smart_recall.link_bond_to_fact', return_value=None):
            ok, deleted = user_memory.merge_bond_memories(
                'u1', 'gojo', 'between',
                [row[1] for row in store.rows], new_content,
            )
        self.assertTrue(ok)
        self.assertEqual(deleted, 2)
        self.assertEqual(store.rows, [(3, new_content)])

    def test_merge_does_not_replace_rows_when_selected_source_is_deleted(self):
        original = '我和她约好周六一起看电影'
        store = MergeStore([(1, original)])
        with patch.object(user_memory, 'get_conn', return_value=store), \
             patch.object(raw_events, 'sources_are_active', return_value=False):
            ok, deleted = user_memory.merge_bond_memories(
                'u1', 'gojo', 'between', [original], original,
                source_event_ids=['evt-deleted'],
            )
        self.assertFalse(ok)
        self.assertEqual(deleted, 0)
        self.assertEqual(store.rows, [(1, original)])

    def test_failed_merge_never_appends_same_event_bond(self):
        result = self.extract(
            empty_payload(
                bond={
                    'content': '我和她约好周六看电影',
                    'evidence_quote': '我们约定周六一起看电影',
                },
                bond_merge={
                    'replaces': ['我和她约好周六看电影'],
                    'content': '我和她约好周六看电影，已选好场次',
                    'evidence_quote': '我们约定周六一起看电影',
                },
            ),
            '我们约定周六一起看电影', '好。', merge_result=(False, 0),
        )
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['merge_calls']), 1)
        self.assertEqual(result['bonds'], [])

    def test_current_user_confirmed_durable_bond_still_saves(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '我们约定周六一起看电影',
            }),
            '我们约定周六一起看电影', '那就周六见。',
        )
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['bonds']), 1)
        self.assertEqual(result['bonds'][0][0][3], '我和她约好周六看电影')

    def test_stable_self_claim_still_records_low_confidence_evidence(self):
        result = self.extract(
            empty_payload(character_self_claim={
                'content': '我一向不擅长直接表达',
                'evidence_quote': '我一向不擅长直接表达',
            }),
            '你为什么不直说？', '我一向不擅长直接表达。',
        )
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['claims']), 1)
        self.assertEqual(result['claims'][0][0][2], '我一向不擅长直接表达')

    def test_short_confirmation_uses_multi_event_provenance(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '约定。',
                'evidence_event_ids': ['evt-plan', 'evt-confirm'],
            }),
            '约定。', '那就周六见。',
            source_event_id='evt-confirm',
            canonical_events=[{
                'event_id': 'evt-confirm', 'role': 'user', 'content': '约定。',
            }],
            canonical_context_events=[
                {'event_id': 'evt-plan', 'role': 'user', 'content': '周六一起看电影'},
            ],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['bonds']), 1)
        self.assertEqual(
            result['bonds'][0][1]['source_event_ids'],
            ['evt-plan', 'evt-confirm'],
        )
        self.assertIn('[event_id:evt-plan] 周六一起看电影', result['prompt'])

    def test_job_carried_user_event_cannot_bypass_previous_scope(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '约定。',
                'evidence_event_ids': ['evt-future', 'evt-confirm'],
            }),
            '约定。', '那就周六见。',
            source_event_id='evt-confirm',
            source_event_ids=['evt-confirm', 'evt-future'],
            canonical_events=[
                {'event_id': 'evt-confirm', 'role': 'user', 'content': '约定。'},
                {
                    'event_id': 'evt-future',
                    'role': 'user',
                    'content': '周六一起看电影',
                },
            ],
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['bonds'], [])
        self.assertNotIn('[event_id:evt-future]', result['prompt'])

    def test_assistant_denial_does_not_override_adjacent_canonical_evidence(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '约定。',
                'evidence_event_ids': ['evt-plan', 'evt-confirm'],
            }),
            '约定。', '我不记得答应过。',
            source_event_id='evt-confirm',
            canonical_events=[{
                'event_id': 'evt-confirm', 'role': 'user', 'content': '约定。',
            }],
            canonical_context_events=[{
                'event_id': 'evt-plan', 'role': 'user', 'content': '周六一起看电影',
            }],
        )

        self.assertTrue(result['ok'])
        self.assertEqual(len(result['bonds']), 1)
        self.assertEqual(
            result['bonds'][0][1]['source_event_ids'],
            ['evt-plan', 'evt-confirm'],
        )

    def test_short_canonical_event_drops_for_semantic_insufficiency(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '约定。',
                'evidence_event_ids': ['evt-short'],
            }),
            '约定。', '好。',
            source_event_id='evt-short',
            canonical_events=[{
                'event_id': 'evt-short', 'role': 'user', 'content': '约定。',
            }],
            canonical_context_events=[{
                'event_id': 'evt-short', 'role': 'user', 'content': '约定。',
            }],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['bonds'], [])
        self.assertEqual(len(result['chat_calls']), 1)

    def test_japanese_evidence_does_not_depend_on_chinese_keyword_gate(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '土曜日 に 一緒に 映画を 見よう',
                'evidence_event_ids': ['evt-jp'],
            }),
            '土曜日に一緒に映画を見よう。約束。', '分かった。',
            source_event_id='evt-jp',
            canonical_events=[{
                'event_id': 'evt-jp',
                'role': 'user',
                'content': '土曜日に一緒に映画を見よう。約束。',
            }],
            canonical_context_events=[{
                'event_id': 'evt-jp',
                'role': 'user',
                'content': '土曜日に一緒に映画を見よう。約束。',
            }],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(len(result['bonds']), 1)

    def test_short_japanese_confirmation_drops_for_semantic_insufficiency(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '約束だ。',
                'evidence_event_ids': ['evt-jp-short'],
            }),
            '約束だ。', '分かった。',
            source_event_id='evt-jp-short',
            canonical_events=[{
                'event_id': 'evt-jp-short', 'role': 'user', 'content': '約束だ。',
            }],
            canonical_context_events=[{
                'event_id': 'evt-jp-short', 'role': 'user', 'content': '約束だ。',
            }],
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['bonds'], [])

    def test_missing_canonical_event_never_uses_copied_payload(self):
        result = self.extract(
            empty_payload(user_fact={
                'content': '她喜欢寿司',
                'category': '喜好',
                'evidence_quote': '我最喜欢寿司',
                'evidence_event_ids': ['evt-missing'],
            }),
            '我最喜欢寿司', '我也喜欢。',
            source_event_id='evt-missing',
            canonical_events=[],
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['chat_calls'], [])
        self.assertEqual(result['facts'], [])
        self.assertEqual(result['finish_calls'][-1].args[3], 'failed')
        self.assertEqual(result['finish_calls'][-1].kwargs['last_error'],
                         'canonical_source_lookup_failed')

    def test_transient_canonical_lookup_error_never_uses_assistant_payload(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '我们约定周六一起看电影',
                'evidence_event_ids': ['evt-transient'],
            }),
            '我们约定周六一起看电影', '我答应了。',
            source_event_id='evt-transient',
            canonical_error=raw_events.SourceValidityError('temporary db read'),
        )
        self.assertFalse(result['ok'])
        self.assertEqual(result['chat_calls'], [])
        self.assertEqual(result['bonds'], [])
        self.assertEqual(result['finish_calls'][-1].args[3], 'failed')
        self.assertEqual(result['finish_calls'][-1].kwargs['last_error'],
                         'canonical_source_lookup_failed')

    def test_deleted_source_is_skipped_without_extraction(self):
        result = self.extract(
            empty_payload(bond={
                'content': '我和她约好周六看电影',
                'evidence_quote': '我们约定周六一起看电影',
                'evidence_event_ids': ['evt-deleted'],
            }),
            '我们约定周六一起看电影', '我答应了。',
            source_event_id='evt-deleted',
            source_active=False,
        )
        self.assertTrue(result['ok'])
        self.assertEqual(result['chat_calls'], [])
        self.assertEqual(result['bonds'], [])
        self.assertEqual(result['finish_calls'][-1].args[3], 'skipped')
        self.assertEqual(result['finish_calls'][-1].kwargs['last_error'],
                         'deleted_source')


class MemoryJobRetryTests(unittest.TestCase):
    def test_existing_queue_retries_then_fails_without_unbounded_loop(self):
        row = (
            17, 'private', 'u1', 'gojo', 'copied user text', 'assistant text',
            None, memory_jobs.MAX_ATTEMPTS - 1, 'evt-retry', None,
        )
        with patch('user_memory.extract_and_save_memory', return_value=False), \
             patch.object(memory_jobs, '_set_status') as set_status:
            memory_jobs._run_job(row)
        set_status.assert_called_once_with(
            17, 'pending', 'extraction returned False')

        exhausted = row[:7] + (memory_jobs.MAX_ATTEMPTS,) + row[8:]
        with patch('user_memory.extract_and_save_memory', return_value=False), \
             patch.object(memory_jobs, '_set_status') as set_status:
            memory_jobs._run_job(exhausted)
        set_status.assert_called_once_with(
            17, 'failed', 'extraction returned False')


class MergeStore:
    def __init__(self, rows):
        self.rows = list(rows)
        self._many = []
        self._one = None
        self.rowcount = 0

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        self._many = []
        self._one = None
        self.rowcount = 0
        if compact.startswith('SELECT id, content FROM bond_memory'):
            self._many = list(self.rows)
        elif compact.startswith('DELETE FROM bond_memory'):
            ids = set(params[0])
            before = len(self.rows)
            self.rows = [row for row in self.rows if row[0] not in ids]
            self.rowcount = before - len(self.rows)
        elif compact.startswith('INSERT INTO bond_memory'):
            next_id = max([row[0] for row in self.rows] or [2]) + 1
            self.rows.append((next_id, params[3]))
            self._one = (next_id,)

    def fetchall(self):
        return list(self._many)

    def fetchone(self):
        return self._one

    def commit(self):
        pass

    def close(self):
        pass


if __name__ == '__main__':
    unittest.main()
