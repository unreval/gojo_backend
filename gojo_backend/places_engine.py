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
from datetime import datetime

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


# ── 季节活动 / 花火大会 等固定地点 ──
# 这些活动有固定举办地点,Nominatim 搜不到"花火大会"但搜得到河/公园
# 日程引擎直接拿去喂 LLM,让角色安排去参加
EVENT_VENUES = {
    'tokyo': [
        # 花火大会(7-8月)
        {'name': '隅田川花火大会', 'lat': 35.7148, 'lng': 139.8019,
         'address': '隅田川 浅草-駒形', 'category': 'event', 'months': [7, 8],
         'label_cn': '花火大会'},
        {'name': '神宫外苑花火大会', 'lat': 35.6745, 'lng': 139.7145,
         'address': '神宫外苑', 'category': 'event', 'months': [8],
         'label_cn': '花火大会'},
        {'name': '江戸川区花火大会', 'lat': 35.7168, 'lng': 139.8969,
         'address': '江戸川河川敷', 'category': 'event', 'months': [8],
         'label_cn': '花火大会'},
        # 夏祭(7-8月)
        {'name': '麻布十番纳凉祭', 'lat': 35.6545, 'lng': 139.7367,
         'address': '麻布十番', 'category': 'event', 'months': [8],
         'label_cn': '夏祭'},
        {'name': '高圆寺阿波舞', 'lat': 35.7054, 'lng': 139.6495,
         'address': '高圆寺站周边', 'category': 'event', 'months': [8],
         'label_cn': '夏祭'},
        # 初詣(1月)
        {'name': '明治神宫初詣', 'lat': 35.6764, 'lng': 139.6993,
         'address': '原宿 明治神宫', 'category': 'event', 'months': [1, 12],
         'label_cn': '初詣'},
        # 红叶(11-12月)
        {'name': '六义园红叶灯光秀', 'lat': 35.7344, 'lng': 139.7454,
         'address': '�的场 六义园', 'category': 'event', 'months': [11, 12],
         'label_cn': '红叶'},
        # 樱花(3-4月)
        {'name': '目黑川赏樱', 'lat': 35.6447, 'lng': 139.6989,
         'address': '中目黑 目黑川沿岸', 'category': 'event', 'months': [3, 4],
         'label_cn': '赏樱'},
        {'name': '上野公园花见', 'lat': 35.7146, 'lng': 139.7734,
         'address': '上野公园', 'category': 'event', 'months': [3, 4],
         'label_cn': '赏樱'},
        # コミケ / 展会
        {'name': '东京Big Sight展会', 'lat': 35.6301, 'lng': 139.7965,
         'address': '有明 东京Big Sight', 'category': 'event', 'months': [8, 12],
         'label_cn': '展会'},
        # 万圣节(10月)
        {'name': '涩谷万圣节', 'lat': 35.6595, 'lng': 139.7004,
         'address': '涩谷站前', 'category': 'event', 'months': [10],
         'label_cn': '万圣节'},
        # 全年常设景点(作为活动备选)
        {'name': '台场海滨公园', 'lat': 35.6267, 'lng': 139.7755,
         'address': '台场', 'category': 'landmark', 'months': list(range(1, 13)),
         'label_cn': '景点'},
    ],
    'kyoto': [
        {'name': '伏见稻荷大社', 'lat': 34.9671, 'lng': 135.7727,
         'address': '伏见区', 'category': 'shrine', 'months': list(range(1, 13)),
         'label_cn': '神社'},
        {'name': '岚山竹林小径', 'lat': 35.0166, 'lng': 135.6717,
         'address': '右京区 岚山', 'category': 'landmark', 'months': list(range(1, 13)),
         'label_cn': '景点'},
        {'name': '祇园祭', 'lat': 35.0038, 'lng': 135.7729,
         'address': '四条通 八坂神社', 'category': 'event', 'months': [7],
         'label_cn': '祭典'},
        {'name': '清水寺红叶', 'lat': 34.9949, 'lng': 135.7850,
         'address': '东山区 清水寺', 'category': 'event', 'months': [11, 12],
         'label_cn': '红叶'},
    ],
    'osaka': [
        {'name': '天神祭', 'lat': 34.6929, 'lng': 135.5122,
         'address': '天满宫 大川沿岸', 'category': 'event', 'months': [7],
         'label_cn': '祭典'},
        {'name': '大阪城公园', 'lat': 34.6873, 'lng': 135.5262,
         'address': '中央区 大阪城', 'category': 'landmark', 'months': list(range(1, 13)),
         'label_cn': '景点'},
        {'name': '淀川花火大会', 'lat': 34.7229, 'lng': 135.4876,
         'address': '�的川河川敷', 'category': 'event', 'months': [8],
         'label_cn': '花火大会'},
    ],
}


def search_places(city='tokyo', category='cafe', limit=30):
    """Nominatim 搜索。支持所有品类。"""
    cache_key = f'{city}_{category}'
    now = time.time()
    if cache_key in _cache:
        ct, cd = _cache[cache_key]
        if now - ct < _CACHE_TTL:
            return random.sample(cd, min(limit, len(cd)))

    city_info = CITIES.get(city, CITIES['tokyo'])
    cat_info = CATEGORIES.get(category, CATEGORIES.get('cafe'))
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
        category = random.choice(list(CATEGORIES.keys()))
    places = search_places(city, category, limit=50)
    return random.choice(places) if places else None


def get_seasonal_events(city='tokyo', month=None):
    """拿当前月份适用的季节活动/地标。"""
    if month is None:
        month = datetime.now().month
    venues = EVENT_VENUES.get(city, [])
    return [v for v in venues if month in v.get('months', [])]


def get_schedule_places(city='tokyo', count=5):
    """给 schedule_engine 用:食 3 + 非食 2 左右的混搭。

    ★ v4:不再只搜吃的。一天的日程应该有吃有逛有打卡。
    """
    # 食物类:搜 3 个
    food_cats = random.sample(list(FOOD_CATEGORIES.keys()), min(3, len(FOOD_CATEGORIES)))
    food_places = []
    for cat in food_cats:
        p = get_random_place(city, cat)
        if p:
            food_places.append(p)
        if len(food_places) >= 3:
            break

    # 非食类:搜 2 个
    act_cats = random.sample(list(ACTIVITY_CATEGORIES.keys()), min(3, len(ACTIVITY_CATEGORIES)))
    act_places = []
    for cat in act_cats:
        p = get_random_place(city, cat)
        if p:
            act_places.append(p)
        if len(act_places) >= 2:
            break

    # 季节活动:当月适用的挑 1 个(概率 40%)
    events = get_seasonal_events(city)
    event_place = None
    if events and random.random() < 0.40:
        evt = random.choice(events)
        event_place = {
            'name': evt['name'], 'lat': evt['lat'], 'lng': evt['lng'],
            'address': evt['address'], 'category': evt['category'],
            'category_label': evt.get('label_cn', '活动'),
            'city': city, 'osm_id': None,
        }

    # 合并,控制总量
    combined = food_places + act_places
    if event_place:
        combined.append(event_place)
    random.shuffle(combined)
    return combined[:count]