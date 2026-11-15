# -*- coding: utf-8 -*-
"""A new batching message during flush discards the first reply and answers once."""
import copy
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / 'gojo_backend'
TESTS = ROOT / 'tests'
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import db_schedule  # noqa: E402
import delayed_reply  # noqa: E402
import test_schedule_reply_state as sched_tests  # noqa: E402
from db_read_receipt import source_event_ids_from_claim  # noqa: E402

FakeCursor = sched_tests.FakeCursor
FakeConn = sched_tests.FakeConn
PhoneCheckStore = sched_tests.PhoneCheckStore

NOW = datetime(2026, 11, 8, 10, 0, tzinfo=timezone.utc)
FREE_WORLD = {
    'activity': None,
    'event': {},
    'phase': {},
    'availability': {'reply_state': 'free', 'can_reply': True},
}
STABLE_FIELDS = ('pending_text', 'pending_count', 'event_meta', 'successor_of_id')


class InterruptCursor(FakeCursor):
    """Phone-check fake plus the two statements this change adds.

    A successor insert keeps the previous row. The shared fake stores one
    batching key, which would otherwise drop the claimed row.
    """

    def execute(self, sql, params=None):
        compact = ' '.join(sql.split())
        params = tuple(params or ())
        if compact.startswith('INSERT INTO char_phone_check'):
            user_id, character_id, _schedule_id, sched_date, start_time, end_time = params[:6]
            inbox = params[23] if len(params) > 23 else ''
            key = (user_id, character_id, sched_date, start_time, end_time, inbox or '')
            previous = self.store.rows.get(key)
            super().execute(sql, params)
            current = self.store.rows.get(key)
            if previous is not None and previous is not current:
                self.store.extra_rows.append(previous)
            return
        if 'successor_of_id=%s' in compact:
            self.store.sql.append((compact, params))
            self._one = None
            self._many = []
            self.rowcount = 0
            oid, user_id, character_id = params
            found = []
            for row in list(self.store.rows.values()) + self.store.extra_rows:
                if str(row.get('successor_of_id')) != str(oid):
                    continue
                if row.get('user_id') != user_id or row.get('character_id') != character_id:
                    continue
                if row.get('inbox_reason') != 'batching' or row.get('resolved_at'):
                    continue
                if (row.get('pending_count') or 0) <= 0:
                    continue
                if (row.get('check_state') or 'pending') not in ('pending', 'deferred'):
                    continue
                found.append(row['id'])
            found.sort()
            self._one = (found[0],) if found else None
            return
        if 'phone_check:release_superseded' in compact:
            self.store.sql.append((compact, params))
            self._one = None
            self._many = []
            self.rowcount = 0
            now, oid, token = params
            row = self._find_by_id(oid)
            if (row and row.get('claim_token') == token
                    and row.get('check_state') == 'processing'):
                row['check_state'] = 'pending'
                row['next_phone_check_at'] = now
                row['claimed_at'] = None
                row['claim_token'] = None
                row['claim_owner'] = None
                row['claim_expires_at'] = None
                self.rowcount = 1
            return
        super().execute(sql, params)


class InterruptConn(FakeConn):
    def cursor(self):
        return InterruptCursor(self.store)


def _stable(row):
    return {name: copy.deepcopy(row.get(name)) for name in STABLE_FIELDS}


def _all_rows(store):
    return list(store.rows.values()) + list(store.extra_rows)


class InterruptOnNewMessageTests(unittest.TestCase):
    def setUp(self):
        delayed_reply._generation_calls.clear()
        delayed_reply._pause_until = 0.0

    def _arrive(self, text, source_id, when=None):
        return db_schedule.decide_phone_check(
            'gojo', 'u', when or NOW, None,
            source_event_id=source_id, pending_text=text,
            event_meta={'kind': 'text', 'source_event_id': source_id, 'text': text},
            batching=True)

    def _patches(self, store, model, commits, bundles, schedule_writes, promise_writes):
        texts = {
            'a-1': '没有啦，小笼包是我在椅子睡觉的时候被饿醒了',
            'b-1': '然后才叫的外卖',
            'c-1': '顺便再要一碗汤',
            'plain': '普通的一条',
        }

        def active(_user_id, _character_id, source_ids, **_kwargs):
            return [{
                'event_id': source_id,
                'role': 'user',
                'content': texts.get(source_id, source_id),
                'timestamp': 't',
            } for source_id in source_ids]

        def persist(bundle):
            bundles.append({
                'id': bundle.get('id'),
                'pending_count': bundle.get('pending_count'),
                'pending_text': bundle.get('pending_text'),
                'event_meta': bundle.get('event_meta'),
                'first_source_event_id': bundle.get('first_source_event_id'),
                'last_source_event_id': bundle.get('last_source_event_id'),
            })

        def commit_message(*args, **kwargs):
            commits.append({
                'jp': args[1] if len(args) > 1 else '',
                'event_id': kwargs.get('event_id'),
            })

        def commit_schedule(_character_id, _user_id, result, source_event_id=''):
            schedule_writes.append(result)
            return {'ok': True, 'noop': True}

        def apply_promise(ctx):
            promise_writes.append(ctx)

        return [
            patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)),
            patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD),
            patch.object(delayed_reply, 'assistant_already_committed', return_value=False),
            patch.object(delayed_reply, '_persist_pending_user_messages', side_effect=persist),
            patch('characters.get_character', return_value={'voice_id': 'v', 'name': '五条'}),
            patch('temporal_awareness.get_temporal_snapshot', return_value={}),
            patch('temporal_awareness.record_turn'),
            patch('prompt.build_system_blocks', return_value=[]),
            patch('schedule_transition.validate_generated_schedule_reply',
                  return_value=(None, {})),
            patch('schedule_transition.commit_generated_schedule_intent',
                  side_effect=commit_schedule),
            patch('tts.tts_to_b64', return_value=''),
            patch('user_memory.get_short_memory', return_value=[]),
            patch('user_memory.commit_visible_assistant_message', side_effect=commit_message),
            patch('user_memory.update_chat_days', return_value=1),
            patch('proactive_msg.add_proactive_msg'),
            patch('behavior_evidence.record_reply_cycle'),
            patch('push_notify.push_to_user'),
            patch('generation_effects.apply_proactive_promise', side_effect=apply_promise),
            patch('generation_effects.apply_promise_detector'),
            patch('raw_events.get_active_events_by_ids', side_effect=active),
            patch('route_chat._turn_context', return_value=({}, [])),
            patch('route_chat._history_plus_current', side_effect=lambda messages, _trigger: messages),
            patch('route_chat._generate_or_none', side_effect=model),
            patch('route_chat._finalize_committed',
                  side_effect=lambda result: (
                      None, None) if not result else (
                      '平静', list(result.get('messages') or []))),
            patch('route_chat._commit_offline_state'),
            patch('route_chat._resolve_sticker', return_value=None),
        ]

    def _run(self, store, model, *, generate_fn=None, worker=False, when=None):
        commits = []
        bundles = []
        schedule_writes = []
        promise_writes = []
        actions = []
        real_log = db_schedule.log_phone_check_action

        def spy(action, claimed, **kwargs):
            actions.append(action)
            return real_log(action, claimed, **kwargs)

        started = []
        for item in self._patches(store, model, commits, bundles, schedule_writes, promise_writes):
            item.start()
            started.append(item)
        log_patch = patch.object(db_schedule, 'log_phone_check_action', side_effect=spy)
        log_patch.start()
        started.append(log_patch)
        try:
            if worker:
                result = delayed_reply.process_due_phone_checks(when or NOW)
            else:
                result = delayed_reply.flush_batching_reply(
                    'u', 'gojo', now=when or NOW, generate_fn=generate_fn)
        finally:
            for item in reversed(started):
                item.stop()
        return {
            'result': result,
            'commits': commits,
            'bundles': bundles,
            'schedule_writes': schedule_writes,
            'promise_writes': promise_writes,
            'actions': actions,
        }

    def _reply(self, jp):
        return {'emotion': '平静', 'messages': [{'jp': jp, 'zh': jp}]}

    def test_successor_during_generation_is_answered_once(self):
        store = PhoneCheckStore()
        calls = {'n': 0}

        def model(*_args, **_kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                self._arrive('然后才叫的外卖', 'b-1')
                return self._reply('只回了第一条'), None
            return self._reply('两条一起回'), None

        with patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
            self._arrive('没有啦，小笼包是我在椅子睡觉的时候被饿醒了', 'a-1')
        outcome = self._run(store, model)
        rows = {row['id']: row for row in _all_rows(store)}
        successors = [row for row in rows.values() if row.get('successor_of_id')]
        self.assertEqual(len(successors), 1)
        successor = successors[0]
        anchor = rows[successor['successor_of_id']]
        self.assertEqual(calls['n'], 2)
        self.assertEqual(
            [item['jp'] for item in outcome['commits']],
            ['两条一起回'])
        self.assertNotIn('只回了第一条', [item['jp'] for item in outcome['commits']])
        self.assertGreaterEqual(len(outcome['bundles']), 2)
        merged = outcome['bundles'][-1]
        self.assertEqual(merged['id'], anchor['id'])
        self.assertEqual(merged['pending_count'], 2)
        self.assertIn('没有啦', merged['pending_text'])
        self.assertIn('然后才叫的外卖', merged['pending_text'])
        self.assertEqual(
            source_event_ids_from_claim(merged),
            ['a-1', 'b-1'])
        self.assertTrue(outcome['result']['ok'])
        for message in outcome['result']['messages']:
            self.assertTrue(message['event_id'].startswith(f'delayed_reply:{anchor["id"]}:'))
        self.assertEqual(anchor['check_state'], 'consumed')
        self.assertEqual(successor['check_state'], 'consumed')
        self.assertFalse(anchor.get('fail_count'))
        self.assertFalse(successor.get('fail_count'))
        self.assertIsNone(anchor.get('retry_not_before'))
        self.assertIsNone(successor.get('retry_not_before'))
        self.assertNotIn('generation_failed', outcome['actions'])
        before = {
            'a': _stable(anchor),
            'b': _stable(successor),
        }
        # The four content fields are not part of claim, release, or finish.
        self.assertEqual(before['a']['pending_text'], '没有啦，小笼包是我在椅子睡觉的时候被饿醒了')
        self.assertEqual(before['a']['pending_count'], 1)
        self.assertIsNone(before['a']['successor_of_id'])
        self.assertEqual(before['b']['pending_text'], '然后才叫的外卖')
        self.assertEqual(before['b']['pending_count'], 1)
        self.assertEqual(before['b']['successor_of_id'], anchor['id'])

    def test_only_one_merge_leaves_the_next_successor_pending(self):
        store = PhoneCheckStore()
        calls = {'n': 0}

        def model(*_args, **_kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                self._arrive('然后才叫的外卖', 'b-1')
            else:
                self._arrive('顺便再要一碗汤', 'c-1')
            return self._reply('合并回复'), None

        with patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
            self._arrive('没有啦，小笼包是我在椅子睡觉的时候被饿醒了', 'a-1')
        outcome = self._run(store, model)
        rows = _all_rows(store)
        by_successor = {row.get('successor_of_id'): row for row in rows if row.get('successor_of_id')}
        anchor = next(row for row in rows if row.get('successor_of_id') is None
                      and row['pending_text'].startswith('没有啦'))
        middle = by_successor[anchor['id']]
        tail = by_successor[middle['id']]
        self.assertEqual(calls['n'], 2)
        self.assertTrue(outcome['result']['ok'])
        self.assertEqual([item['jp'] for item in outcome['commits']], ['合并回复'])
        self.assertEqual(tail['check_state'], 'pending')
        self.assertIsNone(tail.get('resolved_at'))
        self.assertEqual(anchor['check_state'], 'consumed')
        self.assertEqual(middle['check_state'], 'consumed')

    def test_plain_flush_matches_the_injected_path_field_for_field(self):
        def build(use_default):
            store = PhoneCheckStore()

            def model(*_args, **_kwargs):
                return self._reply('普通回复'), None

            def generate_fn(bundle, push=True):
                return delayed_reply.generate_delayed_chat_reply(bundle, push=push)

            with patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)), \
                    patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
                self._arrive('普通的一条', 'plain')
            outcome = self._run(
                store, model,
                generate_fn=None if use_default else generate_fn)
            return outcome['result'], _all_rows(store), store.sql

        default_result, default_rows, default_sql = build(True)
        injected_result, injected_rows, _injected_sql = build(False)
        self.assertEqual(default_result, injected_result)
        self.assertEqual(len(default_rows), 1)
        self.assertEqual(len(injected_rows), 1)
        self.assertEqual(default_rows[0]['check_state'], 'consumed')
        self.assertEqual(injected_rows[0]['check_state'], 'consumed')
        self.assertFalse(any('release_superseded' in compact for compact, _params in default_sql))
        self.assertEqual(default_result['messages'][0]['jp'], '普通回复')

    def test_existing_successor_skips_the_doomed_generation(self):
        store = PhoneCheckStore()
        calls = {'n': 0}

        def model(*_args, **_kwargs):
            calls['n'] += 1
            return self._reply('一次合并'), None

        with patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
            self._arrive('没有啦，小笼包是我在椅子睡觉的时候被饿醒了', 'a-1')
            anchor = _all_rows(store)[0]
            anchor['next_phone_check_at'] = NOW
            claimed = db_schedule.evaluate_due_phone_check(anchor['id'], NOW)
            self._arrive('然后才叫的外卖', 'b-1')
            db_schedule.release_superseded_phone_check(
                anchor['id'], claimed['claimed']['claim_token'], NOW)
        outcome = self._run(store, model)
        rows = _all_rows(store)
        self.assertEqual(calls['n'], 1)
        self.assertIn('然后才叫的外卖', outcome['bundles'][-1]['pending_text'])
        self.assertIn('没有啦', outcome['bundles'][-1]['pending_text'])
        self.assertTrue(all(row['check_state'] == 'consumed' for row in rows))

    def test_failed_merge_counts_only_the_anchor(self):
        store = PhoneCheckStore()
        calls = {'n': 0}

        def model(*_args, **_kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                self._arrive('然后才叫的外卖', 'b-1')
                return self._reply('会被丢掉'), None
            return None, None

        with patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
            self._arrive('没有啦，小笼包是我在椅子睡觉的时候被饿醒了', 'a-1')
        outcome = self._run(store, model)
        rows = _all_rows(store)
        anchor = next(row for row in rows if not row.get('successor_of_id'))
        successor = next(row for row in rows if row.get('successor_of_id'))
        self.assertFalse(outcome['result']['ok'])
        self.assertEqual(outcome['result']['reason'], 'generation_failed')
        self.assertEqual(anchor['fail_count'], 1)
        self.assertIsNotNone(anchor.get('retry_not_before'))
        self.assertEqual(anchor['check_state'], 'pending')
        self.assertFalse(successor.get('fail_count'))
        self.assertIsNone(successor.get('retry_not_before'))
        self.assertEqual(successor['check_state'], 'pending')
        self.assertEqual(anchor['pending_text'], '没有啦，小笼包是我在椅子睡觉的时候被饿醒了')
        self.assertEqual(successor['pending_text'], '然后才叫的外卖')
        self.assertEqual(outcome['commits'], [])

    def test_unclaimable_successor_replies_with_the_anchor_only(self):
        store = PhoneCheckStore()
        calls = {'n': 0}
        real_eval = db_schedule.evaluate_due_phone_check

        def model(*_args, **_kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                self._arrive('然后才叫的外卖', 'b-1')
                return self._reply('会被丢掉'), None
            return self._reply('只剩第一条'), None

        def steal_successor(oid, now, conn=None):
            for row in _all_rows(store):
                if (row.get('id') == oid and row.get('successor_of_id')
                        and row.get('check_state') in ('pending', 'deferred')):
                    row['check_state'] = 'processing'
                    row['claim_token'] = 'other-token'
                    row['claim_owner'] = 'other'
                    row['claim_expires_at'] = now + timedelta(hours=1)
            return real_eval(oid, now, conn=conn)

        with patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
            self._arrive('没有啦，小笼包是我在椅子睡觉的时候被饿醒了', 'a-1')
        with patch.object(db_schedule, 'evaluate_due_phone_check', side_effect=steal_successor):
            outcome = self._run(store, model)
        rows = _all_rows(store)
        anchor = next(row for row in rows if not row.get('successor_of_id'))
        successor = next(row for row in rows if row.get('successor_of_id'))
        self.assertEqual(calls['n'], 2)
        self.assertEqual([item['jp'] for item in outcome['commits']], ['只剩第一条'])
        self.assertEqual(anchor['check_state'], 'consumed')
        self.assertEqual(successor['claim_token'], 'other-token')
        self.assertEqual(successor['check_state'], 'processing')
        self.assertEqual(successor['pending_text'], '然后才叫的外卖')
        self.assertEqual(successor['pending_count'], 1)

    def test_worker_still_commits_when_a_successor_arrives(self):
        store = PhoneCheckStore()
        calls = {'n': 0}

        def model(*_args, **_kwargs):
            calls['n'] += 1
            self._arrive('然后才叫的外卖', 'b-1')
            return self._reply('worker照旧发出'), None

        with patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
            self._arrive('没有啦，小笼包是我在椅子睡觉的时候被饿醒了', 'a-1')
            row = _all_rows(store)[0]
            row['next_phone_check_at'] = NOW
        outcome = self._run(store, model, worker=True)
        rows = _all_rows(store)
        self.assertEqual(calls['n'], 1)
        self.assertEqual([item['jp'] for item in outcome['commits']], ['worker照旧发出'])
        self.assertTrue(any(row.get('successor_of_id') for row in rows))
        self.assertTrue(any(
            not row.get('successor_of_id') and row['check_state'] == 'consumed'
            for row in rows))

    def test_discarded_reply_does_not_write_promise_or_schedule(self):
        store = PhoneCheckStore()
        calls = {'n': 0}
        promise = {'trigger_kind': 'once', 'trigger_at': '2026-11-15 18:00', 'context': '找你'}
        intent = {'type': 'insert', 'event': {'title': '吃饭', 'duration_minutes': 30}}

        def model(*_args, **_kwargs):
            calls['n'] += 1
            if calls['n'] == 1:
                self._arrive('然后才叫的外卖', 'b-1')
                return {
                    'emotion': '平静',
                    'messages': [{'jp': '带副作用', 'zh': '带副作用'}],
                    'proactive_promise': promise,
                    'schedule_action_intent': intent,
                }, None
            return self._reply('干净的回复'), None

        with patch.object(db_schedule, 'get_conn', lambda: InterruptConn(store)), \
                patch.object(db_schedule, 'get_current_world_state', return_value=FREE_WORLD):
            self._arrive('没有啦，小笼包是我在椅子睡觉的时候被饿醒了', 'a-1')
        outcome = self._run(store, model)
        self.assertEqual(outcome['promise_writes'], [])
        self.assertEqual(store.schedule_rows, [])
        self.assertEqual(len(outcome['schedule_writes']), 1)
        self.assertNotIn('proactive_promise', outcome['schedule_writes'][0])
        self.assertNotIn('schedule_action_intent', outcome['schedule_writes'][0])
        self.assertEqual([item['jp'] for item in outcome['commits']], ['干净的回复'])
