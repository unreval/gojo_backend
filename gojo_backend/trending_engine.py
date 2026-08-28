"""trending_engine.py —— 每周搜一次当季限定/新品/联名

用 Claude web_search 工具搜索:
  · 当季限定甜品
  · 新开的网红店
  · 限量联名商品

缓存 7 天。日程引擎调 get_trending_for_schedule() 拿文本块塞进 prompt。
"""
import time
import threading
from datetime import datetime
from config import ANTHROPIC_KEY, CN_TZ

_cache: dict = {}
_CACHE_TTL = 7 * 24 * 3600
_lock = threading.Lock()


def _now_month():
    n = datetime.now(CN_TZ)
    return n.year, n.month


def _search_trending(city='tokyo'):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    year, month = _now_month()
    city_jp = {'tokyo': '東京', 'kyoto': '京都', 'osaka': '大阪',
               'yokohama': '横浜', 'fukuoka': '福岡', 'nagoya': '名古屋',
               'sapporo': '札幌', 'kobe': '神戸'}.get(city, '東京')
    city_cn = {'tokyo': '东京', 'kyoto': '京都', 'osaka': '大阪',
               'yokohama': '横滨', 'fukuoka': '福冈', 'nagoya': '名古屋',
               'sapporo': '札幌', 'kobe': '神户'}.get(city, '东京')

    prompt = f'''搜索{city_cn}({city_jp}){year}年{month}月的:
1. 当季限定甜品/饮品(如某咖啡厅夏季限定冰品)
2. 最近新开的网红店(新咖啡厅/面包店/甜品店)
3. 限量联名商品(品牌联名/期间限定pop-up)

每类3-5条,用中文,JSON格式:
{{"trending":[
  {{"type":"限定","name":"商品名","store":"店铺名","area":"区域","detail":"一句话","rating":"好评/一般/踩雷"}},
  {{"type":"新店","name":"店名","area":"区域","detail":"一句话","rating":"好评/一般"}},
  {{"type":"联名","name":"联名名","store":"店","area":"区域","detail":"一句话","rating":"好评/一般"}}
]}}
至少5条。严格JSON,不要解释。'''

    try:
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2000,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            messages=[{'role': 'user', 'content': prompt}],
        )
        text_parts = []
        for block in resp.content:
            if hasattr(block, 'text'):
                text_parts.append(block.text)
        raw = '\n'.join(text_parts).strip()
        if not raw:
            return []
        from utils import extract_json
        parsed = extract_json(raw)
        if parsed and isinstance(parsed.get('trending'), list):
            items = parsed['trending']
            print(f'[trending] ✅ {city}: 搜到 {len(items)} 条限定/新品/联名')
            return items
        print(f'[trending] {city} 解析失败: {raw[:200]}')
        return []
    except Exception as e:
        err = str(e)
        if '404' in err or 'not supported' in err or 'tool' in err.lower():
            print(f'[trending] ⚠️ web_search 不可用(tdyun可能不支持),尝试无工具搜索')
            return _search_without_tool(city, city_cn, year, month)
        print(f'[trending] 搜索出错: {err[:200]}')
        return []


def _search_without_tool(city, city_cn, year, month):
    """web_search 工具不可用时,直接让 Claude 用训练数据里的知识回答。
    不是实时的,但 Claude 知道很多日本的连锁店和常见的季节限定。"""
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    month_season = {1:'冬',2:'冬',3:'春',4:'春',5:'春',6:'夏',7:'夏',8:'夏',9:'秋',10:'秋',11:'秋',12:'冬'}
    season = month_season.get(month, '夏')

    prompt = f'''你是一个日本美食和潮流专家。请根据你的知识,列出{city_cn}在{season}季({month}月前后)通常会有的:
1. 季节限定甜品/饮品(真实存在的连锁店或知名店铺的季节限定,如星巴克限定/便利店限定/知名甜品店限定)
2. {city_cn}值得打卡的人气店铺(常年热门的,不需要是新开的)
3. 日本品牌常见的联名/限定商品(如便利店联名/动漫联名)

每类3-5条,用中文,必须是真实存在的店铺和商品:
{{"trending":[
  {{"type":"限定","name":"商品名","store":"店铺名","area":"区域","detail":"一句话","rating":"好评/一般/踩雷"}},
  {{"type":"热门","name":"店名","area":"区域","detail":"一句话","rating":"好评/一般"}},
  {{"type":"联名","name":"联名名","store":"店","area":"区域","detail":"一句话","rating":"好评/一般"}}
]}}
至少8条。严格JSON。'''

    try:
        resp = client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = resp.content[0].text.strip() if resp.content else ''
        from utils import extract_json
        parsed = extract_json(raw)
        if parsed and isinstance(parsed.get('trending'), list):
            items = parsed['trending']
            print(f'[trending] ✅ {city} (无工具模式): 拿到 {len(items)} 条')
            return items
        return []
    except Exception as e:
        print(f'[trending] 无工具模式也失败: {e}')
        return []


def get_trending(city='tokyo', force=False):
    now = time.time()
    with _lock:
        if not force and city in _cache:
            ct, cd = _cache[city]
            if now - ct < _CACHE_TTL:
                return cd
    items = _search_trending(city)
    with _lock:
        if items:
            _cache[city] = (now, items)
    return items


def get_trending_for_schedule(city='tokyo'):
    """给 schedule_engine 用。返回一段文本直接塞 prompt。"""
    items = get_trending(city)
    if not items:
        return ''
    lines = []
    for it in items:
        t = it.get('type', '限定')
        name = it.get('name', '')
        store = it.get('store', '')
        area = it.get('area', '')
        detail = it.get('detail', '')
        rating = it.get('rating', '')
        line = f'  · [{t}] {name}'
        if store: line += f' @ {store}'
        if area: line += f'({area})'
        if detail: line += f' — {detail}'
        if rating: line += f' [{rating}]'
        lines.append(line)
    return f'''
【当季限定/新品/联名(可以安排角色去打卡/排队/吐槽)】
{chr(10).join(lines)}
角色可以挑感兴趣的去。踩雷的在 note 里吐槽。不必全用。
'''