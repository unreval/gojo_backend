"""日程任务路由"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from tasks import list_tasks, create_task, update_task, delete_task

router = APIRouter()


@router.get('/tasks')
async def get_tasks(user_id: str = 'default'):
    return JSONResponse({'tasks': list_tasks(user_id)})


@router.post('/tasks')
async def post_task(data: dict):
    user_id = data.get('user_id', 'default')
    title = (data.get('title') or '').strip()
    if not title:
        return JSONResponse({'error': 'no title'}, status_code=400)
    new_id = create_task(
        user_id=user_id,
        title=title,
        category=data.get('category', '个人'),
        due_date=data.get('due_date'),
        due_time=data.get('due_time'),
        reminder_minutes=data.get('reminder_minutes'),
        repeat_type=data.get('repeat_type', 'none'),
    )
    return JSONResponse({'ok': True, 'id': new_id})


@router.put('/tasks/{task_id}')
async def put_task(task_id: int, data: dict):
    if not update_task(task_id, data):
        return JSONResponse({'error': 'nothing to update'}, status_code=400)
    return JSONResponse({'ok': True})


@router.delete('/tasks/{task_id}')
async def del_task(task_id: int):
    delete_task(task_id)
    return JSONResponse({'ok': True})
