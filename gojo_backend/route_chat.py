"""聊天路由：/chat/text /chat/story /chat/proactive /chat/voice_text /chat/voice_story /chat/voice/proactive /transcribe

★ 本版改动（prompt 缓存）：
  - system 改用 build_system_blocks()，返回带 cache_control 的分段数组
  - 场景补充文字（故事模式/语音通话等）必须走 extra_suffix 参数传入，
    绝不能写成 build_system_blocks(...) + '字符串'（列表加字符串会直接 TypeError 崩溃）
  - 每次调用后 log_cache_usage 打印缓存命中，部署后看日志即可确认省了多少

★ v-fix：预填 JSON（修"空循环"）
  - 模型有时不输出 JSON、直接吐纯日语 → 解析失败 → 重试耗尽后返回 generation_failed，绝不伪造角色台词。
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
from datetime import datetime
import json
import re
import uuid
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, TTS_PROVIDER, DEFAULT_CHARACTER_ID, MODEL_MAIN, MODEL_JP_AUX
from utils import (
    ingest_model_output, sanitize_user_reply, contains_offline_marker,
    finalize_user_messages,
    has_visible_text, valid_reply_msg, commit_ready_msgs, msg_has_json_debris,
)
from ai_client import extract_text
from tts import tts_to_b64, transcribe_audio_b64
from prompt import build_system_blocks, log_cache_usage
from user_memory import (
    save_short_memory, save_user_short_memory_once, get_short_memory,
    update_chat_days, SHORT_MEMORY_MAX,
)
from memory_jobs import enqueue_private_extraction
from temporal_awareness import (
    find_reply_calendar_conflict, get_temporal_snapshot, record_assistant_message,
    record_turn, record_user_message,
)
from characters import get_character

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


# 共享校验（utils）在本模块保留旧名，避免调用点大面积改名。
_has_visible_text = has_visible_text
_msg_has_json_debris = msg_has_json_debris
_valid_msg = valid_reply_msg
_commit_ready = commit_ready_msgs


def _append_visible_assistant_raw(user_id, character_id, content, event_id,
                                  metadata=None):
    """Canonical chat_log write for a user-visible assistant bubble. Retry-safe."""
    if not event_id:
        return
    try:
        import raw_events
        extra = dict(metadata or {})
        extra.setdefault('assistant_turn_id', event_id)
        extra.setdefault('segment_index', 0)
        raw_events.append_raw_event(
            user_id, character_id,
            event_id=event_id,
            role='assistant',
            content=content,
            metadata=extra,
        )
    except Exception:
        pass


def resolve_voice_proactive_event_id(data):
    """Stable identity for /chat/voice/proactive. Never uses wall-clock minutes."""
    payload = data or {}
    req = str(payload.get('client_request_id') or '').strip()[:120]
    if req:
        return req
    explicit = str(
        payload.get('event_id')
        or payload.get('assistant_event_id')
        or payload.get('source_event_id')
        or ''
    ).strip()[:120]
    if explicit:
        return explicit
    return f'voice_proactive:{uuid.uuid4()}'


def resolve_chat_proactive_event_id(data):
    """Stable identity for /chat/proactive. Never uses content digest + calendar day."""
    payload = data or {}
    req = str(payload.get('client_request_id') or '').strip()[:120]
    if req:
        return req
    occurrence = str(
        payload.get('occurrence_id')
        or payload.get('task_occurrence_id')
        or payload.get('schedule_occurrence_id')
        or ''
    ).strip()[:120]
    if occurrence:
        return occurrence
    explicit = str(
        payload.get('event_id')
        or payload.get('assistant_event_id')
        or payload.get('source_event_id')
        or ''
    ).strip()[:120]
    if explicit:
        return explicit
    task_id = str(payload.get('task_id') or '').strip()
    due = str(
        payload.get('due_date')
        or payload.get('due_date_str')
        or ''
    ).strip()
    mode = str(payload.get('mode') or '').strip()
    if task_id and due and mode:
        return f'proactive:chat:task:{task_id}:{due}:{mode}'[:120]
    return f'proactive:chat:{uuid.uuid4()}'


def _generation_failed_response(user_id: str, character_id: str, total_days=None, attempts=3):
    print(f'[{user_id}][{character_id}] generation_failed after {attempts} attempts; commit skipped')
    body = {
        'error': 'generation_failed',
        'generation_failed': True,
        'messages': [],
    }
    if total_days is not None:
        body['total_days'] = total_days
    return JSONResponse(body, status_code=502)


def _parse_reply(raw: str):
    """把模型回复解析成 JSON。内部状态块会先被剥离。"""
    _, parsed, _ = ingest_model_output(raw)
    return parsed


def _parse_generation(raw: str):
    """只拆模型原文：visible / parsed / state。绝不写数据库。"""
    visible, parsed, state = ingest_model_output(raw or '')
    return parsed, visible, state


def _ingest(raw: str, user_id: str = None, character_id: str = None):
    """兼容旧调用名。parse-only，不再隐式保存 OFFLINE_CHARACTER_STATES。"""
    return _parse_generation(raw)


def _commit_offline_state(user_id, character_id, state):
    if not state:
        return
    try:
        from relationship_state import save_offline_character_state
        save_offline_character_state(user_id, character_id, state)
        print(f'[{user_id}][{character_id}] 已保存 OFFLINE_CHARACTER_STATES '
              f'keys={list(state.keys())}')
    except Exception as e:
        print(f'[{user_id}][{character_id}] 保存 OFFLINE_CHARACTER_STATES 失败: {e}')


def _parsed_ready(parsed, min_messages=1) -> bool:
    if not parsed or not isinstance(parsed.get('messages'), list):
        return False
    if len(parsed['messages']) < min_messages:
        return False
    return all(_valid_msg(m) for m in parsed['messages'])


def _generate_or_none(
    model, max_tokens, system_blocks, messages, *,
    attempts, log_tag, cache_tag, min_messages=1, salvage=False, reject_fn=None,
):
    """LLM → parse → validate → retry。成功返回 (parsed, state)，失败 (None, None)。"""
    result = None
    last_visible = ''
    committed_state = None
    for attempt in range(attempts):
        try:
            raw, response = _create_json(model, max_tokens, system_blocks, messages)
            log_cache_usage(cache_tag, response)
            print(f'[{log_tag}] attempt {attempt+1}: {(raw or "")[:120]}...')
            parsed, visible, state = _parse_generation(raw)
            if visible:
                last_visible = visible
            elif raw:
                last_visible = sanitize_user_reply(raw)
            if _parsed_ready(parsed, min_messages):
                if reject_fn:
                    reason = reject_fn(parsed)
                    if reason:
                        last_visible = ''
                        continue
                result = parsed
                committed_state = state
                break
        except Exception as e:
            print(f'[{log_tag}] attempt {attempt+1} error: {e}')
    if not result and salvage and last_visible:
        salvaged = _salvage_japanese(last_visible)
        if salvaged and _valid_msg(salvaged):
            result = {'emotion': '平静', 'messages': [salvaged]}
            print(f'[{log_tag}] 纯日语救援：{salvaged["jp"][:40]}')
    return result, committed_state


def _finalize_committed(result, min_messages=1):
    """finalize + 最终 commit gate。通过则 (emotion, msgs)，否则 (None, None)。"""
    if not result:
        return None, None
    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'
    msgs = _finalize_msgs(result.get('messages', []))
    if not _commit_ready(msgs) or len(msgs) < min_messages:
        return None, None
    return emotion, msgs


def _finalize_msgs(msgs):
    """发给前端的最后一道清洗。attempt / rescue 都走这里；fallback 已删除，不能绕过 commit gate。"""
    return finalize_user_messages(msgs)


def _safe_reply_to(data):
    reply_to = data.get('reply_to') or data.get('replyTo')
    if not isinstance(reply_to, dict):
        return None
    text = sanitize_user_reply(str(reply_to.get('text') or ''))[:500]
    if not text:
        return None
    return {
        'id': str(reply_to.get('id') or '')[:120],
        'name': str(reply_to.get('name') or '')[:60],
        'text': text,
        'role': str(reply_to.get('role') or '')[:20],
    }


def _user_prompt_with_reply(user_text, reply_to):
    if not reply_to:
        return user_text
    speaker = reply_to.get('name') or '上一条消息'
    return (
        f'【引用回复】她这次是在回复 {speaker} 的这条消息：'
        f'「{reply_to["text"]}」\n'
        f'【她的新消息】{user_text}'
    )


def _prompt_messages(user_id, character_id, short_memories, limit=24):
    """Recent context for the model. Deleted Raw Events are excluded."""
    _pack, messages = _turn_context(
        user_id, character_id, profile='default', limit=limit)
    return messages


def _turn_context(user_id, character_id, user_message='', profile='default',
                  limit=24, current_event_id=None):
    """Hot-context first; fail-closed on source validity; bounded fallback."""
    pack = None
    try:
        from context_layer import build_chat_context
        pack = build_chat_context(
            user_id, character_id,
            user_message=user_message or '',
            profile=profile,
            include_recall=True,
            current_event_id=current_event_id,
        )
        if getattr(pack, 'failed_closed', False):
            return pack, []
        if pack and pack.messages:
            return pack, list(pack.messages)
    except Exception as e:
        try:
            from raw_events import SourceValidityError
            if isinstance(e, SourceValidityError):
                print(
                    f'[{user_id}][{character_id}] prompt history skipped: '
                    f'source validity unknown:{e}')
                return pack, []
        except Exception:
            pass
        print(f'[{user_id}][{character_id}] context_layer history skipped:{e}')
    try:
        from user_memory import get_short_memory_for_prompt
        from raw_events import SourceValidityError
        from context_layer import assemble_fallback_from_messages
        short_rows = list(get_short_memory_for_prompt(
            user_id, n=limit, character_id=character_id) or [])
        fallback_pack = assemble_fallback_from_messages(
            short_rows, user_id=user_id, character_id=character_id, profile=profile)
        return (pack or fallback_pack), list(fallback_pack.messages)
    except Exception as e:
        try:
            from raw_events import SourceValidityError
            if isinstance(e, SourceValidityError):
                print(
                    f'[{user_id}][{character_id}] prompt history skipped: '
                    f'source validity unknown:{e}')
                return pack, []
        except Exception:
            pass
        print(f'[{user_id}][{character_id}] short_memory prompt skipped:{e}')
        return pack, []


def _history_plus_current(messages, content):
    from context_layer import append_current_user_turn
    return append_current_user_turn(messages, content)


def _salvage_japanese(raw: str):
    """从模型没包成 JSON 的原始回复里，抢救出可用的日语当回复。
    用于：模型直接吐日语大白话、没输出 JSON 时，别浪费他真说的话。
    返回 {'jp':..., 'zh':...} 或 None。"""
    import re
    if not raw:
        return None
    text = sanitize_user_reply(raw).strip().strip('`').strip()
    if contains_offline_marker(text):
        print('[salvage] 仍含 OFFLINE_CHARACTER_STATES,放弃救援')
        return None

    json_field_hits = 0
    for kw in ('"jp"', '"zh"', '"messages"', '"emotion"', '"moodshift"', '"anchor"'):
        if kw in text:
            json_field_hits += 1
    if json_field_hits >= 2:
        print(f'[salvage] 检测到 JSON 结构泄露({json_field_hits} 个字段名),放弃救援')
        return None

    text = re.sub(r'^\s*\{?\s*"?(emotion|messages|jp|zh)"?\s*:?', '', text)
    text = text.replace('{', '').replace('}', '').replace('[', '').replace(']', '').strip()
    text = text.strip('"\'，, 。').strip()
    if not text:
        return None
    if not re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', text):
        return None
    for kw in ('"jp"', '"zh"', '"messages"', '"emotion"', '"moodshift"', '"anchor"'):
        if kw in text:
            print(f'[salvage] 救援后仍含 JSON 残骸 {kw},放弃')
            return None
    text = sanitize_user_reply(text)
    # 截断过长的（避免把一堆乱码全塞进去）
    jp = text[:200].strip()
    if not _has_visible_text(jp):
        return None
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
                              core_snippet, recent_ctx, temporal_snapshot,
                              source_event_id=None):
    """子线程：调 process_turn 更新 v4 关系账本。
    ★ 用 sys.stderr 直写 + flush，避免 uvicorn stdout buffering 吞掉子线程输出。
    ★ 所有异常必须自己捕获——子线程报错默认无声。
    """
    import sys
    def _log(msg):
        # stderr 是 line-buffered，Zeabur/Docker 一定能捕获到
        sys.stderr.write(msg + '\n')
        sys.stderr.flush()

    try:
        from relationship_engine import process_turn
        _log(f'[rel_update] start {user_id}/{character_id} source={source_event_id or "-"}')
        result = process_turn(
            user_id=user_id,
            character_id=character_id,
            user_message=user_text,
            character_reply=full_jp,
            character_core_snippet=core_snippet,
            recent_context=recent_ctx,
            temporal_context=temporal_snapshot,
            source_event_id=source_event_id,
        )
        # 精简输出：只打关键数字 + action 列表 + error（不再刷 state summary 满屏）
        sig_n = result.get('signals_extracted', 0)
        app_n = result.get('signals_applied', 0)
        err = result.get('observer_error')
        err_code = str(err).split(':', 1)[0] if err else None
        actions = [a.get('result', {}).get('action', '?')
                   for a in result.get('applied', [])
                   if a.get('result')]
        _log(f'[rel_update] done {user_id}/{character_id} '
             f'signals={sig_n} applied={app_n} '
             f'actions={actions} err={err_code}')
        return result
    except Exception as e:
        _log(f'[rel_update] EXCEPTION {user_id}/{character_id}: '
             f'{type(e).__name__}')
        return {
            'signals_extracted': 0,
            'signals_applied': 0,
            'observer_error': f'process_turn_exception:{type(e).__name__}',
            'applied': [],
        }


def _start_relationship_update(user_id, character_id, user_text, full_jp,
                               char, short_memories, temporal_snapshot=None,
                               source_event_id=None, pack=None):
    """快捷方法：从 handler 里一行调用起 v4 更新线程。
    上下文优先来自 canonical pack，short_memory 只是兼容缓存。"""
    core_snippet = (char.get('core_prompt') or '')[:300]
    if pack is not None and getattr(pack, 'messages', None):
        recent_ctx = [
            {'role': m.get('role'), 'content': m.get('content')}
            for m in list(pack.messages)[-6:]
        ]
    else:
        recent_ctx = [{'role': r, 'content': c} for r, c in (short_memories or [])[-6:]]
    return _fire_relationship_update(
        user_id, character_id, user_text, full_jp,
        core_snippet, recent_ctx, temporal_snapshot, source_event_id,
    )


def _text_effect_ctx(user_id, character_id, source_event_id, payload, extra=None):
    extra = dict(extra or {})
    char = extra.get('char') or get_character(character_id) or {}
    user_text = extra.get('user_text') or (payload or {}).get('_user_text') or ''
    full_jp = extra.get('full_jp') or ' '.join(
        str((m or {}).get('jp') or '') for m in (payload or {}).get('messages') or [])
    short_memories = extra.get('short_memories')
    if short_memories is None:
        short_memories = get_short_memory(user_id, SHORT_MEMORY_MAX, character_id)
    temporal_snapshot = extra.get('temporal_snapshot')
    if temporal_snapshot is None:
        temporal_snapshot = get_temporal_snapshot(user_id, character_id)
    ctx = {
        'user_text': user_text,
        'full_jp': full_jp,
        'msgs': extra.get('msgs') or (payload or {}).get('messages') or [],
        'char': char,
        'pack': extra.get('pack'),
        'short_memories': short_memories,
        'temporal_snapshot': temporal_snapshot,
        'availability': extra.get('availability'),
        'payload': payload,
        'save_short_memory': save_short_memory,
        'record_turn': record_turn,
        'enqueue_private_extraction': enqueue_private_extraction,
        'relationship_fn': lambda: _start_relationship_update(
            user_id, character_id, user_text, full_jp, char, short_memories,
            temporal_snapshot, source_event_id, pack=extra.get('pack')),
    }
    ctx.update(extra)
    return ctx


def _gate_chat_generation(user_id, character_id, source_event_id, endpoint):
    from db_generation_receipt import (
        hydrate_completed_generation_response, in_progress_body, resolve_generation,
    )
    from generation_effects import (
        client_coupled_effects, client_effects_pending_response,
        pending_client_effects, repair_completed_generation,
    )
    gate = resolve_generation(user_id, character_id, source_event_id, endpoint)
    action = gate.get('action')
    if action == 'in_progress':
        return gate, JSONResponse(
            in_progress_body(gate.get('source_event_id')), status_code=202)
    if action == 'replay':
        payload = gate.get('response') or {}
        sid = gate.get('source_event_id')
        effect_ctx = _text_effect_ctx(user_id, character_id, sid, payload)
        repair_completed_generation(
            user_id, character_id, sid, endpoint, payload,
            extra_ctx=effect_ctx)
        pending = pending_client_effects(
            user_id, character_id, sid, endpoint,
            client_coupled_effects(endpoint, payload, effect_ctx))
        if pending:
            return gate, JSONResponse(
                client_effects_pending_response(sid, pending), status_code=202)
        body = hydrate_completed_generation_response(
            user_id, character_id, sid, endpoint, payload=payload)
        return gate, JSONResponse(body)
    return gate, None


def _stamp_text_reply(msgs, source_event_id):
    from db_generation_receipt import ENDPOINT_CHAT_TEXT
    from db_generation_receipt import assistant_turn_id_for, stamp_assistant_messages
    turn_id = assistant_turn_id_for(ENDPOINT_CHAT_TEXT, source_event_id)
    return turn_id, stamp_assistant_messages(msgs, turn_id)


@router.post('/chat/text')
async def chat_text(data: dict):
    from latency_telemetry import LatencyTrace, bind_trace, reset_trace
    import time as _time
    _trace = LatencyTrace('chat:text')
    _tok = bind_trace(_trace)
    _tmark = _time.perf_counter()

    def _latency_emit():
        try:
            _trace.emit()
        finally:
            reset_trace(_tok)

    user_text    = data.get('text', '')
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    from db_generation_receipt import assign_source_event_id
    source_event_id, _legacy = assign_source_event_id(data.get('source_event_id'))
    reply_to = _safe_reply_to(data)

    if not user_text:
        _latency_emit()
        return JSONResponse({'error': 'no input'}, status_code=400)

    char = get_character(character_id)
    if not char:
        _latency_emit()
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    temporal_snapshot = get_temporal_snapshot(user_id, character_id)

    # ★ 角色日程:free / soft_busy / hard_busy。
    #   inbound busy 只写入 char_phone_check inbox，本请求不生成。
    #   到点/活动结束由 delayed_reply worker 走正常 chat pipeline。
    availability = None
    try:
        from reply_availability import check_reply_availability
        availability = check_reply_availability(
            character_id, user_id,
            source_event_id=source_event_id or '',
            pending_text=user_text,
            event_meta={
                'kind': 'text',
                'reply_to': reply_to,
                'source_event_id': source_event_id,
            },
        )
        if not availability.get('can_reply'):
            act = availability.get('activity') or {}
            if availability.get('seen'):
                save_user_short_memory_once(
                    user_id, user_text, character_id,
                    source_event_id=source_event_id)
                record_user_message(
                    user_id, character_id, source='chat_text_seen_busy',
                    prior_snapshot=temporal_snapshot,
                )
            print(f'[{user_id}] {character_id} {availability.get("reply_state")} '
                  f'seen={availability.get("seen")} pending_phone_check_count='
                  f'{availability.get("pending_count")} '
                  f'phone_check_id={availability.get("opportunity_id")}')
            _trace.mark('availability', (_time.perf_counter() - _tmark) * 1000.0)
            _latency_emit()
            return JSONResponse({
                'busy': True,
                'seen': bool(availability.get('seen')),
                'can_reply': False,
                'reply_state': availability.get('reply_state'),
                'activity': act.get('title', ''),
                'location': act.get('location', ''),
                'until': act.get('end_time', ''),
                'free_at': availability.get('free_at'),
                'phone_check_id': availability.get('opportunity_id'),
                'pending_phone_check_count': availability.get('pending_count') or 0,
                'seen_at': (
                    availability.get('seen_at').isoformat()
                    if getattr(availability.get('seen_at'), 'isoformat', None)
                    else availability.get('seen_at')
                ),
                'next_phone_check_at': (
                    availability.get('next_phone_check_at').isoformat()
                    if getattr(availability.get('next_phone_check_at'), 'isoformat', None)
                    else availability.get('next_phone_check_at')
                ),
                'total_days': update_chat_days(user_id),
            })
    except Exception as _e:
        print(f'[{user_id}] 日程检查跳过(不影响聊天):{_e}')
    _trace.mark('availability', (_time.perf_counter() - _tmark) * 1000.0)
    _tmark = _time.perf_counter()

    from db_generation_receipt import ENDPOINT_CHAT_TEXT
    gate, gated = _gate_chat_generation(
        user_id, character_id, source_event_id, ENDPOINT_CHAT_TEXT)
    if gated is not None:
        _latency_emit()
        return gated
    claim_token = gate.get('claim_token')
    source_event_id = gate.get('source_event_id') or source_event_id

    total_days = update_chat_days(user_id)
    # Compatibility cache for relationship observer only — not the prompt fact source.
    short_memories = get_short_memory(user_id, SHORT_MEMORY_MAX, character_id)
    pack, messages = _turn_context(
        user_id, character_id, user_text, profile='text',
        current_event_id=source_event_id)
    messages = _history_plus_current(
        messages, _user_prompt_with_reply(user_text, reply_to))
    if 'hot' not in _trace.marks:
        _trace.mark('hot', (_time.perf_counter() - _tmark) * 1000.0)
    _tmark = _time.perf_counter()

    recall_query = user_text
    if pack and pack.messages:
        tails = [
            m.get('content', '') for m in pack.messages[-2:]
            if isinstance(m.get('content'), str)
        ]
        if tails:
            recall_query = user_text + ' ' + ' '.join(tails)

    system_blocks = build_system_blocks(
        user_id, character_id, recall_query, temporal_snapshot=temporal_snapshot,
        context_pack=pack)
    _trace.mark('prompt', (_time.perf_counter() - _tmark) * 1000.0)
    _tmark = _time.perf_counter()

    save_user_short_memory_once(
        user_id, user_text, character_id, source_event_id=source_event_id)

    def reject_calendar(parsed):
        reply_text = ' '.join(
            f'{m.get("jp", "")} {m.get("zh", "")}'
            for m in parsed['messages']
        )
        calendar_conflict = find_reply_calendar_conflict(
            user_text,
            reply_text,
            now_utc=temporal_snapshot.get('now_utc'),
        )
        if not calendar_conflict:
            return None
        system_blocks.append({
            'type': 'text',
            'text': (
                '上一候选回复违反了后端确定的日历事实，错误代码：'
                f'{calendar_conflict}。必须按“确定性日历锚点”重新生成；'
                '已经过去的今天中午不能当作未来，明天中午也不能改成今天中午。'
            ),
        })
        print(f'[{user_id}][{character_id}] 时间矛盾，拒绝候选并重试：'
              f'{calendar_conflict}')
        return calendar_conflict

    from db_generation_receipt import GenerationHeartbeat
    with GenerationHeartbeat(
            user_id, character_id, source_event_id, ENDPOINT_CHAT_TEXT, claim_token):
        result, committed_state = _generate_or_none(
            MODEL_MAIN, 1500, system_blocks, messages,
            attempts=3,
            log_tag=f'{user_id}][{character_id}',
            cache_tag=f'chat:{character_id}',
            salvage=True,
            reject_fn=reject_calendar,
        )
    llm_ms = (_time.perf_counter() - _tmark) * 1000.0
    _trace.mark('llm', llm_ms)
    _trace.mark('first_token', llm_ms)

    if not result:
        try:
            from db_generation_receipt import fail_generation
            fail_generation(
                user_id, character_id, source_event_id, ENDPOINT_CHAT_TEXT,
                claim_token, last_error='generation_failed')
        except Exception:
            pass
        _latency_emit()
        return _generation_failed_response(user_id, character_id, total_days, attempts=3)

    emotion, msgs = _finalize_committed(result)
    if msgs is None:
        try:
            from db_generation_receipt import fail_generation
            fail_generation(
                user_id, character_id, source_event_id, ENDPOINT_CHAT_TEXT,
                claim_token, last_error='commit_not_ready')
        except Exception:
            pass
        _latency_emit()
        return _generation_failed_response(user_id, character_id, total_days, attempts=3)

    _commit_offline_state(user_id, character_id, committed_state)

    turn_id, msgs = _stamp_text_reply(msgs, source_event_id)
    full_jp = ' '.join(m['jp'] for m in msgs)

    reminder_spec = None
    if result.get('reminder'):
        rem = result['reminder']
        reminder_spec = {
            'date': rem.get('date'),
            'time': rem.get('time'),
            'content': rem.get('content', ''),
            'notification': rem.get('notification', ''),
        }

    pending_tx = _extract_pending_tx(result, user_id, tag='chat')

    resp = {
        'emotion': emotion,
        'messages': msgs,
        'total_days': total_days,
        'assistant_turn_id': turn_id,
        'source_event_id': source_event_id,
        '_user_text': user_text,
    }
    if reminder_spec:
        resp['reminder'] = reminder_spec
    if result.get('cancel_reminder'):
        resp['_cancel_reminder'] = result.get('cancel_reminder')
    if pending_tx:
        resp['pending_transaction'] = pending_tx
    if result.get('proactive_promise'):
        resp['_proactive_promise'] = result.get('proactive_promise')

    # 重要反思只由 Slow Loop diary_entries 写入。这里不再做 per-turn diary judge。
    from generation_effects import (
        client_effects_pending_response, commit_and_run_effects,
        pending_client_effects,
    )
    from db_generation_receipt import hydrate_completed_generation_response
    effect_ctx = _text_effect_ctx(
        user_id, character_id, source_event_id, resp,
        extra={
            'user_text': user_text,
            'full_jp': full_jp,
            'msgs': msgs,
            'char': char,
            'pack': pack,
            'short_memories': short_memories,
            'temporal_snapshot': temporal_snapshot,
            'availability': availability,
        },
    )
    effect_state = commit_and_run_effects(
        user_id, character_id, source_event_id, ENDPOINT_CHAT_TEXT,
        claim_token, resp, ctx=effect_ctx)
    pending = pending_client_effects(
        user_id, character_id, source_event_id, ENDPOINT_CHAT_TEXT,
        effect_state.get('client_effects'))
    if pending:
        _latency_emit()
        return JSONResponse(
            client_effects_pending_response(source_event_id, pending),
            status_code=202)

    live = hydrate_completed_generation_response(
        user_id, character_id, source_event_id, ENDPOINT_CHAT_TEXT, payload=resp)
    print(f'[TTS:{TTS_PROVIDER}] {character_id} emotion={emotion} segs={len(msgs)} days={total_days}')
    _latency_emit()
    return JSONResponse(live)


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

    temporal_snapshot = get_temporal_snapshot(user_id, character_id)
    total_days = update_chat_days(user_id)
    pack, messages = _turn_context(
        user_id, character_id, user_text, profile='story',
        current_event_id=str(data.get('source_event_id') or '').strip() or None)
    messages = _history_plus_current(messages, user_text)

    recall_query = user_text
    if pack and pack.messages:
        tails = [
            m.get('content', '') for m in pack.messages[-2:]
            if isinstance(m.get('content'), str)
        ]
        if tails:
            recall_query = user_text + ' ' + ' '.join(tails)

    system_blocks = build_system_blocks(
        user_id, character_id, recall_query, extra_suffix=STORY_SCENE,
        temporal_snapshot=temporal_snapshot, context_pack=pack)

    source_event_id = str(data.get('source_event_id') or '').strip() or None
    save_user_short_memory_once(
        user_id, user_text, character_id, source_event_id=source_event_id)

    result, committed_state = _generate_or_none(
        MODEL_MAIN, 4000, system_blocks, messages,
        attempts=5,
        log_tag=f'story:{character_id}',
        cache_tag=f'story:{character_id}',
    )
    emotion, msgs = _finalize_committed(result)
    if msgs is None:
        return _generation_failed_response(user_id, character_id, total_days, attempts=5)

    _commit_offline_state(user_id, character_id, committed_state)

    full_jp = ' '.join(m['jp'] for m in msgs)
    # Frontend owns chat_log segments (`{source}:reply:{i}`). This is cache only.
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    record_turn(
        user_id, character_id, source='chat_story',
        prior_snapshot=temporal_snapshot,
    )
    enqueue_private_extraction(
        user_id, user_text, full_jp, character_id,
        temporal_context=temporal_snapshot,
        source_event_id=source_event_id,
    )

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

    temporal_snapshot = get_temporal_snapshot(user_id, character_id)
    if mode == 'remind':
        trigger = f'【系统触发：到提醒时间了】现在该主动提醒对方去做这件事："{task_title}"。语气慵懒又带点关心，1条气泡。'
    else:
        trigger = f'【系统触发：超时未完成】对方之前要做"{task_title}"，已经过了时间没动静。主动问她做完了没，带点调侃或假装不在意的关心，1条气泡。'

    short_memories = get_short_memory(user_id, 4, character_id)
    pack, messages = _turn_context(
        user_id, character_id, task_title, profile='proactive')
    if pack and getattr(pack, 'failed_closed', False):
        messages = []
    elif messages:
        messages = list(messages)
    else:
        messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages = _history_plus_current(messages, trigger)

    system_blocks = build_system_blocks(
        user_id, character_id, task_title, temporal_snapshot=temporal_snapshot,
        context_pack=pack)

    result, committed_state = _generate_or_none(
        MODEL_MAIN, 400, system_blocks, messages,
        attempts=3,
        log_tag=f'proactive:{character_id}',
        cache_tag=f'proactive:{character_id}',
    )
    emotion, msgs = _finalize_committed(result)
    if msgs is None:
        print(f'[{user_id}][{character_id}] proactive generation_failed mode={mode} task={task_title}')
        return _generation_failed_response(user_id, character_id, attempts=3)

    _commit_offline_state(user_id, character_id, committed_state)

    full_jp = ' '.join(m['jp'] for m in msgs)
    event_id = resolve_chat_proactive_event_id(data)
    save_short_memory(
        user_id, 'assistant', full_jp, character_id,
        source_event_id=event_id,
    )
    _append_visible_assistant_raw(
        user_id, character_id, full_jp, event_id,
        metadata={'proactive_kind': f'chat_{mode}', 'task_title': task_title},
    )
    record_assistant_message(
        user_id, character_id, source=f'chat_proactive:{mode}',
        prior_snapshot=temporal_snapshot,
    )

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[proactive] {character_id} mode={mode} task={task_title}')
    return JSONResponse({
        'emotion': emotion,
        'messages': msgs,
        'assistant_turn_id': event_id,
        'event_id': event_id,
    })


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

    temporal_snapshot = get_temporal_snapshot(user_id, character_id)
    source_event_id = str(data.get('source_event_id') or '').strip() or None
    pack, messages = _turn_context(
        user_id, character_id, user_text, profile='voice',
        current_event_id=source_event_id)
    messages = _history_plus_current(messages, user_text)

    system_blocks = build_system_blocks(
        user_id, character_id, user_text, extra_suffix=VOICE_CALL_SCENE,
        temporal_snapshot=temporal_snapshot, context_pack=pack)

    save_user_short_memory_once(
        user_id, user_text, character_id, source_event_id=source_event_id)

    result, committed_state = _generate_or_none(
        MODEL_JP_AUX, 500, system_blocks, messages,
        attempts=3,
        log_tag=f'voice:{character_id}',
        cache_tag=f'voice:{character_id}',
        salvage=True,
    )
    emotion, msgs = _finalize_committed(result)
    if msgs is None:
        return _generation_failed_response(user_id, character_id, attempts=3)

    _commit_offline_state(user_id, character_id, committed_state)

    full_jp = ' '.join(m['jp'] for m in msgs)
    # Frontend owns chat_log segments (`{source}:reply:{i}`). This is cache only.
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    record_turn(
        user_id, character_id, source='chat_voice_text',
        prior_snapshot=temporal_snapshot,
    )
    enqueue_private_extraction(
        user_id, user_text, full_jp, character_id,
        temporal_context=temporal_snapshot,
        source_event_id=source_event_id,
    )

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

    temporal_snapshot = get_temporal_snapshot(user_id, character_id)
    source_event_id = str(data.get('source_event_id') or '').strip() or None
    pack, messages = _turn_context(
        user_id, character_id, user_text, profile='voice',
        current_event_id=source_event_id)
    messages = _history_plus_current(messages, user_text)

    system_blocks = build_system_blocks(
        user_id, character_id, user_text, extra_suffix=VOICE_STORY_SCENE,
        temporal_snapshot=temporal_snapshot, context_pack=pack)

    save_user_short_memory_once(
        user_id, user_text, character_id, source_event_id=source_event_id)

    result, committed_state = _generate_or_none(
        MODEL_MAIN, 3000, system_blocks, messages,
        attempts=5,
        log_tag=f'voice_story:{character_id}',
        cache_tag=f'voice_story:{character_id}',
        min_messages=3,
    )
    emotion, msgs = _finalize_committed(result, min_messages=3)
    if msgs is None:
        return _generation_failed_response(user_id, character_id, attempts=5)

    _commit_offline_state(user_id, character_id, committed_state)

    full_jp = ' '.join(m['jp'] for m in msgs)
    # Frontend owns chat_log segments (`{source}:reply:{i}`). This is cache only.
    save_short_memory(user_id, 'assistant', full_jp, character_id)
    record_turn(
        user_id, character_id, source='chat_voice_story',
        prior_snapshot=temporal_snapshot,
    )
    enqueue_private_extraction(
        user_id, user_text, full_jp, character_id,
        temporal_context=temporal_snapshot,
        source_event_id=source_event_id,
    )

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

    temporal_snapshot = get_temporal_snapshot(user_id, character_id)
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

    pack, messages = _turn_context(
        user_id, character_id, '', profile='voice')
    if pack and getattr(pack, 'failed_closed', False):
        messages = []
    elif messages:
        messages = list(messages)
    else:
        messages = [
            {'role': r, 'content': c}
            for r, c in get_short_memory(user_id, n_recent, character_id)
        ]
    messages = _history_plus_current(messages, trigger)

    system_blocks = build_system_blocks(
        user_id, character_id, '', extra_suffix=scene,
        temporal_snapshot=temporal_snapshot,
        context_pack=pack,
    )

    result, committed_state = _generate_or_none(
        MODEL_JP_AUX, 300, system_blocks, messages,
        attempts=3,
        log_tag=f'voice_proactive:{character_id}',
        cache_tag=f'voice_proactive:{character_id}',
    )
    emotion, msgs = _finalize_committed(result)
    if msgs is None:
        print(f'[{user_id}][{character_id}] voice_proactive generation_failed mode={mode}')
        return _generation_failed_response(user_id, character_id, attempts=3)

    msgs = msgs[:2] if mode == 'greeting' else msgs[:1]
    if not _commit_ready(msgs):
        return _generation_failed_response(user_id, character_id, attempts=3)

    _commit_offline_state(user_id, character_id, committed_state)

    full_jp = ' '.join(m['jp'] for m in msgs)
    event_id = resolve_voice_proactive_event_id(data)
    save_short_memory(
        user_id, 'assistant', full_jp, character_id,
        source_event_id=event_id,
    )
    _append_visible_assistant_raw(
        user_id, character_id, full_jp, event_id,
        metadata={
            'proactive_kind': f'voice_{mode}',
            'silence_seconds': silence_seconds,
        },
    )
    record_assistant_message(
        user_id, character_id, source=f'chat_voice_proactive:{mode}',
        prior_snapshot=temporal_snapshot,
    )

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    print(f'[voice_proactive] {character_id} mode={mode} silence={silence_seconds}s')
    return JSONResponse({
        'emotion': emotion,
        'messages': msgs,
        'assistant_turn_id': event_id,
        'event_id': event_id,
    })


# ─────────────────── Whisper 转录 ───────────────────

@router.post('/transcribe')
async def transcribe(data: dict):
    audio_b64 = data.get('audio_base64', '')
    if not audio_b64:
        return JSONResponse({'error': 'no audio'}, status_code=400)
    result = transcribe_audio_b64(audio_b64)
    return JSONResponse(result)
