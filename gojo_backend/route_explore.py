"""route_explore.py —— 探店地图路由 v2

  GET  /explore/visited     角色去过的店(地图打点)
  GET  /explore/search      搜附近的店(Nominatim)
  POST /explore/visit       手动标记一家店为"去过"

v2 改动:
  · /explore/visited 支持 with_schedule=1,返回 planned_places（计划）和
    completed visits（已访问）两个明确集合，绝不把计划冒充成访问记录。
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

    with_schedule=1 时，计划地点单独放在 planned_places；places 只包含已经
    authoritative completed 的访问记录。
    """
    items = db_visited_places.list_visited(user_id, character_id, city)
    total = db_visited_places.count_visited(user_id, character_id)

    # ★ 关联日程时间
    if with_schedule:
        today = str(_today())
        now = datetime.now(CN_TZ).strftime('%H:%M')
        # Canonical planned locations are intentionally separate from visits.
        today_scheds = _get_today_schedules(user_id, character_id)
        planned_places = []
        for cid, schedule in today_scheds.items():
            for event in schedule:
                if event.get('status') not in ('planned', 'active'):
                    continue
                place = event.get('planned_place') or {}
                if not place:
                    continue
                planned_places.append({
                    'character_id': cid,
                    'event_id': event.get('id'),
                    'revision': event.get('revision'),
                    'status': event.get('status'),
                    'planned_start_at': event.get('planned_start_at'),
                    'planned_end_at': event.get('planned_end_at'),
                    'title': event.get('title', ''),
                    'place': place,
                })

        return JSONResponse({
            'places': items, 'total': total,
            'planned_places': planned_places,
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
            items = db_schedule.get_canonical_schedule(cid, user_id, today)
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
    """Manually record a completed visit. Schedule generation never calls this."""
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
