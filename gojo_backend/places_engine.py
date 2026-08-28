"""places_engine.py v3 —— Nominatim 为主(Zeabur 上能用)

Overpass 在 Zeabur 上全部被封。Nominatim 能用且免费,直接用它。
"""
import requests
import random
import time

_cache: dict = {}
_CACHE_TTL = 24 * 3600

CITIES = {
    'tokyo': {'lat': 35.6762, 'lng': 139.6503, 'name_jp': '東京', 'name_cn': '东京'},
    'kyoto': {'lat': 35.0116, 'lng': 135.7681, 'name_jp': '京都', 'name_cn': '京都'},
    'osaka': {'lat': 34.6937, 'lng': 135.5023, 'name_jp': '大阪', 'name_cn': '大阪'},
    'yokohama': {'lat': 35.4437, 'lng': 139.6380, 'name_jp': '横浜', 'name_cn': '横滨'},
    'fukuoka': {'lat': 33.5904, 'lng': 130.4017, 'name_jp': '福岡', 'name_cn': '福冈'},
    'nagoya': {'lat': 35.1815, 'lng': 136.9066, 'name_jp': '名古屋', 'name_cn': '名古屋'},
    'sapporo': {'lat': 43.0618, 'lng': 141.3545, 'name_jp': '札幌', 'name_cn': '札幌'},
    'kobe': {'lat': 34.6901, 'lng': 135.1956, 'name_jp': '神戸', 'name_cn': '神户'},
}

CATEGORIES = {
    'cafe':        {'q': 'cafe coffee カフェ', 'label_cn': '咖啡厅'},
    'restaurant':  {'q': 'restaurant レストラン 食堂', 'label_cn': '餐厅'},
    'sweets':      {'q': 'sweets patisserie ケーキ 甜品', 'label_cn': '甜品店'},
    'bakery':      {'q': 'bakery パン屋 面包', 'label_cn': '面包店'},
    'ramen':       {'q': 'ramen ラーメン 拉面', 'label_cn': '拉面店'},
    'fashion':     {'q': 'fashion boutique ファッション', 'label_cn': '服装店'},
    'bookstore':   {'q': 'bookstore 書店 本屋', 'label_cn': '书店'},
}


def search_places(city='tokyo', category='cafe', limit=30):
    cache_key = f'{city}_{category}'
    now = time.time()
    if cache_key in _cache:
        ct, cd = _cache[cache_key]
        if now - ct < _CACHE_TTL:
            return random.sample(cd, min(limit, len(cd)))

    city_info = CITIES.get(city, CITIES['tokyo'])
    cat_info = CATEGORIES.get(category, CATEGORIES['cafe'])

    try:
        resp = requests.get(
            'https://nominatim.openstreetmap.org/search',
            params={
                'q': f'{cat_info["q"]} {city_info["name_jp"]}',
                'format': 'json',
                'limit': 50,
                'addressdetails': 1,
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
        display = item.get('display_name', '')
        parts = display.split(',')
        name = parts[0].strip() if parts else ''
        if not name or len(name) < 2:
            continue
        lat = float(item.get('lat', 0))
        lng = float(item.get('lon', 0))
        if not lat or not lng:
            continue
        addr = ', '.join(p.strip() for p in parts[1:3]) if len(parts) > 1 else city_info['name_cn']
        results.append({
            'name': name, 'lat': lat, 'lng': lng,
            'address': addr, 'category': category,
            'category_label': cat_info['label_cn'],
            'city': city, 'osm_id': item.get('osm_id'),
        })

    if results:
        _cache[cache_key] = (now, results)
        print(f'[places] ✅ {city}/{category}: 搜到 {len(results)} 个地点')
    return random.sample(results, min(limit, len(results))) if results else []


def get_random_place(city='tokyo', category=None):
    if not category:
        category = random.choice(['cafe', 'restaurant', 'sweets', 'bakery', 'ramen'])
    places = search_places(city, category, limit=50)
    return random.choice(places) if places else None


def get_schedule_places(city='tokyo', count=3):
    categories = random.sample(list(CATEGORIES.keys()), min(count + 2, len(CATEGORIES)))
    places = []
    for cat in categories:
        p = get_random_place(city, cat)
        if p:
            places.append(p)
        if len(places) >= count:
            break
    return places