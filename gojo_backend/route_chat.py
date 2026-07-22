"""聊天路由：/chat/text /chat/story /chat/proactive /chat/voice_text /chat/voice_story /chat/voice/proactive /transcribe

★ 本版改动（prompt 缓存）：
  - system 改用 build_system_blocks()，返回带 cache_control 的分段数组
  - 场景补充文字（故事模式/语音通话等）必须走 extra_suffix 参数传入，
    绝不能写成 build_system_blocks(...) + '字符串'（列表加字符串会直接 TypeError 崩溃）
  - 每次调用后 log_cache_usage 打印缓存命中，部署后看日志即可确认省了多少

★ v-fix：预填 JSON（修"空循环"）
  - 模型有时不输出 JSON、直接吐纯日语 → 解析失败 → 重试5次全废 → 落兜底"没听清"。
  - 解法：在 messages 末尾预填一条 {'role':'assistant','content':'{'}，强制模型必须从 { 接着写 JSON，
    拿到回复后把开头的 { 补回去再解析。所有产生 JSON 的端点都套用（见 _create_json）。
"""
import threading
import random
import json
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, TTS_PROVIDER, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json, sanitize_jp, merge_only_extreme_short
from tts import tts_to_b64, transcribe_audio_b64
from prompt import build_system_blocks, log_cache_usage
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
from task_dedup import find_similar_task   # ★ 模糊去重：同时段+意思相近就算同一件事

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ★ 预填：强制模型从 { 开始输出 JSON
def _create_json(model, max_tokens, system_blocks, messages):
    """统一的模型调用。
    ★ 不再预填 assistant '{'——claude-sonnet-4-6 不支持 assistant prefill（会 400）。
    改为直接调用，靠下面 _parse_reply 的宽松解析（从第一个 { 抠到最后一个 }）扛住
    模型偶尔在 JSON 前多说两句的情况。返回 (raw_text, response)。"""
    response = claude_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=messages,
    )
    raw = response.content[0].text.strip()
    return raw, response


def _parse_reply(raw: str):
    """把模型回复解析成 JSON。
    先用 extract_json；失败就宽松地从第一个 { 抠到最后一个 } 再解析——
    这样即使模型在 JSON 前面写了多余的日语/解说，也能把真正的 JSON 抠出来，
    不会再因为"散文前缀"而整段解析失败、掉进兜底。"""
    try:
        parsed = extract_json(raw)
    except Exception:
        parsed = None
    if parsed:
        return parsed
    try:
        i = raw.find('{')
        j = raw.rfind('}')
        if i != -1 and j > i:
            return json.loads(raw[i:j + 1])
    except Exception:
        pass
    return None


def _salvage_japanese(raw: str):
    """从模型没包成 JSON 的原始回复里，抢救出可用的日语当回复。
    用于：模型直接吐日语大白话、没输出 JSON 时，别浪费他真说的话。
    返回 {'jp':..., 'zh':...} 或 None。"""
    import re
    if not raw:
        return None
    # 去掉可能的 JSON 残骸/代码块符号/花括号碎片
    text = raw.strip().strip('`').strip()
    text = re.sub(r'^\s*\{?\s*"?(emotion|messages|jp|zh)"?\s*:?', '', text)
    text = text.replace('{', '').replace('}', '').replace('[', '').replace(']', '').strip()
    text = text.strip('"\'，, 。').strip()
    if not text:
        return None
    # 必须含有假名/日文汉字，才认为是"他真说了话"，否则宁可走兜底
    if not re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', text):
        return None
    # 截断过长的（避免把一堆乱码全塞进去）
    jp = text[:200].strip()
    return {'jp': jp, 'zh': ''}   # zh 留空，前端/TTS 用 jp 即可

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

    system_blocks = build_system_blocks(user_id, character_id, recall_query)

    result = None
    last_raw = ''   # ★ 记住最后一次模型原始回复，用于"纯日语救援"
    for attempt in range(3):
        try:
            raw, response = _create_json('claude-sonnet-4-6', 1500, system_blocks, messages)
            log_cache_usage(f'chat:{character_id}', response)
            print(f'[{user_id}][{character_id}] attempt {attempt+1}: {raw[:120]}...')
            if raw:
                last_raw = raw
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'attempt {attempt+1} error: {e}')

    # ★ 纯日语救援：模型说了日语但没包成 JSON（解析全失败）时，
    #   与其甩一句"没听清"，不如把他真正说的话用上——比兜底自然得多。
    if not result and last_raw:
        salvaged = _salvage_japanese(last_raw)
        if salvaged:
            result = {'emotion': '平静', 'messages': [salvaged]}
            print(f'[{user_id}][{character_id}] 纯日语救援：{salvaged["jp"][:40]}')

    if not result:
        fallback_pool = [
            {'jp': 'ん？ちょっと聞き取れなかった。もう一回言って。', 'zh': '嗯？没太听清，再说一遍。'},
            {'jp': 'さあ、なんだろうね。', 'zh': '谁知道呢。'},
            {'jp': 'へえ、それで？', 'zh': '哦？然后呢？'},
            {'jp': 'ふっ、急にどうしたの。', 'zh': '哼，怎么突然这样。'},
        ]
        result = {'emotion': '调皮', 'messages': [random.choice(fallback_pool)]}

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
            similar = None
            if not existing:
                similar = find_similar_task(
                    user_id,
                    reminder_data['content'],
                    reminder_data['date'],
                    reminder_data['time'],
                )
            if existing or similar:
                if existing:
                    task_id, _ = existing
                    same_title = reminder_data['content']
                else:
                    task_id, _notif, same_title = similar
                    print(f'[{user_id}] 🔁 同时段已有相近提醒「{same_title}」，跳过新建：{reminder_data["content"]}')
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

STORY_SCENE = '''

【★ 故事模式——必须遵守】
对方想听你讲一个完整的故事。用你自己的视角和口吻来讲。
1. 故事要完整：有开头、发展、高潮、结尾，一口气讲完，不要中途停。
2. 融入你的性格。
3. 分成 10-15 个气泡，每个气泡是故事的一小段。
4. 每个气泡的【日语】控制在 40-120 字之间——这点很重要，单段太长会影响语音合成质量。
5. jp 必须是纯日语，zh 是对应的中文翻译，不要把中文混进 jp。

严格按这个 JSON 返回：
{"emotion":"情绪","messages":[{"jp":"第一段日语","zh":"第一段中文"},{"jp":"第二段日语","zh":"第二段中文"}]}'''


@router.post('/chat/story')
async def chat_story(data: dict):
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

    system_blocks = build_system_blocks(user_id, character_id, recall_query, extra_suffix=STORY_SCENE)

    result = None
    for attempt in range(5):
        try:
            raw, response = _create_json('claude-sonnet-4-6', 4000, system_blocks, messages)
            log_cache_usage(f'story:{character_id}', response)
            print(f'[story] attempt {attempt+1}: {raw[:120]}...')
            parsed = _parse_reply(raw)
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

    system_blocks = build_system_blocks(user_id, character_id, task_title)

    result = None
    for attempt in range(3):
        try:
            raw, response = _create_json('claude-sonnet-4-6', 400, system_blocks, messages)
            log_cache_usage(f'proactive:{character_id}', response)
            parsed = _parse_reply(raw)
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

VOICE_CALL_SCENE = '''

【★ 语音通话场景】
现在在和对方打电话。回复自然口语化，根据对方说的话灵活决定回复条数和长度：
- 简单寒暄/短句 → 1条气泡，简短回应
- 对方说了重要的事/问了复杂的问题 → 可以分2-3条气泡，像真打电话一样自然衔接
- 每条气泡10-50字，不要长篇大论，但也不要过于压缩。'''


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

    system_blocks = build_system_blocks(user_id, character_id, user_text, extra_suffix=VOICE_CALL_SCENE)

    result = None
    for attempt in range(3):
        try:
            raw, response = _create_json('claude-haiku-4-5-20251001', 500, system_blocks, messages)
            log_cache_usage(f'voice:{character_id}', response)
            parsed = _parse_reply(raw)
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

VOICE_STORY_SCENE = '''

【★ 语音通话·长故事模式】
对方想在通话里听你讲故事。用你自己的视角和口吻，像真的在电话里娓娓道来。
1. 故事要完整：开头、发展、高潮、结尾，一口气讲完。
2. 分成 8-15 个气泡，每个气泡是故事的一小段。
3. 每个气泡的【日语】控制在 40-90 字之间——通话场景要短一点更自然，也保证语音质量。
4. jp 必须是纯日语，zh 是对应中文翻译，不要把中文混进 jp。

严格按这个 JSON 返回：
{"emotion":"情绪","messages":[{"jp":"第一段日语","zh":"第一段中文"},{"jp":"第二段日语","zh":"第二段中文"}]}'''


@router.post('/chat/voice_story')
async def chat_voice_story(data: dict):
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

    system_blocks = build_system_blocks(user_id, character_id, user_text, extra_suffix=VOICE_STORY_SCENE)

    result = None
    for attempt in range(5):
        try:
            raw, response = _create_json('claude-sonnet-4-6', 3000, system_blocks, messages)
            log_cache_usage(f'voice_story:{character_id}', response)
            parsed = _parse_reply(raw)
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
    user_id         = data.get('user_id', 'default')
    character_id    = data.get('character_id', DEFAULT_CHARACTER_ID)
    mode            = data.get('mode', 'idle')
    silence_seconds = int(data.get('silence_seconds', 15))

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

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

    system_blocks = build_system_blocks(user_id, character_id, '', extra_suffix=scene)

    result = None
    for attempt in range(3):
        try:
            raw, response = _create_json('claude-haiku-4-5-20251001', 300, system_blocks, messages)
            log_cache_usage(f'voice_proactive:{character_id}', response)
            parsed = _parse_reply(raw)
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