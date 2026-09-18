"""Conservative deterministic auto-pin for the current working set.

Pin = information that must not fall out of the hot window this phase.
Not long-term memory. No LLM on the Fast Path. Reading a pin never
raises its priority.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Sequence, Tuple


AUTO_PIN_TYPES = (
    'current_task',
    'explicit_decision',
    'pending_request',
    'deadline',
    'explicit_promise',
    'unresolved_question',
    'unresolved_conflict',
)

PIN_PRIORITY = {
    'pending_request': 92,
    'explicit_decision': 90,
    'current_task': 88,
    'deadline': 86,
    'explicit_promise': 84,
    'unresolved_conflict': 83,
    'unresolved_question': 80,
}

# Casual chatter that must never become a working-set pin.
_CASUAL = re.compile(
    r'(吃饭|吃晚饭|吃午饭|吃早餐|洗澡|冲凉|睡觉|午睡|早安|晚安|'
    r'我今天好累|好累啊|好累|哈哈+|呵呵+|嗯嗯+|天气|下雨)'
)

_PATTERNS = {
    'pending_request': [
        re.compile(r'(不要\s*push|先不要\s*push|先别\s*push|别\s*push|不要提交|先不要提交|改完先给我看|不要推)'),
        re.compile(r'(先别提交|先不要提交|先不要推送)'),
    ],
    'explicit_decision': [
        re.compile(r'(就按这个方案|就按这个|就这么定|就这样定|按这个方案|就用这个)'),
    ],
    'current_task': [
        re.compile(r'(先把.{1,30}做完|继续做.{1,30}|当前任务是.{1,40}|先完成.{1,30})'),
    ],
    'deadline': [
        re.compile(r'(今晚之前|今天之内|明天之前|截止|deadline|在.{0,6}之前弄好|在.{0,6}之前做好)'),
    ],
    'explicit_promise': [
        re.compile(r'(我回来之后继续|回来再继续|等我回来.{0,8}继续)'),
    ],
    'unresolved_question': [
        re.compile(r'(还没解决|这个问题还|仍未解决|还没搞清楚)'),
    ],
    'unresolved_conflict': [
        re.compile(r'(互相冲突|还在卡住|被阻塞|冲突没解决)'),
    ],
}

_PUSH_BLOCK = re.compile(r'(不要\s*push|先不要\s*push|先别\s*push|别\s*push|不要提交|不要推)')
_PUSH_ALLOW = re.compile(r'(现在\s*push|可以\s*push|推上去|现在提交|现在推)')


def _now(now=None):
    value = now or datetime.now(timezone.utc)
    if getattr(value, 'tzinfo', None) is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _clip(text: str, n=180) -> str:
    body = re.sub(r'\s+', ' ', (text or '')).strip()
    return body[:n]


def is_casual_chatter(text: str) -> bool:
    body = (text or '').strip()
    if not body:
        return True
    if any(p.search(body) for patterns in _PATTERNS.values() for p in patterns):
        return False
    if _PUSH_ALLOW.search(body):
        return False
    return bool(_CASUAL.search(body))


def detect_auto_pins(text: str) -> List[dict]:
    """Return zero or more pin drafts. Deterministic, no LLM."""
    body = (text or '').strip()
    if not body or is_casual_chatter(body):
        return []
    found = []
    if _PUSH_ALLOW.search(body):
        found.append({
            'pin_type': 'pending_request',
            'topic': 'git-push',
            'text': _clip(body),
            'supersede_topic': 'git-push-block',
            'action': 'allow-push',
        })
    for pin_type, patterns in _PATTERNS.items():
        for pattern in patterns:
            match = pattern.search(body)
            if not match:
                continue
            topic = pin_type
            if pin_type == 'pending_request' and _PUSH_BLOCK.search(body):
                topic = 'git-push-block'
            found.append({
                'pin_type': pin_type,
                'topic': topic,
                'text': _clip(body),
                'span': match.group(0),
            })
            break
    # De-dup by (pin_type, topic)
    uniq = {}
    for item in found:
        uniq[(item['pin_type'], item.get('topic') or item['pin_type'])] = item
    return list(uniq.values())


def _pin_id(user_id, character_id, pin_type, topic):
    return f'auto:{character_id}:{user_id}:{pin_type}:{topic}'[:120]


def maybe_auto_pin(
    user_id,
    character_id,
    text,
    *,
    source_event_ids: Sequence[str] = (),
    now=None,
):
    """Create/supersede working-set pins. Never bumps priority on read."""
    drafts = detect_auto_pins(text)
    if not drafts:
        return []
    try:
        from context_layer import list_pins, set_pin_status, upsert_pin
    except Exception:
        return []
    now = _now(now)
    ids = tuple(str(x).strip() for x in (source_event_ids or ()) if str(x).strip())
    written = []
    for draft in drafts:
        pin_type = draft['pin_type']
        topic = draft.get('topic') or pin_type
        if draft.get('action') == 'allow-push':
            for item in list_pins(user_id, character_id, status='active', limit=20, now=now):
                meta_type = item.get('pin_type')
                if meta_type == 'pending_request' and (
                    '不要 push' in (item.get('text') or '')
                    or '不要push' in (item.get('text') or '').replace(' ', '')
                    or item.get('pin_id', '').endswith('git-push-block')
                ):
                    set_pin_status(item['pin_id'], 'superseded')
            pid = _pin_id(user_id, character_id, 'pending_request', 'git-push-allow')
            row = upsert_pin(
                user_id, character_id, draft['text'],
                pin_type='pending_request',
                source_event_ids=ids,
                priority=PIN_PRIORITY['pending_request'],
                pin_id=pid,
                status='active',
            )
            written.append(row)
            continue
        pid = _pin_id(user_id, character_id, pin_type, topic)
        # Conflicting same-type pins: supersede the previous active one with a
        # different pin_id / opposite instruction.
        for item in list_pins(user_id, character_id, status='active', limit=20, now=now):
            if item.get('pin_id') == pid:
                continue
            if item.get('pin_type') != pin_type:
                continue
            if pin_type == 'pending_request' and topic == 'git-push-block':
                if str(item.get('pin_id') or '').endswith('git-push-allow'):
                    set_pin_status(item['pin_id'], 'superseded')
        row = upsert_pin(
            user_id, character_id, draft['text'],
            pin_type=pin_type,
            source_event_ids=ids,
            priority=PIN_PRIORITY.get(pin_type, 80),
            pin_id=pid,
            status='active',
            expires_at=(now + timedelta(days=14)) if pin_type in ('deadline', 'current_task') else None,
        )
        written.append(row)
    return written
