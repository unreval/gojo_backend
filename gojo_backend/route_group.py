"""群聊路由（第一步·骨架）

端点：
  POST   /group                 建群（传群名 + 成员角色列表）
  GET    /groups                列出某用户的所有群
  GET    /group/{gid}           群详情（成员 + 最近消息）
  DELETE /group/{gid}           解散群
  POST   /group/chat            ★ 核心：用户在群里发一句 → 智能调度谁回 → 角色依次回复（含角色互动）

第一步范围（明确不做的，留给第二步）：
  - 记忆三通（个人↔群↔跨角色）：本版每个角色仍只读自己的单人记忆，群有自己的历史，互不混。
  - 个人库/群库分离判断：第二步再做。

调度与回复都要求模型返回【单行 JSON】——因为 utils.extract_json 会把换行抹掉，多行 JSON 会被破坏。
"""
import threading
import anthropic
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import ANTHROPIC_KEY, EMOTIONS, DEFAULT_CHARACTER_ID
from db import get_conn
from utils import extract_json, sanitize_jp
from tts import tts_to_b64
from prompt import build_system_prompt
from characters import get_character

router = APIRouter()
claude_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

# 一轮群对话里,角色发言总条数上限(用户1句 + 角色最多 7 句你来我往)
# 8 = 让角色之间能真的"驳回 / 自由聊起来",但有硬上限防止无限互怼烧 API
MAX_TURNS_PER_ROUND = 8


def _parse_reply(raw: str):
    """宽松解析：先 extract_json，失败再从第一个 { 抠到最后一个 }。"""
    parsed = extract_json(raw)
    if parsed:
        return parsed
    try:
        import json
        i = raw.find('{')
        j = raw.rfind('}')
        if i != -1 and j > i:
            return json.loads(raw[i:j + 1])
    except Exception:
        pass
    return None


def _is_repetitive(new_text: str, recent_texts: list, threshold: float = 0.7) -> bool:
    """检测新生成的回复是否和最近回复"复读"。
    简单算法:对每个最近回复,算字符级 Jaccard 相似度(集合交/并),
    任何一个超过阈值就算复读。阈值 0.7 = 70% 字符重合就停。"""
    if not new_text or not recent_texts:
        return False
    new_set = set(new_text)
    if len(new_set) < 5:
        # 太短的句子(比如纯标点),不算复读
        return False
    for old in recent_texts:
        if not old:
            continue
        old_set = set(old)
        if len(old_set) < 5:
            continue
        inter = len(new_set & old_set)
        union = len(new_set | old_set)
        if union == 0:
            continue
        sim = inter / union
        if sim >= threshold:
            return True
        # 额外检查:如果新句子 80% 的字符都在旧句子里,也算复读
        if len(new_set & old_set) / len(new_set) >= 0.85:
            return True
    return False



# ─────────────────── 群成员 / 历史 读取小工具 ───────────────────

def _get_group(gid: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT id, name, avatar_url, owner_user_id FROM groups WHERE id = %s', (gid,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {'id': row[0], 'name': row[1], 'avatar_url': row[2], 'owner_user_id': row[3]}


def _get_member_characters(gid: int):
    """返回群里的角色成员列表 [{id, name, voice_id, is_owner_role}]，按入群顺序。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT member_id, is_owner_role FROM group_members
           WHERE group_id = %s AND member_type = 'character'
           ORDER BY id''',
        (gid,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    result = []
    for member_id, is_owner_role in rows:
        char = get_character(member_id)
        if char:
            result.append({
                'id': char['id'],
                'name': char['name'],
                'voice_id': char.get('voice_id'),
                'is_owner_role': bool(is_owner_role),
            })
    return result


def _get_group_history(gid: int, limit: int = 12):
    """取群最近若干条消息，返回 [{sender_type, sender_id, sender_name, jp, zh}]（旧→新）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT sender_type, sender_id, jp, zh FROM group_messages
           WHERE group_id = %s ORDER BY timestamp DESC LIMIT %s''',
        (gid, limit)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    rows = rows[::-1]  # 转成旧→新
    history = []
    name_cache = {}
    for sender_type, sender_id, jp, zh in rows:
        if sender_type == 'character':
            if sender_id not in name_cache:
                c = get_character(sender_id)
                name_cache[sender_id] = c['name'] if c else sender_id
            sender_name = name_cache[sender_id]
        else:
            sender_name = '群主'
        history.append({
            'sender_type': sender_type,
            'sender_id': sender_id,
            'sender_name': sender_name,
            'jp': jp or '',
            'zh': zh or '',
        })
    return history


def _save_group_message(gid, sender_type, sender_id, jp, zh, emotion='平静'):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO group_messages (group_id, sender_type, sender_id, jp, zh, emotion)
           VALUES (%s, %s, %s, %s, %s, %s) RETURNING id''',
        (gid, sender_type, sender_id, jp, zh, emotion)
    )
    mid = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return mid


def _history_text(history):
    """把群历史拼成给模型看的纯文本。"""
    lines = []
    for h in history:
        who = h['sender_name'] if h['sender_type'] == 'character' else '群主'
        # 角色用日语原文，用户用中文原话
        content = h['jp'] if h['sender_type'] == 'character' and h['jp'] else h['zh']
        lines.append(f'{who}：{content}')
    return '\n'.join(lines)


# ─────────────────── 智能调度：判断这一句该谁接 ───────────────────

def _schedule_speakers(members, history, user_text, mentioned_id=None):
    """用 Haiku 判断这轮该哪些角色发言、顺序如何。返回 character_id 列表。
    若用户 @了某人，强制他打头。调度失败兜底为群主角色或第一个成员。"""
    # @点名优先
    forced = [mentioned_id] if mentioned_id else []

    roster = '\n'.join(f'- {m["id"]}：{m["name"]}' for m in members)
    hist_txt = _history_text(history[-8:]) if history else '（还没有人说过话）'

    sched_prompt = f'''你是一个群聊"发言调度器"。下面是一个群,成员有这些角色：
{roster}

最近的群聊记录：
{hist_txt}

群主刚发了一句话："{user_text}"

请判断这一句话之后,应该由哪些角色开口回应、按什么顺序。规则：
1. 只挑真正适合接话的角色,1 到 2 个,别让所有人都抢着说。
2. 如果话是明显冲着某个角色说的,让他先回。
3. 如果是泛泛的话,挑一个最合适的角色回就行。
4. 只能从上面列出的角色 id 里选。

只返回单行 JSON,不要任何多余文字：
{{"speakers":["角色id1"]}}
或最多两个：
{{"speakers":["角色id1","角色id2"]}}'''

    try:
        resp = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=120,
            messages=[{'role': 'user', 'content': sched_prompt}]
        )
        raw = resp.content[0].text.strip()
        parsed = _parse_reply(raw)
        ids = []
        if parsed and isinstance(parsed.get('speakers'), list):
            valid = {m['id'] for m in members}
            ids = [s for s in parsed['speakers'] if s in valid]
        # 合并 @点名（去重、保序，@的人放最前）
        ordered = forced + [i for i in ids if i not in forced]
        if ordered:
            return ordered[:2]
    except Exception as e:
        print(f'[group][schedule] error: {e}')

    # 兜底：有@就@，否则群主角色，否则第一个
    if forced:
        return forced
    owner_role = next((m['id'] for m in members if m['is_owner_role']), None)
    return [owner_role] if owner_role else ([members[0]['id']] if members else [])


# ─────────────────── 角色互动调度：判断角色之间要不要继续接茬 ───────────────────

def _schedule_interaction(candidates, history, all_members):
    """专门给"角色之间互相接茬"用的调度。和 _schedule_speakers 不同：
       - 这不是用户发话场景,而是判断"刚才那条角色发言,有没有别人想反驳/补充/调侃"
       - 没人想接就直接返回 [],让循环自然停
       - 强调"看话题":严肃话题真实回应,日常话题偏调侃
    返回 character_id 列表(0 或 1 个),空列表 = 没人想接、本轮结束。"""
    if not candidates or not history:
        return []

    last = history[-1]
    if last['sender_type'] != 'character':
        # 最后一条不是角色发言,没什么好"接茬"的
        return []

    last_speaker_name = last['sender_name']
    last_content = last['jp'] if last['jp'] else last['zh']

    roster = '\n'.join(f'- {m["id"]}：{m["name"]}' for m in candidates)
    hist_txt = _history_text(history[-6:])

    sched_prompt = f'''你是一个群聊"互动调度器"。下面这群里的角色刚刚有人说话了,你要判断:**还有没有别的角色会自然地接一句**。

最近的群聊记录：
{hist_txt}

刚才{last_speaker_name}说："{last_content}"

可以接话的角色候选(都还没在这轮说过)：
{roster}

判断规则:
1. **有没有人想反驳、补充、调侃、吐槽他?** 如果有,选一个最想接的人接。
2. **看话题分寸**:
   - 如果刚才的话题严肃(涉及理念冲突、过去的事、价值观),让接话的人**真实尖锐**地回应——该怼就怼,该追问就追问。
   - 如果是日常闲聊,让接话的人**偏向调侃、玩笑、互怼**——别端着,像朋友聊天。
3. **如果没人真想接**(比如刚才那句话已经把话题终结了,或候选角色没立场参与),返回空数组。**不要为了凑热闹硬选人**。
4. 一次最多挑 1 个人接,别同时让所有人挤上去。
5. 只能从候选列表里挑。

只返回单行 JSON,不要任何多余文字:
{{"speakers":["角色id"]}}
或没人接：
{{"speakers":[]}}'''

    try:
        resp = claude_client.messages.create(
            model='claude-haiku-4-5-20251001',
            max_tokens=80,
            messages=[{'role': 'user', 'content': sched_prompt}]
        )
        raw = resp.content[0].text.strip()
        parsed = _parse_reply(raw)
        if parsed and isinstance(parsed.get('speakers'), list):
            valid = {m['id'] for m in candidates}
            ids = [s for s in parsed['speakers'] if s in valid]
            return ids[:1]  # 一次最多 1 个
    except Exception as e:
        print(f'[group][interaction] error: {e}')
    return []


# ─────────────────── 让单个角色在群里生成一条回复 ───────────────────

def _generate_one_reply(gid, member, history, user_text, all_members, replying_to=None,
                        image_b64=None, image_media_type=None):
    """让某个角色基于群上下文回复一句。复用单人的 build_system_prompt + 角色人设/记忆。
    返回 {'jp','zh','emotion'} 或 None。

    replying_to: None 表示"响应群主的发言"(第一波);
                 dict {'speaker_name', 'jp', 'zh'} 表示"接刚才某个角色说的话"(互动场景)。
    image_b64 / image_media_type: 用户发的图片(只在第一波传,互动不传)。
    """
    others = '、'.join(m['name'] for m in all_members if m['id'] != member['id'])
    hist_txt = _history_text(history[-10:]) if history else '（群里还没人说话）'

    if replying_to is None:
        # 第一波:响应群主
        image_hint = '\n群主还发了一张图片（你能看到）。' if (image_b64 and replying_to is None) else ''
        group_scene = f'''

【★ 群聊场景——你现在在一个群里】
这个群里还有：{others}（都是别的角色）,以及群主（用户本人）。
下面是群里最近的对话记录：
{hist_txt}

群主刚说："{user_text}"{image_hint}

现在轮到你（{member['name']}）说话。要求：
1. 这是在回应群主的话,符合你的人设。
2. 1 条气泡,简短自然,像群里随口接话。
3. jp 必须是纯日语,zh 是中文翻译。

只返回单行 JSON：
{{"emotion":"情绪","messages":[{{"jp":"日语","zh":"中文"}}]}}'''

        user_msg = f'（群主刚说：{user_text}）请你在群里接话。'

    else:
        # 互动场景:接前一个角色刚说的话
        prev_name = replying_to['speaker_name']
        prev_content = replying_to['jp'] if replying_to.get('jp') else replying_to.get('zh', '')

        group_scene = f'''

【★ 群聊场景——你现在在一个群里】
这个群里还有：{others}（都是别的角色）,以及群主（用户本人）。
下面是群里最近的对话记录：
{hist_txt}

群主一开始说："{user_text}"
然后 {prev_name} 刚说了一句："{prev_content}"

★★★ 现在轮到你（{member['name']}）接 {prev_name} 的话 ★★★
你不是在重新回应群主——群主的那句已经被 {prev_name} 接过了。
你要做的是:**针对 {prev_name} 刚说的这句话**,做出自然的反应。比如:
- 反驳他("不是这样的""你少胡说")
- 补充他("还有件事你没说""说起来...")
- 调侃他("你又来了""说得真好听")
- 追问他("真的吗""那你呢")
- 或者只是接一句感想

要求：
1. 你的话要**明显是针对 {prev_name} 那句**,不是在和群主对话。偶尔提一下对方名字可以,但**不要每句都喊名字**——真朋友之间大部分时候不用叫名字也知道在跟谁说话。
2. 符合你自己的人设,但要让人看出来你是在接他的话。
3. 1 条气泡,简短自然。**绝对不要重复 {prev_name} 刚才说的话**,你要说点新的。
4. jp 必须是纯日语,zh 是中文翻译。

只返回单行 JSON：
{{"emotion":"情绪","messages":[{{"jp":"日语","zh":"中文"}}]}}'''

        user_msg = f'（{prev_name} 刚在群里说：{prev_content}）请你针对他这句话接一句。'

    system_prompt = build_system_prompt('group_' + str(gid), member['id'], user_text) + group_scene

    # ★ 如果有图片(第一波),用多模态格式让角色"看到"图片
    if image_b64 and image_media_type and replying_to is None:
        messages = [{'role': 'user', 'content': [
            {'type': 'image', 'source': {'type': 'base64', 'media_type': image_media_type, 'data': image_b64}},
            {'type': 'text', 'text': user_msg},
        ]}]
    else:
        messages = [{'role': 'user', 'content': user_msg}]

    for attempt in range(3):
        try:
            resp = claude_client.messages.create(
                model='claude-sonnet-4-6',
                max_tokens=400,
                system=system_prompt,
                messages=messages
            )
            raw = resp.content[0].text.strip()
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                first = parsed['messages'][0]
                if first.get('jp', '').strip() and first.get('zh', '').strip():
                    emotion = parsed.get('emotion', '平静')
                    if emotion not in EMOTIONS:
                        emotion = '平静'
                    return {
                        'jp': sanitize_jp(first['jp']),
                        'zh': first['zh'],
                        'emotion': emotion,
                    }
        except Exception as e:
            print(f'[group][{member["id"]}] attempt {attempt+1} error: {e}')
    return None


# ─────────────────── 建群 / 列群 / 详情 / 解散 ───────────────────

@router.post('/group')
async def create_group(data: dict):
    name = (data.get('name') or '').strip()
    owner_user_id = data.get('user_id', 'default')
    member_ids = data.get('member_ids', [])      # 角色 id 列表
    owner_role_id = data.get('owner_role_id')    # 可选：指定哪个角色是"群主角色"（没@时默认他回）

    if not name:
        return JSONResponse({'error': '群名不能为空'}, status_code=400)
    if not member_ids:
        return JSONResponse({'error': '至少拉一个角色进群'}, status_code=400)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        'INSERT INTO groups (name, owner_user_id) VALUES (%s, %s) RETURNING id',
        (name, owner_user_id)
    )
    gid = cur.fetchone()[0]

    # 用户本人作为成员
    cur.execute(
        '''INSERT INTO group_members (group_id, member_type, member_id)
           VALUES (%s, 'user', %s)''',
        (gid, owner_user_id)
    )
    # 角色成员
    for cid in member_ids:
        is_owner = (cid == owner_role_id)
        cur.execute(
            '''INSERT INTO group_members (group_id, member_type, member_id, is_owner_role)
               VALUES (%s, 'character', %s, %s)''',
            (gid, cid, is_owner)
        )
    conn.commit()
    cur.close()
    conn.close()
    print(f'[group] 已建群 id={gid} name={name} members={member_ids}')
    return JSONResponse({'ok': True, 'group_id': gid})


@router.get('/groups')
async def list_groups(user_id: str = 'default'):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, name, avatar_url FROM groups
           WHERE owner_user_id = %s ORDER BY created_at DESC''',
        (user_id,)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    groups = []
    for gid, name, avatar in rows:
        members = _get_member_characters(gid)
        history = _get_group_history(gid, limit=1)
        last = ''
        if history:
            h = history[-1]
            last = (h['sender_name'] + '：' + (h['zh'] or h['jp'])) if h['sender_type'] == 'character' else ('群主：' + h['zh'])
        groups.append({
            'id': gid, 'name': name, 'avatar_url': avatar,
            'member_names': [m['name'] for m in members],
            'last_message': last,
        })
    return JSONResponse({'groups': groups})


@router.get('/group/{gid}')
async def group_detail(gid: int):
    g = _get_group(gid)
    if not g:
        return JSONResponse({'error': 'group not found'}, status_code=404)
    members = _get_member_characters(gid)
    history = _get_group_history(gid, limit=30)
    return JSONResponse({
        'id': g['id'], 'name': g['name'],
        'members': members,
        'messages': history,
    })


@router.delete('/group/{gid}')
async def delete_group(gid: int):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM group_messages WHERE group_id = %s', (gid,))
    cur.execute('DELETE FROM group_members WHERE group_id = %s', (gid,))
    cur.execute('DELETE FROM groups WHERE id = %s', (gid,))
    conn.commit()
    cur.close()
    conn.close()
    return JSONResponse({'ok': True})


# ─────────────────── ★ 核心：群聊一轮 ───────────────────

@router.post('/group/chat')
async def group_chat(data: dict):
    gid          = data.get('group_id')
    user_text    = data.get('text', '')
    user_id      = data.get('user_id', 'default')
    mentioned_id = data.get('mentioned_id')
    allow_interaction = data.get('allow_interaction', True)
    image_b64    = data.get('image_base64', '')   # ★ 群聊图片
    media_type   = data.get('media_type', 'image/jpeg')

    if not gid or (not user_text and not image_b64):
        return JSONResponse({'error': 'group_id 和 text/image 必填'}, status_code=400)

    g = _get_group(gid)
    if not g:
        return JSONResponse({'error': 'group not found'}, status_code=404)

    members = _get_member_characters(gid)
    if not members:
        return JSONResponse({'error': '群里没有角色成员'}, status_code=400)

    # 1) 存用户这句话
    display_text = user_text or '📷 [图片]'
    _save_group_message(gid, 'user', user_id, '', display_text)

    # 2) 智能调度：这一句该谁开口
    history = _get_group_history(gid, limit=12)
    speakers = _schedule_speakers(members, history, display_text, mentioned_id)
    print(f'[group][{gid}] 调度结果 speakers={speakers}')

    replies = []         # 这一轮所有角色回复，按生成顺序
    member_map = {m['id']: m for m in members}

    # 3) 被选中的角色依次回复（第一波能看到图片）
    for cid in speakers:
        member = member_map.get(cid)
        if not member:
            continue
        cur_history = _get_group_history(gid, limit=12)
        reply = _generate_one_reply(gid, member, cur_history, display_text, members,
                                    image_b64=image_b64, image_media_type=media_type)
        if not reply:
            continue
        _save_group_message(gid, 'character', cid, reply['jp'], reply['zh'], reply['emotion'])
        audio = tts_to_b64(reply['jp'], reply['emotion'], member['voice_id'])
        replies.append({
            'sender_id': cid,
            'sender_name': member['name'],
            'jp': reply['jp'], 'zh': reply['zh'],
            'emotion': reply['emotion'],
            'audio_b64': audio,
        })

    # 4) 角色互动:用专门的"互动调度器"判断要不要有人接茬
    #    - MAX_TURNS_PER_ROUND 是硬上限(防止极端情况下无限互怼)
    #    - _schedule_interaction 是软刹车:模型判断没人想接就返回 [],循环自然停
    #    - candidates 只排除"刚刚说话那个人",允许 A→B→A→B 来回交锋(这才是"驳回"的精髓)
    #    - ★ 复读检测:Sonnet 词穷时会复读,这里检测到就强制停
    if allow_interaction and replies:
        turns_used = len(replies)

        while turns_used < MAX_TURNS_PER_ROUND:
            last_speaker_id = replies[-1]['sender_id']
            # 只排除刚刚说话那个人(避免自言自语),其他人都可接茬
            candidates = [m for m in members if m['id'] != last_speaker_id]
            if not candidates:
                break

            cur_history = _get_group_history(gid, limit=12)
            follow = _schedule_interaction(candidates, cur_history, members)
            if not follow:
                # 调度器判断:没人真想接,本轮自然结束
                print(f'[group][{gid}] 互动调度判断无人接茬,本轮结束(turns={turns_used})')
                break

            cid = follow[0]
            member = member_map.get(cid)
            if not member:
                break
            # ★ 关键:互动场景传 replying_to,让 Sonnet 知道这次是接上一个角色的话,不是回用户
            prev = replies[-1]
            replying_to = {
                'speaker_name': prev['sender_name'],
                'jp': prev['jp'],
                'zh': prev['zh'],
            }
            reply = _generate_one_reply(gid, member, cur_history, user_text, members, replying_to=replying_to)
            if not reply:
                break

            # ★ 复读检测:看新回复和最近 3 条是否过度相似(简单的字符相似度)
            new_jp = reply['jp'].strip()
            recent_jps = [r['jp'].strip() for r in replies[-3:]]
            if _is_repetitive(new_jp, recent_jps):
                print(f'[group][{gid}] 检测到复读,本轮强制结束(turns={turns_used}) 新句="{new_jp[:30]}"')
                break

            _save_group_message(gid, 'character', cid, reply['jp'], reply['zh'], reply['emotion'])
            audio = tts_to_b64(reply['jp'], reply['emotion'], member['voice_id'])
            replies.append({
                'sender_id': cid,
                'sender_name': member['name'],
                'jp': reply['jp'], 'zh': reply['zh'],
                'emotion': reply['emotion'],
                'audio_b64': audio,
            })
            turns_used += 1

        if turns_used >= MAX_TURNS_PER_ROUND:
            print(f'[group][{gid}] 撞到硬上限 MAX_TURNS_PER_ROUND={MAX_TURNS_PER_ROUND},本轮强制结束')

    if not replies:
        return JSONResponse({'replies': [], 'note': '这轮没人接话'})

    print(f'[group][{gid}] 本轮共 {len(replies)} 条回复')
    return JSONResponse({'group_id': gid, 'replies': replies})