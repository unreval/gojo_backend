"""角色 & 角色背景记忆路由（含旧 /gojo_memory 兼容别名）"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from config import DEFAULT_CHARACTER_ID
from characters import (
    list_characters, get_character,
    list_character_memory, add_character_memory,
    update_character_memory, delete_character_memory,
    retrieve_character_memory,
)

router = APIRouter()


# ────────── 角色列表 / 详情 ──────────

@router.get('/characters')
async def get_characters():
    return JSONResponse({'characters': list_characters()})


@router.get('/characters/{character_id}')
async def get_character_detail(character_id: str):
    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': 'not found'}, status_code=404)
    return JSONResponse(char)


# ────────── 角色背景记忆 CRUD ──────────

@router.get('/character_memory')
async def list_cm(character_id: str = DEFAULT_CHARACTER_ID):
    items = list_character_memory(character_id)
    return JSONResponse({'memories': items, 'total': len(items)})


@router.post('/character_memory')
async def add_cm(data: dict):
    content = (data.get('content') or '').strip()
    if not content:
        return JSONResponse({'error': '内容不能为空'}, status_code=400)
    new_id = add_character_memory(
        character_id=data.get('character_id', DEFAULT_CHARACTER_ID),
        content=content,
        category=data.get('category', '其他'),
        keywords=data.get('keywords', ''),
        importance=float(data.get('importance', 0.5)),
    )
    return JSONResponse({'ok': True, 'id': new_id})


@router.put('/character_memory/{mem_id}')
async def edit_cm(mem_id: int, data: dict):
    if not update_character_memory(mem_id, data):
        return JSONResponse({'error': 'nothing to update'}, status_code=400)
    return JSONResponse({'ok': True})


@router.delete('/character_memory/{mem_id}')
async def del_cm(mem_id: int):
    delete_character_memory(mem_id)
    return JSONResponse({'ok': True})


@router.post('/character_memory/test_recall')
async def test_recall_cm(data: dict):
    query = data.get('query', '')
    limit = int(data.get('limit', 4))
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    results = retrieve_character_memory(character_id, query, limit)
    return JSONResponse({
        'query': query, 'character_id': character_id,
        'matched': results, 'count': len(results),
    })


# ────────── 旧 /gojo_memory 兼容别名（指向 gojo 的 character_memory）──────────

@router.get('/gojo_memory')
async def list_gojo_alias():
    items = list_character_memory('gojo')
    return JSONResponse({'memories': items, 'total': len(items)})


@router.post('/gojo_memory')
async def add_gojo_alias(data: dict):
    content = (data.get('content') or '').strip()
    if not content:
        return JSONResponse({'error': '内容不能为空'}, status_code=400)
    new_id = add_character_memory(
        character_id='gojo',
        content=content,
        category=data.get('category', '其他'),
        keywords=data.get('keywords', ''),
        importance=float(data.get('importance', 0.5)),
    )
    return JSONResponse({'ok': True, 'id': new_id})


@router.put('/gojo_memory/{mem_id}')
async def edit_gojo_alias(mem_id: int, data: dict):
    if not update_character_memory(mem_id, data):
        return JSONResponse({'error': 'nothing to update'}, status_code=400)
    return JSONResponse({'ok': True})


@router.delete('/gojo_memory/{mem_id}')
async def del_gojo_alias(mem_id: int):
    delete_character_memory(mem_id)
    return JSONResponse({'ok': True})


@router.post('/gojo_memory/test_recall')
async def test_recall_gojo_alias(data: dict):
    query = data.get('query', '')
    limit = int(data.get('limit', 4))
    results = retrieve_character_memory('gojo', query, limit)
    return JSONResponse({'query': query, 'matched': results, 'count': len(results)})
