"""Read-only interpretation of historical signal labels. Never production authority.

These labels cannot create evidence, mutate a ledger, or settle predictions.
"""

_FLIRT_RESPONSE_BY_SIGNAL = {
    'positive_reciprocal': 'accepted',
    'ambiguous_response': 'held',
    'explicit_rejection': 'rejected',
}


def aggregate_user_flirt_response(signals):
    """Deterministic user-only flirt response for one turn.

    accepted / held / rejected if exactly one of those user signals is present.
    mixed if two or more distinct classes appear (order independent).
    None if there is no user flirt-response evidence.
    offensive_content never maps here.
    """
    classes = []
    seen = set()
    for signal in signals or []:
        if not isinstance(signal, dict):
            continue
        if signal.get('actor') != 'user':
            continue
        mapped = _FLIRT_RESPONSE_BY_SIGNAL.get(signal.get('signal_type'))
        if not mapped or mapped in seen:
            continue
        seen.add(mapped)
        classes.append(mapped)
    if not classes:
        return None
    if len(classes) == 1:
        return classes[0]
    return 'mixed'
