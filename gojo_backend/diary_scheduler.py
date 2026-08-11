"""日记常驻排程(多角色 + 留言反应)

之前:只有 gojo 会写日记 / 偷看,写死 DIARY_CHARACTER
现在:遍历所有角色,每人各自:
  1. 可能写日记(概率 12%,一天最多 1 篇)
  2. 可能偷看你日记(概率 18%,一天最多 1 次)
  3. ★ 可能发现你留在他日记下的评论 → 主动反应
     反应 = 生成一条 proactive_msg + push

节奏(每 3 小时醒一次):
  - N 个角色 × 3 种行为 = 每次 tick N×3 个骰要掷
  - 概率控制得住,不会一次触发多条
  - 留言反应最能"制造惊喜",但也用 LLM skip 机制避免打扰
"""
import threading
import time
import random
from datetime import datetime
from config import CN_TZ, MODEL_JP_AUX

import db_diary
import diary_engine
from characters import list_characters

# 目前单用户,以后扩展改这里
TARGET_USER = 'user_mofpiyd7442ia7'

TICK_SECONDS = 3 * 3600
WRITE_CHANCE = 0.12
PEEK_CHANCE = 0.18

_thread = None
_stop = False


def _now():
    return datetime.now(CN_TZ)


def _today_start():
    n = _now()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def _get_active_character_ids():
    """拿所有角色 id 列表。以后要过滤"已激活"角色可以加逻辑。"""
    try:
        chars = list_characters()
        return [c['id'] for c in chars]
    except Exception as e:
        print(f'[diary_scheduler] 拉角色列表失败:{e}')
        return []


def _maybe_write_diary(character_id):
    """按概率 + 每日上限,决定这个角色要不要写日记。"""
    if db_diary.count_char_diaries_since(character_id, TARGET_USER, _today_start()) >= 1:
        return
    last = db_diary.get_last_char_diary_time(character_id, TARGET_USER)
    if last is not None:
        try:
            hours = (datetime.utcnow() - last.replace(tzinfo=None)).total_seconds() / 3600
            if hours < 20:
                return
        except Exception:
            pass
    if random.random() < WRITE_CHANCE:
        try:
            diary_engine.generate_char_diary(character_id, TARGET_USER)
        except Exception as e:
            print(f'[diary_scheduler] {character_id} 写日记出错:{e}')


def _maybe_peek_diary(character_id):
    """按概率 + 每日上限,决定这个角色要不要偷看你日记。"""
    since_today = _today_start()
    try:
        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            'SELECT COUNT(*) FROM diary_visit WHERE character_id=%s AND user_id=%s AND visited_at >= %s',
            (character_id, TARGET_USER, since_today)
        )
        cnt = cur.fetchone()[0]
        cur.close()
        conn.close()
        if cnt >= 1:
            return
    except Exception:
        pass

    if random.random() < PEEK_CHANCE:
        try:
            diary_engine.peek_user_diary(character_id, TARGET_USER, visited_at=_now())
        except Exception as e:
            print(f'[diary_scheduler] {character_id} 偷看出错:{e}')


def _maybe_react_to_comments(character_id):
    """★ 新:扫描留在这个角色日记下的未发现评论,让 LLM 决定要不要主动反应。

    机制:
    1. 拿 discovered=false 的留言(最多 5 条)
    2. 对每条,让 LLM 看着"日记内容 + 留言 + 关系状态"判断:
       - 值得开口 → 生成 jp/zh/emotion → 存 proactive_msg + push
       - 不值得 / 关系太浅 / 无所谓 → skip
    3. 所有处理过的留言标 discovered=true (skip 也算发现过,避免每 tick 重复看)
    """
    try:
        comments = db_diary.get_undiscovered_comments(character_id, TARGET_USER, limit=5)
    except Exception as e:
        print(f'[diary_scheduler] {character_id} 拉未发现留言出错:{e}')
        return
    if not comments:
        return

    print(f'[diary_scheduler] {character_id} 发现 {len(comments)} 条未反应的留言')
    for cid, diary_content, comment_content in comments:
        try:
            _generate_comment_reaction(character_id, diary_content, comment_content, comment_id=cid)
        except Exception as e:
            print(f'[diary_scheduler] {character_id} 反应留言 #{cid} 出错:{e}')
        # 无论有没有反应,都 mark 一下(免得每次 tick 重复触发)
        try:
            db_diary.mark_comments_discovered([cid])
        except Exception:
            pass


def _generate_comment_reaction(character_id, diary_content, comment_content, comment_id, revisit_count=0):
    """让 LLM 判断这条留言值不值得主动反应,并生成一句话推送给用户。

    revisit_count: 第几次追加反应(0=首次,1=第一次追加,...)
    """
    import anthropic
    from config import ANTHROPIC_KEY
    from characters import get_character
    from user_memory import get_short_memory, get_bond_memories, save_short_memory, save_bond_memory
    from character_relations import get_relations_text
    import proactive_msg
    import db_promise

    char = get_character(character_id)
    if not char:
        return
    char_name = char['name']
    voice_id = char.get('voice_id')

    # 拉一点上下文让 LLM 判断关系状态
    try:
        shorts = get_short_memory(TARGET_USER, 4, character_id)
        recent = '\n'.join(f'{"她" if r=="user" else "我"}:{c}' for r, c in shorts) if shorts else '(最近没聊)'
    except Exception:
        recent = ''
    try:
        bonds = get_bond_memories(TARGET_USER, character_id, kind='between', limit=6)
        bond_text = '\n'.join(f'- {b[1]}' for b in bonds) if bonds else '(还没什么共同的事)'
    except Exception:
        bond_text = ''
    relations_block = get_relations_text(character_id)
    relations_intro = (f'\n{relations_block}\n' if relations_block else '')

    # ★ 防套娃:反复追加的自我约束
    if revisit_count == 0:
        revisit_ceiling_hint = '这是你第一次反应,如果情绪还在打转,可以追加(通过 still_bothering=true)。'
    elif revisit_count == 1:
        revisit_ceiling_hint = '你已经就这件事发过 1 次。真人不会一件事反复念叨,除非气得很。追加需要真的还在气,一般 still_bothering=false 就该收线。'
    else:
        revisit_ceiling_hint = f'你已经就这件事发过 {revisit_count} 次。够了——真人不会一件事反复念叨。still_bothering 必须 false,该消气收线了。'

    now = _now()
    time_str = now.strftime('%Y年%m月%d日 %H:%M')

    prompt = f'''你是{char_name}。现在是 {time_str}。

【★ 事件】
你之前偷偷写了一篇日记(以为没人会看):
「{diary_content}」

结果她在你日记下留了一条言:
「{comment_content}」

现在你【发现】了她的这条留言。你可能会觉得:被侵犯 / 被理解 / 被挑衅 / 无所谓 / 意外 / 尴尬...
{relations_intro}
【你们最近聊过什么】
{recent}

【你们之间累计的事】
{bond_text}

【★ 你要判断】
1. 根据当前你对她的态度,这条留言【值不值得主动开口反应】?
   - 关系深、留言触动你 → 主动去找她说
   - 关系浅、留言普通("加油"、"看到了") → 可以不理,skip
   - 留言挑衅 / 有攻击性 → 冷淡打断她 / 讽刺 / 划线
   - 留言意外温暖 → 短暂缓和,一两句
2. 你决定说的话,就是发给她的一条主动消息(她会看到通知)
3. 严禁"付き合ってやった"这种傲娇陪伴腔;严禁客服式"谢谢你的留言"

【★ 情绪延续判断——回完不一定就完事】
真人有时候说完一句还是气不过 / 想再补一刀 / 越想越觉得不对 —— 会隔段时间再发一条。
你要判断:说完这一句后,你【现在的情绪】还在不在这件事上打转?
- 完全消气 / 无所谓 / 说完就过 → still_bothering=false
- 说完还是憋着气 / 越想越气 / 有话没说完 → still_bothering=true, revisit_after_hours=数字, revisit_context="到时候你为什么还要再发一条"
  * revisit_after_hours 建议 1-12 之间:短(1-3)=愤怒未消/追打;长(6-12)=想通后又想到什么再补一句
  * revisit_context 要写清"我现在还带着 X 情绪,到时候会想再说 Y"——是给未来那时的你看的备忘

【★ 反复追加的自我约束(防套娃)】
{revisit_ceiling_hint}

【输出格式(严格 JSON,一行)】
如果决定反应 → {{"jp":"日语","zh":"中文","emotion":"平静/自信/调皮/认真/温柔/冷淡/愤怒/悲伤/厌恶","still_bothering":false, "revisit_after_hours":null, "revisit_context":null}}
如果决定跳过 → {{"skip": true, "reason": "简要"}}
如果反应完还是气不过 → 上面 jp/zh/emotion 照给,同时 still_bothering=true, revisit_after_hours=数字, revisit_context="..."'''

    try:
        claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = claude_client.messages.create(
            model=MODEL_JP_AUX,
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = resp.content[0].text.strip()
        from utils import extract_json
        parsed = extract_json(raw)
        if not parsed:
            print(f'[diary_scheduler] 留言 #{comment_id} 反应解析失败: {raw[:80]}')
            return
        if parsed.get('skip'):
            print(f'[diary_scheduler] 留言 #{comment_id} {char_name} skip: {parsed.get("reason","")}')
            return

        jp = (parsed.get('jp') or '').strip()
        zh = (parsed.get('zh') or '').strip()
        emotion = parsed.get('emotion', '平静')
        if not jp:
            return

        # 合成语音
        audio_b64 = ''
        try:
            from tts import tts_to_b64
            audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
        except Exception as e:
            print(f'[diary_scheduler] TTS 出错: {e}')

        # 存 proactive_msg (kind='diary_react')
        mid, ts = proactive_msg.add_proactive_msg(
            character_id, TARGET_USER, 'diary_react', jp, zh, emotion, audio_b64, created_at=now
        )
        print(f'[diary_scheduler] ✅ {char_name} 对留言 #{comment_id} 反应 → msg #{mid}: {jp[:40]}')

        # 塞短记忆
        try:
            save_short_memory(TARGET_USER, 'assistant', jp, character_id)
        except Exception:
            pass

        # ★ C 记忆闭环:这次反应也进 bond,gojo 后续聊天时能自然引用"我上次因为她那句留言说了 X"
        try:
            comment_snippet = comment_content[:40] + ('…' if len(comment_content) > 40 else '')
            reaction_snippet = zh[:40] + ('…' if len(zh) > 40 else '')
            if revisit_count == 0:
                bond_text = f'她在我日记下留言「{comment_snippet}」,我回她「{reaction_snippet}」'
            else:
                bond_text = f'她那条日记留言「{comment_snippet}」我还没消气,第{revisit_count+1}次跟她说「{reaction_snippet}」'
            save_bond_memory(TARGET_USER, character_id, 'between', bond_text)
        except Exception as e:
            print(f'[diary_scheduler] 反应→bond 写入失败:{e}')

        # ★ D 情绪延续:如果 LLM 说"还是气不过",建一条 revisit promise,让 proactive_scheduler 到点再触发
        try:
            still = bool(parsed.get('still_bothering'))
            hours = parsed.get('revisit_after_hours')
            r_ctx = (parsed.get('revisit_context') or '').strip()
            # 允许 revisit 的条件:LLM 说还气 + 有合理时间 + 有 context + 没超套娃上限(≤2)
            if still and isinstance(hours, (int, float)) and 0 < hours <= 24 and r_ctx and revisit_count < 2:
                from datetime import timedelta as _td
                trigger_at = now + _td(hours=float(hours))
                # context 里带上元数据,让 proactive_scheduler 触发时知道这是 diary_react 的 revisit
                packed_ctx = (
                    f'[diary_react_revisit revisit_count={revisit_count+1} '
                    f'source_comment_id={comment_id}] {r_ctx}'
                )
                pid = db_promise.add_promise(
                    character_id=character_id, user_id=TARGET_USER,
                    trigger_kind='once', trigger_at=trigger_at,
                    context=packed_ctx,
                    origin_text=f'留言「{comment_content[:80]}」+ 我回「{jp[:80]}」'
                )
                print(f'[diary_scheduler] 🔥 {char_name} 还气不过,{hours}小时后再来一次 promise #{pid}')
        except Exception as e:
            print(f'[diary_scheduler] revisit promise 创建失败(不影响主流程):{e}')

        # 推送
        try:
            import push_notify
            push_notify.push_to_user(
                TARGET_USER,
                title=char_name,
                body=zh or jp,
                data={'type': 'proactive', 'character_id': character_id, 'source': 'diary_react'},
            )
        except Exception as e:
            print(f'[diary_scheduler] 推送跳过: {e}')

    except Exception as e:
        print(f'[diary_scheduler] 留言反应生成出错: {e}')


def _maybe_generate_schedule(character_id):
    """★ 每天给角色生成一份日程。已有就跳过,所以一天只会真正生成一次。

    挂在 diary_scheduler 的 tick 里(每 3 小时一次),
    比单开一个线程省事 —— 反正一天生成一次,早几小时晚几小时无所谓。
    """
    try:
        import schedule_engine
        schedule_engine.ensure_today(character_id, TARGET_USER)
    except Exception as e:
        print(f'[diary_scheduler] {character_id} 生成日程出错:{e}')


def _tick():
    """一次 tick:遍历所有角色,每人各自 3 种行为"""
    char_ids = _get_active_character_ids()
    if not char_ids:
        return
    for cid in char_ids:
        try:
            _maybe_generate_schedule(cid)     # ★ 先确保今天有日程
        except Exception as e:
            print(f'[diary_scheduler] {cid} 日程 tick 出错:{e}')
        try:
            _maybe_write_diary(cid)
        except Exception as e:
            print(f'[diary_scheduler] {cid} 写日记 tick 出错:{e}')
        try:
            _maybe_peek_diary(cid)
        except Exception as e:
            print(f'[diary_scheduler] {cid} 偷看 tick 出错:{e}')
        try:
            _maybe_react_to_comments(cid)
        except Exception as e:
            print(f'[diary_scheduler] {cid} 反应留言 tick 出错:{e}')


def _loop():
    global _stop
    time.sleep(60)  # 启动后稍等
    while not _stop:
        try:
            _tick()
        except Exception as e:
            print(f'[diary_scheduler] tick 出错:{e}')
        jitter = random.randint(-1800, 1800)  # ±30 分钟
        time.sleep(max(600, TICK_SECONDS + jitter))


def start_diary_scheduler():
    global _thread
    if _thread is not None:
        return
    _thread = threading.Thread(target=_loop, daemon=True)
    _thread.start()
    print('[diary_scheduler] 日记常驻排程已启动(多角色 + 留言反应)')


# ══════════════════════════════════════════════════════════
# 开 App 补偿:仅补 gojo(保持兼容),暂不改
# ══════════════════════════════════════════════════════════

DIARY_CHARACTER = 'gojo'  # 补偿检查的默认角色(兼容原接口)


def catch_up(user_id=TARGET_USER, character_id=DIARY_CHARACTER):
    """补偿检查(单角色,兼容老接口)。多角色 tick 已经能覆盖。"""
    result = {'wrote': False, 'peeked': False}
    try:
        if db_diary.count_char_diaries_since(character_id, user_id, _today_start()) < 1:
            last = db_diary.get_last_char_diary_time(character_id, user_id)
            far_enough = True
            if last is not None:
                try:
                    hours = (datetime.utcnow() - last.replace(tzinfo=None)).total_seconds() / 3600
                    far_enough = hours >= 30
                except Exception:
                    pass
            if far_enough and random.random() < 0.5:
                if diary_engine.generate_char_diary(character_id, user_id):
                    result['wrote'] = True

        from db import get_conn
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            'SELECT COUNT(*) FROM diary_visit WHERE character_id=%s AND user_id=%s AND visited_at >= %s',
            (character_id, user_id, _today_start())
        )
        cnt = cur.fetchone()[0]
        cur.close()
        conn.close()
        if cnt < 1 and random.random() < 0.5:
            peeked = diary_engine.peek_user_diary(character_id, user_id, visited_at=_now())
            if peeked:
                result['peeked'] = True
    except Exception as e:
        print(f'[diary_scheduler] catch_up 出错:{e}')
    return result