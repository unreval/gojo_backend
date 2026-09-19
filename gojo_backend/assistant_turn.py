"""Assistant logical-turn identity and canonicalization.

One generation is one logical turn. chat_reply / image_reply write:
  chat_reply:<source>      internal aggregate (full_jp)
  chat_reply:<source>:<n>  user-visible segment

Legacy voice / stream ids used `{source}:reply` and `{source}:reply:<n>`.

This module does not own storage. Callers decide UI vs internal policy.
"""
from __future__ import annotations

import json
import re


_CURRENT_SEGMENT_RE = re.compile(
    r'^(?P<turn>(?:chat_reply|image_reply):.+):(?P<idx>\d+)$'
)
_CURRENT_AGGREGATE_RE = re.compile(
    r'^(?P<turn>(?:chat_reply|image_reply):.+)$'
)
_LEGACY_REPLY_RE = re.compile(
    r'^(?P<turn>.+:reply)(?::(?P<idx>\d+))?$'
)


def _is_assistant_role(role):
    return (role or '').strip() in ('assistant', 'gojo')


def _normalize_event_id(value):
    text = str(value).strip() if value else ''
    return text or ''


def _coerce_extra(value):
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _event_extra(event):
    if not isinstance(event, dict):
        return {}
    for key in ('metadata', 'event_meta', 'extra'):
        if key in event and event.get(key) not in (None, ''):
            return _coerce_extra(event.get(key))
    return {}


def _truthy_flag(value):
    return value is True or value == 1 or value == 'true' or value == 'True'


def _falsey_flag(value):
    return value is False or value == 0 or value == 'false' or value == 'False'


def parse_assistant_event_id(event_id):
    """Parse current or legacy assistant event ids only.

    Returns None for delayed_reply / proactive / other business ids so a
    trailing :digit is never treated as a chat/image segment.
    """
    eid = _normalize_event_id(event_id)
    if not eid:
        return None
    matched = _CURRENT_SEGMENT_RE.match(eid)
    if matched:
        return {
            'assistant_turn_id': matched.group('turn'),
            'segment_index': int(matched.group('idx')),
            'turn_aggregate': False,
        }
    matched = _CURRENT_AGGREGATE_RE.match(eid)
    if matched:
        return {
            'assistant_turn_id': matched.group('turn'),
            'segment_index': None,
            'turn_aggregate': True,
        }
    matched = _LEGACY_REPLY_RE.match(eid)
    if matched:
        idx = matched.group('idx')
        return {
            'assistant_turn_id': matched.group('turn'),
            'segment_index': int(idx) if idx is not None else None,
            'turn_aggregate': idx is None,
        }
    return None


def infer_assistant_identity(role, event_id, extra=None):
    """Stamp assistant_turn_id / segment_index / turn_aggregate onto extra.

    Priority:
    1. explicit metadata assistant_turn_id / segment_index / turn_aggregate
    2. current chat_reply / image_reply id schema
    3. legacy `{source}:reply` / `{source}:reply:<n>`
    4. other assistant events keep event_id as their own turn
    """
    data = _coerce_extra(extra)
    if not _is_assistant_role(role):
        return data

    eid = _normalize_event_id(event_id)
    parsed = parse_assistant_event_id(eid)
    explicit_turn = str(data.get('assistant_turn_id') or '').strip()
    explicit_seg = data.get('segment_index')
    explicit_agg = data.get('turn_aggregate')

    if explicit_turn:
        data['assistant_turn_id'] = explicit_turn
    elif parsed:
        data['assistant_turn_id'] = parsed['assistant_turn_id']
    elif eid:
        data['assistant_turn_id'] = eid

    if explicit_seg is not None and explicit_seg != '':
        try:
            data['segment_index'] = int(explicit_seg)
        except (TypeError, ValueError):
            data['segment_index'] = 0
    elif parsed and parsed['segment_index'] is not None:
        data['segment_index'] = parsed['segment_index']
    elif data.get('assistant_turn_id'):
        data['segment_index'] = 0

    if _truthy_flag(explicit_agg):
        data['turn_aggregate'] = True
    elif _falsey_flag(explicit_agg):
        data['turn_aggregate'] = False
    elif parsed:
        data['turn_aggregate'] = bool(parsed['turn_aggregate'])

    return data


def is_assistant_aggregate(identity, event_id):
    """True only for an identified whole-turn aggregate, never delayed/proactive."""
    data = identity if isinstance(identity, dict) else {}
    if _truthy_flag(data.get('turn_aggregate')):
        return True
    parsed = parse_assistant_event_id(event_id)
    return bool(parsed and parsed['turn_aggregate'])


def is_assistant_segment(identity, event_id):
    """True for chat/image/legacy numbered segments, or explicit non-aggregate siblings."""
    data = identity if isinstance(identity, dict) else {}
    eid = _normalize_event_id(event_id)
    if is_assistant_aggregate(data, eid):
        return False
    parsed = parse_assistant_event_id(eid)
    if parsed and not parsed['turn_aggregate']:
        return True
    turn_id = str(data.get('assistant_turn_id') or '').strip()
    if turn_id and eid and eid != turn_id and data.get('segment_index') is not None:
        return True
    return False


def _empty_turn_fact():
    return {
        'has_segment_rows': False,
        'has_active_segments': False,
        'has_deleted_segments': False,
    }


def _row_status(raw):
    return str((raw or {}).get('status') or 'active').strip() or 'active'


def turn_facts_from_rows(rows):
    """Per-turn segment existence, including deleted historical siblings."""
    facts = {}
    for raw in rows or []:
        eid = _normalize_event_id(
            raw.get('event_id') or raw.get('client_msg_id') or '')
        extra = raw.get('extra') if raw and 'extra' in raw else (raw or {}).get('metadata')
        identity = infer_assistant_identity((raw or {}).get('role'), eid, extra)
        if not eid or not is_assistant_segment(identity, eid):
            continue
        turn_id = str(identity.get('assistant_turn_id') or '').strip()
        if not turn_id:
            continue
        fact = facts.setdefault(turn_id, _empty_turn_fact())
        fact['has_segment_rows'] = True
        if _row_status(raw) == 'deleted':
            fact['has_deleted_segments'] = True
        else:
            fact['has_active_segments'] = True
    return facts


def merge_turn_facts(*groups):
    merged = {}
    for group in groups:
        for turn_id, fact in (group or {}).items():
            current = merged.setdefault(turn_id, _empty_turn_fact())
            for key in current:
                current[key] = bool(current[key] or (fact or {}).get(key))
    return merged


def hidden_aggregate_ids_from_rows(rows):
    """Hide aggregates whenever any segment sibling ever existed.

    Active or deleted siblings both count. True aggregate-only rows stay visible.
    """
    facts = turn_facts_from_rows(rows)
    hidden = set()
    for raw in rows or []:
        eid = _normalize_event_id(
            raw.get('event_id') or raw.get('client_msg_id') or '')
        extra = raw.get('extra') if raw and 'extra' in raw else (raw or {}).get('metadata')
        identity = infer_assistant_identity((raw or {}).get('role'), eid, extra)
        if not eid or not is_assistant_aggregate(identity, eid):
            continue
        turn_id = str(identity.get('assistant_turn_id') or '').strip()
        if turn_id and (facts.get(turn_id) or {}).get('has_segment_rows'):
            hidden.add(eid)
    return hidden


def filter_hidden_aggregates(messages, hidden_ids):
    if not hidden_ids:
        return list(messages or [])
    out = []
    for msg in messages or []:
        keys = (
            msg.get('event_id'),
            msg.get('client_msg_id'),
        )
        if any(_normalize_event_id(key) in hidden_ids for key in keys):
            continue
        out.append(msg)
    return out


def _segment_index(identity):
    try:
        return int(identity.get('segment_index'))
    except (TypeError, ValueError):
        return 0


def _store_extra(event, extra):
    if 'metadata' in event:
        event['metadata'] = extra
    if 'event_meta' in event:
        event['event_meta'] = extra
    if 'extra' in event:
        existing = event.get('extra')
        event['extra'] = (
            extra if not isinstance(existing, str)
            else json.dumps(extra, ensure_ascii=False)
        )
    if 'metadata' not in event and 'event_meta' not in event and 'extra' not in event:
        event['metadata'] = extra


def _join_texts(items, key):
    bits = []
    for _index, event, _identity in items:
        text = str(event.get(key) or '').strip()
        if text:
            bits.append(text)
    return ' '.join(bits)


def _join_segment_events(turn_id, items):
    base = dict(items[0][1])
    joined = _join_texts(items, 'content') or _join_texts(items, 'text')
    if 'content' in base or 'text' not in base:
        base['content'] = joined
    if 'text' in base:
        base['text'] = joined
    if 'subtitle' in base or any(event.get('subtitle') for _i, event, _id in items):
        base['subtitle'] = _join_texts(items, 'subtitle')
    base['event_id'] = turn_id
    extra = _event_extra(base)
    extra['assistant_turn_id'] = turn_id
    extra['turn_aggregate'] = False
    extra['logical_turn'] = True
    _store_extra(base, extra)
    return base


def _logical_turn_event(turn_id, items, fact=None):
    fact = fact or _empty_turn_fact()
    if fact.get('has_segment_rows') and not fact.get('has_active_segments'):
        return None

    if fact.get('has_deleted_segments'):
        items = [
            item for item in items
            if not is_assistant_aggregate(
                item[2],
                item[1].get('event_id') or item[1].get('client_msg_id') or '')
        ]
        if not items:
            return None
        items = sorted(items, key=lambda item: (_segment_index(item[2]), item[0]))
        if len(items) == 1:
            return dict(items[0][1])
        return _join_segment_events(turn_id, items)

    aggregates = [
        item for item in items
        if is_assistant_aggregate(
            item[2], item[1].get('event_id') or item[1].get('client_msg_id') or '')
    ]
    if aggregates:
        aggregates.sort(key=lambda item: (
            0 if _truthy_flag(item[2].get('turn_aggregate')) else 1,
            item[0],
        ))
        event = dict(aggregates[0][1])
        extra = _event_extra(event)
        extra['assistant_turn_id'] = turn_id
        extra['turn_aggregate'] = True
        _store_extra(event, extra)
        event['event_id'] = event.get('event_id') or turn_id
        return event
    items = sorted(items, key=lambda item: (_segment_index(item[2]), item[0]))
    if len(items) == 1:
        return dict(items[0][1])
    return _join_segment_events(turn_id, items)


def collapse_assistant_logical_turns(events, turn_facts=None):
    """Collapse one assistant logical turn to a single internal event.

    A never had segments → aggregate
    B all segments active → prefer aggregate, else join
    C some segments deleted → join remaining active segments only
    D all segments deleted → omit the whole turn
    """
    if not events:
        return []
    inferred = turn_facts_from_rows([
        {
            'event_id': event.get('event_id') or event.get('client_msg_id') or '',
            'client_msg_id': event.get('client_msg_id') or '',
            'role': event.get('role'),
            'extra': _event_extra(event),
            'status': _row_status(event),
        }
        for event in events
    ])
    facts = merge_turn_facts(inferred, turn_facts)
    groups = {}
    others = []
    for index, raw in enumerate(events):
        event = dict(raw or {})
        if _row_status(event) == 'deleted':
            continue
        extra = _event_extra(event)
        eid = _normalize_event_id(
            event.get('event_id') or event.get('client_msg_id') or '')
        identity = infer_assistant_identity(event.get('role'), eid, extra)
        turn_id = ''
        if _is_assistant_role(event.get('role')):
            turn_id = str(identity.get('assistant_turn_id') or '').strip()
        if not turn_id:
            others.append((index, event))
            continue
        groups.setdefault(turn_id, []).append((index, event, identity))

    chosen = []
    for turn_id, items in groups.items():
        logical = _logical_turn_event(turn_id, items, facts.get(turn_id))
        if logical is None:
            continue
        chosen.append((items[0][0], logical))
    chosen.extend(others)
    chosen.sort(key=lambda item: item[0])
    return [event for _index, event in chosen]
