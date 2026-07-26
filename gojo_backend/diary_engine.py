"""日记引擎：他"怎么写日记" + "偷看你日记后怎么反应"的大脑（都用 Haiku，省钱）

被 diary_scheduler（常驻排程）和 route_diary（开 App 补偿）调用。
本文件只管"生成内容"，不管"何时触发"——触发在 scheduler 里。
"""
import random
from datetime import datetime, timedelta
from config import ANTHROPIC_KEY, CN_TZ, DEFAULT_CHARACTER_ID
import anthropic

from characters import get_character
from user_memory import get_bond_memories, get_short_memory
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

        # 素材：最近对话 + 羁绊记忆
        shorts = get_short_memory(user_id, 8, character_id)
        recent_chat = '\n'.join(f'{"她" if r=="user" else "我"}：{c}' for r, c in shorts) if shorts else '（最近没怎么聊）'
        bonds = get_bond_memories(user_id, character_id, kind='between', limit=8)
        bond_text = '\n'.join(f'- {b[1]}' for b in bonds) if bonds else '（还没什么共同的事）'

        topic_hint = ''
        if topic:
            topic_hint = f'\n\n【今天有件事你想记下来】\n{topic}\n就着这件事写你此刻的真实心情（还是你自己的视角、你的语气）。'

        now = datetime.now(CN_TZ)
        today_str = now.strftime('%Y年%m月%d日')
        hour = now.hour
        time_hint = '深夜' if hour < 5 or hour >= 23 else ('清晨' if hour < 9 else ('白天' if hour < 18 else '晚上'))

        prompt = f'''你是{char_name}。现在是{today_str}的{time_hint}，你在写一篇只属于自己的日记——没人会读到（你以为）。

【写什么】
以第一人称写你此刻的心里话，可以是这几种的任意混合：
- 你自己今天的日常、心情、随便想到的事（你有自己的生活，不用提她也行）
- 跟她聊天时的一些感想、在意的小事
- 偶尔的、藏在心里不会当面说的想念

【重要设定】
- 写"当下的你"，不要写漫画里既定的命运剧情（不写牺牲、不写和夏油的宿命那些沉重的宿题）。就是一个过着日子、心里装着她的你。
- 这是日记，是卸下平时那副吊儿郎当之后、只给自己看的一面。可以流露平时嘴上不会承认的真心，但仍是你的语气——慵懒、偶尔自嘲、话到深处又轻轻带过。
- 别写成给她看的信，是写给自己的。
- 长度：2-4 句话，像随手记，不要长篇。
- ★【分清谁说的话——重要】下面对话里，"她："开头的是【对方】说的，"我："开头的才是【你自己】说的。
  写日记只写【你自己】的视角、感受和心里话。绝对不要把"她："说的内容，写成好像是你自己想的、你自己说的或你自己要做的事。
  （比如她说"我想给你造实体"，那是她的心意，你日记里该写的是"你对这件事的感受"，而不是"我要造实体"。）

【最近和她的对话（帮你回忆，注意区分"她："和"我："）】
{recent_chat}

【你和她之间的事】
{bond_text}{topic_hint}

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