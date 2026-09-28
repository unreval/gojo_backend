"""Small, shared invariants for immutable phone-check occurrences."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional, Tuple


TERMINAL = {'consumed', 'resolved', 'expired', 'superseded'}


def inbound_occurrence_action(current: Optional[Dict[str, Any]], *,
                              event_revision: Optional[int] = None) -> str:
    """Return ``append`` or ``successor`` for a new inbound message.

    A processing occurrence is a snapshot owned by one generator.  Appending
    to it would let finish(A) consume B, so B must always start a successor.
    Revision mismatches follow the same rule after a schedule transition.
    """
    if not current:
        return 'new'
    state = str(current.get('check_state') or 'pending')
    if state == 'processing' or state in TERMINAL:
        return 'successor'
    if (event_revision is not None
            and current.get('event_revision') not in (None, event_revision)):
        return 'successor'
    return 'append'


def finish_is_safe(row: Optional[Dict[str, Any]]) -> bool:
    """A claimed occurrence can finish only its immutable claim watermark."""
    if not row:
        return False
    return int(row.get('pending_count') or 0) <= int(row.get('seen_watermark') or 0)


def next_check_window(reply_state: str, now: datetime,
                      replied_at: Optional[datetime] = None) -> Optional[Tuple[int, int]]:
    """Conversation momentum is only available to interruptible soft-busy."""
    if reply_state != 'soft_busy':
        return None
    if replied_at and abs((now - replied_at).total_seconds()) <= 15 * 60:
        return (1, 4)
    return None
