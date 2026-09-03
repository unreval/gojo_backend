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
