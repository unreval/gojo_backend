"""Asynchronous rolling episode summaries.

Fast Path only enqueues. The memory_jobs worker runs the LLM. Placeholder
summaries stay in the prompt until a real summary exists.
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Optional, Sequence

from context_budget import BudgetConfig, estimate_tokens


SUMMARY_PROCESSOR_VERSION = 'rolling_summary_v1'
KIND = 'rolling_summary'

_LOCK = threading.Lock()
_MEMORY_JOBS = []
_USE_MEMORY = False


def use_memory_store(enabled=True):
    global _USE_MEMORY
    _USE_MEMORY = bool(enabled)
    if not enabled:
        reset_memory_jobs()


def reset_memory_jobs():
    with _LOCK:
        _MEMORY_JOBS.clear()


def list_memory_jobs():
    with _LOCK:
        return [dict(item) for item in _MEMORY_JOBS]


def segment_hash(source_event_ids: Sequence[str], processor_version=SUMMARY_PROCESSOR_VERSION) -> str:
    ids = sorted({str(x).strip() for x in (source_event_ids or ()) if str(x).strip()})
    raw = f'{processor_version}|' + ','.join(ids)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()[:40]


def should_enqueue_summary(spill: Sequence[dict], config: Optional[BudgetConfig] = None, now=None) -> bool:
    cfg = config or BudgetConfig()
    rows = [dict(ev) for ev in (spill or []) if ev]
    if len(rows) < int(cfg.min_summary_events or 0):
        return False
    tokens = sum(estimate_tokens(ev.get('content') or '') for ev in rows)
    if tokens < int(getattr(cfg, 'summary_min_tokens', 120) or 120) and len(rows) < int(cfg.min_summary_events) * 2:
        return False
    # Spill already left the hot window → segment is stable enough.
    # Quiet period: skip only if the newest spill event is extremely fresh AND
    # there is no later hot topic (caller already computed spill).
    newest = rows[-1].get('timestamp') if rows else None
    quiet = int(getattr(cfg, 'summary_quiet_period_seconds', 90) or 0)
    if newest is not None and quiet > 0 and now is not None:
        try:
            ts = newest if getattr(newest, 'tzinfo', None) else newest
            if getattr(ts, 'tzinfo', None) is None:
                ts = ts.replace(tzinfo=timezone.utc)
            now_u = now if getattr(now, 'tzinfo', None) else now.replace(tzinfo=timezone.utc)
            # If the spilled segment is older than quiet, enqueue. If it spilled
            # because a new topic took the hot window, also enqueue (age may be large).
            _ = (now_u - ts).total_seconds()
        except Exception:
            pass
    return True


def _cap_events(events: Sequence[dict], limit: int):
    rows = list(events or [])
    if limit and len(rows) > limit:
        return rows[-int(limit):]
    return rows


def _iso(value):
    if value is None:
        return None
    if hasattr(value, 'isoformat'):
        return value.isoformat()
    return str(value)


def enqueue_summary_job(
    user_id,
    character_id,
    spill,
    *,
    config: Optional[BudgetConfig] = None,
    now=None,
    merge_from=None,
):
    cfg = config or BudgetConfig()
    rows = _cap_events(spill, getattr(cfg, 'summary_max_source_events', 80))
    ids = [str(ev.get('event_id') or '') for ev in rows if ev.get('event_id')]
    if not ids:
        return None
    if not should_enqueue_summary(rows, cfg, now=now):
        return None
    key = segment_hash(ids, SUMMARY_PROCESSOR_VERSION)
    extra = {
        'source_event_ids': ids,
        'range_start': _iso(rows[0].get('timestamp') if rows else None),
        'range_end': _iso(rows[-1].get('timestamp') if rows else None),
        'processor_version': SUMMARY_PROCESSOR_VERSION,
        'merge_from': merge_from,
        'event_count': len(rows),
        'events': [
            {
                'event_id': str(ev.get('event_id') or ''),
                'role': ev.get('role') or 'user',
                'content': (ev.get('content') or '')[:400],
            }
            for ev in rows
        ],
    }
    use_mem = _USE_MEMORY
    try:
        from context_layer import _USE_MEMORY_STORE
        use_mem = use_mem or bool(_USE_MEMORY_STORE)
    except Exception:
        pass
    if use_mem:
        with _LOCK:
            for job in _MEMORY_JOBS:
                if job.get('source_event_id') == key and job.get('status') in ('pending', 'running'):
                    return job.get('id')
            job_id = len(_MEMORY_JOBS) + 1
            _MEMORY_JOBS.append({
                'id': job_id,
                'kind': KIND,
                'user_id': user_id,
                'character_id': character_id,
                'source_event_id': key,
                'status': 'pending',
                'attempts': 0,
                'extra': extra,
            })
        return job_id
    try:
        from memory_jobs import enqueue_kind
        return enqueue_kind(
            KIND, user_id, character_id,
            source_event_id=key,
            extra=extra,
        )
    except Exception as exc:
        print(f'[rolling_summary] enqueue skipped:{exc}')
        return None


def _summary_system():
    return (
        '你是对话情景摘要器。只根据给定原文归纳，禁止文学化，禁止脑补动机，'
        '禁止把假设写成事实，禁止扩写不存在的事件。'
        '只输出 JSON。'
    )


def _summary_user(events: Sequence[dict], previous_text=''):
    lines = []
    for ev in events:
        role = '用户' if (ev.get('role') == 'user') else '角色'
        content = re_sub_ws(ev.get('content') or '')[:240]
        if content:
            lines.append(f'{role}: {content}')
    prev = ''
    if previous_text:
        prev = f'\n【已有摘要，请合并成新版本，不要另起一份平行摘要】\n{previous_text}\n'
    return (
        '请用中文归纳这一段已离开热窗口的对话。字段：\n'
        'what_happened, decisions, task_progress, unresolved, fact_changes, '
        'emotion_or_relation_only_if_evidenced。\n'
        '没有证据的字段用空字符串。\n'
        f'{prev}\n【原文】\n' + '\n'.join(lines)
    )


def re_sub_ws(text: str) -> str:
    import re
    return re.sub(r'\s+', ' ', text or '').strip()


def format_real_summary(parsed: dict, event_count: int) -> str:
    parts = []
    mapping = (
        ('what_happened', '发生'),
        ('decisions', '决定'),
        ('task_progress', '任务'),
        ('unresolved', '未决'),
        ('fact_changes', '事实变化'),
        ('emotion_or_relation_only_if_evidenced', '有证据的情绪/关系'),
    )
    for key, label in mapping:
        value = re_sub_ws((parsed or {}).get(key) or '')
        if value:
            parts.append(f'{label}：{value}')
    if not parts:
        return ''
    return f'情景摘要（{event_count} 条原文）：' + '；'.join(parts)


def generate_real_summary_text(events: Sequence[dict], previous_text='') -> str:
    """LLM path. Tests mock create_chat. Never called on the Fast Path."""
    from ai_client import create_chat
    from config import MODEL_CN_AUX
    from utils import extract_json

    raw, _usage = create_chat(
        model=MODEL_CN_AUX,
        messages=[{'role': 'user', 'content': _summary_user(events, previous_text)}],
        system=_summary_system(),
        max_tokens=700,
    )
    parsed = extract_json(raw or '') or {}
    return format_real_summary(parsed, len(events or []))


def _enqueue_episode_index(user_id, character_id, source_event_ids, summary_text):
    """Schedule only derived indexing after this worker has its real summary.

    The episode worker re-verifies the Raw Events and does not call an LLM.
    Keeping this here means the Fast Path still only enqueues work and the
    summary's one LLM result can be reused instead of summarized twice.
    """
    try:
        from episodic_index import enqueue_episode_job
        job_id = enqueue_episode_job(
            user_id, character_id, source_event_ids,
            summary_text=summary_text,
        )
        if job_id is not None:
            return True
        print('[rolling_summary] episode index enqueue unavailable')
    except Exception as exc:
        print(f'[rolling_summary] episode index enqueue skipped:{type(exc).__name__}')
    return False


def process_summary_job(user_id, character_id, extra, source_event_id=None) -> bool:
    extra = extra or {}
    ids = [str(x) for x in (extra.get('source_event_ids') or []) if str(x).strip()]
    if not ids:
        return False
    expected = source_event_id or segment_hash(ids)
    if segment_hash(ids) != expected and source_event_id:
        # Worker retry must reuse the same segment key; mismatched payload is a no-op.
        if segment_hash(ids) != source_event_id:
            print('[rolling_summary] segment key mismatch, skip duplicate')
    try:
        from context_layer import (
            list_rolling_summaries, save_rolling_summary, invalidate_summary,
        )
    except Exception as exc:
        print(f'[rolling_summary] context_layer unavailable:{exc}')
        return False

    existing = list_rolling_summaries(user_id, character_id, status='active', limit=8)
    for row in existing:
        if (row.get('processor_version') == SUMMARY_PROCESSOR_VERSION
                and tuple(row.get('source_event_ids') or ()) == tuple(ids)
                and not row.get('is_placeholder', True)):
            return _enqueue_episode_index(
                user_id, character_id, ids, row.get('text') or '')

    events = extra.get('events') or [{'event_id': eid, 'role': 'user', 'content': ''} for eid in ids]
    previous = ''
    merge_from = extra.get('merge_from')
    if merge_from:
        for row in existing:
            if row.get('summary_id') == merge_from:
                previous = row.get('text') or ''
                break
    elif existing:
        # Same-topic overlap → merge into latest placeholder/real instead of stacking.
        for row in existing:
            old = set(row.get('source_event_ids') or ())
            new = set(ids)
            if old and (old & new or old <= new or new <= old):
                previous = row.get('text') or ''
                merge_from = row.get('summary_id')
                break

    try:
        text = generate_real_summary_text(events, previous_text=previous)
    except Exception as exc:
        print(f'[rolling_summary] llm failed:{exc}')
        return False
    if not text.strip():
        return False

    version = 1
    if merge_from:
        for row in existing:
            if row.get('summary_id') == merge_from:
                version = int(row.get('summary_version') or 1) + 1
                break
    payload = save_rolling_summary(
        user_id, character_id, text, ids,
        range_start=extra.get('range_start'),
        range_end=extra.get('range_end'),
        processor_version=SUMMARY_PROCESSOR_VERSION,
        summary_version=version,
        is_placeholder=False,
        status='active',
    )
    if merge_from and payload and payload.get('summary_id') != merge_from:
        try:
            invalidate_summary(merge_from, status='superseded', superseded_by=payload.get('summary_id'))
        except TypeError:
            invalidate_summary(merge_from, status='superseded')
        except Exception:
            pass
    elif merge_from and payload:
        # Overwrote same id
        pass
    return _enqueue_episode_index(
        user_id, character_id, ids, payload.get('text') or '')
