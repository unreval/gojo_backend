import inspect
import os
import sys
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import raw_events  # noqa: E402
import relationship_engine  # noqa: E402
import relationship_reader  # noqa: E402
from relationship_engine import aggregate_user_flirt_response  # noqa: E402
from relationship_config import FLIRT_RESPONSE_WINDOW_SIZE  # noqa: E402


def _sig(signal_type, actor='user', confidence='high', **attrs):
    return {
        'signal_type': signal_type,
        'actor': actor,
        'confidence': confidence,
        'brief': signal_type,
        'attributes': attrs,
    }


class FakeCursor:
    def __init__(self, fetchall_rows=None):
        self.statements = []
        self.params_list = []
        self.fetchall_rows = fetchall_rows or []
        self.closed = False

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.params_list.append(params)

    def fetchall(self):
        return list(self.fetchall_rows)

    def fetchone(self):
        return None

    def close(self):
        self.closed = True


class FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def _logged_row(signals, source_event_id='evt-1', session_id=None):
    cursor = FakeCursor()
    conn = FakeConn(cursor)
    with patch.object(relationship_engine, 'get_conn', return_value=conn):
        relationship_engine._log_interaction_stats(
            'u', 'gojo', signals, session_id,
            source_event_id=source_event_id,
        )
    sql = cursor.statements[0]
    params = cursor.params_list[0]
    return sql, params, cursor, conn


class AggregateFlirtResponseTests(unittest.TestCase):
    def test_positive_reciprocal_is_accepted(self):
        self.assertEqual(
            aggregate_user_flirt_response([_sig('positive_reciprocal')]),
            'accepted',
        )

    def test_ambiguous_response_is_held(self):
        self.assertEqual(
            aggregate_user_flirt_response([_sig('ambiguous_response')]),
            'held',
        )

    def test_explicit_rejection_is_rejected(self):
        self.assertEqual(
            aggregate_user_flirt_response([_sig('explicit_rejection')]),
            'rejected',
        )

    def test_offensive_content_does_not_set_flirt_response(self):
        self.assertIsNone(aggregate_user_flirt_response([
            _sig('offensive_content', target='character'),
        ]))
        self.assertIsNone(aggregate_user_flirt_response([
            _sig('offensive_content', target='third_party'),
        ]))

    def test_mixed_is_order_independent(self):
        a = [_sig('positive_reciprocal'), _sig('explicit_rejection')]
        b = [_sig('explicit_rejection'), _sig('positive_reciprocal')]
        self.assertEqual(aggregate_user_flirt_response(a), 'mixed')
        self.assertEqual(aggregate_user_flirt_response(b), 'mixed')

    def test_character_actor_does_not_write_user_flirt_response(self):
        self.assertIsNone(aggregate_user_flirt_response([
            _sig('positive_reciprocal', actor='character'),
            _sig('explicit_rejection', actor='character'),
            _sig('ambiguous_response', actor='character'),
            _sig('character_reciprocal', actor='character'),
        ]))

    def test_duplicate_same_class_is_not_mixed(self):
        self.assertEqual(
            aggregate_user_flirt_response([
                _sig('positive_reciprocal'),
                _sig('positive_reciprocal'),
            ]),
            'accepted',
        )


class LogInteractionStatsTests(unittest.TestCase):
    def test_offensive_character_writes_null_flirt_response(self):
        sql, params, _, _ = _logged_row(
            [_sig('offensive_content', target='character')])
        self.assertIsNone(params[8])
        self.assertIsNone(params[4], 'legacy is_reciprocal must stay NULL')
        self.assertNotIn('offensive', sql.lower())

    def test_offensive_third_party_writes_null_flirt_response(self):
        _, params, _, _ = _logged_row(
            [_sig('offensive_content', target='third_party')])
        self.assertIsNone(params[8])
        self.assertIsNone(params[4])

    def test_source_event_id_is_written(self):
        sql, params, _, conn = _logged_row(
            [_sig('positive_reciprocal')], source_event_id='evt-trace')
        self.assertEqual(params[7], 'evt-trace')
        self.assertEqual(params[8], 'accepted')
        self.assertIn('source_event_id', sql)
        self.assertIn('ON CONFLICT', sql)
        self.assertEqual(conn.commits, 1)

    def test_retry_same_source_event_id_uses_on_conflict(self):
        sql, params, _, _ = _logged_row(
            [_sig('explicit_rejection')], source_event_id='evt-retry')
        self.assertIn('ON CONFLICT (user_id, character_id, source_event_id)', sql)
        self.assertIn('WHERE source_event_id IS NOT NULL', sql)
        self.assertEqual(params[7], 'evt-retry')

    def test_null_source_event_id_skips_on_conflict(self):
        sql, params, _, _ = _logged_row(
            [_sig('small_care')], source_event_id=None)
        self.assertNotIn('ON CONFLICT', sql)
        self.assertIsNone(params[7])
        self.assertEqual(params[3], 'support')

    def test_mixed_turn_not_last_write_wins(self):
        _, params, _, _ = _logged_row([
            _sig('positive_reciprocal'),
            _sig('explicit_rejection'),
        ])
        self.assertEqual(params[8], 'mixed')


class ComputeFlirtResponseWindowTests(unittest.TestCase):
    def _run(self, rows):
        cursor = FakeCursor(fetchall_rows=rows)
        conn = FakeConn(cursor)
        with patch.object(relationship_reader, 'get_conn', return_value=conn):
            result = relationship_reader.compute_flirt_response('u', 'gojo')
        return result, cursor

    def test_window_sql_takes_recent_turns_before_counting(self):
        result, cursor = self._run([])
        sql = ' '.join(cursor.statements[0].split())
        self.assertIn('ORDER BY timestamp DESC, id DESC', sql)
        self.assertIn('LIMIT %s', sql)
        self.assertNotIn('flirt_response IS NOT NULL', sql)
        self.assertNotIn('is_reciprocal', sql)
        self.assertEqual(cursor.params_list[0][2], FLIRT_RESPONSE_WINDOW_SIZE)
        self.assertEqual(result['flirt_sample_count'], 0)
        self.assertEqual(result['desc'], '最近没有足够的相关互动')

    def test_window_does_not_backfill_older_rejections(self):
        recent = ([(None,)] * 12) + [('accepted',), ('accepted',), ('rejected',)]
        result, cursor = self._run(recent)
        self.assertEqual(len(cursor.fetchall_rows), 15)
        self.assertEqual(result['accepted'], 2)
        self.assertEqual(result['rejected'], 1)
        self.assertEqual(result['held'], 0)
        self.assertEqual(result['mixed'], 0)
        self.assertEqual(result['flirt_sample_count'], 3)
        self.assertEqual(result['total_recent_turns'], 15)
        self.assertIn('接受 2', result['desc'])
        self.assertIn('拒绝 1', result['desc'])
        self.assertIn('已记录互动', result['desc'])
        self.assertNotIn('偏冷', result['desc'])
        self.assertNotIn('拒绝/回避', result['desc'])

    def test_legacy_is_reciprocal_false_is_not_rejected(self):
        result, _ = self._run([(None,)] * 15)
        self.assertEqual(result['rejected'], 0)
        self.assertEqual(result['flirt_sample_count'], 0)
        self.assertEqual(result['desc'], '最近没有足够的相关互动')

    def test_mixed_appears_in_summary_counts(self):
        result, _ = self._run([
            ('accepted',), (None,), ('rejected',), ('mixed',),
        ] + [(None,)] * 11)
        self.assertEqual(result['accepted'], 1)
        self.assertEqual(result['rejected'], 1)
        self.assertEqual(result['mixed'], 1)
        self.assertEqual(result['flirt_sample_count'], 3)
        self.assertIn('混合 1', result['desc'])


class BuildStateSummaryCopyTests(unittest.TestCase):
    _STATE = {
        'warmth': 10, 'intimacy': 8, 'trust': 12, 'attachment': 9,
        'commitment': 4, 'passion': 0, 'friction': {},
        'pending_passion': 0, 'pending_hypothesis': [],
    }

    def _summary(self, flirt):
        with patch.object(relationship_reader, 'load_state',
                          return_value=self._STATE), \
             patch.object(relationship_reader, 'list_active_stances',
                          return_value=[]), \
             patch.object(relationship_reader, 'compute_tone',
                          return_value='支持型（温和陪伴）'), \
             patch.object(relationship_reader, 'compute_flirt_response',
                          return_value=flirt), \
             patch.object(relationship_reader, 'compute_pursue_withdraw',
                          return_value={'pattern': None}), \
             patch.object(relationship_reader, '_temporal_note',
                          return_value=None), \
             patch.object(relationship_reader, 'derive_label',
                          return_value={'primary': '普通朋友'}):
            return relationship_reader.build_state_summary('u', 'gojo')

    def test_summary_omits_flirt_dimension_when_sample_is_empty(self):
        text = self._summary({
            'desc': '最近没有足够的相关互动',
            'accepted': 0, 'held': 0, 'rejected': 0, 'mixed': 0,
            'flirt_sample_count': 0, 'total_recent_turns': 15,
        })
        self.assertIn('- 对话调性：支持型（温和陪伴）', text)
        self.assertNotIn('关系推进回应', text)
        self.assertNotIn('暧昧回应', text)
        self.assertNotIn('最近没有足够的相关互动', text)
        self.assertNotIn('互惠度', text)
        self.assertNotIn('氛围偏冷', text)
        self.assertNotIn('她多在拒绝/回避', text)

    def test_summary_uses_flirt_evidence_not_reciprocity_verdict(self):
        text = self._summary({
            'desc': '最近 15 轮已记录互动中有 2 次相关回应（接受 1 / 保留 0 / 拒绝 1）',
            'accepted': 1, 'held': 0, 'rejected': 1, 'mixed': 0,
            'flirt_sample_count': 2, 'total_recent_turns': 15,
        })
        self.assertIn('- 对话调性：支持型（温和陪伴）', text)
        self.assertIn(
            '- 关系推进回应：最近 15 轮已记录互动中有 2 次相关回应（接受 1 / 保留 0 / 拒绝 1）',
            text,
        )
        self.assertNotIn('暧昧回应', text)
        self.assertNotIn('互惠度', text)
        self.assertNotIn('氛围偏冷', text)
        self.assertNotIn('氛围偏正向', text)
        self.assertNotIn('她多在拒绝/回避', text)
        self.assertNotIn('她多在配合/延伸', text)

    def test_source_has_no_legacy_reciprocity_copy(self):
        source = inspect.getsource(relationship_reader.build_state_summary)
        reader_src = inspect.getsource(relationship_reader)
        query = inspect.getsource(relationship_reader.compute_flirt_response)
        self.assertNotIn('互惠度', source)
        self.assertNotIn('氛围偏冷', reader_src)
        self.assertNotIn('她多在拒绝/回避', reader_src)
        self.assertNotIn('compute_reciprocity', reader_src)
        self.assertNotIn('SELECT is_reciprocal', query)
        self.assertNotIn('is_reciprocal IS NOT NULL', query)
        self.assertIn('关系推进回应', source)
        self.assertNotIn('暧昧回应', source)


class ProcessTurnStatsOnceTests(unittest.TestCase):
    def test_apply_once_retry_does_not_log_stats_twice(self):
        stats = {'n': 0}

        def log_stats(*_args, **_kwargs):
            stats['n'] += 1

        @contextmanager
        def txn():
            yield object()

        begins = {'n': 0}

        def begin(*_args, **_kwargs):
            begins['n'] += 1
            return begins['n'] == 1

        with patch.object(relationship_engine, 'ensure_state_row'), \
             patch.object(relationship_engine, 'extract_signals',
                          return_value={'signals': [_sig('positive_reciprocal')]}), \
             patch.object(relationship_engine, '_route_signal',
                          return_value={'action': 'reciprocal_non_romantic'}), \
             patch.object(relationship_engine, '_log_interaction_stats',
                          side_effect=log_stats), \
             patch.object(relationship_engine, '_log_temporal_observation'), \
             patch.object(relationship_engine, 'cleanup_hypotheses'), \
             patch.object(relationship_engine, 'check_retreat_boundary_superseded'), \
             patch.object(relationship_engine, 'relationship_txn', txn), \
             patch.object(relationship_engine, 'try_begin_application',
                          side_effect=begin), \
             patch.object(relationship_engine, 'get_application_payload',
                          return_value={'signals': [_sig('positive_reciprocal')]}), \
             patch.object(relationship_engine, '_ingest_v4',
                          return_value={'status': 'duplicate'}), \
             patch.object(raw_events, 'sources_are_active', return_value=True), \
             patch.object(raw_events, 'claim_processor', return_value='claimed'), \
             patch.object(raw_events, 'finish_processor'):
            relationship_engine.process_turn(
                'u', 'gojo', 'hi', source_event_id='evt-rel-stats')
            relationship_engine.process_turn(
                'u', 'gojo', 'hi', source_event_id='evt-rel-stats')

        self.assertEqual(begins['n'], 2)
        self.assertEqual(stats['n'], 1)

    def test_log_stats_receives_source_event_id(self):
        seen = {}

        def log_stats(user_id, character_id, signals, session_id,
                      source_event_id=None):
            seen['source_event_id'] = source_event_id
            seen['signals'] = signals

        @contextmanager
        def txn():
            yield object()

        with patch.object(relationship_engine, 'ensure_state_row'), \
             patch.object(relationship_engine, 'extract_signals',
                          return_value={'signals': [_sig('positive_reciprocal')]}), \
             patch.object(relationship_engine, '_route_signal',
                          return_value={'action': 'ok'}), \
             patch.object(relationship_engine, '_log_interaction_stats',
                          side_effect=log_stats), \
             patch.object(relationship_engine, '_log_temporal_observation'), \
             patch.object(relationship_engine, 'cleanup_hypotheses'), \
             patch.object(relationship_engine, 'check_retreat_boundary_superseded'), \
             patch.object(relationship_engine, 'relationship_txn', txn), \
             patch.object(relationship_engine, 'try_begin_application',
                          return_value=True), \
             patch.object(relationship_engine, '_ingest_v4',
                          return_value={'status': 'inserted'}), \
             patch.object(raw_events, 'sources_are_active', return_value=True), \
             patch.object(raw_events, 'claim_processor', return_value='claimed'), \
             patch.object(raw_events, 'finish_processor'):
            relationship_engine.process_turn(
                'u', 'gojo', 'hi', source_event_id='evt-from-chat')

        self.assertEqual(seen.get('source_event_id'), 'evt-from-chat')


class DurableEngineUntouchedTests(unittest.TestCase):
    def test_rejection_still_routes_to_handle_rejection(self):
        source = inspect.getsource(relationship_engine._route_signal)
        self.assertIn("stype == 'explicit_rejection' and actor == 'user'", source)
        self.assertIn('return _handle_rejection(', source)
        self.assertIn("stype == 'offensive_content' and actor == 'user'", source)
        self.assertIn('return _apply_offensive(', source)
        self.assertIn("stype == 'positive_reciprocal' and actor == 'user'", source)
        self.assertIn('return _handle_reciprocal(', source)

    def test_handle_rejection_still_writes_flirt_friction(self):
        source = inspect.getsource(relationship_engine._handle_rejection)
        self.assertIn("signal_type='explicit_rejection'", source)
        self.assertIn("'flirt_rejected'", source)


if __name__ == '__main__':
    unittest.main()
