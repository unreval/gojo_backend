"""Deterministic Slow Loop revision helpers. No LLM. No rel_state writes."""
import re

from cognitive_config import (
    COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE,
    COGNITIVE_CONFIDENCE_DELTA,
    COGNITIVE_SCOPE_CROSS_MIN_CONTEXTS,
    COGNITIVE_SCOPE_TENDENCY_MIN_CONTEXTS,
    SHARED_RELATIONSHIP_FRAME_KEY,
)


EVIDENCE_RELATIONS = frozenset({
    'support', 'contradiction', 'scope_limiter', 'irrelevant',
})
EVIDENCE_STRENGTHS = frozenset({'weak', 'normal', 'strong'})
BELIEF_SCOPES = frozenset({
    'single_event', 'topic', 'relationship_context',
    'cross_context', 'general_tendency', 'unknown', 'legacy',
})
FRAME_KINDS = frozenset({
    'friends', 'playful_ambiguous', 'probing',
    'serious_unconfirmed', 'committed_romantic', 'frame_shifting',
    'unknown', 'legacy',
})
REVIEW_STATUSES = frozenset({
    'stable', 'under_review', 'reopened',
})

_EPISODIC_RE = re.compile(
    r'(今晚|今天|刚才|刚刚|本次|这轮|这一次|那一次|昨晚|今早)'
    r'|(又喝酒|去睡觉|说自己很痛苦|要求用户)'
)
_QUOTED_UTTERANCE_RE = re.compile(r'[「『“"].{2,40}[」』”"]')
_GLOBAL_JUDGMENT_RE = re.compile(
    r'(缺乏独立|没有主见|不会自主|总是依赖|性格软弱|人格不成熟'
    r'|只有.{0,8}一个长期目标|永远不会)'
)
_REVISION_STICKY_RE = re.compile(
    r'(旧判断|可能不对|认识可能错|需要修正|不再只是'
    r'|好像开始有独立|原有.{0,12}判断)'
)


def is_episodic_statement(text) -> bool:
    """Single-event recap / utterance / instantaneous state cannot be a belief."""
    statement = str(text or '').strip()
    if not statement:
        return True
    if _EPISODIC_RE.search(statement) and len(statement) < 80:
        return True
    if _QUOTED_UTTERANCE_RE.search(statement) and len(statement) < 60:
        return True
    return False


def is_global_personality_judgment(text) -> bool:
    return bool(_GLOBAL_JUDGMENT_RE.search(str(text or '')))


def looks_like_belief_revision_sticky(content) -> bool:
    return bool(_REVISION_STICKY_RE.search(str(content or '')))


def normalize_scope(value, *, legacy=False) -> str:
    raw = str(value or '').strip()
    if raw in BELIEF_SCOPES:
        return raw
    return 'legacy' if legacy else 'unknown'


def allowed_scope_for_contexts(context_count) -> str:
    count = max(0, int(context_count or 0))
    if count >= COGNITIVE_SCOPE_TENDENCY_MIN_CONTEXTS:
        return 'general_tendency'
    if count >= COGNITIVE_SCOPE_CROSS_MIN_CONTEXTS:
        return 'cross_context'
    if count == 1:
        return 'topic'
    return 'single_event'


def scope_exceeds_evidence(scope, independent_contexts) -> bool:
    scope = normalize_scope(scope)
    if scope not in {'cross_context', 'general_tendency'}:
        return False
    allowed = allowed_scope_for_contexts(len(independent_contexts or []))
    rank = {
        'single_event': 0,
        'topic': 1,
        'relationship_context': 1,
        'unknown': 1,
        'legacy': 1,
        'cross_context': 2,
        'general_tendency': 3,
    }
    return rank.get(scope, 1) > rank.get(allowed, 1)


def confidence_delta(relation, strength) -> float:
    relation = str(relation or '').strip()
    strength = str(strength or 'normal').strip()
    if relation not in EVIDENCE_RELATIONS or relation == 'irrelevant':
        return 0.0
    if strength not in EVIDENCE_STRENGTHS:
        strength = 'normal'
    return float(COGNITIVE_CONFIDENCE_DELTA[(relation, strength)])


def apply_confidence_delta(old_confidence, relation, strength) -> float:
    old = float(old_confidence or 0.0)
    updated = old + confidence_delta(relation, strength)
    return max(0.0, min(1.0, round(updated, 4)))


def review_status_after_confidence(new_confidence, *, was_committed=False) -> str:
    if was_committed and new_confidence < COGNITIVE_BELIEF_COMMIT_MIN_CONFIDENCE:
        return 'under_review'
    return 'stable'


def append_revision_history(metadata, *, old_statement, old_confidence,
                            new_statement, new_confidence, relation,
                            reason, evidence_refs, cycle_id):
    meta = dict(metadata or {})
    history = list(meta.get('revision_history') or [])
    history.append({
        'old_statement': old_statement,
        'old_confidence': old_confidence,
        'new_statement': new_statement,
        'new_confidence': new_confidence,
        'evidence_relation': relation,
        'revision_reason': reason,
        'evidence_refs': list(evidence_refs or []),
        'cycle_id': cycle_id,
        'superseded': relation in {'contradiction', 'scope_limiter'},
    })
    meta['revision_history'] = history[-20:]
    return meta


def current_belief_display(belief) -> str:
    """Prompt injection shows the live cognition, not simultaneous old+new facts."""
    statement = str((belief or {}).get('statement') or '').strip()
    meta = belief.get('metadata') if isinstance(belief, dict) else {}
    if not isinstance(meta, dict):
        meta = {}
    review = meta.get('review_status') or 'stable'
    history = meta.get('revision_history') or []
    if review == 'under_review' and history:
        previous = history[-1].get('old_statement') or ''
        if previous and previous != statement:
            return (
                f'过去曾判断「{previous}」；近期证据使其正在被重新评估，'
                f'当前更准确的说法是「{statement}」。'
            )
        return f'{statement}（该认识正在被新证据重新评估，还不是稳定事实）'
    return statement


def is_stable_reader_belief(belief) -> bool:
    if not isinstance(belief, dict):
        return False
    if belief.get('status') not in (None, 'active'):
        return False
    meta = belief.get('metadata') if isinstance(belief.get('metadata'), dict) else {}
    review = meta.get('review_status') or 'stable'
    if review in {'under_review', 'reopened'}:
        return False
    return float(belief.get('confidence') or 0) >= 0.65


def infer_hypothesis_relation(supporting_refs, contradicting_refs) -> str:
    support_n = len(supporting_refs or [])
    contra_n = len(contradicting_refs or [])
    if contra_n and not support_n:
        return 'contradiction'
    if support_n and not contra_n:
        return 'support'
    if contra_n > support_n:
        return 'contradiction'
    if support_n:
        return 'support'
    return 'irrelevant'


SHARED_FRAME_KEY = SHARED_RELATIONSHIP_FRAME_KEY
