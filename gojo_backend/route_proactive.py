"""主动消息路由 /proactive/*

GET  /proactive/pending?user_id=&character_id=   —— 拉未读的主动消息（前端轮询/进聊天时调）
POST /proactive/read                              —— 标记已读 {msg_ids:[...]}
POST /proactive/report_now                        —— 测试用：立刻让他生成一条任务汇报
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

import proactive_msg
import proactive_scheduler
import push_notify

router = APIRouter()

DEFAULT_USER = 'user_mofpiyd7442ia7'


@router.post('/push/register')
async def register_push(data: dict):
    """前端注册推送 token。"""
    user_id = data.get('user_id', DEFAULT_USER)
    token = (data.get('token') or '').strip()
    if not token:
        return JSONResponse({'ok': False, 'error': 'no token'}, status_code=400)
    push_notify.save_token(user_id, token)
    return JSONResponse({'ok': True})


@router.get('/proactive/pending')
async def pending(user_id: str = DEFAULT_USER, character_id: str = None):
    msgs = proactive_msg.get_pending(user_id, character_id)
    return JSONResponse({'messages': msgs})


@router.post('/proactive/read')
async def mark_read(data: dict):
    ids = data.get('msg_ids') or []
    proactive_msg.mark_read(ids)
    return JSONResponse({'ok': True, 'count': len(ids)})


@router.post('/proactive/report_now')
async def report_now(data: dict):
    """测试用：立刻生成一条任务汇报，不等约定时间。"""
    user_id = data.get('user_id', DEFAULT_USER)
    character_id = data.get('character_id', 'gojo')
    r = proactive_scheduler.generate_task_report(character_id, user_id)
    if not r:
        return JSONResponse({'ok': False, 'error': 'generate failed'})
    mid, jp = r
    return JSONResponse({'ok': True, 'id': mid, 'jp': jp})