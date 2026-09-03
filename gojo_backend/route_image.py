"""图片聊天路由：/chat/image

★ 本版改动（prompt 缓存）：
  - system 改用 build_system_blocks()（带 cache_control 分段）
  - 调用后 log_cache_usage 打印缓存命中

★ 记账升级：LLM 返回 pending_transaction 时,后端只透传给前端(不写库),
  由前端确认卡引导用户核对后再 POST /accounting/records 落库。
"""
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, TTS_PROVIDER, DEFAULT_CHARACTER_ID, MODEL_MAIN
from db import get_conn
from utils import extract_json, sanitize_jp, merge_only_extreme_short
from ai_client import extract_text
from tts import tts_to_b64
from prompt import build_system_blocks, log_cache_usage
from user_memory import (
    save_short_memory, get_short_memory,
    update_chat_days,
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


# ★ 记账透传辅助:只做基本形状校验,不写库(前端确认后 POST /accounting/records)
def _extract_pending_tx(result: dict, user_id: str):
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
            print(f'[{user_id}] 💰 [image] 检测到待确认记账 {typ} ¥{amt} {desc}')
            return out
    except Exception as e:
        print(f'[{user_id}] [image] pending_transaction 解析失败:{e}')
    return None


@router.post('/chat/image')
async def chat_image(data: dict):
    """
    接收图片（base64）+ 可选文字（caption），让 Claude Vision 识别后回复。
    请求体：
    {
      "user_id": "xxx",
      "character_id": "gojo",
      "image_base64": "xxxx...",      // 必填
      "media_type": "image/jpeg",     // 可选
      "text": "看这个！"              // 可选 caption
    }
    """
    user_id      = data.get('user_id', 'default')
    character_id = data.get('character_id', DEFAULT_CHARACTER_ID)
    image_b64    = data.get('image_base64', '')
    media_type   = data.get('media_type', 'image/jpeg')
    user_text    = (data.get('text') or '').strip()
    is_video     = bool(data.get('is_video'))

    # 统一成图片列表：单图和多图（视频抽帧）走同一条路
    raw_images = data.get('images')
    images = []
    if isinstance(raw_images, list) and raw_images:
        for it in raw_images[:6]:
            d = (it or {}).get('data')
            if d:
                images.append({'data': d, 'media_type': it.get('media_type') or 'image/jpeg'})
    elif image_b64:
        images.append({'data': image_b64, 'media_type': media_type})

    if not images:
        return JSONResponse({'error': 'no image'}, status_code=400)

    char = get_character(character_id)
    if not char:
        return JSONResponse({'error': f'character {character_id} not found'}, status_code=404)

    total_days = update_chat_days(user_id)
    short_memories = get_short_memory(user_id, 6, character_id)

    # ── 构造 messages（multimodal）──
    messages = [{'role': r, 'content': c} for r, c in short_memories]

    user_content = [
        {
            'type': 'image',
            'source': {
                'type': 'base64',
                'media_type': img['media_type'],
                'data': img['data'],
            }
        }
        for img in images
    ]

    NO_TEXT_HINT = (
        '【对方发来了一张图片，没有附文字。你看到了这张图，自然反应——根据图里的内容回应，'
        '像真朋友收到对方发来的照片一样：可以好奇、调侃、关心、表达喜好。'
        '不要冷冰冰地"描述图片内容"，要像看到了实物一样有情绪。】'
    )

    if is_video:
        # 这些是同一段视频按时间顺序抽出来的画面
        video_hint = (
            '【★ 对方发来的是一段【视频】。上面 %d 张图是这段视频里按时间顺序抽出的画面'
            '（第一张=开头，最后一张=结尾）。把它们当成【连续发生的一件事】来看，'
            '脑补中间的过程，像真的看了这段视频一样反应：聊发生了什么、你的感受、你注意到的细节。'
            '绝不要当成几张无关的照片逐张点评，也不要说"我看不到视频"。'
            '（你听不到声音，所以别评论声音。）】'
        ) % len(images)
        if user_text:
            user_content.append({'type': 'text', 'text': video_hint + '\n她说：' + user_text})
            display_text = '🎬 ' + user_text
        else:
            user_content.append({'type': 'text', 'text': video_hint + '她没有附文字，你看完自然反应就好。'})
            display_text = '🎬 [视频]'
    elif user_text:
        user_content.append({'type': 'text', 'text': user_text})
        display_text = '📷 ' + user_text
    else:
        user_content.append({'type': 'text', 'text': NO_TEXT_HINT})
        display_text = '📷 [图片]'

    messages.append({'role': 'user', 'content': user_content})

    # 用 caption 做背景记忆检索（聊到甜食的照片→召回喜久福那条）
    recall_query = user_text if user_text else ''
    system_blocks = build_system_blocks(user_id, character_id, recall_query)

    # ── 调用 Claude Vision ──
    result = None
    for attempt in range(5):
        try:
            response = claude_client.messages.create(
                model=MODEL_MAIN,
                max_tokens=800,
                system=system_blocks,
                messages=messages,
            )
            log_cache_usage(f'image:{character_id}', response)
            raw = extract_text(response).strip()
            print(f'[{user_id}][{character_id}] image attempt {attempt+1}: {raw[:120]}...')
            parsed = extract_json(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                if all(m.get('jp', '').strip() and m.get('zh', '').strip() for m in parsed['messages']):
                    result = parsed
                    break
        except Exception as e:
            print(f'image attempt {attempt+1} error: {e}')

    if not result:
        result = {
            'emotion': '疑惑',
            'messages': [{'jp': 'おっ、写真か。何これ？', 'zh': '哦，照片啊。这是什么？'}]
        }

    emotion = result.get('emotion', '平静')
    if emotion not in EMOTIONS:
        emotion = '平静'

    msgs = result.get('messages', [])
    for m in msgs:
        m['jp'] = sanitize_jp(m.get('jp', ''))
    msgs = merge_only_extreme_short(msgs)

    full_jp = ' '.join(m['jp'] for m in msgs)

    save_short_memory(user_id, 'user', display_text, character_id)
    save_short_memory(user_id, 'assistant', full_jp, character_id)

    # 如果用户附了文字，尝试提取用户事实
    if user_text:
        enqueue_private_extraction(user_id, user_text, full_jp, character_id)

    voice_id = char.get('voice_id')
    for m in msgs:
        m['audio_b64'] = tts_to_b64(m['jp'], emotion, voice_id)

    kind = 'video' if is_video else 'image'
    print(f'[TTS:{TTS_PROVIDER}] {character_id} {kind}({len(images)}帧) emotion={emotion} segs={len(msgs)}')

    # ─── 处理取消提醒 ───
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
                print(f'[{user_id}] 🗑️ 已取消任务 id={task_id}（来自图片对话）')
        except Exception as e:
            print(f'取消提醒失败：{e}')

    # ─── 处理新增提醒 ───
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
            # ★ 先精确查，再模糊查（治"同一件事换个说法又建一条"）
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
                print(f'[{user_id}] ✅ 提醒已保存（来自图片对话）task_id={task_id}')
        except Exception as e:
            print(f'提醒保存失败：{e}')

    # ★ 记账透传（只透传给前端,不写库；前端确认卡引导用户核对账户后 POST /accounting/records）
    pending_tx = _extract_pending_tx(result, user_id)

    resp = {'emotion': emotion, 'messages': msgs, 'total_days': total_days}
    if reminder_data:
        resp['reminder'] = reminder_data
    if cancelled_tasks:
        resp['cancelled_tasks'] = cancelled_tasks
    if pending_tx:
        resp['pending_transaction'] = pending_tx
    return JSONResponse(resp)