"""Thinking-chain skeleton. Models are fakes. No production database."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gojo_backend'))

from tests import test_memory_candidate_source as source_fixture
import candidate_producer
import memory_search
import raw_events
from memory_authority import project_canonical_memory
from role_view import PROJECTION_VERSION, render_role_view
from structured_output import invoke_structured_llm
import thinking_chain
import thinking_fast
import thinking_inspect


def _loads(value):
    if isinstance(value, str):
        return json.loads(value)
    return value


class ThinkingUnitTests(unittest.TestCase):
    def test_disabled_worker_does_not_look_or_call(self):
        model = Mock(side_effect=AssertionError('model'))
        with patch('db.get_conn', side_effect=AssertionError('db')):
            self.assertEqual(
                thinking_fast.run_worker_once(enabled=False, create_chat_fn=model), [])
        model.assert_not_called()

    def test_unknown_prompt_version_does_not_call_the_model(self):
        model = Mock(side_effect=AssertionError('model'))
        with patch('config.THINKING_PROMPT_VERSION', 'missing_v9'), \
                patch('db.get_conn', side_effect=AssertionError('db')), \
                self.assertLogs('thinking_fast', level='ERROR') as logs:
            self.assertEqual(
                thinking_fast.run_worker_once(enabled=True, create_chat_fn=model), [])
        model.assert_not_called()
        self.assertTrue(any('missing_v9' in line for line in logs.output))

    def test_prompt_templates_name_no_character(self):
        from characters_data._loader import load_core
        root = Path(thinking_chain.__file__).resolve().parent / 'characters_data'
        banned = set()
        for core_path in root.glob('*/core.py'):
            character_id = core_path.parent.name
            banned.add(character_id)
            core = load_core(character_id) or {}
            for key in ('name', 'name_en'):
                value = core.get(key)
                if isinstance(value, str) and value.strip():
                    banned.add(value.strip())
        self.assertTrue(banned)
        for version, text in thinking_chain.PROMPTS.items():
            with self.subTest(version=version):
                for token in banned:
                    self.assertNotIn(token, text)

    def test_only_thinking_modules_name_the_tables(self):
        root = Path(__file__).resolve().parents[1] / 'gojo_backend'
        allowed = {'thinking_chain.py', 'thinking_fast.py', 'thinking_inspect.py'}
        hits = []
        for path in root.glob('*.py'):
            text = path.read_text(encoding='utf-8')
            if 'thinking_understandings' in text or 'character_views' in text:
                hits.append(path.name)
        self.assertTrue(set(hits) <= allowed)
        self.assertIn('thinking_fast.py', hits)

    def test_search_without_ids_keeps_the_old_tuple(self):
        moment = datetime(2026, 1, 1, tzinfo=timezone.utc)
        row = (1, '她喜欢咖啡', moment, '喜好', 'c', None, None)

        class Store:
            def __init__(self):
                self.statements = []

            def cursor(self):
                return self

            def execute(self, sql, params=None):
                self.statements.append(sql)

            def fetchall(self):
                return [row]

            def close(self):
                pass

        store = Store()
        where = memory_search._long_memory_where_sql()
        with patch.object(memory_search, '_VECTOR_READY', True), \
                patch.object(memory_search, 'embed', return_value=[0.2, 0.1]), \
                patch.object(memory_search, '_to_vec', return_value=[0.2, 0.1]), \
                patch.object(memory_search, '_top_k_ids', return_value=[(1, 0.5)]), \
                patch.object(memory_search, 'get_conn', return_value=store):
            plain = memory_search.search_long_memory('u', 'c', 'shared', '咖啡')
            named = memory_search.search_long_memory(
                'u', 'c', 'shared', '咖啡', with_ids=True)
            latest = memory_search.latest_long_memory_with_ids('u', 'c')
        self.assertEqual(plain, [('她喜欢咖啡', moment, '喜好')])
        self.assertEqual(named, [(1, '她喜欢咖啡', moment, '喜好')])
        self.assertEqual(latest, [(1, '她喜欢咖啡', moment, '喜好')])
        self.assertGreaterEqual(len(store.statements), 2)
        for sql in store.statements:
            self.assertIn(where, sql)

    def test_producer_loop_finishes_when_thinking_raises(self):
        calls = []
        logs = []

        def worker():
            calls.append('candidate')
            return [{'status': 'idle'}]

        def thinking():
            calls.append('thinking')
            raise RuntimeError('boom')

        with patch.object(candidate_producer, '_STOP') as stop, \
                patch.object(candidate_producer, 'run_worker_once', side_effect=worker), \
                patch.object(candidate_producer, '_emit', side_effect=logs.append), \
                patch('thinking_fast.run_worker_once', side_effect=thinking):
            stop.wait.side_effect = [False, True]
            candidate_producer._producer_loop()
        self.assertEqual(calls, ['candidate', 'thinking'])
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['stage'], 'thinking_fast')
        self.assertEqual(logs[0]['status'], 'failed')
        self.assertEqual(logs[0]['reason'], 'RuntimeError')
        self.assertEqual(logs[0]['kind'], 'worker_failure')


@unittest.skipUnless(os.getenv('COGNITIVE_TEST_PGLITE'), 'requires disposable local PGlite')
class ThinkingDatabaseTests(unittest.TestCase):
    setUpClass = classmethod(source_fixture.CandidateSourceTests.setUpClass.__func__)
    tearDownClass = classmethod(source_fixture.CandidateSourceTests.tearDownClass.__func__)
    tearDown = source_fixture.CandidateSourceTests.tearDown
    sql = source_fixture.CandidateSourceTests.sql

    def setUp(self):
        source_fixture.CandidateSourceTests.setUp(self)
        self.stack.enter_context(patch(
            'structured_output.invoke_structured_llm', wraps=invoke_structured_llm))
        self.stack.enter_context(patch('memory_search.get_conn', return_value=self.database))
        self.episodes = self.stack.enter_context(patch(
            'episodic_index.recall_episodes', return_value=[]))
        processing = [ddl for ddl in raw_events.RAW_EVENT_DDL
                      if 'CREATE TABLE IF NOT EXISTS event_processing_state' in ddl]
        self.assertEqual(len(processing), 1)
        self.sql(processing[0])
        self.sql('ALTER TABLE chat_log ADD COLUMN reply_to_event_id TEXT')
        self.sql('''CREATE TABLE chat_log_tombstone (
            user_id TEXT NOT NULL, chat_id TEXT NOT NULL, client_msg_id TEXT NOT NULL,
            deleted_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, chat_id, client_msg_id))''')
        thinking_chain.init_thinking_tables(self.database)
        self.database.commit()
        self.now = datetime(2026, 11, 20, 5, 0, tzinfo=timezone.utc)
        self.logs = []

    def steps(self, text='她说了今天的事'):
        return [{'step': name, 'text': text} for name in (
            '看到什么', '想起了什么', '对我意味着什么', '我怎么回应的', '看法有没有变')]

    def item(self, sources, **extra):
        value = {'sources': list(sources), 'steps': self.steps()}
        value.update(extra)
        return value

    def add_line(self, event_id, role, text, when, *, kind='text', extra='{}',
                 subtitle='', status='active', user='u', character='c'):
        self.sql('''INSERT INTO chat_log
            (user_id,chat_id,event_id,role,text,kind,extra,subtitle,created_at,status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
            (user, character, event_id, role, text, kind, extra, subtitle, when, status))

    def add_receipt(self, event_ids, batch_ref, when, *, user='u', character='c'):
        from config import CANDIDATE_PRODUCER_SHADOW
        processor = candidate_producer._processor(user, character)
        version = candidate_producer._receipt_version(CANDIDATE_PRODUCER_SHADOW)
        for event_id in event_ids:
            self.sql('''INSERT INTO event_processing_state
                (event_id,processor_type,processor_version,status,result_ref,consumed_at)
                VALUES (%s,%s,%s,'succeeded',%s,%s)''',
                (event_id, processor, version, batch_ref, when))

    def seed_batch(self, event_ids, texts, *, batch_ref='batch-1', user='u', character='c',
                   when=None):
        moment = when or self.now
        for index, (event_id, text) in enumerate(zip(event_ids, texts)):
            self.add_line(event_id, 'user', text, moment + timedelta(minutes=index),
                          user=user, character=character)
        self.add_receipt(event_ids, batch_ref, moment, user=user, character=character)
        self.database.commit()

    def model(self, payload, stop_reason='end_turn'):
        if not isinstance(payload, str):
            payload = json.dumps(payload, ensure_ascii=False)
        return Mock(return_value=(payload, {'stop_reason': stop_reason}))

    def run_fast(self, model, *, now=None, user='u', character='c', enabled=True):
        return thinking_fast.run_pair(
            user, character, now=now or self.now, create_chat_fn=model,
            conn=self.database, emit=self.logs.append, enabled=enabled)

    def understandings(self, user='u', character='c'):
        cur = self.database.cursor()
        cur.execute('''SELECT item_index, source_event_ids, context_refs, steps,
                cited_experiences, cited_views, view_proposals, prompt_version, model, status
            FROM thinking_understandings
            WHERE user_id=%s AND character_id=%s
            ORDER BY item_index''', (user, character))
        rows = []
        for row in cur.fetchall():
            rows.append({
                'item_index': row[0],
                'source_event_ids': _loads(row[1]),
                'context_refs': _loads(row[2]),
                'steps': _loads(row[3]),
                'cited_experiences': _loads(row[4]),
                'cited_views': _loads(row[5]),
                'view_proposals': _loads(row[6]),
                'prompt_version': row[7],
                'model': row[8],
                'status': row[9],
            })
        return rows

    def audits(self, user='u', character='c'):
        cur = self.database.cursor()
        cur.execute('''SELECT kind, reason, detail FROM thinking_audit_log
            WHERE user_id=%s AND character_id=%s ORDER BY id''', (user, character))
        return [(kind, reason, _loads(detail)) for kind, reason, detail in cur.fetchall()]

    def batch_status(self, batch_ref, user='u', character='c'):
        return self.sql('''SELECT status, last_error FROM event_processing_state
            WHERE event_id=%s AND processor_type=%s AND processor_version=%s''',
            ('thinking-fast:batch:' + batch_ref, thinking_fast._processor(user, character),
             thinking_fast.MODULE_VERSION))

    def candidate_rows(self, user='u', character='c'):
        from config import CANDIDATE_PRODUCER_SHADOW
        return self.sql('''SELECT event_id, status, result_ref, last_error
            FROM event_processing_state
            WHERE processor_type=%s AND processor_version=%s
            ORDER BY event_id''',
            (candidate_producer._processor(user, character),
             candidate_producer._receipt_version(CANDIDATE_PRODUCER_SHADOW)))

    def test_one_batch_maps_labels_to_real_ids(self):
        self.seed_batch(['m1', 'm2'], ['第一句', '第二句'])
        seen = {}

        def chat(**kwargs):
            seen['open'] = self.database.in_transaction
            seen['system'] = kwargs.get('system')
            return (json.dumps([self.item(['R1']), self.item(['R2'])], ensure_ascii=False),
                    {'stop_reason': 'end_turn'})

        result = thinking_fast.run_worker_once(
            enabled=True, now=self.now, create_chat_fn=chat, emit=self.logs.append)
        self.assertEqual([row['status'] for row in result], ['succeeded'])
        self.assertFalse(seen['open'])
        self.assertIn('你是c。', seen['system'])
        rows = self.understandings()
        self.assertEqual([row['item_index'] for row in rows], [0, 1])
        self.assertEqual(rows[0]['source_event_ids'], ['m1'])
        self.assertEqual(rows[1]['source_event_ids'], ['m2'])
        self.assertEqual(rows[0]['prompt_version'], 'neutral_v0')
        from config import MODEL_MAIN
        self.assertEqual(rows[0]['model'], MODEL_MAIN)
        self.assertEqual(self.batch_status('batch-1'), [('succeeded', None)])
        self.assertEqual(result[0]['items_produced'], 2)
        self.assertEqual(result[0]['items_accepted'], 2)
        snapshot = thinking_inspect.fetch_thinking_snapshot(
            'u', 'c', conn=self.database, now=self.now)
        self.assertEqual(snapshot['pending_batches'], 0)
        self.assertEqual(snapshot['views'], [])
        self.assertEqual(len(snapshot['understandings']), 2)
        self.assertEqual(snapshot['understandings'][1]['sources_text'], ['第一句'])
        self.assertTrue(snapshot['understandings'][0]['sources_active'])

    def test_bad_source_drops_only_that_item(self):
        self.add_line('earlier', 'user', '之前的话', self.now - timedelta(hours=1))
        self.seed_batch(['m1', 'm2'], ['第一句', '第二句'])
        payload = [self.item(['R2']), self.item(['R1']), self.item(['R3'])]
        result = self.run_fast(self.model(payload))
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['items_accepted'], 2)
        self.assertEqual(result['reasons'], {'source_not_in_batch': 1})
        rows = self.understandings()
        self.assertEqual([row['item_index'] for row in rows], [0, 2])
        self.assertEqual(rows[0]['source_event_ids'], ['m1'])
        self.assertEqual(rows[1]['source_event_ids'], ['m2'])
        self.assertEqual(self.audits()[0][0:2], ('item_rejected', 'source_not_in_batch'))

    def test_context_outside_the_window_or_inside_the_batch_is_rejected(self):
        for user, context, reason in (
                ('outside', ['R99'], 'context_not_in_window'),
                ('inside', ['R2'], 'context_not_in_window')):
            with self.subTest(user=user):
                self.add_line('earlier-' + user, 'user', '之前', self.now - timedelta(hours=1),
                              user=user)
                self.seed_batch(['m-' + user], ['这句'], batch_ref='b-' + user, user=user)
                result = self.run_fast(self.model([self.item(['R2'], context=context)]), user=user)
                self.assertEqual(result['items_accepted'], 0)
                self.assertEqual(self.audits(user)[0][0:2], ('item_rejected', reason))
                self.assertEqual(self.understandings(user), [])

    def test_unknown_experience_label_is_rejected(self):
        self.seed_batch(['m1'], ['第一句'])
        with patch('memory_search.search_long_memory',
                   return_value=[(12, '她喜欢雨', self.now, '其他')]):
            result = self.run_fast(self.model([self.item(['R1'], experiences=['L999'])]))
        self.assertEqual(result['items_accepted'], 0)
        self.assertEqual(self.audits()[0][0:2], ('item_rejected', 'experience_not_given'))

    def test_fourth_item_is_too_many(self):
        self.seed_batch(['m1'], ['第一句'])
        payload = [self.item(['R1']) for _ in range(4)]
        result = self.run_fast(self.model(payload))
        self.assertEqual(result['items_accepted'], 3)
        self.assertEqual(result['reasons'], {'too_many_items': 1})
        self.assertEqual([row['item_index'] for row in self.understandings()], [0, 1, 2])
        self.assertEqual(self.audits()[0][0:2], ('item_rejected', 'too_many_items'))

    def test_empty_or_oversized_steps_are_rejected(self):
        self.seed_batch(['m1'], ['第一句'])
        long_step = [{'step': '看到什么', 'text': '字' * 301}]
        payload = [self.item(['R1'], steps=[]), self.item(['R1'], steps=long_step)]
        result = self.run_fast(self.model(payload))
        self.assertEqual(result['items_accepted'], 0)
        self.assertEqual([row[1] for row in self.audits()], ['invalid_steps', 'invalid_steps'])

    def test_batch_failure_delays_and_leaves_candidate_rows(self):
        cases = (
            ('explode', Mock(side_effect=RuntimeError('boom')), 'RuntimeError'),
            ('garbage', self.model('not json'), 'invalid_json'),
            ('cut', self.model('[]', stop_reason='max_tokens'), 'truncated_response'),
        )
        for user, model, reason in cases:
            with self.subTest(reason=reason):
                self.seed_batch(['m-' + user], ['一句'], batch_ref='b-' + user, user=user)
                before = self.candidate_rows(user)
                result = self.run_fast(model, user=user)
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['reasons'], {reason: 1})
                self.assertEqual(self.understandings(user), [])
                self.assertEqual(self.audits(user)[0][0:2], ('batch_failed', reason))
                state = thinking_fast._load_control(
                    self.database.cursor(), thinking_fast._control_id(user))
                self.assertEqual(state['fail_count'], 1)
                self.assertEqual(
                    datetime.fromisoformat(state['retry_at']),
                    self.now + timedelta(seconds=30))
                self.assertEqual(self.candidate_rows(user), before)
                self.assertEqual(self.sql('SELECT count(*) FROM cognitive_events')[0][0], 0)

    def test_three_failures_skip_the_batch_and_the_next_call_moves_on(self):
        self.seed_batch(['a1'], ['第一批'], batch_ref='old', when=self.now - timedelta(minutes=5))
        self.seed_batch(['b1'], ['第二批'], batch_ref='new', when=self.now)
        calls = {'n': 0}

        def chat(**kwargs):
            calls['n'] += 1
            if calls['n'] < 4:
                return 'not json', {'stop_reason': 'end_turn'}
            import re
            labels = re.findall(r'\[(R\d+)\]【本批】她', kwargs['messages'][0]['content'])
            payload = [self.item(labels[:1])] if labels else []
            return json.dumps(payload, ensure_ascii=False), {'stop_reason': 'end_turn'}

        moments = (self.now, self.now + timedelta(seconds=30), self.now + timedelta(seconds=90))
        for moment in moments:
            result = self.run_fast(chat, now=moment)
            self.assertEqual(result['status'], 'failed')
        self.assertEqual(self.batch_status('old'), [('skipped', 'invalid_json')])
        state = thinking_fast._load_control(self.database.cursor(), thinking_fast._control_id('u'))
        self.assertEqual(state.get('fail_count', 0), 0)
        moved = self.run_fast(chat, now=self.now + timedelta(seconds=91))
        self.assertEqual(moved['status'], 'succeeded')
        self.assertEqual(moved['batch_ref'], 'new')
        self.assertEqual(self.batch_status('new')[0][0], 'succeeded')
        self.assertGreaterEqual(moved['items_accepted'], 1)

    def test_running_the_same_batch_twice_keeps_one_copy(self):
        self.seed_batch(['m1', 'm2'], ['第一句', '第二句'])
        model = self.model([self.item(['R1']), self.item(['R2'])])
        first = self.run_fast(model)
        second = self.run_fast(model)
        self.assertEqual(first['items_accepted'], 2)
        self.assertEqual(second['status'], 'idle')
        self.assertEqual(len(self.understandings()), 2)
        self.assertEqual(model.call_count, 1)

    def test_daily_limit_does_not_call_the_model(self):
        from config import THINKING_FAST_DAILY_LIMIT
        self.seed_batch(['m1'], ['第一句'])
        day = self.now.astimezone(candidate_producer.SGT).date().isoformat()
        thinking_fast._save_control(
            self.database.cursor(), thinking_fast._BUDGET_ID,
            {'day': day, 'batches': THINKING_FAST_DAILY_LIMIT})
        self.database.commit()
        model = self.model([self.item(['R1'])])
        result = self.run_fast(model)
        self.assertEqual(result['status'], 'daily_limit')
        model.assert_not_called()
        self.assertEqual(self.understandings(), [])

    def test_deleted_sources_skip_without_a_model_call(self):
        self.seed_batch(['m1'], ['第一句'])
        self.sql("UPDATE chat_log SET status='deleted' WHERE event_id='m1'")
        self.database.commit()
        model = self.model([self.item(['R1'])])
        result = self.run_fast(model)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(result['reasons'], {'sources_inactive': 1})
        self.assertEqual(self.batch_status('batch-1'), [('skipped', 'sources_inactive')])
        model.assert_not_called()
        state = thinking_fast._load_control(self.database.cursor(), thinking_fast._control_id('u'))
        self.assertEqual(state.get('fail_count', 0), 0)

    def test_prompt_contains_the_window_and_not_old_understandings(self):
        self.sql('''INSERT INTO thinking_understandings
            (user_id,character_id,batch_ref,item_index,source_event_ids,steps,prompt_version)
            VALUES ('u','c','old',0,'[]'::jsonb,%s::jsonb,'neutral_v0')''',
            (json.dumps([{'step': '看到什么', 'text': '旧理解不该出现XYZ'}], ensure_ascii=False),))
        self.add_line('too-old', 'user', '太早的那句', self.now - timedelta(hours=7))
        for index in range(40):
            self.add_line(
                f'pre-{index}', 'user', f'前文{index}',
                self.now - timedelta(hours=5) + timedelta(minutes=index))
        self.add_line('u1', 'user', '本批她的第一句', self.now)
        self.add_line('mid', 'gojo', 'あいだ', self.now + timedelta(minutes=1), subtitle='夹在中间')
        self.add_line('u2', 'user', '', self.now + timedelta(minutes=2), kind='sticker',
                      extra=json.dumps({'sticker_desc': '猫猫'}, ensure_ascii=False))
        self.add_line('reply', 'gojo', 'わかった', self.now + timedelta(minutes=3), subtitle='知道了')
        self.add_line('later', 'user', '下一条用户', self.now + timedelta(minutes=4))
        self.add_receipt(['u1', 'u2'], 'window', self.now)
        self.database.commit()
        seen = {}

        def chat(**kwargs):
            seen['system'] = kwargs.get('system') or ''
            seen['user'] = kwargs['messages'][0]['content']
            return '[]', {'stop_reason': 'end_turn'}

        result = self.run_fast(chat)
        self.assertEqual(result['status'], 'succeeded')
        text = seen['system'] + '\n' + seen['user']
        self.assertNotIn('旧理解不该出现XYZ', text)
        self.assertNotIn('太早的那句', text)
        self.assertNotIn('前文0', text)
        self.assertNotIn('前文9', text)
        self.assertIn('前文10', text)
        self.assertIn('前文39', text)
        self.assertIn('【本批】她', seen['user'])
        self.assertIn('本批她的第一句', seen['user'])
        self.assertIn('【本批】我', seen['user'])
        self.assertIn('（中文：夹在中间）', seen['user'])
        self.assertIn('【表情】猫猫', seen['user'])
        self.assertIn('【回复】', seen['user'])
        self.assertIn('（中文：知道了）', seen['user'])
        self.assertNotIn('下一条用户', seen['user'])
        self.assertIn('【你现在的看法】\n（暂无）', seen['user'])
        before, _rest = seen['user'].split('【这次的对话】', 1)
        before_lines = [line for line in before.splitlines() if line.startswith('[R')]
        self.assertLessEqual(len(before_lines), 30)
        self.assertGreater(len(before_lines), 0)
        self.assertFalse(any('【本批】' in line for line in before_lines))

    def test_a_finished_batch_does_not_touch_authority_tables(self):
        from candidate_audit import init_candidate_audit_table
        init_candidate_audit_table(self.database)
        self.database.commit()
        tables = (
            'long_memory', 'bond_memory', 'cognitive_beliefs', 'cognitive_hypotheses',
            'cognitive_sticky_notes', 'candidate_audit_log',
        )
        before = {table: self.sql(f'SELECT count(*) FROM {table}')[0][0] for table in tables}
        self.seed_batch(['m1'], ['第一句'])
        result = self.run_fast(self.model([self.item(['R1'])]))
        self.assertEqual(result['status'], 'succeeded')
        after = {table: self.sql(f'SELECT count(*) FROM {table}')[0][0] for table in tables}
        self.assertEqual(after, before)

    def test_vector_miss_uses_only_authoritative_memory(self):
        claim = {
            'subject': 'user:u', 'predicate': 'reported_fact', 'object': '交易结果',
            'time_scope': 'unspecified', 'source': 'canonical_user_turn',
            'value': '亏了69元', 'text': '今天我亏了69元',
        }
        digest = hashlib.sha256(json.dumps(
            {key: claim[key] for key in ('subject', 'predicate', 'object', 'time_scope', 'source')},
            sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest()[:24]
        claim['question_key'] = 'report.' + digest
        self.add_line('fact-src', 'user', '今天我亏了69元，想和你聊一聊。', self.now)
        self.sql('''INSERT INTO cognitive_events
            (user_id,character_id,source_event_type,source_event_id,source,occurred_at,payload)
            VALUES ('u','c','canonical_user_turn','fact-src','chat',%s,%s::jsonb)''',
            (self.now, json.dumps({'content': '今天我亏了69元，想和你聊一聊。', 'claim': claim},
                                  ensure_ascii=False)))
        event_id = self.sql(
            "SELECT id FROM cognitive_events WHERE source_event_id='fact-src'")[0][0]
        semantic = {
            'source_event_id': 'fact-src', 'source_scope': 'c', 'observer_ref': 'c',
            'subject_ref': 'user:u', 'predicate': 'reported_fact', 'object': '交易结果',
            'value': '亏了69元', 'time_scope': 'unspecified', 'evidence_text': '今天我亏了69元',
        }
        content = render_role_view('', observer_id='c', source_character_id='c', semantic=semantic)
        belief_key = claim['question_key'] + f'.e{event_id}'
        decision = {
            'status': 'applied', 'action': 'reported', 'belief_key': belief_key,
            'memory_table': 'long_memory', 'memory_content': content,
            'projection_version': PROJECTION_VERSION, 'semantic_payload': semantic,
        }
        adjudication = {'status': 'applied', 'operations': {'main': decision}}
        self.sql('''UPDATE cognitive_events SET payload=%s::jsonb, adjudication=%s::jsonb
            WHERE id=%s''', (
                json.dumps({'content': '今天我亏了69元，想和你聊一聊。', 'claim': claim},
                           ensure_ascii=False),
                json.dumps(adjudication, ensure_ascii=False), event_id))
        self.sql('''INSERT INTO cognitive_beliefs
            (user_id,character_id,belief_key,statement,confidence,status,belief_type,metadata)
            VALUES ('u','c',%s,%s,0.8,'active','user_model',%s::jsonb)''',
            (belief_key, content, json.dumps({
                'review_status': 'stable', 'semantic_payload': semantic,
                'projection_version': PROJECTION_VERSION}, ensure_ascii=False)))
        project_canonical_memory(
            self.database.cursor(), user_id='u', character_id='c', event_id=event_id,
            source_id='fact-src', content='ignored', claim=claim, belief_key=belief_key,
            occurred_at=self.now)
        self.sql('''INSERT INTO long_memory (user_id,character_id,content,category,timestamp)
            VALUES ('u','c','非权威火星','其他',%s)''', (self.now,))
        self.database.commit()
        rows = memory_search.latest_long_memory_with_ids('u', 'c')
        self.assertTrue(rows)
        self.assertTrue(all('非权威火星' not in row[1] for row in rows))
        self.seed_batch(['talk'], ['今天聊点别的'])
        seen = {}

        def chat(**kwargs):
            seen['user'] = kwargs['messages'][0]['content']
            return '[]', {'stop_reason': 'end_turn'}

        self.assertIsNone(memory_search.search_long_memory('u', 'c', 'shared', '今天聊点别的'))
        self.run_fast(chat)
        self.assertIn(f'[L{rows[0][0]}]', seen['user'])
        self.assertIn(rows[0][1], seen['user'])
        self.assertNotIn('非权威火星', seen['user'])

    def test_previous_turn_window_stays_capped_and_shaped(self):
        ask = self.now - timedelta(minutes=2)
        reply = self.now - timedelta(minutes=1)
        self.add_line('ask', 'user', '那你该回我什么？', ask)
        self.add_line('chat_reply:ask', 'gojo', '🦖', reply)
        self.add_line('confirm', 'user', '对，我们约定好了。', self.now)
        self.database.commit()
        events = raw_events.get_previous_active_turn_events('u', 'c', 'confirm', conn=self.database)
        self.assertEqual(
            [(event['event_id'], event['role'], event['content']) for event in events],
            [('ask', 'user', '那你该回我什么？'), ('chat_reply:ask', 'assistant', '🦖')])
        anchor = self.now + timedelta(hours=2)
        for index in range(8):
            self.add_line(f'old-{index}', 'user', f'旧{index}', anchor - timedelta(minutes=8 - index))
        self.add_line('anchor', 'user', '锚点', anchor)
        self.database.commit()
        capped = raw_events.get_previous_active_turn_events(
            'u', 'c', 'anchor', n=99, hours=99, conn=self.database)
        self.assertEqual(len(capped), 6)
        self.assertEqual([event['event_id'] for event in capped], [f'old-{index}' for index in range(2, 8)])

    def test_audit_detail_accepts_a_datetime(self):
        moment = datetime(2026, 11, 20, 5, 6, tzinfo=timezone.utc)
        thinking_fast._insert_audit(
            self.database.cursor(), user_id='u', character_id='c', batch_ref='b',
            kind='item_rejected', reason='invalid_steps',
            detail={'item': {'at': moment}, 'when': moment}, prompt_version='neutral_v0')
        self.database.commit()
        _kind, _reason, detail = self.audits()[0]
        self.assertEqual(detail['when'], moment.isoformat())
        self.assertEqual(detail['item']['at'], moment.isoformat())

    def test_invalid_view_proposal_is_rejected(self):
        self.seed_batch(['m1'], ['第一句'])
        payload = [self.item(['R1'], view_proposals=[
            {'view': None, 'direction': '飞走', 'note': '不行'},
        ])]
        result = self.run_fast(self.model(payload))
        self.assertEqual(result['items_accepted'], 0)
        self.assertEqual(self.audits()[0][1], 'invalid_view_proposal')


if __name__ == '__main__':
    unittest.main()
