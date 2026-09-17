"""Recall/Search v2 phase-1 context layer.

Adaptive hot window + rolling summary + pinned working set + budget assembly.
Does not replace chat_log as canonical Raw Event. Summaries and pins are
derived objects with provenance, never written back as Raw Events or
short_memory facts.
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from db import get_conn
from context_budget import (
    BudgetConfig,
    ContextBudgetManager,
    ContextItem,
    MIN_SUMMARY_EVENTS,
    estimate_tokens,
)

_FRAG_SPLIT = re.compile(r'[，。,.\s;；、！!？?：:\n]+')
_CJK_CHUNK = re.compile(r'[\u4e00-\u9fff]{2,}')

_memory_lock = threading.Lock()
_USE_MEMORY_STORE = False
_SUMMARIES: Dict[str, dict] = {}
_PINS: Dict[str, dict] = {}

CONTEXT_LAYER_DDL = (
    '''CREATE TABLE IF NOT EXISTS rolling_summaries (
        summary_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        text TEXT NOT NULL,
        source_event_ids TEXT NOT NULL DEFAULT '[]',
        range_start TIMESTAMPTZ,
        range_end TIMESTAMPTZ,
        status TEXT NOT NULL DEFAULT 'active',
        token_cost INTEGER,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_rolling_summaries_user_char
       ON rolling_summaries (user_id, character_id, status, range_end DESC)''',
    '''CREATE TABLE IF NOT EXISTS pinned_context (
        pin_id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        character_id TEXT NOT NULL,
        text TEXT NOT NULL,
        pin_type TEXT NOT NULL DEFAULT 'decision',
        source_event_ids TEXT NOT NULL DEFAULT '[]',
        priority INTEGER NOT NULL DEFAULT 50,
        status TEXT NOT NULL DEFAULT 'active',
        expires_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_pinned_context_active
       ON pinned_context (user_id, character_id, status, priority DESC)''',
)

PIN_STATUSES = ('active', 'resolved', 'expired', 'superseded')
SUMMARY_STATUSES = ('active', 'invalidated', 'superseded')


def init_context_layer_tables():
    conn = get_conn()
    cur = conn.cursor()
    try:
        for stmt in CONTEXT_LAYER_DDL:
            cur.execute(stmt)
        conn.commit()
    finally:
        cur.close()
        conn.close()


def use_memory_store(enabled=True):
    """Tests only: keep summaries/pins in process memory."""
    global _USE_MEMORY_STORE
    _USE_MEMORY_STORE = bool(enabled)
    if enabled:
        reset_memory_store()


def reset_memory_store():
    with _memory_lock:
        _SUMMARIES.clear()
        _PINS.clear()


def _now_utc(now=None):
    if now is None:
        return datetime.now(timezone.utc)
    if getattr(now, 'tzinfo', None) is None:
        return now.replace(tzinfo=timezone.utc)
    return now


def _json_ids(value) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        raw = value
    else:
        text = str(value).strip()
        if not text:
            return ()
        try:
            parsed = json.loads(text)
            raw = parsed if isinstance(parsed, list) else [text]
        except Exception:
            raw = [text]
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _dump_ids(ids: Sequence[str]) -> str:
    return json.dumps(list(_json_ids(ids)), ensure_ascii=False)


def _parse_extra(extra):
    if isinstance(extra, dict):
        return extra
    if not extra:
        return {}
    try:
        parsed = json.loads(extra)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _event_minutes(newer, older) -> float:
    if newer is None or older is None:
        return 0.0
    left, right = _now_utc(newer), _now_utc(older)
    return abs((left - right).total_seconds()) / 60.0


def _fragments(text: str):
    parts = [p for p in _FRAG_SPLIT.split(text or '') if len(p) >= 2]
    if parts:
        return parts
    return _CJK_CHUNK.findall(text or '')


def _overlap_score(a: str, b: str) -> float:
    fa, fb = set(_fragments(a)), set(_fragments(b))
    if not fa or not fb:
        return 0.0
    return len(fa & fb) / float(max(1, min(len(fa), len(fb))))


def is_continuous(newer: dict, older: dict, selected_ids: set, config: BudgetConfig) -> bool:
    """Deterministic continuity. No LLM."""
    if not newer or not older:
        return False
    newer_id = str(newer.get('event_id') or '')
    older_id = str(older.get('event_id') or '')
    newer_reply = str(newer.get('reply_to_event_id') or '')
    older_reply = str(older.get('reply_to_event_id') or '')
    if older_reply and older_reply in selected_ids:
        return True
    if newer_reply and newer_reply == older_id:
        return True
    if older_reply and older_reply == newer_id:
        return True
    nmeta = newer.get('metadata') or {}
    ometa = older.get('metadata') or {}
    for key in ('task_id', 'assistant_turn_id', 'thread_id', 'conversation_id'):
        nv, ov = str(nmeta.get(key) or ''), str(ometa.get(key) or '')
        if nv and nv == ov:
            return True
    gap = _event_minutes(newer.get('timestamp'), older.get('timestamp'))
    if gap <= config.continue_gap_minutes:
        return True
    if gap > config.shift_gap_minutes:
        return False
    return _overlap_score(newer.get('content') or '', older.get('content') or '') >= 0.12


def select_hot_window(
    events: Sequence[dict],
    *,
    config: Optional[BudgetConfig] = None,
    now=None,
    token_budget: Optional[int] = None,
) -> Tuple[List[dict], List[dict]]:
    """Walk newest→oldest. Token + continuity, not LIMIT 40.

    Returns (hot_events oldest-first, spill_events oldest-first).
    Continuity extends the time horizon; token budget and max_events cap growth.
    """
    cfg = config or BudgetConfig()
    rows = [dict(event) for event in (events or []) if event]
    if not rows:
        return [], []
    budget = int(token_budget if token_budget is not None else cfg.hot_token_budget)
    now = _now_utc(now)
    newest_first = list(reversed(rows))
    selected = []
    selected_ids = set()
    used = 0
    for event in newest_first:
        n = len(selected) + 1
        if n > cfg.hot_max_events:
            break
        text = event.get('content') or ''
        cost = estimate_tokens(text)
        age = _event_minutes(now, event.get('timestamp'))
        neighbor = selected[-1] if selected else None
        continuous = (
            is_continuous(neighbor, event, selected_ids, cfg) if neighbor else True
        )
        if n <= cfg.hot_min_events:
            selected.append(event)
            selected_ids.add(str(event.get('event_id') or ''))
            used += cost
            continue
        if used + cost > budget:
            break
        if age > cfg.hot_time_horizon_minutes and not continuous:
            break
        selected.append(event)
        selected_ids.add(str(event.get('event_id') or ''))
        used += cost
    hot = list(reversed(selected))
    if not hot:
        return [], rows
    oldest_hot_ts = hot[0].get('timestamp')
    spill = []
    hot_ids = {str(ev.get('event_id') or '') for ev in hot}
    for event in rows:
        eid = str(event.get('event_id') or '')
        if eid and eid in hot_ids:
            continue
        ts = event.get('timestamp')
        if oldest_hot_ts is None or ts is None or _now_utc(ts) <= _now_utc(oldest_hot_ts):
            spill.append(event)
    return hot, spill


def draft_rolling_summary(events: Sequence[dict]) -> dict:
    """Deterministic placeholder. Not an LLM summarizer."""
    rows = [dict(ev) for ev in (events or []) if ev]
    ids = tuple(str(ev.get('event_id') or '') for ev in rows if ev.get('event_id'))
    if not rows:
        return {
            'text': '',
            'source_event_ids': (),
            'range_start': None,
            'range_end': None,
        }
    start = rows[0].get('timestamp')
    end = rows[-1].get('timestamp')
    samples = []
    picks = []
    if len(rows) <= 6:
        picks = rows
    else:
        picks = rows[:2] + rows[len(rows) // 2:len(rows) // 2 + 2] + rows[-2:]
    for ev in picks:
        snippet = re.sub(r'\s+', ' ', (ev.get('content') or '').strip())[:40]
        if snippet:
            samples.append(snippet)
    start_s = start.isoformat() if hasattr(start, 'isoformat') else str(start or '?')
    end_s = end.isoformat() if hasattr(end, 'isoformat') else str(end or '?')
    text = (
        f'此前 {len(rows)} 条连续对话（{start_s}～{end_s}）已离开热窗口，原文仍保留。'
    )
    if samples:
        text += '要点摘录：' + ' / '.join(samples)
    return {
        'text': text,
        'source_event_ids': ids,
        'range_start': start,
        'range_end': end,
    }


def _row_summary(row) -> dict:
    return {
        'summary_id': row[0],
        'user_id': row[1],
        'character_id': row[2],
        'text': row[3],
        'source_event_ids': _json_ids(row[4]),
        'range_start': row[5],
        'range_end': row[6],
        'status': row[7],
        'token_cost': row[8],
        'created_at': row[9],
        'updated_at': row[10],
    }


def save_rolling_summary(
    user_id, character_id, text, source_event_ids,
    *, range_start=None, range_end=None, summary_id=None, status='active',
):
    sid = (summary_id or f'sum:{uuid.uuid4()}')[:120]
    payload = {
        'summary_id': sid,
        'user_id': user_id,
        'character_id': character_id,
        'text': text or '',
        'source_event_ids': _json_ids(source_event_ids),
        'range_start': range_start,
        'range_end': range_end,
        'status': status if status in SUMMARY_STATUSES else 'active',
        'token_cost': estimate_tokens(text or ''),
        'created_at': datetime.now(timezone.utc),
        'updated_at': datetime.now(timezone.utc),
    }
    if _USE_MEMORY_STORE:
        with _memory_lock:
            _SUMMARIES[sid] = payload
        return payload
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO rolling_summaries
               (summary_id, user_id, character_id, text, source_event_ids,
                range_start, range_end, status, token_cost)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (summary_id) DO UPDATE SET
                 text = EXCLUDED.text,
                 source_event_ids = EXCLUDED.source_event_ids,
                 range_start = EXCLUDED.range_start,
                 range_end = EXCLUDED.range_end,
                 status = EXCLUDED.status,
                 token_cost = EXCLUDED.token_cost,
                 updated_at = CURRENT_TIMESTAMP''',
            (sid, user_id, character_id, payload['text'],
             _dump_ids(payload['source_event_ids']),
             range_start, range_end, payload['status'], payload['token_cost']))
        conn.commit()
        return payload
    finally:
        cur.close()
        conn.close()


def list_rolling_summaries(user_id, character_id, *, status='active', limit=8):
    if _USE_MEMORY_STORE:
        with _memory_lock:
            rows = [
                dict(item) for item in _SUMMARIES.values()
                if item['user_id'] == user_id
                and item['character_id'] == character_id
                and (status is None or item['status'] == status)
            ]
        rows.sort(key=lambda item: item.get('range_end') or item.get('updated_at') or datetime.min, reverse=True)
        return rows[: max(1, int(limit or 8))]
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT summary_id, user_id, character_id, text, source_event_ids,
                      range_start, range_end, status, token_cost, created_at, updated_at
               FROM rolling_summaries
               WHERE user_id=%s AND character_id=%s
                 AND (%s IS NULL OR status=%s)
               ORDER BY range_end DESC NULLS LAST, updated_at DESC
               LIMIT %s''',
            (user_id, character_id, status, status, max(1, int(limit or 8))))
        return [_row_summary(row) for row in cur.fetchall()]
    finally:
        cur.close()
        conn.close()


def invalidate_summary(summary_id, *, status='invalidated'):
    if _USE_MEMORY_STORE:
        with _memory_lock:
            item = _SUMMARIES.get(summary_id)
            if item:
                item['status'] = status
                item['updated_at'] = datetime.now(timezone.utc)
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE rolling_summaries
               SET status=%s, updated_at=CURRENT_TIMESTAMP
               WHERE summary_id=%s''',
            (status, summary_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def upsert_pin(
    user_id, character_id, text, *,
    pin_type='decision', source_event_ids=(), priority=80,
    pin_id=None, status='active', expires_at=None,
):
    pid = (pin_id or f'pin:{uuid.uuid4()}')[:120]
    payload = {
        'pin_id': pid,
        'user_id': user_id,
        'character_id': character_id,
        'text': text or '',
        'pin_type': (pin_type or 'decision')[:40],
        'source_event_ids': _json_ids(source_event_ids),
        'priority': int(priority or 50),
        'status': status if status in PIN_STATUSES else 'active',
        'expires_at': expires_at,
        'created_at': datetime.now(timezone.utc),
        'updated_at': datetime.now(timezone.utc),
    }
    if _USE_MEMORY_STORE:
        with _memory_lock:
            existing = _PINS.get(pid)
            if existing:
                payload['created_at'] = existing.get('created_at') or payload['created_at']
            _PINS[pid] = payload
        return payload
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''INSERT INTO pinned_context
               (pin_id, user_id, character_id, text, pin_type, source_event_ids,
                priority, status, expires_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (pin_id) DO UPDATE SET
                 text = EXCLUDED.text,
                 pin_type = EXCLUDED.pin_type,
                 source_event_ids = EXCLUDED.source_event_ids,
                 priority = EXCLUDED.priority,
                 status = EXCLUDED.status,
                 expires_at = EXCLUDED.expires_at,
                 updated_at = CURRENT_TIMESTAMP''',
            (pid, user_id, character_id, payload['text'], payload['pin_type'],
             _dump_ids(payload['source_event_ids']), payload['priority'],
             payload['status'], expires_at))
        conn.commit()
        return payload
    finally:
        cur.close()
        conn.close()


def set_pin_status(pin_id, status):
    if status not in PIN_STATUSES:
        raise ValueError('invalid pin status')
    if _USE_MEMORY_STORE:
        with _memory_lock:
            item = _PINS.get(pin_id)
            if item:
                item['status'] = status
                item['updated_at'] = datetime.now(timezone.utc)
        return
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''UPDATE pinned_context
               SET status=%s, updated_at=CURRENT_TIMESTAMP
               WHERE pin_id=%s''',
            (status, pin_id))
        conn.commit()
    finally:
        cur.close()
        conn.close()


def resolve_pin(pin_id):
    set_pin_status(pin_id, 'resolved')


def _row_pin(row) -> dict:
    return {
        'pin_id': row[0],
        'user_id': row[1],
        'character_id': row[2],
        'text': row[3],
        'pin_type': row[4],
        'source_event_ids': _json_ids(row[5]),
        'priority': row[6],
        'status': row[7],
        'expires_at': row[8],
        'created_at': row[9],
        'updated_at': row[10],
    }


def list_pins(user_id, character_id, *, status='active', limit=20, now=None):
    now = _now_utc(now)
    if _USE_MEMORY_STORE:
        with _memory_lock:
            rows = [
                dict(item) for item in _PINS.values()
                if item['user_id'] == user_id
                and item['character_id'] == character_id
            ]
        out = []
        for item in rows:
            if status and item['status'] != status:
                continue
            expires = item.get('expires_at')
            if item['status'] == 'active' and expires and _now_utc(expires) <= now:
                item = dict(item)
                item['status'] = 'expired'
                continue
            out.append(item)
        out.sort(key=lambda item: (-int(item.get('priority') or 0), item.get('updated_at') or datetime.min), reverse=False)
        out.sort(key=lambda item: -int(item.get('priority') or 0))
        return out[: max(1, int(limit or 20))]
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            '''SELECT pin_id, user_id, character_id, text, pin_type, source_event_ids,
                      priority, status, expires_at, created_at, updated_at
               FROM pinned_context
               WHERE user_id=%s AND character_id=%s
                 AND (%s IS NULL OR status=%s)
               ORDER BY priority DESC, updated_at DESC
               LIMIT %s''',
            (user_id, character_id, status, status, max(1, int(limit or 20))))
        rows = [_row_pin(row) for row in cur.fetchall()]
        alive = []
        for item in rows:
            expires = item.get('expires_at')
            if item['status'] == 'active' and expires and _now_utc(expires) <= now:
                continue
            alive.append(item)
        return alive
    finally:
        cur.close()
        conn.close()


def _alive_source_ids(source_event_ids, deleted: set) -> Tuple[str, ...]:
    return tuple(eid for eid in _json_ids(source_event_ids) if eid not in deleted)


def _verified_derived(rows, deleted: set, *, require_source=True):
    out = []
    for row in rows:
        ids = _json_ids(row.get('source_event_ids'))
        if require_source and not ids:
            continue
        alive = _alive_source_ids(ids, deleted)
        if ids and not alive:
            continue
        item = dict(row)
        item['source_event_ids'] = alive
        out.append(item)
    return out


def maybe_store_spill_summary(user_id, character_id, spill, *, config: BudgetConfig):
    if len(spill or []) < config.min_summary_events:
        return None
    draft = draft_rolling_summary(spill)
    if not draft['text'] or not draft['source_event_ids']:
        return None
    existing = list_rolling_summaries(user_id, character_id, status='active', limit=4)
    for row in existing:
        old = set(row.get('source_event_ids') or ())
        new = set(draft['source_event_ids'])
        if old and (old <= new or new <= old or old & new):
            return save_rolling_summary(
                user_id, character_id, draft['text'], draft['source_event_ids'],
                range_start=draft['range_start'], range_end=draft['range_end'],
                summary_id=row['summary_id'],
            )
    return save_rolling_summary(
        user_id, character_id, draft['text'], draft['source_event_ids'],
        range_start=draft['range_start'], range_end=draft['range_end'],
    )


def exclude_current_turn_events(events: Sequence[dict], current_event_id=None) -> List[dict]:
    """Hot context stops before this request's user Raw Event."""
    rows = [dict(event) for event in (events or []) if event]
    cid = str(current_event_id or '').strip()
    if not cid:
        return rows
    return [event for event in rows if str(event.get('event_id') or '').strip() != cid]


def append_current_user_turn(messages: Sequence[dict], content: str) -> List[dict]:
    """Append the current user turn exactly once."""
    out = [dict(item) for item in (messages or [])]
    text = content if content is not None else ''
    if not str(text):
        return out
    if out and out[-1].get('role') == 'user' and out[-1].get('content') == text:
        return out
    out.append({'role': 'user', 'content': text})
    return out


def exclude_recall_covered_by_recent(recall_result, recent_event_ids):
    """Drop recalled items whose sources are fully inside the hot window.

    Ranking is unchanged; this is a post-filter. Items without provenance stay.
    """
    if not recall_result or not recent_event_ids:
        return recall_result
    recent = {str(x).strip() for x in recent_event_ids if str(x).strip()}
    if not recent:
        return recall_result

    def _item_ids(item):
        if not isinstance(item, dict):
            return ()
        if item.get('source_event_ids'):
            return _json_ids(item.get('source_event_ids'))
        refs = item.get('source_event_refs') or []
        ids = []
        for ref in refs:
            if isinstance(ref, dict):
                ids.append(str(ref.get('event_id') or ref.get('source_id') or '').strip())
            else:
                ids.append(str(ref).strip())
        return tuple(x for x in ids if x)

    def _keep(item):
        ids = _item_ids(item)
        if not ids:
            return True
        return not set(ids).issubset(recent)

    out = dict(recall_result)
    for key in ('facts', 'loose_bonds', 'tolds', 'lifecycle_memories',
                'sticky_notes', 'diary_memories'):
        rows = list(out.get(key) or [])
        kept = []
        for item in rows:
            if not _keep(item):
                continue
            if key == 'facts' and isinstance(item, dict) and item.get('bonds'):
                item = dict(item)
                item['bonds'] = [bond for bond in item['bonds'] if _keep(bond)]
            kept.append(item)
        out[key] = kept
    out['exclude_event_ids'] = list(recent)
    return out


def _items_from_recall(recall_result) -> List[ContextItem]:
    items = []
    if not recall_result:
        return items

    def _add(kind, row, priority):
        text = (row.get('content') if isinstance(row, dict) else '') or ''
        if not text.strip():
            return
        ids = _json_ids(row.get('source_event_ids'))
        if not ids and row.get('source_event_refs'):
            ids = _json_ids([
                (ref.get('event_id') if isinstance(ref, dict) else ref)
                for ref in row.get('source_event_refs') or []
            ])
        items.append(ContextItem(
            item_id=f'{kind}:{row.get("id") or row.get("diary_key") or len(items)}',
            item_type='recalled_memory' if kind != 'diary' else 'diary',
            text=text,
            source_event_ids=ids,
            priority=int(priority),
            created_at=row.get('timestamp') or row.get('updated_at'),
            metadata={'recall_kind': kind, 'raw': row},
        ))

    for fact in recall_result.get('facts') or []:
        score = float(fact.get('score') or 0)
        _add('fact', fact, 60 + int(score * 10))
        for bond in fact.get('bonds') or []:
            _add('fact_bond', bond, 40)
    for row in recall_result.get('loose_bonds') or []:
        _add('bond', row, 45)
    for row in recall_result.get('tolds') or []:
        _add('told', row, 45)
    for row in recall_result.get('lifecycle_memories') or []:
        _add('lifecycle', row, 35)
    for row in recall_result.get('sticky_notes') or []:
        _add('sticky', row, 30)
    for row in recall_result.get('diary_memories') or []:
        _add('diary', row, 20)
    return items


def _recall_result_from_items(items: Sequence[ContextItem], original):
    if original is None:
        return None
    kept_ids = set()
    for item in items:
        raw = (item.metadata or {}).get('raw')
        if isinstance(raw, dict):
            kept_ids.add(id(raw))
    out = dict(original)
    for key in ('facts', 'loose_bonds', 'tolds', 'lifecycle_memories',
                'sticky_notes', 'diary_memories'):
        rows = []
        for row in (original.get(key) or []):
            if id(row) in kept_ids:
                if key == 'facts':
                    row = dict(row)
                    row['bonds'] = [
                        bond for bond in (row.get('bonds') or [])
                        if id(bond) in kept_ids
                    ]
                rows.append(row)
        out[key] = rows
    return out


def _hot_items(events: Sequence[dict]) -> List[ContextItem]:
    items = []
    for index, event in enumerate(events):
        text = event.get('prompt_text') or event.get('content') or ''
        if not str(text).strip():
            continue
        eid = str(event.get('event_id') or '') or f'hot:{index}'
        items.append(ContextItem(
            item_id=f'hot:{eid}',
            item_type='hot_raw',
            text=text,
            source_event_ids=(eid,) if event.get('event_id') else (),
            priority=100,
            created_at=event.get('timestamp'),
            metadata={'event': event},
            role=event.get('role') or 'user',
        ))
    return items


def _summary_items(rows: Sequence[dict]) -> List[ContextItem]:
    items = []
    for row in rows:
        text = row.get('text') or ''
        if not text.strip():
            continue
        items.append(ContextItem(
            item_id=f"sum:{row.get('summary_id')}",
            item_type='rolling_summary',
            text=text,
            source_event_ids=_json_ids(row.get('source_event_ids')),
            priority=70,
            created_at=row.get('range_end') or row.get('updated_at'),
            metadata={'summary': row},
        ))
    return items


def _pin_items(rows: Sequence[dict]) -> List[ContextItem]:
    items = []
    for row in rows:
        if (row.get('status') or 'active') != 'active':
            continue
        text = row.get('text') or ''
        if not text.strip():
            continue
        items.append(ContextItem(
            item_id=f"pin:{row.get('pin_id')}",
            item_type='pinned',
            text=text,
            source_event_ids=_json_ids(row.get('source_event_ids')),
            priority=int(row.get('priority') or 80),
            created_at=row.get('created_at'),
            metadata={'pin': row, 'pin_type': row.get('pin_type')},
        ))
    return items


@dataclass
class ChatContextPack:
    messages: List[dict] = field(default_factory=list)
    recent_event_ids: List[str] = field(default_factory=list)
    spill_event_ids: List[str] = field(default_factory=list)
    items: List[ContextItem] = field(default_factory=list)
    pinned_prompt_text: str = ''
    summary_prompt_text: str = ''
    memory_text: str = ''
    bond_text: str = ''
    told_text: str = ''
    recall_result: Optional[dict] = None
    recall_ready: bool = False
    support_ready: bool = False
    relationship_prompt_text: str = ''
    cognitive_prompt_text: str = ''
    diary_hint_text: str = ''
    temporal_text: str = ''
    schedule_text: str = ''
    lore_text: str = ''
    anti_repeat_text: str = ''
    period_text: str = ''
    accounts_text: str = ''
    expression_rules: str = ''
    meet_line: str = ''
    allocation: dict = field(default_factory=dict)
    failed_closed: bool = False

    def recent_ids(self) -> List[str]:
        return list(self.recent_event_ids)


def _format_pinned_block(items: Sequence[ContextItem]) -> str:
    if not items:
        return ''
    lines = ['【当前重要事项——工作集，不是永久记忆】']
    for item in items:
        kind = (item.metadata or {}).get('pin_type') or 'item'
        lines.append(f'- [{kind}] {item.text}')
    lines.append('这些是当前仍需记住的事项；完成后不要继续当作未决。')
    return '\n' + '\n'.join(lines) + '\n'


def _format_summary_block(items: Sequence[ContextItem]) -> str:
    if not items:
        return ''
    lines = ['【稍早连续讨论的滚动摘要——不能替代原文 Raw Event】']
    for item in items:
        lines.append(f'- {item.text}')
    return '\n' + '\n'.join(lines) + '\n'


def _format_plain_block(title, items: Sequence[ContextItem]) -> str:
    rows = [item for item in items if (item.text or '').strip()]
    if not rows:
        return ''
    if title:
        return '\n' + title + '\n' + '\n'.join(item.text for item in rows) + '\n'
    return '\n' + '\n'.join(item.text for item in rows) + '\n'


def _support_items(user_id, character_id, user_message, hot_messages, temporal_snapshot=None):
    """Wrap dynamic prompt extras as ContextItems. Never calls an LLM."""
    items = []

    def _add(item_type, text, priority, item_id, meta=None, subjective=False):
        body = (text or '').strip()
        if not body:
            return
        meta = dict(meta or {})
        if subjective:
            meta['subjective'] = True
        items.append(ContextItem(
            item_id=item_id,
            item_type=item_type,
            text=body,
            priority=priority,
            metadata=meta,
        ))

    try:
        from shared_relation_prompt import _EXPRESSION_ONLY_RULES, _build_meet_line
        from user_memory import get_first_interaction_days
        first_days = get_first_interaction_days(user_id, character_id)
        meet_line = _build_meet_line(first_days, 0, 0)
        _add('relationship_state', meet_line, 82, 'rel:meet', {'kind': 'meet_line'})
    except Exception as exc:
        print(f'[context_layer] meet_line skipped:{exc}')
        meet_line = ''
        _EXPRESSION_ONLY_RULES = ''

    try:
        from relationship_reader import build_state_summary
        rel_text = build_state_summary(user_id, character_id)
        _add('relationship_state', rel_text, 80, 'rel:state')
    except Exception as exc:
        print(f'[context_layer] relationship state skipped:{exc}')

    try:
        from cognitive_reader import iter_active_cognitive_items
        for index, row in enumerate(iter_active_cognitive_items(user_id, character_id)):
            _add(
                'cognitive_state', row.get('text') or '',
                40, f"cog:{row.get('kind')}:{index}",
                {'kind': row.get('kind'), 'source_event_ids': row.get('source_event_ids')},
                subjective=True,
            )
    except Exception as exc:
        print(f'[context_layer] cognitive items skipped:{exc}')

    try:
        import diary_engine
        hint = diary_engine.build_diary_hint(character_id, user_id)
        _add('diary', hint, 22, 'diary:hint', {'kind': 'hint'}, subjective=True)
    except Exception as exc:
        print(f'[context_layer] diary hint skipped:{exc}')

    try:
        from temporal_awareness import build_prompt_context
        temporal_text = build_prompt_context(
            user_id, character_id, snapshot=temporal_snapshot)
        _add('temporal', temporal_text, 35, 'temporal:now')
    except Exception as exc:
        print(f'[context_layer] temporal skipped:{exc}')

    try:
        import db_schedule as _dbs
        from datetime import datetime as _dt
        try:
            from config import CN_TZ
            now = _dt.now(CN_TZ)
        except Exception:
            now = _dt.utcnow()
        act = _dbs.get_current_activity(character_id, user_id, now)
        if act:
            where = f'（在{act["location"]}）' if act.get('location') else ''
            note = f'\n你当时的想法：{act["note"]}' if act.get('note') else ''
            try:
                from activity_phone import busy_prompt_hint
                busy = busy_prompt_hint(act)
            except Exception:
                busy = '' if act.get('can_reply') else (
                    '\n★ 这段时间你在忙，但偶尔能瞄一眼手机。语气可以简短一些。'
                )
            schedule_text = (
                f'【你此刻正在做的事——这是你自己安排的，不是设定，是真的在做】\n'
                f'{act["start_time"]}~{act["end_time"]} {act["title"]}{where}{note}{busy}\n'
                '用法：她问"在干嘛"就照实说这件事，别另编一个。'
            )
            _add('schedule', schedule_text, 33, 'schedule:now')
    except Exception as exc:
        print(f'[context_layer] schedule skipped:{exc}')

    try:
        from characters import retrieve_character_memory
        recalls = retrieve_character_memory(character_id, user_message, limit=4)
        if recalls:
            lore = (
                '【你此刻自然想起的、关于你自己的一些事】\n'
                + '\n'.join(f'- {row}' for row in recalls)
            )
            _add('character_lore', lore, 28, 'lore:hits')
    except Exception as exc:
        print(f'[context_layer] lore skipped:{exc}')

    try:
        from user_memory import get_recent_openings
        openings = get_recent_openings(user_id, n=5, character_id=character_id)
        hot_assistant = [
            (m.get('content') or '')[:5]
            for m in (hot_messages or [])
            if m.get('role') == 'assistant'
        ]
        compact = []
        for opening in openings:
            if opening and opening not in hot_assistant and opening not in compact:
                compact.append(opening)
        if compact:
            anti = (
                '【别每句都一个开头】\n'
                f'最近已用开场：{", ".join(compact)}\n'
                '这次换个说法起头。这只是防复读，不是少说话。'
            )
            _add('anti_repeat', anti, 18, 'anti:openings')
        # last_assistant_reply: only if hot window has no assistant turn
        if not any(m.get('role') == 'assistant' for m in (hot_messages or [])):
            from user_memory import get_last_assistant_reply
            last = get_last_assistant_reply(user_id, character_id)
            if last:
                _add(
                    'anti_repeat',
                    f'【别复读上一条】上一条你说的是：「{last[:200]}」',
                    16, 'anti:last',
                )
    except Exception as exc:
        print(f'[context_layer] anti-repeat skipped:{exc}')

    try:
        from prompt import get_period_context, _accounts_block
        _add('period', get_period_context(user_id), 12, 'period:now')
        _add('accounts', _accounts_block(user_id), 10, 'accounts:now')
    except Exception as exc:
        print(f'[context_layer] period/accounts skipped:{exc}')

    return items, locals().get('_EXPRESSION_ONLY_RULES', '')


def assemble_from_events(
    events: Sequence[dict],
    *,
    user_id,
    character_id,
    user_message='',
    now=None,
    config: Optional[BudgetConfig] = None,
    include_recall=True,
    include_support=False,
    deleted_ids=None,
    current_event_id=None,
    temporal_snapshot=None,
) -> ChatContextPack:
    cfg = config or BudgetConfig()
    manager = ContextBudgetManager(cfg)
    now = _now_utc(now)
    events = exclude_current_turn_events(events, current_event_id)
    hot, spill = select_hot_window(events, config=cfg, now=now)
    maybe_store_spill_summary(user_id, character_id, spill, config=cfg)

    deleted = set(deleted_ids or ())
    summaries = _verified_derived(
        list_rolling_summaries(user_id, character_id, status='active', limit=6),
        deleted,
    )
    pins = _verified_derived(
        list_pins(user_id, character_id, status='active', limit=12, now=now),
        deleted,
    )

    items = _hot_items(hot) + _summary_items(summaries) + _pin_items(pins)
    recent_ids = [str(ev.get('event_id') or '') for ev in hot if ev.get('event_id')]
    recall_result = None
    if include_recall:
        try:
            from smart_recall import two_level_recall
            from recall_candidates import (
                collapse_candidates, from_recall_result, to_recall_result,
            )
            recall_result = two_level_recall(
                user_id, character_id, user_message or '',
                exclude_event_ids=recent_ids,
            )
            recall_result = exclude_recall_covered_by_recent(recall_result, recent_ids)
            if recall_result is not None:
                collapsed = collapse_candidates(from_recall_result(recall_result))
                recall_result = to_recall_result(collapsed, recall_result)
            items.extend(_items_from_recall(recall_result))
        except Exception as exc:
            print(f'[context_layer] recall wrap skipped:{exc}')
            recall_result = None

    expression_rules = ''
    if include_support:
        try:
            extra, expression_rules = _support_items(
                user_id, character_id, user_message,
                [
                    {'role': ev.get('role'), 'content': ev.get('prompt_text') or ev.get('content')}
                    for ev in hot
                ],
                temporal_snapshot=temporal_snapshot,
            )
            items.extend(extra)
        except Exception as exc:
            print(f'[context_layer] support wrap skipped:{exc}')

    allocated = manager.split(items)
    hot_kept = allocated.get('hot') or []
    pin_kept = allocated.get('pinned') or []
    sum_kept = allocated.get('summary') or []
    recall_kept = list(allocated.get('recall') or []) + list(allocated.get('diary') or [])
    rel_kept = allocated.get('relationship') or []
    cog_kept = allocated.get('cognitive') or []
    aux_kept = allocated.get('aux') or []

    def _take(kind):
        return [item for item in aux_kept if item.item_type == kind]

    messages = []
    for item in hot_kept:
        content = item.text
        if content:
            messages.append({
                'role': item.role or 'user',
                'content': content,
            })

    pack = ChatContextPack(
        messages=messages,
        recent_event_ids=[
            item.source_event_ids[0]
            for item in hot_kept
            if item.source_event_ids
        ],
        spill_event_ids=[
            str(ev.get('event_id') or '') for ev in spill if ev.get('event_id')
        ],
        items=hot_kept + pin_kept + sum_kept + recall_kept + rel_kept + cog_kept + aux_kept,
        pinned_prompt_text=_format_pinned_block(pin_kept),
        summary_prompt_text=_format_summary_block(sum_kept),
        relationship_prompt_text=_format_plain_block('', rel_kept),
        cognitive_prompt_text=_format_plain_block(
            '【当前未决认知——主观，不是事实】', cog_kept),
        diary_hint_text=_format_plain_block('', [
            item for item in recall_kept if (item.metadata or {}).get('kind') == 'hint'
        ] + _take('diary')),
        temporal_text=_format_plain_block('', _take('temporal')),
        schedule_text=_format_plain_block('', _take('schedule')),
        lore_text=_format_plain_block('', _take('character_lore')),
        anti_repeat_text=_format_plain_block('', _take('anti_repeat')),
        period_text=_format_plain_block('', _take('period')),
        accounts_text=_format_plain_block('', _take('accounts')),
        expression_rules=expression_rules or '',
        support_ready=bool(include_support),
        allocation={
            name: sum(item.token_cost for item in rows)
            for name, rows in allocated.items()
        },
    )
    # diary_hint is a diary ContextItem; if it landed in diary channel not recall
    if not pack.diary_hint_text:
        pack.diary_hint_text = _format_plain_block('', [
            item for item in (allocated.get('diary') or [])
            if (item.metadata or {}).get('kind') == 'hint'
        ])
    if recall_result is not None:
        trimmed = _recall_result_from_items(recall_kept, recall_result)
        try:
            from smart_recall import format_recall_for_prompt
            memory_text, bond_text, told_text = format_recall_for_prompt(trimmed)
        except Exception:
            memory_text, bond_text, told_text = '', '', ''
        pack.memory_text = memory_text
        pack.bond_text = bond_text
        pack.told_text = told_text
        pack.recall_result = trimmed
        pack.recall_ready = True
    return pack


def build_chat_context(
    user_id,
    character_id,
    *,
    user_message='',
    profile='default',
    include_recall=True,
    now=None,
    current_event_id=None,
    temporal_snapshot=None,
) -> ChatContextPack:
    """Fast path: bounded ledger fetch + deterministic window. No LLM."""
    cfg = BudgetConfig.for_profile(profile)
    try:
        import raw_events
        deleted = raw_events.deleted_event_ids(user_id, character_id)
        events = raw_events.get_hot_candidate_events(
            user_id, character_id,
            n=cfg.hot_fetch_max,
            hours=cfg.hot_fetch_hours,
        )
        events = exclude_current_turn_events(events, current_event_id)
        for event in events:
            try:
                from user_memory import assemble_prompt_content
                event['prompt_text'] = assemble_prompt_content(
                    event.get('content') or '', event.get('metadata'))
            except Exception:
                event['prompt_text'] = event.get('content') or ''
        return assemble_from_events(
            events,
            user_id=user_id,
            character_id=character_id,
            user_message=user_message,
            now=now,
            config=cfg,
            include_recall=include_recall,
            include_support=True,
            deleted_ids=deleted,
            current_event_id=current_event_id,
            temporal_snapshot=temporal_snapshot,
        )
    except Exception as exc:
        try:
            from raw_events import SourceValidityError
            if isinstance(exc, SourceValidityError):
                print(f'[context_layer] source validity unknown, empty context:{exc}')
                return ChatContextPack(failed_closed=True)
        except Exception:
            pass
        print(f'[context_layer] build_chat_context fallback empty:{exc}')
        return ChatContextPack()
