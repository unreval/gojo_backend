"""Character initiative policy for expression pacing only.

Passion is interaction-tension telemetry.  It may adjust whether a character
tries a cautious probe or slows down, but it never means the character has
recognized, confirmed, or denied a romantic relationship.
"""

POLICIES = {
    'gojo': {
        'style': 'active',
        'tension_watch': 12,
        'tension_probe': 15,
    },
    'geto': {
        'style': 'cautious',
        'tension_watch': 18,
        'tension_probe': 28,
    },
    'minato': {
        'style': 'avoidant',
        'tension_watch': 20,
        'tension_probe': 35,
    },
}
DEFAULT_POLICY = {
    'style': 'cautious',
    'tension_watch': 18,
    'tension_probe': 28,
}


def initiative_policy(character_id, state=None) -> dict:
    policy = dict(POLICIES.get(str(character_id or ''), DEFAULT_POLICY))
    policy['character_id'] = character_id
    policy['passion'] = float((state or {}).get('passion') or 0)
    return policy


def initiative_guidance(character_id, state=None, *, romantic_label='unresolved',
                         is_love=None) -> str:
    """Give pacing guidance without declaring a hidden relationship state.

    ``is_love`` remains a tolerated keyword for older callers but is
    intentionally ignored.  Only the panel-provided Cognitive label can turn
    off the unresolved-state caution.
    """
    if romantic_label == 'affirmed':
        return ''
    policy = initiative_policy(character_id, state)
    tension = policy['passion']
    style = policy['style']
    if tension < policy['tension_watch']:
        return (
            '互动张力遥测不足以改变关系结论。按人设正常互动；'
            '不要把玩笑、关心或一次回应写成确认的浪漫关系。'
        )
    if style == 'active' and tension >= policy['tension_probe']:
        return (
            '互动张力可供节奏参考；按人设可以主动试探、调情或靠近。'
            '这些只是表达行为，不代表已意识到或确认浪漫性质。'
        )
    if style == 'avoidant':
        return (
            '互动张力可供节奏参考；按人设可以拉开距离、嘴硬或转移话题。'
            '这是节奏与风险管理，不是对关系性质的判断。'
        )
    return (
        '互动张力可供节奏参考；按人设继续观察或谨慎回应。'
        '不得把它解释为角色已经意识到心动、爱情或任何确认结论。'
    )
