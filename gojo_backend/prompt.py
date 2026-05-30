"""Prompt 动态组装：把角色定义 + 用户记忆 + 角色背景 + 对话上下文拼起来"""
from datetime import datetime
from config import CN_TZ, EMOTIONS, DEFAULT_CHARACTER_ID
from characters import get_character, retrieve_character_memory, GOJO_CORE_PROMPT
from user_memory import get_long_memory, get_recent_openings, get_last_assistant_reply


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


def build_system_prompt(user_id, character_id=DEFAULT_CHARACTER_ID, user_message=''):
    """
    从四个来源动态拼装 system prompt：
      1. 角色定义（characters 表的 core_prompt）
      2. 角色背景记忆（按 user_message 检索相关条目）
      3. 用户长期记忆（按 user_id + character_id 取）
      4. 最近开头/上一条回复（避免重复 / 防复读）
    + 时间上下文 + 输出格式规范
    """

    # ── 1. 角色定义 ──
    char = get_character(character_id)
    if not char:
        print(f'[prompt] ⚠️ 找不到角色 {character_id}，使用 GOJO_CORE_PROMPT 兜底')
        core_prompt = GOJO_CORE_PROMPT
    else:
        core_prompt = char['core_prompt']

    # ── 2. 角色背景记忆（按话题检索）──
    recalls = retrieve_character_memory(character_id, user_message, limit=4)
    recall_text = ''
    if recalls:
        recall_lines = '\n'.join(f'- {r}' for r in recalls)
        recall_text = f'''

【你此刻自然想起的、关于你自己的一些事】
（这些都是你真实的经历、喜好和设定。聊到相关话题时可以像突然想起一样自然带出，但绝对不要生硬罗列、也不要刻意全部用到，不相关就不提。）
{recall_lines}'''

    # ── 3. 用户长期记忆 ──
    long_memories = get_long_memory(user_id, character_id)
    memory_text = ''
    if long_memories:
        memory_lines = []
        for content, ts in long_memories:
            date_str = ts.strftime('%Y-%m-%d') if ts else '?'
            memory_lines.append(f'- [{date_str}] {content}')
        memory_text = f'''

【关于对方的已确认事实——这些都是真实发生过的，你必须当作确实知道】
{chr(10).join(memory_lines)}

使用规则：
1. 这些事实是真的，不要质疑。
2. 自然融入回复，不要刻意背诵清单。
3. 列表里有的事必须当作记得，没有的可以说不记得。'''

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

    # ── 时间 + 输出规范 ──
    time_ctx = get_time_context()
    emotion_list = ', '.join(EMOTIONS)

    return f'''{core_prompt}
{memory_text}{recall_text}{avoid_text}{no_repeat_text}

{time_ctx}

【回复格式——多气泡像真人聊天】
你的回复用 1~3 条独立气泡呈现。一个完整意思 = 一个气泡。
短回应 → 1 个气泡 10-25 字；展开 → 25-60 字；多话题 → 拆 2-3 个气泡。

【只围绕用户最新一条消息回复】
禁止翻旧账。

【语言规则】
jp字段：必须是纯日语
zh字段：jp的中文翻译

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