"""主动消息常驻排程（第一层：任务汇报）。

现阶段关系＝记录员↔被记录，五条按你们约定的时间（默认 00:50）
主动发一条【任务汇报】——公事性质的报备，不是嘘寒问暖（陌生人阶段不越界）。

★ 生成时会把【真实当前时间】传给模型，让他知道自己是在深夜/白天报备，话才合时宜。
★ 每天最多 1 条任务汇报。以后关系变深，再在这里加"问候/想念"等更亲密的主动（另做）。

仿 diary_scheduler：后台常驻线程，每隔一段时间醒来看"到点没"。
真推送下一轮接；现在生成的消息存进 proactive_msg 表，前端拉 /proactive/pending。
"""
import threading
import time
from datetime import datetime
from config import CN_TZ, ANTHROPIC_KEY
import anthropic

from characters import get_character
from user_memory import get_bond_memories
import proactive_msg

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# 自用阶段：就你一个人、gojo 一个角色。以后扩展改这里。
TARGET_USER = 'user_mofpiyd7442ia7'
REPORT_CHARACTER = 'gojo'

# 约定的汇报时段（按你们聊天记录：半夜 00:50）。到这个点后的一小时内触发一次。
REPORT_HOUR = 0
REPORT_MINUTE = 50

_thread = None
_stop = False


def _now():
    return datetime.now(CN_TZ)


def _today_start():
    n = _now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def generate_task_report(character_id, user_id):
    """让五条生成一条任务汇报，存进 proactive_msg。返回 (id, jp) 或 None。"""
    try:
        char = get_character(character_id)
        char_name = char['name'] if char else character_id
        voice_id = char.get('voice_id') if char else None

        now = _now()
        time_str = now.strftime('%H:%M')
        hour = now.hour
        time_hint = '深夜' if (hour < 5 or hour >= 23) else ('清晨' if hour < 9 else ('白天' if hour < 18 else '晚上'))

        # 关系背景：这一层是"陌生人/共事"，公事公办、不嘘寒问暖
        prompt = f'''你是{char_name}。你和她现在的关系是【工作关系】——她是记录员，负责记录你的任务，你们才刚认识、还不熟。
你们约好：你做完任务后，主动来跟她【报备任务过程】，她记录。

现在是【{time_str}（{time_hint}）】，你刚结束一个任务，来跟她报备。请生成这条主动消息：
- 内容：简短讲一下你这次任务的过程/见闻/结果（你是最强咒术师，任务多是降咒、出勤这类，可以有具体的场面或吐槽）。
- 语气：【公事公办 + 你本来的慵懒调侃】。是"跟记录员报备工作"，【不是】关心她、不嘘寒问暖、不说"早点睡"这类话——你们还不熟，别越界。
- 贴合当前时间：现在是{time_hint}（{time_str}），如果是深夜，可以自然带一句"这个点还来报备"之类，但别去关心她作息。
- 长度：1-2 句，简短自然，像随手发的消息。

只输出严格 JSON 一行：{{"jp":"日语","zh":"中文翻译","emotion":"情绪"}}
emotion 选：平静/自信/调皮/认真'''

        resp = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=300,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = resp.content[0].text.strip()
        from utils import extract_json
        parsed = extract_json(raw)
        if not parsed or not parsed.get('jp'):
            print(f'[proactive] 任务汇报解析失败：{raw[:80]}')
            return None

        jp = parsed['jp'].strip()
        zh = parsed.get('zh', '').strip()
        emotion = parsed.get('emotion', '平静')

        # 合成语音（跟正常消息一致，前端能点重播）
        audio_b64 = ''
        try:
            from tts import tts_to_b64
            audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
        except Exception:
            pass

        mid, ts = proactive_msg.add_proactive_msg(
            character_id, user_id, 'report', jp, zh, emotion, audio_b64, created_at=now
        )
        print(f'[proactive] ✅ {character_id} 主动汇报 #{mid}：{jp[:40]}')

        # ★ 推送到手机（app 关着也能收到）
        try:
            import push_notify
            push_notify.push_to_user(
                user_id,
                title=char_name if char else '五条悟',
                body=zh or jp,   # 通知栏显示中文（没有就日文）
                data={'type': 'proactive', 'character_id': character_id},
            )
        except Exception as _e:
            print(f'[proactive] 推送跳过：{_e}')

        return mid, jp
    except Exception as e:
        print(f'[proactive] 生成任务汇报出错：{e}')
        return None


def _maybe_report():
    """到了约定时段、且今天还没发过，就发一条任务汇报。"""
    now = _now()
    # 只在 [REPORT_HOUR:REPORT_MINUTE, +1 小时) 这个窗口内触发
    target = now.replace(hour=REPORT_HOUR, minute=REPORT_MINUTE, second=0, microsecond=0)
    delta_min = (now - target).total_seconds() / 60
    if not (0 <= delta_min < 60):
        return
    # 今天已发过就不再发
    if proactive_msg.count_reports_since(REPORT_CHARACTER, TARGET_USER, _today_start()) >= 1:
        return
    generate_task_report(REPORT_CHARACTER, TARGET_USER)


def _loop():
    global _stop
    time.sleep(90)  # 启动后稍等，别和其它初始化抢
    while not _stop:
        try:
            _maybe_report()
        except Exception as e:
            print(f'[proactive] tick 出错：{e}')
        time.sleep(600)  # 每 10 分钟检查一次是否到点


def start_proactive_scheduler():
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print('[proactive] 主动汇报排程已启动（每天约定时段发一条任务报备）')