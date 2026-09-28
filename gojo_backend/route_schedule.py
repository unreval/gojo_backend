"""route_schedule.py —— 角色日程接口

  GET  /schedule?character_id=&user_id=&date=   查某天的日程(默认今天)
  GET  /schedule/now?character_id=&user_id=     他现在在干嘛(前端可用来显示状态)
  POST /schedule/generate                       立刻生成(测试用/手动重排)
  DELETE /schedule?character_id=&user_id=&date= 清空某天
"""
from datetime import datetime, timedelta
from fastapi import APIRouter
from fastapi.responses import JSONResponse

try:
    from fastapi.encoders import jsonable_encoder as _jsonable_encoder
except (ImportError, ModuleNotFoundError):
    def _jsonable_encoder(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, dict):
            return {key: _jsonable_encoder(item) for key, item in value.items()}
        if isinstance(value, list):
            return [_jsonable_encoder(item) for item in value]
        return value

from config import CN_TZ, DEFAULT_CHARACTER_ID
import db_schedule
import schedule_engine

router = APIRouter()
DEFAULT_USER = 'user_mofpiyd7442ia7'


def _parse_date(s):
    if not s:
        return datetime.now(CN_TZ).date()
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return datetime.now(CN_TZ).date()


@router.get('/schedule')
async def get_schedule(character_id: str = DEFAULT_CHARACTER_ID,
                       user_id: str = DEFAULT_USER,
                       date: str = None):
    d = _parse_date(date)
    items = db_schedule.get_canonical_schedule(character_id, user_id, d)

    # 今天还没生成就现生成一份(第一次进页面时会稍慢几秒)
    if not items and d == datetime.now(CN_TZ).date():
        schedule_engine.generate_daily_schedule(character_id, user_id, d)
        items = db_schedule.get_canonical_schedule(character_id, user_id, d)

    now = datetime.now(CN_TZ)
    world = (db_schedule.get_current_world_state(character_id, user_id, now)
             if d == now.date() else None)
    return JSONResponse(_jsonable_encoder({
        'date': str(d),
        'items': items,
        'current': (world or {}).get('activity'),
        'current_event': (world or {}).get('event'),
        'current_phase': (world or {}).get('phase'),
        'availability': (world or {}).get('availability'),
        'now': now.strftime('%H:%M'),
    }))


def schedule_now_payload(world, now):
    """Expose the canonical event/phase pair; never recalculate availability."""
    hhmm = now.strftime('%H:%M')
    act = (world or {}).get('activity')
    event = (world or {}).get('event')
    phase = (world or {}).get('phase')
    availability = (world or {}).get('availability') or {'reply_state': 'free'}
    if not act:
        return {
            'busy': False,
            'reply_state': 'free',
            'activity': None,
            'now': hhmm,
        }
    runtime = availability.get('reply_state') or 'free'
    return {
        'busy': runtime != db_schedule.REPLY_FREE,
        'reply_state': runtime,
        'activity': act['title'],
        'location': act.get('location', ''),
        'note': act.get('note', ''),
        'until': act.get('end_time', ''),
        'event_until': act.get('event_end_time', act.get('end_time', '')),
        'event_id': event.get('id') if event else None,
        'revision': event.get('revision') if event else None,
        'status': event.get('status') if event else None,
        'phase_id': phase.get('id') if phase else None,
        'phase_title': phase.get('title') if phase else '',
        'phase_status': phase.get('status') if phase else None,
        'now': hhmm,
    }


@router.get('/schedule/now')
async def schedule_now(character_id: str = DEFAULT_CHARACTER_ID,
                       user_id: str = DEFAULT_USER):
    """他现在在干嘛、能不能回消息。前端可以拿来在聊天页顶部显示状态。"""
    now = datetime.now(CN_TZ)
    world = db_schedule.get_current_world_state(character_id, user_id, now)
    return JSONResponse(_jsonable_encoder(schedule_now_payload(world, now)))


@router.post('/schedule/generate')
async def generate(data: dict):
    """立刻生成日程。force=true 会覆盖已有的。"""
    character_id = (data.get('character_id') or DEFAULT_CHARACTER_ID).strip()
    user_id = (data.get('user_id') or DEFAULT_USER).strip()
    d = _parse_date(data.get('date'))
    force = bool(data.get('force'))

    items = schedule_engine.generate_daily_schedule(
        character_id, user_id, d, force=force)
    if items is None:
        existing = db_schedule.get_canonical_schedule(character_id, user_id, d)
        if existing and not force:
            return JSONResponse({'ok': True, 'skipped': True,
                                 'note': '当天已有日程,加 force=true 才覆盖',
                                 'items': existing})
        return JSONResponse({'ok': False, 'error': '生成失败,看后端日志'},
                            status_code=500)
    return JSONResponse({'ok': True, 'date': str(d), 'items': items})


@router.delete('/schedule')
async def clear(character_id: str = DEFAULT_CHARACTER_ID,
                user_id: str = DEFAULT_USER,
                date: str = None):
    d = _parse_date(date)
    n = db_schedule.clear_canonical_schedule(character_id, user_id, d)
    return JSONResponse({'ok': True, 'deleted': n})
