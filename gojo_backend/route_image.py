"""图片聊天路由：/chat/image"""
import threading
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, TTS_PROVIDER, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json, sanitize_jp, merge_only_extreme_short
from tts import tts_to_b64
from prompt import build_system_prompt
from user_memory import (
    save_short_memory, get_short_memory,
    update_chat_days, extract_and_save_memory
)
from characters import get_character
from tasks import (
    find_duplicate_task,
    find_and_delete_tasks_by_keyword,
    delete_latest_task,
)

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


@router.post('/chat/image')
async def chat_image(data: dict):
    """
    接收图片（base64）+ 可选文字（caption），让 Claude Vision 识别后回复。
    请求体：
    {
      "user_id": "xxx",
      "character_id": "gojo",
      "image_base64": "xxxx...",      // 必填
      "media_type": "image/jpeg",     // 可选
      "text": "看这个！"              // 可选 caption
    }
    """
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    image_b64    = data.get('image_base64', '')
    media_type   = data.get('media_type', 'image/jpeg')
    user_text    = (data.get('text') or '').strip()

    if not image_b64:
        return JSONResponse({'error': 'no image'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    total_days = update_chat_days(user_id)
    short_memories = get_short_memory(user_id, 6, character_id)

    # ── 构造 messages（multimodal）──
    messages = [{'role': r, 'content': c} for r, c in short_memories]

    user_content = [
        {
            'type': 'image',
            'source': {
                'type': 'base64',
                'media_type': media_type,
                'data': image_b64,
            }
        }
    ]
    # 如果有 caption，作为文字一起发；否则用引导语
    if user_text:
        user_content.append({'type': 'text', 'text': user_text})
        display_text = f'📷 {user_text}'
    else:
        user_content.append({
            'type': 'text',
            'text': '【对方发来了一张图片，没有附文字。你看到了这张图，自然反应——根据图里的内容回应，像真朋友收到对方发来的照片一样：可以好奇、调侃、关心、表达喜好。不要冷冰冰地"描述图片内容"，要像看到了实物一样有情绪。】'
        })
        display_text = '📷 [图片]'

    messages.append({'role': 'user', 'content': user_content})

    # 用 caption 做背景记忆检索（聊到甜食的照片→召回喜久福那条）
    recall_query = user_text if user_text else ''
    system_prompt = build_system_prompt(user_id, character_id, recall_query)

    # ── 调用 Claude Vision ──
    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=800,
                system=system_prompt,
                messages=messages,
            )
            raw = response.content[0].text.strip()
            print(f'[{user_id}][{character_id}] image attempt {attempt+1}: {raw[:120]}...')
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'image attempt {attempt+1} error: {e}')

    if not result:
        result = {
            'emotion': '疑惑',
            'messages': [{'jp': 'おっ、写真か。何これ？', 'zh': '哦，照片啊。这是什么？'}]
        }

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)

    save_short_memory(user_id, 'user', display_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)

    # 如果用户附了文字，尝试提取用户事实
    if user_text:
        threading.Thread(target=extract_and_save_memory,
                         args=(user_id, user_text, full_jp, character_id),
                         daemon=True).start()

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[TTS:{TTS_PROVIDER}] {character_id} image emotion={emotion} segs={len(msgs)}')

    # ─── 处理取消提醒 ───
    cancelled_tasks = []
    if result.get('cancel_reminder'):
        cancel = result['cancel_reminder']
        keyword = (cancel.get('keyword') or '').strip()
        latest = cancel.get('latest', False)
        try:
            if keyword:
                deleted = find_and_delete_tasks_by_keyword(user_id, keyword, latest_only=True)
            elif latest:
                deleted = delete_latest_task(user_id)
            else:
                deleted = []
            for task_id, notif_id in deleted:
                cancelled_tasks.append({'task_id': task_id, 'notification_id': notif_id})
                print(f'[{user_id}] 🗑️ 已取消任务 id={task_id}（来自图片对话）')
        except Exception as e:
            print(f'取消提醒失败：{e}')

    # ─── 处理新增提醒 ───
    reminder_data = None
    if result.get('reminder'):
        rem = result['reminder']
        reminder_data = {
            'date': rem.get('date'),
            'time': rem.get('time'),
            'content': rem.get('content', ''),
            'notification': rem.get('notification', ''),
        }
        try:
            existing = find_duplicate_task(
                user_id,
                reminder_data['content'],
                reminder_data['date'],
                reminder_data['time'],
            )
            if existing:
                task_id, _ = existing
                reminder_data['task_id'] = task_id
                reminder_data['duplicate'] = True
            else:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    '''INSERT INTO tasks (user_id, title, category, due_date, due_time, reminder_minutes)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id''',
                    (user_id, reminder_data['content'], '个人',
                     reminder_data['date'], reminder_data['time'], 0)
                )
                task_id = cur.fetchone()[0]
                conn.commit()
                cur.close()
                conn.close()
                reminder_data['task_id'] = task_id
                reminder_data['duplicate'] = False
                print(f'[{user_id}] ✅ 提醒已保存（来自图片对话）task_id={task_id}')
        except Exception as e:
            print(f'提醒保存失败：{e}')

    resp = {'emotion': emotion, 'messages': msgs, 'total_days': total_days}
    if reminder_data:
        resp['reminder'] = reminder_data
    if cancelled_tasks:
        resp['cancelled_tasks'] = cancelled_tasks
    return JSONResponse(resp)
