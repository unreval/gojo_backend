"""route_chatlog_search.py —— 聊天记录搜索(按关键词 / 按日期)

  GET  /chatlog/search?user_id=&chat_id=&keyword=&limit=
       → 返回所有含该关键词的消息(搜 text + subtitle),按时间倒序

  GET  /chatlog/dates?user_id=&chat_id=
       → 返回有聊天记录的所有日期(用于日历视图打点)

  GET  /chatlog/by_date?user_id=&chat_id=&date=2026-08-14&around=50
       → 返回指定日期前后的消息(用于日期跳转)

这是 route_chatlog.py 的搜索扩展,挂载时加到 gojo_server.py 即可。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from db import get_conn

router = APIRouter()


@router.get('/chatlog/search')
async def search_chatlog(user_id: str, chat_id: str,
                         keyword: str = '', limit: int = 100):
    """搜索所有包含关键词的消息,text 和 subtitle 都搜,按时间倒序。"""
    keyword = (keyword or '').strip()
    if not keyword or not user_id or not chat_id:
        return JSONResponse({'results': [], 'count': 0})

    limit = max(1, min(500, limit))
    conn = get_conn()
    cur = conn.cursor()
    try:
        # ILIKE = 不区分大小写的 LIKE(PostgreSQL)
        pattern = f'%{keyword}%'
        cur.execute(
            '''SELECT id, client_msg_id, role, text, subtitle, emotion,
                      kind, extra, has_audio, created_at
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s
                 AND (text ILIKE %s OR subtitle ILIKE %s)
               ORDER BY created_at DESC
               LIMIT %s''',
            (user_id, chat_id, pattern, pattern, limit)
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    results = [{
        'id': r[0],
        'client_msg_id': r[1] or '',
        'role': r[2],
        'text': r[3] or '',
        'subtitle': r[4] or '',
        'emotion': r[5] or '',
        'kind': r[6] or 'text',
        'extra': r[7] or '',
        'has_audio': bool(r[8]),
        'ts': r[9].isoformat() if r[9] else None,
    } for r in rows]

    return JSONResponse({'results': results, 'count': len(results)})


@router.get('/chatlog/dates')
async def chatlog_dates(user_id: str, chat_id: str):
    """返回有聊天记录的所有日期列表(YYYY-MM-DD 字符串),用于日历视图打点。"""
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT DISTINCT DATE(created_at) as d
               FROM chat_log
               WHERE user_id=%s AND chat_id=%s AND created_at IS NOT NULL
               ORDER BY d DESC''',
            (user_id, chat_id)
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    dates = [str(r[0]) for r in rows if r[0]]
    return JSONResponse({'dates': dates})


@router.get('/chatlog/by_date')
async def chatlog_by_date(user_id: str, chat_id: str,
                          date: str = '', around: int = 50):
    """返回指定日期的消息,以及前后各 around 条用于上下文。
    前端用这个实现"点日期跳转到那天的聊天"。"""
    if not date or not user_id or not chat_id:
        return JSONResponse({'messages': [], 'has_more_before': False, 'has_more_after': False})

    around = max(10, min(200, around))
    conn = get_conn()
    cur = conn.cursor()
    try:
        # 先找到该日期第一条消息的 id
        cur.execute(
            '''SELECT id FROM chat_log
               WHERE user_id=%s AND chat_id=%s AND DATE(created_at) = %s
               ORDER BY id ASC LIMIT 1''',
            (user_id, chat_id, date)
        )
        anchor = cur.fetchone()
        if not anchor:
            return JSONResponse({'messages': [], 'has_more_before': False, 'has_more_after': False})
        anchor_id = anchor[0]

        # 拿 anchor_id 前后各 around 条
        cur.execute(
            '''(SELECT id, client_msg_id, role, text, subtitle, emotion,
                       kind, extra, has_audio, created_at
                FROM chat_log
                WHERE user_id=%s AND chat_id=%s AND id < %s
                ORDER BY id DESC LIMIT %s)
               UNION ALL
               (SELECT id, client_msg_id, role, text, subtitle, emotion,
                       kind, extra, has_audio, created_at
                FROM chat_log
                WHERE user_id=%s AND chat_id=%s AND id >= %s
                ORDER BY id ASC LIMIT %s)
               ORDER BY id ASC''',
            (user_id, chat_id, anchor_id, around,
             user_id, chat_id, anchor_id, around)
        )
        rows = cur.fetchall()

        # 判断前后还有没有更多
        has_before = False
        has_after = False
        if rows:
            first_id = rows[0][0]
            last_id = rows[-1][0]
            cur.execute('SELECT EXISTS(SELECT 1 FROM chat_log WHERE user_id=%s AND chat_id=%s AND id < %s)',
                        (user_id, chat_id, first_id))
            has_before = cur.fetchone()[0]
            cur.execute('SELECT EXISTS(SELECT 1 FROM chat_log WHERE user_id=%s AND chat_id=%s AND id > %s)',
                        (user_id, chat_id, last_id))
            has_after = cur.fetchone()[0]
    finally:
        cur.close()
        conn.close()

    msgs = [{
        'id': r[0],
        'client_msg_id': r[1] or '',
        'role': r[2],
        'text': r[3] or '',
        'subtitle': r[4] or '',
        'emotion': r[5] or '',
        'kind': r[6] or 'text',
        'extra': r[7] or '',
        'has_audio': bool(r[8]),
        'ts': r[9].isoformat() if r[9] else None,
    } for r in rows]

    return JSONResponse({
        'messages': msgs,
        'anchor_id': anchor_id,
        'has_more_before': has_before,
        'has_more_after': has_after,
    })
