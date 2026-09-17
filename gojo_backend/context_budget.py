"""Unified context budget for Recall/Search v2 phase 1.

Hot recent context, rolling summaries, pinned items, and recall candidates
are trimmed here. This is not a fact source: items wrap existing Raw Events
or derived objects. Token estimates are local and deterministic — no LLM,
no tokenizer service, no network.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


# ── defaults (initial knobs, not product-final) ──────────────
TOTAL_CONTEXT_TOKEN_BUDGET = 8000

HOT_CONTEXT_TOKEN_BUDGET = 4000
HOT_CONTEXT_MIN_EVENTS = 8
HOT_CONTEXT_MAX_EVENTS = 180
HOT_CONTEXT_TIME_HORIZON_MINUTES = 180
HOT_CONTEXT_FETCH_MAX = 240
HOT_CONTEXT_FETCH_HOURS = 72
HOT_CONTEXT_CONTINUE_GAP_MINUTES = 25
HOT_CONTEXT_SHIFT_GAP_MINUTES = 90

ROLLING_SUMMARY_TOKEN_BUDGET = 1200
PINNED_CONTEXT_TOKEN_BUDGET = 900
RECALLED_MEMORY_TOKEN_BUDGET = 1800
AUX_TOKEN_BUDGET = 500

MIN_SUMMARY_EVENTS = 8

CHANNEL_SPECS = {
    'hot': {
        'share': 0.46, 'floor': 0.32, 'ceil': 0.70, 'priority': 100,
    },
    'pinned': {
        'share': 0.10, 'floor': 0.07, 'ceil': 0.20, 'priority': 90,
    },
    'summary': {
        'share': 0.12, 'floor': 0.04, 'ceil': 0.25, 'priority': 70,
    },
    'recall': {
        'share': 0.14, 'floor': 0.04, 'ceil': 0.28, 'priority': 50,
    },
    'relationship': {
        'share': 0.06, 'floor': 0.03, 'ceil': 0.12, 'priority': 80,
    },
    'cognitive': {
        'share': 0.04, 'floor': 0.00, 'ceil': 0.10, 'priority': 40,
    },
    'diary': {
        'share': 0.03, 'floor': 0.00, 'ceil': 0.08, 'priority': 25,
    },
    'aux': {
        'share': 0.05, 'floor': 0.00, 'ceil': 0.12, 'priority': 20,
    },
}

ITEM_TYPE_CHANNEL = {
    'hot_raw': 'hot',
    'rolling_summary': 'summary',
    'pinned': 'pinned',
    'recalled_memory': 'recall',
    'relationship_state': 'relationship',
    'cognitive_state': 'cognitive',
    'diary': 'diary',
    'temporal': 'aux',
    'schedule': 'aux',
    'character_lore': 'aux',
    'anti_repeat': 'aux',
    'period': 'aux',
    'accounts': 'aux',
}

PROFILES = {
    'default': {},
    'text': {},
    'story': {},
    'image': {'total_token_budget': 7000},
    'voice': {
        'total_token_budget': 2800,
        'hot_max_events': 80,
        'hot_min_events': 4,
    },
    'proactive': {
        'total_token_budget': 2200,
        'hot_max_events': 60,
        'hot_min_events': 4,
    },
}


def estimate_tokens(text: str) -> int:
    """Stable local token estimate. CJK ≈ 1.2, other ≈ 4 chars / token."""
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        code = ord(ch)
        if (
            0x3400 <= code <= 0x9FFF
            or 0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
            or 0xF900 <= code <= 0xFAFF
        ):
            cjk += 1
        elif ch.isspace():
            continue
        else:
            other += 1
    return max(1, int(cjk * 1.2 + other / 4.0 + 1))


@dataclass
class BudgetConfig:
    total_token_budget: int = TOTAL_CONTEXT_TOKEN_BUDGET
    hot_token_budget: int = HOT_CONTEXT_TOKEN_BUDGET
    hot_min_events: int = HOT_CONTEXT_MIN_EVENTS
    hot_max_events: int = HOT_CONTEXT_MAX_EVENTS
    hot_time_horizon_minutes: int = HOT_CONTEXT_TIME_HORIZON_MINUTES
    hot_fetch_max: int = HOT_CONTEXT_FETCH_MAX
    hot_fetch_hours: int = HOT_CONTEXT_FETCH_HOURS
    continue_gap_minutes: int = HOT_CONTEXT_CONTINUE_GAP_MINUTES
    shift_gap_minutes: int = HOT_CONTEXT_SHIFT_GAP_MINUTES
    summary_token_budget: int = ROLLING_SUMMARY_TOKEN_BUDGET
    pinned_token_budget: int = PINNED_CONTEXT_TOKEN_BUDGET
    recall_token_budget: int = RECALLED_MEMORY_TOKEN_BUDGET
    aux_token_budget: int = AUX_TOKEN_BUDGET
    min_summary_events: int = MIN_SUMMARY_EVENTS

    @classmethod
    def for_profile(cls, profile: str = 'default') -> 'BudgetConfig':
        cfg = cls()
        extra = PROFILES.get(profile) or PROFILES.get(str(profile or '').strip()) or {}
        for key, value in extra.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        if extra.get('total_token_budget') and 'hot_token_budget' not in extra:
            cfg.hot_token_budget = max(
                400, int(cfg.total_token_budget * CHANNEL_SPECS['hot']['share']))
        return cfg


@dataclass
class ContextItem:
    item_id: str
    item_type: str
    text: str
    source_event_ids: Tuple[str, ...] = ()
    token_cost: int = 0
    priority: int = 50
    created_at: Optional[datetime] = None
    metadata: dict = field(default_factory=dict)
    role: Optional[str] = None

    def __post_init__(self):
        self.source_event_ids = tuple(
            str(x).strip() for x in (self.source_event_ids or ()) if str(x).strip()
        )
        if not self.token_cost:
            self.token_cost = estimate_tokens(self.text or '')

    @property
    def channel(self) -> str:
        return ITEM_TYPE_CHANNEL.get(self.item_type, 'aux')


def _channel_of(item: ContextItem) -> str:
    return item.channel


def group_by_channel(items: Sequence[ContextItem]) -> Dict[str, List[ContextItem]]:
    grouped = {name: [] for name in CHANNEL_SPECS}
    for item in items:
        grouped.setdefault(_channel_of(item), []).append(item)
    return grouped


def _keep_last_hot(items: Sequence[ContextItem]) -> Tuple[List[ContextItem], Optional[ContextItem]]:
    if not items:
        return [], None
    last = items[-1]
    return list(items[:-1]), last


def trim_items(
    items: Sequence[ContextItem],
    budget: int,
    *,
    drop_from: str = 'oldest',
    keep_last: bool = False,
) -> List[ContextItem]:
    """Drop whole items until they fit. Never slice item.text mid-message."""
    rows = [item for item in items if item and (item.text or '').strip()]
    if not rows or budget <= 0:
        if keep_last and rows:
            return [rows[-1]]
        return []

    locked = None
    working = list(rows)
    if keep_last:
        working, locked = _keep_last_hot(working)
        locked_cost = locked.token_cost if locked else 0
        budget = max(0, budget - locked_cost)

    if drop_from == 'oldest':
        ordered = list(working)
        kept_rev = []
        used = 0
        for item in reversed(ordered):
            cost = item.token_cost or estimate_tokens(item.text)
            if used + cost <= budget:
                kept_rev.append(item)
                used += cost
        kept = list(reversed(kept_rev))
    else:
        ranked = sorted(
            working,
            key=lambda item: (-int(item.priority or 0), item.created_at or datetime.min),
        )
        kept = []
        used = 0
        for item in ranked:
            cost = item.token_cost or estimate_tokens(item.text)
            if used + cost <= budget:
                kept.append(item)
                used += cost
        original_ids = {id(item): index for index, item in enumerate(working)}
        kept.sort(key=lambda item: original_ids.get(id(item), 0))

    if locked is not None:
        kept.append(locked)
    return kept


def allocate_channels(
    grouped: Dict[str, Sequence[ContextItem]],
    config: Optional[BudgetConfig] = None,
) -> Dict[str, List[ContextItem]]:
    """Floor + share + unused-budget borrow. Hot keeps the newest turn."""
    cfg = config or BudgetConfig()
    total = max(1, int(cfg.total_token_budget))
    specs = CHANNEL_SPECS
    requested = {}
    for name in specs:
        items = list(grouped.get(name) or [])
        requested[name] = sum(item.token_cost or 0 for item in items)

    granted = {name: 0 for name in specs}
    remaining = total
    for name, spec in specs.items():
        floor = int(total * spec['floor'])
        take = min(floor, requested[name], remaining)
        granted[name] = take
        remaining -= take

    want_more = {
        name: max(0, requested[name] - granted[name])
        for name in specs
    }
    by_priority = sorted(specs, key=lambda n: -specs[n]['priority'])
    for name in by_priority:
        if remaining <= 0:
            break
        spec = specs[name]
        ceil = int(total * spec['ceil'])
        target = int(total * spec['share'])
        extra = min(
            want_more[name],
            remaining,
            max(0, ceil - granted[name]),
            max(0, max(target, ceil) - granted[name]) if want_more[name] else 0,
        )
        if extra <= 0:
            continue
        granted[name] += extra
        remaining -= extra
        want_more[name] -= extra

    if remaining > 0:
        for name in by_priority:
            if remaining <= 0:
                break
            spec = specs[name]
            ceil = int(total * spec['ceil'])
            room = min(want_more[name], remaining, max(0, ceil - granted[name]))
            if room <= 0:
                continue
            granted[name] += room
            remaining -= room
            want_more[name] -= room

    out = {}
    for name, spec in specs.items():
        items = list(grouped.get(name) or [])
        budget = granted[name]
        if name == 'hot':
            out[name] = trim_items(items, budget, drop_from='oldest', keep_last=True)
        elif name == 'pinned':
            out[name] = trim_items(items, budget, drop_from='priority')
        else:
            out[name] = trim_items(items, budget, drop_from='priority')
    return out


class ContextBudgetManager:
    def __init__(self, config: Optional[BudgetConfig] = None):
        self.config = config or BudgetConfig()

    def allocate(self, items: Sequence[ContextItem]) -> List[ContextItem]:
        grouped = group_by_channel(items)
        allocated = allocate_channels(grouped, self.config)
        ordered = []
        for name in ('hot', 'pinned', 'summary', 'recall',
                     'relationship', 'cognitive', 'diary', 'aux'):
            ordered.extend(allocated.get(name) or [])
        return ordered

    def split(self, items: Sequence[ContextItem]) -> Dict[str, List[ContextItem]]:
        return allocate_channels(group_by_channel(items), self.config)
