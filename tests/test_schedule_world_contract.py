import os
import sys
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)


class CanonicalPhaseContractTests(unittest.TestCase):
    """Small deterministic contracts for the canonical schedule primitives.

    Database integration is covered by the schedule/phone-check tests.  These
    tests intentionally exercise the pure rules that must not depend on a
    database clock or an LLM response.
    """

    def test_short_soft_task_becomes_completed_focus_and_free_followup(self):
        from schedule_contract import advance_phase_states, build_phase_plan

        target = date(2026, 9, 27)
        phases = build_phase_plan({
            'start_time': '20:00',
            'end_time': '22:00',
            'title': '处理家族报告',
            'reply_state': 'soft_busy',
            'effective_busy_minutes': 15,
        }, target, timezone.utc)

        self.assertEqual(len(phases), 2)
        self.assertEqual(phases[0]['reply_state'], 'soft_busy')
        self.assertEqual(phases[1]['reply_state'], 'free')
        self.assertEqual(phases[0]['planned_end_at'].strftime('%H:%M'), '20:15')

        advanced = advance_phase_states(
            phases, datetime(2026, 9, 27, 20, 16, tzinfo=timezone.utc))
        self.assertEqual(advanced[0]['status'], 'completed')
        self.assertEqual(advanced[1]['status'], 'active')

    def test_active_event_cannot_claim_completion_without_intent(self):
        from schedule_contract import completion_claim_conflict

        world = {
            'event': {'id': 7, 'status': 'active', 'revision': 3,
                      'title': '家族会议'},
        }
        self.assertEqual(
            completion_claim_conflict(
                [{'jp': '会議はもう終わった。', 'zh': '会议已经结束了。'}],
                world,
                None,
            ),
            'active_event_completion_claim_without_intent',
        )

    def test_matching_structured_complete_allows_completion_statement(self):
        from schedule_contract import completion_claim_conflict, normalize_action_intent

        world = {
            'event': {'id': 7, 'status': 'active', 'revision': 3,
                      'title': '家族会议'},
        }
        intent = normalize_action_intent({
            'type': 'complete', 'event_id': 7, 'expected_revision': 3,
        })
        self.assertEqual(
            completion_claim_conflict(
                [{'jp': '会議はもう終わった。', 'zh': '会议已经结束了。'}],
                world,
                intent,
            ),
            None,
        )

    def test_overlapping_events_are_not_a_valid_canonical_timeline(self):
        from schedule_contract import timeline_is_valid

        target = date(2026, 9, 27)
        self.assertTrue(timeline_is_valid([
            {'start_time': '14:00', 'end_time': '14:15', 'title': '处理报告'},
            {'start_time': '14:15', 'end_time': '15:00', 'title': '回消息'},
        ], target, timezone.utc))
        self.assertFalse(timeline_is_valid([
            {'start_time': '14:00', 'end_time': '15:00', 'title': '处理报告'},
            {'start_time': '14:30', 'end_time': '15:30', 'title': '临时通话'},
        ], target, timezone.utc))


class ScheduleTransitionCommitContractTests(unittest.TestCase):
    def test_unavailable_world_does_not_reject_a_normal_reply(self):
        import db_schedule
        from schedule_transition import validate_generated_schedule_reply

        with patch.object(db_schedule, 'get_current_world_state',
                          side_effect=RuntimeError('schedule unavailable')):
            reason, world = validate_generated_schedule_reply(
                'gojo', 'u1', {'messages': [{'jp': '了解。', 'zh': '知道了。'}]})

        self.assertIsNone(reason)
        self.assertIsNone(world)

    def test_insert_is_rejected_while_an_event_is_active(self):
        import db_schedule

        class Cursor:
            def __init__(self):
                self._one = None

            def execute(self, sql, params=None):
                if 'SELECT 1 FROM char_schedule' in ' '.join(sql.split()):
                    self._one = (7,)

            def fetchone(self):
                return self._one

            def close(self):
                pass

        class Conn:
            def __init__(self):
                self.cursor_value = Cursor()
                self.rolled_back = False
                self.committed = False

            def cursor(self):
                return self.cursor_value

            def rollback(self):
                self.rolled_back = True

            def commit(self):
                self.committed = True

            def close(self):
                pass

        conn = Conn()
        intent = {
            'type': 'insert',
            'event': {
                'title': '临时通话',
                'duration_minutes': 15,
                'reply_state': 'soft_busy',
            },
        }
        with patch.object(db_schedule, 'get_conn', return_value=conn), \
             patch.object(db_schedule, '_advance_world_tx', return_value=[]), \
             patch.object(db_schedule, '_insert_transition_event_tx', return_value=99) as insert, \
             patch.object(db_schedule, '_shift_future_flexible_events_tx', return_value=[]), \
             patch.object(db_schedule, '_reconcile_phone_checks_tx'):
            result = db_schedule.commit_schedule_transition(
                'gojo', 'u1', intent,
                now=datetime(2026, 9, 27, 14, 0, tzinfo=timezone.utc))

        self.assertEqual(result, {
            'ok': False,
            'reason': 'active_event_requires_explicit_transition',
        })
        self.assertTrue(conn.rolled_back)
        self.assertFalse(conn.committed)
        insert.assert_not_called()


class PhoneCheckOccurrenceContractTests(unittest.TestCase):
    def test_inbound_during_claim_requires_successor_and_finish_uses_watermark(self):
        from phone_check_occurrence import (
            finish_is_safe, inbound_occurrence_action,
        )

        claimed = {
            'id': 10,
            'check_state': 'processing',
            'pending_count': 1,
            'seen_watermark': 1,
            'event_revision': 4,
        }
        self.assertEqual(inbound_occurrence_action(claimed, event_revision=4), 'successor')
        self.assertTrue(finish_is_safe(claimed))
        claimed['pending_count'] = 2
        self.assertFalse(finish_is_safe(claimed))

    def test_recent_reply_gets_momentum_but_blocked_does_not(self):
        from phone_check_occurrence import next_check_window

        now = datetime(2026, 9, 27, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(next_check_window('soft_busy', now, replied_at=now), (1, 4))
        self.assertIsNone(next_check_window('hard_busy', now, replied_at=now))


class ScheduleNoveltyAndPoiContractTests(unittest.TestCase):
    def test_industrial_osm_result_is_not_a_sweets_poi(self):
        from places_engine import validate_nominatim_result

        industrial = {
            'place_id': 100,
            'osm_type': 'way',
            'osm_id': 88,
            'class': 'man_made',
            'type': 'works',
            'display_name': 'Example Factory, Tokyo',
            'lat': '35.0',
            'lon': '139.0',
            'extratags': {'landuse': 'industrial'},
        }
        self.assertIsNone(validate_nominatim_result(industrial, 'sweets', 'tokyo'))

    def test_strict_novelty_only_applies_to_leisure_and_flavor(self):
        from schedule_novelty import validate_novelty

        history = [
            {'category': 'flavor', 'planned_place': {'provider_place_id': 'p1'},
             'title': '吃草莓蛋糕', 'note': '排队半小时'},
            {'category': 'obligation', 'title': '处理家族文件', 'note': ''},
        ]
        duplicate_flavor = {
            'category': 'flavor', 'planned_place': {'provider_place_id': 'p1'},
            'title': '吃草莓蛋糕', 'note': '排队半小时',
        }
        recurring_obligation = {
            'category': 'obligation', 'title': '处理家族文件', 'note': '',
        }
        self.assertTrue(validate_novelty(duplicate_flavor, history).rejected)
        self.assertFalse(validate_novelty(recurring_obligation, history).rejected)


if __name__ == '__main__':
    unittest.main()
