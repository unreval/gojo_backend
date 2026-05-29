"""用户记忆相关路由"""
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, DEFAULT_CHARACTER_ID
from db import get_conn
from user_memory import (
    get_short_memory, get_long_memory, get_chat_days,
    extract_and_save_memory
)

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


@router.get('/memories')
async def get_memories(user_id: str = 'default', character_id: str = DEFAULT_CHARACTER_ID):
    short = get_short_memory(user_id, 20, character_id)
    long_mems = get_long_memory(user_id, character_id)
    return JSONResponse({
        'short_memory': [{'role': r, 'content': c} for r, c in short],
        'long_memory': [{'content': c, 'date': ts.strftime('%Y-%m-%d') if ts else None} for c, ts in long_mems]
    })


@router.get('/stats')
async def get_stats(user_id: str = 'default'):
    return JSONResponse({'total_days': get_chat_days(user_id)})


@router.get('/long_memory')
async def list_long_memory(user_id: str = 'default', character_id: str = DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content, category, timestamp FROM long_memory
           WHERE user_id = %s AND character_id = %s ORDER BY timestamp DESC''',
        (user_id, character_id)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    memories = [{
        'id': r[0], 'content': r[1],
        'category': r[2] or '其他',
        'timestamp': str(r[3]) if r[3] else None,
    } for r in rows]
    return JSONResponse({'memories': memories})


@router.put('/long_memory/{memory_id}')
async def update_long_memory(memory_id: int, data: dict):
    content = (data.get('content') or '').strip()
    category = data.get('category')
    if not content:
        return JSONResponse({'error': '内容不能为空'}, status_code=400)
    conn = get_conn()
    cur = conn.cursor()
    if category:
        cur.execute('UPDATE long_memory SET content = %s, category = %s WHERE id = %s',
                    (content, category, memory_id))
    else:
        cur.execute('UPDATE long_memory SET content = %s WHERE id = %s',
                    (content, memory_id))
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True, 'id': memory_id})


@router.delete('/long_memory/{memory_id}')
async def delete_long_memory(memory_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM long_memory WHERE id = %s', (memory_id,))
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True, 'id': memory_id})


@router.post('/extract_memory_batch')
async def extract_memory_batch(data: dict):
    user_id = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    short = get_short_memory(user_id, 100, character_id)

    pairs = []
    i = 0
    while i < len(short) - 1:
        if short[i][0] == 'user' and short[i+1][0] == 'assistant':
            pairs.append((short[i][1], short[i+1][1]))
            i += 2
        else:
            i += 1

    if not pairs:
        return JSONResponse({'ok': False, 'message': '没有对话可以处理', 'processed': 0})

    before = len(get_long_memory(user_id, character_id))
    for user_text, jp_reply in pairs:
        try:
            extract_and_save_memory(user_id, user_text, jp_reply, character_id)
        except Exception as e:
            print(f'批量提取出错：{e}')
    after = len(get_long_memory(user_id, character_id))

    return JSONResponse({
        'ok': True,
        'message': f'处理了 {len(pairs)} 轮对话，新增 {after - before} 条记忆',
        'processed': len(pairs),
        'new_memories': after - before,
        'total_memories': after,
    })


@router.post('/reclassify_memories')
async def reclassify_memories(data: dict):
    user_id = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content FROM long_memory
           WHERE user_id = %s AND character_id = %s
             AND (category IS NULL OR category = '其他' OR category = '')''',
        (user_id, character_id)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return JSONResponse({'ok': True, 'message': '没有需要重新分类的记忆', 'processed': 0})

    valid_cats = ('喜好','厌恶','身份','状态','经历','关系','其他')
    updated = 0

    for mem_id, content in rows:
        try:
            response = claude_client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=30,
                messages=[{
                    'role': 'user',
                    'content': f'''把下面这条事实归类到一个脑区（只输出分类名）。

事实：{content}

可选分类：
- 喜好：喜欢的食物/颜色/动物/音乐/动漫/人
- 厌恶：不喜欢的东西
- 身份：名字/年龄/生日/职业/学校/专业
- 状态：在做什么/最近忙什么/计划做什么
- 经历：去过哪里/做过什么
- 关系：家人/朋友/宠物
- 其他：都不符合

只输出分类名，例如：喜好'''
                }]
            )
            cat = response.content[0].text.strip()
            if cat not in valid_cats:
                cat = '其他'
            conn = get_conn()
            cur = conn.cursor()
            cur.execute('UPDATE long_memory SET category = %s WHERE id = %s', (cat, mem_id))
            conn.commit()
            cur.close()
            conn.close()
            updated += 1
            print(f'[{user_id}] 重分类 #{mem_id} → [{cat}]：{content[:30]}')
        except Exception as e:
            print(f'重分类失败 #{mem_id}：{e}')

    return JSONResponse({
        'ok': True,
        'message': f'已重新分类 {updated} 条记忆',
        'processed': updated, 'total': len(rows),
    })


@router.get('/debug/users')
async def debug_users():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT user_id, character_id, COUNT(*) FROM long_memory GROUP BY user_id, character_id')
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return JSONResponse({
        'users': [{'user_id': r[0], 'character_id': r[1], 'count': r[2]} for r in rows]
    })
