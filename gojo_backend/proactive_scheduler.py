"""主动消息常驻排程 —— 【承诺驱动】版

架构变了:
- 老版本:硬编码每天 00:50 发一条任务汇报,不管什么关系状态
- 新版本:读 proactive_promise 表,只有【真的存在约定】的用户才收到主动消息

具体流程:
1. Scheduler 每 10 分钟醒来一次
2. 查 proactive_promise 表里所有【该触发但没触发过】的活跃承诺
3. 对每条承诺,让 LLM 生成一条 gojo 的话 → 存 proactive_msg + 推送
4. 标记这条承诺"已触发"(一次性 → is_fired=true;每天 → 更新 last_fired_at)

★ 陌生用户没有承诺 → scheduler 静默跳过,不打扰
★ 承诺的生成时机由 LLM 判断当时的关系状态,可能非常冷,也可能非常暖
"""
import threading
import time
from datetime import datetime
from config import CN_TZ, ANTHROPIC_KEY
import anthropic

from characters import get_character
from user_memory import get_bond_memories, save_short_memory, get_short_memory
from character_relations import get_relations_text
import proactive_msg
import db_promise

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

_thread = None
_stop = False


def _now():
    return datetime.now(CN_TZ)


def generate_from_promise(promise, now):
    """根据一条 promise,让 gojo 生成他要说的话。存 proactive_msg + 推送。返回 (msg_id, jp) 或 None。"""
    try:
        character_id = promise['character_id']
        user_id = promise['user_id']
        context = promise['context']
        kind = promise['trigger_kind']
        origin_text = promise.get('origin_text', '')

        char = get_character(character_id)
        if not char:
            print(f'[promise] 角色 {character_id} 不存在,跳过 promise #{promise["id"]}')
            return None
        char_name = char['name']
        voice_id = char.get('voice_id')

        time_str = now.strftime('%Y年%m月%d日 %H:%M')

        # 拉最近对话给 LLM 一点感觉(判断当前关系状态用)
        try:
            shorts = get_short_memory(user_id, 4, character_id)
            recent = '\n'.join(f'{"她" if r=="user" else "我"}：{c}' for r, c in shorts) if shorts else '(最近没聊)'
        except Exception:
            recent = '(拉最近对话失败)'

        # 关系背景
        try:
            bonds = get_bond_memories(user_id, character_id, kind='between', limit=6)
            bond_text = '\n'.join(f'- {b[1]}' for b in bonds) if bonds else '(还没什么共同的事)'
        except Exception:
            bond_text = '(拉共同经历失败)'

        # 角色重要人物表
        relations_block = get_relations_text(character_id)
        relations_intro = (f'\n{relations_block}\n' if relations_block else '')

        # ★ 关键:告诉 LLM 这个是【你之前答应过的事】,现在到点了,你要不要开口说,
        #   完全按你【当前对她的态度】决定。
        prompt = f'''你是{char_name}。现在是 {time_str}。

【★ 触发场景】
之前的对话里,你答应过 / 记下了这件事:
「{context}」
{f"(她当时的原话大致是: 「{origin_text}」)" if origin_text else ""}

现在到了这个时刻,你【可能】要主动开口说点什么。
{relations_intro}
【你们最近聊过什么】
{recent}

【你们之间累计的事】
{bond_text}

【★ 你要判断】
根据当前你对她的真实态度(读上面的记忆),你要不要说、说什么、怎么说:
- 关系深、有感情积累 → 你可能会自然带上关心("加油"、"别紧张")
- 关系还浅、公事化 → 简短提醒一下就好,不越界
- 你对她反感 / 完全陌生 → 你可以选择【什么都不说】,输出 {{"skip": true, "reason": "..."}}
  (关系浅 + 只是普通承诺时,尤其可能选择跳过,或者只说非常冷的一句)

【铁律】
- 你的措辞由【当前记忆里的关系】决定,不是由这条承诺当初的语气决定
- 不要为了"温暖"而暖,不要为了"冷淡"而冷 —— 按此刻真实的你
- 【严禁】"付き合ってやった"这种傲娇陪伴腔,更不要"陪你一会儿"这类
- 不熟就短,别脑补场景细节

【输出格式(严格 JSON,一行)】
如果你决定说 → {{"jp":"日语","zh":"中文","emotion":"情绪"}}
如果决定跳过 → {{"skip": true, "reason": "简要原因"}}
emotion 选: 平静/自信/调皮/认真/温柔/冷淡'''

        resp = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = resp.content[0].text.strip()
        from utils import extract_json
        parsed = extract_json(raw)
        if not parsed:
            print(f'[promise] #{promise["id"]} 解析失败: {raw[:80]}')
            return None

        if parsed.get('skip'):
            reason = parsed.get('reason', '')
            print(f'[promise] #{promise["id"]} gojo 决定跳过: {reason}')
            # 无论 once/daily,都 mark 一下,避免这个 tick 反复触发
            db_promise.mark_fired(promise['id'], now)
            return None

        jp = (parsed.get('jp') or '').strip()
        zh = (parsed.get('zh') or '').strip()
        emotion = parsed.get('emotion', '平静')
        if not jp:
            print(f'[promise] #{promise["id"]} jp 为空,跳过')
            return None

        # 合成语音
        audio_b64 = ''
        try:
            from tts import tts_to_b64
            audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
        except Exception as e:
            print(f'[promise] TTS 出错: {e}')

        # 存 proactive_msg (kind 用 'promise' 区分)
        mid, ts = proactive_msg.add_proactive_msg(
            character_id, user_id, 'promise', jp, zh, emotion, audio_b64, created_at=now
        )
        print(f'[promise] ✅ #{promise["id"]} → msg #{mid}: {jp[:40]}')

        # 也塞短记忆,避免上下文异常
        try:
            save_short_memory(user_id, 'assistant', jp, character_id)
        except Exception as e:
            print(f'[promise] 写 short_memory 跳过: {e}')

        # 推送
        try:
            import push_notify
            push_notify.push_to_user(
                user_id,
                title=char_name,
                body=zh or jp,
                data={'type': 'proactive', 'character_id': character_id, 'promise_id': promise['id']},
            )
        except Exception as e:
            print(f'[promise] 推送跳过: {e}')

        # 标记已触发
        db_promise.mark_fired(promise['id'], now)
        return mid, jp

    except Exception as e:
        print(f'[promise] 生成出错: {e}')
        return None


def _tick():
    """一次扫描,把该触发的 promise 全部处理掉。"""
    now = _now()
    try:
        due = db_promise.get_due_promises(now)
    except Exception as e:
        print(f'[promise] 查 due 出错: {e}')
        return
    if not due:
        return
    print(f'[promise] tick: 有 {len(due)} 条到期')
    for p in due:
        try:
            generate_from_promise(p, now)
        except Exception as e:
            print(f'[promise] 处理 #{p["id"]} 出错: {e}')


def _loop():
    global _stop
    time.sleep(90)  # 启动后稍等,别抢初始化
    while not _stop:
        try:
            _tick()
        except Exception as e:
            print(f'[promise] tick 出错: {e}')
        time.sleep(600)  # 每 10 分钟检查一次


def start_proactive_scheduler():
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print('[promise] 承诺驱动的主动排程已启动')