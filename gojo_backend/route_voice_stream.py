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

★ 兼容策略:老的 /chat/voice_text 保留不动,前端可以自由切换。
"""
import asyncio
import json
import re
import threading
import anthropic
from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from config import ANTHROPIC_KEY, EMOTIONS, DEFAULT_CHARACTER_ID
from tts import tts_to_b64
from prompt import build_system_blocks
from user_memory import save_short_memory, get_short_memory, extract_and_save_memory
from characters import get_character

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
    short_memories = get_short_memory(user_id, 6, character_id)
    messages = [{'role': r, 'content': c} for r, c in short_memories]
    messages.append({'role': 'user', 'content': user_text})

    system_blocks = build_system_blocks(
        user_id, character_id, user_text, extra_suffix=VOICE_STREAM_SCENE
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
                out.append(json.dumps({'type': 'text_jp', 'jp': value}) + '\n')
                return out
            if tag == 'ZH':
                # 有 JP 才可以合成
                if not current_jp:
                    return out
                jp_to_tts = current_jp
                zh_final = value
                current_jp = ''
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
                model='claude-haiku-4-5-20251001',
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

            # done 事件
            yield (json.dumps({
                'type': 'done', 'emotion': emotion, 'segments': seq,
            }) + '\n').encode()

            # 后台保存短期记忆 + 提取长期记忆
            if all_jps:
                full_jp = ' '.join(all_jps)
                try:
                    save_short_memory(user_id, 'user', user_text, character_id)
                    save_short_memory(user_id, 'assistant', full_jp, character_id)
                except Exception as e:
                    print(f'[voice_stream] short_memory 保存失败:{e}')
                threading.Thread(
                    target=extract_and_save_memory,
                    args=(user_id, user_text, full_jp, character_id),
                    daemon=True,
                ).start()
                print(f'[voice_stream] ✅ {character_id} 流式回复完成,共 {seq} 段')
            else:
                print(f'[voice_stream] ⚠️ {character_id} 流式回复没抓到任何 JP+ZH 对,可能 LLM 格式漂了')

        except Exception as e:
            print(f'[voice_stream] 主流程出错:{e}')
            yield (json.dumps({'type': 'error', 'msg': str(e)}) + '\n').encode()

    return StreamingResponse(event_stream(), media_type='application/x-ndjson')
