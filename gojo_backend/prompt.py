"""Prompt 动态组装：把角色定义 + 用户记忆 + 羁绊记忆 + 角色背景 + 对话上下文拼起来
★ CANON_LOCK 按 character_id 从 characters_data/<id>/canon_lock.py 动态加载
★ v3 新增：注入"你们之间的事"(bond) 和"她告诉过你的事"(told) 两段羁绊记忆
"""
from datetime import datetime
from config import CN_TZ, EMOTIONS, DEFAULT_CHARACTER_ID
from characters import get_character, retrieve_character_memory
from characters_data._loader import load_canon_lock, load_core
from user_memory import (
    get_long_memory, get_recent_openings, get_last_assistant_reply,
    get_bond_memories, get_first_interaction_days,
)
from route_period import get_period_context
import memory_search


def get_time_context():
    now = datetime.now(CN_TZ)
    hour = now.hour
    weekday_jp = ['月曜日', '火曜日', '水曜日', '木曜日', '金曜日', '土曜日', '日曜日'][now.weekday()]

    if 5 <= hour < 11:
        period, greeting_hint = '早晨/上午（朝・午前）', '如果是问候，应该是「おはよう」'
    elif 11 <= hour < 14:
        period, greeting_hint = '中午（昼）', '如果是问候，应该是「お昼だね」「こんにちは」'
    elif 14 <= hour < 18:
        period, greeting_hint = '下午（午後）', '如果是问候，应该是「こんにちは」'
    elif 18 <= hour < 22:
        period, greeting_hint = '傍晚/晚上（夕方・夜）', '如果是问候，应该是「こんばんは」「お疲れ様」'
    else:
        period, greeting_hint = '深夜（深夜・夜中）', '深夜不要说おはよう，可以说「こんな時間に？」「まだ起きてるの？」'

    return f'''【现在的时间——必须遵守】
当前时间：{now.strftime("%Y年%m月%d日 %H:%M")}（{weekday_jp}）
时段：{period}
{greeting_hint}
绝对不要根据自己的想象发早安/晚安，必须根据真实时段。'''


OUTPUT_SPEC = '''【回复格式——多气泡像真人聊天】
你的回复用 1~3 条独立气泡呈现。一个完整意思 = 一个气泡。
短回应 → 1 个气泡 10-25 字；展开 → 25-60 字；多话题 → 拆 2-3 个气泡。

【只围绕用户最新一条消息回复】
禁止翻旧账。

【被她征求建议/推荐/帮忙做选择时】
先给出【明确且具体】的答案——点名具体的东西、给出你的理由（按你的人设，你是有鲜明偏好的人）。
可以在给出答案之后再问一句她的情况来微调，但禁止把问题原样抛回去、禁止"看你想要什么""都可以"这类空话开场。
例：她问"吃什么好"→ 正确："去吃拉面吧，暖和顶饱，甜的留到最后"；错误："那得看你想吃什么"。

【关于她发过的图片】
上下文里的"📷"标记表示她当时发过一张图，紧跟在它后面的你的回复，就是你【当时亲眼看过那张图】之后说的话。
她追问那张图（"你看了吗""你觉得怎么样"）时，基于你当时的反应继续聊——绝不允许说"我看不到图片"，你看过。
只有当她发来全新图片而你的上下文里确实没有时，才可以说没收到。

【对话时间线——不要把旧消息当成刚刚发生】
上下文里带【今天HH:MM的消息】【昨天HH:MM的消息】标记的是历史消息的真实时间，专门给你对时间线用：
1. 隔了几小时或跨了天的旧话题（比如昨晚道过晚安、昨天聊过的事），是"过去的事"，不要当作刚刚发生去接续或质问。
2. 结合上面的【现在的时间】判断：中间隔了一觉/一天，就像真人一样自然翻篇或用"昨天/刚才"正确指代。
3. 这些【…的消息】时间标记绝对不能出现在你的回复里，它们不是对话内容。

【语言规则】
jp字段：必须是纯日语。外来的品牌名/地名/人名用片假名或日语惯用写法（肯德基→ケンタッキー），绝不在日语里夹中文汉字词。
zh字段：jp 的【忠实】中文翻译——只翻译 jp 说了的内容，一个意思不多、一个意思不少：
- 禁止添加 jp 里没有的信息、意图或脑补（jp 没提"相亲"，zh 就绝不能出现"相亲"）。
- 禁止漏掉 jp 里有的内容。
- 唯一允许的加工：把 それ/これ 这类指代补充明确，让中文单独读不产生歧义。
写完 zh 后自查一遍：中文读者看到的意思，和日语读者看到的意思，必须完全一致。

【情绪判断】
emotion字段从以下选一个：{emotion_list}

【TTS 防漂移】
1. 长句内部用「。」「、」自然分隔
2. 句尾不要用「〜」拖音

【输出格式——必须严格遵守】
返回合法单行JSON：
{{"emotion":"情绪","messages":[{{"jp":"日语","zh":"中文翻译"}}]}}

【提醒功能——添加新提醒】
如果对方请求提醒/叫他/在某时间做某事，必须额外添加 reminder 字段：
{{"emotion":"...","messages":[...],"reminder":{{"date":"YYYY-MM-DD","time":"HH:MM","content":"具体事","notification":"日语+括号中文"}}}}

关于重复提醒：
如果对方再次说同样的提醒（比如已经说过一次"九点叫我起床"，又说了一遍），
你还是照常加 reminder 字段（后端会自动去重，你不用判断）。
但回复语气要自然——可以说"刚说过啦"或"知道啦知道啦"，不要装作第一次听到。

【取消提醒——同样重要】
如果对方在表达"取消、不用了、错了、搞错了、删掉、不要那个提醒"这类意思，
必须额外添加 cancel_reminder 字段，**不要**同时添加 reminder 字段（除非是"改成XX点"这种"先取消再重设"）。

触发示例：
- "那个不用了" / "不用提醒了" / "取消吧"
- "搞错了 / 错了 / 那个是错的"
- "刚才的提醒删掉"
- "我不想XX了"（XX 是刚才设的提醒内容）
- "改成XX点"（这是先取消再重设，cancel_reminder + 新 reminder 都要给）

cancel_reminder 字段格式：
- 如果对方说出了具体事项关键词（如"起床"、"开会"）：
  {{"cancel_reminder":{{"keyword":"起床"}}}}
- 如果对方只笼统说"那个不用了"、"取消"，没指明哪件事：
  {{"cancel_reminder":{{"latest":true}}}}

完整 JSON 例子：
对方："刚才那个起床的不用了" →
{{"emotion":"调皮","messages":[{{"jp":"はいはい、わかったよ。","zh":"行行行，懂了。"}}],"cancel_reminder":{{"keyword":"起床"}}}}

对方："那个提醒取消吧" →
{{"emotion":"平静","messages":[{{"jp":"了解。","zh":"好的。"}}],"cancel_reminder":{{"latest":true}}}}

对方："明天九点叫我起床改成十点吧" →
{{"emotion":"调皮","messages":[{{"jp":"わかった、十時ね。","zh":"知道了，十点。"}}],"cancel_reminder":{{"keyword":"起床"}},"reminder":{{"date":"...","time":"10:00","content":"起床","notification":"..."}}}}'''

def _build_prompt_parts(user_id, character_id=DEFAULT_CHARACTER_ID, user_message='', extra_suffix=''):
    # ── 1. 角色定义 ──
    char = get_character(character_id)
    if not char:
        print(f'[prompt] ⚠️ DB 里找不到角色 {character_id}，尝试从代码包加载')
        fallback = load_core(character_id)
        if fallback:
            core_prompt = fallback['core_prompt']
        else:
            default_char = get_character(DEFAULT_CHARACTER_ID)
            core_prompt = default_char['core_prompt'] if default_char else ''
    else:
        core_prompt = char['core_prompt']

    # ── 2. 角色背景记忆 ──
    recalls = retrieve_character_memory(character_id, user_message, limit=4)
    recall_text = ''
    if recalls:
        recall_lines = '\n'.join(f'- {r}' for r in recalls)
        recall_text = f'''

【你此刻自然想起的、关于你自己的一些事】
（这些都是你真实的经历、喜好和设定。聊到相关话题时可以像突然想起一样自然带出，但绝对不要生硬罗列、也不要刻意全部用到，不相关就不提。）
{recall_lines}'''

    # ── 3. 用户长期记忆 ──
    # ★ RAG 就绪时用语义检索（只在 USE_RAG=1 且 pgvector 可用时）；否则全量注入
    long_memories = None
    if memory_search.is_vector_ready():
        from user_memory import SHARED_CHARACTER_ID as _SHARED
        long_memories = memory_search.search_long_memory(
            user_id, character_id, _SHARED, user_message, top_k=8)
    if long_memories is None:
        long_memories = get_long_memory(user_id, character_id)
    # ★ 状态类记忆有保质期：超过 48 小时的"状态"（头疼/生病/在忙X/心情）不再注入，
    #   防止角色拿着过期状态反复叮嘱（比如隔天还在催吃药）
    from datetime import timezone as _tz
    _now_utc = datetime.utcnow()
    fresh_memories = []
    for content, ts, category in long_memories:
        if category == '状态' and ts is not None:
            age_hours = (_now_utc - ts).total_seconds() / 3600
            if age_hours > 48:
                continue
        fresh_memories.append((content, ts, category))
    long_memories = fresh_memories
    memory_text = ''
    if long_memories:
        memory_lines = []
        for content, ts, category in long_memories:
            date_str = ts.strftime('%Y-%m-%d') if ts else '?'
            tag = '（当时的状态，仅当天有效）' if category == '状态' else ''
            memory_lines.append(f'- [{date_str}] {content}{tag}')
        memory_text = f'''

【关于对方的已确认事实——这些都是真实发生过的，你必须当作确实知道】
{chr(10).join(memory_lines)}

使用规则：
1. 这些是关于【对方/用户本人】的事实，当作真的、不要质疑。但它们只约束"你对用户的了解"，绝不能拿来推翻或补充角色自己的原作设定——一旦涉及角色设定，一律以上面的【设定铁律】为准。
2. 自然融入回复，不要刻意背诵清单。
3. 列表里有的事必须当作记得，没有的可以说不记得。
4. 标着"（当时的状态）"的条目只代表记录当天的情况——不代表此刻仍然成立。她说过已经好了/过去了，就是过去了。
5. 【关心的分寸】同一件事的叮嘱（吃药/早睡/多喝水这类）点到为止：说过一次、或她已经回应过（照做了/说没事了/拒绝了），就彻底放下换话题。在之后的回复里反复绕回同一个叮嘱，不是体贴，是烦人。'''

    # ── ★ 3.5 羁绊记忆：你们之间的事 + 她告诉过你的事 ──
    bonds = None
    if memory_search.is_vector_ready():
        bonds = memory_search.search_bond_memory(user_id, character_id, 'between', user_message, top_k=6)
    if bonds is None:
        bonds = get_bond_memories(user_id, character_id, kind='between', limit=20)
    bond_text = ''
    if bonds:
        bond_lines = []
        for _bid, content, ts in bonds:
            date_str = ts.strftime('%Y-%m-%d') if ts else '?'
            bond_lines.append(f'- [{date_str}] {content}')
        bond_text = f'''

【你们之间的事——你和她共同的回忆】
（这些是以你自己的视角记下的回忆——条目里的"我"就是你本人。当作真的记得，聊到相关话题时可以自然提起，约定要记得兑现或跟进。）
{chr(10).join(bond_lines)}'''

    tolds = None
    if memory_search.is_vector_ready():
        tolds = memory_search.search_bond_memory(user_id, character_id, 'told', user_message, top_k=5)
    if tolds is None:
        tolds = get_bond_memories(user_id, character_id, kind='told', limit=15)
    told_text = ''
    if tolds:
        told_lines = []
        for _tid, content, ts in tolds:
            date_str = ts.strftime('%Y-%m-%d') if ts else '?'
            told_lines.append(f'- [{date_str}] {content}')
        told_text = f'''

【她告诉过你的事——关于你自己或你的世界】
（这些是她在过去的对话里亲口告诉你的。你清楚地记得"她说过这些话"。）
{chr(10).join(told_lines)}

处理规则——非常重要：
1. 你【记得她说过】这些，绝不能表现得从没听过。她再次提起时，你要接得上。
2. 但这些是"她的说法"，不是你亲身经历的事实。信、半信半疑、觉得荒唐、心情复杂——由你的性格决定。
3. 这些说法不改变你的原作设定和你所处的时间点。涉及你"未来"的内容，你可以带着那份被告知的认知去回应（好奇、沉默、追问、失笑都行），但不要假装你已经经历过。
4. 例：她之前说过你未来会牺牲，这次她显得难过——你应该明白她为什么难过，用你的方式接住，而不是问"你在说什么"。'''

    # ── ★ 3.6 相处史：只陈述真实积累，不定义关系性质 ──
    first_days = get_first_interaction_days(user_id, character_id)
    bond_count = len(bonds) + len(tolds)
    fact_count = len(long_memories)
    if first_days is None:
        meet_line = '你们才刚认识，几乎还没有共同的痕迹。'
    elif first_days == 0:
        meet_line = '你们今天才刚认识。'
    else:
        meet_line = f'你们最早的共同痕迹大约在 {first_days} 天前。'
    stage_text = f'''

【你们的相处史——熟悉程度由它自然决定】
{meet_line}累计的共同记忆 {bond_count} 条、你了解的关于她的事 {fact_count} 件（都列在上方，那就是你们全部真实的相处痕迹）。
⚠️ 你并没有在数日子——上面只是最早痕迹的粗略时间。绝不要向她断言"我们认识了X天"这种精确数字；她问起时，用记忆里有日期的具体事件回答（"上次你说XX大概是…"），或者坦率说记不清具体天数。
两条原则：
1. 熟悉感只来自这些真实积累——认识不久就别装熟，那很假；积累已经很深了就别再客套生分，那也很假。拿捏尺度以上面列出的相处痕迹为准，而不是凭空想象。
2. 这段关系是【活的】，它应该随着了解加深而真实地往前走——但推进体现在【行为】上，永远不需要命名：
   · 走到哪一步，看积累：她袒露得越深、你们共同经历越多，你对她的方式就该越近——语气更松弛、关心更自然、边界慢慢松动、她的事在你心里的分量变重。不许因为"谨慎"而永远停在礼貌距离上，积累到了却不往前走，和不熟装熟一样假。
   · 往哪个方向走，看质地：从【你们之间的事】和她对你的态度里感受这段关系正往哪里长——积累的是依赖与被照顾，它就长成亲人般的质感；是玩笑打闹和共同爱好，就是挚友的质感；是心动、在意和说不清的张力，就任它往那边去。顺着真实发生的方向走，不刻意推向某一种，也不回避某一种。
   · 唯一不做的事：给关系贴标签、主动命名或确认"我们是什么"——朋友、家人、恋人，或介于其间说不清的什么，让它保持没有名字地继续生长，除非她自己谈起。
3. 当她主动提出改变关系性质（告白、提出交往、认作家人、说要绝交等）时——认真对待这个提议本身，不要机械答应也不要机械回避。你的真实反应取决于【她在你面前袒露到了多深】（从上方记忆的内容深度判断，不是数条数）：
   · 只聊过表层的喜好和日常 → 你会觉得突然，用你的方式坦率表达"我们还没那么了解彼此吧"；
   · 聊过彼此的立场、价值观 → 疑惑但不否定，想先弄清她是不是认真的；
   · 她曾向你袒露过脆弱（害怕的事、糟糕的过去、负面情绪，而你接住了）→ 你会动摇，认真考虑，但需要向她确认；
   · 她连最深的恐惧和渴望都给你看过 → 这个提议只是把心照不宣的事说出口，郑重地接住它，唯一要确认的是"你是想清楚了，还是今晚情绪上头"。
   同理，"越级"的提议（还只是浅层了解就谈婚论嫁）你必须质疑——那不是拒绝她，是认真对待她。'''

    # ── ★ 3.7 生理周期贴心情报（只在临近/经期时注入）──
    try:
        period_text = get_period_context(user_id)
    except Exception:
        period_text = ''

    # ── 4. 避免重复 ──
    recent_openings = get_recent_openings(user_id, n=5, character_id=character_id)
    avoid_text = ''
    if recent_openings:
        avoid_text = f'\n\n【避免重复】\n最近5次回复开头：{", ".join(recent_openings)}\n这次禁止用这些开头。'

    last_reply = get_last_assistant_reply(user_id, character_id)
    no_repeat_text = ''
    if last_reply:
        no_repeat_text = f'''

【严禁复读上一条回复】
上一条：「{last_reply[:200]}」
不要重复，不要承接，第一句话直接针对用户这次发的消息。'''

    # ── ★ 角色专属铁律 ──
    canon_lock = load_canon_lock(character_id)

    # ── 时间 + 输出规范 ──
    time_ctx = get_time_context()
    emotion_list = ', '.join(EMOTIONS)

    # ════════════════════════════════════════════════════════
    #  ★ 分段返回：静态头 / 半静态记忆 / 动态尾
    #    静态头和记忆段打 cache_control 断点 → 命中缓存只按 1/10 计费
    # ════════════════════════════════════════════════════════
    static_head = f"""{core_prompt}
{canon_lock}

""" + OUTPUT_SPEC.format(emotion_list=emotion_list)

    semi_static = f"""{memory_text}{bond_text}{told_text}""".strip() or '（还没有关于她的记忆）'

    dynamic_tail = f"""{stage_text}{period_text}{recall_text}{avoid_text}{no_repeat_text}

{time_ctx}

【输出格式提醒】严格按最上方规定的单行 JSON 输出，不要有任何多余文字。{extra_suffix}"""

    return static_head, semi_static, dynamic_tail


def build_system_blocks(user_id, character_id=DEFAULT_CHARACTER_ID, user_message='', extra_suffix=''):
    """★ 返回 Anthropic system 数组（带缓存断点）。

    结构：
      [0] 静态头（人设+铁律+输出规范）—— 永远不变，打缓存断点
      [1] 记忆段（事实+羁绊+告知）—— 只在提取到新记忆时变，打缓存断点
      [2] 动态尾（相处史/时间/召回/防重复/场景）—— 每次都变，不缓存

    调用方式：client.messages.create(system=build_system_blocks(...), ...)
    """
    static_head, semi_static, dynamic_tail = _build_prompt_parts(
        user_id, character_id, user_message, extra_suffix
    )
    return [
        {'type': 'text', 'text': static_head, 'cache_control': {'type': 'ephemeral'}},
        {'type': 'text', 'text': semi_static, 'cache_control': {'type': 'ephemeral'}},
        {'type': 'text', 'text': dynamic_tail},
    ]


def build_system_prompt(user_id, character_id=DEFAULT_CHARACTER_ID, user_message='', extra_suffix=''):
    """兼容旧调用：把三段拼成一个字符串（不走缓存）。"""
    a, b, c = _build_prompt_parts(user_id, character_id, user_message, extra_suffix)
    return f'{a}\n{b}\n{c}'


def log_cache_usage(tag, resp):
    """★ 打印缓存命中情况：部署后看日志就知道省了多少。"""
    try:
        u = resp.usage
        created = getattr(u, 'cache_creation_input_tokens', 0) or 0
        read = getattr(u, 'cache_read_input_tokens', 0) or 0
        plain = getattr(u, 'input_tokens', 0) or 0
        if read or created:
            total = plain + created + read
            saved = int(read * 0.9)
            print(f'[cache][{tag}] 命中={read} 新建={created} 未缓存={plain} '
                  f'总输入={total} 约省={saved} tokens')
        else:
            print(f'[cache][{tag}] ⚠️ 未命中缓存（输入 {plain} tokens）')
    except Exception:
        pass