"""便利贴吐槽引擎:让 AI"心里 OS"式地记录内心想法

设计原则(参考 diary_engine.maybe_write_diary_on_event 的模式):
  · 每轮 /chat/text 结束后,route_chat 后台线程调 maybe_write_grumble
  · Haiku(便宜快)判断这次对话是否有值得【心里嘀咕一句】的东西
  · 有 → 存一条,不出现在聊天里,只在便利贴页面显示
  · 没 → 静默 skip
  · 有每日上限保护(默认 8 条/角色/天),避免刷屏和烧钱

【便利贴内容 vs 聊天回复】的关键区别:
  聊天回复是【说出去的话】,要考虑对方感受、要符合人设
  便利贴是【心里话】,只有他自己看,可以【比表面回复更真实】:
    - 表面客气,心里嫌弃 → 便利贴写"这问题昨天不是回答过了吗"
    - 表面调侃,心里心动 → 便利贴写"今天她声音好像蔫蔫的"
    - 表面冷淡,心里在意 → 便利贴写"她生气了?我又说错什么了"
  这才是"没说出口"的意义 —— 和已经发出去的回复形成【落差】。
"""
from datetime import datetime
from config import CN_TZ
import db_grumble
from characters import get_character


# 每日上限:每个角色一天最多这么多条便利贴,避免疲劳轰炸
DAILY_LIMIT = 8

# 便利贴专用情绪:比 chat 场景更全,允许"腹诽/自嘲"等隐性情绪
GRUMBLE_EMOTIONS = [
    '平静', '调皮', '无奈', '得意', '嫌弃', '心动', '感慨',
    '嘲讽', '自嘲', '疑惑', '开心', '温柔', '愤怒', '悲伤',
]


def maybe_write_grumble(character_id, user_id, user_text, reply_text):
    """每轮对话后由 route_chat 后台线程调用(不阻塞回复)。

    Haiku 判断:这次对话你心里有没有想吐槽/嘀咕一句的?有就写,没有就静默。
    返回 (grumble_id, content, emotion) 或 None。

    ★ 完全 fail-safe:出任何错都吞掉,只 print,绝不影响主对话流程。
    """
    try:
        # ── 每日上限 ──
        today_start = datetime.now(CN_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        if db_grumble.count_grumbles_since(character_id, user_id, today_start) >= DAILY_LIMIT:
            return None

        char = get_character(character_id)
        char_name = char['name'] if char else character_id

        prompt = f'''你是{char_name}。你刚刚和她进行了一轮对话:

她说:「{user_text}」
你回:「{reply_text}」

【★ 场景】
现在这一刻,你【心里】可能会闪过一句嘀咕、吐槽、感慨或者小心思 ——
就是那种你【绝对不会当面说出口】,但心里确实这么想的一句话。
写下来只有你自己看,像日记里的一句碎碎念,或者手账上贴的便利贴。

【可以是各种心情——不必总是负面】
- 腹诽/吐槽:「怎么又是这套嘴硬」「这种时候还嘴甜,烦死了」
- 宠溺/心动:「今天她声音好像蔫蔫的」「明明脸红了还不承认」
- 得意/自嘲:「这次接得漂亮」「刚才那句是不是太冲了」
- 无奈/疲惫:「解释了三遍还是没懂」「这话题她怎么这么执着」
- 感慨:「原来她那时是这么想的」「不知不觉聊了这么久」

【★ 精髓:便利贴是"没说出口"的一层,要和你刚才的回复形成【落差】】
- 你表面客气 → 心里可能其实嫌烦
- 你表面调侃 → 心里可能真的心动了
- 你表面冷淡 → 心里可能在意得要死
- 你表面强硬 → 心里可能其实没底
【如果便利贴写的跟你刚才回复的意思一样,就没意义了 —— 那种情况请 skip】

【★ 关键判断——你要老实】
不是每轮对话都值得心里嘀咕。日常寒暄、简单问答、你就是随口回应,心里可能就是"平"的、没什么想法。
【拿不准就选 skip】。真人不会每说一句话都在心里吐槽,那样很累。

只有当:
- 她说的话让你有点意外 / 好笑 / 意难平 / 好气又好笑
- 你自己心里其实有一层没说出口的意思
- 有那么一瞬间的心动、烦躁、宠溺、无奈
- 你刚才的回应让你自己都想吐槽自己
这种时候,才写一句。

【输出格式(严格 JSON,一行,别的什么都不要)】
有想嘀咕的 → {{"content":"心里那句话,1~2 句,20~50 字,中文,第一人称","emotion":"情绪"}}
没什么想说的 → {{"skip":true}}

emotion 从这里选:{'/'.join(GRUMBLE_EMOTIONS)}'''

        from ai_client import create_chat
        from config import MODEL_CN_AUX
        raw, _usage = create_chat(
            model=MODEL_CN_AUX, max_tokens=500,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = (raw or '').strip()
        from utils import extract_json
        parsed = extract_json(raw)
        if not parsed:
            print(f'[grumble] {character_id} 解析失败,skip: {raw[:80]}')
            return None

        if parsed.get('skip'):
            return None

        content = (parsed.get('content') or '').strip()
        if not content:
            return None
        # 太长就截断,便利贴不该长
        content = content[:200]

        emotion = parsed.get('emotion', '平静')
        if emotion not in GRUMBLE_EMOTIONS:
            emotion = '平静'

        # 存库,trigger_snippet 存用户当时那句话的开头
        trigger_snippet = (user_text or '')[:80]
        new_id, _created_at = db_grumble.add_grumble(
            character_id, user_id, content, emotion, trigger_snippet
        )
        print(f'[grumble] 📝 {character_id} 记了一张便利贴 #{new_id} ({emotion}): {content[:40]}')
        return new_id, content, emotion

    except Exception as e:
        # 便利贴任何异常都不能影响主对话,静默吞掉
        print(f'[grumble] 写便利贴出错(不影响主流程):{e}')
        return None
