"""Character initiative policy: expression only, never writes rel_state."""

POLICIES = {
    'gojo': {
        'style': 'active',
        'romantic_awareness': 12,
        'flirt_probe': 15,
        'confession': 40,
        'commitment': 55,
    },
    'geto': {
        'style': 'cautious',
        'romantic_awareness': 18,
        'flirt_probe': 28,
        'confession': 48,
        'commitment': 60,
    },
    'minato': {
        'style': 'avoidant',
        'romantic_awareness': 20,
        'flirt_probe': 35,
        'confession': 55,
        'commitment': 65,
    },
}
DEFAULT_POLICY = {
    'style': 'cautious',
    'romantic_awareness': 18,
    'flirt_probe': 28,
    'confession': 45,
    'commitment': 60,
}


def initiative_policy(character_id, state=None) -> dict:
    policy = dict(POLICIES.get(str(character_id or ''), DEFAULT_POLICY))
    policy['character_id'] = character_id
    state = state or {}
    policy['passion'] = float(state.get('passion') or 0)
    return policy


def initiative_guidance(character_id, state=None, *, is_love=False) -> str:
    """Tell the generator it may act, without rewriting durable relationship state."""
    if is_love:
        return ''
    policy = initiative_policy(character_id, state)
    passion = policy['passion']
    style = policy['style']
    aware = passion >= policy['romantic_awareness']
    can_probe = passion >= policy['flirt_probe']
    if not aware:
        return (
            '底层关系状态仍不是爱情。你可以按人设正常互动；'
            '不要把一次玩笑或关心自动说成确认恋爱。'
        )
    if style == 'active' and can_probe:
        return (
            '底层关系状态仍不是爱情，但按你的性格可以主动试探、调情或靠近。'
            '这些是你的行为，不代表账本已经升级。用户怎么回应才会成为新的关系证据。'
        )
    if style == 'avoidant' and aware:
        return (
            '你可能已经察觉到自己的在意，但按性格更可能拉开距离、嘴硬或转移话题。'
            '主动回避也是行为，不是把关系状态改回去。'
        )
    return (
        '你或许已经意识到某种心动或暧昧张力，但按性格仍应继续观察，'
        '不必急着表白或确认关系。主动与否由人设决定，不能改写底层关系事实。'
    )
