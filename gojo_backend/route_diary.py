"""日记路由 /diary/*

他的日记：
  GET  /diary/char/{character_id}?user_id=      —— 读他的日记（含你留过的评论）
  POST /diary/char/{diary_id}/comment           —— 你在他某篇日记下留言（他之后会"发现"）

你的日记：
  GET  /diary/user?user_id=                      —— 读你的日记（含他的访客记号）
  POST /diary/user                               —— 写一篇你的日记（可选 visibility/password）
  PUT  /diary/user/{diary_id}                    —— 改内容/可见性/密码
  POST /diary/user/{diary_id}/password           —— 改密码（空=解锁变可见）
  DELETE /diary/user/{diary_id}?user_id=         —— 删一篇

补偿：
  POST /diary/catch_up                           —— 开 App 时前端调，补跑漏掉的写/看

调试用（可选）：
  POST /diary/char/{character_id}/write_now      —— 立刻让他写一篇（测试用）
  POST /diary/peek_now                           —— 立刻让他偷看一次（测试用）
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import DEFAULT_CHARACTER_ID
import db_diary
import diary_engine
import diary_scheduler

router = APIRouter()

DEFAULT_USER = 'user_mofpiyd7442ia7'


# ─────────── 他的日记 ───────────

@router.get('/diary/char/{character_id}')
async def get_char_diary(character_id: str, user_id: str = DEFAULT_USER):
    diaries = db_diary.list_char_diaries(character_id, user_id, limit=50)
    return JSONResponse({'diaries': diaries})


@router.post('/diary/char/{diary_id}/comment')
async def comment_char_diary(diary_id: int, data: dict):
    user_id = data.get('user_id', DEFAULT_USER)
    content = (data.get('content') or '').strip()
    if not content:
        return JSONResponse({'error': 'empty comment'}, status_code=400)
    cid, created_at = db_diary.add_diary_comment(diary_id, user_id, content)
    return JSONResponse({'ok': True, 'comment_id': cid, 'created_at': str(created_at)})


# ─────────── 你的日记 ───────────

@router.get('/diary/user')
async def get_user_diary(user_id: str = DEFAULT_USER):
    diaries = db_diary.list_user_diaries(user_id, limit=50)
    return JSONResponse({'diaries': diaries})


@router.post('/diary/user')
async def create_user_diary(data: dict):
    user_id    = data.get('user_id', DEFAULT_USER)
    content    = (data.get('content') or '').strip()
    visibility = data.get('visibility', 'open')
    password   = data.get('password') or None
    if not content:
        return JSONResponse({'error': 'empty diary'}, status_code=400)
    did, created_at = db_diary.add_user_diary(user_id, content, visibility, password)
    return JSONResponse({'ok': True, 'diary_id': did, 'created_at': str(created_at)})


@router.put('/diary/user/{diary_id}')
async def edit_user_diary(diary_id: int, data: dict):
    user_id = data.get('user_id', DEFAULT_USER)
    fields = {}
    for k in ('content', 'visibility', 'password'):
        if k in data:
            fields[k] = data[k]
    ok = db_diary.update_user_diary(diary_id, user_id, fields)
    return JSONResponse({'ok': ok})


@router.post('/diary/user/{diary_id}/password')
async def set_diary_password(diary_id: int, data: dict):
    user_id = data.get('user_id', DEFAULT_USER)
    new_pw  = data.get('password', '')   # 空串=取消上锁
    ok = db_diary.change_diary_password(diary_id, user_id, new_pw)
    return JSONResponse({'ok': ok})


@router.delete('/diary/user/{diary_id}')
async def remove_user_diary(diary_id: int, user_id: str = DEFAULT_USER):
    db_diary.delete_user_diary(diary_id, user_id)
    return JSONResponse({'ok': True})


# ─────────── 补偿 & 调试 ───────────

@router.post('/diary/catch_up')
async def catch_up(data: dict):
    user_id      = data.get('user_id', DEFAULT_USER)
    character_id = data.get('character_id', 'gojo')
    result = diary_scheduler.catch_up(user_id, character_id)
    return JSONResponse(result)


@router.post('/diary/char/{character_id}/write_now')
async def write_now(character_id: str, data: dict):
    """测试用：立刻让他写一篇。"""
    user_id = data.get('user_id', DEFAULT_USER)
    r = diary_engine.generate_char_diary(character_id, user_id)
    if not r:
        return JSONResponse({'ok': False, 'error': 'generate failed'})
    did, content, emotion = r
    return JSONResponse({'ok': True, 'diary_id': did, 'content': content, 'emotion': emotion})


@router.post('/diary/peek_now')
async def peek_now(data: dict):
    """测试用：立刻让他偷看一次你的日记。"""
    user_id      = data.get('user_id', DEFAULT_USER)
    character_id = data.get('character_id', 'gojo')
    r = diary_engine.peek_user_diary(character_id, user_id)
    if not r:
        return JSONResponse({'ok': False, 'note': '没有可看的日记（你还没写，或他都看过了）'})
    _visited, diary_id, unlocked = r
    return JSONResponse({'ok': True, 'diary_id': diary_id, 'unlocked': unlocked})
