"""Stable vocabulary for relationship items owned by the Cognitive Slow Loop.

This module deliberately contains no scoring, inference, database access, or
LLM calls.  It is only a shared schema boundary so the ledger, reader, and
Slow Loop agree on which layer owns each semantic conclusion.
"""

ENGAGEMENT_STYLE_KEY = 'relationship.engagement_style'
ROMANTIC_LABEL_KEY = 'relationship.romantic_label'
ROMANTIC_OPENNESS_KEY = 'relationship.romantic_openness'
INTERNAL_CONFLICT_KEY = 'relationship.internal_conflict'

PANEL_SEMANTIC_KEYS = frozenset({
    ENGAGEMENT_STYLE_KEY,
    ROMANTIC_LABEL_KEY,
    ROMANTIC_OPENNESS_KEY,
    INTERNAL_CONFLICT_KEY,
})

# Existing ledger hypotheses may remain as historical telemetry cues.  New
# relationship semantics must use the dotted Cognitive vocabulary above.
LEGACY_PENDING_RELATIONSHIP_CUES = frozenset({
    'love_candidate',
    'strong_attachment_low_trust',
    'romantic_reappraisal',
    'relationship_frame_break',
    'pending_passion_ambiguous',
})

ROMANTIC_LABEL_VALUES = frozenset({
    'unresolved', 'affirmed', 'declined', 'other',
})

# These are user-visible generated expressions, not source facts for a
# relationship conclusion.  The check is intentionally narrow so ordinary
# user chat source ids are never suppressed.
NON_RELATIONSHIP_GENERATED_SOURCE_PREFIXES = (
    'proactive:life_share:',
    'proactive:diary_react:',
    'group:',
)


def is_panel_semantic_key(value) -> bool:
    return str(value or '').strip() in PANEL_SEMANTIC_KEYS


def is_relationship_semantic_key(value) -> bool:
    """Dotted relationship keys belong to Cognitive, never rel_state JSONB."""
    return str(value or '').strip().startswith('relationship.')


def is_new_pending_relationship_semantic_key(value) -> bool:
    """Block newly named relationship conclusions in the ledger JSONB."""
    key = str(value or '').strip()
    if key in LEGACY_PENDING_RELATIONSHIP_CUES:
        return False
    return key.startswith(('relationship.', 'relationship_', 'romantic_'))


def normalize_romantic_label(value, *, allow_legacy_polarity=True):
    """Normalize only explicit narrow encodings; never infer from prose."""
    raw = str(value or '').strip().casefold()
    if raw in ROMANTIC_LABEL_VALUES:
        return raw
    if allow_legacy_polarity:
        if raw == 'yes':
            return 'affirmed'
        if raw == 'no':
            return 'declined'
    return None


def is_nonrelationship_generated_source(source_event_id) -> bool:
    value = str(source_event_id or '').strip()
    return any(value.startswith(prefix)
               for prefix in NON_RELATIONSHIP_GENERATED_SOURCE_PREFIXES)
