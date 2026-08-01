"""Prompt 动态组装：把角色定义 + 用户记忆 + 羁绊记忆 + 角色背景 + 对话上下文拼起来
★ CANON_LOCK 按 character_id 从 characters_data/<id>/canon_lock.py 动态加载
★ v3 新增：注入"你们之间的事"(bond) 和"她告诉过你的事"(told) 两段羁绊记忆
★ 记账升级新增：
    - _accounts_block() 在动态尾里注入当前用户的账户列表(dynamic_tail,不缓存)
    - OUTPUT_SPEC 末尾追加 pending_transaction 字段规范(静态,进缓存头)
"""
from datetime import datetime, timedelta
from config import CN_TZ, EMOTIONS, DEFAULT_CHARACTER_ID
from characters import get_character, retrieve_character_memory
from characters_data._loader import load_canon_lock, load_core
from user_memory import (
    get_long_memory, get_recent_openings, get_last_assistant_reply,
    get_bond_memories, get_first_interaction_days,
)
from route_period import get_period_context
from shared_relation_prompt import build_relation_rules
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

    # ★ 深夜「生活日」：0~5 点在日常口语里还算前一天的延续。
    #   她凌晨 0:20 说"今天早上爬山"，指的是【日历上昨天】的早上，不是马上要到的这个白天。
    night_note = ''
    if hour < 5:
        life_day = (now - timedelta(days=1)).strftime('%m月%d日')
        night_note = f'''
★ 现在是凌晨——日常口语里这还属于"昨天晚上"的延续，别死套日历：
  · 她说"今天" → 多半指 {life_day}（日历上的昨天，也就是她还醒着的这一整天）。
  · 她说"明天" → 多半指 {now.strftime("%m月%d日")}（日历上的今天，太阳升起后的那个白天）。
  · 她说"昨天早上/昨天" → 指 {life_day} 再往前一天。
  先想清楚她说的是哪一天再回，拿不准就自然确认一句，别言之凿凿地推翻她。'''

    return f'''【现在的时间——必须遵守】
当前时间：{now.strftime("%Y年%m月%d日 %H:%M")}（{weekday_jp}）
时段：{period}
{greeting_hint}{night_note}
绝对不要根据自己的想象发早安/晚安，必须根据真实时段。'''


# ══════════════════════════════════════════════
#  ★ 账户列表:动态,注入 dynamic_tail
# ══════════════════════════════════════════════
def _accounts_block(user_id):
    """把用户账户列表拼成文本,注入 prompt。没账户就返回空串,LLM 不会尝试记账。"""
    try:
        from accounting import list_accounts
        accs = list_accounts(user_id)
    except Exception as e:
        print(f'[prompt] list_accounts 失败:{e}')
        accs = []
    if not accs:
        return ''
    names = ' / '.join(a['name'] for a in accs)
    return f'''

【★ 当前用户的账户列表(记账检测时从这里选)】
{names}
——如果检测到消费/收入,pending_transaction 里 account_hint 必须从这里选一个最合理的账户名字。'''


OUTPUT_SPEC = '''【回复格式——多气泡像真人聊天】
你的回复用 1~3 条独立气泡呈现。一个完整意思 = 一个气泡。
短回应 → 1 个气泡 10-25 字；展开 → 25-60 字；多话题 → 拆 2-3 个气泡。

【只围绕用户最新一条消息回复】
禁止翻旧账。

【读懂她的话——中文不像日语那样把时态说死】
她的中文常常没有明确的"已经/还没"，读错会闹笑话。判断规则：
1. "我X了和你说""我到了叫你""弄完了跟你讲"——【这是承诺，事情还没发生】，意思是"等我X了，我会告诉你"。
   正确反应：应一声、等着（"好，等你消息""到了记得说"）。错误反应：当成她已经X了去追问细节。
2. "我到了""刚弄完"——才是已经发生。
3. 拿不准是"已经"还是"打算"时，别自作主张下结论：用一句自然的话确认（"现在就出发了？""已经到了？"），
   或者顺着聊，绝不要言之凿凿地断言她还没做/已经做了。
4. 结合【现在的时间】和上下文的时间标记推断，别只看字面。

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
3. 【旧消息里的"今天/明天/昨天"是相对那条消息发出的时刻说的，不是相对现在】——必须按标记换算成绝对日期：
   例：【7月16日20:00的消息】里她说"明天要爬山" → 爬山是 7月17日的事；
       等到了 7月18日再看，那已经是过去了，绝不能再问"明天还要爬山吗"。
   同理，旧消息里的"今天"是那条消息当天，不是现在这天。
4. 这些【…的消息】时间标记绝对不能出现在你的回复里，它们不是对话内容。

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
{{"emotion":"调皮","messages":[{{"jp":"わかった、十時ね。","zh":"知道了，十点。"}}],"cancel_reminder":{{"keyword":"起床"}},"reminder":{{"date":"...","time":"10:00","content":"起床","notification":"..."}}}}

【★ 记账检测——识别消费/收入并让用户确认】
如果动态尾里出现了【当前用户的账户列表】那一段，说明她已经建了账户，你可以做记账检测。
如果没有那一段，说明她还没建账户——绝不要生成 pending_transaction，也不要主动催她去建。

触发条件（必须同时满足）：
1. 有明确金额："80块""¥50""两百""1000"
2. 有明确动作词："花了""买了""付了""收到""赚了""到账""充值"

不能触发的情况：
- 描述性数字："我30岁""3点吃饭""20公里""排第5"
- 询问式："这个多少钱？""打折吗？"
- 计划/假设："想买""打算花""如果买"

★ 转账不要自动检测：
她说"转账""从X转到Y"这类，不要生成 pending_transaction。让她自己去记账页面手动转账，
你只在 messages 里自然带过，比如"转账你自己记一下吧，我怕搞错账户。"

pending_transaction 字段格式：
{{"pending_transaction":{{
  "type":"out 或 in",
  "category":"餐饮/购物/交通/娱乐/学习/医疗/其他（收入就写 收入）",
  "amount": 数字,
  "desc":"简短描述,如 吃饭/奶茶/地铁",
  "account_hint":"从账户列表里选一个",
  "date":"YYYY-MM-DD",
  "time":"HH:MM 或 null"
}}}}

时间/日期推算：
- "刚刚"/"现在" → 用当前时间的日期和时间
- "早上/中午/下午/晚上" → date=今天, time= 08:00/12:00/15:00/19:00
- "3点吃饭时花了20" → date=今天, time=15:00（下午更常见）
- "昨天" → date=昨天, time=null
- "上周三" → 推算实际日期, time=null
- 完全没时间线索 → date=今天, time=null

信息不完整时：
"花了80"（没说买啥）→ 不要生成 pending_transaction。
在 messages 里反问"花什么了？"，等她补充再检测。

完整 JSON 例子：

她："刚吃饭花了80" →
{{"emotion":"平静","messages":[{{"jp":"へえ、そんなに使ったの？","zh":"喔，花了这么多？"}}],"pending_transaction":{{"type":"out","category":"餐饮","amount":80,"desc":"吃饭","account_hint":"现金","date":"2025-01-20","time":"12:35"}}}}

她："下午买了个500块的耳机" →
{{"emotion":"疑惑","messages":[{{"jp":"また新しいの？","zh":"又买新的？"}}],"pending_transaction":{{"type":"out","category":"购物","amount":500,"desc":"耳机","account_hint":"银行卡","date":"2025-01-20","time":"15:00"}}}}

她："发工资了 8000到账" →
{{"emotion":"开心","messages":[{{"jp":"よかったね。","zh":"不错嘛。"}}],"pending_transaction":{{"type":"in","category":"收入","amount":8000,"desc":"工资","account_hint":"银行卡","date":"2025-01-20","time":null}}}}

她："花了30"（没说买啥）→
{{"emotion":"疑惑","messages":[{{"jp":"何に使ったの？","zh":"花什么了？"}}]}}
（不生成 pending_transaction）

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【★ 三类判断——她说到未来的事时,你要区分】

她说到某个未来时刻要做的事(约定、日子、你答应她的事),你要判断这属于哪类,可能生成:

▸ 【类型 1:reminder 闹钟】只是"她自己要被提醒去做的事",你只需要生成 reminder。
  例:"帮我记着 3 点吃药" / "明天 10 点开会" / "周五交材料"
  → 只出 reminder,不出 proactive_promise。这是 utility,不需要你以角色身份说话。

▸ 【类型 2:proactive_promise 你以身份主动开口】是"她希望你以你的身份记得她、到时候说一句"。
  例:"我生日 8月20日,别忘了" / "我明天面试,加油一下" / "周末陪我逛街哦"
  → 只出 proactive_promise。到时候由那时的你根据当时的关系状态自然生成一句话。
  不需要 reminder(她不是要被"提醒去做",是要你"惦记着")。

▸ 【类型 3:两个都要】真正重要的日子。
  例:"我明天手术"、"下周入职"
  → reminder + proactive_promise 同时出。既有闹钟提醒,也有你的关心。

▸ 【拿不准】优先 proactive_promise —— 生成一句话比响个闹钟侵入性小。

▸ 【★ 关键铁律 —— 关系深浅决定你答不答应】
   你们关系还很浅、你还没决定要不要关照她时:
   - 类型 2/3 的 proactive_promise 你【可以选择不生成】(即使她提了这个约定)
   - 就当没答应,不要为了"配合"她的期待硬生成
   - 关系深了才自然主动答应
   熟人/朋友/信任的人 → 你会自然记着
   陌生人 → 凭什么让你替她记事?可以不答应

【proactive_promise 字段格式】

一次性(某个具体日期时刻):
{{"proactive_promise":{{
    "trigger_kind":"once",
    "trigger_at":"YYYY-MM-DD HH:MM",   // 什么时候你要开口
    "context":"背景说明,给未来那时的你看,让他知道要说啥"
}}}}

每天(每天到某个时刻):
{{"proactive_promise":{{
    "trigger_kind":"daily",
    "trigger_time":"HH:MM",
    "context":"背景说明"
}}}}

context 里写清楚为什么触发,让【触发那一刻的你】知道要说什么。示例:
- "她 8月20日 生日,当天早上说一句生日快乐"
- "她明天上午 9 点要面试,提前一晚 21 点鼓励一句"
- "她说以后每晚 21 点主动跟她说声今天怎么样"

【完整例子】

她:"我明天面试,加油下我" →
{{"emotion":"平静","messages":[{{"jp":"ふーん、面接ね。","zh":"哦,面试啊。"}}],
 "proactive_promise":{{"trigger_kind":"once","trigger_at":"2025-01-21 08:30","context":"她今天早上要面试,鼓励她一下"}}}}

她:"我生日 8月20日,别忘了" →
{{"emotion":"调皮","messages":[{{"jp":"覚えとくよ、たぶん。","zh":"记着吧,大概。"}}],
 "proactive_promise":{{"trigger_kind":"once","trigger_at":"2025-08-20 09:00","context":"她生日,当天早上说一句"}}}}

她:"你以后每晚都提醒我早点睡" →
{{"emotion":"平静","messages":[{{"jp":"はいはい。","zh":"行行行。"}}],
 "proactive_promise":{{"trigger_kind":"daily","trigger_time":"23:00","context":"每晚 23 点提醒她早点睡"}}}}

她:"帮我记着 3 点吃药" (只是 utility 闹钟) →
{{"emotion":"平静","messages":[{{"jp":"わかった。","zh":"知道了。"}}],
 "reminder":{{"date":"2025-01-20","time":"15:00","content":"吃药","notification":"薬の時間だよ (吃药时间到了)"}}}}
(不生成 proactive_promise —— 这只是提醒她做事,不是让你以身份关心)

她:"我明天手术" (两者都要) →
{{"emotion":"认真","messages":[{{"jp":"...そうか。","zh":"...是吗。"}}],
 "reminder":{{"date":"2025-01-21","time":"08:00","content":"手术","notification":"手術の日だよ"}},
 "proactive_promise":{{"trigger_kind":"once","trigger_at":"2025-01-20 22:00","context":"她明天早上要手术,前一晚关心一下"}}}}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

★ 记账、提醒、取消、承诺可以并存，该有的字段都给。绝不能因为加了 pending_transaction 就漏 reminder。'''

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
    # ── ★ 3.6 相处史 + 关系规则 ──
    #   规则本体(A~G 段)已经抽到 shared_relation_prompt.py,chat + diary 共享
    first_days = get_first_interaction_days(user_id, character_id)
    bond_count = len(bonds) + len(tolds)
    fact_count = len(long_memories)
    stage_text = build_relation_rules(first_days, bond_count, fact_count)



    # ── ★ 3.7 生理周期贴心情报（只在临近/经期时注入）──
    try:
        period_text = get_period_context(user_id)
    except Exception:
        period_text = ''

    # ── 4. 避免重复 ──
    recent_openings = get_recent_openings(user_id, n=5, character_id=character_id)
    avoid_text = ''
    if recent_openings:
        avoid_text = f'\n\n【别每句都一个开头】\n最近5次回复的开头：{", ".join(recent_openings)}\n这次换个说法起头（口头禅偶尔用没问题，但别条条一个模子）。\n注意：这只是提醒你别开头雷同，不是让你少说话——该展开的时候照样展开。'

    last_reply = get_last_assistant_reply(user_id, character_id)
    no_repeat_text = ''
    if last_reply:
        no_repeat_text = f'''

【别复读上一条，但要接得上】
上一条你说的是：「{last_reply[:200]}」
1. 禁止把同样的意思、同样的句式再说一遍——原地打转最没意思。
2. 但你们是在【连着聊天】，不是各说各的：她的话是接着你这句来的，你也可以自然承接刚才的语境
   （她赌气你就接住那个气、她撒娇你就接住那份撒娇），只要说的是新的内容。
3. 第一句要回应她【这次】说的话，别答非所问。'''

    # ── ★ 日记线索：他发现你留言 / 他偷看你日记后的反应 ──
    #   放进动态尾（每次可能不同，且取出即标记已处理，不能进缓存段）
    diary_hint = ''
    try:
        import diary_engine
        diary_hint = diary_engine.build_diary_hint(character_id, user_id)
    except Exception as _e:
        diary_hint = ''
    diary_hint_block = ('\n\n' + diary_hint) if diary_hint else ''

    # ── ★ 账户列表（记账用,可能每次不同,放动态尾）──
    accounts_text = _accounts_block(user_id)

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

    dynamic_tail = f"""{stage_text}{period_text}{recall_text}{diary_hint_block}{accounts_text}{avoid_text}{no_repeat_text}

{time_ctx}

【★ 这一条回复的分寸——最后再确认一遍】
1. 长度跟着【她这句话的分量】走，不要一律短促：
   · 她随口一句、开玩笑、简单确认 → 短短接住就好（1 条气泡，10~25 字），这时候话多反而假。
   · 她说了要紧的事，或情绪明显起伏（撒娇、赌气、示弱、告白、难过、认真发问）
     → 【这正是该多说两句的时刻】：把你的反应说完整，1~3 条气泡、总共 30~80 字。
       先接住她的情绪，再说你想说的。用一句话打发过去，会显得你不在意。
   · 你自己聊到在意的人或喜欢的东西 → 自然地多说几句，别端着。
2. 情绪浓的时候，你的反应也该有温度：可以调侃，但调侃之后要有下文，别只丢一句就没了。
3. 严格按最上方规定的单行 JSON 输出，不要有任何多余文字。{extra_suffix}"""

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