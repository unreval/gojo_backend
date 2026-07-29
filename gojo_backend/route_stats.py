"""通用查询路由:
- /stats?user_id=X          → 首页要展示的"陪伴天数"等统计
- /characters_all           → 列出所有角色(给日记列表页动态铺卡片用)

用了 /characters_all 这种带下划线的名字是为了避开 route_character.py 里
已经存在的 GET /characters/{id}——放同名字面路径可能会互相盖住,分开省心。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db import get_conn

router = APIRouter()


@router.get('/stats')
async def get_stats(user_id: str = 'default'):
    """返回用户的聊天累计天数、首末日期。
    没有记录就返回 0(不是 404,免得前端把它当错误)。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT first_chat_date, last_chat_date, total_days FROM user_stats WHERE user_id = %s',
        (user_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return JSONResponse({
            'user_id': user_id,
            'total_days': 0,
            'first_chat_date': None,
            'last_chat_date': None,
        })
    return JSONResponse({
        'user_id': user_id,
        'total_days': int(row[2] or 0),
        'first_chat_date': row[0],
        'last_chat_date': row[1],
    })


@router.get('/characters_all')
async def list_all_characters():
    """列出 characters 表里所有角色的基本信息,给日记列表这类"多角色"页面用。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, name, avatar_url FROM characters ORDER BY created_at ASC, id ASC'
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return JSONResponse({
        'characters': [{'id': r[0], 'name': r[1], 'avatar_url': r[2]} for r in rows],
    })
