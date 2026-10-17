"""test_opening_hours.py — opening_hours 解析器单元测试

覆盖:
  - 常见格式解析 (24/7, Mo-Fr, Sa,Su, off, 跨午夜)
  - is_definitely_closed 判定
  - 边界条件 (None, 空字符串, 不支持的语法)
  - 集成: validate_nominatim_result 输出包含 opening_spec
  - 集成: _sanitize 过滤关门 POI
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gojo_backend'))


class TestParseOpeningHours:
    """Test parse_opening_hours()."""

    def test_none_input(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours(None)
        assert result['status'] == 'unknown'

    def test_empty_string(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('')
        assert result['status'] == 'unknown'

    def test_24_7(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('24/7')
        assert result['status'] == 'always_open'
        assert result['raw'] == '24/7'

    def test_weekday_range(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('Mo-Fr 09:00-18:00')
        assert result['status'] == 'parsed'
        assert len(result['rules']) == 1
        rule = result['rules'][0]
        assert rule['days'] == [0, 1, 2, 3, 4]  # Mon-Fri
        assert rule['ranges'] == [(540, 1080)]  # 09:00-18:00
        assert rule['off'] is False

    def test_weekend(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('Sa,Su 10:00-22:00')
        assert result['status'] == 'parsed'
        rule = result['rules'][0]
        assert rule['days'] == [5, 6]  # Sat, Sun

    def test_day_off(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('Tu off')
        assert result['status'] == 'parsed'
        rule = result['rules'][0]
        assert rule['days'] == [1]  # Tuesday
        assert rule['off'] is True

    def test_multiple_rules(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('Mo-Fr 09:00-18:00; Sa 10:00-14:00')
        assert result['status'] == 'parsed'
        assert len(result['rules']) == 2
        assert result['rules'][0]['days'] == [0, 1, 2, 3, 4]
        assert result['rules'][1]['days'] == [5]
        assert result['rules'][1]['ranges'] == [(600, 840)]

    def test_every_day(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('Mo-Su 11:00-23:00')
        assert result['status'] == 'parsed'
        rule = result['rules'][0]
        assert rule['days'] == [0, 1, 2, 3, 4, 5, 6]

    def test_overnight_range(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('Mo-Su 22:00-04:00')
        assert result['status'] == 'parsed'
        rule = result['rules'][0]
        assert rule['ranges'] == [(1320, 240)]  # 22:00-04:00

    def test_complex_unsupported_falls_to_unknown(self):
        from opening_hours import parse_opening_hours
        # OSM has many complex formats we don't support
        result = parse_opening_hours('Mo-Fr 09:00-12:00,13:00-18:00; PH off')
        assert result['status'] == 'unknown'  # 'PH off' not parseable

    def test_freeform_text_unknown(self):
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('sunrise-sunset')
        assert result['status'] == 'unknown'

    def test_split_time_ranges(self):
        """Mo-Fr 09:00-12:00,13:00-18:00 — lunch break."""
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('Mo-Fr 09:00-12:00,13:00-18:00')
        assert result['status'] == 'parsed'
        rule = result['rules'][0]
        assert len(rule['ranges']) == 2
        assert rule['ranges'][0] == (540, 720)   # 09:00-12:00
        assert rule['ranges'][1] == (780, 1080)  # 13:00-18:00

    def test_wrap_around_days(self):
        """Fr-Mo should be Fri, Sat, Sun, Mon."""
        from opening_hours import parse_opening_hours
        result = parse_opening_hours('Fr-Mo 10:00-20:00')
        assert result['status'] == 'parsed'
        rule = result['rules'][0]
        assert sorted(rule['days']) == [0, 4, 5, 6]


class TestIsDefinitelyClosed:
    """Test is_definitely_closed()."""

    def test_unknown_never_filters(self):
        from opening_hours import is_definitely_closed
        spec = {'status': 'unknown', 'raw': None, 'rules': []}
        assert is_definitely_closed(spec, 0, 540) is False

    def test_always_open_never_filters(self):
        from opening_hours import is_definitely_closed
        spec = {'status': 'always_open', 'raw': '24/7', 'rules': []}
        assert is_definitely_closed(spec, 3, 180) is False  # 3am Thursday

    def test_none_spec_never_filters(self):
        from opening_hours import is_definitely_closed
        assert is_definitely_closed(None, 0, 540) is False

    def test_open_during_hours(self):
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Fr 09:00-18:00')
        # Wednesday 10:00 → open
        assert is_definitely_closed(spec, 2, 600) is False

    def test_closed_before_hours(self):
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Fr 09:00-18:00')
        # Wednesday 03:00 → closed
        assert is_definitely_closed(spec, 2, 180) is True

    def test_closed_after_hours(self):
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Fr 09:00-18:00')
        # Wednesday 21:00 → closed
        assert is_definitely_closed(spec, 2, 1260) is True

    def test_day_off(self):
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Fr 09:00-18:00; Tu off')
        # Tuesday 10:00 → off (later rule overrides)
        assert is_definitely_closed(spec, 1, 600) is True
        # Monday 10:00 → open
        assert is_definitely_closed(spec, 0, 600) is False

    def test_uncovered_day_not_filtered(self):
        """Saturday not in Mo-Fr rule → conservative, don't filter."""
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Fr 09:00-18:00')
        # Saturday 10:00 → no rule covers Saturday → don't filter
        assert is_definitely_closed(spec, 5, 600) is False

    def test_overnight_open(self):
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Su 22:00-04:00')
        # 23:00 → open
        assert is_definitely_closed(spec, 3, 1380) is False
        # 02:00 → open
        assert is_definitely_closed(spec, 3, 120) is False
        # 12:00 → closed
        assert is_definitely_closed(spec, 3, 720) is True

    def test_boundary_start(self):
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Fr 09:00-18:00')
        # Exactly 09:00 → open (start is inclusive)
        assert is_definitely_closed(spec, 0, 540) is False

    def test_boundary_end(self):
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Fr 09:00-18:00')
        # Exactly 18:00 → closed (end is exclusive)
        assert is_definitely_closed(spec, 0, 1080) is True

    def test_lunch_break_closed(self):
        from opening_hours import parse_opening_hours, is_definitely_closed
        spec = parse_opening_hours('Mo-Fr 09:00-12:00,13:00-18:00')
        # 12:30 → closed (lunch break)
        assert is_definitely_closed(spec, 0, 750) is True
        # 09:30 → open
        assert is_definitely_closed(spec, 0, 570) is False
        # 14:00 → open
        assert is_definitely_closed(spec, 0, 840) is False


class TestValidateNominatimResultOpeningSpec:
    """Verify opening_spec appears in validated POI dict."""

    def _make_nominatim_item(self, opening_hours=None):
        tags = {'name': 'テスト喫茶店'}
        if opening_hours:
            tags['opening_hours'] = opening_hours
        return {
            'place_id': '12345',
            'class': 'amenity',
            'type': 'cafe',
            'display_name': 'テスト喫茶店, 渋谷区, 東京都, 日本',
            'lat': '35.6580',
            'lon': '139.7016',
            'osm_type': 'node',
            'osm_id': '999',
            'extratags': tags,
            'namedetails': {'name': 'テスト喫茶店'},
        }

    def test_opening_spec_present_when_hours_available(self):
        from places_engine import validate_nominatim_result
        item = self._make_nominatim_item('Mo-Fr 09:00-18:00')
        result = validate_nominatim_result(item, 'cafe', 'tokyo')
        assert result is not None
        assert 'opening_spec' in result
        assert result['opening_spec']['status'] == 'parsed'

    def test_opening_spec_none_when_no_hours(self):
        from places_engine import validate_nominatim_result
        item = self._make_nominatim_item()
        result = validate_nominatim_result(item, 'cafe', 'tokyo')
        assert result is not None
        assert result.get('opening_spec') is None

    def test_opening_spec_unknown_when_unparseable(self):
        from places_engine import validate_nominatim_result
        item = self._make_nominatim_item('sunrise-sunset')
        result = validate_nominatim_result(item, 'cafe', 'tokyo')
        assert result is not None
        spec = result.get('opening_spec')
        # Either None or unknown — both are fine
        if spec is not None:
            assert spec['status'] == 'unknown'


class TestSanitizeOpeningHoursFiltering:
    """Verify _sanitize filters POIs that are definitely closed."""

    def _make_candidate(self, opening_hours=None):
        spec = None
        if opening_hours:
            from opening_hours import parse_opening_hours
            spec = parse_opening_hours(opening_hours)
        return {
            'provider': 'nominatim',
            'provider_place_id': 'test_poi_001',
            'canonical_name': '测试咖啡厅',
            'canonical_address': '渋谷区',
            'lat': 35.658,
            'lng': 139.7016,
            'verified_category': 'cafe',
            'provider_raw_type': {},
            'fetched_at': '2026-01-01T00:00:00Z',
            'name': '测试咖啡厅',
            'address': '渋谷区',
            'category': 'cafe',
            'category_label': '咖啡厅',
            'city': 'tokyo',
            'opening_spec': spec,
        }

    def test_closed_poi_falls_back_to_general_area(self):
        """POI with Mo-Fr 09:00-18:00, schedule at 03:00 on Monday → filtered."""
        from datetime import date
        from schedule_engine import _sanitize
        candidates = [self._make_candidate('Mo-Fr 09:00-18:00')]
        raw_items = [{
            'start_time': '03:00',
            'end_time': '04:00',
            'title': '深夜散步',
            'location': '渋谷',
            'note': '睡不着',
            'category': 'leisure',
            'fixedness': 'optional',
            'reply_state': 'free',
            'planned_place_ref': 'nominatim:test_poi_001',
        }]
        # Monday
        target = date(2026, 10, 19)  # a Monday
        result = _sanitize(raw_items, poi_candidates=candidates, target_date=target)
        assert len(result) == 1
        # Should NOT have planned_place (filtered out)
        assert result[0].get('planned_place') is None
        # location should be general area, not the POI name
        assert result[0]['location'] != '测试咖啡厅'

    def test_open_poi_kept(self):
        """POI with Mo-Fr 09:00-18:00, schedule at 10:00 on Monday → kept."""
        from datetime import date
        from schedule_engine import _sanitize
        candidates = [self._make_candidate('Mo-Fr 09:00-18:00')]
        raw_items = [{
            'start_time': '10:00',
            'end_time': '11:00',
            'title': '喝咖啡',
            'location': '渋谷',
            'note': '好喝',
            'category': 'flavor',
            'fixedness': 'optional',
            'reply_state': 'free',
            'planned_place_ref': 'nominatim:test_poi_001',
        }]
        target = date(2026, 10, 19)  # Monday
        result = _sanitize(raw_items, poi_candidates=candidates, target_date=target)
        assert len(result) == 1
        # POI should be kept
        assert result[0].get('planned_place') is not None
        assert result[0]['location'] == '测试咖啡厅'

    def test_unknown_hours_not_filtered(self):
        """POI with unparseable hours should NOT be filtered."""
        from datetime import date
        from schedule_engine import _sanitize
        candidates = [self._make_candidate('sunrise-sunset')]
        raw_items = [{
            'start_time': '03:00',
            'end_time': '04:00',
            'title': '深夜散步',
            'location': '渋谷',
            'note': '',
            'category': 'leisure',
            'fixedness': 'optional',
            'reply_state': 'free',
            'planned_place_ref': 'nominatim:test_poi_001',
        }]
        target = date(2026, 10, 19)
        result = _sanitize(raw_items, poi_candidates=candidates, target_date=target)
        assert len(result) == 1
        # Unknown hours → not filtered, POI kept
        assert result[0].get('planned_place') is not None

    def test_no_spec_not_filtered(self):
        """POI with no opening_hours at all should NOT be filtered."""
        from datetime import date
        from schedule_engine import _sanitize
        candidates = [self._make_candidate()]  # No opening_hours
        raw_items = [{
            'start_time': '03:00',
            'end_time': '04:00',
            'title': '深夜散步',
            'location': '渋谷',
            'note': '',
            'category': 'leisure',
            'fixedness': 'optional',
            'reply_state': 'free',
            'planned_place_ref': 'nominatim:test_poi_001',
        }]
        target = date(2026, 10, 19)
        result = _sanitize(raw_items, poi_candidates=candidates, target_date=target)
        assert len(result) == 1
        assert result[0].get('planned_place') is not None


@pytest.mark.parametrize('raw', [
    'Mo 22:00-24:30', 'Mo 25:00-26:00', 'Mo 09:60-18:00',
    'Mo +9:00-18:00', 'Mo 09:0-18:00', 'Mo ０９:００-１８:００',
    'Mo, 09:00-18:00', 'Mo 09:00-18:00,',
    'Mo 09:00-18:00; PH off', 'Mo 09:00-18:00; sunrise-sunset',
])
def test_unsupported_spec_never_partially_filters(raw):
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours(raw)
    assert spec['status'] == 'unknown'
    assert spec['rules'] == []
    assert is_definitely_closed(spec, 0, 180) is False


@pytest.mark.parametrize('raw,weekday', [
    ('Tu off; Mo 22:00-02:00', 1),
    ('Tu 09:00-18:00; Mo 22:00-02:00', 1),
    ('Mo off; Su 22:00-02:00', 0),
])
def test_previous_day_overnight_is_not_falsely_closed(raw, weekday):
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours(raw)
    assert is_definitely_closed(spec, weekday, 60) is False
    assert is_definitely_closed(spec, weekday, 120) is True


def test_end_of_day_and_reopening_override():
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours('Mo off; Mo 09:00-24:00')
    assert spec['status'] == 'parsed'
    assert is_definitely_closed(spec, 0, 540) is False
    assert is_definitely_closed(spec, 0, 1439) is False
    assert is_definitely_closed(spec, 0, 539) is True


@pytest.mark.parametrize('raw', [42, [], {}, '   ', '; ;'])
def test_missing_or_non_string_hours_are_unknown(raw):
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours(raw)
    assert spec['status'] == 'unknown'
    assert is_definitely_closed(spec, 0, 180) is False


def test_nominatim_parser_failure_keeps_verified_poi(monkeypatch):
    import opening_hours
    from places_engine import validate_nominatim_result
    def fail(_raw):
        raise ValueError('unsupported provider hours')
    monkeypatch.setattr(opening_hours, 'parse_opening_hours', fail)
    item = TestValidateNominatimResultOpeningSpec()._make_nominatim_item('bad')
    result = validate_nominatim_result(item, 'cafe', 'tokyo')
    assert result['provider_place_id'] == '12345'
    assert result['opening_spec'] is None


def test_sanitize_uses_schedule_date_and_keeps_candidate_unchanged(monkeypatch):
    from copy import deepcopy
    from datetime import date, datetime
    import schedule_engine
    from config import CN_TZ
    candidate = TestSanitizeOpeningHoursFiltering()._make_candidate('Tu off')
    original = deepcopy(candidate)
    monkeypatch.setattr(schedule_engine, '_now', lambda: datetime(2026, 10, 19, tzinfo=CN_TZ))
    item = {'start_time': '10:00', 'end_time': '11:00', 'title': '散步',
            'location': '渋谷', 'planned_place_ref': 'nominatim:test_poi_001'}
    current = schedule_engine._sanitize([dict(item)], poi_candidates=[candidate])
    future = schedule_engine._sanitize([dict(item)], poi_candidates=[candidate],
                                       target_date=date(2026, 10, 20))
    assert current[0]['planned_place']['provider_place_id'] == 'test_poi_001'
    assert future[0]['location'] == '一般区域'
    assert 'planned_place' not in future[0]
    assert 'planned_place_ref' not in future[0]
    assert candidate == original


def test_sanitize_evaluation_failure_keeps_poi(monkeypatch):
    from datetime import date
    import opening_hours
    from schedule_engine import _sanitize
    candidate = TestSanitizeOpeningHoursFiltering()._make_candidate('Mo off')
    def fail(*_args):
        raise ValueError('invalid spec')
    monkeypatch.setattr(opening_hours, 'is_definitely_closed', fail)
    item = {'start_time': '10:00', 'end_time': '11:00', 'title': '散步',
            'location': '渋谷', 'planned_place_ref': 'nominatim:test_poi_001'}
    result = _sanitize([item], poi_candidates=[candidate], target_date=date(2026, 10, 19))
    assert result[0]['planned_place']['provider_place_id'] == 'test_poi_001'


def test_closed_poi_regression_with_preparsed_spec():
    # Direct reproduction does not depend on the new parser being importable.
    from datetime import date
    from schedule_engine import _sanitize
    candidate = TestSanitizeOpeningHoursFiltering()._make_candidate()
    candidate['opening_spec'] = {
        'status': 'parsed', 'raw': 'Mo-Fr 09:00-18:00',
        'rules': [{'days': [0, 1, 2, 3, 4], 'ranges': [(540, 1080)], 'off': False}],
    }
    item = {'start_time': '03:00', 'end_time': '04:00', 'title': '深夜散步',
            'location': '渋谷', 'planned_place_ref': 'nominatim:test_poi_001'}
    result = _sanitize([item], poi_candidates=[candidate], target_date=date(2026, 10, 19))
    assert len(result) == 1
    assert 'planned_place' not in result[0]
    assert result[0]['location'] == '一般区域'


def test_equal_endpoints_preserve_previous_day_full_day_tail():
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours('Tu off; Mo 09:00-09:00')
    assert is_definitely_closed(spec, 1, 480) is False
    assert is_definitely_closed(spec, 1, 540) is True


def test_later_day_off_overrides_previous_day_overnight():
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours('Mo 22:00-02:00; Tu off')
    assert is_definitely_closed(spec, 1, 60) is True


@pytest.mark.parametrize('raw', [
    'Mo 22:00-02:00; Tu 10:00-18:00',
    'Tu 10:00-18:00; Mo 22:00-02:00',
])
@pytest.mark.parametrize('time_minutes,closed', [
    (30, False), (60, False), (119, False), (120, True), (540, True),
])
def test_overnight_tail_with_daytime_rule_is_order_independent(raw, time_minutes, closed):
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours(raw)
    assert is_definitely_closed(spec, 1, time_minutes) is closed


@pytest.mark.parametrize('raw,weekday', [
    ('Fr-Sa 18:00-03:00; Su 12:00-22:00', 6),
    ('Su 22:00-02:00; Mo 10:00-18:00', 0),
])
def test_natural_order_overnight_tail_across_weekend(raw, weekday):
    from opening_hours import parse_opening_hours, is_definitely_closed
    assert is_definitely_closed(parse_opening_hours(raw), weekday, 60) is False


@pytest.mark.parametrize('override', ['Mo off', 'Mo 08:00-09:00'])
def test_overridden_previous_day_overnight_does_not_keep_poi_open(override):
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours('Tu 10:00-18:00; Mo 22:00-02:00; ' + override)
    assert is_definitely_closed(spec, 1, 60) is True


@pytest.mark.parametrize('time_minutes', [60, 120])
def test_uncovered_day_with_previous_overnight_is_never_filtered(time_minutes):
    from opening_hours import parse_opening_hours, is_definitely_closed
    spec = parse_opening_hours('Mo 22:00-02:00')
    assert is_definitely_closed(spec, 1, time_minutes) is False


@pytest.mark.parametrize('raw,day', [
    ('Mo 22:00-02:00; Tu 10:00-18:00', 20),
    ('Fr-Sa 18:00-03:00; Su 12:00-22:00', 18),
])
def test_sanitize_keeps_natural_order_overnight_poi(raw, day):
    from datetime import date
    from schedule_engine import _sanitize
    candidate = TestSanitizeOpeningHoursFiltering()._make_candidate(raw)
    item = {'start_time': '01:00', 'end_time': '01:30', 'title': '夜间散步',
            'location': '渋谷', 'planned_place_ref': 'nominatim:test_poi_001'}
    result = _sanitize([item], poi_candidates=[candidate],
                       target_date=date(2026, 10, day))
    assert result[0]['planned_place']['provider_place_id'] == 'test_poi_001'


@pytest.mark.parametrize('fixedness', ['flexible', 'optional'])
@pytest.mark.parametrize('raw,day', [
    ('Mo 22:00-02:00; Tu 10:00-18:00', 20),
    ('Fr-Sa 18:00-03:00; Su 12:00-22:00', 18),
])
def test_revision_does_not_commit_poi_closed_for_overnight_tail(monkeypatch, raw, day, fixedness):
    from copy import deepcopy
    from datetime import datetime
    from unittest.mock import Mock
    from config import CN_TZ
    import schedule_revision
    commit = Mock()
    monkeypatch.setattr(schedule_revision, '_commit', commit)
    now = datetime(2026, 10, day, 0, 30, tzinfo=CN_TZ)
    event = {
        'id': 1, 'revision': 1, 'fixedness': fixedness,
        'planned_start_at': now.replace(hour=1, minute=0),
        'planned_place': {'provider_raw_type': {'tags': {'opening_hours': raw}}},
    }
    original = deepcopy(event)
    assert schedule_revision._rule_poi_closed([event], 'c1', 'u1', now) is None
    commit.assert_not_called()
    assert event == original
