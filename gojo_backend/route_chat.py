"""聊天路由：/chat/text /chat/proactive /chat/voice_text /chat/voice/proactive /transcribe"""
import threading
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, TTS_PROVIDER, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json, sanitize_jp, merge_only_extreme_short
from tts import tts_to_b64, transcribe_audio_b64
from prompt import build_system_prompt
from user_memory import (
    save_short_memory, get_short_memory,
    update_chat_days, extract_and_save_memory
)
from characters import get_character

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ─────────────────── 普通文本聊天 ───────────────────

@router.post('/chat/text')
async def chat_text(data: dict):
    user_text    = data.get('text', '')
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not user_text:
        return JSONResponse({'error': 'no input'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    total_days = update_chat_days(user_id)
    short_memories = get_short_memory(user_id, 6, character_id)

    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': user_text})

    recall_query = user_text
    if short_memories:
        recall_query = user_text + ' ' + ' '.join(c for _, c in short_memories[-2:])

    system_prompt = build_system_prompt(user_id, character_id, recall_query)

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=800,
                system=system_prompt,
                messages=messages
            )
            raw = response.content[0].text.strip()
            print(f'[{user_id}][{character_id}] attempt {attempt+1}: {raw[:120]}...')
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(m.get('jp','').strip() and m.get('zh','').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'attempt {attempt+1} error: {e}')

    if not result:
        result = {'emotion': '调皮', 'messages': [{'jp': 'まあ、僕最強だから気にしないで。', 'zh': '嗯，反正我最强，别在意。'}]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp',''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'user', user_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    threading.Thread(target=extract_and_save_memory,
                     args=(user_id, user_text, full_jp, character_id),
                     daemon=True).start()

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[TTS:{TTS_PROVIDER}] {character_id} emotion={emotion} segs={len(msgs)} days={total_days}')

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
            print(f'[{user_id}] 提醒已保存 task_id={task_id}')
        except Exception as e:
            print(f'提醒保存失败：{e}')

    resp = {'emotion': emotion, 'messages': msgs, 'total_days': total_days}
    if reminder_data:
        resp['reminder'] = reminder_data
    return JSONResponse(resp)


# ─────────────────── 主动消息（日程提醒 / 超时追问） ───────────────────

@router.post('/chat/proactive')
async def chat_proactive(data: dict):
    user_id      = data.get('user_id', 'default')
    task_title   = data.get('task_title', '')
    mode         = data.get('mode', 'remind')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not task_title:
        return JSONResponse({'error': 'no task'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    if mode == 'remind':
        trigger = f'【系统触发：到提醒时间了】现在该主动提醒对方去做这件事："{task_title}"。语气慵懒又带点关心，1条气泡。'
    else:
        trigger = f'【系统触发：超时未完成】对方之前要做"{task_title}"，已经过了时间没动静。主动问她做完了没，带点调侃或假装不在意的关心，1条气泡。'

    short_memories = get_short_memory(user_id, 4, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': trigger})

    system_prompt = build_system_prompt(user_id, character_id, task_title)

    result = None
    for attempt in range(3):
        try:
            response = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=400,
                system=system_prompt,
                messages=messages
            )
            raw = response.content[0].text.strip()
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                result = parsed
                break
        except Exception as e:
            print(f'[proactive] attempt {attempt+1} error: {e}')

    if not result:
        if mode == 'remind':
            result = {'emotion': '调皮', 'messages': [{'jp': f'おい、{task_title}の時間だよ。', 'zh': f'喂，该{task_title}了哦。'}]}
        else:
            result = {'emotion': '疑惑', 'messages': [{'jp': f'{task_title}、ちゃんとやった？', 'zh': f'{task_title}，好好做了吗？'}]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp',''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'assistant', full_jp, character_id)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[proactive] {character_id} mode={mode} task={task_title}')
    return JSONResponse({'emotion': emotion, 'messages': msgs})


# ─────────────────── 语音通话专用（Haiku 极速版） ───────────────────

@router.post('/chat/voice_text')
async def chat_voice_text(data: dict):
    """语音通话快速回复（Haiku，比 Sonnet 快 2-3 倍）"""
    user_text    = data.get('text', '')
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not user_text:
        return JSONResponse({'error': 'no input'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    short_memories = get_short_memory(user_id, 4, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': user_text})

    # 使用完整 prompt（带背景检索）+ 通话场景额外约束
    system_prompt = build_system_prompt(user_id, character_id, user_text) + '''

【★ 语音通话场景——必须遵守】
现在在和对方打电话。回复要简短自然，只输出1条气泡，15-35字。
不要长篇大论，像真打电话一样简洁。'''

    result = None
    for attempt in range(3):
        try:
            response = claude_client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=300,
                system=system_prompt,
                messages=messages
            )
            raw = response.content[0].text.strip()
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                result = parsed
                break
        except Exception as e:
            print(f'[voice_text] attempt {attempt+1} error: {e}')

    if not result:
        result = {'emotion': '调皮', 'messages': [{'jp': 'ふっ、何か言った？', 'zh': '哼，你说了什么？'}]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp',''))
    # 语音场景只用第一条气泡，避免话太多
    msgs = msgs[:1]

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'user', user_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    threading.Thread(target=extract_and_save_memory,
                     args=(user_id, user_text, full_jp, character_id),
                     daemon=True).start()

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[voice_text] {character_id} emotion={emotion}')
    return JSONResponse({'emotion': emotion, 'messages': msgs})


# ─────────────────── 语音通话沉默主动消息 ───────────────────

@router.post('/chat/voice/proactive')
async def chat_voice_proactive(data: dict):
    """语音通话中，对方长时间不说话时悟主动开口"""
    user_id         = data.get('user_id', 'default')
    character_id    = data.get('character_id', DEFAULT_CHARACTER_ID)
    mode            = data.get('mode', 'idle')         # idle / missed
    silence_seconds = int(data.get('silence_seconds', 15))

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    # 根据沉默时长选语气
    if mode == 'missed' or silence_seconds > 60:
        trigger = '【系统：对方已经很久没说话了，可能在发呆或者走神了。你主动问她在干嘛，语气慵懒带点调侃，一两句就好。】'
    elif silence_seconds > 30:
        trigger = '【系统：对方沉默了一会儿了。你稍微催一下，带点撒娇或不耐烦，一两句就好。】'
    else:
        trigger = '【系统：对方刚沉默了几秒。你轻声问一句"在干嘛？"或者类似的，自然一点，一两句就好。】'

    short_memories = get_short_memory(user_id, 4, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': trigger})

    system_prompt = build_system_prompt(user_id, character_id, '') + '''

【★ 语音通话沉默场景】
现在你和对方在打电话，对方没说话。你主动开口打破沉默。
只输出1条气泡，15字以内，自然简短，像真打电话一样。'''

    result = None
    for attempt in range(3):
        try:
            response = claude_client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=250,
                system=system_prompt,
                messages=messages
            )
            raw = response.content[0].text.strip()
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                result = parsed
                break
        except Exception as e:
            print(f'[voice_proactive] attempt {attempt+1} error: {e}')

    if not result:
        if mode == 'missed':
            result = {'emotion': '疑惑', 'messages': [{'jp': 'おい、聞こえてる？', 'zh': '喂，能听到吗？'}]}
        elif silence_seconds > 30:
            result = {'emotion': '调皮', 'messages': [{'jp': 'ねえ、寝ちゃった？', 'zh': '喂，睡着了吗？'}]}
        else:
            result = {'emotion': '平静', 'messages': [{'jp': 'どうした？', 'zh': '怎么了？'}]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp',''))
    msgs = msgs[:1]

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'assistant', full_jp, character_id)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[voice_proactive] {character_id} mode={mode} silence={silence_seconds}s')
    return JSONResponse({'emotion': emotion, 'messages': msgs})


# ─────────────────── Whisper 转录 ───────────────────

@router.post('/transcribe')
async def transcribe(data: dict):
    audio_b64 = data.get('audio_base64', '')
    if not audio_b64:
        return JSONResponse({'error': 'no audio'}, status_code=400)
    result = transcribe_audio_b64(audio_b64)
    return JSONResponse(result)