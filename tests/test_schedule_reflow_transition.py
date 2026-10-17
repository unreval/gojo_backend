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
