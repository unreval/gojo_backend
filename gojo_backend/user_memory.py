"""用户记忆 v3（短期 + 长期 + 羁绊 + 统一三桶提取 + 自动纠错）

记忆四层结构：
  1. 她的事实      long_memory (character_id='shared')  —— 关于用户本人，全角色共享
  2. 我们之间的事  bond_memory (kind='between')          —— 她和某角色的共同经历，按角色独立
  3. 她告诉我的事  bond_memory (kind='told')             —— 她告诉某角色的、关于角色本人/其世界的信息
  4. 角色背景      character_memory                      —— 原作设定，只手动管理，聊天不写入

提取只用一次 Haiku 调用，同时产出 1/2/3 三类，成本和原来一样。
"""
import anthropic
from datetime import datetime, timedelta, timezone
from config import ANTHROPIC_KEY, CN_TZ, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ────────── 当前对话上下文范围（短期记忆喂给模型的部分）──────────
SHORT_MEMORY_HOURS = 24   # 把最近这么多小时的对话当"当前上下文"（想要两天就改 48）
SHORT_MEMORY_MAX   = 30   # 最多带这么多条，保护速度和 API 成本（嫌贵调小，想记更多调大）

# ★ 跨角色共享的"用户事实"桶。
SHARED_CHARACTER_ID = 'shared'

# 全部角色名缓存（做违禁词用，启动后第一次用时查一次库）
_char_names_cache = None


def _all_character_names():
    """返回库里所有角色的名字列表（含常见简称），用作用户事实的违禁词。
    ★ 以后加新角色不用再手动改违禁词列表了。"""
    global _char_names_cache
    if _char_names_cache is not None:
        return _char_names_cache
    names = []
    try:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute('SELECT name FROM characters')
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for (n,) in rows:
            if not n:
                continue
            names.append(n)
            if len(n) >= 3:
                names.append(n[:2])   # 五条 / 夏油 / 波风
                names.append(n[-2:])  # 条悟 / 油杰 / 水门
    except Exception as e:
        print(f'[memory] 读取角色名失败：{e}')
    _char_names_cache = list(dict.fromkeys(names))  # 去重保序
    return _char_names_cache


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
    """★ v3.1：给历史消息加时间标记，防止把昨晚的话当成刚刚发生。
    规则：2小时内的消息不加标记（保持自然）；更早的加【今天HH:MM】【昨天HH:MM】【M月D日 HH:MM】。
    标记只在读取时拼接，不改数据库内容。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT role, content, timestamp FROM short_memory
           WHERE user_id = %s AND character_id = %s
             AND timestamp >= NOW() - (%s * INTERVAL '1 hour')
           ORDER BY timestamp DESC
           LIMIT %s''',
        (user_id, character_id, SHORT_MEMORY_HOURS, SHORT_MEMORY_MAX)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()

    now = datetime.now(CN_TZ)
    today = now.date()
    result = []
    for role, content, ts in reversed(rows):
        marker = ''
        if ts is not None:
            # 数据库存的是 UTC，换算到北京时间再判断
            ts_cn = ts.replace(tzinfo=timezone.utc).astimezone(CN_TZ)
            gap_hours = (now - ts_cn).total_seconds() / 3600
            if gap_hours >= 2:
                d = ts_cn.date()
                if d == today:
                    day_label = '今天'
                elif (today - d).days == 1:
                    day_label = '昨天'
                else:
                    day_label = f'{d.month}月{d.day}日'
                marker = f'【{day_label}{ts_cn.strftime("%H:%M")}的消息】'
        result.append((role, marker + content if marker else content))
    return result


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


# ────────── 第 1 层：用户事实（长期记忆，shared 共享桶）──────────

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
    """返回该角色专属记忆 + 共享用户事实（shared 桶）。"""
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


# ────────── 第 2/3 层：羁绊记忆（我们之间的事 / 她告诉我的事）──────────

def save_bond_memory(user_id, character_id, kind, content):
    """kind='between'（我们之间）或 'told'（她告诉我的）。带去重。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'SELECT content FROM bond_memory WHERE user_id = %s AND character_id = %s AND kind = %s',
        (user_id, character_id, kind)
    )
    existing = cur.fetchall()
    for (e,) in existing:
        if content == e or (abs(len(content) - len(e)) < 5 and (content in e or e in content)):
            cur.close(); conn.close()
            print(f'[{user_id}] 羁绊记忆重复，跳过：{content}')
            return False
    cur.execute(
        'INSERT INTO bond_memory (user_id, character_id, kind, content) VALUES (%s, %s, %s, %s)',
        (user_id, character_id, kind, content)
    )
    conn.commit()
    cur.close()
    conn.close()
    return True


def get_bond_memories(user_id, character_id, kind=None, limit=30):
    """返回 [(id, content, timestamp)]，新→旧。kind=None 时返回全部种类。"""
    conn = get_conn()
    cur = conn.cursor()
    if kind:
        cur.execute(
            '''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s AND kind = %s
               ORDER BY timestamp DESC LIMIT %s''',
            (user_id, character_id, kind, limit)
        )
    else:
        cur.execute(
            '''SELECT id, content, timestamp FROM bond_memory
               WHERE user_id = %s AND character_id = %s
               ORDER BY timestamp DESC LIMIT %s''',
            (user_id, character_id, limit)
        )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return rows


def delete_bond_memory(memory_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM bond_memory WHERE id = %s', (memory_id,))
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


# ────────── 记忆自动纠错 ──────────

def correct_memories(user_id, user_text, character_id=DEFAULT_CHARACTER_ID):
    """用户纠正之前说错的信息时，扫描长期记忆删掉错的那条。"""
    correction_keywords = [
        '不是', '我说错', '说错了', '其实', '不对', '搞错', '记错',
        '哪有', '才不', '说反了', '重新说', '纠正',
    ]
    if not any(kw in user_text for kw in correction_keywords):
        return False

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


# ────────── 提取结果的通用校验小工具 ──────────

def _clean_content(raw_content):
    return (raw_content or '').strip().strip('「」"\'').rstrip('。.')


def _valid_user_fact(user_id, content, char_names):
    """用户事实：必须"她"开头、不含任何角色名（角色相关的应归入 bond/told）。"""
    if not content or content == '无' or len(content) < 4:
        return False
    if not content.startswith('她'):
        print(f'[{user_id}] ❌ user_fact 拒绝（非"她"开头）：{content}')
        return False
    forbidden = ['AI', 'ai', '机器人'] + char_names
    for word in forbidden:
        if word and word in content:
            print(f'[{user_id}] ❌ user_fact 拒绝（含违禁词 {word}）：{content}')
            return False
    return True


def _valid_bond(user_id, content):
    """羁绊记忆：必须"她"开头即可（允许出现角色名，那正是它的用途）。"""
    if not content or content == '无' or len(content) < 4:
        return False
    if not content.startswith('她'):
        print(f'[{user_id}] ❌ bond/told 拒绝（非"她"开头）：{content}')
        return False
    return True


VALID_CATS = ('喜好', '厌恶', '身份', '状态', '经历', '关系', '其他')


# ────────── ★ 统一三桶提取（私聊）──────────

def extract_and_save_memory(user_id, user_text, assistant_text, character_id=DEFAULT_CHARACTER_ID):
    """一次 Haiku 调用同时提取三类记忆：
    A user_fact —— 她透露的关于她自己的新事实 → long_memory(shared)
    B bond      —— 她和这个角色之间发生的事/约定/共同经历 → bond_memory(between)
    C told      —— 她告诉这个角色的、关于角色本人或其世界的信息（含剧透）→ bond_memory(told)
    """
    try:
        corrected = correct_memories(user_id, user_text, character_id)
        correction_hint = ''
        if corrected:
            correction_hint = '\n【提示】用户刚纠正了之前说错的信息，旧记忆已删除，请提取她这次给出的正确事实。'

        char_names = _all_character_names()
        from characters import get_character
        char = get_character(character_id)
        char_name = char['name'] if char else character_id

        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
        tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        existing = get_long_memory(user_id, character_id)
        existing_text = '\n'.join(f'- {m[0]}' for m in existing) if existing else '（暂无）'
        existing_bond = get_bond_memories(user_id, character_id, limit=20)
        bond_text = '\n'.join(f'- {r[1]}' for r in existing_bond) if existing_bond else '（暂无）'

        response = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=400,
            messages=[{
                'role': 'user',
                'content': f'''你是记忆整理助手。从下面这轮对话中提取值得长期记住的信息，分成三类。

【对话双方】
- "她" = 用户
- "{char_name}" = 角色（她的聊天对象）

【今天日期】{today_str}（{weekday_cn}）{correction_hint}

【已记录的她的事实】
{existing_text}

【已记录的羁绊记忆】
{bond_text}

【这次对话】
她说：{user_text}
{char_name}回复：{assistant_text}

【三类记忆的定义——每类独立判断，可以同时有，也可以都没有】
A. user_fact：她透露的、关于她自己的新事实（生日/喜好/近况/经历等）。
   - 内容里【不许】出现角色名字，只写她自己的事。
B. bond：她和{char_name}之间这次发生的、值得记住的事——约定、承诺、重要的共同话题、她对他表达的重要情感。
   - 日常寒暄闲聊不算，只记"以后会被提起"级别的事。例："她和{char_name}约好2026-07-10一起看电影"。
C. told：她告诉{char_name}的、关于{char_name}本人或他的世界的信息——包括原作剧情、他的未来、他不知道的设定。
   - content 用"她说过..."或"她告诉过{char_name}..."开头的转述。例："她说过{char_name}的未来会发生某某事"。
   - 只有当她明确在陈述这类信息时才提取；她提问、开玩笑不算。

【通用规则】
1. 只从"她说"里提取。{char_name}的回复仅供理解语境，绝不作为事实来源。
2. 撒娇/调侃/情绪宣泄/问候/提问/简单回应都不算。
3. 时间换算成绝对日期："明天"→{tomorrow_str}，"昨天"→{yesterday_str}。
4. 三类内容都必须是以"她"字开头的中文第三人称陈述句。
5. 与已记录内容重复的不要再提。
6. 某类没有就填 null。

【输出格式——严格 JSON，只输出一行】
{{"user_fact":{{"content":"她XXX","category":"喜好"}},"bond":{{"content":"她和{char_name}XXX"}},"told":{{"content":"她说过XXX"}}}}
没有的类填 null，例如全都没有：
{{"user_fact":null,"bond":null,"told":null}}
category 只能选：喜好/厌恶/身份/状态/经历/关系/其他'''
            }]
        )
        raw = response.content[0].text.strip()
        print(f'[{user_id}][{character_id}] Haiku：{raw[:150]}')

        parsed = extract_json(raw)
        if not parsed:
            return

        # A. 用户事实 → shared 桶
        uf = parsed.get('user_fact')
        if isinstance(uf, dict):
            content = _clean_content(uf.get('content'))
            category = (uf.get('category') or '其他').strip()
            if category not in VALID_CATS:
                category = '其他'
            if _valid_user_fact(user_id, content, char_names):
                if save_long_memory(user_id, content, category, SHARED_CHARACTER_ID):
                    print(f'[{user_id}] ✅ 用户事实 [{category}]（shared）：{content}')

        # B. 我们之间的事 → bond_memory(between)
        bd = parsed.get('bond')
        if isinstance(bd, dict):
            content = _clean_content(bd.get('content'))
            if _valid_bond(user_id, content):
                if save_bond_memory(user_id, character_id, 'between', content):
                    print(f'[{user_id}] ✅ 羁绊记忆（{character_id}）：{content}')

        # C. 她告诉我的事 → bond_memory(told)
        td = parsed.get('told')
        if isinstance(td, dict):
            content = _clean_content(td.get('content'))
            if _valid_bond(user_id, content):
                if save_bond_memory(user_id, character_id, 'told', content):
                    print(f'[{user_id}] ✅ 告知记忆（{character_id}）：{content}')

    except Exception as e:
        print(f'记忆提取失败：{e}')


# ────────── ★ 群聊统一提取（用户事实 + 定向告知）──────────

def extract_and_save_group_memory(user_id, user_text, round_transcript, members):
    """群聊版提取（bond 在群里语义模糊，只做 A 和 C 两类）：
    A user_fact —— 她的新事实 → long_memory(shared)
    C told      —— 她在群里告诉【某个具体角色】的关于他/他世界的信息 → 该角色的 bond_memory(told)

    members: [{'id','name'}, ...] 群里全部角色。
    """
    try:
        corrected = correct_memories(user_id, user_text, SHARED_CHARACTER_ID)
        correction_hint = ''
        if corrected:
            correction_hint = '\n【提示】用户刚纠正了之前说错的信息，旧记忆已删除，请提取她这次给出的正确事实。'

        character_names = [m['name'] for m in members]
        name_to_id = {m['name']: m['id'] for m in members}
        names_str = '、'.join(character_names)
        char_names_all = _all_character_names()

        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y-%m-%d')
        weekday_cn = ['周一', '周二', '周三', '周四', '周五', '周六', '周日'][now.weekday()]
        tomorrow_str = (now + timedelta(days=1)).strftime('%Y-%m-%d')
        yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')

        existing = get_long_memory(user_id, SHARED_CHARACTER_ID)
        existing_text = '\n'.join(f'- {m[0]}' for m in existing) if existing else '（暂无）'

        response = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=350,
            messages=[{
                'role': 'user',
                'content': f'''你是记忆整理助手。下面是一个群聊的一轮对话记录。

【群里的说话人】
- "群主" = 用户本人（她）——你【只能】从她的发言里提取
- {names_str} = 虚构角色——他们说的任何话都不得提取

【今天日期】{today_str}（{weekday_cn}）{correction_hint}

【已记录的她的事实】
{existing_text}

【群主这一轮说的话】
{user_text}

【本轮完整对话（仅供理解语境）】
{round_transcript}

【两类记忆——各自独立判断】
A. user_fact：她透露的、关于她自己的新事实。内容里不许出现角色名。
B. told：她在这句话里告诉【某个具体角色】的、关于那个角色本人或他世界的信息（含剧情/未来）。
   - target 必须是这些名字之一：{names_str}
   - content 用"她说过..."开头的转述。她是泛泛对全群说的、没有明确对象时，target 填 null。

【通用规则】
1. 只从群主的发言提取；角色说的话（哪怕角色说"她喜欢XX"）一律忽略。
2. 撒娇/调侃/提问/简单回应不算。
3. 时间换算绝对日期："明天"→{tomorrow_str}，"昨天"→{yesterday_str}。
4. 内容以"她"字开头。与已有记录重复的不提。没有就填 null。

【输出格式——严格 JSON，只输出一行】
{{"user_fact":{{"content":"她XXX","category":"喜好"}},"told":{{"target":"角色名","content":"她说过XXX"}}}}
没有的类填 null。category 只能选：喜好/厌恶/身份/状态/经历/关系/其他'''
            }]
        )
        raw = response.content[0].text.strip()
        print(f'[{user_id}][group] Haiku：{raw[:150]}')

        parsed = extract_json(raw)
        if not parsed:
            return

        # A. 用户事实 → shared
        uf = parsed.get('user_fact')
        if isinstance(uf, dict):
            content = _clean_content(uf.get('content'))
            category = (uf.get('category') or '其他').strip()
            if category not in VALID_CATS:
                category = '其他'
            if _valid_user_fact(user_id, content, char_names_all):
                if save_long_memory(user_id, content, category, SHARED_CHARACTER_ID):
                    print(f'[{user_id}][group] ✅ 用户事实 [{category}]：{content}')

        # C. 定向告知 → 目标角色的 told 桶
        td = parsed.get('told')
        if isinstance(td, dict):
            content = _clean_content(td.get('content'))
            target_name = (td.get('target') or '').strip()
            target_id = name_to_id.get(target_name)
            if target_id and _valid_bond(user_id, content):
                if save_bond_memory(user_id, target_id, 'told', content):
                    print(f'[{user_id}][group] ✅ 告知记忆（{target_id}）：{content}')

    except Exception as e:
        print(f'群聊记忆提取失败：{e}')