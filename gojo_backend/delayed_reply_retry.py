"""Shared retry limits from the delayed-reply failure policy.

Callers own their pending state and decide how to pause after exhaustion.
"""

PROVIDER_PAUSE_SECONDS = 600


def failure_retry_seconds(fail_count, *, auth_failed=False):
    """Return the cooldown after a failure, or None on the fifth failure."""
    if fail_count >= 5:
        return None
    delay = (120, 600, 1800, 7200)[fail_count - 1]
    return max(delay, 900) if auth_failed else delay
