"""test_a4_visit_weights.py — A4 到访加权单元测试

覆盖:
  - _is_negative_review 关键词匹配
  - get_visit_weight_map 权重计算
  - _weighted_sample_without_replacement 加权抽样
  - get_schedule_places 加权集成
  - 回归:无到访记录时行为不变
"""
import sys
import os
import random
from collections import Counter
from datetime import date, timedelta
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gojo_backend'))


@pytest.fixture(autouse=True)
def reproducible_random_state():
    state = random.getstate()
    random.seed(20261016)
    try:
        yield
    finally:
        random.setstate(state)


# ═══════════════════════════════════════════════════════════════
# 1. _is_negative_review
# ═══════════════════════════════════════════════════════════════

class TestIsNegativeReview:

    def test_empty_review(self):
        from db_visited_places import _is_negative_review
        assert _is_negative_review('') is False
        assert _is_negative_review(None) is False

    def test_positive_review(self):
        from db_visited_places import _is_negative_review
        assert _is_negative_review('抹茶拿铁超好喝！下次还来') is False

    def test_negative_keywords(self):
        from db_visited_places import _is_negative_review
        assert _is_negative_review('味道不好，不推荐') is True
        assert _is_negative_review('这家店踩雷了') is True
        assert _is_negative_review('难吃到哭') is True
        assert _is_negative_review('再也不去了') is True
        assert _is_negative_review('不想再来这里') is True

    def test_partial_match(self):
        """关键词必须是子串匹配,不是整词匹配。"""
        from db_visited_places import _is_negative_review
        assert _is_negative_review('环境差劲') is True  # 差劲 in keywords
        assert _is_negative_review('很失望') is True  # 失望 in keywords

    def test_neutral_review(self):
        from db_visited_places import _is_negative_review
        assert _is_negative_review('还可以吧,一般般的味道') is False  # "一般般" not in keywords
        # Note: "一般般" is NOT in the negative keyword list —
        # it's deliberately excluded; ambivalence ≠ negative


# ═══════════════════════════════════════════════════════════════
# 2. get_visit_weight_map
# ═══════════════════════════════════════════════════════════════

def _make_visit(provider='nominatim', pid='12345', review='很好!', visit_date=None):
    """Helper: construct a visit dict like list_visited returns."""
    return {
        'id': 1,
        'character_id': 'char_1',
        'place_name': 'Test Place',
        'place_address': '东京都...',
        'lat': 35.6, 'lng': 139.7,
        'category': 'cafe', 'city': 'tokyo',
        'char_review': review,
        'visit_date': visit_date or str(date.today() - timedelta(days=3)),
        'created_at': '2026-01-01',
        'provider': provider,
        'provider_place_id': pid,
        'verified_category': 'cafe',
        'schedule_event_id': 100,
        'provenance': 'schedule_completed',
    }


class TestGetVisitWeightMap:

    @patch('db_visited_places.list_visited', return_value=[])
    def test_no_visits(self, mock_list):
        from db_visited_places import get_visit_weight_map
        result = get_visit_weight_map('char_1', 'user_1', city='tokyo')
        assert result == {}

    @patch('db_visited_places.list_visited')
    def test_positive_visit_boost(self, mock_list):
        mock_list.return_value = [_make_visit(review='抹茶拿铁超好喝')]
        from db_visited_places import get_visit_weight_map
        result = get_visit_weight_map('char_1', 'user_1')
        assert result.get('nominatim:12345') == 2.5

    @patch('db_visited_places.list_visited')
    def test_negative_visit_suppress_recent(self, mock_list):
        """负评 + 距今 ≤56 天 → 0.05"""
        recent_date = str(date.today() - timedelta(days=10))
        mock_list.return_value = [_make_visit(review='踩雷了,难吃', visit_date=recent_date)]
        from db_visited_places import get_visit_weight_map
        result = get_visit_weight_map('char_1', 'user_1')
        assert result.get('nominatim:12345') == 0.05

    @patch('db_visited_places.list_visited')
    def test_negative_visit_suppress_expired(self, mock_list):
        """负评 + 距今 >56 天 → 回归 1.0"""
        old_date = str(date.today() - timedelta(days=60))
        mock_list.return_value = [_make_visit(review='踩雷了,难吃', visit_date=old_date)]
        from db_visited_places import get_visit_weight_map
        result = get_visit_weight_map('char_1', 'user_1')
        assert result.get('nominatim:12345') == 1.0

    @patch('db_visited_places.list_visited')
    def test_multiple_visits_same_poi_takes_latest(self, mock_list):
        """同一 POI 多次到访,取最近一次(list_visited 按 visit_date DESC)。"""
        mock_list.return_value = [
            _make_visit(review='这次很好!', visit_date=str(date.today() - timedelta(days=1))),
            _make_visit(review='踩雷了', visit_date=str(date.today() - timedelta(days=30))),
        ]
        from db_visited_places import get_visit_weight_map
        result = get_visit_weight_map('char_1', 'user_1')
        # 最近的一次是正面评价 → 2.5
        assert result.get('nominatim:12345') == 2.5

    @patch('db_visited_places.list_visited')
    def test_missing_provider_skipped(self, mock_list):
        mock_list.return_value = [_make_visit(provider='', pid='123')]
        from db_visited_places import get_visit_weight_map
        result = get_visit_weight_map('char_1', 'user_1')
        assert result == {}

    @patch('db_visited_places.list_visited')
    def test_no_review_is_positive(self, mock_list):
        """空评价 → 非负面 → 2.5 (去过就是去过)"""
        mock_list.return_value = [_make_visit(review='')]
        from db_visited_places import get_visit_weight_map
        result = get_visit_weight_map('char_1', 'user_1')
        assert result.get('nominatim:12345') == 2.5

    @patch('db_visited_places.list_visited')
    def test_city_passthrough(self, mock_list):
        """city 参数透传到 list_visited。"""
        mock_list.return_value = []
        from db_visited_places import get_visit_weight_map
        get_visit_weight_map('char_1', 'user_1', city='osaka')
        mock_list.assert_called_once_with('user_1', character_id='char_1', city='osaka', limit=500)

    @pytest.mark.parametrize('days, expected', [(56, 0.05), (57, 1.0)])
    def test_negative_suppression_boundary(self, days, expected):
        from db_visited_places import get_visit_weight_map
        visit = _make_visit(review='难吃', visit_date=str(date.today() - timedelta(days=days)))
        with patch('db_visited_places.list_visited', return_value=[visit]):
            assert get_visit_weight_map('char_1', 'user_1') == {'nominatim:12345': expected}

    @pytest.mark.parametrize('visit_date', [None, '', 'invalid', '2026-99-99', 12345, {}])
    def test_missing_or_malformed_negative_date_is_suppressed(self, visit_date):
        from db_visited_places import get_visit_weight_map
        visit = _make_visit(review='难吃')
        visit['visit_date'] = visit_date
        with patch('db_visited_places.list_visited', return_value=[visit]):
            assert get_visit_weight_map('char_1', 'user_1') == {'nominatim:12345': 0.05}

    @pytest.mark.parametrize('key', ['provider', 'provider_place_id'])
    @pytest.mark.parametrize('value', [None, ''])
    def test_incomplete_provider_identity_is_skipped(self, key, value):
        from db_visited_places import get_visit_weight_map
        visit = _make_visit()
        visit[key] = value
        missing_key_visit = _make_visit()
        missing_key_visit.pop(key)
        with patch('db_visited_places.list_visited', return_value=[visit, missing_key_visit]):
            assert get_visit_weight_map('char_1', 'user_1') == {}

    def test_latest_negative_visit_overrides_older_positive(self):
        from db_visited_places import get_visit_weight_map
        visits = [
            _make_visit(review='难吃', visit_date=str(date.today())),
            _make_visit(review='很好', visit_date=str(date.today() - timedelta(days=1))),
        ]
        with patch('db_visited_places.list_visited', return_value=visits):
            assert get_visit_weight_map('char_1', 'user_1') == {'nominatim:12345': 0.05}


# ═══════════════════════════════════════════════════════════════
# 3. _weighted_sample_without_replacement
# ═══════════════════════════════════════════════════════════════

class TestWeightedSample:

    def test_empty_candidates(self):
        from places_engine import _weighted_sample_without_replacement
        assert _weighted_sample_without_replacement([], [], 5) == []

    def test_k_larger_than_n(self):
        from places_engine import _weighted_sample_without_replacement
        items = ['a', 'b', 'c']
        result = _weighted_sample_without_replacement(items, [1.0, 1.0, 1.0], 10)
        assert sorted(result) == ['a', 'b', 'c']

    def test_no_duplicates(self):
        from places_engine import _weighted_sample_without_replacement
        items = list(range(20))
        weights = [1.0] * 20
        result = _weighted_sample_without_replacement(items, weights, 10)
        assert len(result) == 10
        assert len(set(result)) == 10  # no duplicates

    def test_high_weight_favored(self):
        """高权重的 item 应该更频繁被选中。"""
        from places_engine import _weighted_sample_without_replacement
        items = ['favored', 'normal_1', 'normal_2', 'normal_3', 'normal_4']
        weights = [100.0, 0.01, 0.01, 0.01, 0.01]
        # 只选 1 个,100 次里应该绝大多数是 favored
        favored_count = 0
        for _ in range(100):
            result = _weighted_sample_without_replacement(items, weights, 1)
            if result[0] == 'favored':
                favored_count += 1
        assert favored_count >= 90  # 极大概率

    def test_zero_weight_suppressed(self):
        """有正权重候选时,权重 0.0 的 item 不应被选中。"""
        from places_engine import _weighted_sample_without_replacement
        items = ['suppressed', 'a', 'b', 'c', 'd']
        weights = [0.0, 1.0, 1.0, 1.0, 1.0]
        # 选 1 个,suppressed 的权重是 0 → 全零才会走均匀分布
        # 但这里只有 suppressed 是 0,其他是 1.0,total > 0,所以 suppressed 不可能被选
        results = [_weighted_sample_without_replacement(items, weights, 1)[0]
                   for _ in range(50)]
        assert 'suppressed' not in results

    def test_zero_draw_never_selects_zero_weight(self):
        from places_engine import _weighted_sample_without_replacement
        with patch('places_engine.random.random', return_value=0.0):
            result = _weighted_sample_without_replacement(
                ['zero', 'positive', 'other'], [0.0, 2.5, 1.0], 2)
        assert result == ['positive', 'other']

    def test_actual_visit_weight_bias(self):
        from places_engine import _weighted_sample_without_replacement
        counts = Counter(
            _weighted_sample_without_replacement(
                ['regular', 'unvisited', 'negative'], [2.5, 1.0, 0.05], 1)[0]
            for _ in range(6000)
        )
        for name, weight in [('regular', 2.5), ('unvisited', 1.0), ('negative', 0.05)]:
            assert counts[name] / 6000 == pytest.approx(weight / 3.55, abs=0.025)
        assert counts['negative'] > 0  # Suppression remains probabilistic.

    def test_all_zero_weights_fallback_uniform(self):
        """全零权重 → 均匀随机回退。"""
        from places_engine import _weighted_sample_without_replacement
        items = ['a', 'b', 'c']
        weights = [0.0, 0.0, 0.0]
        result = _weighted_sample_without_replacement(items, weights, 2)
        assert len(result) == 2
        assert len(set(result)) == 2  # still no duplicates


# ═══════════════════════════════════════════════════════════════
# 4. get_schedule_places 加权集成
# ═══════════════════════════════════════════════════════════════

def _make_poi(provider='nominatim', pid='', category='cafe', name='Test'):
    """Helper: minimal POI dict as returned by search_places."""
    return {
        'provider': provider,
        'provider_place_id': pid or str(random.randint(10000, 99999)),
        'canonical_name': name,
        'name': name,
        'canonical_address': '东京都...',
        'address': '东京都...',
        'lat': 35.6, 'lng': 139.7,
        'verified_category': category,
        'category': category,
        'city': 'tokyo',
    }


class TestGetSchedulePlacesA4:

    @patch('places_engine.search_places')
    def test_no_character_id_uses_original_logic(self, mock_search):
        """不传 character_id → 原逻辑,不调 get_visit_weight_map。"""
        mock_search.return_value = [_make_poi() for _ in range(3)]
        from places_engine import get_schedule_places
        with patch('db_visited_places.get_visit_weight_map') as mock_vw:
            result = get_schedule_places('tokyo', count=5)
            mock_vw.assert_not_called()
        assert len(result) <= 5

    @patch('places_engine.search_places')
    @patch('db_visited_places.get_visit_weight_map', return_value={})
    def test_empty_weight_map_uses_original_logic(self, mock_vw, mock_search):
        """到访记录为空 → 走原逻辑(shuffle)。"""
        pois = [_make_poi(pid=str(i)) for i in range(10)]
        mock_search.return_value = pois[:3]
        from places_engine import get_schedule_places
        result = get_schedule_places('tokyo', count=5, character_id='c1', user_id='u1')
        assert len(result) <= 5

    @patch('places_engine.search_places')
    @patch('db_visited_places.get_visit_weight_map')
    def test_boosted_poi_more_likely(self, mock_vw, mock_search):
        """到访权重高的 POI 更容易被选中。"""
        boosted_pid = 'boosted_999'
        # Each category returns distinct candidates, so duplication cannot fake a boost.
        def search(city, category, limit):
            first_pid = boosted_pid if category == 'cafe' else category + '_0'
            return [_make_poi(pid=first_pid), _make_poi(pid=category + '_1')]

        mock_search.side_effect = search
        mock_vw.return_value = {f'nominatim:{boosted_pid}': 100.0}

        from places_engine import get_schedule_places
        boosted_count = 0
        with patch('places_engine.FOOD_CATEGORIES', {'cafe': {}}), \
                patch('places_engine.ACTIVITY_CATEGORIES', {'park': {}}):
            for _ in range(50):
                result = get_schedule_places('tokyo', count=1, character_id='c1', user_id='u1')
                if any(p.get('provider_place_id') == boosted_pid for p in result):
                    boosted_count += 1
        # 100x 权重 → 几乎每次都被选中
        assert boosted_count >= 40

    @patch('places_engine.search_places')
    @patch('db_visited_places.get_visit_weight_map', side_effect=Exception('DB down'))
    def test_weight_query_failure_graceful(self, mock_vw, mock_search):
        """get_visit_weight_map 异常 → 降级走原逻辑,不 raise。"""
        mock_search.return_value = [_make_poi(pid=str(i)) for i in range(3)]
        from places_engine import get_schedule_places
        result = get_schedule_places('tokyo', count=5, character_id='c1', user_id='u1')
        assert isinstance(result, list)  # 不崩

    @pytest.mark.parametrize('ids, fails', [
        ({}, False),
        ({'character_id': 'c1'}, False),
        ({'user_id': 'u1'}, False),
        ({'character_id': 'c1', 'user_id': 'u1'}, False),
        ({'character_id': 'c1', 'user_id': 'u1'}, True),
    ])
    def test_fallback_matches_original_output_and_random_state(self, ids, fails):
        from places_engine import get_schedule_places

        def search(city, category, limit):
            return [_make_poi(pid=category + str(i)) for i in range(3)]

        with patch('places_engine.search_places', side_effect=search) as mock_search, \
                patch('db_visited_places.get_visit_weight_map', return_value={}) as mock_vw, \
                patch('places_engine._weighted_sample_without_replacement') as mock_sample:
            state = random.getstate()
            original = get_schedule_places('tokyo', count=5)
            original_random_state = random.getstate()
            original_search_calls = mock_search.call_args_list[:]
            mock_vw.assert_not_called()
            mock_search.reset_mock()
            random.setstate(state)
            if fails:
                mock_vw.side_effect = RuntimeError('DB down')
            actual = get_schedule_places('tokyo', count=5, **ids)
            assert actual == original
            assert random.getstate() == original_random_state
            assert mock_search.call_args_list == original_search_calls
            mock_sample.assert_not_called()
            if ids.get('character_id') and ids.get('user_id'):
                mock_vw.assert_called_once_with('c1', 'u1', city='tokyo')
            else:
                mock_vw.assert_not_called()

    def test_weights_use_provider_identity_instead_of_display_name(self):
        from places_engine import get_schedule_places
        candidates = [
            _make_poi(pid='stable', name='新店名'),
            _make_poi(pid='unvisited', name='新店名'),
            _make_poi(provider='other', pid='stable', name='新店名'),
            _make_poi(pid='missing', name='新店名'),
        ]
        candidates[-1].pop('provider_place_id')
        with patch('places_engine.FOOD_CATEGORIES', {'cafe': {}}), \
                patch('places_engine.ACTIVITY_CATEGORIES', {'park': {}}), \
                patch('places_engine.search_places', side_effect=[candidates[:2], candidates[2:]]), \
                patch('db_visited_places.get_visit_weight_map', return_value={
                    'nominatim:stable': 2.5, 'other:stable': 0.05, '新店名': 99.0,
                }), \
                patch('places_engine._weighted_sample_without_replacement', return_value=[]) as sample:
            assert get_schedule_places('tokyo', count=2, character_id='c1', user_id='u1') == []
        sample.assert_called_once_with(candidates, [2.5, 1.0, 0.05, 1.0], 2)


# ═══════════════════════════════════════════════════════════════
# 5. _fetch_real_places 透传
# ═══════════════════════════════════════════════════════════════

class TestFetchRealPlacesA4:

    @patch('places_engine.get_schedule_places')
    def test_passes_character_and_user(self, mock_gsp):
        mock_gsp.return_value = []
        from schedule_engine import _fetch_real_places
        _fetch_real_places('tokyo', character_id='c1', user_id='u1')
        mock_gsp.assert_called_once_with('tokyo', count=15, character_id='c1', user_id='u1')

    @patch('places_engine.get_schedule_places')
    def test_backward_compat_no_ids(self, mock_gsp):
        mock_gsp.return_value = []
        from schedule_engine import _fetch_real_places
        _fetch_real_places('osaka')
        mock_gsp.assert_called_once_with('osaka', count=15, character_id=None, user_id=None)

    def test_daily_generation_passes_exact_ids_to_visit_authority(self, monkeypatch):
        import schedule_engine
        import places_engine

        monkeypatch.setattr(schedule_engine.db_schedule, 'has_schedule', lambda *args: False)
        monkeypatch.setattr(schedule_engine.db_schedule, 'get_recent_schedule_history',
                            lambda *args, **kwargs: [])
        monkeypatch.setattr(schedule_engine, 'get_character', lambda char_id: {'name': '角色'})
        monkeypatch.setattr(schedule_engine, 'load_core', lambda char_id: {})
        monkeypatch.setattr(schedule_engine, 'get_rhythm_text', lambda char_id: '')
        with patch('schedule_engine.random.random', return_value=0.5), \
                patch('places_engine.search_places', return_value=[]), \
                patch('db_visited_places.list_visited', return_value=[]) as visits, \
                patch('schedule_engine._fetch_real_places', wraps=schedule_engine._fetch_real_places) as fetch, \
                patch('places_engine.get_schedule_places', wraps=places_engine.get_schedule_places) as places, \
                patch('schedule_engine.get_anthropic', side_effect=RuntimeError('offline boundary')) as model:
            assert schedule_engine.generate_daily_schedule(
                'character_exact', 'user_exact', target_date=date(2026, 10, 16)) is None
        fetch.assert_called_once_with('tokyo', character_id='character_exact', user_id='user_exact')
        places.assert_called_once_with('tokyo', count=15,
                                       character_id='character_exact', user_id='user_exact')
        visits.assert_called_once_with('user_exact', character_id='character_exact', city='tokyo', limit=500)
        model.assert_called_once_with()


# ═══════════════════════════════════════════════════════════════
# 6. 回归:无改动路径行为不变
# ═══════════════════════════════════════════════════════════════

class TestA4Regression:

    def test_negative_keywords_is_frozenset(self):
        from db_visited_places import _NEGATIVE_KEYWORDS
        assert isinstance(_NEGATIVE_KEYWORDS, frozenset)

    def test_negative_suppress_days_is_56(self):
        from db_visited_places import NEGATIVE_SUPPRESS_DAYS
        assert NEGATIVE_SUPPRESS_DAYS == 56

    @patch('places_engine.search_places')
    def test_get_schedule_places_returns_list(self, mock_search):
        mock_search.return_value = [_make_poi(pid=str(i)) for i in range(3)]
        from places_engine import get_schedule_places
        result = get_schedule_places('tokyo', count=5)
        assert isinstance(result, list)

    def test_no_llm_imports(self):
        """A4 不引入任何 LLM 相关 import。"""
        import db_visited_places
        import inspect
        source = inspect.getsource(db_visited_places.get_visit_weight_map)
        for llm_word in ['anthropic', 'openai', 'claude', 'gpt', 'model_client',
                         'chat_completion', 'llm']:
            assert llm_word not in source.lower(), f'LLM reference found: {llm_word}'


# ═══════════════════════════════════════════════════════════════
# 7. 架构守卫
# ═══════════════════════════════════════════════════════════════

class TestA4ArchitectureGuard:

    def test_no_new_tables(self):
        """A4 不创建新表,只读已有的 char_visited_places。"""
        import db_visited_places
        import inspect
        source = inspect.getsource(db_visited_places.get_visit_weight_map)
        source += inspect.getsource(db_visited_places._is_negative_review)
        assert 'CREATE TABLE' not in source

    def test_no_new_columns(self):
        """A4 不给 char_visited_places 加新列。"""
        import db_visited_places
        import inspect
        source = inspect.getsource(db_visited_places.get_visit_weight_map)
        source += inspect.getsource(db_visited_places._is_negative_review)
        assert 'ALTER TABLE' not in source
        assert 'ADD COLUMN' not in source

    def test_weighted_sample_deterministic_seed(self):
        """给定相同 seed,加权抽样结果一致(可复现)。"""
        from places_engine import _weighted_sample_without_replacement
        items = list(range(10))
        weights = [1.0 + i * 0.5 for i in range(10)]
        random.seed(42)
        r1 = _weighted_sample_without_replacement(items, weights, 5)
        random.seed(42)
        r2 = _weighted_sample_without_replacement(items, weights, 5)
        assert r1 == r2
