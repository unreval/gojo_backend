"""聊天路由：/chat/text /chat/proactive /chat/voice_text /chat/voice/proactive /transcribe"""
import threading
from datetime import datetime, timezone, timedelta

import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, TTS_PROVIDER, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json, sanitize_jp, merge_only_extreme_short
from tts import tts_to_b64, transcribe_audio_b64
from prompt import build_system_prompt
from character_lore import get_relevant_lore
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


# ─────────────────── 时间感知（马来西亚/新加坡 UTC+8）───────────────────
# Railway 服务器跑的是 UTC，比本地慢 8 小时，所以这里用固定 +8 偏移修正。
TZ_LOCAL = timezone(timedelta(hours=8))
WEEKDAYS_ZH = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def get_local_time_context():
    """返回 (本地datetime, 星期几中文, 时段中文)。"""
    now = datetime.now(TZ_LOCAL)
    h = now.hour
    if 5 <= h < 8:
        period = '清晨'
    elif 8 <= h < 11:
        period = '上午'
    elif 11 <= h < 13:
        period = '中午'
    elif 13 <= h < 18:
        period = '下午'
    elif 18 <= h < 23:
        period = '晚上'
    else:
        period = '深夜凌晨'
    return now, WEEKDAYS_ZH[now.weekday()], period


def build_time_block():
    """拼到系统提示词末尾的时间感知段落。让悟说话符合真实时间，但别每句报时。"""
    now, weekday, period = get_local_time_context()
    return (
        '\n\n【★ 当前真实时间】现在是 '
        + now.strftime('%Y-%m-%d %H:%M')
        + '（' + weekday + '，' + period + '）。'
        + '说话必须符合这个时间点，但不要每句都报时间，自然融入就行。'
        + '深夜/凌晨绝对不要脑补白天的事（上课、午饭、上班等）；'
        + '如果对方深夜还醒着，可以自然地关心她怎么不睡。'
    )


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

    # ★ 角色背景知识库：按时间档 + 关键词检索，匹配到就拼进提示词（没匹配到返回空串，不影响任何东西）
    lore_block = get_relevant_lore(user_text, character_id=character_id)
    if lore_block:
        system_prompt = system_prompt + '\n\n' + lore_block

    # ★ 时间感知：普通聊天也需要，否则会出现「凌晨2点还问你上课没」的错乱
    system_prompt = system_prompt + build_time_block()

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

    # ★ 主动消息最容易因为不知道时间而说错话，把时间直接写进触发指令里
    now, weekday, period = get_local_time_context()
    time_hint = f'（现在是 {now.strftime("%H:%M")}，{weekday}，{period}）'

    if mode == 'remind':
        trigger = f'【系统触发：到提醒时间了{time_hint}】现在该主动提醒对方去做这件事："{task_title}"。语气慵懒又带点关心，1条气泡。注意符合当前时间，别说错时段。'
    else:
        trigger = f'【系统触发：超时未完成{time_hint}】对方之前要做"{task_title}"，已经过了时间没动静。主动问她做完了没，带点调侃或假装不在意的关心，1条气泡。注意符合当前时间，别说错时段。'

    short_memories = get_short_memory(user_id, 4, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': trigger})

    system_prompt = build_system_prompt(user_id, character_id, task_title) + build_time_block()

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

    # ★ 先拿基础提示词，再拼上背景知识，最后才接语音通话场景说明
    base_prompt = build_system_prompt(user_id, character_id, user_text)
    lore_block = get_relevant_lore(user_text, character_id=character_id)
    if lore_block:
        base_prompt = base_prompt + '\n\n' + lore_block

    # ★ 时间感知也要拼进来
    base_prompt = base_prompt + build_time_block()

    system_prompt = base_prompt + '''

【★ 语音通话场景】
现在在和对方打电话。回复自然口语化，根据对方说的话灵活决定回复条数和长度：
- 简单寒暄/短句 → 1条气泡，简短回应
- 对方说了重要的事/问了复杂的问题 → 可以分2-3条气泡，像真打电话一样自然衔接
- 每条气泡10-50字，不要长篇大论，但也不要过于压缩。

【★ 输出格式铁律】无论如何，只输出一个 JSON 对象，前后不要任何说明文字，也不要 markdown 代码块。
格式严格为：{"emotion":"情绪","messages":[{"jp":"日文","zh":"中文"}]}
每一条都必须同时有 jp 和 zh，不能漏。'''

    # ★ 修复语音失败率过高：
    #   1) max_tokens 500 → 800，避免多气泡 JSON 被截断（主因）
    #   2) 重试 3 → 5 次，和文字版一致
    #   3) 加上和文字版相同的严格校验（每条必须有 jp + zh）
    #   4) 最后一次重试改用 Sonnet 兜底，几乎不会再出现「ふっ、何か言った？」
    result = None
    for attempt in range(5):
        model = 'claude-haiku-4-5-20251001' if attempt < 4 else 'claude-sonnet-4-6'
        try:
            response = claude_client.messages.create(
                model=model,
                max_tokens=800,
                system=system_prompt,
                messages=messages
            )
            raw = response.content[0].text.strip()
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[voice_text] attempt {attempt+1} ({model}) error: {e}')

    if not result:
        result = {'emotion': '调皮', 'messages': [{'jp': 'ふっ、何か言った？', 'zh': '哼，你说了什么？'}]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    # ★ 用 merge_only_extreme_short（和 /chat/text 一致），多气泡才能真正传到前端
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


# ─────────────────── 语音通话沉默主动消息 ───────────────────

@router.post('/chat/voice/proactive')
async def chat_voice_proactive(data: dict):
    """语音通话中，对方长时间不说话时悟主动开口"""
    user_id         = data.get('user_id', 'default')
    character_id    = data.get('character_id', DEFAULT_CHARACTER_ID)
    mode            = data.get('mode', 'idle')
    silence_seconds = int(data.get('silence_seconds', 15))

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    if mode == 'missed' or silence_seconds > 60:
        trigger = '【系统：对方已经很久没说话了，可能在发呆或者走神了。你主动问她在干嘛，语气慵懒带点调侃，一两句就好。】'
    elif silence_seconds > 30:
        trigger = '【系统：对方沉默了一会儿了。你稍微催一下，带点撒娇或不耐烦，一两句就好。】'
    else:
        trigger = '【系统：对方刚沉默了几秒。你轻声问一句"在干嘛？"或者类似的，自然一点，一两句就好。】'

    short_memories = get_short_memory(user_id, 4, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': trigger})

    system_prompt = build_system_prompt(user_id, character_id, '') + build_time_block() + '''

【★ 语音通话沉默场景】
现在你和对方在打电话，对方没说话。你主动开口打破沉默。
只输出1条气泡，15字以内，自然简短，像真打电话一样。'''

    # ★ 和 voice_text 一样的保护：max_tokens 抬到 350、重试 4 次、最后一次 Sonnet 兜底、严格校验
    result = None
    for attempt in range(4):
        model = 'claude-haiku-4-5-20251001' if attempt < 3 else 'claude-sonnet-4-6'
        try:
            response = claude_client.messages.create(
                model=model,
                max_tokens=350,
                system=system_prompt,
                messages=messages
            )
            raw = response.content[0].text.strip()
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[voice_proactive] attempt {attempt+1} ({model}) error: {e}')

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
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = msgs[:1]   # 主动开口保持只1条气泡，避免突然刷屏

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