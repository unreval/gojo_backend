"""Pure rules for the canonical schedule world.

This module deliberately has no database or model dependency.  The database
adapter owns persistence; every caller shares these rules for phase progress,
action-intent shape, and the truth guard for active events.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, tzinfo
import re
from typing import Any, Dict, Iterable, List, Optional


EVENT_PLANNED = 'planned'
EVENT_ACTIVE = 'active'
EVENT_COMPLETED = 'completed'
EVENT_CANCELLED = 'cancelled'
EVENT_STATUSES = (
    EVENT_PLANNED,
    EVENT_ACTIVE,
    EVENT_COMPLETED,
    EVENT_CANCELLED,
)
EVENT_TERMINAL = (EVENT_COMPLETED, EVENT_CANCELLED)

REPLY_FREE = 'free'
REPLY_SOFT_BUSY = 'soft_busy'
REPLY_HARD_BUSY = 'hard_busy'
REPLY_STATES = (REPLY_FREE, REPLY_SOFT_BUSY, REPLY_HARD_BUSY)

FIXED = 'fixed'
FLEXIBLE = 'flexible'
OPTIONAL = 'optional'
FLAVOR = 'flavor'
FLEXIBILITY = (FIXED, FLEXIBLE, OPTIONAL, FLAVOR)

ACTION_COMPLETE = 'complete'
ACTION_EXTEND = 'extend'
ACTION_CANCEL = 'cancel'
ACTION_RELOCATE = 'relocate'
ACTION_INSERT = 'insert'
ACTION_TYPES = (
    ACTION_COMPLETE,
    ACTION_EXTEND,
    ACTION_CANCEL,
    ACTION_RELOCATE,
    ACTION_INSERT,
)
_ACTION_ALIASES = {
    'complete_early': ACTION_COMPLETE,
    'complete': ACTION_COMPLETE,
    'extend': ACTION_EXTEND,
    'cancel': ACTION_CANCEL,
    'change_location': ACTION_RELOCATE,
    'relocate': ACTION_RELOCATE,
    'insert': ACTION_INSERT,
    'insert_event': ACTION_INSERT,
}

# Guard only: it never writes a state and it never infers an intent from text.
_COMPLETION_CLAIM = re.compile(
    r'(?:已经|已|终于|刚刚)?(?:结束了|完成了|做完了|处理完了|搞定了|收尾了)'
    r'|(?:会議|会议|ミーティング).{0,8}(?:終わった|終わりました|完了した)',
    re.IGNORECASE,
)


def normalize_reply_state(value: Any, can_reply: bool = True) -> str:
    state = str(value or '').strip().lower()
    if state in REPLY_STATES:
        return state
    return REPLY_FREE if bool(can_reply) else REPLY_HARD_BUSY


def parse_effective_busy_minutes(value: Any) -> Optional[int]:
    if value is None or value == '' or isinstance(value, bool):
        return None
    if isinstance(value, float) and (value <= 0 or not value.is_integer()):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clock_for(sched_date: date, hhmm: str, timezone: tzinfo) -> datetime:
    hour, minute = (int(part) for part in str(hhmm).split(':', 1))
    return datetime(
        sched_date.year, sched_date.month, sched_date.day,
        hour, minute, tzinfo=timezone,
    )


def event_bounds(item: Dict[str, Any], sched_date: date, timezone: tzinfo):
    """Return date-anchored planned bounds, preserving overnight events."""
    start = item.get('planned_start_at')
    end = item.get('planned_end_at')
    if isinstance(start, datetime) and isinstance(end, datetime):
        return start, end
    start = _clock_for(sched_date, item['start_time'], timezone)
    end = _clock_for(sched_date, item['end_time'], timezone)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def timeline_is_valid(items: Iterable[Dict[str, Any]], sched_date: date,
                      timezone: tzinfo) -> bool:
    """Return whether planned items form one non-overlapping timeline."""
    bounds = []
    for item in items or []:
        if not isinstance(item, dict):
            return False
        try:
            start, end = event_bounds(item, sched_date, timezone)
        except (KeyError, TypeError, ValueError):
            return False
        if end <= start:
            return False
        bounds.append((start, end))
    bounds.sort(key=lambda value: value[0])
    return all(
        previous[1] <= current[0]
        for previous, current in zip(bounds, bounds[1:])
    )


def _phase_from_source(source: Dict[str, Any], ordinal: int,
                       event_start: datetime, event_end: datetime,
                       timezone: tzinfo) -> Optional[Dict[str, Any]]:
    try:
        start = source.get('planned_start_at')
        end = source.get('planned_end_at')
        if not isinstance(start, datetime):
            start = event_start.replace(
                hour=int(str(source['start_time']).split(':', 1)[0]),
                minute=int(str(source['start_time']).split(':', 1)[1]),
            )
            if start < event_start and event_end.date() > event_start.date():
                start += timedelta(days=1)
        if not isinstance(end, datetime):
            end = event_start.replace(
                hour=int(str(source['end_time']).split(':', 1)[0]),
                minute=int(str(source['end_time']).split(':', 1)[1]),
            )
            if end <= start:
                end += timedelta(days=1)
    except (KeyError, TypeError, ValueError):
        return None
    start = max(start, event_start)
    end = min(end, event_end)
    if end <= start:
        return None
    if start < event_start or end > event_end:
        return None
    reply_state = normalize_reply_state(
        source.get('reply_state'), source.get('can_reply', True))
    return {
        'ordinal': ordinal,
        'title': str(source.get('title') or '').strip(),
        'kind': str(source.get('kind') or 'activity').strip() or 'activity',
        'reply_state': reply_state,
        'planned_start_at': start,
        'planned_end_at': end,
        'actual_start_at': source.get('actual_start_at'),
        'actual_end_at': source.get('actual_end_at'),
        'status': source.get('status') or EVENT_PLANNED,
        'planned_place': source.get('planned_place'),
        'provenance': dict(source.get('provenance') or {}),
    }


def build_phase_plan(item: Dict[str, Any], sched_date: date,
                     timezone: tzinfo) -> List[Dict[str, Any]]:
    """Expand a generated event into the phases that govern availability.

    An LLM may provide explicit phases.  Otherwise, a soft-busy event with a
    shorter effective duration is split into a focused phase and an explicitly
    free follow-up phase.  The visual event remains intact; only the canonical
    current phase controls phone availability.
    """
    event_start, event_end = event_bounds(item, sched_date, timezone)
    explicit = item.get('phases')
    if isinstance(explicit, list) and explicit:
        phases = []
        for index, raw in enumerate(explicit):
            if not isinstance(raw, dict):
                continue
            phase = _phase_from_source(raw, index, event_start, event_end, timezone)
            if phase:
                phases.append(phase)
        phases.sort(key=lambda phase: phase['planned_start_at'])
        complete_timeline = bool(
            phases
            and phases[0]['planned_start_at'] == event_start
            and phases[-1]['planned_end_at'] == event_end
            and all(phase.get('title') for phase in phases)
            and all(
                phases[index]['planned_end_at']
                == phases[index + 1]['planned_start_at']
                for index in range(len(phases) - 1)
            )
        )
        if complete_timeline:
            return phases

    reply_state = normalize_reply_state(
        item.get('reply_state'), item.get('can_reply', True))
    title = str(item.get('title') or '').strip()
    base = {
        'title': title,
        'kind': str(item.get('phase_kind') or 'activity'),
        'planned_place': item.get('planned_place'),
        'provenance': dict(item.get('provenance') or {}),
    }
    minutes = parse_effective_busy_minutes(item.get('effective_busy_minutes'))
    duration = int((event_end - event_start).total_seconds() // 60)
    if (reply_state == REPLY_SOFT_BUSY and minutes is not None
            and minutes < duration):
        focus_end = event_start + timedelta(minutes=minutes)
        return [
            {
                **base,
                'ordinal': 0,
                'title': title,
                'kind': 'focused_task',
                'reply_state': REPLY_SOFT_BUSY,
                'planned_start_at': event_start,
                'planned_end_at': focus_end,
                'actual_start_at': None,
                'actual_end_at': None,
                'status': EVENT_PLANNED,
            },
            {
                **base,
                'ordinal': 1,
                'title': f'{title}（后续）' if title else '后续安排',
                'kind': 'follow_up',
                'reply_state': REPLY_FREE,
                'planned_start_at': focus_end,
                'planned_end_at': event_end,
                'actual_start_at': None,
                'actual_end_at': None,
                'status': EVENT_PLANNED,
            },
        ]
    return [{
        **base,
        'ordinal': 0,
        'reply_state': reply_state,
        'planned_start_at': event_start,
        'planned_end_at': event_end,
        'actual_start_at': None,
        'actual_end_at': None,
        'status': EVENT_PLANNED,
    }]


def advance_phase_states(phases: Iterable[Dict[str, Any]], now: datetime):
    """Return a copied phase list after deterministic clock progression."""
    advanced = []
    for source in phases:
        phase = deepcopy(source)
        status = phase.get('status') or EVENT_PLANNED
        start = phase.get('planned_start_at')
        end = phase.get('planned_end_at')
        if status in EVENT_TERMINAL or not isinstance(start, datetime) or not isinstance(end, datetime):
            advanced.append(phase)
            continue
        if now >= end:
            phase['status'] = EVENT_COMPLETED
            phase['actual_start_at'] = phase.get('actual_start_at') or start
            phase['actual_end_at'] = phase.get('actual_end_at') or end
        elif now >= start:
            phase['status'] = EVENT_ACTIVE
            phase['actual_start_at'] = phase.get('actual_start_at') or start
        else:
            phase['status'] = EVENT_PLANNED
        advanced.append(phase)
    return advanced


def availability_for_phase(phase: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    state = normalize_reply_state((phase or {}).get('reply_state'), True)
    return {
        'reply_state': state,
        'can_reply': state == REPLY_FREE,
        'phase_id': (phase or {}).get('id'),
        'phase_status': (phase or {}).get('status'),
    }


def normalize_action_intent(value: Any) -> Optional[Dict[str, Any]]:
    """Accept only explicit, structured schedule changes.

    This deliberately refuses free text.  A caller may display model text, but
    it cannot become world state without this structured object.
    """
    if not isinstance(value, dict):
        return None
    action = _ACTION_ALIASES.get(str(value.get('type') or '').strip().lower())
    if action not in ACTION_TYPES:
        return None
    intent = {'type': action}
    if action != ACTION_INSERT:
        event_id = value.get('event_id')
        if isinstance(event_id, bool) or event_id in (None, ''):
            return None
        try:
            intent['event_id'] = int(event_id)
        except (TypeError, ValueError):
            return None
        revision = value.get('expected_revision')
        if isinstance(revision, bool) or revision in (None, ''):
            return None
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            return None
        if revision < 1:
            return None
        intent['expected_revision'] = revision
    if action == ACTION_EXTEND:
        try:
            minutes = int(value.get('extend_minutes'))
        except (TypeError, ValueError):
            return None
        if not 1 <= minutes <= 480:
            return None
        intent['extend_minutes'] = minutes
    if action in (ACTION_RELOCATE, ACTION_INSERT):
        place = value.get('planned_place') or value.get('target_place')
        if place is not None and not isinstance(place, dict):
            return None
        if isinstance(place, dict):
            # Resolver-owned identity only; an unverified display string cannot
            # become a precise map fact.
            provider = str(place.get('provider') or '').strip()
            provider_place_id = str(place.get('provider_place_id') or '').strip()
            if provider or provider_place_id:
                if not (provider and provider_place_id):
                    return None
                intent['planned_place'] = {
                    'provider': provider[:40],
                    'provider_place_id': provider_place_id[:120],
                }
            elif place.get('area_description'):
                intent['planned_place'] = {
                    'area_description': str(place['area_description']).strip()[:120],
                }
    if action == ACTION_INSERT:
        new_event = value.get('event') or value.get('next_event')
        if not isinstance(new_event, dict):
            return None
        title = str(new_event.get('title') or '').strip()
        try:
            duration = int(new_event.get('duration_minutes'))
        except (TypeError, ValueError):
            return None
        if not title or not 1 <= duration <= 480:
            return None
        intent['event'] = {
            'title': title[:80],
            'duration_minutes': duration,
            'reply_state': normalize_reply_state(
                new_event.get('reply_state'), new_event.get('can_reply', True)),
            'category': str(new_event.get('category') or 'routine')[:40],
            'fixedness': str(new_event.get('fixedness') or FLEXIBLE)[:20],
        }
        if intent.get('planned_place'):
            intent['event']['planned_place'] = intent['planned_place']
    return intent


def completion_claim_conflict(messages: Iterable[Dict[str, Any]],
                              world: Optional[Dict[str, Any]],
                              intent: Optional[Dict[str, Any]]) -> Optional[str]:
    """Reject uncommitted completion claims; never converts words into state."""
    event = (world or {}).get('event') or {}
    if event.get('status') != EVENT_ACTIVE:
        return None
    text = ' '.join(
        f'{item.get("jp", "")} {item.get("zh", "")}'
        for item in (messages or []) if isinstance(item, dict)
    )
    if not _COMPLETION_CLAIM.search(text):
        return None
    if (intent and intent.get('type') == ACTION_COMPLETE
            and intent.get('event_id') == event.get('id')
            and intent.get('expected_revision') == event.get('revision')):
        return None
    return 'active_event_completion_claim_without_intent'
