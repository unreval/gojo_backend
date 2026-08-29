"""课程表路由 · CRUD + 按周查询 + 放假 + 调休

风格和 route_tasks.py 保持一致：函数薄壳，业务写在 courses.py 里。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from courses import (
    list_courses, get_course, create_course, update_course, delete_course,
    replace_sessions, add_session, delete_session,
    list_exceptions, create_exception, delete_exception,
    list_day_offs, create_day_off, delete_day_off,
    get_week_view,
)

router = APIRouter()


# ══════════════════════════════════════════════════════════════
#  courses
# ══════════════════════════════════════════════════════════════

@router.get('/courses')
async def get_courses(user_id: str = 'default'):
    return JSONResponse({'courses': list_courses(user_id)})


# ⚠️ 顺序很关键：/courses/week 必须放在 /courses/{course_id} 前面
# 否则 FastAPI 会把 "week" 当成 course_id 试图解析成整数，报 int_parsing 错误
@router.get('/courses/week')
async def get_courses_week(user_id: str = 'default', monday: str | None = None):
    """给一个"周一"日期(YYYY-MM-DD)，返回那一周所有具体的课
    （已应用调课 / 请假 / 放假 / 临时加课）。
    monday 不传就自动用今天所在周。"""
    if not monday:
        from datetime import date, timedelta
        today = date.today()
        monday = str(today - timedelta(days=today.weekday()))
    return JSONResponse({
        'monday': monday,
        'instances': get_week_view(user_id, monday),
    })


@router.get('/courses/{course_id}')
async def get_course_detail(course_id: int):
    c = get_course(course_id)
    if not c:
        return JSONResponse({'error': 'not found'}, status_code=404)
    return JSONResponse(c)


@router.post('/courses')
async def post_course(data: dict):
    """新建课程。body 见 courses.create_course 参数。"""
    user_id = data.get('user_id', 'default')
    name = (data.get('name') or '').strip()
    if not name:
        return JSONResponse({'error': 'name required'}, status_code=400)
    new_id = create_course(
        user_id=user_id,
        name=name,
        teacher=(data.get('teacher') or '').strip(),
        location=(data.get('location') or '').strip(),
        color=data.get('color') or '#3b82f6',
        note=(data.get('note') or '').strip(),
        semester_start=data.get('semester_start'),
        semester_end=data.get('semester_end'),
        sessions=data.get('sessions') or [],
    )
    return JSONResponse({'ok': True, 'id': new_id})


@router.put('/courses/{course_id}')
async def put_course(course_id: int, data: dict):
    """更新课程字段。sessions 传了就整批替换。"""
    changed = update_course(course_id, data)
    if 'sessions' in data:
        replace_sessions(course_id, data.get('sessions') or [])
        changed = True
    if not changed:
        return JSONResponse({'error': 'nothing to update'}, status_code=400)
    return JSONResponse({'ok': True})


@router.delete('/courses/{course_id}')
async def del_course(course_id: int):
    delete_course(course_id)
    return JSONResponse({'ok': True})


# ── 单独增删 session ──

@router.post('/course/sessions')
async def post_session(data: dict):
    course_id = data.get('course_id')
    weekday = data.get('weekday')
    st = data.get('start_time')
    et = data.get('end_time')
    if not course_id or not weekday or not st or not et:
        return JSONResponse({'error': 'course_id/weekday/start_time/end_time required'}, status_code=400)
    new_id = add_session(course_id, weekday, st, et, data.get('weeks', ''))
    return JSONResponse({'ok': True, 'id': new_id})


@router.delete('/course/sessions/{session_id}')
async def del_session(session_id: int):
    delete_session(session_id)
    return JSONResponse({'ok': True})


# ══════════════════════════════════════════════════════════════
#  course_exceptions（调课 / 请假 / 临时加课）
# ══════════════════════════════════════════════════════════════

@router.get('/course/exceptions')
async def get_exceptions(user_id: str = 'default',
                          start: str | None = None,
                          end: str | None = None):
    return JSONResponse({'exceptions': list_exceptions(user_id, start, end)})


@router.post('/course/exceptions')
async def post_exception(data: dict):
    """body:
      { course_id, session_id?, exception_date(YYYY-MM-DD),
        exception_type('cancel'|'reschedule'|'extra'),
        new_date?, new_start_time?, new_end_time?, new_location?, note? }
    """
    course_id = data.get('course_id')
    ex_date = data.get('exception_date')
    ex_type = data.get('exception_type')
    if not course_id or not ex_date or ex_type not in ('cancel', 'reschedule', 'extra'):
        return JSONResponse(
            {'error': 'course_id / exception_date / exception_type(cancel|reschedule|extra) required'},
            status_code=400
        )
    if ex_type == 'reschedule':
        if not data.get('new_date') or not data.get('new_start_time') or not data.get('new_end_time'):
            return JSONResponse(
                {'error': 'reschedule requires new_date / new_start_time / new_end_time'},
                status_code=400
            )
    if ex_type == 'extra':
        # 临时加课至少要有 new_start_time / new_end_time；new_date 默认等于 exception_date
        if not data.get('new_start_time') or not data.get('new_end_time'):
            return JSONResponse(
                {'error': 'extra requires new_start_time / new_end_time'},
                status_code=400
            )
    new_id = create_exception(
        course_id=course_id,
        exception_date=ex_date,
        exception_type=ex_type,
        session_id=data.get('session_id'),
        new_date=data.get('new_date') or (ex_date if ex_type == 'extra' else None),
        new_start_time=data.get('new_start_time'),
        new_end_time=data.get('new_end_time'),
        new_location=(data.get('new_location') or '').strip(),
        note=(data.get('note') or '').strip(),
    )
    return JSONResponse({'ok': True, 'id': new_id})


@router.delete('/course/exceptions/{exception_id}')
async def del_exception(exception_id: int):
    delete_exception(exception_id)
    return JSONResponse({'ok': True})


# ══════════════════════════════════════════════════════════════
#  course_day_off（这一天全部放假）
# ══════════════════════════════════════════════════════════════

@router.get('/course/day-off')
async def get_day_offs(user_id: str = 'default',
                        start: str | None = None,
                        end: str | None = None):
    return JSONResponse({'day_offs': list_day_offs(user_id, start, end)})


@router.post('/course/day-off')
async def post_day_off(data: dict):
    """body: { user_id, off_date(YYYY-MM-DD), note? }
    同一 user_id + off_date 幂等（第二次调用会更新 note）。"""
    user_id = data.get('user_id', 'default')
    off_date = data.get('off_date')
    if not off_date:
        return JSONResponse({'error': 'off_date required'}, status_code=400)
    new_id = create_day_off(user_id, off_date, (data.get('note') or '').strip())
    return JSONResponse({'ok': True, 'id': new_id})


@router.delete('/course/day-off/{day_off_id}')
async def del_day_off(day_off_id: int):
    delete_day_off(day_off_id)
    return JSONResponse({'ok': True})