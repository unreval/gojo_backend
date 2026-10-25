"""Persisted reflow facts and conversation rendering, without live services."""
import copy
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gojo_backend'))

import db_schedule
from schedule_contract import normalize_action_intent


NOW = datetime(2026, 10, 17, 21, tzinfo=timezone(timedelta(hours=8)))
HISTORY = '【最近在你们对话里改过的安排】'


@pytest.fixture
def schedule_database(monkeypatch):
    if not os.environ.get('COGNITIVE_TEST_PGLITE'):
        pytest.skip('COGNITIVE_TEST_PGLITE is required for canonical SQL coverage')
    from tests.offline_pg import Connection
    import db

    connection = Connection()
    monkeypatch.setattr(db, 'get_conn', lambda: connection)
    monkeypatch.setattr(db_schedule, 'get_conn', lambda: connection)
    try:
        db_schedule.init_schedule_table()
        yield connection
    finally:
        connection.rollback()
        connection.shutdown()


def _prepare(action, *, future=True, fixed=False):
    items = []
    if action == 'extend':
        items.append({'start_time': '20:30', 'end_time': '21:30',
                      'title': '当前聊天', 'fixedness': 'flexible',
                      'reply_state': 'free'})
    if future:
        items.extend([
            {'start_time': '22:00', 'end_time': '22:30', 'title': '深夜事务',
             'fixedness': 'flexible'},
            {'start_time': '22:30', 'end_time': '23:00', 'title': '备课',
             'fixedness': 'fixed' if fixed else 'flexible'},
        ])
    events = db_schedule.save_canonical_schedule(
        'c1', 'u1', NOW.date(), items, now=NOW)
    if action == 'insert':
        intent = {'type': 'insert', 'event': {'title': '和她聊天',
                  'duration_minutes': 30, 'reply_state': 'free'}}
    else:
        intent = {'type': 'extend', 'event_id': events[0]['id'],
                  'expected_revision': events[0]['revision'], 'extend_minutes': 15}
    return intent, [event['id'] for event in events if event['status'] == 'planned']


def _timeline(connection, table, id_column):
    cur = connection.cursor()
    try:
        cur.execute(f'''SELECT id, {id_column}, planned_start_at, planned_end_at,
                               revision FROM {table} ORDER BY id''')
        return {row[0]: row[1:] for row in cur.fetchall()}
    finally:
        cur.close()


def _render(connection):
    # Production uses CURRENT_TIMESTAMP; pin it so the 24-hour window is stable.
    connection.query('UPDATE char_schedule_transition SET created_at=%s', (NOW,))
    connection.commit()
    return db_schedule.format_world_prompt('c1', 'u1', now=NOW)[0]


@pytest.mark.parametrize('action, minutes, description', [
    ('insert', 30, '和她聊天：新增了'), ('extend', 15, '当前聊天：延长了'),
])
@pytest.mark.parametrize('future', [True, False])
def test_commit_records_only_actual_reflow_and_renders_history(
        schedule_database, action, minutes, description, future):
    intent, expected_ids = _prepare(action, future=future)
    original = copy.deepcopy(intent)
    before_events = _timeline(schedule_database, 'char_schedule', 'id')
    before_phases = _timeline(schedule_database, 'char_schedule_phase', 'schedule_id')
    schedule_database.commit()

    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', intent, now=NOW, source_event_id='user-chat-1')
    assert result['ok'] is True
    assert result['reflowed_event_ids'] == expected_ids
    assert intent == original
    rows = schedule_database.query('''SELECT intent, source_event_id, status
        FROM char_schedule_transition''')['rows']
    assert len(rows) == 1
    stored = rows[0]['intent']
    assert rows[0]['source_event_id'] == 'user-chat-1'
    assert rows[0]['status'] == 'committed'
    normalized = normalize_action_intent(intent)
    assert {key: stored[key] for key in normalized} == normalized

    after_events = _timeline(schedule_database, 'char_schedule', 'id')
    after_phases = _timeline(schedule_database, 'char_schedule_phase', 'schedule_id')
    actual_reflowed_ids = [event_id for event_id, before in before_events.items()
                          if after_events[event_id][1] != before[1]]
    assert actual_reflowed_ids == expected_ids
    delta = timedelta(minutes=minutes)
    for before, after in ((before_events, after_events), (before_phases, after_phases)):
        for row_id, (event_id, start, end, revision) in before.items():
            if event_id in expected_ids:
                assert after[row_id] == (event_id, start + delta, end + delta, revision + 1)
    if future:
        assert stored['reflowed_event_ids'] == actual_reflowed_ids
        assert stored['reflowed_minutes'] == minutes
    else:
        assert 'reflowed_event_ids' not in stored
        assert 'reflowed_minutes' not in stored

    text = _render(schedule_database)
    assert HISTORY in text
    assert f'今天 21:00 {description}' in text
    if future:
        assert f'{description}（后续 2 项各顺延 {minutes} 分钟）' in text
    else:
        assert '顺延' not in text


@pytest.mark.parametrize('action', ['insert', 'extend'])
def test_fixed_future_conflict_rolls_back_events_phases_and_transition(
        schedule_database, action):
    intent, _ = _prepare(action, fixed=True)
    tables = ('char_schedule', 'char_schedule_phase', 'char_schedule_transition',
              'char_phone_check')
    before = {table: schedule_database.query(f'SELECT * FROM {table} ORDER BY id')['rows']
              for table in tables}

    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', intent, now=NOW, source_event_id='user-chat-fixed')
    assert result == {'ok': False, 'reason': 'fixed_future_event_conflict'}
    after = {table: schedule_database.query(f'SELECT * FROM {table} ORDER BY id')['rows']
             for table in tables}
    assert after == before


def test_background_reflow_metadata_is_excluded_from_conversation_history(schedule_database):
    intent, expected_ids = _prepare('insert')
    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', intent, now=NOW, source_event_id='tick_revision')
    assert result['ok'] is True
    stored = schedule_database.query('SELECT intent FROM char_schedule_transition')['rows'][0]['intent']
    assert stored['reflowed_event_ids'] == expected_ids
    assert stored['reflowed_minutes'] == 30

    text = _render(schedule_database)
    assert HISTORY not in text
    assert '顺延' not in text
    assert '【今天接下来的安排' in text
    for event_id in expected_ids:
        assert f'event_id={event_id} revision=2' in text


def test_history_reflow_count_does_not_require_target_events(schedule_database):
    intent, _ = _prepare('insert', future=False)
    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', intent, now=NOW, source_event_id='user-chat-history')
    assert result['ok'] is True
    # Historical metadata is sufficient even when the shifted target is absent.
    schedule_database.query('''UPDATE char_schedule_transition
        SET intent=intent ||
            '{"reflowed_event_ids":[999],"reflowed_minutes":30}'::jsonb''')

    text = _render(schedule_database)
    assert '和她聊天：新增了（后续 1 项各顺延 30 分钟）' in text


@pytest.mark.parametrize('action', ['insert', 'extend'])
def test_normalizer_does_not_accept_storage_owned_reflow_metadata(action):
    intent = {'type': action, 'event_id': 47, 'expected_revision': 1,
              'extend_minutes': 15, 'event': {'title': '和她聊天', 'duration_minutes': 30},
              'reflowed_event_ids': [999], 'reflowed_minutes': 999}
    original = copy.deepcopy(intent)
    normalized = normalize_action_intent(intent)
    assert normalized is not None
    assert normalized['type'] == action
    assert 'reflowed_event_ids' not in normalized
    assert 'reflowed_minutes' not in normalized
    assert intent == original
    if action == 'insert':
        assert normalized['event']['reply_state'] == 'free'


INSERT_NOW = datetime(2026, 10, 24, 15, 30, tzinfo=NOW.tzinfo)


def _afternoon_schedule(*, fixed=False):
    return db_schedule.save_canonical_schedule('c1', 'u1', INSERT_NOW.date(), [
        {'start_time': '15:00', 'end_time': '16:00', 'title': '当前事务',
         'fixedness': 'flexible', 'reply_state': 'free'},
        {'start_time': '18:00', 'end_time': '19:00', 'title': '晚饭',
         'fixedness': 'flexible'},
        {'start_time': '21:00', 'end_time': '04:00', 'title': '深夜事务处理',
         'fixedness': 'fixed' if fixed else 'flexible'},
    ], now=INSERT_NOW)


def _timed_insert(start_time='21:00', duration=60):
    return {'type': 'insert', 'event': {
        'title': '教她日语', 'duration_minutes': duration,
        'reply_state': 'free', 'start_time': start_time}}


def _schedule_snapshot(connection):
    return {table: connection.query(f'SELECT * FROM {table} ORDER BY id')['rows']
            for table in ('char_schedule', 'char_schedule_phase',
                          'char_schedule_transition', 'char_phone_check')}


def test_timed_insert_reserves_nine_pm_during_active_event_and_reflows_overnight(
        schedule_database):
    events = _afternoon_schedule()
    active_id, dinner_id, overnight_id = [event['id'] for event in events]
    assert events[0]['status'] == 'active'
    before_events = _timeline(schedule_database, 'char_schedule', 'id')
    before_phases = _timeline(schedule_database, 'char_schedule_phase', 'schedule_id')
    schedule_database.commit()
    intent = _timed_insert()
    original = copy.deepcopy(intent)

    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', intent, now=INSERT_NOW, source_event_id='japanese-lesson')

    assert result['ok'] is True
    assert result['reflowed_event_ids'] == [overnight_id]
    assert intent == original
    after_events = _timeline(schedule_database, 'char_schedule', 'id')
    after_phases = _timeline(schedule_database, 'char_schedule_phase', 'schedule_id')
    for before, after in ((before_events, after_events), (before_phases, after_phases)):
        for row_id, (event_id, start, end, revision) in before.items():
            delta = timedelta(minutes=60 if event_id == overnight_id else 0)
            assert after[row_id] == (
                event_id, start + delta, end + delta,
                revision + (event_id == overnight_id))
    start = INSERT_NOW.replace(hour=21, minute=0)
    assert after_events[result['event_id']][1:3] == (start, start + timedelta(hours=1))
    assert after_events[overnight_id][1:3] == (
        start + timedelta(hours=1), start + timedelta(hours=8))
    rows = schedule_database.query('''SELECT status, reply_state, actual_start_at
        FROM char_schedule WHERE id=%s''', (result['event_id'],))['rows']
    assert rows == [{'status': 'planned', 'reply_state': 'free', 'actual_start_at': None}]
    stored = schedule_database.query(
        'SELECT intent FROM char_schedule_transition')['rows'][0]['intent']
    assert stored['event']['start_time'] == '21:00'
    assert stored['reflowed_event_ids'] == [overnight_id]
    assert stored['reflowed_minutes'] == 60
    assert active_id not in stored['reflowed_event_ids']
    assert dinner_id not in stored['reflowed_event_ids']


@pytest.mark.parametrize('start_time', ['09:00', '15:30'])
def test_timed_insert_rejects_past_or_equal_start_without_writes(
        schedule_database, start_time):
    _afternoon_schedule()
    before = _schedule_snapshot(schedule_database)

    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', _timed_insert(start_time), now=INSERT_NOW)

    assert result == {'ok': False, 'reason': 'insert_start_not_future'}
    assert _schedule_snapshot(schedule_database) == before


@pytest.mark.parametrize('start_time, actual_end, overlaps', [
    ('15:45', None, True),
    ('16:00', None, False),
    ('16:00', '16:30', True),
    ('16:30', '16:30', False),
])
def test_timed_insert_checks_active_actual_or_planned_half_open_window(
        schedule_database, start_time, actual_end, overlaps):
    active = _afternoon_schedule()[0]
    if actual_end:
        hour, minute = map(int, actual_end.split(':'))
        schedule_database.query('UPDATE char_schedule SET actual_end_at=%s WHERE id=%s',
                                (INSERT_NOW.replace(hour=hour, minute=minute), active['id']))
        schedule_database.commit()
    before = _schedule_snapshot(schedule_database)

    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', _timed_insert(start_time, duration=15), now=INSERT_NOW)

    if overlaps:
        assert result == {'ok': False, 'reason': 'insert_overlaps_active_event'}
        assert _schedule_snapshot(schedule_database) == before
    else:
        assert result['ok'] is True


def test_timed_insert_fixed_conflict_rolls_back_all_schedule_tables(schedule_database):
    _afternoon_schedule(fixed=True)
    before = _schedule_snapshot(schedule_database)

    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', _timed_insert(), now=INSERT_NOW, source_event_id='blocked-lesson')

    assert result == {'ok': False, 'reason': 'fixed_future_event_conflict'}
    assert _schedule_snapshot(schedule_database) == before


def test_insert_without_start_keeps_active_event_rejection(schedule_database):
    _afternoon_schedule()
    intent = _timed_insert()
    del intent['event']['start_time']
    before = _schedule_snapshot(schedule_database)

    result = db_schedule.commit_schedule_transition('c1', 'u1', intent, now=INSERT_NOW)

    assert result == {'ok': False, 'reason': 'active_event_requires_explicit_transition'}
    assert _schedule_snapshot(schedule_database) == before


@pytest.mark.parametrize('now, start_time, duration, expected_start', [
    (INSERT_NOW, '23:30', 120, INSERT_NOW.replace(hour=23, minute=30)),
    (datetime(2026, 10, 23, 16, 30, tzinfo=timezone.utc), '01:00', 60,
     INSERT_NOW.replace(hour=1, minute=0)),
])
def test_timed_insert_anchors_to_local_today_and_preserves_overnight_end(
        schedule_database, now, start_time, duration, expected_start):
    result = db_schedule.commit_schedule_transition(
        'c1', 'u1', _timed_insert(start_time, duration), now=now)

    assert result['ok'] is True
    events = _timeline(schedule_database, 'char_schedule', 'id')
    phases = _timeline(schedule_database, 'char_schedule_phase', 'schedule_id')
    expected_end = expected_start + timedelta(minutes=duration)
    assert events[result['event_id']][1:3] == (expected_start, expected_end)
    assert list(phases.values())[0][1:3] == (expected_start, expected_end)
    row = schedule_database.query(
        'SELECT sched_date::text AS sched_date FROM char_schedule WHERE id=%s',
        (result['event_id'],))['rows'][0]
    assert row['sched_date'] == expected_start.date().isoformat()


AGREEMENT_NOW = datetime(2026, 10, 25, 14, 48, tzinfo=NOW.tzinfo)
DURING_NIGHT = datetime(2026, 10, 25, 22, 30, tzinfo=NOW.tzinfo)
CASUAL_AGREEMENT = '行吧，你中途别睡着就行。'


def _evening_schedule(*, fixed=False):
    return db_schedule.save_canonical_schedule('gojo', 'u', AGREEMENT_NOW.date(), [
        {'start_time': '16:30', 'end_time': '17:30', 'title': '备课',
         'fixedness': 'flexible', 'reply_state': 'soft_busy'},
        {'start_time': '22:00', 'end_time': '04:00', 'title': '深夜事务处理',
         'fixedness': 'fixed' if fixed else 'flexible', 'reply_state': 'soft_busy'},
    ], now=AGREEMENT_NOW)


def _bounds(timeline, event_id):
    return timeline[event_id][1:3]


def test_cancel_after_casual_agreement_clears_soft_busy_without_reflow(schedule_database):
    """2026-10-25：答应空出晚上，cancel 只取消挡路安排，不顺延邻居。"""
    events = _evening_schedule()
    night = next(event for event in events if event['title'] == '深夜事务处理')
    lesson = next(event for event in events if event['title'] == '备课')
    schedule_database.commit()

    during = db_schedule.get_current_world_state('gojo', 'u', DURING_NIGHT)
    assert during['event']['id'] == night['id']
    assert during['availability']['reply_state'] == 'soft_busy'
    before = _timeline(schedule_database, 'char_schedule', 'id')

    result = db_schedule.commit_schedule_transition(
        'gojo', 'u', {
            'type': 'cancel', 'event_id': night['id'],
            'expected_revision': during['event']['revision'],
        }, now=DURING_NIGHT, source_event_id='user-clear-1830-0100')

    assert result['ok'] is True
    assert result['action'] == 'cancel'
    assert result['reflowed_event_ids'] == []
    after_world = db_schedule.get_current_world_state('gojo', 'u', DURING_NIGHT)
    assert after_world['event'] is None
    assert after_world['availability']['reply_state'] == 'free'
    assert after_world['availability']['reply_state'] != 'soft_busy'
    after = _timeline(schedule_database, 'char_schedule', 'id')
    assert _bounds(after, lesson['id']) == _bounds(before, lesson['id'])
    assert _bounds(after, night['id']) == _bounds(before, night['id'])
    stored = schedule_database.query(
        'SELECT intent FROM char_schedule_transition')['rows'][0]['intent']
    assert stored['type'] == 'cancel'
    assert 'reflowed_event_ids' not in stored
    assert CASUAL_AGREEMENT == '行吧，你中途别睡着就行。'


def test_fixed_night_event_rejects_cancel_and_stays_soft_busy(schedule_database):
    events = _evening_schedule(fixed=True)
    night = next(event for event in events if event['title'] == '深夜事务处理')
    schedule_database.commit()
    during = db_schedule.get_current_world_state('gojo', 'u', DURING_NIGHT)
    before = _timeline(schedule_database, 'char_schedule', 'id')

    result = db_schedule.commit_schedule_transition(
        'gojo', 'u', {
            'type': 'cancel', 'event_id': night['id'],
            'expected_revision': during['event']['revision'],
        }, now=DURING_NIGHT, source_event_id='user-clear-fixed')

    assert result == {'ok': False, 'reason': 'fixed_event_requires_external_override'}
    still = db_schedule.get_current_world_state('gojo', 'u', DURING_NIGHT)
    assert still['availability']['reply_state'] == 'soft_busy'
    after = _timeline(schedule_database, 'char_schedule', 'id')
    assert _bounds(after, night['id']) == _bounds(before, night['id'])
