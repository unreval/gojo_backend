"""Compatibility ingress into the single canonical evidence policy.

No observer, signal router, inferred deltas, or mutable legacy brain lives here.
The historical result shape is retained for chat callers.
"""
from typing import Dict, List, Optional

from relationship_semantics import is_nonrelationship_generated_source


def _empty_turn_result(**overrides):
    result = {
        'signals_extracted': 0,
        'signals_applied': 0,
        'skipped': None,
        'observer_error': None,
        'cognitive_ingress': None,
        'cognitive_ingress_error': None,
        'applied': [],
    }
    result.update(overrides)
    return result


def process_turn(
    user_id: str,
    character_id: str,
    user_message: str,
    character_reply: Optional[str] = None,
    character_core_snippet: Optional[str] = None,
    recent_context: Optional[List[Dict]] = None,
    temporal_context: Optional[Dict] = None,
    session_id: Optional[str] = None,
    signal_model: Optional[str] = None,
    source_event_id: Optional[str] = None,
) -> Dict:
    """Submit canonical evidence; model-labelled relationship writers are removed.

    Keep the existing public entry and result shape. The cognitive policy owns
    judgments; a second observer must not restore model authority upstream.
    """
    if is_nonrelationship_generated_source(source_event_id):
        return _empty_turn_result(skipped='nonrelationship_generated_source')
    from cognitive_events import ingest_canonical_turn
    result = ingest_canonical_turn(user_id=user_id, character_id=character_id,
                                   source_event_id=source_event_id)
    return _empty_turn_result(cognitive_ingress=result,
                              skipped='deterministic_cognitive_policy')
