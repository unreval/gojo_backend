"""places_engine.py v4 —— Nominatim 为主 + 非餐饮品类 + 活动地标

v4 改动:
  · 新增景点/神社/公园/美术馆/娱乐/温泉/购物 品类
  · 新增"活动场地"静态列表(花火大会/夏祭/初詣 等有固定地点的季节活动)
  · get_schedule_places 会按 食:非食 ≈ 3:2 的比例混搭
  · get_random_place 支持所有品类
"""
import requests
import random
import time
from datetime import datetime, timezone
from math import radians, cos, sin, asin, sqrt

_cache: dict = {}
_CACHE_TTL = 24 * 3600

CITIES = {
    'tokyo':    {'lat': 35.6762, 'lng': 139.6503, 'name_jp': '東京', 'name_cn': '东京'},
    'kyoto':    {'lat': 35.0116, 'lng': 135.7681, 'name_jp': '京都', 'name_cn': '京都'},
    'osaka':    {'lat': 34.6937, 'lng': 135.5023, 'name_jp': '大阪', 'name_cn': '大阪'},
    'yokohama': {'lat': 35.4437, 'lng': 139.6380, 'name_jp': '横浜', 'name_cn': '横滨'},
    'fukuoka':  {'lat': 33.5904, 'lng': 130.4017, 'name_jp': '福岡', 'name_cn': '福冈'},
    'nagoya':   {'lat': 35.1815, 'lng': 136.9066, 'name_jp': '名古屋', 'name_cn': '名古屋'},
    'sapporo':  {'lat': 43.0618, 'lng': 141.3545, 'name_jp': '札幌', 'name_cn': '札幌'},
    'kobe':     {'lat': 34.6901, 'lng': 135.1956, 'name_jp': '神戸', 'name_cn': '神户'},
}

# ── 餐饮品类(原有) ──
FOOD_CATEGORIES = {
    'cafe':        {'q': 'cafe coffee カフェ', 'label_cn': '咖啡厅'},
    'restaurant':  {'q': 'restaurant レストラン 食堂', 'label_cn': '餐厅'},
    'sweets':      {'q': 'sweets patisserie ケーキ 甜品', 'label_cn': '甜品店'},
    'bakery':      {'q': 'bakery パン屋 面包', 'label_cn': '面包店'},
    'ramen':       {'q': 'ramen ラーメン 拉面', 'label_cn': '拉面店'},
    'fashion':     {'q': 'fashion boutique ファッション', 'label_cn': '服装店'},
    'bookstore':   {'q': 'bookstore 書店 本屋', 'label_cn': '书店'},
}

# ── 非餐饮品类(新增) ──
ACTIVITY_CATEGORIES = {
    'shrine':       {'q': '神社 shrine jinja', 'label_cn': '神社'},
    'temple':       {'q': '寺 寺院 temple', 'label_cn': '寺庙'},
    'park':         {'q': '公園 park garden 庭園', 'label_cn': '公园'},
    'landmark':     {'q': 'tower 展望台 タワー landmark', 'label_cn': '景点'},
    'museum':       {'q': '美術館 博物館 museum gallery', 'label_cn': '美术馆'},
    'entertainment':{'q': '映画館 cinema カラオケ arcade', 'label_cn': '娱乐'},
    'shopping':     {'q': '百貨店 department store ショッピング mall', 'label_cn': '购物'},
    'onsen':        {'q': '温泉 銭湯 onsen spa bath', 'label_cn': '温泉'},
}

# 合并:所有品类
CATEGORIES = {**FOOD_CATEGORIES, **ACTIVITY_CATEGORIES}


def haversine_km(lat1, lng1, lat2, lng2):
    """Haversine great-circle distance in kilometres.

    Pure deterministic, no external API. Used by:
      - schedule_engine._sanitize (A3: generation-time filter)
      - schedule_revision (B1: review-time check, future)
    """
    lat1, lng1, lat2, lng2 = map(radians, (lat1, lng1, lat2, lng2))
    dlat = lat2 - lat1
    dlng = lng2 - lng1
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlng / 2) ** 2
    return 2 * 6371 * asin(sqrt(a))


# Nominatim's query text is only a search hint. It is never enough to decide
# what a returned object actually is. In particular, an industrial facility
# that happened to rank for "sweets" must not become a dessert-shop map fact.
_INDUSTRIAL_CLASSES = {'industrial', 'factory', 'works', 'plant', 'warehouse'}
_CATEGORY_TYPES = {
    'cafe': {('amenity', 'cafe')},
    'restaurant': {('amenity', 'restaurant'), ('amenity', 'fast_food')},
    'sweets': {
        ('shop', 'confectionery'), ('shop', 'pastry'), ('shop', 'bakery'),
        ('shop', 'chocolate'), ('shop', 'ice_cream'), ('amenity', 'ice_cream'),
        ('amenity', 'cafe'), ('amenity', 'restaurant'),
    },
    'bakery': {('shop', 'bakery')},
    'ramen': {('amenity', 'restaurant'), ('amenity', 'fast_food')},
    'fashion': {('shop', 'clothes'), ('shop', 'fashion'), ('shop', 'boutique')},
    'bookstore': {('shop', 'books'), ('shop', 'bookstore')},
    'shrine': {('amenity', 'place_of_worship'), ('historic', 'shrine')},
    'temple': {('amenity', 'place_of_worship'), ('historic', 'temple')},
    'park': {('leisure', 'park'), ('leisure', 'garden')},
    'landmark': {
        ('tourism', 'attraction'), ('tourism', 'viewpoint'),
        ('man_made', 'tower'), ('historic', 'monument'),
    },
    'museum': {('tourism', 'museum'), ('tourism', 'gallery')},
    'entertainment': {
        ('amenity', 'cinema'), ('amenity', 'theatre'), ('amenity', 'nightclub'),
        ('leisure', 'adult_gaming_centre'),
    },
    'shopping': {('shop', 'mall'), ('shop', 'department_store')},
    'onsen': {('amenity', 'public_bath'), ('leisure', 'spa')},
}


def _raw_tags(item):
    # Nominatim exposes useful OSM tags and localized names in separate maps.
    # Merge them; choosing one with ``or`` used to discard name data whenever
    # extratags happened to be present.
    tags = {}
    for source in (item.get('extratags'), item.get('namedetails')):
        if isinstance(source, dict):
            tags.update(source)
    return tags


def _is_industrial(item):
    raw_class = str(item.get('class') or '').strip().lower()
    raw_type = str(item.get('type') or '').strip().lower()
    tags = {str(k).lower(): str(v).lower() for k, v in _raw_tags(item).items()}
    values = {raw_class, raw_type, tags.get('landuse', ''), tags.get('man_made', '')}
    display = str(item.get('display_name') or '').lower()
    return bool(values & _INDUSTRIAL_CLASSES) or any(
        term in display for term in ('factory', 'industrial', '工場', '工厂')
    )


def _matches_requested_category(item, category):
    raw_class = str(item.get('class') or '').strip().lower()
    raw_type = str(item.get('type') or '').strip().lower()
    actual = (raw_class, raw_type)
    allowed = _CATEGORY_TYPES.get(category, set())
    if actual not in allowed:
        return False
    if category == 'sweets':
        specific_sweets = {
            ('shop', 'confectionery'), ('shop', 'pastry'),
            ('shop', 'bakery'), ('shop', 'chocolate'),
            ('shop', 'ice_cream'), ('amenity', 'ice_cream'),
        }
        if actual in specific_sweets:
            return True
        tags = _raw_tags(item)
        evidence = ' '.join((
            str(tags.get('cuisine') or ''),
            str(tags.get('name') or ''),
            str(item.get('display_name') or ''),
        )).lower()
        return any(term in evidence for term in (
            'dessert', 'sweets', 'sweet shop', 'patisserie', 'pastry',
            'cake', 'confectionery', 'chocolate', 'ice cream',
            'ケーキ', '菓子', '洋菓子', 'デザート', '甜品', '蛋糕',
        ))
    # Ramen needs a restaurant that is actually named/tagged as ramen; do not
    # label an arbitrary restaurant as ramen merely because of the query.
    if category == 'ramen':
        tags = _raw_tags(item)
        text = ' '.join([
            str(item.get('display_name') or ''),
            str(tags.get('cuisine') or ''), str(tags.get('name') or ''),
        ]).lower()
        return 'ramen' in text or 'ラーメン' in text or '拉面' in text
    return True


def validate_nominatim_result(item, requested_category, city='tokyo'):
    """Convert one verified Nominatim result into a resolver-owned POI.

    ``requested_category`` is validated against OSM's actual class/type/tags.
    The returned ``verified_category`` is derived from that validation; callers
    must not write a free-text location as a coordinate-bearing map fact.
    """
    if not isinstance(item, dict) or requested_category not in CATEGORIES:
        return None
    if _is_industrial(item) or not _matches_requested_category(item, requested_category):
        return None
    display = str(item.get('display_name') or '').strip()
    parts = [part.strip() for part in display.split(',') if part.strip()]
    name = str((_raw_tags(item).get('name') or (parts[0] if parts else ''))).strip()
    if len(name) < 2:
        return None
    try:
        lat = float(item.get('lat'))
        lng = float(item.get('lon'))
    except (TypeError, ValueError):
        return None
    if not lat or not lng:
        return None
    provider_place_id = str(item.get('place_id') or '').strip()
    if not provider_place_id:
        return None
    city_info = CITIES.get(city, CITIES['tokyo'])
    address = ', '.join(parts[1:4]) if len(parts) > 1 else city_info['name_cn']
    raw_type = {
        'class': item.get('class'),
        'type': item.get('type'),
        'osm_type': item.get('osm_type'),
        'osm_id': item.get('osm_id'),
        'tags': _raw_tags(item),
    }
    # A1: parse opening_hours from OSM extratags
    opening_spec = None
    try:
        from opening_hours import parse_opening_hours
        tags = _raw_tags(item)
        oh_raw = tags.get('opening_hours')
        if oh_raw:
            opening_spec = parse_opening_hours(oh_raw)
    except Exception:
        opening_spec = None

    return {
        # Canonical resolver fields.
        'provider': 'nominatim',
        'provider_place_id': provider_place_id,
        'canonical_name': name,
        'canonical_address': address,
        'lat': lat,
        'lng': lng,
        'verified_category': requested_category,
        'provider_raw_type': raw_type,
        'fetched_at': datetime.now(timezone.utc).isoformat(),
        'opening_spec': opening_spec,
        # Compatibility/display aliases. They are derived only from the
        # verified fields above, never from a model-generated location string.
        'name': name,
        'address': address,
        'category': requested_category,
        'category_label': CATEGORIES[requested_category]['label_cn'],
        'city': city,
        'osm_id': item.get('osm_id'),
    }


def resolve_schedule_poi(reference, candidates):
    """Resolve a planned-place reference only against this verified pool."""
    if not reference or not candidates:
        return None
    if isinstance(reference, str):
        provider, _, provider_place_id = reference.partition(':')
    elif isinstance(reference, dict):
        provider = str(reference.get('provider') or '').strip()
        provider_place_id = str(reference.get('provider_place_id') or '').strip()
    else:
        return None
    if not provider or not provider_place_id:
        return None
    for candidate in candidates:
        if (candidate.get('provider') == provider
                and str(candidate.get('provider_place_id')) == provider_place_id):
            return dict(candidate)
    return None


def _nominatim_search(city, category):
    """A2: Extract Nominatim HTTP call for reuse by search_places and prefetch."""
    city_info = CITIES.get(city, CITIES['tokyo'])
    cat_info = CATEGORIES.get(category)
    if not cat_info:
        return []
    try:
        resp = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={
                'q': f'{cat_info["q"]} {city_info["name_jp"]}',
                'format': 'json',
                'limit': 50,
                'addressdetails': 1,
                'extratags': 1,
                'namedetails': 1,
                'viewbox': f'{city_info["lng"]-0.15},{city_info["lat"]+0.1},{city_info["lng"]+0.15},{city_info["lat"]-0.1}',
                'bounded': 1,
            },
            timeout=15,
            headers={'User-Agent': 'GojoAssistant/1.0 (contact: dev@gojoassistant.app)'},
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f'[places] Nominatim 请求失败: {e}')
        return []
    results = []
    for item in data:
        verified = validate_nominatim_result(item, category, city)
        if verified:
            results.append(verified)
    return results


def _db_poi_to_candidate(row, city='tokyo'):
    """A2: Convert a DB cache row into a full POI candidate dict."""
    cat = row.get('verified_category', '')
    cat_info = CATEGORIES.get(cat, {})
    raw_type = row.get('provider_raw_type') or {}
    result = {
        # Canonical resolver fields
        'provider': row['provider'],
        'provider_place_id': row['provider_place_id'],
        'canonical_name': row['canonical_name'],
        'canonical_address': row['canonical_address'],
        'lat': row['lat'],
        'lng': row['lng'],
        'verified_category': cat,
        'provider_raw_type': raw_type,
        'fetched_at': row.get('fetched_at', ''),
        # Compatibility aliases (schedule_engine uses these)
        'name': row['canonical_name'],
        'address': row['canonical_address'],
        'category': cat,
        'category_label': cat_info.get('label_cn', cat),
        'city': city,
        'osm_id': (raw_type.get('osm_id') if isinstance(raw_type, dict) else None),
    }
    # A1 compatibility: re-derive opening_spec from stored tags if module exists
    try:
        from opening_hours import parse_opening_hours
        tags = raw_type.get('tags') or {} if isinstance(raw_type, dict) else {}
        oh_str = tags.get('opening_hours', '')
        if oh_str:
            result['opening_spec'] = parse_opening_hours(oh_str)
    except ImportError:
        pass  # A1 not deployed yet — no opening_spec field
    return result


def search_places(city='tokyo', category='cafe', limit=30):
    """A2: DB 缓存优先 → 冷启动才打 Nominatim。"""
    cache_key = f'{city}_{category}'
    now = time.time()

    # 1. In-memory cache (fastest, same process)
    if cache_key in _cache:
        ct, cd = _cache[cache_key]
        if now - ct < _CACHE_TTL:
            return random.sample(cd, min(limit, len(cd)))

    # 2. DB cache (survives restart)
    city_info = CITIES.get(city, CITIES['tokyo'])
    try:
        from db_schedule import get_cached_pois
        db_rows = get_cached_pois(
            city_info['lat'] - 0.1, city_info['lat'] + 0.1,
            city_info['lng'] - 0.15, city_info['lng'] + 0.15,
            category, limit=50,
        )
        if db_rows:
            db_candidates = [_db_poi_to_candidate(r, city) for r in db_rows]
            _cache[cache_key] = (now, db_candidates)
            print(f'[places] DB 缓存命中 {city}/{category}: {len(db_candidates)} 个地点')
            return random.sample(db_candidates, min(limit, len(db_candidates)))
    except Exception as e:
        print(f'[places] DB 缓存查询失败: {e}')

    # 3. Nominatim fallback (cold start only)
    results = _nominatim_search(city, category)
    if results:
        _cache[cache_key] = (now, results)
        # Write to DB cache for next time
        try:
            from db_schedule import bulk_cache_pois
            bulk_cache_pois(results)
            print(f'[places] ✅ {city}/{category}: Nominatim → {len(results)} 个地点 (已存 DB)')
        except Exception as e:
            print(f'[places] DB 缓存写入失败: {e}')
            print(f'[places] ✅ {city}/{category}: Nominatim → {len(results)} 个地点')
    return random.sample(results, min(limit, len(results))) if results else []


def get_random_place(city='tokyo', category=None):
    if not category:
        category = random.choice(list(CATEGORIES.keys()))
    places = search_places(city, category, limit=50)
    return random.choice(places) if places else None


def prefetch_pois(city='tokyo'):
    """A2: 预取 2 个随机品类到 DB 缓存。由 diary_scheduler 调用,始终打 Nominatim。"""
    cats = random.sample(list(CATEGORIES.keys()), min(2, len(CATEGORIES)))
    total = 0
    for cat in cats:
        results = _nominatim_search(city, cat)
        if results:
            try:
                from db_schedule import bulk_cache_pois
                bulk_cache_pois(results)
                total += len(results)
                print(f'[places] 预取 {city}/{cat}: {len(results)} 个地点')
            except Exception as e:
                print(f'[places] 预取写入 DB 失败 {city}/{cat}: {e}')
        # Respect Nominatim rate limit (1 req/sec policy)
        time.sleep(1.5)
    print(f'[places] 预取完成 {city}: 共 {total} 个地点, {len(cats)} 个品类')
    return total


def get_schedule_places(city='tokyo', count=15):
    """A2: 增大候选池到 15。食 8 + 非食 7 左右的混搭。

    ★ v4:不再只搜吃的。一天的日程应该有吃有逛有打卡。
    ★ A2:候选从 5 增到 15,让模型有更多选择余地。
    """
    # 食物类:搜 5 个品类,每品类取 2 个,上限 8
    food_cats = random.sample(list(FOOD_CATEGORIES.keys()), min(5, len(FOOD_CATEGORIES)))
    food_places = []
    for cat in food_cats:
        places = search_places(city, cat, limit=3)
        food_places.extend(places[:2])
        if len(food_places) >= 8:
            break

    # 非食类:搜 4 个品类,每品类取 2 个,上限 7
    act_cats = random.sample(list(ACTIVITY_CATEGORIES.keys()), min(4, len(ACTIVITY_CATEGORIES)))
    act_places = []
    for cat in act_cats:
        places = search_places(city, cat, limit=3)
        act_places.extend(places[:2])
        if len(act_places) >= 7:
            break

    # Do not inject a fixed seasonal venue every year. It anchored generation
    # to the same famous entities (and was not resolver-verified); seasonal
    # variety is now an abstract prompt hint and POIs always come from the
    # validated resolver pool above.
    combined = food_places + act_places
    random.shuffle(combined)
    return combined[:count]
