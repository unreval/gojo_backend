"""游戏路由 v3 —— 算法下棋 + LLM 说话 + 游戏记忆

设计决策:
  · 落子用威胁分算法(快/强/不花钱),LLM 看文本棋盘太蠢了
  · 说话用 LLM(角色人设/吐槽/被说服)
  · user_chat 永远能用(不受回合限制)
  · 游戏结束保存有趣瞬间到记忆
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_client import create_chat
from config import EMOTIONS, DEFAULT_CHARACTER_ID, MODEL_JP_AUX, MODEL_CN_AUX
from characters import get_character
from utils import extract_json, sanitize_jp
from tts import tts_to_b64

router = APIRouter()

# ────────────────── 算法落子 ──────────────────

DIRS = [(1, 0), (0, 1), (1, 1), (1, -1)]
SIZE = 15

def _eval_pos(b, x, y, color):
    score = 0
    for dx, dy in DIRS:
        count, openEnds = 1, 0
        nx, ny = x + dx, y + dy
        while 0 <= nx < SIZE and 0 <= ny < SIZE and b[ny][nx] == color:
            count += 1; nx += dx; ny += dy
        if 0 <= nx < SIZE and 0 <= ny < SIZE and b[ny][nx] == 0:
            openEnds += 1
        nx, ny = x - dx, y - dy
        while 0 <= nx < SIZE and 0 <= ny < SIZE and b[ny][nx] == color:
            count += 1; nx -= dx; ny -= dy
        if 0 <= nx < SIZE and 0 <= ny < SIZE and b[ny][nx] == 0:
            openEnds += 1
        if count >= 5: score += 100000
        elif count == 4 and openEnds == 2: score += 10000
        elif count == 4 and openEnds == 1: score += 1000
        elif count == 3 and openEnds == 2: score += 500
        elif count == 3 and openEnds == 1: score += 100
        elif count == 2 and openEnds == 2: score += 50
        elif count == 2 and openEnds == 1: score += 10
        else: score += count
    return score


def algo_move(board):
    """威胁分算法:攻防兼顾,快速且够强。"""
    best, bestScore = None, -1
    for y in range(SIZE):
        for x in range(SIZE):
            if board[y][x] != 0: continue
            hasN = False
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    nx2, ny2 = x + dx, y + dy
                    if 0 <= nx2 < SIZE and 0 <= ny2 < SIZE and board[ny2][nx2] != 0:
                        hasN = True; break
                if hasN: break
            if not hasN: continue
            sc = _eval_pos(board, x, y, 2) + _eval_pos(board, x, y, 1) * 0.9
            if sc > bestScore:
                bestScore = sc; best = (x, y)
    return best if best else (7, 7)


@router.post('/game/gomoku/move')
async def gomoku_move(data: dict):
    """算法决定落子。快/强/不花钱。"""
    board = data.get('board', [])
    if not board or len(board) != SIZE:
        return JSONResponse({'x': 7, 'y': 7})
    x, y = algo_move(board)
    return JSONResponse({'x': x, 'y': y})


# ────────────────── AI 说话 ──────────────────

_TACTICAL_HINTS = {
    'game_start':         '棋局刚开始。开个场,轻松招呼就行,一句话。',
    'ai_normal':          '你刚下了普通一手。大部分时候不说话(skip)。',
    'ai_attack_three':    '你形成了活三,开始施压了。',
    'ai_attack_four':     '你形成了冲四/活四,眼看要赢。',
    'user_normal':        '她刚下了普通一手。大部分时候不说话(skip)。',
    'user_attack_three':  '她形成活三,压过来了。',
    'user_attack_four':   '她形成冲四/活四,你要挡了。',
    'ai_win':             '你赢了。',
    'user_win':           '她赢了。',
    'user_chat':          '她在下棋中间跟你说了句话,你直接接她的话。',
}


@router.post('/game/gomoku/talk')
async def gomoku_talk(data: dict):
    """对战中 AI 说话。user_chat 强制必回,其他可以 skip。"""
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    event = data.get('event', 'game_start')
    user_text = (data.get('user_text') or '').strip()
    move_count = int(data.get('move_count', 0))
    situation = data.get('situation', 'even')

    char = get_character(character_id)
    if not char:
        return JSONResponse({'say': False})
    char_name = char['name']
    voice_id = char.get('voice_id')

    trigger = _TACTICAL_HINTS.get(event, '')
    user_chat_part = ''
    if event == 'user_chat' and user_text:
        user_chat_part = f'\n\n【她说】「{user_text}」\n你【必须】接她的话,不能 skip。'

    situation_cn = {'winning': '你在赢面', 'losing': '她压着你打', 'even': '势均力敌'}.get(situation, '')

    prompt = f'''你是{char_name}。你正和她下五子棋,第 {move_count} 手。{situation_cn}。

{trigger}{user_chat_part}

用你的语气自然反应,1-2 句,别长篇。
没什么想说的 → {{"skip":true}}
说 → {{"jp":"日语","zh":"中文","emotion":"情绪"}}
emotion: {'/'.join(EMOTIONS)}'''

    try:
        raw, _ = create_chat(model=MODEL_JP_AUX, max_tokens=300,
                             messages=[{'role': 'user', 'content': prompt}])
        parsed = extract_json((raw or '').strip())
    except Exception as e:
        print(f'[game_talk] LLM 出错: {e}')
        if event == 'user_chat':
            return JSONResponse({'say': True, 'jp': 'んー?', 'zh': '嗯?',
                                 'emotion': '平静', 'audio_b64': ''})
        return JSONResponse({'say': False})

    if not parsed or (parsed.get('skip') and event != 'user_chat'):
        return JSONResponse({'say': False})

    jp = sanitize_jp((parsed.get('jp') or '').strip())
    zh = (parsed.get('zh') or '').strip()
    emotion = parsed.get('emotion', '平静')
    if emotion not in EMOTIONS: emotion = '平静'
    if not jp or not zh:
        if event == 'user_chat': jp, zh = 'んー?', '嗯?'
        else: return JSONResponse({'say': False})

    audio_b64 = ''
    try: audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
    except: pass

    return JSONResponse({'say': True, 'jp': jp, 'zh': zh,
                         'emotion': emotion, 'audio_b64': audio_b64})


# ────────────────── 游戏记忆 ──────────────────

@router.post('/game/gomoku/save_memory')
async def gomoku_save_memory(data: dict):
    """游戏结束后保存有趣瞬间到 bond_memory。"""
    user_id = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    result = data.get('result', '')
    move_count = int(data.get('move_count', 0))
    highlights = data.get('chat_highlights', [])[:8]

    char = get_character(character_id)
    char_name = char['name'] if char else character_id
    result_cn = '她赢了' if result == 'user_win' else f'{char_name}赢了'

    highlight_text = ''
    for h in highlights:
        role_label = '她' if h.get('role') == 'user' else char_name
        highlight_text += f'  {role_label}:「{h.get("text", "")}」\n'

    prompt = f'''{char_name}和她下了五子棋,{result_cn},共 {move_count} 手。
{f"对话片段:{chr(10)}{highlight_text}" if highlight_text else ""}
用一句话(20-40字)记录值得记住的事。写成{char_name}视角。
没什么特别的 → {{"skip":true}}
有 → {{"memory":"一句话"}}'''

    try:
        raw, _ = create_chat(model=MODEL_CN_AUX, max_tokens=200,
                             messages=[{'role': 'user', 'content': prompt}])
        parsed = extract_json((raw or '').strip())
    except:
        return JSONResponse({'saved': False})

    if not parsed or parsed.get('skip') or not parsed.get('memory'):
        return JSONResponse({'saved': False})

    memory_text = (parsed['memory'] or '').strip()[:200]
    try:
        from user_memory import save_bond_memory
        saved = save_bond_memory(user_id, character_id, 'between', memory_text)
        if saved: print(f'[game_memory] 📝 {char_name}: {memory_text}')
        return JSONResponse({'saved': bool(saved), 'memory': memory_text})
    except Exception as e:
        print(f'[game_memory] 保存失败: {e}')
        return JSONResponse({'saved': False})