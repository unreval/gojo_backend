"""Explicit single-bubble subtitle repair; no generation or side effects."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db_translation import repair_subtitle, restore_subtitle


router = APIRouter()
_HTTP_STATUS = {
    'invalid_translation': 400,
    'identity_mismatch': 409,
    'translation_conflict': 409,
    'deleted_rejected': 409,
    'message_not_synced': 409,
    'invalid_existing_metadata': 409,
    'receipt_unavailable': 404,
    'delivery_unavailable': 404,
}


@router.post('/chat/translation/repair')
async def repair_translation(data: dict):
    """Manual subtitle entry; an existing receipt subtitle takes priority."""
    try:
        result = repair_subtitle(
            data.get('user_id'), data.get('character_id'),
            data.get('source_event_id'), data.get('endpoint'),
            data.get('event_id'), data.get('jp'), data.get('zh'))
    except Exception as exc:
        print(f'[translation] repair failed: {type(exc).__name__}')
        return JSONResponse({'status': 'repair_failed'}, status_code=500)
    return JSONResponse(result, status_code=_HTTP_STATUS.get(result['status'], 200))


@router.post('/chat/translation/restore')
async def restore_translation(data: dict):
    """Read only the exact event's successful subtitle; never generate one."""
    try:
        result = restore_subtitle(
            data.get('user_id'), data.get('character_id'),
            data.get('source_event_id'), data.get('endpoint'),
            data.get('event_id'), data.get('jp'))
    except Exception as exc:
        print(f'[translation] restore failed: {type(exc).__name__}')
        return JSONResponse({'status': 'repair_failed'}, status_code=500)
    return JSONResponse(result, status_code=_HTTP_STATUS.get(result['status'], 200))
