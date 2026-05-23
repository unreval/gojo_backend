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
        category TEXT,
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
        notification_id VARCHAR(255) DEFAULT NULL,
        repeat_type TEXT DEFAULT 'none',
        last_completed_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    # 旧表自动补列
    cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS notification_id VARCHAR(255) DEFAULT NULL")
    cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS repeat_type TEXT DEFAULT 'none'")
    cur.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS last_completed_date TEXT")
    cur.execute("ALTER TABLE long_memory ADD COLUMN IF NOT EXISTS category VARCHAR(50) DEFAULT NULL")
    # 把旧记忆里的"用户"全部改成"她"
    cur.execute("UPDATE long_memory SET content = REPLACE(content, '用户', '她') WHERE content LIKE '用户%'")
    conn.commit()
    cur.close()
    conn.close()

def save_short_memory(user_id, role, content):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('INSERT INTO short_memory (user_id, role, content) VALUES (%s, %s, %s)', (user_id, role, content))
    cur.execute('''DELETE FROM short_memory WHERE user_id = %s AND id NOT IN (
        SELECT id FROM short_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT 100)''',
        (user_id, user_id))
    conn.commit()
    cur.close()
    conn.close()

def save_long_memory(user_id, content):
    """放宽的去重：只拒绝完全一致或几乎一字不差的"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT content FROM long_memory WHERE user_id = %s', (user_id,))
    existing = cur.fetchall()
    for (e,) in existing:
        if content == e:
            cur.close()
            conn.close()
            print(f'[{user_id}] 记忆完全重复，跳过：{content}')
            return False
        # 只在长度差<5字且互相包含时拒绝
        if abs(len(content) - len(e)) < 5 and (content in e or e in content):
            cur.close()
            conn.close()
            print(f'[{user_id}] 记忆高度重复，跳过：{content}（已有：{e}）')
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
        'SELECT content, timestamp FROM long_memory WHERE user_id = %s ORDER BY timestamp DESC LIMIT 50',
        (user_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1]) for r in rows]

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
        memory_lines = []
        for content, ts in long_memories:
            date_str = ts.strftime('%Y-%m-%d') if ts else '?'
            memory_lines.append(f'- [{date_str}] {content}')
        memory_text = f'''

【关于对方的已确认事实——这些都是真实发生过的，你必须当作确实知道】
以下是你和对方过去对话中提取的事实，每条都是对方亲口说过的真实信息：
{chr(10).join(memory_lines)}

使用规则：
1. **这些事实是真的，不要质疑**。如果对方问"我喜欢吃什么"，从上方记忆中找答案，不要说"你没说过"。
2. **自然融入回复**，不要刻意背诵清单（错误："你说过你喜欢A、B、C"；正确："要不要吃草莓蛋糕？"）。
3. 如果记忆里有相对时间（如"还有3天考试"），结合方括号里的记录日期推算现在情况。
4. 如果对方问的事确实不在上面列表里，可以诚实说不记得；但**列表里有的事必须当作记得**。'''

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
- 上方【关于对方的已确认事实】中的内容 → 这些是真实的，可以也应该使用
- 不在上面列表里的"过去的事" → 绝对不要编造

如果对方问"我喜欢什么"之类的问题，**先去上方事实列表里找答案**，找到了就如实说，找不到才说不记得。

【回复格式——多气泡像真人聊天】
你的回复用 1~3 条独立气泡呈现。
关键原则：一个完整意思 = 一个气泡。
短回应 → 1 个气泡 10-25 字；要展开 → 1 个气泡 25-60 字；多话题 → 拆 2-3 个气泡。

【只围绕用户最新一条消息回复——严格执行】
禁止翻旧账：不要翻出对话中用户几条消息前说过的话来吐槽，不要做对比。
鼓励回忆：上方已确认事实列表里的内容可以自然提到。

【语言规则——严格执行】
jp字段：必须是纯日语，绝对不能混入任何中文字符
zh字段：jp的中文翻译，自然口语化

【情绪判断】
emotion字段：根据你这次回复的语气，从下列中选一个：
{emotion_list}

【TTS 防漂移】
1. 长句内部用「。」「、」自然分隔
2. 句尾不要用「〜」拖音
3. 每条气泡都是独立完整的句子

【输出格式——必须严格遵守】
返回合法单行JSON：
{{"emotion":"情绪","messages":[{{"jp":"第一条","zh":"第一条翻译"}}]}}

【提醒功能——非常重要！必须严格执行】
如果用户在消息中**任何形式**地请求你提醒他/她、叫他/她、或者在某个时间做某事，
**必须**在 JSON 中额外添加 reminder 字段。

触发关键词："提醒我"/"叫我起床"/"XX点叫我"/"到时候喊我"/"记得提醒我"/"别忘了提醒"等

JSON 格式：
{{"emotion":"情绪","messages":[...],"reminder":{{"date":"YYYY-MM-DD","time":"HH:MM","content":"提醒内容","notification":"提醒文本"}}}}

字段说明：
- date：YYYY-MM-DD
- time：HH:MM 24小时制
- content：具体要做的事（如"起床"/"去代课"/"吃药"），不要写"提醒用户"
- notification：到点时手机弹出的通知文本，用五条悟的日语语气写一句（带括号附中文）
  - 例："おい、起きる時間だよ。サボるなよ。\\n（喂，该起床了。别偷懒哦。）"

只有用户**完全没提**任何提醒请求时才不加 reminder 字段。'''

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
    result = []
    i = 0
    while i < len(msgs):
        cur = msgs[i]
        if len(cur.get('jp', '')) < 6 and i + 1 < len(msgs):
            nxt = msgs[i + 1]
            merged = {
                'jp': cur['jp'].rstrip('。') + '。' + nxt['jp'],
                'zh': cur['zh'] + nxt['zh'],
                'audio_b64': ''
            }
            result.append(merged)
            i += 2
        else:
            result.append(cur)
            i += 1
    return result

# ───────── 记忆提取 ─────────

def extract_and_save_memory(user_id, user_text, assistant_text):
    """从一段对话里抽取关于用户的事实，存到 long_memory。"""
    try:
        # 计算当前时间相关变量
        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
        tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        # 取现有记忆作为去重参考
        existing = get_long_memory(user_id)
        existing_text = '\n'.join(f'- {m[0]}' for m in existing) if existing else '（暂无）'

        response = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=200,
            messages=[{
                'role': 'user',
                'content': f'''你是事实抽取助手。从下面这段对话中提取关于"她"（用户）的新事实。

【今天日期】{today_str}（{weekday_cn}）

【已记录的事实】
{existing_text}

【这次对话】
她说：{user_text}
AI回复：{assistant_text}

提取规则：
1. 提取她主动透露的关于自己的具体事实：
   - 喜好（食物、颜色、动物、音乐、活动、人、动漫等）
   - 厌恶（不喜欢的东西）
   - 身份（名字、年龄、生日、职业、学校、专业）
   - 状态（在做什么、最近在忙什么、计划做什么）
   - 经历（去过哪里、做过什么）
   - 关系（家人、朋友、宠物的存在）
   - 承诺/约定/计划（"答应做X""决定做Y""周末打算去Z"都要记！）

2. 撒娇/调侃/单纯情绪宣泄不算事实。

3. 去重：只在【已记录的事实】中有**完全一样**或**几乎一字不差**的条目时才回复"无"。
   补充细节、新角度、新时间都算新事实，要记！

4. **时间必须用绝对日期，不要用相对日期！**
   - "考试还有3天" → "她的考试在 {(now + timedelta(days=3)).strftime('%Y-%m-%d')}"
   - "明天交作业" → "她在 {tomorrow_str} 要交作业"
   - "昨天去了X" → "她在 {yesterday_str} 去了X"
   - 绝对禁止 "明天""下周""还有X天" 这种相对表述

5. 用第三人称简短中文陈述句记录，以"她"开头。

【输出】只输出一行：
- 有新事实：直接写"她XXX"，不加引号不加解释
- 没有新事实：写"无"'''
            }]
        )
        summary = response.content[0].text.strip()
        summary = summary.strip('「」"\'').strip()
        summary = summary.rstrip('。.')

        # 调试日志
        print(f'[{user_id}] Haiku 原始输出："{summary}" | 用户原话："{user_text[:50]}"')

        # 接受多种主语开头
        valid_prefixes = ('她', '他', '用户', '对方')
        if summary and summary != '无' and len(summary) > 4 and summary.startswith(valid_prefixes):
            saved = save_long_memory(user_id, summary)
            if saved:
                print(f'[{user_id}] ✅ 新长期记忆：{summary}')
            else:
                print(f'[{user_id}] ⚠️ 记忆已存在，跳过：{summary}')
        elif summary and summary != '无':
            print(f'[{user_id}] ❌ 格式不符（不以她/他/用户开头）："{summary}"')
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

    # 处理提醒请求
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
                'INSERT INTO tasks (user_id, title, category, due_date, due_time, reminder_minutes) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
                (user_id, reminder_data['content'], '个人', reminder_data['date'], reminder_data['time'], 0)
            )
            task_id = cur.fetchone()[0]
            conn.commit()
            cur.close()
            conn.close()
            reminder_data['task_id'] = task_id
            print(f'[{user_id}] 提醒已保存 task_id={task_id}：{reminder_data["date"]} {reminder_data["time"]} - {reminder_data["content"]}')
        except Exception as e:
            print(f'提醒保存失败：{e}')
    else:
        reminder_keywords = ['提醒我', '叫我', '喊我', '记得提醒', '别忘', '到时候叫', '点叫', '点喊', '点提醒']
        if any(kw in user_text for kw in reminder_keywords):
            print(f'⚠️ [{user_id}] 用户消息疑似含提醒请求但 LLM 未识别: "{user_text}"')

    resp = {
        'emotion': emotion,
        'messages': msgs,
        'total_days': total_days,
    }
    if reminder_data:
        resp['reminder'] = reminder_data

    return JSONResponse(resp)


@app.post('/chat/voice')
async def chat_voice(file: UploadFile = File(...)):
    return JSONResponse({'error': 'voice input not available'}, status_code=501)


@app.get('/memories')
async def get_memories(user_id: str = 'default'):
    short = get_short_memory(user_id, 20)
    long_mems = get_long_memory(user_id)
    return JSONResponse({
        'short_memory': [{'role': r, 'content': c} for r, c in short],
        'long_memory': [{'content': c, 'date': ts.strftime('%Y-%m-%d') if ts else None} for c, ts in long_mems]
    })


@app.get('/stats')
async def get_stats(user_id: str = 'default'):
    total_days = get_chat_days(user_id)
    return JSONResponse({'total_days': total_days})


@app.get('/health')
async def health():
    return {'status': 'ok', 'tts_provider': TTS_PROVIDER, 'db': 'postgresql'}


# ───────── 批量记忆提取（一键补齐过去聊天）─────────

@app.post('/extract_memory_batch')
async def extract_memory_batch(data: dict):
    """从短期记忆里批量提取长期记忆。处理还没被删的最近 100 条消息。"""
    user_id = data.get('user_id', 'default')
    short = get_short_memory(user_id, 100)

    # 把消息按 user → assistant 配对
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

    before_count = len(get_long_memory(user_id))

    # 同步处理（要等结果）
    for user_text, jp_reply in pairs:
        try:
            extract_and_save_memory(user_id, user_text, jp_reply)
        except Exception as e:
            print(f'批量提取出错：{e}')

    after_count = len(get_long_memory(user_id))
    new_count = after_count - before_count

    return JSONResponse({
        'ok': True,
        'message': f'处理了 {len(pairs)} 轮对话，新增 {new_count} 条记忆',
        'processed': len(pairs),
        'new_memories': new_count,
        'total_memories': after_count,
    })


# ───────── 日程任务 API ─────────

@app.get('/tasks')
async def get_tasks(user_id: str = 'default'):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, title, category, due_date, due_time, reminder_minutes, completed,
                  repeat_type, last_completed_date, notification_id, created_at
           FROM tasks WHERE user_id = %s
           ORDER BY completed ASC, due_date ASC NULLS LAST, created_at DESC''',
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
            'repeat_type': r[7] or 'none',
            'last_completed_date': r[8],
            'notification_id': r[9],
            'created_at': str(r[10]) if r[10] else None,
        })
    return JSONResponse({'tasks': tasks})


@app.post('/tasks')
async def create_task(data: dict):
    user_id = data.get('user_id', 'default')
    title = data.get('title', '').strip()
    if not title:
        return JSONResponse({'error': 'no title'}, status_code=400)
    category = data.get('category', '个人')
    due_date = data.get('due_date')
    due_time = data.get('due_time')
    reminder_minutes = data.get('reminder_minutes')
    repeat_type = data.get('repeat_type', 'none')

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO tasks (user_id, title, category, due_date, due_time, reminder_minutes, repeat_type)
           VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id''',
        (user_id, title, category, due_date, due_time, reminder_minutes, repeat_type)
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
    for key in ['title', 'category', 'due_date', 'due_time', 'reminder_minutes', 'completed',
                'repeat_type', 'last_completed_date', 'notification_id']:
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


# ───────── 记忆管理 API（CRUD）─────────

@app.get('/long_memory')
async def get_long_memory_api(user_id: str = 'default'):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT id, content, category, timestamp FROM long_memory WHERE user_id = %s ORDER BY timestamp DESC',
        (user_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    memories = []
    for r in rows:
        memories.append({
            'id': r[0],
            'content': r[1],
            'category': r[2] or '其他',
            'timestamp': str(r[3]) if r[3] else None,
        })
    return JSONResponse({'memories': memories})


@app.put('/long_memory/{memory_id}')
async def update_long_memory(memory_id: int, data: dict):
    content  = data.get('content', '').strip()
    category = data.get('category')
    if not content:
        return JSONResponse({'error': '内容不能为空'}, status_code=400)
    conn = get_conn()
    cur = conn.cursor()
    if category:
        cur.execute('UPDATE long_memory SET content = %s, category = %s WHERE id = %s', (content, category, memory_id))
    else:
        cur.execute('UPDATE long_memory SET content = %s WHERE id = %s', (content, memory_id))
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True, 'id': memory_id})


@app.delete('/long_memory/{memory_id}')
async def delete_long_memory(memory_id: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM long_memory WHERE id = %s', (memory_id,))
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True, 'id': memory_id})


@app.post('/transcribe')
async def transcribe_audio(data: dict):
    """接收 base64 编码的音频，用 Groq Whisper 转文字（免费）"""
    audio_b64 = data.get('audio_base64', '')
    if not audio_b64:
        return JSONResponse({'error': 'no audio'}, status_code=400)

    groq_key = os.environ.get('GROQ_KEY', '')
    if not groq_key:
        print('[transcribe] GROQ_KEY 未配置')
        return JSONResponse({'error': 'GROQ_KEY not configured', 'text': ''})

    try:
        from groq import Groq as _Groq
        import tempfile
        import base64 as _b64

        client = _Groq(api_key=groq_key)
        audio_bytes = _b64.b64decode(audio_b64)

        with tempfile.NamedTemporaryFile(suffix='.m4a', delete=False) as f:
            f.write(audio_bytes)
            temp_path = f.name

        try:
            with open(temp_path, 'rb') as f:
                transcript = client.audio.transcriptions.create(
                    model='whisper-large-v3-turbo',
                    file=f,
                    language='zh',
                    response_format='text',
                )
            text = transcript if isinstance(transcript, str) else transcript.text
            print(f'[transcribe] 识别结果：{text}')
            return JSONResponse({'text': text})
        finally:
            try: os.unlink(temp_path)
            except: pass

    except Exception as e:
        print(f'转录失败：{e}')
        return JSONResponse({'error': str(e), 'text': ''})
    

if __name__ == '__main__':
    print(f'Gojo server starting... TTS: {TTS_PROVIDER} | DB: PostgreSQL')
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
