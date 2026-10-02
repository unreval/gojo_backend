"""Read-only lookup of exact per-message read receipts."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db_read_receipt import MAX_SOURCE_EVENT_ID_LENGTH, query_read_receipts


router = APIRouter()


@router.post('/chat/read_receipts/query')
async def read_receipts_query(data: dict):
    user_id = data.get('user_id')
    character_id = data.get('character_id')
    raw_ids = data.get('source_event_ids')
    if (not isinstance(user_id, str) or not user_id.strip()
            or not isinstance(character_id, str) or not character_id.strip()
            or not isinstance(raw_ids, list) or len(raw_ids) > 100):
        return JSONResponse({'error': 'invalid read receipt query'}, status_code=400)
    if any(not isinstance(item, str) or not item.strip()
           or len(item.strip()) > MAX_SOURCE_EVENT_ID_LENGTH for item in raw_ids):
        return JSONResponse({'error': 'invalid source_event_id'}, status_code=400)
    ids = list(dict.fromkeys(item.strip() for item in raw_ids))
    receipts = query_read_receipts(user_id.strip(), character_id.strip(), ids)
    return JSONResponse({'receipts': receipts})
