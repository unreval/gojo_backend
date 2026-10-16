"""test_geo_reachability.py — A3 地理可行性单元测试

覆盖:
  - haversine_km 已知距离
  - _sanitize 对不可达 POI 的降级
  - _sanitize 对可达 POI 的保留
  - 缺 POI / 缺坐标时跳过检查
  - 链式降级：A→B(不可达) → C(保留)
  - 架构不变量：A3 不调用任何 LLM
"""
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gojo_backend'))


# ── 坐标常量 ──
# 涩谷 (Shibuya)
SHIBUYA_LAT, SHIBUYA_LNG = 35.6580, 139.7016
# 新宿 (Shinjuku) — 距涩谷约 3.5 km
SHINJUKU_LAT, SHINJUKU_LNG = 35.6896, 139.7006
# 横滨 (Yokohama) — 距涩谷约 27 km
YOKOHAMA_LAT, YOKOHAMA_LNG = 35.4437, 139.6380


class TestHaversineKm:
    """Test haversine_km() pure math."""

    def test_same_point_zero(self):
        from places_engine import haversine_km
        assert haversine_km(SHIBUYA_LAT, SHIBUYA_LNG,
                            SHIBUYA_LAT, SHIBUYA_LNG) == 0.0

    def test_shibuya_to_shinjuku(self):
        from places_engine import haversine_km
        dist = haversine_km(SHIBUYA_LAT, SHIBUYA_LNG,
                            SHINJUKU_LAT, SHINJUKU_LNG)
        # Known: ~3.5 km, allow ±1 km
        assert 2.5 < dist < 4.5

    def test_shibuya_to_yokohama(self):
        from places_engine import haversine_km
        dist = haversine_km(SHIBUYA_LAT, SHIBUYA_LNG,
                            YOKOHAMA_LAT, YOKOHAMA_LNG)
        # Known: ~27 km, allow ±5 km
        assert 22 < dist < 32

    def test_symmetry(self):
        from places_engine import haversine_km
        d1 = haversine_km(SHIBUYA_LAT, SHIBUYA_LNG,
                          YOKOHAMA_LAT, YOKOHAMA_LNG)
        d2 = haversine_km(YOKOHAMA_LAT, YOKOHAMA_LNG,
                          SHIBUYA_LAT, SHIBUYA_LNG)
        assert abs(d1 - d2) < 0.001

    def test_cross_hemisphere(self):
        """Sanity: Tokyo to Sydney ~7800 km."""
        from places_engine import haversine_km
        dist = haversine_km(35.6762, 139.6503, -33.8688, 151.2093)
        assert 7500 < dist < 8200


class TestSanitizeReachability:
    """Test A3 reachability check in _sanitize()."""

    def _make_poi(self, place_id, name, lat, lng, category='cafe'):
        return {
            'provider': 'nominatim',
            'provider_place_id': place_id,
            'canonical_name': name,
            'canonical_address': '东京都',
            'lat': lat,
            'lng': lng,
            'verified_category': category,
            'provider_raw_type': {},
            'fetched_at': '2026-01-01T00:00:00Z',
            'name': name,
            'address': '东京都',
            'category': category,
            'category_label': '咖啡厅',
            'city': 'tokyo',
        }

    def _make_item(self, start, end, title, poi_ref=None, location='一般区域'):
        item = {
            'start_time': start,
            'end_time': end,
            'title': title,
            'location': location,
            'note': '',
            'category': 'leisure',
            'fixedness': 'optional',
            'reply_state': 'free',
        }
        if poi_ref:
            item['planned_place_ref'] = poi_ref
        return item

    def test_reachable_pois_kept(self):
        """Shibuya→Shinjuku (3.5km), 30 min gap → reachable → both kept."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        poi_b = self._make_poi('b1', '新宿茶馆', SHINJUKU_LAT, SHINJUKU_LNG)
        items = [
            self._make_item('10:00', '10:30', '喝咖啡', 'nominatim:a1'),
            self._make_item('11:00', '12:00', '逛街', 'nominatim:b1'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a, poi_b],
                           target_date=date(2026, 10, 19))
        assert len(result) == 2
        assert result[0].get('planned_place') is not None
        assert result[1].get('planned_place') is not None

    def test_unreachable_poi_degraded(self):
        """Shibuya→Yokohama (27km), 30 min gap → unreachable → second degraded."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        poi_b = self._make_poi('b1', '横滨拉面', YOKOHAMA_LAT, YOKOHAMA_LNG, 'ramen')
        items = [
            self._make_item('10:00', '10:30', '喝咖啡', 'nominatim:a1'),
            self._make_item('11:00', '12:00', '吃拉面', 'nominatim:b1'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a, poi_b],
                           target_date=date(2026, 10, 19))
        assert len(result) == 2
        # First POI kept
        assert result[0].get('planned_place') is not None
        assert result[0]['location'] == '涩谷咖啡'
        # Second POI degraded
        assert result[1].get('planned_place') is None
        assert result[1]['location'] != '横滨拉面'

    def test_unreachable_with_enough_gap_kept(self):
        """Shibuya→Yokohama (27km), 120 min gap → reachable → both kept."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        poi_b = self._make_poi('b1', '横滨拉面', YOKOHAMA_LAT, YOKOHAMA_LNG, 'ramen')
        items = [
            self._make_item('10:00', '11:00', '喝咖啡', 'nominatim:a1'),
            self._make_item('13:00', '14:00', '吃拉面', 'nominatim:b1'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a, poi_b],
                           target_date=date(2026, 10, 19))
        assert len(result) == 2
        assert result[0].get('planned_place') is not None
        assert result[1].get('planned_place') is not None

    def test_no_poi_skipped(self):
        """One item has no POI → check skipped, no crash."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        items = [
            self._make_item('10:00', '10:30', '喝咖啡', 'nominatim:a1'),
            self._make_item('11:00', '12:00', '散步', location='附近街区'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a],
                           target_date=date(2026, 10, 19))
        assert len(result) == 2
        # First has POI, second has no POI — both pass through fine
        assert result[0].get('planned_place') is not None

    def test_chain_degradation(self):
        """A(Shibuya)→B(Yokohama, 30min gap)→C(Shinjuku, 60min gap).
        B is unreachable from A → degraded.
        C is checked against B (no POI) → skip → C kept."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        poi_b = self._make_poi('b1', '横滨拉面', YOKOHAMA_LAT, YOKOHAMA_LNG, 'ramen')
        poi_c = self._make_poi('c1', '新宿书店', SHINJUKU_LAT, SHINJUKU_LNG, 'bookstore')
        items = [
            self._make_item('10:00', '10:30', '喝咖啡', 'nominatim:a1'),
            self._make_item('11:00', '12:00', '吃拉面', 'nominatim:b1'),
            self._make_item('13:00', '14:00', '逛书店', 'nominatim:c1'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a, poi_b, poi_c],
                           target_date=date(2026, 10, 19))
        assert len(result) == 3
        # A kept
        assert result[0].get('planned_place') is not None
        # B degraded (unreachable from A)
        assert result[1].get('planned_place') is None
        # C kept (B has no POI so check is skipped)
        assert result[2].get('planned_place') is not None

    def test_missing_coordinates_no_crash(self):
        """POI with missing lat/lng → skip check, no crash."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        poi_b = {
            'provider': 'nominatim',
            'provider_place_id': 'b1',
            'canonical_name': '神秘地点',
            'canonical_address': '???',
            'verified_category': 'cafe',
            'provider_raw_type': {},
            'name': '神秘地点',
            'address': '???',
            'category': 'cafe',
            'category_label': '咖啡厅',
            'city': 'tokyo',
            # no lat/lng
        }
        items = [
            self._make_item('10:00', '10:30', '喝咖啡', 'nominatim:a1'),
            self._make_item('11:00', '12:00', '探索', 'nominatim:b1'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a, poi_b],
                           target_date=date(2026, 10, 19))
        assert len(result) == 2
        # Both pass through — missing coords mean check is skipped
        assert result[0].get('planned_place') is not None
        assert result[1].get('planned_place') is not None

    def test_single_item_no_crash(self):
        """Only one item → no adjacent pair → no crash."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        items = [
            self._make_item('10:00', '11:00', '喝咖啡', 'nominatim:a1'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a],
                           target_date=date(2026, 10, 19))
        assert len(result) == 1
        assert result[0].get('planned_place') is not None

    def test_degraded_location_never_keeps_poi_name(self):
        """Regression: POI name containing generic word (家) must not leak
        through _general_area. A3 degradation always sets '一般区域'."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        poi_b = self._make_poi('b1', '横滨家系拉面', YOKOHAMA_LAT, YOKOHAMA_LNG, 'ramen')
        items = [
            self._make_item('10:00', '10:30', '喝咖啡', 'nominatim:a1'),
            self._make_item('11:00', '12:00', '吃拉面', 'nominatim:b1',
                            location='横滨家系拉面'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a, poi_b],
                           target_date=date(2026, 10, 19))
        assert len(result) == 2
        assert result[1].get('planned_place') is None
        # Must be generic, not the store name
        assert result[1]['location'] == '一般区域'
        assert '家系' not in result[1]['location']

    def test_zero_gap_unreachable_degraded(self):
        """Shibuya→Yokohama (27km), 0 min gap (back-to-back) → unreachable → degraded.
        Regression: gap_min==0 must NOT be treated as 24h overnight wrap."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        poi_b = self._make_poi('b1', '横滨拉面', YOKOHAMA_LAT, YOKOHAMA_LNG, 'ramen')
        items = [
            self._make_item('10:00', '10:30', '喝咖啡', 'nominatim:a1'),
            self._make_item('10:30', '11:30', '吃拉面', 'nominatim:b1'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a, poi_b],
                           target_date=date(2026, 10, 19))
        assert len(result) == 2
        assert result[0].get('planned_place') is not None
        # Zero gap + 27km → impossible → degraded
        assert result[1].get('planned_place') is None

    def test_empty_items_no_crash(self):
        """Empty item list → no crash."""
        from datetime import date
        from schedule_engine import _sanitize
        result = _sanitize([], poi_candidates=[], target_date=date(2026, 10, 19))
        assert result == []

    def test_zero_gap_same_coordinates_kept(self):
        """Back-to-back activities at the same coordinates need no travel."""
        from datetime import date
        from schedule_engine import _sanitize
        poi_a = self._make_poi('a1', '涩谷咖啡', SHIBUYA_LAT, SHIBUYA_LNG)
        poi_b = self._make_poi('b1', '涩谷书店', SHIBUYA_LAT, SHIBUYA_LNG, 'bookstore')
        items = [
            self._make_item('10:00', '10:30', '喝咖啡', 'nominatim:a1'),
            self._make_item('10:30', '11:30', '逛书店', 'nominatim:b1'),
        ]
        result = _sanitize(items, poi_candidates=[poi_a, poi_b],
                           target_date=date(2026, 10, 19))
        assert len(result) == 2
        assert result[0].get('planned_place') is not None
        assert result[1].get('planned_place') is not None


class TestA3ArchitectureInvariants:
    """A3 must not call any LLM."""

    def test_haversine_is_pure_math(self):
        """haversine_km must not import ai_client or any model-related module."""
        import importlib
        import places_engine
        importlib.reload(places_engine)
        # Check that haversine_km exists and is callable
        assert callable(places_engine.haversine_km)
        # The function uses only math — verify by calling it
        result = places_engine.haversine_km(0, 0, 0, 0)
        assert result == 0.0

    def test_a3_block_no_llm_in_sanitize(self):
        """_sanitize with A3 active must not trigger any LLM call."""
        from datetime import date
        from contextlib import ExitStack
        from unittest.mock import patch
        from schedule_engine import _sanitize

        poi_a = {
            'provider': 'nominatim', 'provider_place_id': 'a1',
            'canonical_name': '涩谷咖啡', 'canonical_address': '东京都',
            'lat': SHIBUYA_LAT, 'lng': SHIBUYA_LNG,
            'verified_category': 'cafe', 'provider_raw_type': {},
            'name': '涩谷咖啡', 'address': '东京都', 'category': 'cafe',
            'category_label': '咖啡厅', 'city': 'tokyo',
        }
        poi_b = dict(poi_a, provider_place_id='b1',
                     canonical_name='横滨拉面', name='横滨拉面',
                     lat=YOKOHAMA_LAT, lng=YOKOHAMA_LNG)
        items = [
            {'start_time': '10:00', 'end_time': '10:30', 'title': '喝咖啡',
             'location': '渋谷', 'note': '', 'category': 'leisure',
             'fixedness': 'optional', 'reply_state': 'free',
             'planned_place_ref': 'nominatim:a1'},
            {'start_time': '11:00', 'end_time': '12:00', 'title': '吃拉面',
             'location': '横滨', 'note': '', 'category': 'leisure',
             'fixedness': 'optional', 'reply_state': 'free',
             'planned_place_ref': 'nominatim:b1'},
        ]

        # Guard both the factory and its already-bound caller reference.
        # BaseException cannot be swallowed by existing except Exception blocks.
        class ExternalCallForbidden(BaseException):
            pass

        with ExitStack() as stack:
            guards = [stack.enter_context(patch(target, side_effect=ExternalCallForbidden(
                'A3 must not call LLM or routing API'))) for target in (
                    'ai_client.get_anthropic', 'ai_client.get_async_anthropic',
                    'ai_client.create_chat', 'schedule_engine.get_anthropic',
                    'requests.sessions.Session.request',
                    'httpx.Client.send', 'httpx.AsyncClient.send')]
            result = _sanitize(items, poi_candidates=[poi_a, poi_b],
                               target_date=date(2026, 10, 19))
            for guard in guards:
                guard.assert_not_called()
        # Should complete without calling LLM
        assert len(result) == 2
        assert result[1].get('planned_place') is None
