"""Routes for visible Cognitive Loop notes and reflective diary entries."""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import DEFAULT_CHARACTER_ID
from cognitive_reader import (
    complete_sticky_note,
    list_diary_entries,
    list_sticky_notes,
)


router = APIRouter()


@router.get('/cognitive/sticky_notes')
async def get_sticky_notes(user_id: str = 'default',
                           character_id: str = DEFAULT_CHARACTER_ID,
                           include_inactive: bool = False,
                           limit: int = 50):
    notes = list_sticky_notes(
        user_id,
        character_id,
        include_inactive=include_inactive,
        limit=limit,
    )
    return JSONResponse({'sticky_notes': notes})


@router.post('/cognitive/sticky_notes/{note_id}/complete')
async def mark_sticky_note_complete(note_id: int, data: dict):
    user_id = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    ok = complete_sticky_note(user_id, character_id, note_id)
    return JSONResponse({'ok': ok})


@router.get('/cognitive/diary_entries')
async def get_cognitive_diary_entries(user_id: str = 'default',
                                      character_id: str = DEFAULT_CHARACTER_ID,
                                      limit: int = 30):
    entries = list_diary_entries(user_id, character_id, limit=limit)
    return JSONResponse({'diary_entries': entries})
