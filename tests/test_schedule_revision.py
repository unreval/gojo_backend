"""test_schedule_revision.py — B1 自主复核单元测试

覆盖:
  - normalize_action_intent 的 reason 字段
  - poi_closed 规则 (含 raw-tag 回退、时区转换)
  - unreachable 规则
  - fatigue 规则 (含 active 事件截断到 now)
  - 提交后状态刷新
  - 架构不变量 (零 LLM, 只写 commit_schedule_transition)
"""
import sys
import os
import math
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gojo_backend'))

CN_TZ = timezone(timedelta(hours=8))
UTC = timezone.utc


def _ts(hour, minute=0, day=16, tz=CN_TZ):
    """Helper: datetime on 2026-10-{day} at {hour}:{minute} in given tz."""
    return datetime(2026, 10, day, hour, minute, tzinfo=tz)


def _make_event(eid, start_h, end_h, **kw):
    """Helper: build a minimal event dict for testing."""
    return {
        'id': eid,
        'start_time': f'{start_h:02d}:00',
        'end_time': f'{end_h:02d}:00',
        'title': kw.get('title', f'Event {eid}'),
        'location': kw.get('location', ''),
        'planned_start_at': kw.get('planned_start_at', _ts(start_h)),
        'planned_end_at': kw.get('planned_end_at', _ts(end_h)),
        'actual_start_at': kw.get('actual_start_at'),
        'actual_end_at': kw.get('actual_end_at'),
        'status': kw.get('status', 'planned'),
        'revision': kw.get('revision', 1),
        'fixedness': kw.get('fixedness', 'flexible'),
        'reply_state': kw.get('reply_state', 'free'),
        'planned_place': kw.get('planned_place'),
        'category': kw.get('category', 'routine'),
    }


# ─────────────────────────────────────────────
#  Test: normalize_action_intent reason field
# ─────────────────────────────────────────────

class TestReasonField:
    """schedule_contract.normalize_action_intent must accept and validate reason."""

    def test_valid_reason_accepted(self):
        from schedule_contract import normalize_action_intent
        intent = normalize_action_intent({
            'type': 'cancel', 'event_id': 1, 'expected_revision': 1,
            'reason': 'poi_closed',
        })
        assert intent is not None
        assert intent['reason'] == 'poi_closed'

    def test_all_valid_reasons(self):
        from schedule_contract import normalize_action_intent, REVISION_REASONS
        for reason in sorted(REVISION_REASONS):
            intent = normalize_action_intent({
                'type': 'cancel', 'event_id': 1, 'expected_revision': 1,
                'reason': reason,
            })
            assert intent is not None, f'reason={reason} was rejected'
            assert intent['reason'] == reason

    def test_unknown_reason_silently_dropped(self):
        from schedule_contract import normalize_action_intent
        intent = normalize_action_intent({
            'type': 'cancel', 'event_id': 1, 'expected_revision': 1,
            'reason': 'totally_made_up',
        })
        assert intent is not None, 'unknown reason should not reject'
        assert 'reason' not in intent

    def test_no_reason_still_valid(self):
        from schedule_contract import normalize_action_intent
        intent = normalize_action_intent({
            'type': 'cancel', 'event_id': 1, 'expected_revision': 1,
        })
        assert intent is not None
        assert 'reason' not in intent

    def test_reason_case_insensitive(self):
        from schedule_contract import normalize_action_intent
        intent = normalize_action_intent({
            'type': 'cancel', 'event_id': 1, 'expected_revision': 1,
            'reason': 'POI_CLOSED',
        })
        assert intent is not None
        assert intent['reason'] == 'poi_closed'

    def test_reason_on_relocate(self):
        from schedule_contract import normalize_action_intent
        intent = normalize_action_intent({
            'type': 'relocate', 'event_id': 1, 'expected_revision': 1,
            'reason': 'unreachable',
        })
        assert intent is not None
        assert intent['reason'] == 'unreachable'
        # No planned_place → relocate to 一般区域
        assert 'planned_place' not in intent


# ─────────────────────────────────────────────
#  Test: _get_opening_spec helper
# ─────────────────────────────────────────────

class TestGetOpeningSpec:
    """_get_opening_spec must find spec directly or re-parse from raw tags."""

    def test_uses_stored_spec(self):
        """Post-A1 events: opening_spec already in JSONB → use it directly."""
        from opening_hours import parse_opening_hours
        import schedule_revision
        spec = parse_opening_hours('Mo-Fr 09:00-17:00')
        place = {'opening_spec': spec, 'lat': 35.68, 'lng': 139.76}
        result = schedule_revision._get_opening_spec(place)
        assert result is not None
        assert result['status'] == 'parsed'

    def test_reparses_from_raw_tags(self):
        """Pre-A1 events: no opening_spec but raw tag in provider_raw_type."""
        import schedule_revision
        place = {
            'canonical_name': 'Old POI',
            'lat': 35.68, 'lng': 139.76,
            'provider_raw_type': {
                'tags': {'opening_hours': 'Mo-Fr 09:00-17:00'}
            },
        }
        result = schedule_revision._get_opening_spec(place)
        assert result is not None
        assert result['status'] == 'parsed'
        assert len(result['rules']) == 1

    def test_no_spec_no_tags_returns_none(self):
        """POI with neither opening_spec nor raw tags → None."""
        import schedule_revision
        place = {'canonical_name': 'Unknown', 'lat': 35.68, 'lng': 139.76}
        assert schedule_revision._get_opening_spec(place) is None

    def test_empty_opening_hours_returns_none(self):
        """Raw tag present but empty string → None."""
        import schedule_revision
        place = {
            'provider_raw_type': {'tags': {'opening_hours': ''}}
        }
        assert schedule_revision._get_opening_spec(place) is None

    def test_jsonb_roundtrip_spec(self):
        """opening_spec after JSON round-trip (tuples become lists) still works."""
        import json
        from opening_hours import parse_opening_hours, is_definitely_closed
        import schedule_revision
        spec = parse_opening_hours('Mo-Fr 09:00-17:00; Sa,Su off')
        # Simulate JSONB round-trip
        roundtripped = json.loads(json.dumps(spec))
        place = {'opening_spec': roundtripped}
        result = schedule_revision._get_opening_spec(place)
        assert result is not None
        # is_definitely_closed should still work
        assert is_definitely_closed(result, 6, 600)  # Sunday 10:00 → closed


# ─────────────────────────────────────────────
#  Test: poi_closed rule
# ─────────────────────────────────────────────

class TestRulePoiClosed:

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_closed_optional_cancelled(self, mock_events, mock_commit):
        """Optional event at a definitely-closed POI → cancel."""
        from opening_hours import parse_opening_hours
        spec = parse_opening_hours('Mo-Fr 09:00-17:00; Sa,Su off')  # closed on weekends

        sunday = datetime(2026, 10, 18, 10, 0, tzinfo=CN_TZ)  # Sunday
        events = [_make_event(
            10, 14, 15, fixedness='optional',
            planned_start_at=sunday.replace(hour=14),
            planned_end_at=sunday.replace(hour=15),
            planned_place={
                'canonical_name': 'Test Cafe',
                'opening_spec': spec,
                'lat': 35.68, 'lng': 139.76,
            },
        )]

        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', sunday.replace(hour=10))
        assert len(results) >= 1
        poi_r = [r for r in results if r['rule'] == 'poi_closed']
        assert len(poi_r) == 1
        assert poi_r[0]['action'] == 'cancel'
        assert poi_r[0]['ok'] is True

        intent = mock_commit.call_args[0][2]
        assert intent['type'] == 'cancel'
        assert intent['reason'] == 'poi_closed'
        assert intent['event_id'] == 10

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_closed_flexible_relocated(self, mock_events, mock_commit):
        """Flexible event at a closed POI → relocate (not cancel)."""
        from opening_hours import parse_opening_hours
        spec = parse_opening_hours('Mo-Fr 09:00-17:00; Sa,Su off')

        sunday = datetime(2026, 10, 18, 10, 0, tzinfo=CN_TZ)
        events = [_make_event(
            11, 14, 15, fixedness='flexible',
            planned_start_at=sunday.replace(hour=14),
            planned_end_at=sunday.replace(hour=15),
            planned_place={
                'canonical_name': 'Office Place',
                'opening_spec': spec,
                'lat': 35.68, 'lng': 139.76,
            },
        )]

        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', sunday.replace(hour=10))
        poi_r = [r for r in results if r['rule'] == 'poi_closed']
        assert len(poi_r) == 1
        assert poi_r[0]['action'] == 'relocate'

        intent = mock_commit.call_args[0][2]
        assert intent['type'] == 'relocate'
        assert 'planned_place' not in intent  # omit → falls to 一般区域

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_open_poi_untouched(self, mock_events, mock_commit):
        """Event at an open POI → no action."""
        from opening_hours import parse_opening_hours
        spec = parse_opening_hours('Mo-Su 08:00-22:00')  # open every day

        now = _ts(10)
        events = [_make_event(
            12, 14, 15, fixedness='flexible',
            planned_place={
                'canonical_name': 'Always Open Cafe',
                'opening_spec': spec,
                'lat': 35.68, 'lng': 139.76,
            },
        )]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        assert len(results) == 0
        mock_commit.assert_not_called()

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_no_opening_spec_no_tags_untouched(self, mock_events, mock_commit):
        """POI without opening_spec AND without raw tags → skip."""
        now = _ts(10)
        events = [_make_event(
            13, 14, 15,
            planned_place={'canonical_name': 'Mystery Shop', 'lat': 35.68, 'lng': 139.76},
        )]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        assert len(results) == 0
        mock_commit.assert_not_called()

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_raw_tag_fallback_works(self, mock_events, mock_commit):
        """Pre-A1 event: no opening_spec, but raw tag → still detects closed."""
        sunday = datetime(2026, 10, 18, 10, 0, tzinfo=CN_TZ)
        events = [_make_event(
            60, 14, 15, fixedness='optional',
            planned_start_at=sunday.replace(hour=14),
            planned_end_at=sunday.replace(hour=15),
            planned_place={
                'canonical_name': 'Old Cafe',
                'lat': 35.68, 'lng': 139.76,
                # No opening_spec — only raw tag in provider_raw_type
                'provider_raw_type': {
                    'tags': {'opening_hours': 'Mo-Fr 09:00-17:00; Sa,Su off'}
                },
            },
        )]

        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', sunday.replace(hour=10))
        poi_r = [r for r in results if r['rule'] == 'poi_closed']
        assert len(poi_r) == 1
        assert poi_r[0]['action'] == 'cancel'

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_timezone_conversion(self, mock_events, mock_commit):
        """Weekday must be computed in CN_TZ, not UTC.

        2026-10-18 00:30 UTC = 2026-10-18 08:30 CN_TZ (Sunday).
        In UTC it's 00:30 Sunday. In CN_TZ it's 08:30 Sunday.
        A shop open Mo-Sa should be detected as closed on Sunday
        regardless of the stored timezone representation.
        """
        from opening_hours import parse_opening_hours
        spec = parse_opening_hours('Mo-Sa 07:00-22:00; Su off')  # closed Sundays

        # Store planned_start_at in UTC — Sunday 06:30 UTC = Sunday 14:30 CN_TZ
        # Both are Sunday, but time differs. Spec says closed on Sunday.
        start_utc = datetime(2026, 10, 18, 6, 30, tzinfo=UTC)
        end_utc = datetime(2026, 10, 18, 7, 30, tzinfo=UTC)
        events = [_make_event(
            61, 14, 15, fixedness='optional',
            planned_start_at=start_utc,
            planned_end_at=end_utc,
            planned_place={
                'canonical_name': 'UTC Test Shop',
                'opening_spec': spec,
                'lat': 35.68, 'lng': 139.76,
            },
        )]

        now_utc = datetime(2026, 10, 18, 2, 0, tzinfo=UTC)  # Sunday 10:00 CN_TZ
        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now_utc)
        poi_r = [r for r in results if r['rule'] == 'poi_closed']
        assert len(poi_r) == 1, 'Should detect closed using CN_TZ weekday'

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_no_poi_untouched(self, mock_events, mock_commit):
        """Event without planned_place → skip."""
        now = _ts(10)
        events = [_make_event(14, 14, 15)]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        assert len(results) == 0

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_only_first_closed_event_processed(self, mock_events, mock_commit):
        """Each rule fires on at most one event per tick."""
        from opening_hours import parse_opening_hours
        spec = parse_opening_hours('Mo-Fr 09:00-17:00; Sa,Su off')  # closed weekends

        sunday = datetime(2026, 10, 18, 10, 0, tzinfo=CN_TZ)
        events = [
            _make_event(20, 14, 15, fixedness='optional',
                        planned_start_at=sunday.replace(hour=14),
                        planned_end_at=sunday.replace(hour=15),
                        planned_place={'canonical_name': 'A', 'opening_spec': spec,
                                       'lat': 35.68, 'lng': 139.76}),
            _make_event(21, 16, 17, fixedness='optional',
                        planned_start_at=sunday.replace(hour=16),
                        planned_end_at=sunday.replace(hour=17),
                        planned_place={'canonical_name': 'B', 'opening_spec': spec,
                                       'lat': 35.69, 'lng': 139.77}),
        ]

        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', sunday.replace(hour=10))
        # poi_closed fires only once (on event 20); event 21 goes to
        # unreachable/fatigue only after refresh (where it may also fire)
        poi_results = [r for r in results if r['rule'] == 'poi_closed']
        assert len(poi_results) == 1
        assert poi_results[0]['event_id'] == 20


# ─────────────────────────────────────────────
#  Test: unreachable rule
# ─────────────────────────────────────────────

class TestRuleUnreachable:

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_unreachable_pair_relocated(self, mock_events, mock_commit):
        """Two POIs far apart with a short gap → relocate the later one."""
        now = _ts(10)
        # Shibuya → Yokohama ≈ 30 km, with only 0 min gap
        events = [
            _make_event(30, 12, 13, status='completed',
                        actual_start_at=_ts(12), actual_end_at=_ts(13),
                        planned_place={'lat': 35.6580, 'lng': 139.7016}),  # Shibuya
            _make_event(31, 13, 14, status='planned',  # 0 min gap!
                        planned_place={'lat': 35.4437, 'lng': 139.6380}),  # Yokohama
        ]
        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        unreach = [r for r in results if r['rule'] == 'unreachable']
        assert len(unreach) == 1
        assert unreach[0]['event_id'] == 31
        assert unreach[0]['action'] == 'relocate'

        intent = mock_commit.call_args[0][2]
        assert intent['type'] == 'relocate'
        assert intent['reason'] == 'unreachable'
        assert 'planned_place' not in intent

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_reachable_pair_untouched(self, mock_events, mock_commit):
        """Two close POIs with adequate gap → no action."""
        now = _ts(10)
        # Two places in Shibuya, ~2km apart, 60 min gap
        events = [
            _make_event(32, 12, 13, status='completed',
                        actual_start_at=_ts(12), actual_end_at=_ts(13),
                        planned_place={'lat': 35.6580, 'lng': 139.7016}),
            _make_event(33, 14, 15, status='planned',
                        planned_place={'lat': 35.6620, 'lng': 139.7050}),
        ]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        unreach = [r for r in results if r['rule'] == 'unreachable']
        assert len(unreach) == 0

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_missing_coords_skipped(self, mock_events, mock_commit):
        """POI without lat/lng → skip check."""
        now = _ts(10)
        events = [
            _make_event(34, 12, 13, status='completed',
                        planned_place={'canonical_name': 'A'}),
            _make_event(35, 13, 14, status='planned',
                        planned_place={'canonical_name': 'B'}),
        ]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        assert len([r for r in results if r['rule'] == 'unreachable']) == 0

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_only_planned_event_relocated(self, mock_events, mock_commit):
        """Only the planned (later) event is modified, not the completed one."""
        now = _ts(10)
        events = [
            _make_event(36, 12, 13, status='completed',
                        actual_start_at=_ts(12), actual_end_at=_ts(13),
                        planned_place={'lat': 35.6580, 'lng': 139.7016}),
            _make_event(37, 13, 14, status='planned',
                        planned_place={'lat': 35.4437, 'lng': 139.6380}),
        ]
        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        schedule_revision.revise_schedule('c1', 'u1', now)
        intent = mock_commit.call_args[0][2]
        assert intent['event_id'] == 37  # the planned one, not 36


# ─────────────────────────────────────────────
#  Test: fatigue rule
# ─────────────────────────────────────────────

class TestRuleFatigue:

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_over_threshold_optional_cancelled(self, mock_events, mock_commit):
        """Cumulative hard_busy > 360 min → cancel next optional event."""
        now = _ts(18)
        events = [
            # 7 hours of hard_busy (420 min > 360 threshold)
            _make_event(40, 8, 15, status='completed', reply_state='hard_busy',
                        actual_start_at=_ts(8), actual_end_at=_ts(15)),
            # Future optional event
            _make_event(41, 19, 20, status='planned', fixedness='optional'),
        ]
        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        fatigue = [r for r in results if r['rule'] == 'fatigue']
        assert len(fatigue) == 1
        assert fatigue[0]['event_id'] == 41
        assert fatigue[0]['action'] == 'cancel'

        intent = mock_commit.call_args[0][2]
        assert intent['reason'] == 'fatigue'

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_under_threshold_untouched(self, mock_events, mock_commit):
        """Cumulative hard_busy < 360 min → no action."""
        now = _ts(14)
        events = [
            _make_event(42, 8, 12, status='completed', reply_state='hard_busy',
                        actual_start_at=_ts(8), actual_end_at=_ts(12)),  # 240 min
            _make_event(43, 15, 16, status='planned', fixedness='optional'),
        ]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        assert len([r for r in results if r['rule'] == 'fatigue']) == 0

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_active_event_capped_at_now(self, mock_events, mock_commit):
        """Active hard_busy event: only count elapsed time up to now, not planned_end."""
        # Event runs 08:00-20:00 (12h) but now is 14:00 → only 6h elapsed (360 min)
        # 360 == threshold exactly, so NOT over threshold → no cancel
        now = _ts(14)
        events = [
            _make_event(70, 8, 20, status='active', reply_state='hard_busy',
                        actual_start_at=_ts(8), actual_end_at=None,
                        planned_end_at=_ts(20)),
            _make_event(71, 21, 22, status='planned', fixedness='optional'),
        ]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        # 6h = 360 min = threshold, not OVER → no fatigue
        assert len([r for r in results if r['rule'] == 'fatigue']) == 0

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_active_event_over_threshold_when_capped(self, mock_events, mock_commit):
        """Active hard_busy: elapsed time > threshold → cancel."""
        # Event runs 08:00-22:00 but now is 14:30 → 6.5h = 390 min > 360
        now = _ts(14, 30)
        events = [
            _make_event(72, 8, 22, status='active', reply_state='hard_busy',
                        actual_start_at=_ts(8), actual_end_at=None,
                        planned_end_at=_ts(22)),
            _make_event(73, 23, 23, status='planned', fixedness='optional',
                        planned_start_at=_ts(23), planned_end_at=_ts(23, day=17)),
        ]
        mock_events.return_value = events
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        fatigue = [r for r in results if r['rule'] == 'fatigue']
        assert len(fatigue) == 1

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_no_optional_even_fatigued(self, mock_events, mock_commit):
        """Over threshold but no optional/flavor events → no action."""
        now = _ts(18)
        events = [
            _make_event(44, 8, 15, status='completed', reply_state='hard_busy',
                        actual_start_at=_ts(8), actual_end_at=_ts(15)),
            # Only fixed/flexible remaining
            _make_event(45, 19, 20, status='planned', fixedness='flexible'),
        ]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        assert len([r for r in results if r['rule'] == 'fatigue']) == 0

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_free_events_not_counted(self, mock_events, mock_commit):
        """Events with reply_state != hard_busy don't count toward fatigue."""
        now = _ts(18)
        events = [
            _make_event(46, 8, 15, status='completed', reply_state='free',
                        actual_start_at=_ts(8), actual_end_at=_ts(15)),
            _make_event(47, 19, 20, status='planned', fixedness='optional'),
        ]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        assert len([r for r in results if r['rule'] == 'fatigue']) == 0


# ─────────────────────────────────────────────
#  Test: post-commit state refresh
# ─────────────────────────────────────────────

class TestPostCommitRefresh:
    """After a rule commits, subsequent rules must see the updated state."""

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_cancelled_event_not_seen_by_later_rules(self, mock_events, mock_commit):
        """poi_closed cancels event → unreachable should not see it as planned."""
        from opening_hours import parse_opening_hours
        spec = parse_opening_hours('Mo-Fr 09:00-17:00; Sa,Su off')

        sunday = datetime(2026, 10, 18, 10, 0, tzinfo=CN_TZ)

        event_before_cancel = _make_event(
            80, 14, 15, fixedness='optional',
            planned_start_at=sunday.replace(hour=14),
            planned_end_at=sunday.replace(hour=15),
            planned_place={
                'canonical_name': 'Closed Shop',
                'opening_spec': spec,
                'lat': 35.68, 'lng': 139.76,
            },
        )
        # Second event far away — would be unreachable from event 80
        event_after = _make_event(
            81, 15, 16, fixedness='flexible',
            planned_start_at=sunday.replace(hour=15),
            planned_end_at=sunday.replace(hour=16),
            planned_place={'lat': 35.44, 'lng': 139.64},  # ~30km away
        )

        # First call: both events
        # Second call (after cancel): only event 81 remains as planned
        event_after_refresh = _make_event(
            81, 15, 16, fixedness='flexible',
            planned_start_at=sunday.replace(hour=15),
            planned_end_at=sunday.replace(hour=16),
            planned_place={'lat': 35.44, 'lng': 139.64},
        )
        mock_events.side_effect = [
            [event_before_cancel, event_after],          # initial fetch
            [event_after_refresh],                        # after poi_closed commit
        ]
        mock_commit.return_value = {'ok': True}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', sunday.replace(hour=10))

        # poi_closed should fire on event 80
        poi_r = [r for r in results if r['rule'] == 'poi_closed']
        assert len(poi_r) == 1
        assert poi_r[0]['event_id'] == 80

        # unreachable should NOT fire because after refresh, event 80 is gone
        # and event 81 has no predecessor with coords
        unreach = [r for r in results if r['rule'] == 'unreachable']
        assert len(unreach) == 0

        # _get_today_events called twice (initial + after poi_closed)
        assert mock_events.call_count == 2


# ─────────────────────────────────────────────
#  Test: edge cases
# ─────────────────────────────────────────────

class TestEdgeCases:

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_no_planned_events(self, mock_events, mock_commit):
        """All events completed → empty result."""
        now = _ts(18)
        events = [
            _make_event(50, 8, 12, status='completed'),
            _make_event(51, 13, 17, status='completed'),
        ]
        mock_events.return_value = events

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', now)
        assert results == []
        mock_commit.assert_not_called()

    @patch('schedule_revision._get_today_events')
    def test_empty_schedule(self, mock_events):
        """No events at all → empty result."""
        mock_events.return_value = []

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', _ts(10))
        assert results == []

    @patch('schedule_revision._commit')
    @patch('schedule_revision._get_today_events')
    def test_commit_failure_still_returns(self, mock_events, mock_commit):
        """If commit fails (stale revision), the result records ok=False."""
        from opening_hours import parse_opening_hours
        spec = parse_opening_hours('Mo-Fr 09:00-17:00; Sa,Su off')

        sunday = datetime(2026, 10, 18, 10, 0, tzinfo=CN_TZ)
        events = [_make_event(
            52, 14, 15, fixedness='optional',
            planned_start_at=sunday.replace(hour=14),
            planned_end_at=sunday.replace(hour=15),
            planned_place={'canonical_name': 'X', 'opening_spec': spec,
                           'lat': 35.68, 'lng': 139.76},
        )]
        mock_events.return_value = events
        mock_commit.return_value = {'ok': False, 'reason': 'stale_event_revision'}

        import schedule_revision
        results = schedule_revision.revise_schedule('c1', 'u1', sunday.replace(hour=10))
        assert len(results) >= 1
        poi_r = [r for r in results if r['rule'] == 'poi_closed']
        assert len(poi_r) == 1
        assert poi_r[0]['ok'] is False


# ─────────────────────────────────────────────
#  Test: architecture invariants
# ─────────────────────────────────────────────

class TestArchitectureInvariants:
    """schedule_revision must be zero-LLM, zero-external-API."""

    def test_no_llm_imports(self):
        """Module source must not import any model/LLM client."""
        import ast
        import inspect
        import schedule_revision
        source = inspect.getsource(schedule_revision)
        tree = ast.parse(source)
        banned = {'ai_client', 'anthropic', 'openai', 'get_anthropic',
                  'route_chat', 'cognitive'}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name not in banned, f'banned import: {alias.name}'
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module not in banned, f'banned import: {node.module}'

    def test_no_requests_import(self):
        """Module must not make HTTP requests (zero external API)."""
        import ast
        import inspect
        import schedule_revision
        source = inspect.getsource(schedule_revision)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != 'requests', 'must not import requests'
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module != 'requests', 'must not import requests'

    def test_haversine_self_contained(self):
        """_haversine_km should produce correct results."""
        import schedule_revision
        # Tokyo Station to Shibuya Station ≈ 6-7 km
        dist = schedule_revision._haversine_km(35.6812, 139.7671, 35.6580, 139.7016)
        assert 5.0 < dist < 8.0

    def test_revision_reasons_match(self):
        """Rule IDs in schedule_revision must be valid REVISION_REASONS."""
        from schedule_contract import REVISION_REASONS
        import schedule_revision
        for rule_id in (schedule_revision.RULE_POI_CLOSED,
                        schedule_revision.RULE_UNREACHABLE,
                        schedule_revision.RULE_FATIGUE):
            assert rule_id in REVISION_REASONS, f'{rule_id} not in REVISION_REASONS'

    def test_get_opening_spec_exists(self):
        """_get_opening_spec must be a callable in schedule_revision."""
        import schedule_revision
        assert callable(schedule_revision._get_opening_spec)


@pytest.fixture
def revision_database(monkeypatch):
    """Exercise the canonical SQL writer against an isolated in-memory database."""
    if not os.environ.get('COGNITIVE_TEST_PGLITE'):
        pytest.skip('COGNITIVE_TEST_PGLITE is required for canonical SQL coverage')
    from tests.offline_pg import Connection
    import db
    import db_schedule

    connection = Connection()
    monkeypatch.setattr(db, 'get_conn', lambda: connection)
    monkeypatch.setattr(db_schedule, 'get_conn', lambda: connection)
    try:
        db_schedule.init_schedule_table()
        yield connection
    finally:
        connection.rollback()
        connection.shutdown()


def _verified_revision_place(place_id='closed', hours='Fr off', *, lat=35.68):
    return {
        'provider': 'osm', 'provider_place_id': place_id,
        'canonical_name': f'Place {place_id}', 'canonical_address': 'Tokyo',
        'lat': lat, 'lng': 139.76, 'verified_category': 'cafe',
        'provider_raw_type': {'tags': {'opening_hours': hours}},
        'fetched_at': _ts(9).isoformat(),
    }


def _save_revision_events(items, now=None):
    import db_schedule
    return db_schedule.save_canonical_schedule(
        'c1', 'u1', _ts(10).date(), items, now=now or _ts(10))


class TestCanonicalRevisionPersistence:
    def test_cancel_persists_reason_phases_and_phone_reconciliation_once(self, revision_database):
        import schedule_revision

        event = _save_revision_events([{
            'start_time': '14:00', 'end_time': '15:00', 'title': 'Closed visit',
            'fixedness': 'optional', 'planned_place': _verified_revision_place(),
        }])[0]
        assert 'opening_spec' not in event['planned_place']
        phase_id = event['phases'][0]['id']
        for state, pending in [('pending', 1), ('processing', 1), ('pending', 0)]:
            revision_database.query('''INSERT INTO char_phone_check
                (user_id, character_id, sched_date, start_time, end_time,
                 schedule_event_id, phase_id, event_revision, check_state,
                 pending_count, next_phone_check_at, claim_token)
                VALUES ('u1','c1',%s,'14:00','15:00',%s,%s,1,%s,%s,%s,%s)''',
                (_ts(10).date(), event['id'], phase_id, state, pending,
                 _ts(14), 'frozen' if state == 'processing' else None))

        first = schedule_revision.revise_schedule('c1', 'u1', _ts(10))
        assert [(r['rule'], r['action'], r['ok']) for r in first] == [
            ('poi_closed', 'cancel', True)]
        assert schedule_revision.revise_schedule('c1', 'u1', _ts(10)) == []

        stored = revision_database.query(
            'SELECT status, revision FROM char_schedule')['rows'][0]
        assert stored == {'status': 'cancelled', 'revision': 2}
        phases = revision_database.query(
            'SELECT status, revision FROM char_schedule_phase')['rows']
        assert phases and all(p == {'status': 'cancelled', 'revision': 2} for p in phases)
        transitions = revision_database.query('''SELECT intent, source_event_id,
            prior_revision, committed_revision FROM char_schedule_transition''')['rows']
        assert len(transitions) == 1
        assert transitions[0]['intent']['reason'] == 'poi_closed'
        assert transitions[0]['source_event_id'] == 'tick_revision'
        assert transitions[0]['prior_revision'] == 1
        assert transitions[0]['committed_revision'] == 2

        cur = revision_database.cursor()
        cur.execute('''SELECT check_state, pending_count, next_phone_check_at,
                       claim_token FROM char_phone_check ORDER BY id''')
        assert cur.fetchall() == [
            ('pending', 1, _ts(10), None),
            ('processing', 1, _ts(14), 'frozen'),
            ('superseded', 0, _ts(14), None),
        ]
        cur.close()

    def test_empty_place_relocation_clears_event_and_phases_once(self, revision_database):
        import schedule_revision

        event = _save_revision_events([{
            'start_time': '14:00', 'end_time': '15:00', 'title': 'Closed visit',
            'fixedness': 'flexible', 'planned_place': _verified_revision_place(),
        }])[0]
        result = schedule_revision.revise_schedule('c1', 'u1', _ts(10))
        assert [(r['event_id'], r['action'], r['ok']) for r in result] == [
            (event['id'], 'relocate', True)]
        assert schedule_revision.revise_schedule('c1', 'u1', _ts(10)) == []
        stored = revision_database.query('''SELECT status, revision, location,
            planned_place FROM char_schedule''')['rows'][0]
        assert stored == {'status': 'planned', 'revision': 2,
                          'location': '一般区域', 'planned_place': None}
        phases = revision_database.query('''SELECT status, revision, planned_place
            FROM char_schedule_phase''')['rows']
        assert phases and all(p == {'status': 'planned', 'revision': 2,
                                   'planned_place': None} for p in phases)
        transitions = revision_database.query(
            'SELECT intent FROM char_schedule_transition')['rows']
        assert len(transitions) == 1
        assert transitions[0]['intent']['reason'] == 'poi_closed'
        assert 'planned_place' not in transitions[0]['intent']

    @pytest.mark.parametrize('hours, closed', [
        ('Fr 05:00-10:00', True), ('Fr 09:00-17:00', False),
    ])
    @pytest.mark.parametrize('now', [_ts(10), _ts(1).astimezone(timezone.utc)])
    def test_persisted_raw_hours_use_local_time_for_utc_database_rows(
            self, revision_database, hours, closed, now):
        import schedule_revision

        event = _save_revision_events([{
            'start_time': '14:00', 'end_time': '15:00', 'title': 'Local hours',
            'fixedness': 'optional',
            'planned_place': _verified_revision_place(hours=hours),
        }])[0]
        assert 'opening_spec' not in event['planned_place']
        cur = revision_database.cursor()
        cur.execute('SELECT planned_start_at FROM char_schedule')
        assert cur.fetchone()[0].hour == 6
        cur.close()
        result = schedule_revision.revise_schedule('c1', 'u1', now)
        assert [(r['rule'], r['ok']) for r in result] == (
            [('poi_closed', True)] if closed else [])

    @pytest.mark.parametrize('fixedness, action', [
        ('optional', 'cancel'), ('flexible', 'relocate'),
    ])
    def test_later_rule_does_not_use_place_removed_by_earlier_rule(
            self, revision_database, fixedness, action):
        import schedule_revision

        events = _save_revision_events([
            {'start_time': '14:00', 'end_time': '15:00', 'title': 'Closed visit',
             'fixedness': fixedness, 'planned_place': _verified_revision_place()},
            {'start_time': '15:00', 'end_time': '16:00', 'title': 'Open visit',
             'fixedness': 'flexible',
             'planned_place': _verified_revision_place('open', '24/7', lat=36.68)},
        ])
        result = schedule_revision.revise_schedule('c1', 'u1', _ts(10))
        assert [(r['rule'], r['event_id'], r['action'], r['ok']) for r in result] == [
            ('poi_closed', events[0]['id'], action, True)]
        second = revision_database.query('''SELECT revision, planned_place
            FROM char_schedule WHERE id=%s''', (events[1]['id'],))['rows'][0]
        assert second['revision'] == 1
        assert second['planned_place']['provider_place_id'] == 'open'

    def test_fatigue_uses_revision_committed_by_unreachable_rule(self, revision_database):
        import schedule_revision

        events = _save_revision_events([
            {'start_time': '05:00', 'end_time': '12:00', 'title': 'Hard work',
             'reply_state': 'hard_busy', 'fixedness': 'fixed'},
            {'start_time': '14:00', 'end_time': '15:00', 'title': 'First visit',
             'fixedness': 'flexible',
             'planned_place': _verified_revision_place('first', '24/7')},
            {'start_time': '15:00', 'end_time': '16:00', 'title': 'Optional visit',
             'fixedness': 'optional',
             'planned_place': _verified_revision_place('far', '24/7', lat=36.68)},
        ], now=_ts(13))
        assert events[0]['status'] == 'completed'
        event_id = events[2]['id']
        results = schedule_revision.revise_schedule('c1', 'u1', _ts(13))
        assert [(r['rule'], r['event_id'], r['action'], r['ok']) for r in results] == [
            ('unreachable', event_id, 'relocate', True),
            ('fatigue', event_id, 'cancel', True),
        ]
        transitions = revision_database.query('''SELECT intent, prior_revision,
            committed_revision FROM char_schedule_transition
            WHERE schedule_id=%s ORDER BY id''', (event_id,))['rows']
        assert [(row['intent']['reason'], row['prior_revision'], row['committed_revision'])
                for row in transitions] == [('unreachable', 1, 2), ('fatigue', 2, 3)]
        stored = revision_database.query('''SELECT status, revision, planned_place
            FROM char_schedule WHERE id=%s''', (event_id,))['rows'][0]
        assert stored == {'status': 'cancelled', 'revision': 3, 'planned_place': None}


@pytest.mark.parametrize('now, should_cancel', [
    (_ts(13, 59), False), (_ts(14), False), (_ts(14, 1), True),
])
def test_active_fatigue_counts_elapsed_minutes_only(now, should_cancel):
    import schedule_revision

    events = [
        _make_event(60, 8, 16, status='active', reply_state='hard_busy',
                    actual_start_at=_ts(8)),
        _make_event(61, 17, 18, fixedness='optional'),
    ]
    with patch.object(schedule_revision, '_get_today_events', return_value=events), \
            patch.object(schedule_revision, '_commit', return_value={'ok': True}) as commit:
        results = schedule_revision.revise_schedule('c1', 'u1', now)
    assert [(r['rule'], r['event_id']) for r in results] == (
        [('fatigue', 61)] if should_cancel else [])
    assert commit.call_count == int(should_cancel)


@pytest.mark.parametrize('revision_fails', [False, True])
def test_scheduler_orders_revision_and_isolates_character_failure(revision_fails):
    from contextlib import ExitStack
    import diary_scheduler
    import schedule_revision

    calls = []

    def revise(character_id, user_id):
        assert user_id == diary_scheduler.TARGET_USER
        calls.append(('revise', character_id))
        if revision_fails and character_id == 'c1':
            raise RuntimeError('isolated revision failure')
        return []

    with ExitStack() as stack:
        stack.enter_context(patch.object(diary_scheduler, '_get_active_character_ids',
                                         return_value=['c1', 'c2']))
        stack.enter_context(patch.object(diary_scheduler, '_maybe_prefetch_pois'))
        stack.enter_context(patch.object(schedule_revision, 'revise_schedule', side_effect=revise))
        worker = stack.enter_context(patch.object(diary_scheduler.threading, 'Thread'))
        for name in ('generate_schedule', 'write_diary', 'peek_diary', 'react_to_comments'):
            stack.enter_context(patch.object(
                diary_scheduler, '_maybe_' + name,
                side_effect=lambda cid, name=name: calls.append((name, cid))))
        diary_scheduler._tick()
        worker.assert_not_called()
    assert calls == [
        (action, cid) for cid in ('c1', 'c2')
        for action in ('generate_schedule', 'revise', 'write_diary',
                       'peek_diary', 'react_to_comments')
    ]
