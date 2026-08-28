"""places_engine.py —— 用 Overpass API 搜东京/京都的真实店铺(完全免费)

功能:
  · search_places(city, category) → 返回真实的餐厅/咖啡厅/甜品店列表
  · 带名字、地址、坐标、类型
  · 缓存 24 小时避免重复请求
  · schedule_engine 生成日程时调这个拿真实地点

Overpass API: 完全免费、无需 key、无需注册
  https://overpass-api.de/api/interpreter
"""
import requests
import random
import time
import json
import os

# 缓存:按 city+category 缓存,24 小时过期
_cache: dict = {}
_CACHE_TTL = 24 * 3600

# 城市中心坐标 + 搜索半径(米)
CITIES = {
    'tokyo': {'lat': 35.6762, 'lng': 139.6503, 'radius': 15000, 'name_jp': '東京'},
    'kyoto': {'lat': 35.0116, 'lng': 135.7681, 'radius': 8000, 'name_jp': '京都'},
    'osaka': {'lat': 34.6937, 'lng': 135.5023, 'radius': 10000, 'name_jp': '大阪'},
    'yokohama': {'lat': 35.4437, 'lng': 139.6380, 'radius': 8000, 'name_jp': '横浜'},
    'fukuoka': {'lat': 33.5904, 'lng': 130.4017, 'radius': 8000, 'name_jp': '福岡'},
    'nagoya': {'lat': 35.1815, 'lng': 136.9066, 'radius': 8000, 'name_jp': '名古屋'},
    'sapporo': {'lat': 43.0618, 'lng': 141.3545, 'radius': 8000, 'name_jp': '札幌'},
    'kobe': {'lat': 34.6901, 'lng': 135.1956, 'radius': 6000, 'name_jp': '神戸'},
}

# 类别 → Overpass 查询条件
CATEGORIES = {
    'cafe': {
        'query': '["amenity"="cafe"]',
        'label': 'カフェ',
        'label_cn': '咖啡厅',
    },
    'restaurant': {
        'query': '["amenity"="restaurant"]',
        'label': 'レストラン',
        'label_cn': '餐厅',
    },
    'sweets': {
        'query': '["shop"="confectionery"]',
        'label': 'スイーツ',
        'label_cn': '甜品店',
    },
    'bakery': {
        'query': '["shop"="bakery"]',
        'label': 'パン屋',
        'label_cn': '面包店',
    },
    'ramen': {
        'query': '["amenity"="restaurant"]["cuisine"="ramen"]',
        'label': 'ラーメン',
        'label_cn': '拉面店',
    },
    'fashion': {
        'query': '["shop"="clothes"]',
        'label': 'ファッション',
        'label_cn': '服装店',
    },
    'bookstore': {
        'query': '["shop"="books"]',
        'label': '本屋',
        'label_cn': '书店',
    },
    'convenience': {
        'query': '["shop"="convenience"]',
        'label': 'コンビニ',
        'label_cn': '便利店',
    },
}


def search_places(city: str = 'tokyo', category: str = 'cafe', limit: int = 30) -> list:
    """搜指定城市+类别的真实店铺。返回 [{name, lat, lng, address, category, city}]"""
    cache_key = f'{city}_{category}'
    now = time.time()

    # 检查缓存
    if cache_key in _cache:
        cached_time, cached_data = _cache[cache_key]
        if now - cached_time < _CACHE_TTL:
            return random.sample(cached_data, min(limit, len(cached_data)))

    city_info = CITIES.get(city, CITIES['tokyo'])
    cat_info = CATEGORIES.get(category, CATEGORIES['cafe'])

    lat = city_info['lat']
    lng = city_info['lng']
    radius = city_info['radius']

    query = f'''[out:json][timeout:15];
(
  node{cat_info['query']}(around:{radius},{lat},{lng});
  way{cat_info['query']}(around:{radius},{lat},{lng});
);
out center 100;'''

    # 多个 Overpass 镜像,挨个试
    OVERPASS_MIRRORS = [
        'https://overpass.kumi.systems/api/interpreter',
        'https://z.overpass-api.de/api/interpreter',
        'https://overpass-api.de/api/interpreter',
    ]

    last_err = None
    for mirror in OVERPASS_MIRRORS:
        try:
            resp = requests.post(
                mirror,
                data={'data': query},
                timeout=20,
                headers={'User-Agent': 'GojoAssistant/1.0'},
            )
            resp.raise_for_status()
            data = resp.json()
            break
        except Exception as e:
            last_err = e
            print(f'[places] {mirror} 失败: {e}')
            continue
    else:
        # 所有镜像都失败,试 Nominatim 兜底
        print(f'[places] 所有 Overpass 镜像失败,尝试 Nominatim...')
        return _nominatim_fallback(city, category, cat_info, city_info, limit)

    results = []
    for el in data.get('elements', []):
        tags = el.get('tags', {})
        name = tags.get('name') or tags.get('name:ja') or tags.get('name:en')
        if not name:
            continue

        # 坐标:node 直接有,way 用 center
        place_lat = el.get('lat') or (el.get('center', {}).get('lat'))
        place_lng = el.get('lon') or (el.get('center', {}).get('lon'))
        if not place_lat or not place_lng:
            continue

        address = tags.get('addr:full') or tags.get('addr:street', '')
        if not address:
            # 拼接部分地址
            parts = [tags.get('addr:city', ''), tags.get('addr:district', ''),
                     tags.get('addr:street', ''), tags.get('addr:housenumber', '')]
            address = ''.join(p for p in parts if p)

        results.append({
            'name': name,
            'lat': float(place_lat),
            'lng': float(place_lng),
            'address': address or f'{city_info["name_jp"]}',
            'category': category,
            'category_label': cat_info['label_cn'],
            'city': city,
            'osm_id': el.get('id'),
        })

    # 缓存
    if results:
        _cache[cache_key] = (now, results)
        print(f'[places] {city}/{category}: 搜到 {len(results)} 个地点')

    return random.sample(results, min(limit, len(results))) if results else []


def get_random_place(city: str = 'tokyo', category: str = None) -> dict | None:
    """随机拿一个地点。category=None 就随机类别。"""
    if not category:
        category = random.choice(['cafe', 'restaurant', 'sweets', 'bakery', 'ramen'])
    places = search_places(city, category, limit=50)
    return random.choice(places) if places else None


def get_schedule_places(city: str = 'tokyo', count: int = 3) -> list:
    """给日程引擎用：搜几个不同类别的地点,供 LLM 挑选。"""
    categories = random.sample(
        ['cafe', 'restaurant', 'sweets', 'bakery', 'ramen', 'fashion', 'bookstore'],
        min(count + 2, 7)
    )
    places = []
    for cat in categories:
        p = get_random_place(city, cat)
        if p:
            places.append(p)
        if len(places) >= count:
            break
    return places


def _nominatim_fallback(city, category, cat_info, city_info, limit):
    """Overpass 全挂时用 Nominatim 搜(免费,不同域名)。"""
    try:
        search_terms = {
            'cafe': 'cafe coffee',
            'restaurant': 'restaurant',
            'sweets': 'sweets dessert patisserie',
            'bakery': 'bakery bread',
            'ramen': 'ramen noodle',
            'fashion': 'fashion clothing boutique',
            'bookstore': 'bookstore books',
            'convenience': 'convenience store',
        }
        q = search_terms.get(category, category)
        city_name = city_info.get('name_jp', 'Tokyo')

        resp = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={
                'q': f'{q} {city_name}',
                'format': 'json',
                'limit': min(limit, 30),
                'addressdetails': 1,
            },
            timeout=15,
            headers={'User-Agent': 'GojoAssistant/1.0'},
        )
        resp.raise_for_status()
        data = resp.json()

        results = []
        for item in data:
            name = item.get('display_name', '').split(',')[0]
            if not name:
                continue
            results.append({
                'name': name,
                'lat': float(item.get('lat', 0)),
                'lng': float(item.get('lon', 0)),
                'address': item.get('display_name', '').split(',')[1] if ',' in item.get('display_name', '') else city_name,
                'category': category,
                'category_label': cat_info['label_cn'],
                'city': city,
                'osm_id': item.get('osm_id'),
            })

        if results:
            print(f'[places] Nominatim 兜底成功: {city}/{category} 搜到 {len(results)} 个')
        return random.sample(results, min(limit, len(results))) if results else []

    except Exception as e:
        print(f'[places] Nominatim 也失败了: {e}')
        return []