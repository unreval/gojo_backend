"""游戏路由 —— 目前只有五子棋对战中的 AI 说话

/game/gomoku/talk —— 五子棋对战中,让 AI 用人设吐槽/回应
  · 用 Haiku 便宜快,游戏对话不需要 Opus
  · 战术情境(活三/活四/防守)由前端算好传进来,LLM 只负责说人话
  · 支持 skip —— 大部分普通落子 LLM 会选择不开口(真人对战不会每步都说话)
  · user_chat 强制必回(用户主动说话)
"""
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, DEFAULT_CHARACTER_ID, MODEL_JP_AUX
from characters import get_character
from utils import extract_json, sanitize_jp
from tts import tts_to_b64

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)


# event 值 → 情境描述,喂给 LLM 让它知道该什么情绪说什么
_TACTICAL_HINTS = {
    'game_start':         '棋局刚开始,还没落子。开个场,轻松挑衅或简短招呼都行。',
    'ai_normal':          '你刚下了普通一手。绝大多数时候可以选择不说话(skip)。',
    'ai_attack_three':    '你刚下的这手形成了活三,你开始施压了。可以带点得意或调侃。',
    'ai_attack_four':     '你刚下的这手形成了冲四/活四,眼看要赢。可以放狠话,或反常温柔。',
    'user_normal':        '她刚下了普通一手。绝大多数时候可以选择不说话(skip)。',
    'user_attack_three':  '她刚形成活三,压过来了。可以皱眉、承认、嘴硬或挑衅回去。',
    'user_attack_four':   '她刚形成冲四/活四,你要挡了。可以慌一下、认真起来、或死鸭子嘴硬。',
    'ai_win':             '你赢了。可以得意但别太狠,看你人设。',
    'user_win':           '她赢了。可以酸溜溜、输不起、大方认输,看你人设。',
    'user_chat':          '她在下棋中间跟你说了句话,你直接接她的话。',
}


@router.post('/game/gomoku/talk')
async def gomoku_talk(data: dict):
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    event        = data.get('event', 'ai_normal')
    move_count   = int(data.get('move_count', 0))
    ai_color     = data.get('ai_color', 'white')       # 'black' or 'white'
    situation    = data.get('situation', 'even')       # 'winning' | 'losing' | 'even'
    user_text    = (data.get('user_text') or '').strip()

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)
    char_name = char['name']
    voice_id = char.get('voice_id')

    ai_color_cn = '白' if ai_color == 'white' else '黑'
    user_color_cn = '黑' if ai_color == 'white' else '白'
    first_hand = '她' if ai_color == 'white' else '你'

    situation_desc = {
        'winning': '目前局势对你有利,你在赢面上',
        'losing':  '目前局势对你不利,她压着你打',
        'even':    '目前势均力敌',
    }.get(situation, '')

    trigger = _TACTICAL_HINTS.get(event, '')

    user_chat_part = ''
    if event == 'user_chat' and user_text:
        user_chat_part = f'\n\n【她说】「{user_text}」\n你【必须】接她的话,不能 skip。'

    prompt = f'''你是{char_name}。你正和她在下五子棋 —— 你执{ai_color_cn},她执{user_color_cn},{first_hand}先手。

【当前局势】
第 {move_count} 手。{situation_desc}。

【触发场景】
{trigger}{user_chat_part}

【要求】
你现在在电子棋盘上和她对战,就是一次很日常的对局。用你的语气自然反应,1-2 句就够,别长篇。
- 想吐槽/挑衅/示弱/得意/自嘲/装模作样 都可以,按你此刻的人设和心情
- 用户主动说话时【必须回】(见上面 event=user_chat 的处理)
- 其他情况没什么想说的 → 输出 {{"skip":true}} —— 真人对战大部分时间是沉默的,只在关键节点或有话说时开口
- 【避免复读】:如果一句话你上一步已经类似地说过,这次就 skip

【输出格式,严格 JSON 一行,不要任何解释】
说 → {{"jp":"日语","zh":"中文","emotion":"情绪"}}
不说 → {{"skip":true}}

emotion 从这里选:{'/'.join(EMOTIONS)}'''

    try:
        resp = claude_client.messages.create(
            model=MODEL_JP_AUX,
            max_tokens=400,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = resp.content[0].text.strip()
        parsed = extract_json(raw)
    except Exception as e:
        print(f'[game_talk] LLM 出错: {e}')
        # user_chat 兜底,别让用户说话没回应
        if event == 'user_chat':
            return JSONResponse({
                'say': True,
                'jp': 'んー?', 'zh': '嗯?',
                'emotion': '平静',
                'audio_b64': '',
            })
        return JSONResponse({'say': False})

    if not parsed:
        if event == 'user_chat':
            return JSONResponse({
                'say': True,
                'jp': 'んー?', 'zh': '嗯?',
                'emotion': '平静', 'audio_b64': '',
            })
        return JSONResponse({'say': False})

    # user_chat 强制不能 skip
    if parsed.get('skip') and event != 'user_chat':
        return JSONResponse({'say': False})

    jp = sanitize_jp((parsed.get('jp') or '').strip())
    zh = (parsed.get('zh') or '').strip()
    emotion = parsed.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    if not jp or not zh:
        if event == 'user_chat':
            jp = 'んー?'; zh = '嗯?'
        else:
            return JSONResponse({'say': False})

    audio_b64 = ''
    try:
        audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
    except Exception as e:
        print(f'[game_talk] TTS 出错(不影响文字): {e}')

    print(f'[game_talk] {character_id} event={event} 说了: {zh[:30]}')
    return JSONResponse({
        'say': True,
        'jp': jp,
        'zh': zh,
        'emotion': emotion,
        'audio_b64': audio_b64,
    })
