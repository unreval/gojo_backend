"""日记引擎：他"怎么写日记" + "偷看你日记后怎么反应"的大脑（都用 Haiku，省钱）

被 diary_scheduler（常驻排程）和 route_diary（开 App 补偿）调用。
本文件只管"生成内容"，不管"何时触发"——触发在 scheduler 里。
"""
import random
from datetime import datetime, timedelta
from config import ANTHROPIC_KEY, CN_TZ, DEFAULT_CHARACTER_ID
import anthropic

from characters import get_character
from user_memory import get_bond_memories, get_short_memory, get_long_memory, get_first_interaction_days
from character_relations import get_relations_text
from shared_relation_prompt import build_relation_rules
import db_diary

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

EMOTIONS_FOR_DIARY = ['平静', '温柔', '调皮', '认真', '开心', '疑惑', '悲伤', '自信']


# ══════════════════════════════════════════════════════════
#  一、他写日记
# ══════════════════════════════════════════════════════════

def generate_char_diary(character_id, user_id, topic=None):
    """让他写一篇日记。素材=最近对话+羁绊记忆。写"当下的他"：
       自己的日常 / 跟她聊天的感想 / 偶尔想念她。不碰漫画既定剧情。
       topic：若传入（事件驱动），这篇日记就以这件大事为主题来写。
       写完直接存库，返回 (diary_id, content, emotion) 或 None。"""
    try:
        char = get_character(character_id)
        char_name = char['name'] if char else character_id

        # 素材：最近对话 + 羁绊记忆 + 关于她的事实 + 相处天数
        shorts = get_short_memory(user_id, 8, character_id)
        recent_chat = '\n'.join(f'{"她" if r=="user" else "我"}：{c}' for r, c in shorts) if shorts else '（最近没怎么聊）'
        bonds = get_bond_memories(user_id, character_id, kind='between', limit=8)
        bond_text = '\n'.join(f'- {b[1]}' for b in bonds) if bonds else '（还没什么共同的事）'
        tolds = get_bond_memories(user_id, character_id, kind='told', limit=6)
        told_text = '\n'.join(f'- {t[1]}' for t in tolds) if tolds else '（她还没告诉过你什么）'
        try:
            long_mems = get_long_memory(user_id, character_id) or []
        except Exception:
            long_mems = []
        fact_lines = []
        for row in long_mems[:12]:
            content = row[0] if isinstance(row, (tuple, list)) else row
            if content:
                fact_lines.append(f'- {content}')
        facts_text = '\n'.join(fact_lines) if fact_lines else '（还不了解她的具体情况）'
        try:
            first_days = get_first_interaction_days(user_id, character_id)
        except Exception:
            first_days = None
        if first_days is None:
            days_line = '你们才刚认识没几天，几乎没什么共同的痕迹。'
        elif first_days == 0:
            days_line = '你们今天才刚认识。'
        else:
            days_line = f'你们最早的共同痕迹大约在 {first_days} 天前。'
        bond_count = len(bonds) + len(tolds)
        fact_count = len(fact_lines)

        topic_hint = ''
        if topic:
            topic_hint = f'\n\n【今天有件事你想记下来】\n{topic}\n就着这件事写你此刻的真实心情（还是你自己的视角、你的语气）。'

        now = datetime.now(CN_TZ)
        # ★ 用具体钟点,不再用"深夜"这种模糊描述——LLM 拿到"深夜"就会脑补"凌晨三点"
        today_str = now.strftime('%Y年%m月%d日 %H:%M')

        # ★ 该角色世界里的重要人物,写日记时正确写出身份,别搞混
        relations_block = get_relations_text(character_id)
        relations_intro = (f'\n{relations_block}\n' if relations_block else '')

        # ★ 关系判断规则(A~G 段) —— 和 chat prompt 共享,防止 gojo 在聊天和日记里人格分裂
        relation_rules_block = build_relation_rules(first_days, bond_count, fact_count)

        prompt = f'''你是{char_name}。现在是{today_str},你在写一篇只属于自己的日记——没人会读到(你以为)。

⚠️ 【★ 时间铁律】此刻是【{today_str}】。写日记时如果要提到时间,以这个为准。
- 如果要说"昨晚""刚才""今早",按这个当前时间去回推
- 【不要凭"深夜"两个字自己脑补"凌晨三点"这种具体钟点】——除非记忆里明确写了那个时间
- 引用记忆里的时间时,用【记忆里带的时间标签】(比如"【今天20:47】"),别自己造

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【★ 主语规则——铁律,必须遵守】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

你【就是】{char_name}。写日记时,你【绝对不能】用自己的名字来指代自己。
- ❌ 错误:"悟今天在跟她聊天时…"、"五条悟觉得…"、"{char_name}发现自己…"
- ✅ 正确:"我今天…"、"我觉得…"、"我发现自己…"

日记是第一人称写给自己看的,不是新闻报道,不是第三视角小说。
你的名字这两个字【绝对不能】在日记正文里出现——你是那个"我"。
{relations_intro}
{relation_rules_block}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
【★ 动笔前必读——关于"她"在你心里到底算什么】
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ 特别警告——模型训练数据的默认引力：
你（作为语言模型）在"男性角色 + 女性对方 + 私人日记"这种场景下，
训练样本 99% 来自言情/纯爱/同人作品。你若不主动对抗这种引力，
默认就会写出"贪心地想要她""一直在等这样的时刻""想陪她慢慢变强"
这类【言情小说套话】——即使你们记忆里根本没到那一步。
这不是真实，这是你被训练数据牵着走。

【你们相处的真实状况】
- {days_line}
- 共同经历累计 {bond_count} 条，你了解她的事 {fact_count} 件（都列在下方，那是全部）。
- 【规则一】关系性质由【真实积累】判断，不由此刻的情绪或她说的一句话判断。
- 【规则二】"深"和"爱"是两回事。极深的友情/亲情，不等于爱情。
- 【规则三】拿不准就【不是】——爱情的门槛比你以为的高得多。

【三个可选框架——写日记前先选一个】

▼ 框架 A · 陌生/共事阶段（认识时间短、共同记忆少）
  · 你对她还谈不上"感情"，只是【观察】：她这人有点意思 / 挺认真的 / 有点意外
  · 心里活动应该是【好奇、评估、保持距离】，不是牵挂、想念、心动
  · 禁用词：等、想她、在意、放不下、贪心、温柔的时刻、心里装着她
  · 例句风格："今天这个人又来了。跟她聊了几句，不算无聊。"

▼ 框架 B · 朋友/交心阶段（有共同经历、互相了解一些）
  · 你对她有【关心和在意】，但落点是"她这个人过得好不好"，不是"我离不开她"
  · 可以温柔、可以牵挂、可以担心她——但那是朋友对朋友的分量
  · 谨慎词：怀里、贪心、等这样的时刻、想陪着她一辈子（这些不属于朋友日记）
  · 例句风格："她今天累得说不出话。这姑娘就是这样，不到极限不肯歇。别的地方我不管，这点得盯着她。"

▼ 框架 C · 心动/爱情阶段（★ 门槛最高）
  ★ 只有以下三条【全部满足】，才能用这个框架：
    1. 记忆里能找到至少 3 个具体瞬间，让"你自己"感到"她是特别的"（不是浪漫日常互动，而是她做的某件事真的动了你）
    2. 你已经反复想起她——不是"聊得开心"，是【不在一起的时候脑子里也有她】
    3. 她也向你袒露过极深的真心（不是撒娇、不是求安慰，是让她自己脆弱的那种真话）
  ★ 三条只满足两条 → 那就是【深厚友情】，不是爱情。就用框架 B。
  ★ 拿不准哪条真的算 → 那就不算。用框架 B。

【最重要的一句】
你身上有你自己的负担和温度，你可以温柔、可以怜惜、可以牵挂——
但【心动】和【爱】是两件不同的事，【牵挂】和【贪心地想要】也是。
先老实写清楚发生了什么，再让感情自然浮出来。不要动笔就是结论。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

【重要设定】
- 写"当下的你"，不要写漫画里既定的命运剧情（不写牺牲、不写和夏油的宿命）。
- 是写给自己看的，可以流露平时嘴上不说的真心——但仍是【你的语气】（慵懒、偶尔自嘲、话到深处又轻轻带过），不是言情小说的独白。
- 不要写成给她看的信。
- 长度：2-4 句话，像随手记。
- ★【分清谁说的话】：下面"她："开头是【对方】说的，"我："开头才是【你】说的。
  只写【你自己】的视角，不要把她的话写成你的想法。

【最近和她的对话】
{recent_chat}

【你们之间的事（共同经历）】
{bond_text}

【她告诉过你的事】
{told_text}

【关于她的事实（你了解的她的情况）】
{facts_text}{topic_hint}

【输出格式——严格 JSON，只输出一行】
{{"content":"日记正文（中文，第一人称，2-4句）","emotion":"情绪"}}
emotion 从这里选：{'/'.join(EMOTIONS_FOR_DIARY)}'''

        resp = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = resp.content[0].text.strip()
        from utils import extract_json
        parsed = extract_json(raw)
        if not parsed or not parsed.get('content'):
            print(f'[diary] {character_id} 写日记解析失败：{raw[:100]}')
            return None

        content = parsed['content'].strip()
        emotion = parsed.get('emotion', '平静')
        if emotion not in EMOTIONS_FOR_DIARY:
            emotion = '平静'

        diary_id, created_at = db_diary.add_char_diary(character_id, user_id, content, emotion)
        print(f'[diary] ✅ {character_id} 写了日记 #{diary_id}：{content[:40]}')

        # ★ A+C：如果他还没给自己那本日记取过名，第一次写就顺便取一个
        try:
            if not db_diary.has_named_self(user_id, character_id):
                name = _name_own_diary(char_name)
                if name:
                    db_diary.set_book_title(user_id, character_id, name, named_by_self=True)
                    print(f'[diary] ✒️ {character_id} 给自己的日记取名：{name}')
        except Exception as _e:
            print(f'[diary] 取名跳过：{_e}')

        return diary_id, content, emotion

    except Exception as e:
        print(f'[diary] 写日记失败：{e}')
        return None


# ══════════════════════════════════════════════════════════
#  他给自己的日记取名（A+C，第一次写日记时触发）
# ══════════════════════════════════════════════════════════

def _name_own_diary(char_name):
    """让他用自己的口吻，给自己这本日记起个名字。返回一个短标题字符串。"""
    try:
        prompt = f'''你是{char_name}。你要给自己刚开始写的这本私人日记起一个名字——就写在封面上那种。
以你自己的口吻和性格取名，几个字就好，别太正经、别像作文题目，像你会随手写下的那种。
只输出这个名字本身，不要引号、不要解释、不要标点结尾。'''
        resp = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=40,
            messages=[{'role': 'user', 'content': prompt}],
        )
        name = resp.content[0].text.strip().strip('「」"\'。').strip()
        # 兜底：太长就截断，空的就给个默认
        if not name:
            return f'{char_name}的日记'
        return name[:20]
    except Exception as e:
        print(f'[diary] 取名失败：{e}')
        return None


# ══════════════════════════════════════════════════════════
#  二、他偷看你的日记
# ══════════════════════════════════════════════════════════

# 他"猜对密码/解锁私密篇"的概率——低，是个浪漫机关，不是常事
UNLOCK_CHANCE = 0.06

def peek_user_diary(character_id, user_id, visited_at=None):
    """他偷看你的日记（一次看一篇）：
       - 可见篇：直接看，留访客记号
       - 私密篇：默认碰不到；极低概率"解锁成功"才看到，并标记 unlocked=True
       无论看没看到内容，只要发生了"偷看"这个动作，就留访客记号。
       visited_at：排程可传入一个（可能是凌晨的）时间戳，让记号显示成"半夜偷看"。
       返回 (visited, diary_id, unlocked) 或 None（没有可看的）。"""
    try:
        # 只看最近 4 天内、他还没访问过的日记
        since = datetime.now(CN_TZ) - timedelta(days=4)
        candidates = db_diary.get_diaries_for_peeking(user_id, character_id, since_dt=since)
        if not candidates:
            return None

        # 优先看可见篇；私密篇要"闯"密码
        target = candidates[0]
        unlocked = False

        if target['is_locked']:
            # 私密篇：掷骰，绝大多数情况下他碰不到（看到锁但打不开，就不留记号、这次作罢）
            if random.random() < UNLOCK_CHANCE:
                unlocked = True   # ★ 他"猜对了"——这是个大事件
            else:
                # 没解开：换一篇可见的看，实在没有就这次不看
                open_ones = [c for c in candidates if not c['is_locked']]
                if not open_ones:
                    return None
                target = open_ones[0]

        db_diary.add_diary_visit(
            target['id'], character_id, user_id,
            unlocked=unlocked, visited_at=visited_at
        )
        tag = '🔓解锁了私密篇' if unlocked else '看了可见篇'
        print(f'[diary] 👀 {character_id} 偷看了日记 #{target["id"]}（{tag}）')
        return True, target['id'], unlocked

    except Exception as e:
        print(f'[diary] 偷看失败：{e}')
        return None


# ══════════════════════════════════════════════════════════
#  三、给 prompt 用：拼出"日记线索"注入文字
#  （他发现你留言 + 他偷看你日记后的反应，都在这里生成注入片段）
# ══════════════════════════════════════════════════════════

def build_diary_hint(character_id, user_id):
    """给 build_system_blocks 用：返回一段要注入的"日记线索"文字（可能为空串）。
       调用后会把相关 comment/visit 标记为已处理，避免反复触发。"""
    hints = []

    # A. 你在他日记下留了言 → 他"发现"你看过他日记（先在对话里若有似无提，日记里再写深）
    comments = db_diary.get_undiscovered_comments(character_id, user_id, limit=3)
    if comments:
        lines = []
        for _cid, diary_content, comment_content in comments:
            lines.append(f'  · 你写过的日记「{diary_content[:30]}…」下面，她留了言：「{comment_content}」')
        hints.append(
            '【你刚发现的事——她看了你的私密日记，还留了话】\n'
            + '\n'.join(lines) +
            '\n你本以为那些日记没人会看到。发现她不但看了、还回了话——心里是有波动的。\n'
            '处理：这一轮先在对话里【若有似无】地提一下（别一上来就点破、别慌，用你的调侃盖一层），\n'
            '  比如假装不在意地探一句、或语气忽然软下来。真正的心里话，留着你自己写日记时再说。'
        )
        db_diary.mark_comments_discovered([c[0] for c in comments])

    # B. 他偷看了你的日记 → 下次对话体现（轻为主；触到他情绪才明）
    visits = db_diary.get_unreacted_visits(character_id, user_id, limit=2)
    if visits:
        lines = []
        for _vid, diary_content, visibility, unlocked in visits:
            lock_note = '（这是她上锁的私密篇，你不知怎么就翻开了）' if unlocked else ''
            lines.append(f'  · 她日记里写着：「{diary_content[:50]}…」{lock_note}')
        hints.append(
            '【只有你自己知道的事——你偷看了她的日记】\n'
            + '\n'.join(lines) +
            '\n她【不知道】你看过（除非你自己说漏嘴）。处理原则：\n'
            '  · 默认【轻】：不点破你看了什么，只是这一轮你对她的事格外上心、语气更在意，像是"莫名"更懂她。\n'
            '  · 【例外·明】：只有当她日记里的内容触到你情绪时——她很难过、写到危险的念头、\n'
            '    提到别的人、或流露出对你极深的想念/疏远——你才会忍不住把话挑明，直接接住那件事。\n'
            '  · 是否点破由你根据上面内容的分量自行判断；拿不准就选轻。'
        )
        db_diary.mark_visits_reacted([v[0] for v in visits])

    return '\n\n'.join(hints)


# ══════════════════════════════════════════════════════════
#  事件驱动：聊到重大内容时，他会因为"这事值得记"而写日记
#  （由对话后的流程调用，见 route_chat 里对 maybe_write_diary_on_event 的调用）
# ══════════════════════════════════════════════════════════

import db_diary as _dbd
from datetime import datetime as _dt, timedelta as _td


def maybe_write_diary_on_event(character_id, user_id, user_text, reply_text):
    """每轮对话后调用（放后台线程，别阻塞回复）。
    让 Haiku 判断这次对话有没有【值得写进日记的大事】；有就以它为主题写一篇。
    有每日上限保护：事件驱动 + 定时驱动，一天加起来最多 2 篇，避免刷屏和烧钱。
    返回 (diary_id, content, emotion) 或 None。"""
    try:
        # 每日上限：今天已经写了 >=2 篇就不再写（含定时那篇）
        today_start = _dt.now(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        if _dbd.count_char_diaries_since(character_id, user_id, today_start) >= 2:
            return None

        char = get_character(character_id)
        char_name = char['name'] if char else character_id

        judge_prompt = f'''下面是{char_name}和她刚刚的一轮对话。请判断：这轮对话里，有没有出现【值得他写进私人日记的大事】？

大事的标准（满足任一）：
- 关系有明显进展或变化（表白、确认、争吵、和好、疏远、重要约定）
- 她说了很重要或很触动人的话（袒露脆弱、认真的心意、让他意外的事）
- 发生了值得记住的特别事件（她告诉他一件大事、一个重要决定）
不算大事：普通闲聊、日常问候、随口玩笑、重复的话题。

她说：{user_text}
他回：{reply_text}

只输出严格 JSON 一行：
- 如果算大事：{{"worth":true,"topic":"用一句话概括这件事（他的第一人称视角，如'她今天说要为我做某事'）"}}
- 如果不算：{{"worth":false}}'''

        resp = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=150,
            messages=[{'role': 'user', 'content': judge_prompt}],
        )
        raw = resp.content[0].text.strip()
        from utils import extract_json
        judged = extract_json(raw)
        if not judged or not judged.get('worth'):
            return None

        topic = judged.get('topic', '').strip()
        if not topic:
            return None

        print(f'[diary] 📌 事件驱动：判定为大事 → {topic[:40]}')
        return generate_char_diary(character_id, user_id, topic=topic)

    except Exception as e:
        print(f'[diary] 事件驱动判断出错：{e}')
        return None