"""聊天路由：/chat/text /chat/story /chat/proactive /chat/voice_text /chat/voice_story /chat/voice/proactive /transcribe"""
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
from tasks import (
    find_duplicate_task,
    find_and_delete_tasks_by_keyword,
    delete_latest_task,
)

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
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
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
        m['jp'] = sanitize_jp(m.get('jp', ''))
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

    # ─── 处理取消提醒（先取消再新增）───
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
                print(f'[{user_id}] 🗑️ 已取消任务 id={task_id} keyword={keyword or "(latest)"}')
        except Exception as e:
            print(f'取消提醒失败：{e}')

    # ─── 处理新增提醒（带去重）───
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
                print(f'[{user_id}] 🔁 提醒已存在 task_id={task_id}，跳过新建')
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
                print(f'[{user_id}] ✅ 提醒已保存 task_id={task_id}')
        except Exception as e:
            print(f'提醒保存失败：{e}')

    resp = {'emotion': emotion, 'messages': msgs, 'total_days': total_days}
    if reminder_data:
        resp['reminder'] = reminder_data
    if cancelled_tasks:
        resp['cancelled_tasks'] = cancelled_tasks
    return JSONResponse(resp)


# ─────────────────── 长故事模式（文本）───────────────────

@router.post('/chat/story')
async def chat_story(data: dict):
    """
    长故事模式：生成一个完整的长故事，分成很多气泡，每条独立 TTS。
    前端在检测到"讲故事"类请求时调用这个端点（而不是 /chat/text）。
    声音质量靠和 /chat/text 完全相同的 per-bubble 合成保证（同 voice_id、同参数）。
    """
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

    system_prompt = build_system_prompt(user_id, character_id, recall_query) + '''

【★ 故事模式——必须遵守】
对方想听你讲一个完整的故事。用你（五条悟）的视角和口吻来讲。
1. 故事要完整：有开头、发展、高潮、结尾，一口气讲完，不要中途停。
2. 融入你的性格：慵懒、偶尔毒舌、偶尔温柔。
3. 分成 10-15 个气泡，每个气泡是故事的一小段。
4. 每个气泡的【日语】控制在 40-120 字之间——这点很重要，单段太长会影响语音合成质量。
5. jp 必须是纯日语，zh 是对应的中文翻译，不要把中文混进 jp。

严格按这个 JSON 返回：
{"emotion":"情绪","messages":[{"jp":"第一段日语","zh":"第一段中文"},{"jp":"第二段日语","zh":"第二段中文"}]}'''

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=4000,      # ★ 故事模式给更多 token
                system=system_prompt,
                messages=messages
            )
            raw = response.content[0].text.strip()
            print(f'[story] attempt {attempt+1}: {raw[:120]}...')
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[story] attempt {attempt+1} error: {e}')

    if not result:
        result = {
            'emotion': '平静',
            'messages': [
                {'jp': 'まあ、いいよ。話を聞かせてあげる。', 'zh': '嘛，好啊，讲个故事给你听。'},
                {'jp': '昔々、最強の呪術師がいてね。', 'zh': '很久很久以前，有一个最强的咒术师。'},
                {'jp': 'まあ、それ僕のことなんだけど。', 'zh': '嘛，虽然那说的就是我啦。'},
            ],
        }

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'user', user_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    threading.Thread(target=extract_and_save_memory,
                     args=(user_id, user_text, full_jp, character_id),
                     daemon=True).start()

    # ★ 和 /chat/text 一样，每段日语单独合成（串行、同 voice_id，声音一致）
    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    total_chars = sum(len(m['jp']) for m in msgs)
    print(f'[story] {character_id} emotion={emotion} segs={len(msgs)} chars={total_chars} days={total_days}')

    return JSONResponse({
        'emotion': emotion,
        'messages': msgs,
        'total_days': total_days,
        'total_chars': total_chars,
    })


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
        m['jp'] = sanitize_jp(m.get('jp', ''))
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

    short_memories = get_short_memory(user_id, 6, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': user_text})

    system_prompt = build_system_prompt(user_id, character_id, user_text) + '''

【★ 语音通话场景】
现在在和对方打电话。回复自然口语化，根据对方说的话灵活决定回复条数和长度：
- 简单寒暄/短句 → 1条气泡，简短回应
- 对方说了重要的事/问了复杂的问题 → 可以分2-3条气泡，像真打电话一样自然衔接
- 每条气泡10-50字，不要长篇大论，但也不要过于压缩。'''

    result = None
    for attempt in range(3):
        try:
            response = claude_client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=500,
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
        m['jp'] = sanitize_jp(m.get('jp', ''))
    # ★ 修复：去掉强制截断，改用 merge_only_extreme_short（和 /chat/text 一致）
    # 这样多气泡才能真正传到前端
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

    print(f'[voice_text] {character_id} emotion={emotion} segs={len(msgs)}')
    return JSONResponse({'emotion': emotion, 'messages': msgs})


# ─────────────────── 语音通话·长故事模式 ───────────────────

@router.post('/chat/voice_story')
async def chat_voice_story(data: dict):
    """
    语音通话里的长故事：用 Sonnet 生成完整故事，分成很多短气泡，每段独立 TTS。
    前端按顺序播放（在 voice 通话中检测到"讲故事"类请求时调用）。
    """
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

    system_prompt = build_system_prompt(user_id, character_id, user_text) + '''

【★ 语音通话·长故事模式】
对方想在通话里听你讲故事。用你（五条悟）的视角和口吻，像真的在电话里娓娓道来。
1. 故事要完整：开头、发展、高潮、结尾，一口气讲完。
2. 分成 8-15 个气泡，每个气泡是故事的一小段。
3. 每个气泡的【日语】控制在 40-90 字之间——通话场景要短一点更自然，也保证语音质量。
4. jp 必须是纯日语，zh 是对应中文翻译，不要把中文混进 jp。

严格按这个 JSON 返回：
{"emotion":"情绪","messages":[{"jp":"第一段日语","zh":"第一段中文"},{"jp":"第二段日语","zh":"第二段中文"}]}'''

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=3000,
                system=system_prompt,
                messages=messages
            )
            raw = response.content[0].text.strip()
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) >= 3:
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[voice_story] attempt {attempt+1} error: {e}')

    if not result:
        result = {
            'emotion': '平静',
            'messages': [
                {'jp': 'さて、どんな話をしようか。', 'zh': '那么，讲个什么故事呢。'},
                {'jp': '昔々、最強の呪術師がいてね。', 'zh': '很久很久以前，有一个最强的咒术师。'},
                {'jp': 'まあ、それ僕のことなんだけど。', 'zh': '嘛，虽然那说的就是我啦。'},
            ],
        }

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
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

    total_chars = sum(len(m['jp']) for m in msgs)
    print(f'[voice_story] {character_id} emotion={emotion} segs={len(msgs)} chars={total_chars}')

    return JSONResponse({
        'emotion': emotion,
        'messages': msgs,
        'total_chars': total_chars,
    })


# ─────────────────── 语音通话主动开口（接通开场 / 沉默追问） ───────────────────

@router.post('/chat/voice/proactive')
async def chat_voice_proactive(data: dict):
    """语音通话主动开口：接通开场(greeting) / 沉默追问(idle/missed)"""
    user_id         = data.get('user_id', 'default')
    character_id    = data.get('character_id', DEFAULT_CHARACTER_ID)
    mode            = data.get('mode', 'idle')
    silence_seconds = int(data.get('silence_seconds', 15))

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    # ★ greeting = 电话刚接通，顺着刚才文字聊的内容主动开口
    if mode == 'greeting':
        trigger = ('【系统：电话刚接通。请顺着你们刚才在文字里聊的内容（见上方对话历史），'
                   '像真的接起电话一样自然主动开口——把刚才的话题接上，或随口关心一句。'
                   '1-2句，简短自然。如果之前没怎么聊过，就用你的风格随意打个招呼。】')
        scene = '''

【★ 语音通话·接通开场】
你刚接起和对方的电话。主动开口，自然口语化，1-2句。
如果上方有刚才的聊天内容，就顺着那个话题接上去（别一字不差地重复，像继续聊）。'''
        n_recent = 6
    elif mode == 'missed' or silence_seconds > 60:
        trigger = '【系统：对方已经很久没说话了，可能在发呆或者走神了。你主动问她在干嘛，语气慵懒带点调侃，一两句就好。】'
        scene = '''

【★ 语音通话沉默场景】
现在你和对方在打电话，对方没说话。你主动开口打破沉默。
只输出1条气泡，15字以内，自然简短，像真打电话一样。'''
        n_recent = 4
    elif silence_seconds > 30:
        trigger = '【系统：对方沉默了一会儿了。你稍微催一下，带点撒娇或不耐烦，一两句就好。】'
        scene = '''

【★ 语音通话沉默场景】
现在你和对方在打电话，对方没说话。你主动开口打破沉默。
只输出1条气泡，15字以内，自然简短，像真打电话一样。'''
        n_recent = 4
    else:
        trigger = '【系统：对方刚沉默了几秒。你轻声问一句"在干嘛？"或者类似的，自然一点，一两句就好。】'
        scene = '''

【★ 语音通话沉默场景】
现在你和对方在打电话，对方没说话。你主动开口打破沉默。
只输出1条气泡，15字以内，自然简短，像真打电话一样。'''
        n_recent = 4

    short_memories = get_short_memory(user_id, n_recent, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': trigger})

    system_prompt = build_system_prompt(user_id, character_id, '') + scene

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
            print(f'[voice_proactive] attempt {attempt+1} error: {e}')

    if not result:
        if mode == 'greeting':
            result = {'emotion': '调皮', 'messages': [{'jp': 'もしもし、どうした？', 'zh': '喂，怎么啦？'}]}
        elif mode == 'missed':
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
        m['jp'] = sanitize_jp(m.get('jp', ''))
    # 接通开场最多2条；沉默追问保持1条
    msgs = msgs[:2] if mode == 'greeting' else msgs[:1]

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