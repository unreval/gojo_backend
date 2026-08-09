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


@router.get('/chatlog/count')
async def chatlog_count(user_id: str, chat_id: str):
    return JSONResponse({'count': db_chatlog.count_messages(user_id, chat_id)})
