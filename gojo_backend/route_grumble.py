"""便利贴路由 /grumbles/*

  GET    /grumbles                  列表(可选 character_id 过滤;不传就全部角色混着来)
  GET    /grumbles/unviewed_count   未看条数(首页红点用)
  POST   /grumbles/mark_viewed      打开便利贴页时一键标已看
  DELETE /grumbles/{id}             撕掉一张(用户隐藏,不改 cognitive status)

便利贴是 Persistent Cognitive System 的表达层,不是每轮聊天后的第二次角色扮演。
数据源: cognitive_sticky_notes,且仅 source=cognitive_slow_loop。
memory_lifecycle_fast_loop 的 sticky 是内部记忆提示,不进入本 API。
已读/撕掉与 Slow Loop 的 completed/expired 生命周期相互独立。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from cognitive_config import USER_FACING_STICKY_SOURCE
from cognitive_reader import (
    count_unviewed_sticky_notes,
    hide_sticky_note,
    list_user_facing_sticky_notes,
    mark_sticky_notes_viewed,
)


router = APIRouter()

# 与 route_diary 一致,单用户模式下的固定 user_id 默认值
DEFAULT_USER = 'user_mofpiyd7442ia7'


def _public_grumble(note):
    return {
        'id': note['id'],
        'character_id': note['character_id'],
        'content': note['content'],
        'emotion': note.get('emotion') or '',
        'tone': note.get('tone') or '',
        'trigger_snippet': note.get('trigger_snippet') or '',
        'tag': note.get('tag') or note.get('emotion_tag') or '·',
        'emotion_tag': note.get('emotion_tag') or note.get('tag') or '·',
        'source': note.get('source') or USER_FACING_STICKY_SOURCE,
        'created_at': note.get('created_at'),
        'updated_at': note.get('updated_at'),
        'viewed': bool(note.get('viewed')),
        'source_event_refs': note.get('source_event_refs') or [],
        'created_by_cycle_id': note.get('created_by_cycle_id'),
        'updated_by_cycle_id': note.get('updated_by_cycle_id'),
        'note_key': note.get('note_key'),
        'status': note.get('status'),
    }


@router.get('/grumbles')
async def get_grumbles(user_id: str = DEFAULT_USER,
                       character_id: str = None,
                       limit: int = 100):
    """列出 user-facing Slow Loop 便利贴。character_id 传空就是全部角色混着来。"""
    items = list_user_facing_sticky_notes(
        user_id, character_id or None, limit=limit,
    )
    return JSONResponse({'grumbles': [_public_grumble(item) for item in items]})


@router.get('/grumbles/unviewed_count')
async def unviewed_count(user_id: str = DEFAULT_USER, character_id: str = None):
    """首页给便利贴 tile 显示红点用。character_id 可选。"""
    n = count_unviewed_sticky_notes(
        user_id, character_id or None, source=USER_FACING_STICKY_SOURCE,
    )
    return JSONResponse({'count': n})


@router.post('/grumbles/mark_viewed')
async def mark_viewed(data: dict):
    """打开便利贴页时前端调一下,把未看的标为已看。不改 semantic status。"""
    user_id = data.get('user_id', DEFAULT_USER)
    character_id = data.get('character_id') or None
    n = mark_sticky_notes_viewed(
        user_id, character_id, source=USER_FACING_STICKY_SOURCE,
    )
    return JSONResponse({'ok': True, 'marked': n})


@router.delete('/grumbles/{grumble_id}')
async def del_grumble(grumble_id: int, user_id: str = DEFAULT_USER):
    """撕掉一张便利贴:用户隐藏,不把 cognitive status 改成 completed。"""
    ok = hide_sticky_note(user_id, grumble_id)
    return JSONResponse({'ok': ok})
