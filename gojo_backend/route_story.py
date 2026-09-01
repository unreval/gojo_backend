"""睡前故事模块（独立于聊天与记忆）—— 两段式

/story/generate  ：只生成故事文字（一次 Claude 调用，很快，不做 TTS），返回分好的段落
/story/tts       ：给一段日语，单独合成一段语音并返回（前端按需逐段调用）

这样长故事不会因为"一次性合成十几段语音太久"而超时。
不写入聊天历史 / 长期记忆 / 聊天天数 —— 与聊天、记忆系统完全隔离。
"""
import re
import random
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, DEFAULT_CHARACTER_ID
from utils import extract_json, sanitize_jp
from ai_client import extract_text
from tts import tts_to_b64
from characters import get_character

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

STORY_MAX_JP = 80        # ★ 单段日语超过这个长度，就按句子再切（调短了，语音更稳，不易跑偏）
SEGMENT_PAUSE_MS = 1500  # ★ 每段之间停顿多少毫秒（想更慢就调大，比如 2200；更快就调小）

# ★ 随机主题池：每次没指定主题就随机抽一个，避免老讲同一个故事
STORY_THEMES = [
    # —— 格林 / 经典童话改编（用悟的口吻重讲，可以偶尔吐槽，但结尾温柔）——
    '用你的口吻重讲格林童话《小红帽》，加一点你慵懒的吐槽，但结尾温柔',
    '用你的口吻重讲《白雪公主》，偶尔忍不住吐槽剧情',
    '用你的口吻重讲《灰姑娘》，结尾温柔一点',
    '用你的口吻重讲《青蛙王子》',
    '用你的口吻重讲《糖果屋》（汉赛尔与格莱特）',
    '用你的口吻重讲《不来梅的城市乐手》',
    '用你的口吻重讲《睡美人》',
    '用你的口吻重讲《穿靴子的猫》',
    '用你的口吻重讲《杰克与魔豆》',
    '用你的口吻重讲《丑小鸭》',
    '用你的口吻重讲《拇指姑娘》',
    '用你的口吻重讲《卖火柴的小女孩》，但把结局改得温暖一些',
    # —— 原创治愈系 ——
    '讲一个关于深夜便利店和一只会说话的猫的原创故事',
    '讲一个迷路的小星星想回家、被路灯一盏盏接力送回天上的故事',
    '讲一个住在甜品店楼上的小妖怪，偷偷帮人留住最后一块蛋糕的故事',
    '讲一个冬天第一场雪和一盏永远不灭的旧路灯的故事',
    '讲一个海边灯塔守护人和一条总来串门的小鱼的故事',
    '讲一个会迷路的云朵，在夜空里寻找朋友的故事',
    '讲一个旧书店里的书，趁夜里没人时偷偷聊天的故事',
    '讲一个总是睡不着的小孩，在屋顶遇见月亮的故事',
    '讲一个森林里负责收集大家做过的梦、再轻轻还回去的小狐狸的故事',
    '讲一个钟表店里一只走得最慢的怀表，陪一个老人慢慢过日子的故事',
]


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


# ─────────────────── 第一步：只生成文字（秒回）───────────────────

@router.post('/story/generate')
async def story_generate(data: dict):
    """生成一篇完整睡前故事，按句子切成多段，只返回文字（不合成语音）。"""
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    theme        = (data.get('theme') or '').strip()

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    # ★ 没指定主题就随机抽一个，避免每次都讲同样的故事
    if not theme:
        theme = random.choice(STORY_THEMES)

    # 只用角色核心人格，不接聊天/记忆那套脚手架 —— 保证隔离
    system_prompt = char.get('core_prompt', '') + f'''

【★ 睡前故事模式】
现在是睡前，对方想听你（五条悟）讲一个故事哄她入睡。
【这次要讲的故事】{theme}

要求：
1. 语气温柔、舒缓、慵懒，适合入睡，不要激烈或紧张的情节。
2. 故事完整且有内容：温柔的开头、有起伏的发展、平静温暖的结尾。讲得丰富一点、长一点。
3. 分成 16-24 个气泡，每个气泡只讲一两句，像轻声细语慢慢道来。
4. 每个气泡的【日语】控制在 30-70 字以内（短一点，语音合成更稳，不会跑调）。
5. jp 必须是纯日语，zh 是对应中文翻译，不要把中文混进 jp。
6. 别每次都用"最强咒术师"那种开头，这次就好好讲上面指定的故事。

严格按这个 JSON 返回：
{{"emotion":"温柔","messages":[{{"jp":"第一句日语","zh":"第一句中文"}},{{"jp":"第二句日语","zh":"第二句中文"}}]}}'''

    user_line = '给我讲一个睡前故事吧。'
    if data.get('theme'):
        user_line = f'给我讲一个睡前故事吧，我想听：{theme}'

    messages = [{'role': 'user', 'content': user_line}]

    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=6000,     # ★ 更长更丰富的故事
                temperature=1.0,     # ★ 拉满变化，配合随机主题进一步防重复
                system=system_prompt,
                messages=messages,
            )
            raw = extract_text(response).strip()
            print(f'[story] attempt {attempt+1}: {raw[:120]}...')
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) >= 6:
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
                {'jp': 'ゆっくり目を閉じて、聞いてて。', 'zh': '慢慢闭上眼睛，听着就好。'},
                {'jp': '昔々、静かな夜の街にね。', 'zh': '很久很久以前，在一座安静的夜晚的城市里。'},
                {'jp': '小さな灯りが一つ、ともっていた。', 'zh': '亮着一盏小小的灯。'},
                {'jp': 'その灯りは、誰かの帰りをずっと待ってた。', 'zh': '那盏灯，一直在等着谁回家。'},
                {'jp': 'もう眠っていいよ。おやすみ。', 'zh': '可以睡了哦，晚安。'},
            ],
        }

    emotion = result.get('emotion', '温柔')
    if emotion not in EMOTIONS:
        emotion = '平静'

    # 按句子切成 TTS 友好的小段（只切文字，不合成语音）
    segments = []
    for m in result.get('messages', []):
        jp_clean = sanitize_jp(m.get('jp', ''))
        zh = (m.get('zh') or '').strip()
        for idx, chunk in enumerate(_split_jp_sentences(jp_clean)):
            segments.append({'jp': chunk, 'zh': zh if idx == 0 else ''})

    print(f'[story] {character_id} theme="{theme[:20]}" emotion={emotion} segments={len(segments)} (text only)')
    return JSONResponse({'emotion': emotion, 'segments': segments, 'pause_ms': SEGMENT_PAUSE_MS, 'theme': theme})


# ─────────────────── 第二步：单段语音合成（前端按需调用）───────────────────

@router.post('/story/tts')
async def story_tts(data: dict):
    """给一段日语，合成一段语音返回。前端播一段取一段，并提前预取下一段。"""
    text         = (data.get('text') or '').strip()
    emotion      = data.get('emotion', '平静')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)

    if not text:
        return JSONResponse({'error': 'no text'}, status_code=400)
    if emotion not in EMOTIONS:
        emotion = '平静'

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    voice_id = char.get('voice_id')
    try:
        audio = tts_to_b64(text, emotion, voice_id)
    except Exception as e:
        print(f'[story_tts] error: {e}')
        audio = ''

    return JSONResponse({'audio_b64': audio or ''})