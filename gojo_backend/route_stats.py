"""通用查询路由:
- /stats?user_id=X          → 首页要展示的"陪伴天数"等统计
- /characters_all           → 列出所有角色(给日记列表页动态铺卡片用)
- /reset_memory (POST)      → 清空用户在指定角色的所有对话记忆(不动角色/账户/日程/日记)

用了 /characters_all 这种带下划线的名字是为了避开 route_character.py 里
已经存在的 GET /characters/{id}——放同名字面路径可能会互相盖住,分开省心。
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from db import get_conn

router = APIRouter()


@router.get('/stats')
async def get_stats(user_id: str = 'default'):
    """返回用户的聊天累计天数、首末日期。
    没有记录就返回 0(不是 404,免得前端把它当错误)。
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT first_chat_date, last_chat_date, total_days FROM user_stats WHERE user_id = %s',
        (user_id,)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return JSONResponse({
            'user_id': user_id,
            'total_days': 0,
            'first_chat_date': None,
            'last_chat_date': None,
        })
    return JSONResponse({
        'user_id': user_id,
        'total_days': int(row[2] or 0),
        'first_chat_date': row[0],
        'last_chat_date': row[1],
    })


@router.get('/characters_all')
async def list_all_characters():
    """列出 characters 表里所有角色的基本信息,给日记列表这类"多角色"页面用。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, name, avatar_url FROM characters ORDER BY created_at ASC, id ASC'
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return JSONResponse({
        'characters': [{'id': r[0], 'name': r[1], 'avatar_url': r[2]} for r in rows],
    })


@router.get('/rag/status')
async def rag_status():
    """看 RAG 现在什么状态:开没开、补了多少向量、缓存多少条。"""
    import memory_search
    return JSONResponse(memory_search.rag_status())


@router.post('/rag/backfill')
async def rag_backfill(data: dict = None):
    """给历史记忆补 embedding。老记忆没向量就搜不到,启用 RAG 后必须跑一次。

    body(可选): {"limit": 500}  —— 一次补多少条,默认 500
    条数多的话多调用几次,直到 remaining 变 0。
    """
    import memory_search
    limit = 500
    if isinstance(data, dict) and data.get('limit'):
        try:
            limit = max(1, min(2000, int(data['limit'])))
        except (ValueError, TypeError):
            pass
    return JSONResponse(memory_search.backfill_embeddings(limit=limit))


@router.post('/rag/invalidate')
async def rag_invalidate():
    """强制重载向量缓存(手动改过数据库时用)。"""
    import memory_search
    memory_search.invalidate_cache()
    return JSONResponse({'ok': True, 'note': '缓存已清,下次检索时重载'})


@router.post('/memory/merge')
async def manual_merge_memory(data: dict):
    """★ 手动合并羁绊记忆:把几条碎片替换成一条完整的。

    body: {
      "user_id": "...",
      "character_id": "gojo",
      "replaces": ["旧记忆原文1", "旧记忆原文2"],
      "content": "合并后的完整版"
    }

    走和自动合并同一套安全检查(最多3条、必须真实存在、不能信息缩水)。
    """
    from user_memory import merge_bond_memories
    user_id = (data.get('user_id') or '').strip()
    character_id = (data.get('character_id') or 'gojo').strip()
    replaces = data.get('replaces')
    content = (data.get('content') or '').strip()

    if not user_id or not content or not isinstance(replaces, list) or not replaces:
        return JSONResponse(
            {'error': '需要 user_id / replaces(数组) / content'}, status_code=400)

    try:
        ok, deleted = merge_bond_memories(
            user_id, character_id, 'between', replaces, content)
    except Exception as e:
        return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)

    return JSONResponse({
        'ok': ok,
        'deleted': deleted,
        'content': content if ok else None,
        'note': '成功' if ok else '未执行:目标找不到 或 新内容比旧的短(防信息缩水)',
    })


@router.post('/reset_memory')
async def reset_memory(data: dict):
    """★ 清空指定用户的对话记忆(short + long + bond + 主动消息)。
    ★ 不动:角色定义、账户/记账、日程任务、日记本身、生理期、群聊消息。
    ★ 只清"对话上下文"这一块,让 gojo"重新认识"你。

    参数:
    - user_id (必填):要清哪个用户
    - character_id (可选):只清对某个角色的记忆;不传就清所有角色的
    - include_proactive (可选,默认 true):是否也清主动消息队列
    """
    user_id = (data.get('user_id') or '').strip()
    if not user_id or user_id == 'default':
        return JSONResponse({'error': 'user_id required (不能是 default)'}, status_code=400)

    character_id = (data.get('character_id') or '').strip() or None
    include_proactive = data.get('include_proactive', True)

    conn = get_conn()
    cur = conn.cursor()
    deleted = {}

    # 三张核心记忆表都按 user_id 清
    tables = ['short_memory', 'long_memory', 'bond_memory']
    for tbl in tables:
        try:
            if character_id:
                cur.execute(
                    f'DELETE FROM {tbl} WHERE user_id = %s AND character_id = %s',
                    (user_id, character_id)
                )
            else:
                cur.execute(f'DELETE FROM {tbl} WHERE user_id = %s', (user_id,))
            deleted[tbl] = cur.rowcount
        except Exception as e:
            deleted[tbl] = f'error: {e}'

    # 顺手清没读完的主动消息(不然清完记忆后又冒出老队列里的报备)
    if include_proactive:
        try:
            if character_id:
                cur.execute(
                    'DELETE FROM proactive_msg WHERE user_id = %s AND character_id = %s',
                    (user_id, character_id)
                )
            else:
                cur.execute('DELETE FROM proactive_msg WHERE user_id = %s', (user_id,))
            deleted['proactive_msg'] = cur.rowcount
        except Exception as e:
            deleted['proactive_msg'] = f'error: {e}'

    conn.commit()
    cur.close()
    conn.close()

    print(f'[reset_memory] 清空 {user_id}'
          f'{" 关于 " + character_id if character_id else "(全部角色)"}: {deleted}')
    return JSONResponse({
        'ok': True,
        'user_id': user_id,
        'character_id': character_id,
        'deleted': deleted,
    })