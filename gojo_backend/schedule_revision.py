"""Deterministic schedule revision — zero LLM, zero external API.

Called once per character from diary_scheduler._tick (every ~3 hours).
Each rule processes at most one event per tick to avoid wholesale rearrangement.
All writes go through db_schedule.commit_schedule_transition.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# ── Rule IDs — must match schedule_contract.REVISION_REASONS ──
RULE_POI_CLOSED = 'poi_closed'
RULE_UNREACHABLE = 'unreachable'
RULE_FATIGUE = 'fatigue'

# ── Tuning constants ──
_FATIGUE_THRESHOLD_MINUTES = 360   # 6 h of hard_busy → cancel optional
_ASSUMED_SPEED_KMH = 18            # city transit assumption


# ═══════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════

def _refresh_planned(character_id, user_id, now):
    """Re-fetch today's events and filter to planned-future subset."""
    today = _get_today_events(character_id, user_id, now)
    planned = [e for e in today
               if e['status'] == 'planned' and e['planned_start_at']
               and e['planned_start_at'] > now]
    return today, planned


def revise_schedule(character_id: str, user_id: str,
                    now: datetime | None = None) -> List[Dict[str, Any]]:
    """Run deterministic revision rules on today's planned future events.

    Returns [{rule, event_id, action, detail, ok}, ...] for each fired rule.
    Each rule fires on at most one event per call.
    After a rule commits a change, subsequent rules re-read the DB
    so they see the post-commit state.
    """
    from config import CN_TZ
    now = now or datetime.now(CN_TZ)
    now = now.astimezone(CN_TZ) if now.tzinfo else now.replace(tzinfo=CN_TZ)

    today_events, planned = _refresh_planned(character_id, user_id, now)
    if not planned:
        return []

    results: list[dict] = []

    r = _rule_poi_closed(planned, character_id, user_id, now)
    if r:
        results.append(r)
        today_events, planned = _refresh_planned(character_id, user_id, now)
        if not planned:
            return results

    r = _rule_unreachable(today_events, planned, character_id, user_id, now)
    if r:
        results.append(r)
        today_events, planned = _refresh_planned(character_id, user_id, now)
        if not planned:
            return results

    r = _rule_fatigue(today_events, planned, character_id, user_id, now)
    if r:
        results.append(r)

    return results


# ═══════════════════════════════════════════════════════════
#  Data access
# ═══════════════════════════════════════════════════════════

def _get_today_events(character_id: str, user_id: str,
                      now: datetime) -> List[Dict[str, Any]]:
    """All non-cancelled events for today, sorted by planned_start_at.

    Advances world state first so statuses reflect clock-driven transitions.
    """
    import db_schedule

    # Advance world state (auto-completes overdue phases/events)
    try:
        db_schedule.get_current_world_state(character_id, user_id, now)
    except Exception:
        pass

    from db import get_conn
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute('''
            SELECT id, start_time, end_time, title, location,
                   planned_start_at, planned_end_at,
                   actual_start_at, actual_end_at,
                   status, revision, fixedness,
                   COALESCE(reply_state, ''), planned_place, category
            FROM char_schedule
            WHERE character_id=%s AND user_id=%s
              AND sched_date=%s
              AND status <> 'cancelled'
            ORDER BY planned_start_at ASC, id ASC
        ''', (character_id, user_id, now.date()))
        rows = cur.fetchall() or []
        return [_row_to_event(row) for row in rows]
    finally:
        cur.close()
        conn.close()


def _row_to_event(row) -> Dict[str, Any]:
    place_raw = row[13]
    if place_raw is None:
        place = None
    elif isinstance(place_raw, dict):
        place = place_raw
    else:
        try:
            place = json.loads(place_raw)
        except Exception:
            place = None
    return {
        'id': row[0],
        'start_time': row[1],
        'end_time': row[2],
        'title': row[3] or '',
        'location': row[4] or '',
        'planned_start_at': row[5],
        'planned_end_at': row[6],
        'actual_start_at': row[7],
        'actual_end_at': row[8],
        'status': row[9] or 'planned',
        'revision': int(row[10] or 1),
        'fixedness': row[11] or 'flexible',
        'reply_state': row[12] or 'free',
        'planned_place': place,
        'category': row[14] or 'routine',
    }


# ═══════════════════════════════════════════════════════════
#  Commit helper
# ═══════════════════════════════════════════════════════════

def _commit(character_id: str, user_id: str, intent: dict,
            now: datetime) -> Dict[str, Any]:
    """Submit an action intent through the single canonical write path."""
    import db_schedule
    return db_schedule.commit_schedule_transition(
        character_id, user_id, intent, now=now,
        source_event_id='tick_revision')


# ═══════════════════════════════════════════════════════════
#  Opening-hours helper
# ═══════════════════════════════════════════════════════════

def _get_opening_spec(place: dict) -> Optional[dict]:
    """Extract or re-derive opening_spec from planned_place data.

    Callers may supply an already-derived opening_spec. Canonical persisted
    events retain the raw 'opening_hours' text in provider_raw_type.tags,
    so parse those existing provider facts when no usable spec is present.
    """
    # Fast path: spec already present
    spec = place.get('opening_spec')
    if spec and isinstance(spec, dict) and spec.get('status') in ('parsed', 'always_open'):
        return spec

    # Fallback: re-parse from raw OSM tags
    raw_type = place.get('provider_raw_type')
    if not raw_type or not isinstance(raw_type, dict):
        return None
    tags = raw_type.get('tags')
    if not tags or not isinstance(tags, dict):
        return None
    oh_raw = tags.get('opening_hours')
    if not oh_raw:
        return None
    try:
        from opening_hours import parse_opening_hours
        return parse_opening_hours(oh_raw)
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════
#  Haversine (self-contained — avoids importing places_engine)
# ═══════════════════════════════════════════════════════════

def _haversine_km(lat1: float, lng1: float,
                  lat2: float, lng2: float) -> float:
    """Great-circle distance in km."""
    R = 6371.0
    lat1_r, lat2_r = math.radians(lat1), math.radians(lat2)
    dlat = lat2_r - lat1_r
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1_r) * math.cos(lat2_r) * math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ═══════════════════════════════════════════════════════════
#  Rule 1: poi_closed
# ═══════════════════════════════════════════════════════════

def _rule_poi_closed(planned: list, character_id: str, user_id: str,
                     now: datetime) -> Optional[Dict[str, Any]]:
    """If a planned POI is definitely closed at event time → relocate or cancel.

    Uses is_definitely_closed from opening_hours.py (A1).
    Optional/flavor events are cancelled; others relocate to 一般区域.

    Extracts opening_spec from the stored JSONB; falls back to re-parsing
    the raw tag from provider_raw_type.tags.opening_hours for persisted events.
    Converts planned_start_at to CN_TZ before extracting weekday/time.
    """
    try:
        from opening_hours import is_definitely_closed
    except ImportError:
        return None

    from config import CN_TZ

    for event in planned:
        place = event.get('planned_place')
        if not place:
            continue

        spec = _get_opening_spec(place)
        if not spec:
            continue

        # Derive weekday and time_minutes in character timezone
        start = event['planned_start_at']
        if not start:
            continue
        start_local = start.astimezone(CN_TZ) if start.tzinfo else start.replace(tzinfo=CN_TZ)
        weekday = start_local.weekday()
        time_min = start_local.hour * 60 + start_local.minute

        if not is_definitely_closed(spec, weekday, time_min):
            continue

        # ── POI is definitely closed ──
        poi_name = (place.get('canonical_name')
                    or place.get('name')
                    or event.get('location')
                    or '?')

        if event['fixedness'] in ('optional', 'flavor'):
            intent = {
                'type': 'cancel',
                'event_id': event['id'],
                'expected_revision': event['revision'],
                'reason': RULE_POI_CLOSED,
            }
            result = _commit(character_id, user_id, intent, now)
            return {
                'rule': RULE_POI_CLOSED,
                'event_id': event['id'],
                'action': 'cancel',
                'detail': f'{poi_name} closed',
                'ok': result.get('ok', False),
            }
        else:
            # Relocate: omit planned_place entirely → commit_schedule_transition
            # sets planned_place=NULL and location='一般区域' via _general_area(None).
            intent = {
                'type': 'relocate',
                'event_id': event['id'],
                'expected_revision': event['revision'],
                'reason': RULE_POI_CLOSED,
            }
            result = _commit(character_id, user_id, intent, now)
            return {
                'rule': RULE_POI_CLOSED,
                'event_id': event['id'],
                'action': 'relocate',
                'detail': f'{poi_name} closed → 一般区域',
                'ok': result.get('ok', False),
            }

    return None


# ═══════════════════════════════════════════════════════════
#  Rule 2: unreachable
# ═══════════════════════════════════════════════════════════

def _rule_unreachable(all_events: list, planned: list,
                      character_id: str, user_id: str,
                      now: datetime) -> Optional[Dict[str, Any]]:
    """If travel between adjacent POIs exceeds the available gap → relocate.

    Checks all adjacent event pairs (regardless of status) where the LATER
    event is a planned future event. Only acts when BOTH events have lat/lng.
    """
    planned_ids = {e['id'] for e in planned}

    for i in range(1, len(all_events)):
        prev_evt = all_events[i - 1]
        curr_evt = all_events[i]

        # Only act on planned future events
        if curr_evt['id'] not in planned_ids:
            continue

        pp = prev_evt.get('planned_place')
        cp = curr_evt.get('planned_place')
        if not pp or not cp:
            continue

        # Both must have coordinates
        try:
            prev_lat = float(pp['lat'])
            prev_lng = float(pp['lng'])
            curr_lat = float(cp['lat'])
            curr_lng = float(cp['lng'])
        except (KeyError, TypeError, ValueError):
            continue

        dist_km = _haversine_km(prev_lat, prev_lng, curr_lat, curr_lng)

        # Calculate gap in minutes
        prev_end = (prev_evt.get('actual_end_at')
                    or prev_evt.get('planned_end_at'))
        curr_start = curr_evt.get('planned_start_at')
        if not prev_end or not curr_start:
            continue
        try:
            gap_seconds = (curr_start - prev_end).total_seconds()
        except (TypeError, AttributeError):
            continue
        if gap_seconds < 0:
            # Events overlap or are out of order — skip
            continue
        gap_min = gap_seconds / 60.0

        travel_min = (dist_km / _ASSUMED_SPEED_KMH) * 60.0
        if travel_min <= gap_min:
            continue

        # ── Unreachable ──
        intent = {
            'type': 'relocate',
            'event_id': curr_evt['id'],
            'expected_revision': curr_evt['revision'],
            'reason': RULE_UNREACHABLE,
        }
        result = _commit(character_id, user_id, intent, now)
        return {
            'rule': RULE_UNREACHABLE,
            'event_id': curr_evt['id'],
            'action': 'relocate',
            'detail': f'{dist_km:.1f}km / {gap_min:.0f}min gap',
            'ok': result.get('ok', False),
        }

    return None


# ═══════════════════════════════════════════════════════════
#  Rule 3: fatigue
# ═══════════════════════════════════════════════════════════

def _rule_fatigue(all_events: list, planned: list,
                  character_id: str, user_id: str,
                  now: datetime) -> Optional[Dict[str, Any]]:
    """If cumulative hard_busy exceeds threshold → cancel next optional/flavor.

    Derives fatigue from today's completed+active events (B2 inline).
    For active events, counts only the elapsed portion up to ``now``
    (actual_end_at is None while the event is still running).
    """
    hard_busy_minutes = 0.0
    for event in all_events:
        if event['status'] not in ('active', 'completed'):
            continue
        if event['reply_state'] != 'hard_busy':
            continue
        start = event.get('actual_start_at') or event.get('planned_start_at')
        end = event.get('actual_end_at') or event.get('planned_end_at')
        if not start or not end:
            continue
        # Active events: cap end at now — only count elapsed time
        if event['status'] == 'active' and end > now:
            end = now
        try:
            minutes = (end - start).total_seconds() / 60.0
            if minutes > 0:
                hard_busy_minutes += minutes
        except (TypeError, AttributeError):
            pass

    if hard_busy_minutes <= _FATIGUE_THRESHOLD_MINUTES:
        return None

    # Find the first optional/flavor planned event to cancel
    for event in planned:
        if event['fixedness'] not in ('optional', 'flavor'):
            continue
        intent = {
            'type': 'cancel',
            'event_id': event['id'],
            'expected_revision': event['revision'],
            'reason': RULE_FATIGUE,
        }
        result = _commit(character_id, user_id, intent, now)
        return {
            'rule': RULE_FATIGUE,
            'event_id': event['id'],
            'action': 'cancel',
            'detail': f'{hard_busy_minutes:.0f}min hard_busy',
            'ok': result.get('ok', False),
        }

    return None
