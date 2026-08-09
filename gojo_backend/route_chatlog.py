"""route_chatlog.py —— 单聊记录同步接口

  GET    /chatlog?user_id=&chat_id=&limit=&before_id=   拉历史(旧→新)
  POST   /chatlog/append                                追加消息(幂等)
  DELETE /chatlog?user_id=&chat_id=                     清空
  GET    /chatlog/count?user_id=&chat_id=               条数

前端用法:
  · 进聊天页 → GET 拉最近 200 条铺上去(比本地缓存权威)
  · 每次发/收消息 → POST 追加(带 client_msg_id,重发不会重复)
  · 往上翻 → 带 before_id 再 GET
  · 点「清空」→ DELETE
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

import db_chatlog

router = APIRouter()


@router.get('/chatlog')
async def get_chatlog(user_id: str, chat_id: str,
                      limit: int = 200, before_id: int = None):
    limit = max(1, min(500, limit))
    msgs, has_more = db_chatlog.get_messages(
        user_id, chat_id, limit=limit, before_id=before_id)
    return JSONResponse({
        'messages': msgs,
        'has_more': has_more,
        'count': len(msgs),
    })


@router.post('/chatlog/append')
async def append_chatlog(data: dict):
    """body: {user_id, chat_id, messages: [{client_msg_id, role, text, ...}]}"""
    user_id = (data.get('user_id') or '').strip()
    chat_id = (data.get('chat_id') or '').strip()
    msgs = data.get('messages')
    if not user_id or not chat_id or not isinstance(msgs, list):
        return JSONResponse(
            {'error': '需要 user_id / chat_id / messages(数组)'}, status_code=400)
    try:
        written = db_chatlog.append_messages(user_id, chat_id, msgs[:100])
    except Exception as e:
        print(f'[chatlog] 写入失败:{e}')
        return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)
    return JSONResponse({'ok': True, 'written': written})


@router.delete('/chatlog')
async def clear_chatlog(user_id: str, chat_id: str):
    n = db_chatlog.clear_chat(user_id, chat_id)
    return JSONResponse({'ok': True, 'deleted': n})


@router.post('/chatlog/rescue_from_short_memory')
async def rescue_from_short_memory(data: dict):
    """★ 救援:把 short_memory 里的对话导进 chat_log。

    用途:前端本地缓存坏掉、聊天记录断层时,short_memory 里还留着
    最近 24 小时的对话文本,用这个补回聊天页。

    局限:short_memory 只有纯文本(角色的日语原文),没有中文翻译、
    没有情绪标记、没有音频。补回来的记录会比正常的简陋一些,
    但至少内容还在。

    body: {user_id, character_id, dry_run?}
      dry_run=true 时只统计不写入,先看看会导多少条。
    """
    from db import get_conn
    user_id = (data.get('user_id') or '').strip()
    character_id = (data.get('character_id') or 'gojo').strip()
    dry_run = bool(data.get('dry_run'))
    if not user_id:
        return JSONResponse({'error': '需要 user_id'}, status_code=400)

    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT role, content, timestamp FROM short_memory
               WHERE user_id=%s AND character_id=%s
               ORDER BY timestamp ASC''',
            (user_id, character_id))
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    if not rows:
        return JSONResponse({'ok': True, 'found': 0,
                             'note': 'short_memory 里没有记录'})

    msgs = []
    for role, content, ts in rows:
        if not content:
            continue
        # 用时间戳做 client_msg_id,重复跑这个接口不会写重复
        key = f'rescue_{int(ts.timestamp() * 1000)}' if ts else f'rescue_{len(msgs)}'
        msgs.append({
            'client_msg_id': key,
            'role': 'user' if role == 'user' else 'gojo',
            'text': content,
            'subtitle': '',
            'kind': 'text',
            'ts': ts.isoformat() if ts else None,
        })

    if dry_run:
        return JSONResponse({
            'ok': True, 'dry_run': True, 'found': len(msgs),
            'earliest': msgs[0]['ts'] if msgs else None,
            'latest': msgs[-1]['ts'] if msgs else None,
            'preview': [m['text'][:40] for m in msgs[:3]],
        })

    written = db_chatlog.append_messages(user_id, character_id, msgs)
    return JSONResponse({
        'ok': True, 'found': len(msgs), 'written': written,
        'note': f'导入 {written} 条(重复的自动跳过)',
    })


@router.get('/chatlog/count')
async def chatlog_count(user_id: str, chat_id: str):
    return JSONResponse({'count': db_chatlog.count_messages(user_id, chat_id)})