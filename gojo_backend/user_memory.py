"""用户记忆（短期 + 长期 + 提取 + 自动纠错 + ★shared 共享桶 + ★群聊专用提取）"""
import anthropic
from datetime import datetime, timedelta
from config import ANTHROPIC_KEY, CN_TZ, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ────────── 当前对话上下文范围（短期记忆喂给模型的部分）──────────
SHORT_MEMORY_HOURS = 24   # 把最近这么多小时的对话当"当前上下文"（想要两天就改 48）
SHORT_MEMORY_MAX   = 30   # 最多带这么多条，保护速度和 API 成本（嫌贵调小，想记更多调大）

# ★ 跨角色共享的"用户事实"桶。
#   关于用户本人的事实（生日/喜好/经历…）存这里，所有角色、私聊群聊都能读到。
SHARED_CHARACTER_ID = 'shared'


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
    """返回最近 SHORT_MEMORY_HOURS 小时内、最多 SHORT_MEMORY_MAX 条对话（时间正序）。
       说明：原来的参数 n 不再决定条数，统一由上面两个常量控制，方便一处调。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT role, content FROM short_memory
           WHERE user_id = %s AND character_id = %s
             AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
           ORDER BY timestamp DESC
           LIMIT %s''',
        (user_id, character_id, SHORT_MEMORY_HOURS, SHORT_MEMORY_MAX)
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
    """★ 返回该角色专属记忆 + 共享用户事实（shared 桶）。
    关于用户的事实存在 shared 桶里，所有角色（私聊/群聊）都能读到 → 记忆互通。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content, timestamp FROM long_memory
           WHERE user_id = %s AND character_id IN (%s, %s)
           ORDER BY timestamp DESC LIMIT 50''',
        (user_id, character_id, SHARED_CHARACTER_ID)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def _get_memories_with_id(user_id, character_id=DEFAULT_CHARACTER_ID):
    """内部用：返回 (id, content)，仅供自动纠错定位要删的记忆。★ 包含 shared 桶。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content FROM long_memory
           WHERE user_id = %s AND character_id IN (%s, %s)
           ORDER BY timestamp DESC LIMIT 50''',
        (user_id, character_id, SHARED_CHARACTER_ID)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1]) for r in rows]


def delete_long_memory(memory_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM long_memory WHERE id = %s', (memory_id,))
    conn.commit()
    cur.close()
    conn.close()


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


# ────────── ★ 记忆自动纠错 ──────────

def correct_memories(user_id, user_text, character_id=DEFAULT_CHARACTER_ID):
    """
    当用户纠正自己之前说错的信息时（"不是""我说错了""其实"…），
    扫描已有长期记忆，找出被纠正的那条并删除。
    返回 True 表示确实删了旧记忆。
    """
    # 1) 先用关键词做廉价预筛——没有纠正语气就直接跳过，省一次 Haiku 调用
    correction_keywords = [
        '不是', '我说错', '说错了', '其实', '不对', '搞错', '记错',
        '哪有', '才不', '说反了', '重新说', '纠正',
    ]
    if not any(kw in user_text for kw in correction_keywords):
        return False

    # 2) 取出带 ID 的记忆，交给 Haiku 判断到底要删哪条
    memories = _get_memories_with_id(user_id, character_id)
    if not memories:
        return False

    memory_list = '\n'.join(f'[ID:{mid}] {content}' for mid, content in memories)

    try:
        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]

        response = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{
                'role': 'user',
                'content': f'''你是记忆纠错助手。用户正在纠正自己之前说过的错误信息，你要找出哪些旧记忆需要删除。

【今天日期】{today_str}（{weekday_cn}）

【用户这次说的话】
{user_text}

【已有的记忆列表】
{memory_list}

任务：
1. 看用户这次在纠正什么。
2. 在记忆列表里找到被纠正的那一条（可能多条），返回它的 ID。
3. 如果用户只是在否定话题、撒娇、开玩笑，并没有纠正某条具体事实，返回 none。

判断示例：
- 用户说"我生日不是今天，是5月26号" → 删掉含"今天""当天日期"的生日记忆
- 用户说"我才不喜欢吃甜食" → 删掉"她喜欢甜食"那条
- 用户说"不是，我开玩笑的" → 没有纠正具体事实，返回 none
- 拿不准时，宁可返回 none，不要乱删

【输出格式——严格 JSON，只输出一行】
要删除：{{"action":"delete","ids":[1,2]}}
不删除：{{"action":"none","ids":[]}}'''
            }]
        )
        raw = response.content[0].text.strip()
        print(f'[{user_id}] 纠错扫描：{raw[:120]}')

        parsed = extract_json(raw)
        if not parsed:
            return False

        if parsed.get('action') == 'delete' and parsed.get('ids'):
            conn = get_conn()
            cur = conn.cursor()
            deleted = 0
            for mem_id in parsed['ids']:
                try:
                    mid_int = int(mem_id)
                except (ValueError, TypeError):
                    continue
                # id + user_id + character_id 三重限定，防止 Haiku 万一给错 ID 误删别人的记忆
                # ★ character_id 范围扩到 shared 桶，不然纠错删不掉共享记忆
                cur.execute(
                    '''DELETE FROM long_memory
                       WHERE id = %s AND user_id = %s AND character_id IN (%s, %s)''',
                    (mid_int, user_id, character_id, SHARED_CHARACTER_ID)
                )
                if cur.rowcount:
                    deleted += cur.rowcount
                    print(f'[{user_id}] ✂️ 纠错删除记忆 #{mid_int}')
            conn.commit()
            cur.close()
            conn.close()
            if deleted:
                print(f'[{user_id}] 纠错完成：删除了 {deleted} 条旧记忆')
                return True

        return False

    except Exception as e:
        print(f'[{user_id}] 纠错扫描失败：{e}')
        return False


# ────────── 记忆提取（★ 先纠错，再严格只提取用户事实）──────────

def extract_and_save_memory(user_id, user_text, assistant_text, character_id=DEFAULT_CHARACTER_ID):
    """
    严格只从用户那段话里提取关于"她"的事实。
    若用户在纠正旧信息，先删掉错的，再提取新的正确事实。
    悟回复仅作语境提示，绝不提取悟说的事。
    ★ 提取结果统一存 shared 桶 → 所有角色、私聊群聊互通。
    """
    try:
        # ★ 第一步：用户若在纠正，先把记错的旧记忆删掉
        corrected = correct_memories(user_id, user_text, character_id)
        correction_hint = ''
        if corrected:
            correction_hint = '\n【提示】用户刚纠正了之前说错的信息，旧记忆已删除，请提取她这次给出的正确事实。'

        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
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

【今天日期】{today_str}（{weekday_cn}）{correction_hint}

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
8. 如果用户这次没透露任何事实（只是闲聊/调侃/提问/纠正），返回"无"。

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

        if not content.startswith('她'):
            print(f'[{user_id}] ❌ 拒绝（非"她"开头）：{content}')
            return

        forbidden = ['AI', 'ai', '五条悟', '五条', '机器人']
        for word in forbidden:
            if word in content:
                print(f'[{user_id}] ❌ 拒绝（含违禁词 {word}）：{content}')
                return

        valid_cats = ('喜好', '厌恶', '身份', '状态', '经历', '关系', '其他')
        if category not in valid_cats:
            category = '其他'

        # ★ 用户事实统一存 shared 桶 → 私聊/群聊/所有角色互通
        if save_long_memory(user_id, content, category, SHARED_CHARACTER_ID):
            print(f'[{user_id}] ✅ 新长期记忆 [{category}]（shared）：{content}')
    except Exception as e:
        print(f'记忆提取失败：{e}')


# ────────── ★ 群聊专用记忆提取（多说话人安全版）──────────

def extract_and_save_group_memory(user_id, user_text, round_transcript, character_names):
    """群聊版：只从"群主"（用户本人）的发言里提取事实，角色说的话一律忽略。
    提取结果存入 shared 桶。

    user_id          : 群主真实 user_id（不是 group_xx！）
    user_text        : 群主这一轮说的那句话
    round_transcript : 本轮完整转录（带说话人标签，如 "五条悟：xxx"）
    character_names  : 群里所有角色名列表，用于违禁词过滤
    """
    try:
        # 先跑纠错（扫 shared 桶 + 违禁范围一致）
        corrected = correct_memories(user_id, user_text, SHARED_CHARACTER_ID)
        correction_hint = ''
        if corrected:
            correction_hint = '\n【提示】用户刚纠正了之前说错的信息，旧记忆已删除，请提取她这次给出的正确事实。'

        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
        tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        existing = get_long_memory(user_id, SHARED_CHARACTER_ID)
        existing_text = '\n'.join(f'- {m[0]}' for m in existing) if existing else '（暂无）'
        names_str = '、'.join(character_names)

        response = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=250,
            messages=[{
                'role': 'user',
                'content': f'''你是事实抽取助手。下面是一个群聊的一轮对话记录。

【群里的说话人】
- "群主" = 用户本人（她）——你【只能】从她的发言里提取事实
- {names_str} = 虚构角色——他们说的【任何话】都不得提取，包括他们声称的关于群主的事

【今天日期】{today_str}（{weekday_cn}）{correction_hint}

【已记录的事实】
{existing_text}

【群主这一轮说的话】
{user_text}

【本轮完整对话（仅供理解语境）】
{round_transcript}

【提取规则——严格遵守】
1. 只从"群主这一轮说的话"里提取她主动透露的关于她自己的新事实。
2. 角色（{names_str}）的发言只是语境，哪怕角色说"她喜欢XX"也绝对不要记。
3. 撒娇/调侃/情绪宣泄/问候/提问/简单回应都不算事实。
4. 时间必须换算成绝对日期："明天"→{tomorrow_str}，"昨天"→{yesterday_str}。
5. 用第三人称中文陈述句，必须以"她"字开头。
6. 绝对禁止的主语：{names_str}、AI、机器人、你、他、对方、用户。一律用"她"。
7. 已有列表里有相同或几乎相同的内容时返回"无"。
8. 这次没透露任何新事实就返回"无"。

【输出格式——严格 JSON，只输出一行】
有新事实：{{"content":"她XXX","category":"喜好"}}
没有新事实：{{"content":"无","category":""}}
分类只能选：喜好/厌恶/身份/状态/经历/关系/其他'''
            }]
        )
        raw = response.content[0].text.strip()
        print(f'[{user_id}][group] Haiku：{raw[:100]}')

        parsed = extract_json(raw)
        if not parsed:
            return

        content = parsed.get('content', '').strip().strip('「」"\'').rstrip('。.')
        category = parsed.get('category', '').strip() or '其他'

        if not content or content == '无' or len(content) < 4:
            return
        if not content.startswith('她'):
            print(f'[{user_id}][group] ❌ 拒绝（非"她"开头）：{content}')
            return

        # 违禁词：通用词 + 群里所有角色名（防止把角色的事记成用户的事）
        forbidden = ['AI', 'ai', '机器人'] + [n for n in character_names if n]
        for word in forbidden:
            if word in content:
                print(f'[{user_id}][group] ❌ 拒绝（含违禁词 {word}）：{content}')
                return

        valid_cats = ('喜好', '厌恶', '身份', '状态', '经历', '关系', '其他')
        if category not in valid_cats:
            category = '其他'

        if save_long_memory(user_id, content, category, SHARED_CHARACTER_ID):
            print(f'[{user_id}][group] ✅ 新共享记忆 [{category}]：{content}')
    except Exception as e:
        print(f'群聊记忆提取失败：{e}')