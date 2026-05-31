"""用户记忆（短期 + 长期 + 提取）"""
import anthropic
from datetime import datetime, timedelta
from config import ANTHROPIC_KEY, CN_TZ, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# ────────── 短期记忆 ──────────

def save_short_memory(user_id, role, content, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO short_memory (user_id, character_id, role, content) VALUES (%s, %s, %s, %s)',
        (user_id, character_id, role, content)
    )
    cur.execute('''DELETE FROM short_memory WHERE user_id = %s AND character_id = %s AND id NOT IN (
        SELECT id FROM short_memory WHERE user_id = %s AND character_id = %s
        ORDER BY timestamp DESC LIMIT 100)''',
        (user_id, character_id, user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()


def get_short_memory(user_id, n=6, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT role, content FROM short_memory
           WHERE user_id = %s AND character_id = %s
           ORDER BY timestamp DESC LIMIT %s''',
        (user_id, character_id, n)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return list(reversed(rows))


def get_recent_openings(user_id, n=5, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content FROM short_memory
           WHERE user_id = %s AND character_id = %s AND role = 'assistant'
           ORDER BY timestamp DESC LIMIT %s''',
        (user_id, character_id, n)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [r[0].strip()[:5] for r in rows if r[0].strip()]


def get_last_assistant_reply(user_id, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content FROM short_memory
           WHERE user_id = %s AND character_id = %s AND role = 'assistant'
           ORDER BY timestamp DESC LIMIT 1''',
        (user_id, character_id)
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else ''


# ────────── 用户长期记忆 ──────────

def save_long_memory(user_id, content, category=None, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT content FROM long_memory WHERE user_id = %s AND character_id = %s',
        (user_id, character_id)
    )
    existing = cur.fetchall()
    for (e,) in existing:
        if content == e:
            cur.close(); conn.close()
            print(f'[{user_id}] 记忆完全重复，跳过：{content}')
            return False
        if abs(len(content) - len(e)) < 5 and (content in e or e in content):
            cur.close(); conn.close()
            print(f'[{user_id}] 记忆高度重复，跳过：{content}（已有：{e}）')
            return False
    cur.execute(
        'INSERT INTO long_memory (user_id, character_id, content, category) VALUES (%s, %s, %s, %s)',
        (user_id, character_id, content, category)
    )
    conn.commit()
    cur.close()
    conn.close()
    return True


def get_long_memory(user_id, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content, timestamp FROM long_memory
           WHERE user_id = %s AND character_id = %s
           ORDER BY timestamp DESC LIMIT 50''',
        (user_id, character_id)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1]) for r in rows]


# ────────── 用户统计（聊天天数）──────────

def update_chat_days(user_id):
    today = datetime.now(CN_TZ).strftime('%Y-%m-%d')
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT first_chat_date, last_chat_date, total_days FROM user_stats WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute(
            'INSERT INTO user_stats (user_id, first_chat_date, last_chat_date, total_days) VALUES (%s, %s, %s, 1)',
            (user_id, today, today)
        )
        total_days = 1
    else:
        first_date, last_date, total_days = row
        if last_date != today:
            total_days += 1
            cur.execute(
                'UPDATE user_stats SET last_chat_date = %s, total_days = %s WHERE user_id = %s',
                (today, total_days, user_id)
            )
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


# ────────── 记忆提取（★ 严格只提取用户事实）──────────

def extract_and_save_memory(user_id, user_text, assistant_text, character_id=DEFAULT_CHARACTER_ID):
    """
    严格只从用户那段话里提取关于"她"的事实。
    悟回复仅作语境提示，绝对不提取悟说的事（悟自己的设定存在 character_memory 表）。
    """
    try:
        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]
        tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        existing = get_long_memory(user_id, character_id)
        existing_text = '\n'.join(f'- {m[0]}' for m in existing) if existing else '（暂无）'

        response = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=250,
            messages=[{
                'role': 'user',
                'content': f'''你是事实抽取助手。任务：从下面对话中提取**用户（"她"）**主动透露的关于她自己的新事实。

【重要：对话有两方】
- "她" = 用户（你要提取的对象）
- "悟" = 角色（仅作语境参考，**绝对不要**从他的话里抓任何事实当作用户的事）

【今天日期】{today_str}（{weekday_cn}）

【已记录的事实】
{existing_text}

【这次对话】
她说：{user_text}
悟回复：{assistant_text}

【提取规则——严格遵守】
1. **只从"她说"那一段里提取**。悟说了什么是给你理解上下文用的，不要把悟的话当成事实来源。
2. 例如：悟说"我喜欢甜食"——这是悟的喜好，**不要**记成"她喜欢甜食"或"她说悟喜欢甜食"，**直接忽略**。
3. 只提取她主动透露的具体事实——撒娇/调侃/情绪宣泄/问候/提问/简单回应（"嗯""好""对"）都不算。
4. 时间必须用绝对日期：
   - "明天" → {tomorrow_str}
   - "昨天" → {yesterday_str}
   - "还有3天" → {(now + timedelta(days=3)).strftime('%Y-%m-%d')}
5. 用第三人称中文陈述句，**必须以"她"字开头**。
6. **绝对禁止**使用以下主语：悟、五条、五条悟、AI、机器人、你、他、对方、用户。一律用"她"。
7. 去重：已有列表里有完全一样或几乎一字不差时回复"无"。
8. 如果用户这次没透露任何事实（只是闲聊/调侃/提问），返回"无"。

分类（必须选一个）：
- 喜好/厌恶/身份/状态/经历/关系/其他

【输出格式——严格 JSON，只输出一行】
有新事实：{{"content":"她XXX","category":"喜好"}}
没有新事实：{{"content":"无","category":""}}

【判断你输出是否合格的标准】
content 必须以"她"字开头，否则系统会丢弃你的输出。'''
            }]
        )
        raw = response.content[0].text.strip()
        print(f'[{user_id}] Haiku：{raw[:100]}')

        parsed = extract_json(raw)
        if not parsed:
            return

        content = parsed.get('content', '').strip().strip('「」"\'').rstrip('。.')
        category = parsed.get('category', '').strip() or '其他'

        if not content or content == '无' or len(content) < 4:
            return

        # ★ 严格首字检查：必须以"她"开头，其他主语一律拒绝
        if not content.startswith('她'):
            print(f'[{user_id}] ❌ 拒绝（非"她"开头）：{content}')
            return

        # ★ 内容黑名单：含有这些词的也拒绝（防止"她说悟..."这种间接污染）
        forbidden = ['AI', 'ai', '五条悟', '五条', '机器人']
        for word in forbidden:
            if word in content:
                print(f'[{user_id}] ❌ 拒绝（含违禁词 {word}）：{content}')
                return

        valid_cats = ('喜好','厌恶','身份','状态','经历','关系','其他')
        if category not in valid_cats:
            category = '其他'

        if save_long_memory(user_id, content, category, character_id):
            print(f'[{user_id}] ✅ 新长期记忆 [{category}]：{content}')
    except Exception as e:
        print(f'记忆提取失败：{e}')