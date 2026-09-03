"""聊天路由：/chat/text /chat/story /chat/proactive /chat/voice_text /chat/voice_story /chat/voice/proactive /transcribe

★ 本版改动（prompt 缓存）：
  - system 改用 build_system_blocks()，返回带 cache_control 的分段数组
  - 场景补充文字（故事模式/语音通话等）必须走 extra_suffix 参数传入，
    绝不能写成 build_system_blocks(...) + '字符串'（列表加字符串会直接 TypeError 崩溃）
  - 每次调用后 log_cache_usage 打印缓存命中，部署后看日志即可确认省了多少

★ v-fix：预填 JSON（修"空循环"）
  - 模型有时不输出 JSON、直接吐纯日语 → 解析失败 → 重试5次全废 → 落兜底"没听清"。
  - 解法：在 messages 末尾预填一条 {'role':'assistant','content':'{'}，强制模型必须从 { 接着写 JSON，
    拿到回复后把开头的 { 补回去再解析。所有产生 JSON 的端点都套用（见 _create_json）。

★ 记账升级：/chat/text 里,LLM 返回 pending_transaction 时,后端只透传给前端(不写库),
  由前端确认卡引导用户核对后再 POST /accounting/records 落库。其他 handler 一律不做记账检测。

★ v4 感情判断接入（本次改动）：
  - _fire_and_forget_relationship_update 改用 sys.stderr 直写 + flush，
    避免 uvicorn/Docker 里子线程 print 被 stdout buffer 吞掉（无法诊断问题）。
  - 传 character_core_snippet（core_prompt 前 300 字）+ recent_context（最近 6 轮对话）
    给 Observer，让它判断短消息（"哈哈"、"嗯"）时有上下文可参考，判断质量提升一大档。
  - 输出精简为一行 done 汇总，不再刷屏 state summary。
  - 参数通过 threading args= 传入，避免闭包读被 handler return 后的变量。
  - 修复原实现里 `except Exception:` 后 `print({e})` 但 e 未定义的 bug。
"""
import threading
import random
import json
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, TTS_PROVIDER, DEFAULT_CHARACTER_ID, MODEL_MAIN, MODEL_JP_AUX
from db import get_conn
from utils import extract_json, sanitize_jp, merge_only_extreme_short
from ai_client import extract_text
from tts import tts_to_b64, transcribe_audio_b64
from prompt import build_system_blocks, log_cache_usage
from user_memory import (
    save_short_memory, get_short_memory,
    update_chat_days, SHORT_MEMORY_MAX,
)
from memory_jobs import enqueue_private_extraction
from characters import get_character
from tasks import (
    find_duplicate_task,
    find_and_delete_tasks_by_keyword,
    delete_latest_task,
)
from task_dedup import find_similar_task   # ★ 模糊去重：同时段+意思相近就算同一件事

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# ★ 预填：强制模型从 { 开始输出 JSON
def _create_json(model, max_tokens, system_blocks, messages):
    """统一的模型调用。
    ★ 不再预填 assistant '{'——claude-sonnet-4-6 不支持 assistant prefill（会 400）。
    改为直接调用，靠下面 _parse_reply 的宽松解析（从第一个 { 抠到最后一个 }）扛住
    模型偶尔在 JSON 前多说两句的情况。返回 (raw_text, response)。"""
    response = claude_client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system_blocks,
        messages=messages,
    )
    raw = extract_text(response).strip()
    return raw, response


def _msg_has_json_debris(m: dict) -> bool:
    """检测消息 dict 里的 jp/zh 是否含 JSON 结构残骸(如 `","messages":"jp":"`)。
    True = 消息脏了,不该用。用于所有 LLM 消息数组验证。"""
    jp = str(m.get('jp', ''))
    zh = str(m.get('zh', ''))
    for kw in ('"jp"', '"zh"', '"messages"', '"emotion"'):
        if kw in jp or kw in zh:
            return True
    return False


def _valid_msg(m: dict) -> bool:
    """统一验证:jp/zh 都非空 + 没 JSON 残骸。"""
    if not str(m.get('jp', '')).strip() or not str(m.get('zh', '')).strip():
        return False
    return not _msg_has_json_debris(m)


def _parse_reply(raw: str):
    """把模型回复解析成 JSON。
    先用 extract_json；失败就宽松地从第一个 { 抠到最后一个 } 再解析——
    这样即使模型在 JSON 前面写了多余的日语/解说，也能把真正的 JSON 抠出来，
    不会再因为"散文前缀"而整段解析失败、掉进兜底。"""
    try:
        parsed = extract_json(raw)
    except Exception:
        parsed = None
    if parsed:
        return parsed
    try:
        i = raw.find('{')
        j = raw.rfind('}')
        if i != -1 and j > i:
            return json.loads(raw[i:j + 1])
    except Exception:
        pass
    return None


def _salvage_japanese(raw: str):
    """从模型没包成 JSON 的原始回复里，抢救出可用的日语当回复。
    用于：模型直接吐日语大白话、没输出 JSON 时，别浪费他真说的话。
    返回 {'jp':..., 'zh':...} 或 None。"""
    import re
    if not raw:
        return None
    text = raw.strip().strip('`').strip()

    # ★ 强化:如果原文里出现【多个】JSON 字段名残骸,说明这是【JSON 结构坏了】,
    #   不能当"纯日语"救,否则会把 `调皮","messages":"jp":"...` 直接塞给用户看
    #   (那种脏数据比默认兜底更糟)
    json_field_hits = 0
    for kw in ('"jp"', '"zh"', '"messages"', '"emotion"'):
        if kw in text:
            json_field_hits += 1
    if json_field_hits >= 2:
        # 2 个及以上字段名残骸 = 明显是坏 JSON 泄露,不救
        print(f'[salvage] 检测到 JSON 结构泄露({json_field_hits} 个字段名),放弃救援')
        return None

    text = re.sub(r'^\s*\{?\s*"?(emotion|messages|jp|zh)"?\s*:?', '', text)
    text = text.replace('{', '').replace('}', '').replace('[', '').replace(']', '').strip()
    text = text.strip('"\'，, 。').strip()
    if not text:
        return None
    # 必须含有假名/日文汉字，才认为是"他真说了话"，否则宁可走兜底
    if not re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', text):
        return None
    # ★ 再一次防御:救援后的文本里如果还包含 `"jp":`、`"zh":`、`"emotion"` 这些残骸,也算失败
    for kw in ('"jp"', '"zh"', '"messages"', '"emotion"'):
        if kw in text:
            print(f'[salvage] 救援后仍含 JSON 残骸 {kw},放弃')
            return None
    # 截断过长的（避免把一堆乱码全塞进去）
    jp = text[:200].strip()
    zh = _quick_translate(jp)
    return {'jp': jp, 'zh': zh}


def _quick_translate(jp: str) -> str:
    """把一句日语快速翻成中文（救援用）。失败就返回空串，不阻断主流程。"""
    if not jp:
        return ''
    try:
        resp = claude_client.messages.create(
            model=MODEL_JP_AUX,
            max_tokens=200,
            messages=[{'role': 'user', 'content':
                f'把下面这句日语忠实翻译成中文，只输出译文本身，不要解释、不要引号：\n{jp}'}],
        )
        return extract_text(resp).strip().strip('「」"\'。 ').strip()
    except Exception:
        return ''


# ★ 记账透传辅助：只做基本形状校验,不写库(由前端确认后 POST /accounting/records)
def _extract_pending_tx(result: dict, user_id: str, tag: str = 'chat'):
    """从模型回复里抠出 pending_transaction 字段,校验后返回给前端。
    校验失败或字段不存在都返回 None,不抛错(记账不应影响主对话)。"""
    pt = result.get('pending_transaction') if isinstance(result, dict) else None
    if not pt:
        return None
    try:
        amt = float(pt.get('amount', 0))
        typ = pt.get('type')
        desc = (pt.get('desc') or '').strip()
        if amt > 0 and typ in ('in', 'out') and desc:
            out = {
                'type': typ,
                'category': pt.get('category', '其他'),
                'amount': amt,
                'desc': desc,
                'account_hint': pt.get('account_hint', ''),
                'date': pt.get('date'),
                'time': pt.get('time'),
            }
            print(f'[{user_id}] 💰 [{tag}] 检测到待确认记账 {typ} ¥{amt} {desc}')
            return out
    except Exception as e:
        print(f'[{user_id}] [{tag}] pending_transaction 解析失败:{e}')
    return None


# ═══════════════════════════════════════════════════════════════════
# ★ v4 感情判断异步触发器 —— 顶层函数，方便所有 endpoint 调用
# ═══════════════════════════════════════════════════════════════════
def _fire_relationship_update(user_id, character_id, user_text, full_jp,
                              core_snippet, recent_ctx):
    """子线程：调 process_turn 更新 v4 关系账本。
    ★ 用 sys.stderr 直写 + flush，避免 uvicorn stdout buffering 吞掉子线程输出。
    ★ 所有异常必须自己捕获——子线程报错默认无声。
    """
    import sys
    import traceback

    def _log(msg):
        # stderr 是 line-buffered，Zeabur/Docker 一定能捕获到
        sys.stderr.write(msg + '\n')
        sys.stderr.flush()

    try:
        from relationship_engine import process_turn
        _log(f'[rel_update] start {user_id}/{character_id}')
        result = process_turn(
            user_id=user_id,
            character_id=character_id,
            user_message=user_text,
            character_reply=full_jp,
            character_core_snippet=core_snippet,
            recent_context=recent_ctx,
        )
        # 精简输出：只打关键数字 + action 列表 + error（不再刷 state summary 满屏）
        sig_n = result.get('signals_extracted', 0)
        app_n = result.get('signals_applied', 0)
        err = result.get('observer_error')
        actions = [a.get('result', {}).get('action', '?')
                   for a in result.get('applied', [])
                   if a.get('result')]
        _log(f'[rel_update] done {user_id}/{character_id} '
             f'signals={sig_n} applied={app_n} '
             f'actions={actions} err={err}')
    except Exception as e:
        _log(f'[rel_update] EXCEPTION {user_id}/{character_id}: {type(e).__name__}: {e}')
        _log(traceback.format_exc())


def _start_relationship_update(user_id, character_id, user_text, full_jp,
                               char, short_memories):
    """快捷方法：从 handler 里一行调用起 v4 更新线程。
    char + short_memories 由 handler 提供（handler 里已经拿到了）。"""
    core_snippet = (char.get('core_prompt') or '')[:300]
    recent_ctx = [{'role': r, 'content': c} for r, c in (short_memories or [])[-6:]]
    threading.Thread(
        target=_fire_relationship_update,
        args=(user_id, character_id, user_text, full_jp, core_snippet, recent_ctx),
        daemon=True,
    ).start()


@router.post('/chat/text')
async def chat_text(data: dict):
    user_text    = data.get('text', '')
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not user_text:
        return JSONResponse({'error': 'no input'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    # ★ 角色日程:他现在可能真的走不开(上课/出任务/洗澡)。
    #   走不开就【只已读不回】,并排一条 promise 等忙完再回 ——
    #   这比秒回一句"我在忙"更像真人。
    try:
        import db_schedule, db_promise
        from datetime import datetime as _dt, timedelta as _td
        from config import CN_TZ as _CN_TZ
        _now_dt = _dt.now(_CN_TZ)
        act = db_schedule.get_current_activity(character_id, user_id, _now_dt)
        if act and not act['can_reply']:
            # 先把这句话存进短期记忆,不然他忙完回来不知道你说了啥
            save_short_memory(user_id, 'user', user_text, character_id)

            free_at = db_schedule.get_next_free_time(character_id, user_id, _now_dt) or act['end_time']
            try:
                hh, mm = free_at.split(':')
                trigger_at = _now_dt.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
                if trigger_at <= _now_dt:          # 跨到明天了
                    trigger_at += _td(days=1)

                # ★ 关键去重:用户在你忙的期间连发几条,不要每条都建 promise ——
                #   否则你"忙完"那一刻 scheduler 会一次触发多条 promise,
                #   生成 4-5 条内容相似的复读消息(实测踩过的坑,图2 的锅)。
                #   做法:查一下最近 6h 内有没有还没触发的 once promise,
                #   有 → 合并进那条的 context;没有 → 才新建。
                _conn = get_conn()
                _cur = _conn.cursor()
                try:
                    _cur.execute(
                        """SELECT id, context FROM proactive_promise
                           WHERE character_id=%s AND user_id=%s
                             AND trigger_kind='once'
                             AND is_fired=FALSE AND is_active=TRUE
                             AND created_at >= NOW() - INTERVAL '6 hours'
                           ORDER BY created_at DESC LIMIT 1""",
                        (character_id, user_id))
                    _row = _cur.fetchone()
                    if _row:
                        # 已经有一条待触发的 promise → 追加这句进去,并把触发时间刷成最新的 free_at
                        _pid, _existing_ctx = _row
                        _new_ctx = (_existing_ctx or '') + f'\n她后来又说:「{user_text[:150]}」'
                        _cur.execute(
                            """UPDATE proactive_promise
                               SET context=%s, trigger_at=%s
                               WHERE id=%s""",
                            (_new_ctx, trigger_at, _pid))
                        _conn.commit()
                        print(f'[{user_id}] 📵 追加到已有 promise #{_pid},合并回复不复读')
                    else:
                        db_promise.add_promise(
                            character_id=character_id, user_id=user_id,
                            trigger_kind='once', trigger_at=trigger_at,
                            context=(f'刚才我在{act["title"]}(走不开),没能回她。'
                                     f'她当时说:「{user_text[:150]}」。'
                                     f'现在忙完了,回一下她 —— 可以顺口提一句刚才在忙什么。'
                                     f'如果她期间还说了别的事,把话头拢起来一起回,别逐条应答。'),
                            origin_text=user_text[:200],
                        )
                        print(f'[{user_id}] 📵 {character_id} 正在「{act["title"]}」,只已读,{free_at} 忙完再回')
                finally:
                    _cur.close()
                    _conn.close()
            except Exception as _e:
                print(f'[{user_id}] 排延迟回复失败:{_e}')

            return JSONResponse({
                'busy': True,
                'activity': act['title'],
                'location': act.get('location', ''),
                'until': act['end_time'],
                'free_at': free_at,
                'total_days': update_chat_days(user_id),
            })
    except Exception as _e:
        print(f'[{user_id}] 日程检查跳过(不影响聊天):{_e}')


    total_days = update_chat_days(user_id)
    short_memories = get_short_memory(user_id, SHORT_MEMORY_MAX, character_id)

    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': user_text})

    recall_query = user_text
    if short_memories:
        recall_query = user_text + ' ' + ' '.join(c for _, c in short_memories[-2:])

    system_blocks = build_system_blocks(user_id, character_id, recall_query)

    result = None
    last_raw = ''   # ★ 记住最后一次模型原始回复，用于"纯日语救援"
    for attempt in range(3):
        try:
            raw, response = _create_json(MODEL_MAIN, 1500, system_blocks, messages)
            log_cache_usage(f'chat:{character_id}', response)
            print(f'[{user_id}][{character_id}] attempt {attempt+1}: {raw[:120]}...')
            if raw:
                last_raw = raw
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(_valid_msg(m) for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'attempt {attempt+1} error: {e}')

    # ★ 纯日语救援：模型说了日语但没包成 JSON（解析全失败）时，
    #   与其甩一句"没听清"，不如把他真正说的话用上——比兜底自然得多。
    if not result and last_raw:
        salvaged = _salvage_japanese(last_raw)
        if salvaged:
            result = {'emotion': '平静', 'messages': [salvaged]}
            print(f'[{user_id}][{character_id}] 纯日语救援：{salvaged["jp"][:40]}')

    if not result:
        fallback_pool = [
            {'jp': 'ん？ちょっと聞き取れなかった。もう一回言って。', 'zh': '嗯？没太听清，再说一遍。'},
            {'jp': 'さあ、なんだろうね。', 'zh': '谁知道呢。'},
            {'jp': 'へえ、それで？', 'zh': '哦？然后呢？'},
            {'jp': 'ふっ、急にどうしたの。', 'zh': '哼，怎么突然这样。'},
        ]
        result = {'emotion': '调皮', 'messages': [random.choice(fallback_pool)]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'user', user_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    enqueue_private_extraction(user_id, user_text, full_jp, character_id)
    # ★ 事件驱动日记：聊到大事时，他会因为"这事值得记"而写一篇（后台，不阻塞回复）
    try:
        import diary_engine
        threading.Thread(target=diary_engine.maybe_write_diary_on_event,
                         args=(character_id, user_id, user_text, full_jp),
                         daemon=True).start()
    except Exception:
        pass

    # ★ 承诺检测:角色回复里如果有"答应/承诺/会主动找你"类的话,自动创建 proactive_promise
    try:
        import promise_detector
        # full_jp 是日语回复,但 promise_detector 需要中文。从 result 里拿 zh
        reply_zh_combined = ' '.join(m.get('zh', '') for m in msgs if m.get('zh'))
        if reply_zh_combined:
            threading.Thread(target=promise_detector.detect_and_save,
                            args=(character_id, user_id, user_text, reply_zh_combined),
                            daemon=True).start()
    except Exception:
        pass

    # ★ 便利贴吐槽:这一轮聊完,他心里可能会嘀咕一句(不发出来,只写便利贴)
    #   完全后台,失败也不影响主对话
    try:
        import grumble_engine
        threading.Thread(target=grumble_engine.maybe_write_grumble,
                         args=(character_id, user_id, user_text, full_jp),
                         daemon=True).start()
    except Exception:
        pass

    # ★ v4 感情账本异步更新（传上下文 + stderr 可靠输出，见文件顶部说明）
    _start_relationship_update(user_id, character_id, user_text, full_jp,
                               char, short_memories)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[TTS:{TTS_PROVIDER}] {character_id} emotion={emotion} segs={len(msgs)} days={total_days}')

    cancelled_tasks = []
    if result.get('cancel_reminder'):
        cancel = result['cancel_reminder']
        keyword = (cancel.get('keyword') or '').strip()
        latest = cancel.get('latest', False)
        try:
            if keyword:
                deleted = find_and_delete_tasks_by_keyword(user_id, keyword, latest_only=True)
            elif latest:
                deleted = delete_latest_task(user_id)
            else:
                deleted = []
            for task_id, notif_id in deleted:
                cancelled_tasks.append({'task_id': task_id, 'notification_id': notif_id})
                print(f'[{user_id}] 🗑️ 已取消任务 id={task_id} keyword={keyword or "(latest)"}')
        except Exception as e:
            print(f'取消提醒失败：{e}')

    reminder_data = None
    if result.get('reminder'):
        rem = result['reminder']
        reminder_data = {
            'date': rem.get('date'),
            'time': rem.get('time'),
            'content': rem.get('content', ''),
            'notification': rem.get('notification', ''),
        }
        try:
            existing = find_duplicate_task(
                user_id,
                reminder_data['content'],
                reminder_data['date'],
                reminder_data['time'],
            )
            similar = None
            if not existing:
                similar = find_similar_task(
                    user_id,
                    reminder_data['content'],
                    reminder_data['date'],
                    reminder_data['time'],
                )
            if existing or similar:
                if existing:
                    task_id, _ = existing
                    same_title = reminder_data['content']
                else:
                    task_id, _notif, same_title = similar
                    print(f'[{user_id}] 🔁 同时段已有相近提醒「{same_title}」，跳过新建：{reminder_data["content"]}')
                reminder_data['task_id'] = task_id
                reminder_data['duplicate'] = True
                print(f'[{user_id}] 🔁 提醒已存在 task_id={task_id}，跳过新建')
            else:
                conn = get_conn()
                cur = conn.cursor()
                cur.execute(
                    '''INSERT INTO tasks (user_id, title, category, due_date, due_time, reminder_minutes)
                       VALUES (%s, %s, %s, %s, %s, %s) RETURNING id''',
                    (user_id, reminder_data['content'], '个人',
                     reminder_data['date'], reminder_data['time'], 0)
                )
                task_id = cur.fetchone()[0]
                conn.commit()
                cur.close()
                conn.close()
                reminder_data['task_id'] = task_id
                reminder_data['duplicate'] = False
                print(f'[{user_id}] ✅ 提醒已保存 task_id={task_id}')
        except Exception as e:
            print(f'提醒保存失败：{e}')

    # ★ 记账透传（只透传给前端,不写库；前端确认卡引导用户核对账户后 POST /accounting/records）
    pending_tx = _extract_pending_tx(result, user_id, tag='chat')

    # ★ 承诺处理:LLM 决定"以后要主动开口"时,存到 proactive_promise 表,scheduler 到时候触发
    saved_promise = None
    if result.get('proactive_promise'):
        try:
            import db_promise
            from datetime import datetime as _dt
            pp = result['proactive_promise']
            kind = pp.get('trigger_kind')
            context_ = (pp.get('context') or '').strip()
            if kind == 'once' and pp.get('trigger_at') and context_:
                # 解析 YYYY-MM-DD HH:MM
                trigger_at = _dt.strptime(pp['trigger_at'], '%Y-%m-%d %H:%M')
                pid = db_promise.add_promise(
                    character_id=character_id, user_id=user_id,
                    trigger_kind='once', trigger_at=trigger_at,
                    context=context_, origin_text=user_text[:200]
                )
                saved_promise = {'id': pid, 'kind': 'once', 'trigger_at': pp['trigger_at'], 'context': context_}
                print(f'[{user_id}] 🤝 记下承诺 #{pid} once @ {pp["trigger_at"]}: {context_}')
            elif kind == 'daily' and pp.get('trigger_time') and context_:
                pid = db_promise.add_promise(
                    character_id=character_id, user_id=user_id,
                    trigger_kind='daily', trigger_time=pp['trigger_time'],
                    context=context_, origin_text=user_text[:200]
                )
                saved_promise = {'id': pid, 'kind': 'daily', 'trigger_time': pp['trigger_time'], 'context': context_}
                print(f'[{user_id}] 🤝 记下承诺 #{pid} daily @ {pp["trigger_time"]}: {context_}')
            else:
                print(f'[{user_id}] proactive_promise 字段不全,跳过:{pp}')
        except Exception as e:
            print(f'[{user_id}] proactive_promise 保存失败:{e}')

    resp = {'emotion': emotion, 'messages': msgs, 'total_days': total_days}
    if reminder_data:
        resp['reminder'] = reminder_data
    if cancelled_tasks:
        resp['cancelled_tasks'] = cancelled_tasks
    if pending_tx:
        resp['pending_transaction'] = pending_tx
    if saved_promise:
        resp['saved_promise'] = saved_promise
    return JSONResponse(resp)


# ─────────────────── 长故事模式（文本）───────────────────

STORY_SCENE = '''

【★ 故事模式——必须遵守】
对方想听你讲一个完整的故事。用你自己的视角和口吻来讲。
1. 故事要完整：有开头、发展、高潮、结尾，一口气讲完，不要中途停。
2. 融入你的性格。
3. 分成 10-15 个气泡，每个气泡是故事的一小段。
4. 每个气泡的【日语】控制在 40-120 字之间——这点很重要，单段太长会影响语音合成质量。
5. jp 必须是纯日语，zh 是对应的中文翻译，不要把中文混进 jp。

严格按这个 JSON 返回：
{"emotion":"情绪","messages":[{"jp":"第一段日语","zh":"第一段中文"},{"jp":"第二段日语","zh":"第二段中文"}]}'''


@router.post('/chat/story')
async def chat_story(data: dict):
    user_text    = data.get('text', '')
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not user_text:
        return JSONResponse({'error': 'no input'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    total_days = update_chat_days(user_id)
    short_memories = get_short_memory(user_id, SHORT_MEMORY_MAX, character_id)

    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': user_text})

    recall_query = user_text
    if short_memories:
        recall_query = user_text + ' ' + ' '.join(c for _, c in short_memories[-2:])

    system_blocks = build_system_blocks(user_id, character_id, recall_query, extra_suffix=STORY_SCENE)

    result = None
    for attempt in range(5):
        try:
            raw, response = _create_json(MODEL_MAIN, 4000, system_blocks, messages)
            log_cache_usage(f'story:{character_id}', response)
            print(f'[story] attempt {attempt+1}: {raw[:120]}...')
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(_valid_msg(m) for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[story] attempt {attempt+1} error: {e}')

    if not result:
        result = {
            'emotion': '平静',
            'messages': [
                {'jp': 'まあ、いいよ。話を聞かせてあげる。', 'zh': '嘛，好啊，讲个故事给你听。'},
                {'jp': '昔々、最強の呪術師がいてね。', 'zh': '很久很久以前，有一个最强的咒术师。'},
                {'jp': 'まあ、それ僕のことなんだけど。', 'zh': '嘛，虽然那说的就是我啦。'},
            ],
        }

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'user', user_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    enqueue_private_extraction(user_id, user_text, full_jp, character_id)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    total_chars = sum(len(m['jp']) for m in msgs)
    print(f'[story] {character_id} emotion={emotion} segs={len(msgs)} chars={total_chars} days={total_days}')

    return JSONResponse({
        'emotion': emotion,
        'messages': msgs,
        'total_days': total_days,
        'total_chars': total_chars,
    })


# ─────────────────── 主动消息（日程提醒 / 超时追问） ───────────────────

@router.post('/chat/proactive')
async def chat_proactive(data: dict):
    user_id      = data.get('user_id', 'default')
    task_title   = data.get('task_title', '')
    mode         = data.get('mode', 'remind')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not task_title:
        return JSONResponse({'error': 'no task'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    if mode == 'remind':
        trigger = f'【系统触发：到提醒时间了】现在该主动提醒对方去做这件事："{task_title}"。语气慵懒又带点关心，1条气泡。'
    else:
        trigger = f'【系统触发：超时未完成】对方之前要做"{task_title}"，已经过了时间没动静。主动问她做完了没，带点调侃或假装不在意的关心，1条气泡。'

    short_memories = get_short_memory(user_id, 4, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': trigger})

    system_blocks = build_system_blocks(user_id, character_id, task_title)

    result = None
    for attempt in range(3):
        try:
            raw, response = _create_json(MODEL_MAIN, 400, system_blocks, messages)
            log_cache_usage(f'proactive:{character_id}', response)
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(_valid_msg(m) for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[proactive] attempt {attempt+1} error: {e}')

    if not result:
        if mode == 'remind':
            result = {'emotion': '调皮', 'messages': [{'jp': f'おい、{task_title}の時間だよ。', 'zh': f'喂，该{task_title}了哦。'}]}
        else:
            result = {'emotion': '疑惑', 'messages': [{'jp': f'{task_title}、ちゃんとやった？', 'zh': f'{task_title}，好好做了吗？'}]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'assistant', full_jp, character_id)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[proactive] {character_id} mode={mode} task={task_title}')
    return JSONResponse({'emotion': emotion, 'messages': msgs})


# ─────────────────── 语音通话专用（Haiku 极速版） ───────────────────

VOICE_CALL_SCENE = '''

【★ 语音通话场景】
现在在和对方打电话。回复自然口语化，根据对方说的话灵活决定回复条数和长度：
- 简单寒暄/短句 → 1条气泡，简短回应
- 对方说了重要的事/问了复杂的问题 → 可以分2-3条气泡，像真打电话一样自然衔接
- 每条气泡10-50字，不要长篇大论，但也不要过于压缩。'''


@router.post('/chat/voice_text')
async def chat_voice_text(data: dict):
    """语音通话快速回复（Haiku，比 Sonnet 快 2-3 倍）"""
    user_text    = data.get('text', '')
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not user_text:
        return JSONResponse({'error': 'no input'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    short_memories = get_short_memory(user_id, SHORT_MEMORY_MAX, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': user_text})

    system_blocks = build_system_blocks(user_id, character_id, user_text, extra_suffix=VOICE_CALL_SCENE)

    result = None
    for attempt in range(3):
        try:
            raw, response = _create_json(MODEL_JP_AUX, 500, system_blocks, messages)
            log_cache_usage(f'voice:{character_id}', response)
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(_valid_msg(m) for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[voice_text] attempt {attempt+1} error: {e}')

    if not result:
        result = {'emotion': '调皮', 'messages': [{'jp': 'ふっ、何か言った？', 'zh': '哼，你说了什么？'}]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'user', user_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    enqueue_private_extraction(user_id, user_text, full_jp, character_id)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[voice_text] {character_id} emotion={emotion} segs={len(msgs)}')
    return JSONResponse({'emotion': emotion, 'messages': msgs})


# ─────────────────── 语音通话·长故事模式 ───────────────────

VOICE_STORY_SCENE = '''

【★ 语音通话·长故事模式】
对方想在通话里听你讲故事。用你自己的视角和口吻，像真的在电话里娓娓道来。
1. 故事要完整：开头、发展、高潮、结尾，一口气讲完。
2. 分成 8-15 个气泡，每个气泡是故事的一小段。
3. 每个气泡的【日语】控制在 40-90 字之间——通话场景要短一点更自然，也保证语音质量。
4. jp 必须是纯日语，zh 是对应中文翻译，不要把中文混进 jp。

严格按这个 JSON 返回：
{"emotion":"情绪","messages":[{"jp":"第一段日语","zh":"第一段中文"},{"jp":"第二段日语","zh":"第二段中文"}]}'''


@router.post('/chat/voice_story')
async def chat_voice_story(data: dict):
    user_text    = data.get('text', '')
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not user_text:
        return JSONResponse({'error': 'no input'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    short_memories = get_short_memory(user_id, 4, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': user_text})

    system_blocks = build_system_blocks(user_id, character_id, user_text, extra_suffix=VOICE_STORY_SCENE)

    result = None
    for attempt in range(5):
        try:
            raw, response = _create_json(MODEL_MAIN, 3000, system_blocks, messages)
            log_cache_usage(f'voice_story:{character_id}', response)
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) >= 3:
                if all(_valid_msg(m) for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[voice_story] attempt {attempt+1} error: {e}')

    if not result:
        result = {
            'emotion': '平静',
            'messages': [
                {'jp': 'さて、どんな話をしようか。', 'zh': '那么，讲个什么故事呢。'},
                {'jp': '昔々、最強の呪術師がいてね。', 'zh': '很久很久以前，有一个最强的咒术师。'},
                {'jp': 'まあ、それ僕のことなんだけど。', 'zh': '嘛，虽然那说的就是我啦。'},
            ],
        }

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'user', user_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    enqueue_private_extraction(user_id, user_text, full_jp, character_id)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    total_chars = sum(len(m['jp']) for m in msgs)
    print(f'[voice_story] {character_id} emotion={emotion} segs={len(msgs)} chars={total_chars}')

    return JSONResponse({
        'emotion': emotion,
        'messages': msgs,
        'total_chars': total_chars,
    })


# ─────────────────── 语音通话主动开口（接通开场 / 沉默追问） ───────────────────

@router.post('/chat/voice/proactive')
async def chat_voice_proactive(data: dict):
    user_id         = data.get('user_id', 'default')
    character_id    = data.get('character_id', DEFAULT_CHARACTER_ID)
    mode            = data.get('mode', 'idle')
    silence_seconds = int(data.get('silence_seconds', 15))

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    if mode == 'greeting':
        trigger = ('【系统:电话刚接通。'
                   '按你此刻对她的【真实态度】开口——不是"客服接通"式打招呼,不是默认关心。'
                   '如果记忆里你们【几乎不认识】(短记忆里没什么东西),你的反应应该像"陌生人突然打进来电话"——警觉/不解/追问"是谁""什么事",按你的人设自然反应。'
                   '如果之前刚吵过、刚被冒犯过、话题正憋着气,那就【带着那股气】开口,不要装没事。'
                   '如果之前聊得正常,就顺着上一句自然接下去(别复述)。'
                   '1-2 句,自然口语。'
                   '★ 严禁默认"想听你声音"/"来了啊"/"怎么打过来了想我了"这类熟人调情腔——除非你们真的到那一步。】')
        scene = '''

【★ 语音通话·接通开场】
你刚接起对方的电话。你的【第一反应】完全取决于:
1. 记忆里你们是什么关系?(陌生 / 有过几次接触 / 熟 / 亲近)
2. 上一次对话是什么气氛?(和平 / 有摩擦 / 你还带着气 / 温和收线)

按这两点决定开口方式,不要走"接电话默认打招呼"的自动化剧本。
1-2 句,自然口语。'''
        n_recent = 6
    elif mode == 'missed' or silence_seconds > 60:
        trigger = '【系统：对方已经很久没说话了，可能在发呆或者走神了。你主动问她在干嘛，语气慵懒带点调侃，一两句就好。】'
        scene = '''

【★ 语音通话沉默场景】
现在你和对方在打电话，对方没说话。你主动开口打破沉默。
只输出1条气泡，15字以内，自然简短，像真打电话一样。'''
        n_recent = 4
    elif silence_seconds > 30:
        trigger = '【系统：对方沉默了一会儿了。你稍微催一下，带点撒娇或不耐烦，一两句就好。】'
        scene = '''

【★ 语音通话沉默场景】
现在你和对方在打电话，对方没说话。你主动开口打破沉默。
只输出1条气泡，15字以内，自然简短，像真打电话一样。'''
        n_recent = 4
    else:
        trigger = '【系统：对方刚沉默了几秒。你轻声问一句"在干嘛？"或者类似的，自然一点，一两句就好。】'
        scene = '''

【★ 语音通话沉默场景】
现在你和对方在打电话，对方没说话。你主动开口打破沉默。
只输出1条气泡，15字以内，自然简短，像真打电话一样。'''
        n_recent = 4

    short_memories = get_short_memory(user_id, n_recent, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': trigger})

    system_blocks = build_system_blocks(user_id, character_id, '', extra_suffix=scene)

    result = None
    for attempt in range(3):
        try:
            raw, response = _create_json(MODEL_JP_AUX, 300, system_blocks, messages)
            log_cache_usage(f'voice_proactive:{character_id}', response)
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(_valid_msg(m) for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[voice_proactive] attempt {attempt+1} error: {e}')

    if not result:
        if mode == 'greeting':
            result = {'emotion': '调皮', 'messages': [{'jp': 'もしもし、どうした？', 'zh': '喂，怎么啦？'}]}
        elif mode == 'missed':
            result = {'emotion': '疑惑', 'messages': [{'jp': 'おい、聞こえてる？', 'zh': '喂，能听到吗？'}]}
        elif silence_seconds > 30:
            result = {'emotion': '调皮', 'messages': [{'jp': 'ねえ、寝ちゃった？', 'zh': '喂，睡着了吗？'}]}
        else:
            result = {'emotion': '平静', 'messages': [{'jp': 'どうした？', 'zh': '怎么了？'}]}

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = msgs[:2] if mode == 'greeting' else msgs[:1]

    full_jp = ' '.join(m['jp'] for m in msgs)
    save_short_memory(user_id, 'assistant', full_jp, character_id)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[voice_proactive] {character_id} mode={mode} silence={silence_seconds}s')
    return JSONResponse({'emotion': emotion, 'messages': msgs})


# ─────────────────── Whisper 转录 ───────────────────

@router.post('/transcribe')
async def transcribe(data: dict):
    audio_b64 = data.get('audio_base64', '')
    if not audio_b64:
        return JSONResponse({'error': 'no audio'}, status_code=400)
    result = transcribe_audio_b64(audio_b64)
    return JSONResponse(result)