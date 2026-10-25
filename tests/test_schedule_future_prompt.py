"""Future schedule prompt contracts, using only an in-memory SQL database."""
import copy
import json
import os
import sqlite3
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch


BACKEND = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import db_schedule


TZ = timezone(timedelta(hours=8))
FUTURE = '【今天接下来的安排——canonical，可调整】'
HISTORY = '【最近在你们对话里改过的安排】'
sqlite3.register_converter('ISO_TIMESTAMP', lambda value: datetime.fromisoformat(value.decode()))
sqlite3.register_converter('ISO_JSON', lambda value: json.loads(value.decode()))


def _sql_value(value):
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value.isoformat() if isinstance(value, date) else value


class ReadCursor:
    """Execute production SELECTs; adapt placeholders/types, never filter rows."""

    def __init__(self, connection):
        self.cursor = connection.cursor()
        self.queries = []
        self.close = Mock()

    def execute(self, sql, params):
        if not sql.lstrip().upper().startswith('SELECT '):
            raise AssertionError('Prompt assembly must only read schedule facts')
        self.queries.append((sql, params))
        self.cursor.execute(sql.replace('%s', '?'), tuple(map(_sql_value, params)))

    def fetchall(self):
        return self.cursor.fetchall()


class FutureSchedulePromptTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 17, 18, tzinfo=TZ)
        self.db = sqlite3.connect(':memory:', detect_types=sqlite3.PARSE_DECLTYPES)
        self.addCleanup(self.db.close)
        self.db.executescript('''
            CREATE TABLE char_schedule (
                id INTEGER PRIMARY KEY, revision INTEGER, character_id TEXT,
                user_id TEXT, status TEXT, sched_date TEXT,
                planned_start_at ISO_TIMESTAMP, planned_end_at ISO_TIMESTAMP,
                title TEXT, location TEXT, fixedness TEXT, category TEXT,
                reply_state TEXT);
            CREATE TABLE char_schedule_transition (
                schedule_id INTEGER, character_id TEXT, user_id TEXT,
                status TEXT, source_event_id TEXT, intent ISO_JSON,
                created_at ISO_TIMESTAMP);
        ''')
        self.cursor = ReadCursor(self.db)
        self.conn = Mock()
        self.conn.cursor.return_value = self.cursor
        self.mock_schedule('_canonical_tz', return_value=TZ)
        self.connection = self.mock_schedule('get_conn', return_value=self.conn)
        self.advance = self.mock_schedule(
            '_advance_world_tx', side_effect=AssertionError('Unexpected clock advance'))
        self.world = {'event': None, 'activity': None, 'phase': None,
                      'availability': {'reply_state': 'free'}}
        self.current = self.mock_schedule(
            'get_current_world_state', side_effect=lambda *args, **kwargs: self.world)

    def mock_schedule(self, name, **kwargs):
        patcher = patch.object(db_schedule, name, **kwargs)
        self.addCleanup(patcher.stop)
        return patcher.start()

    def event(self, event_id, start=None, **changes):
        start = start or self.now + timedelta(hours=1)
        row = dict(id=event_id, revision=event_id + 1, character_id='gojo', user_id='u1',
                   status='planned', sched_date=start.astimezone(TZ).date(),
                   planned_start_at=start, planned_end_at=start + timedelta(minutes=30),
                   title=f'安排{event_id}', location='', fixedness='flexible',
                   category='leisure', reply_state='free')
        row.update(changes)
        self.db.execute(f'INSERT INTO char_schedule ({", ".join(row)}) '
                        f'VALUES ({", ".join("?" for _ in row)})',
                        tuple(map(_sql_value, row.values())))

    def transition(self, event_id, created=None, **changes):
        row = dict(schedule_id=event_id, character_id='gojo', user_id='u1',
                   status='committed', source_event_id=f'user-event-{event_id}',
                   intent={'type': 'cancel', 'event_id': event_id, 'expected_revision': 1},
                   created_at=created or self.now - timedelta(minutes=10))
        row.update(changes)
        row['intent'] = db_schedule.normalize_action_intent(row['intent'])
        self.assertIsNotNone(row['intent'])
        self.db.execute(f'INSERT INTO char_schedule_transition ({", ".join(row)}) '
                        f'VALUES ({", ".join("?" for _ in row)})',
                        tuple(map(_sql_value, row.values())))

    def activate(self):
        self.world.update(event={'id': 7, 'revision': 3, 'status': 'active'},
                          activity={'start_time': '17:30', 'end_time': '18:30',
                                    'title': '当前任务', 'location': '原地点', 'note': '原想法'},
                          phase={'planned_start_at': self.now - timedelta(minutes=30),
                                 'planned_end_at': self.now + timedelta(minutes=30),
                                 'title': '当前阶段'})

    def render(self, now=None):
        original = copy.deepcopy(self.world)
        writes = self.db.total_changes
        text, world = db_schedule.format_world_prompt('gojo', 'u1', now or self.now)
        self.assertIs(world, self.world)
        self.assertEqual(world, original)
        self.assertEqual(self.db.total_changes, writes)
        self.current.assert_called_once()
        self.advance.assert_not_called()
        self.conn.commit.assert_not_called()
        return text

    def test_active_and_three_future_events_keep_canonical_ids_and_both_sections(self):
        self.activate()
        for event_id, hour, fixedness in ((47, 19, 'flexible'), (48, 20, 'optional'),
                                          (49, 21, 'fixed')):
            self.event(event_id, self.now.replace(hour=hour), fixedness=fixedness,
                       location='库内地点 · 一般区域' if event_id == 47 else '')
        self.event(10, status='cancelled', title='对话里取消的安排')
        self.transition(10, self.now - timedelta(hours=20, minutes=20))
        text = self.render()
        self.assertIn('event_id=7 revision=3 status=active', text)
        self.assertIn('当前任务（在原地点）', text)
        self.assertIn('当前阶段', text)
        self.assertIn('原想法', text)
        self.assertIn(FUTURE, text)
        self.assertIn(HISTORY, text)
        for event_id in (47, 48, 49):
            self.assertIn(f'event_id={event_id} revision={event_id + 1}', text)
        self.assertLess(text.index('event_id=47'), text.index('event_id=48'))
        self.assertLess(text.index('event_id=48'), text.index('event_id=49'))
        self.assertIn('19:00~19:30', text)
        self.assertIn('库内地点 · 一般区域', text)
        self.assertIn('fixedness=fixed', text)
        self.assertIn('expected_revision', text)
        self.assertIn('insert 的 start_time 只表示新事项今天晚于现在的开始时间', text)
        self.assertIn('不要用 insert 把后面的安排往后推来假装把时间空出来', text)
        self.assertNotIn('为她预留时段', text)
        self.assertIn('不写就是立刻开始', text)
        self.assertIn('没有已提交 intent', text)
        self.assertIn('昨天 21:40', text)
        self.assertIn('对话里取消的安排', text)
        self.assertEqual(len(self.cursor.queries), 2)
        self.connection.assert_called_once()
        self.cursor.close.assert_called_once()
        self.conn.close.assert_called_once()

    def test_future_section_survives_without_active_event(self):
        self.event(47, revision=3, location='已解析地点')
        text = self.render()
        self.assertIn(FUTURE, text)
        self.assertIn('event_id=47 revision=3', text)
        self.assertIn('已解析地点', text)
        self.assertNotIn('你此刻正在做的事', text)
        self.assertNotIn(HISTORY, text)

    def test_no_future_events_does_not_emit_empty_heading_or_hint(self):
        self.activate()
        text = self.render()
        self.assertIn('当前任务', text)
        self.assertNotIn(FUTURE, text)
        self.assertNotIn(HISTORY, text)
        self.assertNotIn('这些还没发生', text)

    def test_no_current_future_or_transition_returns_empty_text(self):
        self.assertEqual(self.render(), '')

    def test_history_renders_without_current_or_future_events(self):
        self.event(10, status='cancelled', title='已取消晚饭')
        self.transition(10)
        text = self.render()
        self.assertIn(HISTORY, text)
        self.assertIn('今天 17:50', text)
        self.assertIn('已取消晚饭', text)
        self.assertNotIn(FUTURE, text)
        self.assertNotIn('为她', text)

    def test_background_unlinked_failed_stale_and_other_tenant_transitions_are_excluded(self):
        cases = [dict(source_event_id='tick_revision'), dict(source_event_id=''),
                 dict(source_event_id=None), dict(status='failed'),
                 dict(created=self.now - timedelta(hours=24)),
                 dict(created=self.now - timedelta(hours=25)),
                 dict(character_id='other'), dict(user_id='other')]
        for event_id, changes in enumerate(cases, 100):
            self.event(event_id, status='cancelled', title=f'不可注入{event_id}')
            self.transition(event_id, **changes)
        self.assertEqual(self.render(), '')
        self.assertEqual(len(self.cursor.queries), 2)

    def test_user_history_is_newest_first_capped_at_three_and_exact_sentinel_only(self):
        for event_id in range(4):
            self.event(event_id, status='cancelled', title=f'历史{event_id}')
            self.transition(event_id, self.now - timedelta(minutes=event_id + 1),
                            source_event_id='tick_revision-extra' if event_id == 0 else 'user-event')
        text = self.render()
        self.assertIn(HISTORY, text)
        self.assertLess(text.index('历史0'), text.index('历史1'))
        self.assertLess(text.index('历史1'), text.index('历史2'))
        self.assertNotIn('历史3', text)

    def test_background_relocation_exposes_current_future_facts_without_history(self):
        self.event(47, revision=4, location='数据库更新后的地点')
        self.transition(47, source_event_id='tick_revision',
                        intent={'type': 'relocate', 'event_id': 47, 'expected_revision': 3})
        text = self.render()
        self.assertIn('event_id=47 revision=4', text)
        self.assertIn('数据库更新后的地点', text)
        self.assertNotIn(HISTORY, text)

    def test_committed_cancel_relocate_and_insert_describe_only_recorded_actions(self):
        for event_id, action in enumerate(('cancel', 'relocate', 'insert'), 1):
            self.event(event_id, status='completed', title=f'已改安排{event_id}')
            self.transition(event_id, intent={'type': action, 'event_id': event_id,
                                              'expected_revision': 1,
                                              'event': {'title': '新增安排', 'duration_minutes': 30}})
        text = self.render()
        for action in ('取消了', '换了地点', '新增了'):
            self.assertIn(action, text)
        self.assertNotIn('为她', text)

    def test_committed_complete_and_extend_describe_recorded_actions(self):
        for event_id, action in enumerate(('complete', 'extend'), 1):
            self.event(event_id, status='completed')
            self.transition(event_id, intent={'type': action, 'event_id': event_id,
                                              'expected_revision': 1, 'extend_minutes': 15})
        text = self.render()
        self.assertIn('提前完成了', text)
        self.assertIn('延长了', text)

    def test_planned_query_uses_canonical_day_strict_future_scope_and_eight_row_limit(self):
        now = datetime(2026, 10, 16, 16, 30, tzinfo=timezone.utc)
        for event_id in range(10):
            self.event(event_id, now + timedelta(minutes=30 * (event_id + 1)))
        for event_id, changes in enumerate([
                dict(status='active'), dict(status='cancelled'), dict(character_id='other'),
                dict(user_id='other'), dict(sched_date=date(2026, 10, 16)),
                dict(sched_date=date(2026, 10, 18))], 100):
            self.event(event_id, now + timedelta(minutes=1), **changes)
        self.event(200, now)
        self.event(201, now - timedelta(minutes=1))
        rows = db_schedule._remaining_planned_events_tx(self.cursor, 'gojo', 'u1', now)
        self.assertEqual([row[0] for row in rows], list(range(8)))
        self.advance.assert_not_called()
        self.connection.assert_not_called()

    def test_future_display_uses_canonical_time_when_input_clock_has_another_zone(self):
        now = datetime(2026, 10, 16, 16, 30, tzinfo=timezone.utc)
        self.event(47, now + timedelta(minutes=30))
        text = self.render(now)
        self.assertIn(FUTURE, text)
        self.assertIn('01:00~01:30', text)
        self.assertEqual(self.current.call_args.args[2].date(), date(2026, 10, 17))

    def test_transition_left_join_preserves_committed_history_without_title(self):
        self.transition(999)
        text = self.render()
        self.assertIn(HISTORY, text)
        self.assertNotIn('None', text)


if __name__ == '__main__':
    unittest.main()
