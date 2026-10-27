"""Generator-facing guard for structured schedule transitions.

This is deliberately a narrow adapter: generated prose may be rejected when
it contradicts the canonical world, but only an explicit validated object can
reach ``db_schedule.commit_schedule_transition``.
"""
from __future__ import annotations

import json

from typing import Any, Dict, Optional, Tuple

from schedule_contract import (
    EVENT_ACTIVE, change_claim_conflict, completion_claim_conflict,
    normalize_action_intent,
)


def _claim_outcome(reason):
    """Record a claim-guard hit; veto only when the strict flag is on.

    These guards read wording alone and hold no authority over schedule facts:
    commit_schedule_transition is the only writer and revalidates in its own
    transaction, and a claim conflict means no matching intent was proposed, so
    nothing was going to be committed either way. A lexical miss must therefore
    not cost the user a reply. This mirrors TRANSLATION_CHECKS_BLOCKING. Intent
    preflight is a separate check and stays blocking.
    """
    if not reason:
        return None
    from config import SCHEDULE_CLAIM_CHECKS_BLOCKING
    print('[schedule_check] ' + json.dumps(
        {'reason': reason, 'blocking': SCHEDULE_CLAIM_CHECKS_BLOCKING},
        ensure_ascii=False))
    return reason if SCHEDULE_CLAIM_CHECKS_BLOCKING else None


def validate_generated_schedule_reply(character_id: str, user_id: str,
                                      parsed: Dict[str, Any], *, now=None
                                      ) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """Return a rejection reason and the canonical world used for the check."""
    import db_schedule

    try:
        world = db_schedule.get_current_world_state(character_id, user_id, now)
    except Exception as exc:
        # An unavailable schedule reader cannot establish a contradictory
        # current fact. Keep an ordinary reply available; an explicit action
        # intent still goes through the writer and fails closed there.
        print(f'[schedule] world truth guard skipped: {exc}')
        return None, None
    intent = normalize_action_intent(
        (parsed or {}).get('schedule_action_intent'))
    conflict = completion_claim_conflict(
        (parsed or {}).get('messages') or [], world, intent)
    if conflict:
        return _claim_outcome(conflict), world
    planned = []
    if ((world or {}).get('event') or {}).get('status') != EVENT_ACTIVE:
        try:
            conn = db_schedule.get_conn()
            try:
                cur = conn.cursor()
                try:
                    planned = db_schedule._remaining_planned_events_tx(
                        cur, character_id, user_id, world['now'])
                finally:
                    cur.close()
            finally:
                conn.close()
        except Exception as exc:
            # Preserve the existing unavailable-reader behavior for ordinary chat.
            print(f'[schedule] planned truth guard skipped: {exc}')
    return _claim_outcome(change_claim_conflict(
        (parsed or {}).get('messages') or [], world, planned, intent)), world


def commit_generated_schedule_intent(character_id: str, user_id: str,
                                     parsed: Dict[str, Any], *, now=None,
                                     source_event_id: str = '') -> Dict[str, Any]:
    """Commit an explicit proposal after the visible reply has passed guards.

    ``None`` is a normal no-op.  Invalid action objects do not silently become
    facts; they are reported to the caller so it can fail closed before showing
    any potentially contradictory reply.
    """
    raw_intent = (parsed or {}).get('schedule_action_intent')
    if raw_intent is None:
        return {'ok': True, 'noop': True}
    intent = normalize_action_intent(raw_intent)
    if not intent:
        return {'ok': False, 'reason': 'invalid_structured_action_intent'}
    import db_schedule
    return db_schedule.commit_schedule_transition(
        character_id, user_id, intent, now=now,
        source_event_id=source_event_id)
