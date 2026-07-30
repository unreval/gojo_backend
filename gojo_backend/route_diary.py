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
from db import get_conn
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


# ★ 删单篇他的日记(附带的 comment 也删掉)
@router.delete('/diary/char_entry/{diary_id}')
async def delete_char_diary_entry(diary_id: int, user_id: str = DEFAULT_USER):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute('DELETE FROM char_diary_comment WHERE diary_id = %s', (diary_id,))
        # 尝试带 user_id 过滤(如果 char_diary 有该列),失败则退回不带
        try:
            cur.execute('DELETE FROM char_diary WHERE id = %s AND user_id = %s',
                        (diary_id, user_id))
        except Exception:
            conn.rollback()
            cur = conn.cursor()
            cur.execute('DELETE FROM char_diary WHERE id = %s', (diary_id,))
        rows = cur.rowcount
        conn.commit()
    finally:
        cur.close()
        conn.close()
    return JSONResponse({'ok': True, 'deleted': rows})


# ★ 清空某角色的所有日记(慎用)
@router.delete('/diary/char/{character_id}/all')
async def clear_char_diaries(character_id: str, user_id: str = DEFAULT_USER):
    conn = get_conn()
    cur = conn.cursor()
    try:
        # 先拿到要删的 id 列表,再删 comment,最后删 diary
        try:
            cur.execute(
                'SELECT id FROM char_diary WHERE character_id = %s AND user_id = %s',
                (character_id, user_id)
            )
        except Exception:
            conn.rollback()
            cur = conn.cursor()
            cur.execute(
                'SELECT id FROM char_diary WHERE character_id = %s',
                (character_id,)
            )
        ids = [r[0] for r in cur.fetchall()]
        if ids:
            cur.execute('DELETE FROM char_diary_comment WHERE diary_id = ANY(%s)', (ids,))
        try:
            cur.execute(
                'DELETE FROM char_diary WHERE character_id = %s AND user_id = %s',
                (character_id, user_id)
            )
        except Exception:
            conn.rollback()
            cur = conn.cursor()
            cur.execute(
                'DELETE FROM char_diary WHERE character_id = %s',
                (character_id,)
            )
        rows = cur.rowcount
        conn.commit()
        print(f'[diary] 清空 {character_id} 全部日记:{rows} 条')
    finally:
        cur.close()
        conn.close()
    return JSONResponse({'ok': True, 'deleted': rows})


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


# ★ 清空你所有的日记(附带的访客记号也删掉)
@router.delete('/diary/user_all')
async def clear_all_user_diaries(user_id: str = DEFAULT_USER):
    conn = get_conn()
    cur = conn.cursor()
    try:
        # 先删访客记号
        try:
            cur.execute('DELETE FROM diary_visit WHERE user_id = %s', (user_id,))
        except Exception:
            conn.rollback()
            cur = conn.cursor()
        # 再删日记本身
        cur.execute('DELETE FROM user_diary WHERE user_id = %s', (user_id,))
        rows = cur.rowcount
        conn.commit()
        print(f'[diary] 清空 {user_id} 全部我的日记:{rows} 条')
    finally:
        cur.close()
        conn.close()
    return JSONResponse({'ok': True, 'deleted': rows})


# ─────────── 日记本名字（A+C）───────────

@router.get('/diary/book/{owner}')
async def get_book(owner: str, user_id: str = DEFAULT_USER):
    """取日记本名字。owner='user'=你那本；owner=角色id=他那本。"""
    if owner == 'user':
        default = '我的日记'
    else:
        default = f'{owner}的日记'
    info = db_diary.get_book_title(user_id, owner, default)
    return JSONResponse(info)


@router.put('/diary/book/{owner}')
async def rename_book(owner: str, data: dict):
    """改名（你手动改；他那本你也能改）。"""
    user_id = data.get('user_id', DEFAULT_USER)
    title = (data.get('title') or '').strip()
    if not title:
        return JSONResponse({'error': 'empty title'}, status_code=400)
    db_diary.set_book_title(user_id, owner, title[:20], named_by_self=False)
    return JSONResponse({'ok': True, 'title': title[:20]})


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