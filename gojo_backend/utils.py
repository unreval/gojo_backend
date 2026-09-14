"""工具函数"""
import json
import re


def _try_parse_json(text):
    if not text or not isinstance(text, str):
        return None
    try:
        obj = json.loads(text)
    except Exception:
        return None
    return obj if isinstance(obj, (dict, list)) else None


def _slice_balanced_object(s, start):
    """从 s[start] 的 '{' 起，按括号配平切出完整 JSON 对象（忽略字符串里的括号）。"""
    if start < 0 or start >= len(s) or s[start] != '{':
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if escape:
                escape = False
            elif ch == '\\':
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
    return None


def extract_json(raw: str):
    """从模型输出里抠 JSON。支持 markdown 代码块、前置解释、后置解释、reasoning。"""
    if not raw or not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    # markdown ```json ... ```
    if '```' in text:
        parts = text.split('```')
        for p in parts:
            p = p.strip()
            if p[:4].lower() == 'json':
                p = p[4:].strip()
            parsed = _try_parse_json(p)
            if isinstance(parsed, dict):
                return parsed
            start = p.find('{')
            if start != -1:
                candidate = _slice_balanced_object(p, start)
                parsed = _try_parse_json(candidate) if candidate else None
                if isinstance(parsed, dict):
                    return parsed

    parsed = _try_parse_json(text)
    if isinstance(parsed, dict):
        return parsed

    # 从每一个 { 尝试配平（跳过字符串里的花括号）
    idx = 0
    while True:
        start = text.find('{', idx)
        if start == -1:
            break
        candidate = _slice_balanced_object(text, start)
        parsed = _try_parse_json(candidate) if candidate else None
        if isinstance(parsed, dict):
            return parsed
        idx = start + 1

    # 最后兜底：第一个 { 到最后一个 }
    i = text.find('{')
    j = text.rfind('}')
    if i != -1 and j > i:
        parsed = _try_parse_json(text[i:j + 1])
        if isinstance(parsed, dict):
            return parsed
    return None


# ══════════════════════════════════════════════
#  内部角色状态块：<<<OFFLINE_CHARACTER_STATES>>>
#  模型有时会在用户可见回复后追加这段，必须拆开保存、绝不能进前端。
# ══════════════════════════════════════════════
OFFLINE_STATE_MARKER = '<<<OFFLINE_CHARACTER_STATES>>>'
_OFFLINE_MARKER_RE = re.compile(r'<<<\s*OFFLINE_CHARACTER_STATES\s*>>>', re.IGNORECASE)
_OFFLINE_KEYS = {'inner', 'intent', 'moodshift', 'anchor', 'from', 'status'}


def _looks_like_offline_state(obj) -> bool:
    """像内部状态，而不像对话 JSON（没有 messages）。"""
    if not isinstance(obj, dict):
        return False
    if 'messages' in obj:
        return False
    return len(set(obj.keys()) & _OFFLINE_KEYS) >= 2


def contains_offline_marker(text: str) -> bool:
    return bool(text and isinstance(text, str) and _OFFLINE_MARKER_RE.search(text))


def _peel_offline_json(text: str, first: bool = False):
    """从文本里抠一段内部状态 JSON。
    first=True：从前往后找第一段；False：只看最后一个 { 起的对象（避免误伤对话 JSON）。
    返回 (剩余文本, state_or_None)。
    """
    if not text or not isinstance(text, str):
        return text or '', None
    if first:
        idx = 0
        while True:
            start = text.find('{', idx)
            if start < 0:
                return text, None
            candidate = _slice_balanced_object(text, start)
            parsed = _try_parse_json(candidate) if candidate else None
            if _looks_like_offline_state(parsed):
                remaining = (text[:start] + text[start + len(candidate):]).strip()
                return remaining, parsed
            idx = start + 1
    start = text.rfind('{')
    if start < 0:
        return text, None
    candidate = _slice_balanced_object(text, start)
    parsed = _try_parse_json(candidate) if candidate else None
    if _looks_like_offline_state(parsed):
        return text[:start].rstrip(), parsed
    return text, None


def split_offline_character_states(raw: str):
    """把用户可见文本和内部角色状态块拆开。返回 (visible_text, state_or_None)。"""
    if not raw or not isinstance(raw, str):
        return (raw or ''), None

    m = _OFFLINE_MARKER_RE.search(raw)
    if not m:
        return _peel_offline_json(raw, first=False)

    before = raw[:m.start()]
    after = raw[m.end():]
    remaining_after, state = _peel_offline_json(after.strip(), first=True)
    if state is None:
        parsed = extract_json(after) if after.strip() else None
        if _looks_like_offline_state(parsed):
            state = parsed
            remaining_after = ''
        else:
            # 标记后面解析不了，也不给用户看
            remaining_after = ''
    visible = (before + ('\n' + remaining_after if remaining_after else '')).strip()
    return visible, state


def sanitize_user_reply(text: str) -> str:
    """发给用户看的最后一道清洗：砍掉内部状态标记及其后内容。"""
    if not text or not isinstance(text, str):
        return text or ''
    text = _OFFLINE_MARKER_RE.split(text, maxsplit=1)[0]
    text, _ = _peel_offline_json(text, first=False)
    return text.strip()


def _extract_state_from_messages(messages):
    if not isinstance(messages, list):
        return None
    for m in messages:
        if not isinstance(m, dict):
            continue
        for k in ('jp', 'zh'):
            val = m.get(k)
            if not isinstance(val, str) or not val:
                continue
            _, state = split_offline_character_states(val)
            if state:
                return state
    return None


def _extract_sibling_offline_state(raw: str):
    """对话 JSON 已经成功解析时，从它后面的兄弟块抠内部状态。"""
    if not raw:
        return None
    start = raw.find('{')
    if start < 0:
        return None
    first = _slice_balanced_object(raw, start)
    if not first:
        return None
    rest = raw[start + len(first):]
    if not rest or not rest.strip():
        return None
    _, state = split_offline_character_states(rest)
    return state


def _sanitize_message_fields(chat: dict) -> dict:
    msgs = chat.get('messages')
    if not isinstance(msgs, list):
        return chat
    for m in msgs:
        if not isinstance(m, dict):
            continue
        for k in ('jp', 'zh'):
            if isinstance(m.get(k), str):
                m[k] = sanitize_user_reply(m[k])
    return chat


def ingest_model_output(raw: str):
    """统一拆模型原文。

    返回 (visible_text, chat_parsed_or_None, offline_state_or_None)
    - visible_text: 给 salvage / 重解析用，不含内部状态块
    - chat_parsed: 抠到的 JSON dict（调用方再校验 messages）
    - offline_state: 内部角色状态，由调用方保存，不要丢
    """
    if not raw or not isinstance(raw, str):
        return '', None, None

    parsed = extract_json(raw)
    chat = parsed if isinstance(parsed, dict) else None
    has_msgs = bool(chat and isinstance(chat.get('messages'), list) and chat['messages'])

    if has_msgs:
        state = (
            _extract_state_from_messages(chat['messages'])
            or _extract_sibling_offline_state(raw)
        )
        _sanitize_message_fields(chat)
        return '', chat, state

    # extract_json 可能先抠到了内部状态 JSON（没有 messages）
    if chat and _looks_like_offline_state(chat):
        state = chat
        start = raw.find('{')
        first = _slice_balanced_object(raw, start) if start >= 0 else None
        rest = raw[start + len(first):] if first else ''
        chat2 = extract_json(rest) if rest.strip() else None
        if isinstance(chat2, dict) and isinstance(chat2.get('messages'), list) and chat2['messages']:
            _sanitize_message_fields(chat2)
            return rest.strip(), chat2, state
        visible, _ = split_offline_character_states(raw)
        return visible, None, state

    visible, state = split_offline_character_states(raw)
    chat2 = extract_json(visible) if visible else None
    if isinstance(chat2, dict) and _looks_like_offline_state(chat2):
        if not state:
            state = chat2
        chat2 = None
    elif isinstance(chat2, dict) and isinstance(chat2.get('messages'), list):
        _sanitize_message_fields(chat2)
    return visible, chat2 if isinstance(chat2, dict) else None, state


def sanitize_jp(jp: str) -> str:
    jp = jp.replace('ふふ', 'へへ')
    jp = re.sub(r'あはは+', 'ふっ', jp)
    jp = re.sub(r'ハハハ+', 'はは', jp)
    jp = re.sub(r'〜+(?=[。!?、\s]|$)', '', jp)
    jp = re.sub(r'…+〜+', '…', jp)
    if jp and jp[-1] not in '。!?…':
        jp = jp + '。'
    return jp


def merge_only_extreme_short(msgs):
    if len(msgs) <= 1:
        return msgs
    result = []
    i = 0
    while i < len(msgs):
        cur = msgs[i]
        if len(cur.get('jp', '')) < 6 and i + 1 < len(msgs):
            nxt = msgs[i + 1]
            merged = {
                'jp': cur['jp'].rstrip('。') + '。' + nxt['jp'],
                'zh': cur['zh'] + nxt['zh'],
                'audio_b64': ''
            }
            result.append(merged)
            i += 2
        else:
            result.append(cur)
            i += 1
    return result


def finalize_user_messages(msgs):
    """发给前端的最后一道清洗。attempt / rescue / fallback 都应走这里。"""
    cleaned = []
    for m in msgs or []:
        if not isinstance(m, dict):
            continue
        jp = sanitize_jp(sanitize_user_reply(str(m.get('jp', '') or '')))
        zh = sanitize_user_reply(str(m.get('zh', '') or ''))
        if not jp.strip() and not zh.strip():
            continue
        out = dict(m)
        out['jp'] = jp
        out['zh'] = zh
        cleaned.append(out)
    return merge_only_extreme_short(cleaned)
