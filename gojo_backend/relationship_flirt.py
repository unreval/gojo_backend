"""Deterministic flirt interpretation for v4 Fast Loop. No LLM."""

PLAYFUL_FLIRT = frozenset({
    'playful_flirt', 'habitual_flirt', 'social_flirt',
})
ROMANTIC_FLIRT = frozenset({
    'romantic_probe', 'romantic_admission',
})
FLIRT_INTERPRETATIONS = PLAYFUL_FLIRT | ROMANTIC_FLIRT | frozenset({
    'ambiguous_flirt',
})
FRAME_BREAK_MARKERS = (
    '这次不是开玩笑', '这次我是认真的', '不是开玩笑', '别当玩笑', '我是认真的',
)


def normalize_flirt_interpretation(value) -> str:
    raw = str(value or '').strip()
    if raw in FLIRT_INTERPRETATIONS:
        return raw
    return 'ambiguous_flirt'


def utterance_suggests_frame_break(text) -> bool:
    blob = str(text or '')
    return any(marker in blob for marker in FRAME_BREAK_MARKERS)


def interpret_flirt_effect(
    interpretation,
    *,
    reciprocal=False,
    frame_kind=None,
    frame_confidence=0.0,
    frame_break=False,
    exclusive_to_character=False,
    habitual_with_others=False,
    meta_serious=False,
):
    """Return deterministic v4 routing for a flirt / reciprocal signal.

    Never returns a direct passion score change. Pending passion is only
    allowed for romantic evidence, and even then only as +0 or +1.
    """
    kind = normalize_flirt_interpretation(interpretation)
    frame_kind = str(frame_kind or 'unknown')
    friendship_lock = (
        frame_kind in {'friends', 'playful_ambiguous'}
        and float(frame_confidence or 0) >= 0.7
        and not frame_break
        and not meta_serious
    )
    if habitual_with_others and not exclusive_to_character and not meta_serious:
        kind = 'habitual_flirt'

    if frame_break or meta_serious:
        return {
            'interpretation': 'romantic_probe',
            'pending_passion_delta': 1 if reciprocal else 0,
            'warmth_delta': 0.0,
            'intimacy_delta': 0.0,
            'frame_break_candidate': True,
            'unresolved': False,
            'romantic_evidence': bool(reciprocal),
            'reason': 'frame_break_or_meta_serious',
        }

    if kind in PLAYFUL_FLIRT:
        warmth = 1.5 if reciprocal else 0.5
        intimacy = 1.0 if reciprocal else 0.0
        return {
            'interpretation': kind,
            'pending_passion_delta': 0,
            'warmth_delta': warmth,
            'intimacy_delta': intimacy,
            'frame_break_candidate': False,
            'unresolved': False,
            'romantic_evidence': False,
            'reason': (
                'playful_under_friendship_frame'
                if friendship_lock else 'playful_or_habitual_flirt'
            ),
        }

    if kind == 'ambiguous_flirt':
        return {
            'interpretation': 'ambiguous_flirt',
            'pending_passion_delta': 0,
            'warmth_delta': 0.5 if reciprocal else 0.0,
            'intimacy_delta': 0.0,
            'frame_break_candidate': False,
            'unresolved': True,
            'romantic_evidence': False,
            'reason': 'ambiguous_keep_unresolved',
        }

    pending = 1 if reciprocal else 0
    if kind == 'romantic_admission' and reciprocal:
        pending = 1
    if exclusive_to_character and reciprocal:
        pending = 1
    return {
        'interpretation': kind,
        'pending_passion_delta': pending,
        'warmth_delta': 0.0,
        'intimacy_delta': 0.0,
        'frame_break_candidate': False,
        'unresolved': False,
        'romantic_evidence': bool(pending),
        'reason': 'romantic_probe_or_admission',
    }


def salient_event_allows_reappraisal(state, *, salience='normal') -> bool:
    """High-salience event may open a question/hypothesis, never a passion jump."""
    if salience != 'high':
        return False
    state = state or {}
    return (
        float(state.get('trust') or 0) >= 55
        and float(state.get('intimacy') or 0) >= 50
        and float(state.get('attachment') or 0) >= 50
    )
