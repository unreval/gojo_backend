"""流式语音通话 endpoint /chat/voice_stream (B档半流式)

流程:
1. 客户端 POST { text, user_id, character_id }
2. 服务器建 AsyncAnthropic 流,LLM 边生成边输出 token
3. 服务器按行解析:遇到完整的 JP + ZH 一对 → 立刻用线程池调 Fish TTS 合成
4. 每合成完一句 → 立刻 yield 一个 NDJSON 事件推给客户端
5. 客户端边收边播,首字延迟从"生成完 + 全部 TTS 完"变成"第一句生成完 + 第一句 TTS 完"

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
import anthropic
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from config import ANTHROPIC_KEY, EMOTIONS, DEFAULT_CHARACTER_ID, MODEL_JP_AUX
from tts import tts_to_b64
from prompt import build_system_blocks
from user_memory import save_short_memory, save_user_short_memory_once, get_short_memory
from memory_jobs import enqueue_private_extraction
from characters import get_character
from temporal_awareness import get_temporal_snapshot, record_turn
from utils import has_visible_text, valid_reply_pair

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
    source_event_id = str(data.get('source_event_id') or '').strip() or None

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

    voice_id = char.get('voice_id')
    temporal_snapshot = get_temporal_snapshot(user_id, character_id)

    # Commit before reading: history includes this event once, even on retries.
    save_user_short_memory_once(
        user_id, user_text, character_id, source_event_id=source_event_id,
    )
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
        )
        if getattr(pack, 'failed_closed', False):
            failed_closed = True
            messages = []
        elif pack and pack.messages:
            messages = list(pack.messages)
    except Exception as exc:
        print(f'[{user_id}][{character_id}] voice context_layer skipped:{exc}')
    if not messages and not failed_closed:
        short_memories = get_short_memory(user_id, 6, character_id)
        messages = [{'role': r, 'content': c} for r, c in short_memories]
    from context_layer import append_current_user_turn
    messages = append_current_user_turn(messages, user_text)

    system_blocks = build_system_blocks(
        user_id, character_id, user_text, extra_suffix=VOICE_STREAM_SCENE,
        temporal_snapshot=temporal_snapshot,
        context_pack=pack,
    )

    async def event_stream():
        emotion = '平静'
        buffer = ''
        current_jp = ''
        seq = 0
        all_jps = []  # 结束时保存 short_memory 用
        loop = asyncio.get_event_loop()

        async def _process_line(line: str):
            """处理一整行输入,如果拿到完整 JP+ZH 就 TTS + yield。
            用列表返回 yield 内容,交由外层 yield(内嵌 async gen 太绕)。"""
            nonlocal emotion, current_jp, seq
            out = []
            parsed = _parse_line(line)
            if not parsed:
                return out
            tag, value = parsed
            if tag == 'EMOTION':
                if value in EMOTIONS:
                    emotion = value
                return out
            if tag == 'JP':
                current_jp = value
                # 纯标点不预显示；有效短句（ん？）可以预显示
                if has_visible_text(value):
                    out.append(json.dumps({'type': 'text_jp', 'jp': value}) + '\n')
                return out
            if tag == 'ZH':
                # 有 JP 才可以合成；segment-level gate：过校验才 TTS / yield / 持久化
                if not current_jp:
                    return out
                jp_to_tts = current_jp
                zh_final = value
                current_jp = ''
                if not valid_reply_pair(jp_to_tts, zh_final):
                    print(f'[voice_stream] skip invalid pair jp={jp_to_tts!r} zh={zh_final!r}')
                    return out
                try:
                    audio_b64 = await loop.run_in_executor(
                        None, tts_to_b64, jp_to_tts, emotion, voice_id
                    )
                except Exception as e:
                    print(f'[voice_stream] TTS 出错:{e}')
                    audio_b64 = ''
                out.append(json.dumps({
                    'type': 'audio',
                    'seq': seq,
                    'jp': jp_to_tts,
                    'zh': zh_final,
                    'emotion': emotion,
                    'audio_b64': audio_b64,
                }) + '\n')
                all_jps.append(jp_to_tts)
                seq += 1
                return out
            return out

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
                        outs = await _process_line(line)
                        for o in outs:
                            yield o.encode()

            # 流结束后 flush 剩余 buffer
            if buffer.strip():
                outs = await _process_line(buffer.strip())
                for o in outs:
                    yield o.encode()
        except Exception as e:
            print(f'[voice_stream] 主流程出错:{e}')
            yield (json.dumps({'type': 'error', 'msg': str(e)}) + '\n').encode()

        if not all_jps:
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
        enqueue_private_extraction(
            user_id, user_text, full_jp, character_id,
            temporal_context=temporal_snapshot,
            source_event_id=source_event_id,
            assistant_event_id=assistant_event_id,
        )
        print(f'[voice_stream] ✅ {character_id} 流式回复完成,共 {seq} 段')

    return StreamingResponse(event_stream(), media_type='application/x-ndjson')
