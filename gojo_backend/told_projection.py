"""A told is a scoped view of an adjudicated user fact, not another fact row."""

from role_view import PROJECTION_VERSION, render_role_view


def canonical_told_view(row, user_id, observer_id):
    """Project a reader-validated long_memory row for its permitted audience."""
    semantic = row.get('semantic_payload')
    scope = row.get('source_character_id')
    if row.get('projection_version') != PROJECTION_VERSION or not isinstance(semantic, dict):
        return None
    if (semantic.get('subject_ref') != f'user:{user_id}'
            or semantic.get('source_scope') != scope
            or not semantic.get('source_event_id')
            or scope not in (observer_id, 'shared')):
        return None
    audience = semantic.get('audience')
    if audience is not None and (not isinstance(audience, (list, tuple))
                                 or observer_id not in audience):
        return None
    content = render_role_view(row.get('content'), observer_id=observer_id,
                               source_character_id=scope, semantic=semantic)
    if not content:
        return None
    return dict(row, content=content, source_table='long_memory',
                source_scope=scope, audience=observer_id)


def told_display(content):
    """Label the provenance of a user report without asserting its truth."""
    content = str(content or '').strip()
    return f'她告诉过我：{content}' if content else ''
