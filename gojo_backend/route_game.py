"""游戏路由 v2 —— AI 用 LLM 决定落子 + 说话 + 游戏记忆

/game/gomoku/move —— LLM 看棋盘决定下哪里(可以被说服让子)
/game/gomoku/talk —— 对战中 AI 说话(吐槽/回应)
/game/gomoku/save_memory —— 游戏结束后保存有趣瞬间到记忆

★ LLM 决定落子的设计:
  · 把棋盘状态用文本画出来给 LLM 看(15×15 用坐标)
  · LLM 看到对局聊天记录,知道用户有没有在撒娇/求饶
  · 正常打 → LLM 按角色性格给出一步(不一定最优,但合理)
  · 用户耍赖 → LLM 可能"看心情"下弱一点("行行行,让你一步")
  · LLM 返回无效位置 → 自动 fallback 到算法(避免卡死)
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ai_client import create_chat
from config import EMOTIONS, DEFAULT_CHARACTER_ID, MODEL_JP_AUX, MODEL_CN_AUX
from characters import get_character
from utils import extract_json, sanitize_jp
from tts import tts_to_b64

router = APIRouter()


# ────────────────── 棋盘可视化 ──────────────────

def _board_to_text(board, last_move=None):
    """把 15×15 棋盘转成 LLM 能看懂的文本。
    · = 空, ● = 黑(玩家), ○ = 白(AI), ★ = 最后一手
    坐标:列 A-O(左→右),行 1-15(上→下)
    """
    cols = 'ABCDEFGHIJKLMNO'
    lines = ['   ' + ' '.join(cols)]
    for y in range(15):
        row_str = f'{y+1:2d} '
        for x in range(15):
            c = board[y][x] if isinstance(board, list) and y < len(board) and x < len(board[y]) else 0
            if last_move and last_move[0] == x and last_move[1] == y:
                row_str += '★ '
            elif c == 1:
                row_str += '● '
            elif c == 2:
                row_str += '○ '
            else:
                row_str += '· '
        lines.append(row_str)
    return '\n'.join(lines)


def _coord_to_xy(coord_str):
    """把 LLM 输出的坐标(如 "H8" "J12")转成 (x, y)。容错大小写和空格。"""
    s = (coord_str or '').strip().upper().replace(' ', '')
    if len(s) < 2:
        return None
    col = s[0]
    row_str = s[1:]
    cols = 'ABCDEFGHIJKLMNO'
    if col not in cols:
        return None
    try:
        row = int(row_str)
    except ValueError:
        return None
    x = cols.index(col)
    y = row - 1
    if x < 0 or x >= 15 or y < 0 or y >= 15:
        return None
    return (x, y)


# ────────────────── /game/gomoku/move ──────────────────

@router.post('/game/gomoku/move')
async def gomoku_move(data: dict):
    """LLM 看棋盘 + 对话记录,决定下哪里。

    入参:
      board: 15×15 数组(0=空 1=黑/玩家 2=白/AI)
      last_move: [x, y] 最后一手
      chat_history: [{role, text}] 最近的对局聊天(最多 10 条)
      character_id, user_id

    出参:
      {x, y, reason, say: {jp, zh, emotion, audio_b64}?}
      say 有值 = AI 落子时顺便说了句话(合并一次调用,省一轮请求)
    """
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    board = data.get('board', [])
    last_move = data.get('last_move')
    chat_history = data.get('chat_history', [])[:10]
    user_id = data.get('user_id', 'default')

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)
    char_name = char['name']
    voice_id = char.get('voice_id')

    board_text = _board_to_text(board, last_move)

    # 对局聊天记录(最近 10 条)
    chat_lines = ''
    if chat_history:
        for msg in chat_history[-10:]:
            role_label = '她' if msg.get('role') == 'user' else '你'
            chat_lines += f'{role_label}:「{msg.get("text", "")}」\n'

    # 统计手数
    move_count = sum(1 for row in board for c in row if c != 0)

    prompt = f'''你是{char_name}。你正和她下五子棋。你执白(○),她执黑(●),她先手。

【当前棋盘】(列 A-O,行 1-15,● = 她,○ = 你,★ = 上一手)
{board_text}

当前第 {move_count} 手。

{f"【对局中的聊天】{chr(10)}{chat_lines}" if chat_lines else ""}

【你要做的事】
1. 看清棋盘局势,选一个空位(·)落白子(○)
2. 你是{char_name},按你的性格决定怎么下:
   - 正常情况:认真下,选一步合理的(不一定要最优,人类水平就行)
   - 如果她在撒娇/耍赖/求你让她赢 → 你可以"看心情"选一步弱一点的,
     但不要太明显(别故意下到角落),要像是"不小心没看到"
   - 如果她在嘲讽你/说你菜 → 你可能会更认真,选一步强的
3. 顺便:如果你在落子时想说句话(吐槽/调侃/解释为什么下这里/回应她的话),
   也一起说出来。没什么想说的就不说。

【基本策略提示(帮你看棋)】
- 最重要:如果对方有冲四/活四(4 连且有空端),必须挡!不挡就输了
- 其次:你自己能连成 4+ 就进攻
- 再其次:活三(3 连两端空)是大威胁,能挡则挡
- 普通局面:靠近中心、靠近已有子的位置通常更好

【输出格式,严格 JSON 一行,不要任何解释】
{{"move":"列行 如 H8","reason":"一句话说为什么下这里","say":{{"jp":"日语","zh":"中文","emotion":"情绪"}}}}

如果没话说,say 设为 null:
{{"move":"H8","reason":"天元开局","say":null}}

emotion 从这里选:{'/'.join(EMOTIONS)}
坐标格式:列字母(A-O) + 行数字(1-15),如 H8 = 第8列第8行'''

    try:
        raw, _usage = create_chat(
            model=MODEL_JP_AUX,
            max_tokens=500,
            messages=[{'role': 'user', 'content': prompt}],
        )
        raw = (raw or '').strip()
        parsed = extract_json(raw)
    except Exception as e:
        print(f'[gomoku_move] LLM 出错: {e}')
        return _fallback_move(board, char_name, voice_id)

    if not parsed or not parsed.get('move'):
        print(f'[gomoku_move] 解析失败: {raw[:100]}')
        return _fallback_move(board, char_name, voice_id)

    # 解析坐标
    xy = _coord_to_xy(parsed['move'])
    if not xy:
        print(f'[gomoku_move] 坐标无效: {parsed["move"]}')
        return _fallback_move(board, char_name, voice_id)

    x, y = xy
    # 检查位置是否为空
    if isinstance(board, list) and 0 <= y < len(board) and 0 <= x < len(board[y]):
        if board[y][x] != 0:
            print(f'[gomoku_move] 位置已占: ({x},{y})={board[y][x]}')
            return _fallback_move(board, char_name, voice_id)
    else:
        return _fallback_move(board, char_name, voice_id)

    reason = (parsed.get('reason') or '').strip()
    print(f'[gomoku_move] {char_name} 下在 {parsed["move"]} ({x},{y}): {reason}')

    # 处理 say
    say_data = None
    say_raw = parsed.get('say')
    if say_raw and isinstance(say_raw, dict) and say_raw.get('jp'):
        jp = sanitize_jp((say_raw.get('jp') or '').strip())
        zh = (say_raw.get('zh') or '').strip()
        emotion = say_raw.get('emotion', '平静')
        if emotion not in EMOTIONS:
            emotion = '平静'
        audio_b64 = ''
        try:
            audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
        except Exception as e:
            print(f'[gomoku_move] TTS 出错: {e}')
        say_data = {'jp': jp, 'zh': zh, 'emotion': emotion, 'audio_b64': audio_b64}

    return JSONResponse({
        'x': x, 'y': y,
        'reason': reason,
        'say': say_data,
    })


def _fallback_move(board, char_name, voice_id):
    """LLM 返回无效结果时的算法兜底。"""
    x, y = _algo_move(board)
    print(f'[gomoku_move] ⚠️ fallback 到算法: ({x},{y})')
    return JSONResponse({'x': x, 'y': y, 'reason': 'fallback', 'say': None})


def _algo_move(board):
    """简单的威胁分算法(和之前前端版一样)。"""
    DIRS = [(1, 0), (0, 1), (1, 1), (1, -1)]
    SIZE = 15

    def eval_pos(b, x, y, color):
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
            else: score += count
        return score

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
            sc = eval_pos(board, x, y, 2) + eval_pos(board, x, y, 1) * 0.9
            if sc > bestScore:
                bestScore = sc; best = (x, y)
    return best if best else (7, 7)


# ────────────────── /game/gomoku/talk ──────────────────

_TACTICAL_HINTS = {
    'game_start':         '棋局刚开始。开个场,轻松招呼就行。',
    'user_chat':          '她在下棋中间跟你说了句话,你直接接她的话。',
    'ai_win':             '你赢了。',
    'user_win':           '她赢了。',
}


@router.post('/game/gomoku/talk')
async def gomoku_talk(data: dict):
    """对战中 AI 单独说话(不带落子决策,只用于 game_start/user_chat/win 等)。
    大部分对战中的说话已经合并到 /game/gomoku/move 的 say 字段里了。
    """
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    event = data.get('event', 'game_start')
    user_text = (data.get('user_text') or '').strip()
    move_count = int(data.get('move_count', 0))

    char = get_character(character_id)
    if not char:
        return JSONResponse({'say': False})
    char_name = char['name']
    voice_id = char.get('voice_id')

    trigger = _TACTICAL_HINTS.get(event, '')
    user_chat_part = ''
    if event == 'user_chat' and user_text:
        user_chat_part = f'\n\n【她说】「{user_text}」\n你【必须】接她的话,不能 skip。'

    prompt = f'''你是{char_name}。你正和她下五子棋,第 {move_count} 手。

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
        if event == 'user_chat':
            jp, zh = 'んー?', '嗯?'
        else:
            return JSONResponse({'say': False})

    audio_b64 = ''
    try: audio_b64 = tts_to_b64(jp, emotion, voice_id) or ''
    except: pass

    return JSONResponse({'say': True, 'jp': jp, 'zh': zh,
                         'emotion': emotion, 'audio_b64': audio_b64})


# ────────────────── /game/gomoku/save_memory ──────────────────

@router.post('/game/gomoku/save_memory')
async def gomoku_save_memory(data: dict):
    """游戏结束后保存有趣瞬间到记忆。

    入参:
      user_id, character_id
      result: 'user_win' | 'ai_win'
      move_count: 总手数
      chat_highlights: [{role, text}] 对局中有趣的对话片段
    """
    user_id = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    result = data.get('result', '')
    move_count = int(data.get('move_count', 0))
    highlights = data.get('chat_highlights', [])[:8]

    char = get_character(character_id)
    char_name = char['name'] if char else character_id
    result_cn = '她赢了' if result == 'user_win' else f'{char_name}赢了'

    highlight_text = ''
    if highlights:
        for h in highlights:
            role_label = '她' if h.get('role') == 'user' else char_name
            highlight_text += f'  {role_label}:「{h.get("text", "")}」\n'

    prompt = f'''下面是{char_name}和她下五子棋的对局记录摘要:

结果:{result_cn},共 {move_count} 手。
{f"对局中的对话:{chr(10)}{highlight_text}" if highlight_text else "没有特别的对话。"}

请用一句话(20-40字)记录这次对局中值得记住的事情。
写成{char_name}的视角,像日记里的一笔:
- 不要写"我们下了五子棋"这种废话(下棋本身不值得记)
- 要记的是:她有没有耍赖/她的有趣反应/谁赢了但过程怎么样/她说了什么好笑的话
- 如果没什么特别的,输出 {{"skip":true}}

格式(JSON 一行):
{{"memory":"一句话记录","skip":false}}
或:
{{"skip":true}}'''

    try:
        raw, _ = create_chat(model=MODEL_CN_AUX, max_tokens=200,
                             messages=[{'role': 'user', 'content': prompt}])
        parsed = extract_json((raw or '').strip())
    except Exception as e:
        print(f'[game_memory] LLM 出错: {e}')
        return JSONResponse({'saved': False})

    if not parsed or parsed.get('skip') or not parsed.get('memory'):
        return JSONResponse({'saved': False, 'reason': 'nothing interesting'})

    memory_text = (parsed['memory'] or '').strip()[:200]

    # 存到 bond_memory(between):这是"我们之间"发生的事
    try:
        from user_memory import save_bond_memory
        saved = save_bond_memory(user_id, character_id, 'between', memory_text)
        if saved:
            print(f'[game_memory] 📝 {char_name} 记下了: {memory_text}')
        return JSONResponse({'saved': bool(saved), 'memory': memory_text})
    except Exception as e:
        print(f'[game_memory] 保存失败: {e}')
        return JSONResponse({'saved': False, 'error': str(e)})