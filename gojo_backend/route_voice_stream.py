"""流式语音通话 endpoint /chat/voice_stream (B档半流式)

流程:
1. 客户端 POST { text, user_id, character_id }
2. 服务器建 AsyncAnthropic 流,按行收集完整的 JP + ZH 对
3. 整轮候选经过日历、时钟和日程校验后，才开始合成语音
4. 校验通过后逐句 yield NDJSON 文本和音频事件

返回 NDJSON:每行一个独立的 JSON 事件对象。
事件类型:
- {"type":"text_jp","jp":"..."}    LLM 吐出的日语句子(供字幕预显示,可选)
- {"type":"audio","seq":0,"jp":"...","zh":"...","emotion":"...","audio_b64":"..."}
                                      TTS 生成好的一段音频(前端入播放队列)
- {"type":"done","emotion":"...","segments":3}   结束事件
- {"type":"error","msg":"..."}    出错
- {"type":"generation_failed","error":"generation_failed","generation_failed":true}
                                      整轮没有任何有效 JP/ZH pair，不伪造台词

★ 兼容策略:老的 /chat/voice_text 保留不动,前端可以自由切换。
"""
import asyncio
import json
import re
import time
import anthropic
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from config import ANTHROPIC_KEY, EMOTIONS, DEFAULT_CHARACTER_ID, MODEL_JP_AUX
from tts import tts_to_b64
from prompt import build_system_blocks
from user_memory import save_short_memory, save_user_short_memory_once, get_short_memory
from characters import get_character
from temporal_awareness import get_temporal_snapshot, record_turn
from utils import valid_reply_pair

router = APIRouter()

# ★ 用 AsyncAnthropic 才能在 async generator 里流式迭代
async_claude = anthropic.AsyncAnthropic(api_key=ANTHROPIC_KEY)


# 让 LLM 用行标记格式输出,方便流式解析。绝不用 JSON——
# JSON 只有全部生成完才可解析,和流式冲突。
VOICE_STREAM_SCENE = '''

【★ 语音通话·流式输出场景】
现在你和对方在语音通话中,你说的每一句话【会马上被合成语音播放】。
所以你的输出格式很特殊,必须严格遵守。

【输出格式——非常严格】
按照下面这个特殊格式输出(不要 JSON、不要标签、不要引号、不要解释):

EMOTION: (情绪,只写一个词,如"平静"/"调皮"/"温柔"/"认真")
JP: (第一句日语)
ZH: (第一句的中文翻译)
JP: (第二句日语,如果有的话)
ZH: (第二句的中文翻译)

每句 JP 后必须【紧跟】一句 ZH,配对出现。
每句 10-40 字,口语化,不要长篇。
短寒暄 1 句即可;有内容的对话 2-3 句就够,别超过 4 句。

【好例子】
EMOTION: 平静
JP: へえ、そんなに使ったの
ZH: 喔,花了这么多

【坏例子——严格禁止】
{"emotion":"..."}  ← 禁止 JSON
【日语】...        ← 禁止其他标签
"喔,花了这么多"    ← 禁止只有 ZH 没有 JP
'''


def _parse_line(line: str):
    """从一行文本里抽出 (tag, value) 或 None。
    支持半角/全角冒号,大小写。"""
    m = re.match(r'^\s*(EMOTION|JP|ZH|emotion|jp|zh)\s*[::]\s*(.*)$', line)
    if not m:
        return None
    return m.group(1).upper(), m.group(2).strip()


@router.post('/chat/voice_stream')
async def chat_voice_stream(data: dict):
    user_text = (data.get('text') or '').strip()
    user_id = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    from db_generation_receipt import assign_source_event_id
    source_event_id, _legacy = assign_source_event_id(data.get('source_event_id'))

    def _err(msg: str):
        return StreamingResponse(
            iter([(json.dumps({'type': 'error', 'msg': msg}) + '\n').encode()]),
            media_type='application/x-ndjson',
        )

    if not user_text:
        return _err('no input')
    char = get_character(character_id)
    if not char:
        return _err(f'character {character_id} not found')

    temporal_snapshot = get_temporal_snapshot(user_id, character_id)
    snapshot_started = time.monotonic()
    availability = None

    # Streaming voice is an inbound chat surface, so it enters through the
    # same canonical availability/phone-check gate as text and image.
    try:
        from reply_availability import check_reply_availability
        availability = check_reply_availability(
            character_id, user_id,
            source_event_id=source_event_id or '',
            pending_text=user_text,
            now=temporal_snapshot.get('now_local'),
            event_meta={
                'kind': 'voice_stream',
                'source_event_id': source_event_id or '',
            },
        )
        if not availability.get('can_reply'):
            act = availability.get('activity') or {}
            return StreamingResponse(
                iter([(json.dumps({
                    'type': 'busy',
                    'busy': True,
                    'seen': bool(availability.get('seen')),
                    'can_reply': False,
                    'reply_state': availability.get('reply_state'),
                    'activity': act.get('title', ''),
                    'location': act.get('location', ''),
                    'until': act.get('end_time', ''),
                    'phone_check_id': availability.get('opportunity_id'),
                }) + '\n').encode()]),
                media_type='application/x-ndjson',
            )
    except Exception as exc:
        print(f'[{user_id}] voice_stream schedule check skipped:{exc}')

    voice_id = char.get('voice_id')

    # Commit before reading: history includes this event once, even on retries.
    save_user_short_memory_once(
        user_id, user_text, character_id, source_event_id=source_event_id,
    )
    if availability and availability.get('can_reply'):
        try:
            from db_read_receipt import mark_immediate_seen
            mark_immediate_seen(user_id, character_id, source_event_id)
        except Exception as exc:
            print(f'[read_receipt] immediate voice failed source_event_id={source_event_id} '
                  f'error={type(exc).__name__}')
            return _err('read_receipt_unavailable')
    pack = None
    messages = []
    failed_closed = False
    try:
        from context_layer import build_chat_context
        pack = build_chat_context(
            user_id, character_id,
            user_message=user_text,
            profile='voice',
            include_recall=True,
            current_event_id=source_event_id,
            temporal_snapshot=temporal_snapshot,
            now=temporal_snapshot.get('now_utc'),
        )
        if getattr(pack, 'failed_closed', False):
            failed_closed = True
            messages = []
        elif pack and pack.messages:
            messages = list(pack.messages)
    except Exception as exc:
        print(f'[{user_id}][{character_id}] voice context_layer skipped:{exc}')
    if not messages and not failed_closed:
        try:
            from context_layer import assemble_fallback_from_messages
            short_memories = get_short_memory(user_id, 6, character_id)
            fallback_pack = assemble_fallback_from_messages(
                short_memories, user_id=user_id, character_id=character_id, profile='voice')
            pack = pack or fallback_pack
            messages = list(fallback_pack.messages)
        except Exception as exc:
            print(f'[{user_id}][{character_id}] voice fallback budget skipped:{exc}')
            short_memories = get_short_memory(user_id, 6, character_id)
            messages = [{'role': r, 'content': c} for r, c in short_memories]
    from context_layer import append_current_user_turn
    messages = append_current_user_turn(messages, user_text)

    system_blocks = build_system_blocks(
        user_id, character_id, user_text, extra_suffix=VOICE_STREAM_SCENE,
        temporal_snapshot=temporal_snapshot,
        context_pack=pack,
    )
    system_blocks = system_blocks + [{
        'type': 'text',
        'text': (
            '这是流式语音格式，不能输出结构化 schedule_action_intent。因此绝不能'
            '声称任何仍为 active 的日程已经结束，也不能用台词改地点或日程。'
        ),
    }]

    async def event_stream():
        emotion = '平静'
        buffer = ''
        current_jp = ''
        seq = 0
        pairs = []
        all_jps = []
        loop = asyncio.get_event_loop()

        async def _process_line(line: str):
            """Stage complete pairs until the entire candidate passes validation."""
            nonlocal emotion, current_jp
            parsed = _parse_line(line)
            if not parsed:
                return
            tag, value = parsed
            if tag == 'EMOTION':
                if value in EMOTIONS:
                    emotion = value
                return
            if tag == 'JP':
                current_jp = value
                return
            if tag == 'ZH':
                if not current_jp:
                    return
                jp = current_jp
                current_jp = ''
                if valid_reply_pair(jp, value):
                    pairs.append({'jp': jp, 'zh': value})
                else:
                    print(f'[voice_stream] skip invalid pair jp={jp!r} zh={value!r}')

        try:
            async with async_claude.messages.stream(
                model=MODEL_JP_AUX,
                max_tokens=500,
                system=system_blocks,
                messages=messages,
            ) as stream:
                async for text_delta in stream.text_stream:
                    buffer += text_delta
                    # 按行处理,遇到完整一行就吃掉
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        line = line.strip()
                        if not line:
                            continue
                        await _process_line(line)

            # 流结束后 flush 剩余 buffer
            if buffer.strip():
                await _process_line(buffer.strip())
        except Exception as e:
            print(f'[voice_stream] 主流程出错:{e}')
            yield (json.dumps({'type': 'error', 'msg': str(e)}) + '\n').encode()
            return

        if not pairs:
            print(f'[voice_stream] ⚠️ {character_id} 无有效 JP+ZH pair，generation_failed')
            yield (json.dumps({
                'type': 'generation_failed',
                'error': 'generation_failed',
                'generation_failed': True,
                'messages': [],
                'segments': 0,
            }) + '\n').encode()
            yield (json.dumps({
                'type': 'done', 'emotion': emotion, 'segments': 0,
                'generation_failed': True,
            }) + '\n').encode()
            return

        from temporal_awareness import (
            find_commit_clock_conflict, find_reply_calendar_conflict)
        candidate_text = ' '.join(
            f'{pair["jp"]} {pair["zh"]}' for pair in pairs)
        conflict = find_reply_calendar_conflict(
            user_text, candidate_text,
            now_utc=temporal_snapshot.get('now_utc'))
        if not conflict:
            conflict = find_commit_clock_conflict(
                user_text, candidate_text, temporal_snapshot,
                time.monotonic() - snapshot_started)
        if not conflict:
            try:
                import db_schedule
                from schedule_contract import completion_claim_conflict
                world = db_schedule.get_current_world_state(
                    character_id, user_id, temporal_snapshot.get('now_local'))
                if completion_claim_conflict(pairs, world, None):
                    conflict = 'schedule_truth_conflict'
            except Exception:
                conflict = 'schedule_truth_unavailable'
        if conflict:
            yield (json.dumps({
                'type': 'generation_failed', 'error': conflict,
                'generation_failed': True, 'messages': [], 'segments': 0,
            }) + '\n').encode()
            return

        # Validate the complete candidate before any subtitle or audio is sent.
        for pair in pairs:
            yield (json.dumps({'type': 'text_jp', 'jp': pair['jp']}) + '\n').encode()
            try:
                audio_b64 = await loop.run_in_executor(
                    None, tts_to_b64, pair['jp'], emotion, voice_id)
            except Exception as e:
                print(f'[voice_stream] TTS 出错:{e}')
                audio_b64 = ''
            yield (json.dumps({
                'type': 'audio', 'seq': seq,
                'jp': pair['jp'], 'zh': pair['zh'],
                'emotion': emotion, 'audio_b64': audio_b64,
            }) + '\n').encode()
            all_jps.append(pair['jp'])
            seq += 1

        # 已通过 gate 并 yield 过的 segment 是真实角色行为，即使后续流失败也要持久化
        yield (json.dumps({
            'type': 'done', 'emotion': emotion, 'segments': seq,
        }) + '\n').encode()
        full_jp = ' '.join(all_jps)
        assistant_event_id = (
            f'{source_event_id}:reply' if source_event_id else None
        )
        try:
            save_short_memory(
                user_id, 'assistant', full_jp, character_id,
                source_event_id=assistant_event_id,
            )
            record_turn(
                user_id, character_id, source='chat_voice_stream',
                prior_snapshot=temporal_snapshot,
            )
        except Exception as e:
            print(f'[voice_stream] short_memory 保存失败:{e}')
        print(f'[voice_stream] ✅ {character_id} 流式回复完成,共 {seq} 段')

    return StreamingResponse(event_stream(), media_type='application/x-ndjson')
