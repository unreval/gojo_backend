"""工具函数"""
import json
import re


def extract_json(raw: str):
    raw = raw.strip()
    if '```' in raw:
        parts = raw.split('```')
        for p in parts:
            p = p.strip()
            if p.startswith('json'):
                p = p[4:].strip()
            if p.startswith('{'):
                raw = p
                break
    raw = raw.replace('\n', ' ').replace('\r', '')
    try:
        return json.loads(raw)
    except:
        pass
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
