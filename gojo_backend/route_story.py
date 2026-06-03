"""睡前故事模块（独立于聊天与记忆）：/story/generate

特点：
- 不写入 short_memory（聊天历史）
- 不写入 long_memory（长期记忆）
- 不调用 update_chat_days
- 只借用角色的核心人格（core_prompt）来保证是"五条在讲故事"
和聊天、记忆系统完全隔离，互不影响。
"""
import re
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, DEFAULT_CHARACTER_ID
from utils import extract_json, sanitize_jp
from tts import tts_to_b64
from characters import get_character

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

STORY_MAX_JP = 120  # 单段日语超过这个长度，就按句子再切，保证 TTS 质量


def _split_jp_sentences(text: str, max_chars: int = STORY_MAX_JP):
    """把过长的日语按句末标点（。！？!?）切成多段，每段不超过 max_chars。短句原样返回。"""
    text = (text or '').strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts = re.split(r'(?<=[。！？!?])', text)
    chunks, cur = [], ''
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) > max_chars and cur:
            chunks.append(cur)
            cur = p
        else:
            cur += p
    if cur:
        chunks.append(cur)
    return chunks or [text]


@router.post('/story/generate')
async def story_generate(data: dict):
    """
    睡前故事：生成一篇完整长故事，按句子切成多段，逐段 TTS（同一声音、串行）。
    返回 segments 列表，前端按顺序播放、显示中文字幕。
    """
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    theme        = (data.get('theme') or '').strip()   # 可选：故事主题；不传就自由发挥

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    theme_line = f'故事主题：{theme}。' if theme else '主题自由发挥，温馨治愈即可。'

    # ★ 只用角色核心人格，不接聊天/记忆/提醒那套脚手架 —— 保证隔离
    system_prompt = char.get('core_prompt', '') + f'''

【★ 睡前故事模式】
现在是睡前，对方想听你（五条悟）讲一个故事哄她入睡。{theme_line}
1. 语气温柔、舒缓、慵懒，适合入睡，不要激烈或紧张的情节。
2. 故事完整：温柔的开头、舒缓的发展、平静温暖的结尾。
3. 分成 12-20 个气泡，每个气泡只讲一小句，像轻声细语。
4. 每个气泡的【日语】控制在 40-100 字以内。
5. jp 必须是纯日语，zh 是对应中文翻译，不要把中文混进 jp。

严格按这个 JSON 返回：
{{"emotion":"温柔","messages":[{{"jp":"第一句日语","zh":"第一句中文"}},{{"jp":"第二句日语","zh":"第二句中文"}}]}}'''

    messages = [{'role': 'user', 'content': '给我讲一个睡前故事吧。'}]

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=4000,
                system=system_prompt,
                messages=messages,
            )
            raw = response.content[0].text.strip()
            print(f'[story] attempt {attempt+1}: {raw[:120]}...')
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) >= 3:
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'[story] attempt {attempt+1} error: {e}')

    if not result:
        result = {
            'emotion': '温柔',
            'messages': [
                {'jp': 'まあ、特別に話を聞かせてあげる。', 'zh': '嘛，特别讲个故事给你听吧。'},
                {'jp': '昔々、静かな夜の街にね。', 'zh': '很久很久以前，在一座安静的夜晚的城市里。'},
                {'jp': 'ゆっくり目を閉じて、聞いてて。', 'zh': '慢慢闭上眼睛，听着就好。'},
            ],
        }

    emotion = result.get('emotion', '温柔')
    if emotion not in EMOTIONS:
        emotion = '平静'

    voice_id = char.get('voice_id')

    # ★ 把每个气泡按句子切成 TTS 友好的小段，逐段合成（串行、同 voice_id，声音一致）
    segments = []
    for m in result.get('messages', []):
        jp_clean = sanitize_jp(m.get('jp', ''))
        zh = (m.get('zh') or '').strip()
        sub_chunks = _split_jp_sentences(jp_clean)
        for idx, chunk in enumerate(sub_chunks):
            audio = tts_to_b64(chunk, emotion, voice_id)
            segments.append({
                'jp': chunk,
                'zh': zh if idx == 0 else '',   # 字幕跟随原句；被切出来的后半段不重复显示
                'audio_b64': audio,
            })

    total_chars = sum(len(s['jp']) for s in segments)
    print(f'[story] {character_id} emotion={emotion} segments={len(segments)} chars={total_chars}')

    # 注意：这里故意不调用 save_short_memory / extract_and_save_memory / update_chat_days
    return JSONResponse({
        'emotion': emotion,
        'segments': segments,
        'total_chars': total_chars,
    })
