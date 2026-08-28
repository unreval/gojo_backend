"""route_explore.py —— 探店地图路由 v2

  GET  /explore/visited     角色去过的店(地图打点)
  GET  /explore/search      搜附近的店(Nominatim)
  POST /explore/visit       手动标记一家店为"去过"

v2 改动:
  · /explore/visited 支持 with_schedule=1,会把今天的日程时间交叉关联回来
    → 前端拿到每个地点对应的日程 start_time,就能按时间进度渐进显示
  · 修复 character_id / city 联合过滤
"""
from datetime import datetime
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import CN_TZ
import db_visited_places
import places_engine
import db_schedule

router = APIRouter()
DEFAULT_USER = 'user_mofpiyd7442ia7'


def _today():
    return datetime.now(CN_TZ).date()


@router.get('/explore/visited')
async def get_visited(user_id: str = DEFAULT_USER,
                      character_id: str = None,
                      city: str = None,
                      with_schedule: int = 0):
    """拿角色去过的店列表(地图打点用)。

    with_schedule=1 时,为今天的地点附加日程 start_time:
      · 前端用这个判断"日程还没到就不显示"
      · 历史地点 sched_start = null → 始终显示
    """
    items = db_visited_places.list_visited(user_id, character_id, city)
    total = db_visited_places.count_visited(user_id, character_id)

    # ★ 关联日程时间
    if with_schedule:
        today = str(_today())
        now = datetime.now(CN_TZ).strftime('%H:%M')
        # 拿今天所有角色的日程
        today_scheds = _get_today_schedules(user_id, character_id)

        for item in items:
            item['sched_start'] = None
            if item.get('visit_date') != today:
                continue
            # 找匹配的日程条目
            cid = item.get('character_id', '')
            name = item.get('place_name', '')
            addr = item.get('place_address', '')
            for s in today_scheds.get(cid, []):
                text = (s.get('title', '') + ' ' + s.get('location', '')).strip()
                if name and (name in text or (s.get('location', '') and s['location'] in name)):
                    item['sched_start'] = s['start_time']
                    break

        return JSONResponse({
            'places': items, 'total': total,
            'now': now, 'date': today,
        })

    return JSONResponse({'places': items, 'total': total})


def _get_today_schedules(user_id, character_id=None):
    """拿今天的日程,按角色分组。"""
    today = _today()
    result = {}
    if character_id:
        char_ids = [character_id]
    else:
        # 拿所有有探店记录的角色
        try:
            from characters import list_characters
            char_ids = [c['id'] for c in list_characters()]
        except Exception:
            char_ids = ['gojo', 'geto', 'minato']

    for cid in char_ids:
        try:
            items = db_schedule.get_schedule(cid, user_id, today)
            result[cid] = items
        except Exception:
            result[cid] = []
    return result


@router.get('/explore/search')
async def search_nearby(city: str = 'tokyo', category: str = 'cafe',
                        limit: int = 20):
    """搜指定城市的真实店铺(Nominatim,免费)。"""
    places = places_engine.search_places(city, category, limit)
    return JSONResponse({'places': places, 'count': len(places)})


@router.post('/explore/visit')
async def mark_visit(data: dict):
    """手动标记一家店为"角色去过"。日程引擎也会自动调这个。"""
    user_id = data.get('user_id', DEFAULT_USER)
    character_id = data.get('character_id', 'gojo')
    place = data.get('place', {})
    review = data.get('review', '')
    visit_date = data.get('visit_date')

    if not place.get('name') or not place.get('lat'):
        return JSONResponse({'error': 'place needs name and lat/lng'}, status_code=400)

    new_id = db_visited_places.add_visited(
        character_id, user_id, place, review, visit_date
    )
    return JSONResponse({'ok': True, 'id': new_id})