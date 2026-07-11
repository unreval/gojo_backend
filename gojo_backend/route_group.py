"""群聊路由（第二步·记忆互通版）

端点：
  POST   /group                 建群（传群名 + 成员角色列表）
  GET    /groups                列出某用户的所有群
  GET    /group/{gid}           群详情（成员 + 最近消息）
  DELETE /group/{gid}           解散群
  POST   /group/chat            ★ 核心：用户在群里发一句 → 智能调度谁回 → 角色依次回复（含角色互动）
  POST   /group/chat/continue   逐条互动（前端轮询，支持打断）

第二步已完成：
  - ★ 记忆互通：角色在群里读真实 user_id 的长期记忆（含 shared 共享桶），
    群里用户透露的新事实由 extract_and_save_group_memory 提取并存入 shared 桶，
    私聊立刻可用；反之私聊提取的事实群里也能读到。
  - ★ 修复：原来 build_system_prompt 传的是 'group_<gid>' 假 user_id，
    导致角色在群里读的是空记忆桶（记忆混乱的根源），现已改为真实群主 user_id。

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
from user_memory import extract_and_save_group_memory   # ★ 群聊专用记忆提取

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
                'avatar_url': char.get('avatar_url'),   # ★ 前端头像用
                'is_owner_role': bool(is_owner_role),
            })
    return result


def _get_group_history(gid: int, limit: int = 12):
    """取群最近若干条消息，返回 [{msg_id, ts, sender_type, sender_id, sender_name, jp, zh}]（旧→新）。
    ★ msg_id/ts 供前端同步与去重；ts 是 epoch 毫秒。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT id, sender_type, sender_id, jp, zh, timestamp FROM group_messages
           WHERE group_id = %s ORDER BY timestamp DESC LIMIT %s''',
        (gid, limit)
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    rows = rows[::-1]  # 转成旧→新
    history = []
    name_cache = {}
    for mid, sender_type, sender_id, jp, zh, ts in rows:
        if sender_type == 'character':
            if sender_id not in name_cache:
                c = get_character(sender_id)
                name_cache[sender_id] = c['name'] if c else sender_id
            sender_name = name_cache[sender_id]
        else:
            sender_name = '群主'
        history.append({
            'msg_id': mid,
            'ts': int(ts.timestamp() * 1000) if ts else None,
            'sender_type': sender_type,
            'sender_id': sender_id,
            'sender_name': sender_name,
            'jp': jp or '',
            'zh': zh or '',
        })
    return history


def _group_msg_count(gid: int) -> int:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('SELECT COUNT(*) FROM group_messages WHERE group_id = %s', (gid,))
    n = cur.fetchone()[0]
    cur.close()
    conn.close()
    return n


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
1. 先想:刚才这句话说完,一个真实群聊的自然走向是什么?
   - 话已经说完整、或已经得到回应 → **没人需要接,返回空数组**(这应该是最常见的结果,不要为了热闹硬选人)
   - 确实有人会自然接一句 → 挑最想接的那个人
2. 接话的动机是多样的,**不要总往"反驳/互怼"上靠**:
   附和共鸣、补充信息、聊起自己相关的事、简单搭个腔、关心群主、开个小玩笑,
   偶尔才是不同意见。朋友之间大多数时候是顺着聊,不是抬杠。
3. 已经你来我往接了 2 轮以上 → 强烈倾向返回空数组,真实群聊不会没完没了地对线。
4. 一次最多挑 1 个人接。
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
                        image_b64=None, image_media_type=None, user_id='default',
                        already_said=None):
    """让某个角色基于群上下文回复一句。复用单人的 build_system_prompt + 角色人设/记忆。
    返回 {'jp','zh','emotion'} 或 None。

    replying_to: None 表示"响应群主的发言"(第一波);
                 dict {'speaker_name', 'jp', 'zh'} 表示"接刚才某个角色说的话"(互动场景)。
    image_b64 / image_media_type: 用户发的图片(只在第一波传,互动不传)。
    user_id: ★ 群主真实 user_id——组 prompt 时用它读长期记忆（含 shared 桶），
             这样角色在群里也认识你。绝对不要再传 'group_xx' 这种假 ID。
    already_said: ★ 第一波里排在你前面的角色已经说了什么
                  [{'sender_name','zh'}]——用来强制后发言者换角度,防止"复读机合唱"。
    """
    others = '、'.join(m['name'] for m in all_members if m['id'] != member['id'])
    hist_txt = _history_text(history[-10:]) if history else '（群里还没人说话）'

    # ★ 声纹隔离:防止多个角色输出趋同,像"一个AI套了几个名字"
    voice_lock = f'''

【★ 你不是"群里的AI",你是{member['name']}本人】
群里每个人的说话方式截然不同。你只用你自己的语气、口癖、节奏和态度说话。
哪怕要表达和别人相同的意思,你的说法、句式、切入点也必须和对方完全不像。
禁止"先共情一句+再给建议"这种通用模板腔——按你的性格想怎么说就怎么说:
可以只调侃不建议、可以只丢一句短话、可以说反话、可以岔开,只要像"你"。

【关于群里的其他角色】
{others} 未必和你来自同一个世界。如果对方是你认识的人,按你们真实的关系相处;
如果对方谈到你不认识的人名、事件、世界观设定,那是"他的事",你可以好奇、可以调侃,
但不要不懂装懂顺着编,更不要把对方世界的设定当成你自己世界里的事实。'''

    if replying_to is None:
        # 第一波:响应群主
        image_hint = '\n群主还发了一张图片（你能看到）。' if (image_b64 and replying_to is None) else ''

        # ★ 防复读机合唱:告诉后发言者前面的人已经说了什么,强制换角度
        said_block = ''
        if already_said:
            said_lines = '\n'.join(f"- {s['sender_name']}已经说了：「{s['zh']}」" for s in already_said)
            said_block = f'''

【★★ 本轮已经有人回应过群主了——你绝对不能当复读机 ★★】
{said_lines}
同样的意思被说第二遍毫无意义。你必须做到以下之一:
- 换一个完全不同的切入角度(他讲道理你就讲感受,他严肃你就轻松,反之亦然)
- 顺着他的话自然补充,或表达你自己不同的感受
- 聊群主那句话里被他忽略掉的另一个部分
禁止重复上面已出现的建议、观点和句式,哪怕换个说法复述也不行。
注意:换角度≠必须唱反调,自然就好。'''

        group_scene = voice_lock + f'''

【★ 群聊场景——你现在在一个群里】
这个群里还有：{others}（都是别的角色）,以及群主（用户本人）。
下面是群里最近的对话记录：
{hist_txt}

群主刚说："{user_text}"{image_hint}{said_block}

现在轮到你（{member['name']}）说话。要求：
1. 这是在回应群主的话,符合你的人设。
2. 用 1~3 条气泡回复,像真人聊天一样自然。简单的话1条就够,想展开就拆2~3条。
3. jp 必须是纯日语,zh 是中文翻译。

只返回单行 JSON：
{{"emotion":"情绪","messages":[{{"jp":"日语","zh":"中文"}}]}}'''

        user_msg = f'（群主刚说：{user_text}）请你在群里接话。'

    else:
        # 互动场景:接前一个角色刚说的话
        prev_name = replying_to['speaker_name']
        prev_content = replying_to['jp'] if replying_to.get('jp') else replying_to.get('zh', '')

        group_scene = voice_lock + f'''

【★ 群聊场景——你现在在一个群里】
这个群里还有：{others}（都是别的角色）,以及群主（用户本人）。
下面是群里最近的对话记录：
{hist_txt}

群主一开始说："{user_text}"
然后 {prev_name} 刚说了一句："{prev_content}"

★★★ 现在轮到你（{member['name']}）接 {prev_name} 的话 ★★★
你不是在重新回应群主——群主的那句已经被 {prev_name} 接过了。
你要做的是:对 {prev_name} 刚说的话做出【自然】的反应。反应方式是多样的,
按此刻的语境和你的心情挑**最自然**的一种,不要总选同一种:
- 顺着聊/附和("确实""就是说")
- 补充点什么,或聊起自己相关的事
- 简单搭一句腔——不必句句都有观点
- 话题关于群主时,自然地关心她
- 开个小玩笑、轻轻调侃
- **只有真的不同意时才反驳**——朋友不会为了热闹而抬杠,别把每次接话都变成对线

要求：
1. 你的话要**明显是针对 {prev_name} 那句**,不是在和群主对话。偶尔提一下对方名字可以,但**不要每句都喊名字**——真朋友之间大部分时候不用叫名字也知道在跟谁说话。
2. 符合你自己的人设,但要让人看出来你是在接他的话。
3. 用 1~3 条气泡回复,像真人聊天一样自然。**绝对不要重复 {prev_name} 刚才说的话**,你要说点新的。
   群里已经有人给过的建议/观点,你换个说法再讲一遍也算重复——禁止。
4. jp 必须是纯日语,zh 是中文翻译。

只返回单行 JSON：
{{"emotion":"情绪","messages":[{{"jp":"日语","zh":"中文"}}]}}'''

        user_msg = f'（{prev_name} 刚在群里说：{prev_content}）请你针对他这句话接一句。'

    # ★★★ 核心修复：用真实 user_id 组装 prompt。
    #     原来这里是 'group_' + str(gid)，角色读的是空记忆桶——群聊记忆混乱的根源。
    system_prompt = build_system_prompt(user_id, member['id'], user_text) + group_scene

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
                max_tokens=800,
                system=system_prompt,
                messages=messages
            )
            raw = resp.content[0].text.strip()
            parsed = _parse_reply(raw)
            if parsed and isinstance(parsed.get('messages'), list) and len(parsed['messages']) > 0:
                all_msgs = parsed['messages']
                # 确保每条都有 jp 和 zh
                valid = [m for m in all_msgs if m.get('jp', '').strip() and m.get('zh', '').strip()]
                if valid:
                    emotion = parsed.get('emotion', '平静')
                    if emotion not in EMOTIONS:
                        emotion = '平静'
                    return {
                        'messages': [{'jp': sanitize_jp(m['jp']), 'zh': m['zh']} for m in valid],
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
            'msg_count': _group_msg_count(gid),   # ★ 未读红点用
        })
    return JSONResponse({'groups': groups})


@router.get('/group/{gid}')
async def group_detail(gid: int):
    g = _get_group(gid)
    if not g:
        return JSONResponse({'error': 'group not found'}, status_code=404)
    members = _get_member_characters(gid)
    history = _get_group_history(gid, limit=50)
    return JSONResponse({
        'id': g['id'], 'name': g['name'],
        'members': members,
        'messages': history,
        'msg_count': _group_msg_count(gid),   # ★ 进群即已读的基准
    })


@router.delete('/group/{gid}/messages')
async def clear_group_messages(gid: int):
    """★ 只清空聊天记录，群和成员保留（前端"清空"按钮用）。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('DELETE FROM group_messages WHERE group_id = %s', (gid,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    print(f'[group][{gid}] 已清空 {deleted} 条群消息')
    return JSONResponse({'ok': True, 'deleted': deleted})


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

    # ★ 以群的创建者为准（前端传错也不怕），这是记忆读写的真实 user_id
    owner_id = g.get('owner_user_id') or user_id

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
        # ★ 把本轮前面的人已经说的话喂给后发言者,强制他换角度,不许合唱
        already = [{'sender_name': r['sender_name'], 'zh': r['zh']} for r in replies] or None
        reply = _generate_one_reply(gid, member, cur_history, display_text, members,
                                    image_b64=image_b64, image_media_type=media_type,
                                    user_id=owner_id, already_said=already)
        if not reply:
            continue
        for m in reply['messages']:
            mid = _save_group_message(gid, 'character', cid, m['jp'], m['zh'], reply['emotion'])
            audio = tts_to_b64(m['jp'], reply['emotion'], member['voice_id'])
            replies.append({
                'msg_id': mid,
                'sender_id': cid,
                'sender_name': member['name'],
                'jp': m['jp'], 'zh': m['zh'],
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
            reply = _generate_one_reply(gid, member, cur_history, user_text, members,
                                        replying_to=replying_to, user_id=owner_id)
            if not reply:
                break

            # ★ 复读检测:用第一条气泡跟最近3条比
            first_jp = reply['messages'][0]['jp'].strip()
            recent_jps = [r['jp'].strip() for r in replies[-3:]]
            if _is_repetitive(first_jp, recent_jps):
                print(f'[group][{gid}] 检测到复读,本轮强制结束(turns={turns_used}) 新句="{first_jp[:30]}"')
                break

            for m in reply['messages']:
                mid = _save_group_message(gid, 'character', cid, m['jp'], m['zh'], reply['emotion'])
                audio = tts_to_b64(m['jp'], reply['emotion'], member['voice_id'])
                replies.append({
                    'msg_id': mid,
                    'sender_id': cid,
                    'sender_name': member['name'],
                    'jp': m['jp'], 'zh': m['zh'],
                    'emotion': reply['emotion'],
                    'audio_b64': audio,
                })
            turns_used += 1

        if turns_used >= MAX_TURNS_PER_ROUND:
            print(f'[group][{gid}] 撞到硬上限 MAX_TURNS_PER_ROUND={MAX_TURNS_PER_ROUND},本轮强制结束')

    if not replies:
        return JSONResponse({'replies': [], 'note': '这轮没人接话'})

    # 5) ★ 后台提取记忆:从群主这句话里抽用户事实(shared)和定向告知(目标角色的 told 桶)
    if user_text and replies:
        round_transcript = f'群主：{user_text}\n' + '\n'.join(
            f"{r['sender_name']}：{r['zh']}" for r in replies
        )
        threading.Thread(
            target=extract_and_save_group_memory,
            args=(owner_id, user_text, round_transcript,
                  [{'id': m['id'], 'name': m['name']} for m in members]),
            daemon=True
        ).start()

    print(f'[group][{gid}] 本轮共 {len(replies)} 条回复')
    return JSONResponse({'group_id': gid, 'replies': replies})


# ─────────────────── 逐条互动（前端轮询,支持打断）───────────────────

@router.post('/group/chat/continue')
async def group_chat_continue(data: dict):
    """前端每调一次,返回一条角色互动回复(或 done=True 表示没人想说了)。
    前端循环调这个接口,每条回复到手就显示+播音,用户随时可以打断。"""
    gid         = data.get('group_id')
    turns_used  = data.get('turns_used', 0)
    user_text   = data.get('user_text', '')   # 这一轮最初用户说的话(给 prompt 上下文)

    if not gid:
        return JSONResponse({'error': 'group_id 必填'}, status_code=400)

    if turns_used >= MAX_TURNS_PER_ROUND:
        return JSONResponse({'reply': None, 'done': True, 'reason': 'max_turns'})

    # ★ 拿群的创建者作为真实 user_id（记忆读取用）
    g = _get_group(gid)
    if not g:
        return JSONResponse({'reply': None, 'done': True, 'reason': 'group_missing'})
    owner_id = g.get('owner_user_id') or 'default'

    members = _get_member_characters(gid)
    if not members:
        return JSONResponse({'reply': None, 'done': True, 'reason': 'no_members'})

    member_map = {m['id']: m for m in members}
    history = _get_group_history(gid, limit=12)

    # 找到最后一条角色发言
    last_char_msg = None
    for h in reversed(history):
        if h['sender_type'] == 'character':
            last_char_msg = h
            break

    if not last_char_msg:
        return JSONResponse({'reply': None, 'done': True, 'reason': 'no_char_msg'})

    last_speaker_id = last_char_msg['sender_id']
    candidates = [m for m in members if m['id'] != last_speaker_id]
    if not candidates:
        return JSONResponse({'reply': None, 'done': True, 'reason': 'no_candidates'})

    # 调度:有没有人想接茬?
    follow = _schedule_interaction(candidates, history, members)
    if not follow:
        print(f'[group][{gid}] continue: 无人接茬, done')
        return JSONResponse({'reply': None, 'done': True, 'reason': 'no_one_wants'})

    cid = follow[0]
    member = member_map.get(cid)
    if not member:
        return JSONResponse({'reply': None, 'done': True, 'reason': 'member_missing'})

    # 生成回复(互动场景:接上一个角色的话)
    replying_to = {
        'speaker_name': last_char_msg.get('sender_name', ''),
        'jp': last_char_msg.get('jp', ''),
        'zh': last_char_msg.get('zh', ''),
    }
    reply = _generate_one_reply(gid, member, history, user_text, members,
                                replying_to=replying_to, user_id=owner_id)
    if not reply:
        return JSONResponse({'replies': [], 'done': True, 'reason': 'gen_failed'})

    # 复读检测(用第一条气泡)
    first_jp = reply['messages'][0]['jp'].strip()
    recent_jps = [h.get('jp', '').strip() for h in history[-3:] if h.get('sender_type') == 'character']
    if _is_repetitive(first_jp, recent_jps):
        print(f'[group][{gid}] continue: 检测到复读, done')
        return JSONResponse({'replies': [], 'done': True, 'reason': 'repetitive'})

    # 存 + TTS(每条气泡分别处理)
    result_replies = []
    for m in reply['messages']:
        mid = _save_group_message(gid, 'character', cid, m['jp'], m['zh'], reply['emotion'])
        audio = tts_to_b64(m['jp'], reply['emotion'], member['voice_id'])
        result_replies.append({
            'msg_id': mid,
            'sender_id': cid,
            'sender_name': member['name'],
            'jp': m['jp'], 'zh': m['zh'],
            'emotion': reply['emotion'],
            'audio_b64': audio,
        })

    print(f'[group][{gid}] continue: {member["name"]} replied {len(result_replies)} bubbles (turns={turns_used+1})')
    return JSONResponse({
        'replies': result_replies,
        'done': False,
    })