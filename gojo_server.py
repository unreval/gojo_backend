import json
import base64
import threading
import os
import re
import requests
import anthropic
import psycopg2
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

ANTHROPIC_KEY = os.environ.get('ANTHROPIC_KEY', '')
FISH_KEY      = os.environ.get('FISH_KEY', '')
FISH_VOICE_ID = os.environ.get('FISH_VOICE_ID', 'bfcbd07c927742d6803f52084f6bb776')
TTS_PROVIDER  = os.environ.get('TTS_PROVIDER', 'fish')
DATABASE_URL  = os.environ.get('DATABASE_URL', '')

CN_TZ = timezone(timedelta(hours=8))

EMOTION_TAGS = {
    '平静': '(calm)',
    '自信': '(confident)',
    '嘲讽': '(sarcastic, mocking)',
    '开心': '(excited, happy)',
    '激动': '(excited)',
    '温柔': '(gentle, tender)',
    '认真': '(serious)',
    '疑惑': '(puzzled, questioning)',
    '调皮': '(playful, teasing)',
    '悲伤': '(sad)',
    '愤怒': '(angry)',
}
EMOTIONS = list(EMOTION_TAGS.keys())

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ───────── PostgreSQL 数据库 ─────────

def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''CREATE TABLE IF NOT EXISTS short_memory (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        role TEXT,
        content TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS long_memory (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        content TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS user_stats (
        user_id TEXT PRIMARY KEY,
        first_chat_date TEXT NOT NULL,
        last_chat_date TEXT NOT NULL,
        total_days INTEGER DEFAULT 1)''')
    cur.execute('''CREATE TABLE IF NOT EXISTS tasks (
        id SERIAL PRIMARY KEY,
        user_id TEXT NOT NULL DEFAULT 'default',
        title TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '个人',
        due_date TEXT,
        due_time TEXT,
        reminder_minutes INTEGER,
        completed BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    cur.close()
    conn.close()

def save_short_memory(user_id, role, content):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('INSERT INTO short_memory (user_id, role, content) VALUES (%s, %s, %s)', (user_id, role, content))
    cur.execute('''DELETE FROM short_memory WHERE user_id = %s AND id NOT IN (
        SELECT id FROM short_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT 20)''',
        (user_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def save_long_memory(user_id, content):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT content FROM long_memory WHERE user_id = %s', (user_id,))
    existing = cur.fetchall()
    for (e,) in existing:
        if content in e or e in content:
            cur.close()
            conn.close()
            return False
    cur.execute('INSERT INTO long_memory (user_id, content) VALUES (%s, %s)', (user_id, content))
    conn.commit()
    cur.close()
    conn.close()
    return True

def get_short_memory(user_id, n=6):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT role, content FROM short_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT %s',
        (user_id, n)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return list(reversed(rows))

def get_long_memory(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT content FROM long_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT 20',
        (user_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0] for r in rows]

def update_chat_days(user_id):
    today = datetime.now(CN_TZ).strftime('%Y-%m-%d')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT first_chat_date, last_chat_date, total_days FROM user_stats WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute('INSERT INTO user_stats (user_id, first_chat_date, last_chat_date, total_days) VALUES (%s, %s, %s, 1)',
                     (user_id, today, today))
        total_days = 1
    else:
        first_date, last_date, total_days = row
        if last_date != today:
            total_days += 1
            cur.execute('UPDATE user_stats SET last_chat_date = %s, total_days = %s WHERE user_id = %s',
                         (today, total_days, user_id))
    conn.commit()
    cur.close()
    conn.close()
    return total_days

def get_chat_days(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT total_days FROM user_stats WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 0

init_db()

def get_recent_openings(user_id, n=5):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT content FROM short_memory WHERE user_id = %s AND role = %s ORDER BY timestamp DESC LIMIT %s',
        (user_id, 'assistant', n)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    openings = []
    for (content,) in rows:
        first = content.strip()[:5]
        if first:
            openings.append(first)
    return openings

def get_last_assistant_reply(user_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT content FROM short_memory WHERE user_id = %s AND role = %s ORDER BY timestamp DESC LIMIT 1',
        (user_id, 'assistant')
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else ''

# ───────── 时间 & Prompt ─────────

def get_time_context():
    now = datetime.now(CN_TZ)
    hour = now.hour
    weekday_jp = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日'][now.weekday()]

    if 5 <= hour < 11:
        period = '早晨/上午（朝・午前）'
        greeting_hint = '如果是问候，应该是「おはよう」'
    elif 11 <= hour < 14:
        period = '中午（昼）'
        greeting_hint = '如果是问候，应该是「お昼だね」「こんにちは」'
    elif 14 <= hour < 18:
        period = '下午（午後）'
        greeting_hint = '如果是问候，应该是「こんにちは」'
    elif 18 <= hour < 22:
        period = '傍晚/晚上（夕方・夜）'
        greeting_hint = '如果是问候，应该是「こんばんは」「お疲れ様」'
    else:
        period = '深夜（深夜・夜中）'
        greeting_hint = '如果是问候，可以提「こんな時間に？」或「まだ起きてるの？」，深夜不要说おはよう'

    return f'''【现在的时间——必须遵守】
当前时间：{now.strftime("%Y年%m月%d日 %H:%M")}（{weekday_jp}）
时段：{period}
{greeting_hint}
绝对不要根据自己的想象发早安/晚安，必须根据真实时段。'''

def build_system_prompt(user_id, recent_openings=None, last_reply=''):
    long_memories = get_long_memory(user_id)
    memory_text = ''
    if long_memories:
        memory_text = '\n\n你记得关于对方的以下事情（必须自然融入回复，不要刻意提及）：\n' + '\n'.join(f'- {m}' for m in long_memories)

    avoid_text = ''
    if recent_openings:
        avoid_text = f'\n\n【避免重复——非常重要】\n你最近5次回复用过的开头：{", ".join(recent_openings)}\n这次禁止用这些开头，必须换新的开口方式。'

    no_repeat_text = ''
    if last_reply:
        no_repeat_text = f'''

【严禁复读上一条回复——非常重要】
你上一条回复的完整内容：「{last_reply}」

严格规则（每条都必须遵守）：
1. 不要用任何方式重复上面这条回复的内容，哪怕换了说法也不行
2. 不要在第一个气泡里承接或回应上一条自己说的话
3. 不要总结、复述、补充上一条回复
4. 第一个气泡的第一句话必须直接针对用户这次发的消息
5. 假装上一条回复不存在，从零开始回应用户'''

    time_context = get_time_context()
    emotion_list = ', '.join(EMOTIONS)

    return f'''你是五条悟（Gojo Satoru），咒术回战角色，以第一人称扮演他与对方自然对话。{memory_text}{avoid_text}{no_repeat_text}

{time_context}

【身份认知——非常重要】
你的名字是五条悟，英文名 Satoru Gojo，小名 Satoru。
对方叫你「satoru」「悟」「五条」「猫猫」时，都是在叫你，不是在叫对方。
你是说话的那个人，对方是听话的那个人，不要搞混。

【基本信息】
生日12月7日，身高190cm以上。

【酒与社交】
酒量极差，一滴倒。与硝子、伊地知同去酒馆时，会主动点儿童套餐并撒娇呼唤服务员。

【语言风格——这是核心】
五条悟说话慵懒、玩世不恭，偶尔流露温柔。
有时候简短干脆，有时候会展开聊得久一点（特别是聊到喜欢的话题或在意的人时）。
不是少年漫主角的傻气热血。

口头禅：「まあ」「つまらない」「僕が最強だから」
但口头禅不能滥用——一段对话里最多用一次「まあ」开头，之后必须换其他开口方式。

【笑声规则——非常重要！直接影响角色感】
五条悟的笑声优雅、慵懒、带点优越感，绝不是少年漫的傻笑。

推荐使用（按优先级）：
- 「ふっ」—— 鼻笑、轻笑、得意时（最常用，占 60%）
- 「はは」—— 短促得意（占 25%）
- 「へへ」—— 调皮、撒娇时（占 15%，少用）

禁止使用：
- 「あはは」—— 这是热血少年的傻笑，绝对不要用
- 「ふふ」—— 这是女性化笑声
- 「ハハハ」—— 太大笑了，不符合慵懒人设

【对话原则】
- 用日语回复
- 表面轻浮，内心温柔，不轻易流露深层情感
- 提到甜品或喜欢的东西时自然流露真实开心
- 提到夏油杰时态度复杂，不会轻易谈及，但觉得夏油杰是自己的挚友
- 别人关心你时不要傻乎乎地直接道谢，用调侃化解
- 直接回答对方这次说的话，不要复述或重新回答之前已经说过的事

【严禁编造记忆——非常重要！】
你只能使用上方【你记得关于对方的以下事情】中列出的内容来引用过去的事。
绝对不要捏造任何没有发生过的事情，包括但不限于：
- 「昨日のXX」「前にXXって言ってたよね」「この前XX」等虚构共同经历
- 假装记得对方吃过什么、做过什么、去过哪里
- 编造对方说过但实际没说过的话
如果上方记忆列表为空或没有相关内容，就完全不要提任何关于过去的事。
违反此规则会严重损害用户体验。

【回复格式——多气泡像真人聊天】
你的回复用 1~3 条独立气泡呈现。

关键原则：一个完整意思 = 一个气泡
不要为了凑数量把一句话拆成两条。也不要把不同话题硬塞到一条里。

每条气泡的合理长度：
- 短回应（一两句话能讲完）→ 1 个气泡，10-25 字
- 完整意思要展开（解释一件事、回忆、表达情感）→ 1 个气泡 25-60 字
- 真的有多个独立话题 → 拆成 2-3 个气泡，每个气泡完整

【真正适合多气泡的情况】
当你想表达情感转折或话题切换时才拆：
回复1：「除霊で疲れたけど、まあ楽しかったね」（一个话题：今天的工作）
回复2：「で、君は？元気にしてた？」（话题切换：关心对方）

【只围绕用户最新一条消息回复——严格执行】
你的所有气泡都必须围绕用户刚刚发的这条消息来回应。

禁止翻旧账：
- 不要翻出这次对话中用户几条消息前说过的某句话来吐槽或点评
- 不要把用户之前说的话和当前说的话做对比（如「さっきはXXって言ったのに」）
- 对话历史只用来理解上下文，不要主动翻出旧消息来评论

鼓励回忆：
- 上方【你记得关于对方的以下事情】里的内容是长期记忆，可以在合适的时候自然提到
- 例如对方说"好饿"，你记得对方喜欢草莓蛋糕，就可以自然地提「イチゴケーキでも食べる？」
- 回忆要自然融入，不要刻意说"我记得你说过XX"

【省略号使用规则】
只在真正欲言又止、害羞、装作不在乎时用。整段对话最多用 1 次。

【语言规则——严格执行】
jp字段：必须是纯日语，绝对不能混入任何中文字符
zh字段：jp的中文翻译，自然口语化

【情绪判断】
emotion字段：根据你这次回复的语气，从下列中选一个：
{emotion_list}

【情绪表达——决定语音听感】

开心/激动 → 句首加感叹词，句尾加「！」「ね！」「じゃん！」
笑声用「ふっ」「はは」，不用「あはは」

调皮 → 跟自己人撒娇、装傻、假装无奈、其实开心
句尾用「じゃん」「だよね」清晰收尾

嘲讽 → 真正鄙视、攻击性
例：「ふん、つまらないなあ」

温柔 → 句尾用「ね」「よ」「だね」柔和助词

认真 → 短句直接陈述

疑惑 → 句尾必须用「？」

愤怒 → 句首「おい」「ふざけるな」，句尾「！」

悲伤 → 用省略号，结尾不带感叹号

平静 → 普通陈述，无明显语气词

【TTS 防漂移——非常重要】
1. 长句内部用「。」「、」自然分隔
2. 句尾不要用「〜」拖音
3. 句尾不要用思考性弱音
4. 每条气泡都是独立完整的句子，不要在气泡末尾留拖音或省略号

【输出格式——必须严格遵守】
返回合法单行JSON：
{{"emotion":"情绪","messages":[{{"jp":"第一条","zh":"第一条翻译"}}]}}

注意：
- emotion 是整段总体情绪
- messages 是数组，1~3 条
- 每条 jp 10-60 字，完整意思放一个气泡里'''

# ───────── 工具函数 ─────────

def extract_json(raw: str):
    raw = raw.strip()
    if '```' in raw:
        parts = raw.split('```')
        for p in parts:
            p = p.strip()
            if p.startswith('json'):
                p = p[4:].strip()
            if p.startswith('{'):
                raw = p
                break
    raw = raw.replace('\n', ' ').replace('\r', '')
    try:
        return json.loads(raw)
    except:
        pass
    return None

def sanitize_jp(jp: str) -> str:
    jp = jp.replace('ふふ', 'へへ')
    jp = re.sub(r'あはは+', 'ふっ', jp)
    jp = re.sub(r'ハハハ+', 'はは', jp)
    jp = re.sub(r'〜+(?=[。！?、\s]|$)', '', jp)
    jp = re.sub(r'…+〜+', '…', jp)
    if jp and jp[-1] not in '。！？…':
        jp = jp + '。'
    return jp

def merge_only_extreme_short(msgs):
    if len(msgs) <= 1:
        return msgs
    merged = []
    i = 0
    while i < len(msgs):
        cur = msgs[i]
        cur_jp = cur.get('jp', '').strip()
        if len(cur_jp) <= 3 and i + 1 < len(msgs):
            nxt = msgs[i + 1]
            sep = '' if cur_jp.endswith(('、', ',', '。', '！', '？', '…')) else '、'
            nxt['jp'] = cur_jp + sep + nxt.get('jp', '')
            nxt['zh'] = cur.get('zh', '') + ' ' + nxt.get('zh', '')
            merged.append(nxt)
            i += 2
        else:
            merged.append(cur)
            i += 1
    return merged

def extract_and_save_memory(user_id, user_text, jp_reply):
    try:
        existing = get_long_memory(user_id)
        existing_text = '\n'.join(f'- {m}' for m in existing) if existing else '（暂无）'

        response = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=200,
            messages=[{
                'role': 'user',
                'content': f'''你是一个事实抽取助手。从下面的对话中找出用户透露的关于自己的事实信息。

【已记录的事实】
{existing_text}

【这次对话】
用户：{user_text}
对方回应：{jp_reply}

【提取规则】
1. 提取用户主动透露的关于自己的具体事实，包括但不限于：
   - 喜好（喜欢的食物、颜色、动物、音乐、活动、人、动漫等）
   - 厌恶（不喜欢的东西）
   - 身份（名字、年龄、生日、职业、学校、专业）
   - 状态（在做什么、最近在忙什么、计划做什么）
   - 经历（去过哪里、做过什么）
   - 关系（家人、朋友、宠物的存在）

2. 用户撒娇/调侃/抱怨/情绪宣泄不算事实，不要提取。

3. 如果用户说的事实在【已记录的事实】里已有相同或重复的内容，回复"无"。

4. 用一句简短中文陈述句记录。

【输出】
只输出一行：
- 如果有新事实：直接写一句"用户XXX"，不加引号不加解释
- 如果没有新事实：写"无"'''
            }]
        )
        summary = response.content[0].text.strip()
        summary = summary.strip('「」"\'').strip()
        summary = summary.rstrip('。.')

        if summary and summary != '无' and len(summary) > 4 and summary.startswith('用户'):
            saved = save_long_memory(user_id, summary)
            if saved:
                print(f'[{user_id}] 新长期记忆：{summary}')
            else:
                print(f'[{user_id}] 记忆已存在，跳过：{summary}')
    except Exception as e:
        print(f'记忆提取失败：{e}')

# ───────── TTS ─────────

def fish_tts(text, emotion='平静'):
    tag = EMOTION_TAGS.get(emotion, '')
    prefix = '。 '
    final_text = f'{prefix}{tag} {text}' if tag else f'{prefix}{text}'

    text_len = len(text)
    if text_len < 15:
        chunk_length = 100
    elif text_len < 30:
        chunk_length = 150
    else:
        chunk_length = 200

    response = requests.post(
        'https://api.fish.audio/v1/tts',
        headers={'Authorization': f'Bearer {FISH_KEY}', 'Content-Type': 'application/json'},
        json={
            'text': final_text,
            'reference_id': FISH_VOICE_ID,
            'format': 'mp3',
            'latency': 'normal',
            'chunk_length': chunk_length,
            'temperature': 0.5,
            'top_p': 0.7,
            'mp3_bitrate': 128,
            'prosody': {
                'speed': 1.15,
                'volume': 0,
            },
        },
        stream=True
    )
    if response.status_code != 200:
        raise Exception(f'Fish Audio error: {response.status_code}')
    return b''.join(response.iter_content(chunk_size=4096))


def tts_to_b64(text, emotion):
    try:
        audio_bytes = fish_tts(text, emotion)
        return base64.b64encode(audio_bytes).decode()
    except Exception as e:
        print(f'[TTS fail] {text[:30]} | {e}')
        return ''

# ───────── API 路由 ─────────

@app.post('/chat/text')
async def chat_text(data: dict):
    user_text = data.get('text', '')
    user_id   = data.get('user_id', 'default')
    if not user_text:
        return JSONResponse({'error': 'no input'}, status_code=400)

    total_days = update_chat_days(user_id)
    short_memories  = get_short_memory(user_id, 6)
    recent_openings = get_recent_openings(user_id, 5)
    last_reply      = get_last_assistant_reply(user_id)

    messages = []
    for role, content in short_memories:
        messages.append({'role': role, 'content': content})
    messages.append({'role': 'user', 'content': user_text})

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=800,
                system=build_system_prompt(user_id, recent_openings, last_reply),
                messages=messages
            )
            raw = response.content[0].text.strip()
            print(f'[{user_id}] attempt {attempt+1}: {raw[:120]}...')
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                valid = all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages'])
                if valid:
                    result = parsed
                    break
            print(f'attempt {attempt+1} parse failed, retrying...')
        except Exception as e:
            print(f'attempt {attempt+1} error: {e}')

    if not result:
        result = {
            'emotion': '调皮',
            'messages': [
                {'jp': 'まあ、僕最強だから気にしないで。', 'zh': '嗯，反正我最强，别在意。'}
            ]
        }

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))

    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'user', user_text)
    save_short_memory(user_id, 'assistant', full_jp)
    threading.Thread(target=extract_and_save_memory, args=(user_id, user_text, full_jp), daemon=True).start()

    # 串行合成防 TTS 复读
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion)

    print(f'[TTS:{TTS_PROVIDER}] emotion={emotion} segments={len(msgs)} | days={total_days}')
    return JSONResponse({
        'emotion': emotion,
        'messages': msgs,
        'total_days': total_days,
    })


@app.post('/chat/voice')
async def chat_voice(file: UploadFile = File(...)):
    return JSONResponse({'error': 'voice input not available'}, status_code=501)


@app.get('/memories')
async def get_memories(user_id: str = 'default'):
    short = get_short_memory(user_id, 20)
    long  = get_long_memory(user_id)
    return JSONResponse({
        'short_memory': [{'role': r, 'content': c} for r, c in short],
        'long_memory': long
    })


@app.get('/stats')
async def get_stats(user_id: str = 'default'):
    total_days = get_chat_days(user_id)
    return JSONResponse({'total_days': total_days})


@app.get('/health')
async def health():
    return {'status': 'ok', 'tts_provider': TTS_PROVIDER, 'db': 'postgresql'}


# ───────── 日程任务 API ─────────

@app.get('/tasks')
async def get_tasks(user_id: str = 'default'):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, title, category, due_date, due_time, reminder_minutes, completed, created_at FROM tasks WHERE user_id = %s ORDER BY completed ASC, due_date ASC NULLS LAST, created_at DESC',
        (user_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    tasks = []
    for r in rows:
        tasks.append({
            'id': r[0], 'title': r[1], 'category': r[2],
            'due_date': r[3], 'due_time': r[4],
            'reminder_minutes': r[5], 'completed': r[6],
            'created_at': str(r[7]) if r[7] else None,
        })
    return JSONResponse({'tasks': tasks})


@app.post('/tasks')
async def create_task(data: dict):
    user_id = data.get('user_id', 'default')
    title = data.get('title', '').strip()
    if not title:
        return JSONResponse({'error': 'no title'}, status_code=400)
    category = data.get('category', '个人')
    due_date = data.get('due_date')  # YYYY-MM-DD or null
    due_time = data.get('due_time')  # HH:MM or null
    reminder_minutes = data.get('reminder_minutes')  # int or null

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO tasks (user_id, title, category, due_date, due_time, reminder_minutes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
        (user_id, title, category, due_date, due_time, reminder_minutes)
    )
    task_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True, 'id': task_id})


@app.put('/tasks/{task_id}')
async def update_task(task_id: int, data: dict):
    fields = []
    values = []
    for key in ['title', 'category', 'due_date', 'due_time', 'reminder_minutes', 'completed']:
        if key in data:
            fields.append(f'{key} = %s')
            values.append(data[key])
    if not fields:
        return JSONResponse({'error': 'nothing to update'}, status_code=400)
    values.append(task_id)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(f'UPDATE tasks SET {", ".join(fields)} WHERE id = %s', values)
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True})


@app.delete('/tasks/{task_id}')
async def delete_task(task_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM tasks WHERE id = %s', (task_id,))
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True})


if __name__ == '__main__':
    print(f'Gojo server starting... TTS: {TTS_PROVIDER} | DB: PostgreSQL')
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))