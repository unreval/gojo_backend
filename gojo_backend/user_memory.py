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
from character_relations import get_relations_text

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ────────── 当前对话上下文范围（短期记忆喂给模型的部分）──────────
SHORT_MEMORY_HOURS = 24   # 把最近这么多小时的对话当"当前上下文"（想要两天就改 48）
SHORT_MEMORY_MAX   = 20   # 最多带这么多条，保护速度和 API 成本（嫌贵调小，想记更多调大）

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

def _bg_embed(table, row_id, content):
    """后台补 embedding（RAG 未启用时是空操作）。"""
    try:
        import memory_search, threading
        if not memory_search.is_vector_ready():
            return
        threading.Thread(target=memory_search.save_embedding,
                         args=(table, row_id, content), daemon=True).start()
    except Exception:
        pass


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
        'INSERT INTO long_memory (user_id, character_id, content, category) VALUES (%s, %s, %s, %s) RETURNING id',
        (user_id, character_id, content, category)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    _bg_embed('long_memory', new_id, content)   # ★ RAG 启用时后台补向量
    return True


def get_long_memory(user_id, character_id=DEFAULT_CHARACTER_ID):
    """返回该角色专属记忆 + 共享用户事实（shared 桶）。[(content, timestamp, category)]"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT content, timestamp, category FROM long_memory
           WHERE user_id = %s AND character_id IN (%s, %s)
           ORDER BY timestamp DESC LIMIT 40''',
        (user_id, character_id, SHARED_CHARACTER_ID)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [(r[0], r[1], r[2] or '其他') for r in rows]


def _get_memories_with_id(user_id, character_id=DEFAULT_CHARACTER_ID):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, content FROM long_memory
           WHERE user_id = %s AND character_id IN (%s, %s)
           ORDER BY timestamp DESC LIMIT 40''',
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
        'INSERT INTO bond_memory (user_id, character_id, kind, content) VALUES (%s, %s, %s, %s) RETURNING id',
        (user_id, character_id, kind, content)
    )
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    _bg_embed('bond_memory', new_id, content)   # ★ RAG 启用时后台补向量
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


# ────────── 认识时长（按角色最早共同痕迹算，不是全局app天数）──────────

def get_first_interaction_days(user_id, character_id):
    """返回和【这个角色】最早的共同痕迹距今多少天；完全没有痕迹返回 None。
    依据：该角色的羁绊记忆 + 该角色专属长期记忆 + 该角色的短期记忆，取最早时间。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT LEAST(
        COALESCE((SELECT MIN(timestamp) FROM bond_memory  WHERE user_id=%s AND character_id=%s), 'infinity'::timestamp),
        COALESCE((SELECT MIN(timestamp) FROM long_memory  WHERE user_id=%s AND character_id=%s), 'infinity'::timestamp),
        COALESCE((SELECT MIN(timestamp) FROM short_memory WHERE user_id=%s AND character_id=%s), 'infinity'::timestamp)
    )''', (user_id, character_id, user_id, character_id, user_id, character_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    earliest = row[0] if row else None
    # psycopg2 会把 'infinity' 转成 9999 年的 datetime.max
    if earliest is None or str(earliest) == 'infinity' or getattr(earliest, 'year', 0) >= 9000:
        return None
    days = (datetime.utcnow() - earliest).days
    return max(days, 0)


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
    """实际"聊过天的天数"（不含没说话的日子）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT total_days FROM user_stats WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 0


def get_companion_days(user_id):
    """★ 陪伴的日子 = 从第一次聊天那天到今天的【日历天数】。
    主页显示用这个：哪怕某天没说话，日子也照样在走——这才叫陪伴。
    （旧的 total_days 只数"开口说过话的天数"，所以会停在 27 不动。）"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT first_chat_date FROM user_stats WHERE user_id = %s', (user_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row[0]:
        return 0
    try:
        first = datetime.strptime(str(row[0])[:10], '%Y-%m-%d').date()
        today = datetime.now(CN_TZ).date()
        return max((today - first).days + 1, 1)
    except Exception:
        return 0


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


def _valid_bond(user_id, content, char_name=''):
    """羁绊记忆：主语可以是 她 / 他们 / 角色本人（他的表态记成他的）。"""
    if not content or content == '无' or len(content) < 4:
        return False
    ok_prefixes = ['我', '我们', '她', '他们']
    if char_name:
        ok_prefixes.append(char_name)   # 兼容旧格式
    if not any(content.startswith(p) for p in ok_prefixes):
        print(f'[{user_id}] ❌ bond 拒绝（主语不合规）：{content}')
        return False
    return True


def _valid_told(user_id, content):
    """告知记忆：她告诉角色的事，必须"她"开头。"""
    if not content or content == '无' or len(content) < 4:
        return False
    if not content.startswith('她'):
        print(f'[{user_id}] ❌ told 拒绝（非"她"开头）：{content}')
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

        # ★ 该角色世界里的重要人物 —— 让 Haiku 知道名字对应的身份,别把"杰"猜成学生
        relations_block = get_relations_text(character_id)
        relations_intro = (f'\n{relations_block}\n' if relations_block else '')

        response = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=400,
            messages=[{
                'role': 'user',
                'content': f'''你是记忆整理助手。从下面这轮对话中提取值得长期记住的信息，分成三类。

【对话双方】
- "她" = 用户
- "{char_name}" = 角色（她的聊天对象）
{relations_intro}
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
   - 【只记她本人】：她在讲别人（朋友/同事/家人）的事时，不属于 user_fact，填 null。见通用规则第 9 条。
B. bond：她和{char_name}之间这次发生的、值得记住的事——约定、承诺、重要表态、她表达的重要情感、{char_name}对她说的重要的话。
   - 【视角】：以{char_name}的第一人称写，"我"就是{char_name}——这是要存进他自己脑子里的回忆。
     她做的写"她…对我…"；{char_name}自己做的写"我…"；共同的写"我和她…"。绝不把我说的话写成"她说过"。
   - 【必须是一句话的总结，30 字以内】：像人脑记事一样只记"发生了什么"，不是聊天记录存档。
     ❌ 绝对禁止：抄日语原文、附中文翻译、加"——这是我在她…时期对她的鼓励"这种旁白解说、写成小作文。
     ✅ 正确："我鼓励她签证快点下来"、"我劝她早点回家别后悔"、"我安慰她说长辈不是在怨她"。
   - 日常寒暄闲聊不算，只记"以后会被提起"级别的事。
   - 例："我和她约好2026-07-10一起看电影"；"她夸了我的新发型"。
C. told：她告诉{char_name}的、关于{char_name}本人或他的世界的信息——包括原作剧情、他的未来、他不知道的设定。
   - content 用"她说过..."或"她告诉过{char_name}..."开头的转述。例："她说过{char_name}的未来会发生某某事"。
   - 只有当她明确在陈述这类信息时才提取；她提问、开玩笑不算。

【通用规则】
1. 【事实只信她】：user_fact 和 told 只能从"她说"里提取，{char_name}的回复绝不作为这两类的来源。
2. 【我的话记成我的】：{char_name}（也就是"我"）的重要表态可以记入 bond，写成"我说过/我认为/我答应了…"，
   绝不写成"她说过"。我随口报的数字、天数、结论（如"我们认识35天了"）多半只是顺着聊，一般不值得记；
   真要记也只能记成"我当时说…"，绝不能当客观事实。
3. 撒娇/调侃/情绪宣泄/问候/提问/简单回应都不算。"她问了XX"这类只有在话题本身重大时才值得记。
4. "确认了认识多少天"这类元对话不要提取；"讨论了是什么关系"只有当某一方给出了值得记住的正式表态时才记，且主语写对。
5. 时间换算成绝对日期："明天"→{tomorrow_str}，"昨天"→{yesterday_str}。
6. user_fact 和 told 必须以"她"开头；bond 以"我""我们"或"她"开头（第一人称，"我"={char_name}）。
7. 与已记录内容重复或【意思相近】的，绝不再提——宁可漏记不可重复。
7.5 所有记忆都必须是【简短的一句话】（30 字以内），只记事实和事件本身。
    禁止引用原文对话、禁止附翻译、禁止补充解说和背景铺垫——那是聊天记录该干的事，不是记忆。
8. 某类没有就填 null（大多数日常对话三类都是 null，这很正常）。
9. 【必须分清"她自己"和"她转述的别人"——非常重要】：
   她说的话里，有些是关于她本人，有些是她在讲【别人】（她的朋友、同学、同事、家人等）的事。
   - 只有【明确是她本人】的事，才提取成 user_fact。
   - 她在转述"我朋友…""我有个朋友…""她（指第三人）…"这类【别人的事】，
     【绝对不要】记成关于她的 user_fact，也不要记成 bond。这类基本应该直接 null。
   - 唯一例外：如果"她对这件别人的事的反应/感受"本身是关于她的重要情绪（且明确是她的感受），
     可以只记那份感受，写清楚是"她因为朋友的事而…"，绝不能把朋友的处境写成她的处境。
   - 分不清是她还是她朋友时：宁可 null，绝不猜成她本人。
   - 反例（禁止）：用户说"我朋友陷入情感困境整天哭" → ❌ 不许记成"她陷入情感困境整天哭"。
     正确处理：这是她朋友的事，不是她的事，user_fact 填 null。

10. ★【词汇中性化——非常重要】你作为记忆提取器，作为语言模型你在描述"男女互动"时训练默认词汇偏言情小说，
    这会污染记忆库、把中性事件写成暧昧场景。你必须【主动对抗】这种引力：
    ▸ 只用【中性动词】描述行为，不加解读：
      · "问"（不是"试探"、"探"、"暗示"）
      · "说"（不是"直言"、"坦言"、"表白"——除非她真的在正式表白）
      · "回答/回应"（不是"承认"、"默认"——除非上下文明确）
      · "告诉"（不是"透露"、"吐露心声"）
      · "约"、"答应"、"拒绝"、"劝"、"催"、"提醒"、"关心"——都是好动词
    ▸ 【禁用的言情腔词汇】（这些词写进记忆就是污染）：
      试探 / 心思 / 心动 / 动心 / 暗示 / 若有所思 / 眼神交汇 /
      直言说出 / 主动靠近 / 依赖 / 撒娇（除非明显是撒娇动作）/
      引导她坦率表达 / 探问 / 情愫 / 心事
    ▸ 【正例 vs 反例】：
      ❌ 她用疑问句试探我心里想的答案
      ✅ 她问我怎么想
      ❌ 她直言说出喜欢我
      ✅ 她跟我说她喜欢我（★ 注意"跟我说"是中性,"直言说出"是言情腔）
      ❌ 我引导她坦率表达
      ✅ 我劝她把真话说出来（或直接写"她后来告诉我 XX"）
      ❌ 她眼神里带着期待
      ✅ 她等着我回话（如果发生了）；否则不记
    ▸ 【判断标准】：写完 bond 后自查——把内容读一遍，"这句话是否像言情小说的旁白？"
      像 → 重写成流水账；不像 → OK。宁可平实到无聊，也不要暧昧文艺。

11. ★【遇到人名先查上面的"重要人物"表——非常重要】
    你可能不熟悉"{char_name}"这个角色的世界里谁是谁。当对话里出现名字时:
    - 先看上方【★ 你世界里的重要人物】那段(如果有的话)
    - 记忆里必须写对身份 —— 例:"她说杰是叛徒"应该写"她说我挚友是叛徒"(因为杰=我的挚友),
      不是"她说学生是叛徒"或"她说某人是叛徒"
    - 【禁止】在不知道对方是谁时,瞎猜身份("学生""同事""朋友")——那会写错记忆
    - 关系表里没有的名字 → 直接用原名,别猜(例:"她说小张是叛徒" → "她说小张是叛徒",别改成"她说朋友是叛徒")

12. ★【一次分享 ≠ 身份特征——非常重要】
    她**发了一次某样东西 / 提了一句某件事**,只能提取那**一次的行为**,不能扩展成【她是 X】这种身份/专业/长期状态的判断。
    -  ❌ 错误(过度推断):
      · 她发了一张芙莉莲的图 → "她喜欢芙莉莲"
      · 她说她刷到某个视频 → "她爱看某某类型视频"
      · 她说她今天吃了寿司 → "她喜欢寿司"
      · 她提了一次她的学校 → "她学 XX 专业"
    -  ✅ 正确(只记那次事件本身):
      · 她今天分享了一张芙莉莲的图给我
      · 她今天说她刷到某个视频
      · 她今天吃了寿司
      · 她提到自己的学校
    - 【判断标准】:她说"我喜欢/常常/一直/我是 X 的" → 才能提"她 X"。
      她只是【单次分享/提及】 → 只能记"她今天分享了/提了 XX"。
    - 【禁止】把一次分享推断成【爱好/习惯/身份】,那是言情小说主角推断女主的写法,不是记忆整理。

【输出格式——严格 JSON，只输出一行】
{{"user_fact":{{"content":"她XXX","category":"喜好"}},"bond":{{"content":"我和她XXX 或 我说过XXX 或 她对我XXX"}},"told":{{"content":"她说过XXX"}}}}
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
                # ★ 单聊里说的只有这个角色知道（谁在场谁知道）；群聊说的才进 shared
                if save_long_memory(user_id, content, category, character_id):
                    print(f'[{user_id}] ✅ 用户事实 [{category}]（{character_id} 专属）：{content}')

        # B. 我们之间的事 → bond_memory(between)
        bd = parsed.get('bond')
        if isinstance(bd, dict):
            content = _clean_content(bd.get('content'))
            if _valid_bond(user_id, content, char_name):
                if save_bond_memory(user_id, character_id, 'between', content):
                    print(f'[{user_id}] ✅ 羁绊记忆（{character_id}）：{content}')

        # C. 她告诉我的事 → bond_memory(told)
        td = parsed.get('told')
        if isinstance(td, dict):
            content = _clean_content(td.get('content'))
            if _valid_told(user_id, content):
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

【三类记忆——各自独立判断】
A. user_fact：她透露的、关于她自己的新事实。内容里不许出现角色名。
B. told：她在这句话里告诉【某个具体角色】的、关于那个角色本人或他世界的信息（含剧情/未来）。
   - target 必须是这些名字之一：{names_str}
   - content 用"她说过..."开头的转述。她是泛泛对全群说的、没有明确对象时，target 填 null。
C. char_bonds：这一轮里发生的、值得【某个角色】记进自己回忆的互动——角色之间的交流、角色和群主之间的重要往来都算。
   - 为每个相关角色各写一条（0~3条），以【该角色的第一人称】写，"我"=该角色本人。
   - target 是这条回忆属于谁；content 例："我和杰在群里为说话方式拌了几句嘴，她在旁边看着"（存进五条悟）、
     "我和悟斗了几句嘴，她说我们像老夫老妻"（存进夏油杰）。
   - 日常寒暄不记，只记有内容的互动。

【通用规则】
1. 【事实只信群主】：user_fact 和 told 只能来自群主的发言；角色说的话（哪怕角色说"她喜欢XX"）不得作为这两类的来源。
   但 char_bonds 记录的是互动事件本身，谁参与了、发生了什么，可以基于完整对话判断。
2. 撒娇/调侃/提问/简单回应不算 user_fact 和 told。
3. 时间换算绝对日期："明天"→{tomorrow_str}，"昨天"→{yesterday_str}。
4. user_fact 和 told 以"她"开头；char_bonds 以"我"或"我们"开头。与已有记录重复的不提。没有就填 null。
5. ★【词汇中性化——同样重要】跟单聊记忆一样，你在描述"男女互动"时训练数据默认走言情风，
   必须【主动对抗】。char_bonds 里只用中性动词（问/说/告诉/约/答应/劝/催/提醒/关心），
   禁用言情腔词（试探/心思/心动/暗示/直言/坦言/表白/引导/情愫/心事）。
   写完自查：是否像言情小说旁白？像就重写成流水账。宁可平实无聊，也不要暧昧文艺。

【输出格式——严格 JSON，只输出一行】
{{"user_fact":{{"content":"她XXX","category":"喜好"}},"told":{{"target":"角色名","content":"她说过XXX"}},"char_bonds":[{{"target":"角色名","content":"我XXX"}}]}}
没有的类填 null（char_bonds 没有就填 []）。category 只能选：喜好/厌恶/身份/状态/经历/关系/其他'''
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
            if target_id and _valid_told(user_id, content):
                if save_bond_memory(user_id, target_id, 'told', content):
                    print(f'[{user_id}][group] ✅ 告知记忆（{target_id}）：{content}')

        # D. ★ 角色互动回忆 → 各自的 bond 桶（第一人称）
        cbs = parsed.get('char_bonds')
        if isinstance(cbs, list):
            for cb in cbs[:3]:
                if not isinstance(cb, dict):
                    continue
                content = _clean_content(cb.get('content'))
                target_name = (cb.get('target') or '').strip()
                target_id = name_to_id.get(target_name)
                if target_id and _valid_bond(user_id, content):
                    if save_bond_memory(user_id, target_id, 'between', content):
                        print(f'[{user_id}][group] ✅ 互动记忆（{target_id}）：{content}')

    except Exception as e:
        print(f'群聊记忆提取失败：{e}')