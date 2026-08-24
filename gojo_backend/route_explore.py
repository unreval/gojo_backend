"""route_explore.py —— 探店地图路由

  GET  /explore/visited     角色去过的店(地图打点)
  GET  /explore/search      搜附近的店(Overpass)
  POST /explore/visit       手动标记一家店为"去过"
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

import db_visited_places
import places_engine

router = APIRouter()
DEFAULT_USER = 'user_mofpiyd7442ia7'


@router.get('/explore/visited')
async def get_visited(user_id: str = DEFAULT_USER,
                      character_id: str = None,
                      city: str = None):
    """拿角色去过的店列表(地图打点用)。"""
    items = db_visited_places.list_visited(user_id, character_id, city)
    total = db_visited_places.count_visited(user_id, character_id)
    return JSONResponse({'places': items, 'total': total})


@router.get('/explore/search')
async def search_nearby(city: str = 'tokyo', category: str = 'cafe',
                        limit: int = 20):
    """搜指定城市的真实店铺(Overpass API,免费)。"""
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
