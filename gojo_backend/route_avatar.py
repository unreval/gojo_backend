"""头像路由（独立文件，不碰 route_character.py）

在 gojo_server.py 里挂载：
    from route_avatar import router as avatar_router
    app.include_router(avatar_router)

头像用 data URI（data:image/jpeg;base64,xxx）直接存进 characters.avatar_url /
groups.avatar_url 的 TEXT 列——个人 app 三五个角色，不需要对象存储。
前端选图时记得压缩（quality 0.4~0.5 + 1:1 裁剪），单张控制在几百 KB 以内。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from db import get_conn

router = APIRouter()

MAX_AVATAR_LEN = 2_000_000  # data URI 字符数上限（约 1.5MB 图片），防止误传原图撑爆请求


@router.put('/characters/{character_id}/avatar')
async def set_character_avatar(character_id: str, data: dict):
    avatar_url = (data.get('avatar_url') or '').strip()
    if not avatar_url:
        return JSONResponse({'error': 'avatar_url 必填'}, status_code=400)
    if len(avatar_url) > MAX_AVATAR_LEN:
        return JSONResponse({'error': '图片太大，请压缩后再传'}, status_code=413)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE characters SET avatar_url = %s WHERE id = %s RETURNING id',
                (avatar_url, character_id))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not row:
        return JSONResponse({'error': 'character not found'}, status_code=404)
    print(f'[avatar] 已更新角色头像：{character_id}（{len(avatar_url)} 字符）')
    return JSONResponse({'ok': True, 'id': character_id})


@router.delete('/characters/{character_id}/avatar')
async def clear_character_avatar(character_id: str):
    """恢复默认（文字首字）头像。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE characters SET avatar_url = NULL WHERE id = %s', (character_id,))
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True, 'id': character_id})


@router.put('/group/{gid}/avatar')
async def set_group_avatar(gid: int, data: dict):
    """群头像，同样机制（前端以后想做群头像时直接可用）。"""
    avatar_url = (data.get('avatar_url') or '').strip()
    if not avatar_url:
        return JSONResponse({'error': 'avatar_url 必填'}, status_code=400)
    if len(avatar_url) > MAX_AVATAR_LEN:
        return JSONResponse({'error': '图片太大，请压缩后再传'}, status_code=413)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('UPDATE groups SET avatar_url = %s WHERE id = %s RETURNING id',
                (avatar_url, gid))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    if not row:
        return JSONResponse({'error': 'group not found'}, status_code=404)
    return JSONResponse({'ok': True, 'id': gid})
