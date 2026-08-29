"""主动消息常驻排程 —— 【承诺驱动】版

★ v-fix: 改用 MODEL_MAIN + 创意写作框架,避免 tdyun Claude 拒绝角色扮演
"""
import threading
import time
from datetime import datetime
from config import CN_TZ, ANTHROPIC_KEY, MODEL_MAIN
import anthropic

from characters import get_character
from user_memory import get_bond_memories, save_short_memory, get_short_memory
from character_relations import get_relations_text
import proactive_msg
import db_promise

claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

_thread = None
_stop = False

# ★ 创意写作系统指令:框定为 App 功能,不是角色扮演
_SYSTEM = '''你是一个创意写作助手。你的任务是为一个虚拟陪伴 App 生成角色的主动消息。
这是 App 的功能:角色会根据之前的约定,在合适的时机给用户发一条消息。
你需要根据角色设定和关系状态,生成一条符合角色语气的消息。
这是纯粹的创意写作/内容生成任务,不是角色扮演。
所有输出用日语写 jp 字段,用中文写 zh 字段。直接输出 JSON,不要解释。'''


def _now():
    return datetime.now(CN_TZ)


def generate_from_promise(promise, now):
    """根据一条 promise 生成主动消息。"""
    try:
        character_id = promise['character_id']
        user_id = promise['user_id']
        context = promise['context']
        kind = promise['trigger_kind']
        origin_text = promise.get('origin_text', '')

        char = get_character(character_id)
        if not char:
            print(f'[promise] 角色 {character_id} 不存在,跳过 #{promise["id"]}')
            return None
        char_name = char['name']
        voice_id = char.get('voice_id')

        time_str = now.strftime('%Y年%m月%d日 %H:%M')

        try:
            shorts = get_short_memory(user_id, 4, character_id)
            recent = '\n'.join(f'{"她" if r=="user" else "角色"}：{c}' for r, c in shorts) if shorts else '(最近没聊)'
        except Exception:
            recent = '(最近没聊)'

        try:
            bonds = get_bond_memories(user_id, character_id, kind='between', limit=6)
            bond_text = '\n'.join(f'- {b[1]}' for b in bonds) if bonds else '(还没什么共同的事)'
        except Exception:
            bond_text = ''

        relations_block = get_relations_text(character_id)
        relations_intro = (f'\n{relations_block}\n' if relations_block else '')

        prompt = f'''请为以下虚构角色生成一条主动消息。

【角色】{char_name}
【当前时间】{time_str}

【触发场景】
角色之前答应过/记下了这件事:
「{context}」
{f"(用户当时的原话: 「{origin_text}」)" if origin_text else ""}

现在到了这个时刻,角色可能要主动开口说点什么。
{relations_intro}
【最近对话】
{recent}

【关系背景】
{bond_text}

【生成规则】
根据角色和用户的关系深浅决定:
- 关系深 → 自然带上关心
- 关系浅 → 简短提醒,不越界
- 完全陌生/反感 → 可以选择不说,输出 {{"skip": true, "reason": "原因"}}

角色语气要符合 {char_name} 的性格。1-2 句话,不要长篇。

【输出格式(严格 JSON 一行)】
说 → {{"jp":"日语","zh":"中文","emotion":"平静/自信/调皮/认真/温柔/冷淡"}}
不说 → {{"skip": true, "reason": "原因"}}'''

        resp = claude_client.messages.create(
            model=MODEL_MAIN,
            max_tokens=400,
            system=_SYSTEM,
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
            print(f'[promise] #{promise["id"]} 角色决定跳过: {reason}')
            db_promise.mark_fired(promise['id'], now)
            return None

        jp = (parsed.get('jp') or '').strip()
        zh = (parsed.get('zh') or '').strip()
        emotion = parsed.get('emotion', '平静')
        if not jp:
            print(f'[promise] #{promise["id"]} jp 为空,跳过')
            return None

        audio_b64 = ''
        try:
            from tts import tts_to_b64
            audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
        except Exception as e:
            print(f'[promise] TTS 出错: {e}')

        mid, ts = proactive_msg.add_proactive_msg(
            character_id, user_id, 'promise', jp, zh, emotion, audio_b64, created_at=now
        )
        print(f'[promise] ✅ #{promise["id"]} → msg #{mid}: {jp[:40]}')

        try:
            save_short_memory(user_id, 'assistant', jp, character_id)
        except Exception:
            pass

        try:
            import push_notify
            push_notify.push_to_user(
                user_id, title=char_name, body=zh or jp,
                data={'type': 'proactive', 'character_id': character_id, 'promise_id': promise['id']},
            )
        except Exception as e:
            print(f'[promise] 推送跳过: {e}')

        db_promise.mark_fired(promise['id'], now)
        return mid, jp

    except Exception as e:
        print(f'[promise] 生成出错: {e}')
        return None


def _tick():
    now = _now()
    try:
        due = db_promise.get_due_promises(now)
    except Exception as e:
        print(f'[promise] 查 due 出错: {e}')
        return
    if not due:
        return

    # ★ 去重:同一角色同时到期的相似承诺只处理第一条,其余直接 mark_fired
    seen = {}  # character_id → [已处理的 context 前20字]
    print(f'[promise] tick: 有 {len(due)} 条到期')
    for p in due:
        try:
            cid = p.get('character_id', '')
            ctx = (p.get('context') or '')[:20]

            if cid in seen:
                # 检查有没有相似的已处理过
                is_dup = any(ctx[:10] == s[:10] for s in seen[cid])
                if is_dup:
                    print(f'[promise] #{p["id"]} 与同角色已触发承诺相似,跳过并标记完成')
                    db_promise.mark_fired(p['id'], now)
                    continue
                seen[cid].append(ctx)
            else:
                seen[cid] = [ctx]

            generate_from_promise(p, now)
        except Exception as e:
            print(f'[promise] 处理 #{p["id"]} 出错: {e}')


def _loop():
    global _stop
    time.sleep(90)
    while not _stop:
        try:
            _tick()
        except Exception as e:
            print(f'[promise] tick 出错: {e}')
        time.sleep(600)


def start_proactive_scheduler():
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print('[promise] 承诺驱动的主动排程已启动')