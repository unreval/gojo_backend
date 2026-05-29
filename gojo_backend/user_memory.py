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


# ────────── 记忆提取（带分类）──────────

def extract_and_save_memory(user_id, user_text, assistant_text, character_id=DEFAULT_CHARACTER_ID):
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
                'content': f'''你是事实抽取助手。从下面对话中提取关于"她"（用户）的新事实，并分类。

【今天日期】{today_str}（{weekday_cn}）

【已记录的事实】
{existing_text}

【这次对话】
她说：{user_text}
AI回复：{assistant_text}

提取规则：
1. 只提取她主动透露的具体事实，撒娇/调侃/情绪宣泄不算事实。
2. 时间必须用绝对日期：
   - "明天" → {tomorrow_str}
   - "昨天" → {yesterday_str}
   - "还有3天" → {(now + timedelta(days=3)).strftime('%Y-%m-%d')}
3. 用第三人称中文陈述句，以"她"开头。
4. 去重：只在已有列表里有完全一样或几乎一字不差时才回复"无"。

分类（必须选一个）：
- 喜好：喜欢的食物/颜色/动物/音乐/动漫/人
- 厌恶：不喜欢的东西
- 身份：名字/年龄/生日/职业/学校/专业
- 状态：在做什么/最近忙什么/计划做什么
- 经历：去过哪里/做过什么
- 关系：家人/朋友/宠物的存在
- 其他：以上都不是

【输出格式——严格 JSON，只输出一行】
有新事实：{{"content":"她XXX","category":"喜好"}}
没有新事实：{{"content":"无","category":""}}'''
            }]
        )
        raw = response.content[0].text.strip()
        print(f'[{user_id}] Haiku：{raw[:100]}')

        parsed = extract_json(raw)
        if not parsed:
            summary = raw.strip('「」"\'').strip().rstrip('。.')
            if summary and summary != '无' and summary.startswith(('她','他','用户','对方')) and len(summary) > 4:
                if save_long_memory(user_id, summary, '其他', character_id):
                    print(f'[{user_id}] ✅ 新长期记忆（兼容）：{summary}')
            return

        content = parsed.get('content', '').strip().strip('「」"\'').rstrip('。.')
        category = parsed.get('category', '').strip() or '其他'

        if not content or content == '无' or len(content) < 4:
            return
        if not content.startswith(('她','他','用户','对方')):
            print(f'[{user_id}] ❌ 格式不符：{content}')
            return

        valid_cats = ('喜好','厌恶','身份','状态','经历','关系','其他')
        if category not in valid_cats:
            category = '其他'

        if save_long_memory(user_id, content, category, character_id):
            print(f'[{user_id}] ✅ 新长期记忆 [{category}]：{content}')
    except Exception as e:
        print(f'记忆提取失败：{e}')
