"""便利贴吐槽路由 /grumbles/*

  GET    /grumbles                  列表(可选 character_id 过滤;不传就全部角色混着来)
  GET    /grumbles/unviewed_count   未看条数(首页红点用)
  POST   /grumbles/mark_viewed      打开便利贴页时一键标已看
  DELETE /grumbles/{id}             撕掉一张

★ 便利贴【只能读和删】,不提供 POST 创建 —— 它是 AI 自己产出的,不是用户能主动添加的东西。
   写入在 grumble_engine.maybe_write_grumble,由 route_chat 后台线程触发。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

import db_grumble

router = APIRouter()

# 与 route_diary 一致,单用户模式下的固定 user_id 默认值
DEFAULT_USER = 'user_mofpiyd7442ia7'


@router.get('/grumbles')
async def get_grumbles(user_id: str = DEFAULT_USER,
                       character_id: str = None,
                       limit: int = 100):
    """列出便利贴。character_id 传空就是全部角色混着来(按时间倒序)。"""
    items = db_grumble.list_grumbles(user_id, character_id, limit=limit)
    return JSONResponse({'grumbles': items})


@router.get('/grumbles/unviewed_count')
async def unviewed_count(user_id: str = DEFAULT_USER, character_id: str = None):
    """首页给便利贴 tile 显示红点用。character_id 可选。"""
    n = db_grumble.count_unviewed(user_id, character_id)
    return JSONResponse({'count': n})


@router.post('/grumbles/mark_viewed')
async def mark_viewed(data: dict):
    """打开便利贴页时前端调一下,把所有未看的标为已看。"""
    user_id = data.get('user_id', DEFAULT_USER)
    character_id = data.get('character_id')   # 可选:只标某个角色的
    n = db_grumble.mark_all_viewed(user_id, character_id)
    return JSONResponse({'ok': True, 'marked': n})


@router.delete('/grumbles/{grumble_id}')
async def del_grumble(grumble_id: int, user_id: str = DEFAULT_USER):
    """撕掉一张便利贴。带 user_id 做基本鉴权。"""
    ok = db_grumble.delete_grumble(grumble_id, user_id)
    return JSONResponse({'ok': ok})
