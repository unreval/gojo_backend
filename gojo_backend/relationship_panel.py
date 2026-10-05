"""Read-only relationship authority projection.

The panel combines existing canonical sources without turning their values into
a new verdict.  Numeric ledger fields remain measurements; semantic meaning is
read only from declared stances and source-valid Cognitive items.
"""
from relationship_state import read_state, list_declared_stances
from cognitive_reader import read_relationship_semantic_state
from relationship_semantics import normalize_romantic_label
from role_view import render_role_view


def _legacy_projection(state):
    """Keep the former reader label available only as a compatibility field."""
    try:
        # Imported lazily to avoid a module cycle: relationship_reader formats
        # this panel, but the legacy calculation is not an authority input.
        from relationship_reader import derive_label
        value = derive_label(state)
    except Exception:
        value = {}
    return {
        'status': 'legacy_projection',
        'value': str(value.get('primary') or 'legacy_unavailable'),
        'code': str(value.get('code') or 'legacy_unavailable'),
        'note': '兼容/调试数值投影，不能覆盖认知结论。',
    }


def _ledger_dimension(state, name):
    observed = bool((state or {}).get('has_state_row'))
    return {
        'status': 'observed' if observed else 'unassessed',
        'value': float((state or {}).get(name) or 0) if observed else None,
        'source': 'rel_state',
    }


def _stance_rows(stances, kind, observer_id):
    return [
        {
            'id': row.get('id'),
            'type': row.get('type'),
            'content': render_role_view(
                row.get('content'), observer_id=observer_id,
                source_character_id=observer_id),
            'status': row.get('status') or 'active',
            'declared_at': row.get('declared_at'),
            'source_event_ref': row.get('source_event_ref'),
        }
        for row in (stances or [])
        if kind is None or row.get('type') in kind
    ]


def _stance_dimension(rows):
    active = [row for row in rows if row.get('status') == 'active']
    historical = [row for row in rows if row.get('status') != 'active']
    return {
        'status': 'active' if active else ('historical' if historical else 'unassessed'),
        'active': active,
        'historical': historical,
        'source': 'rel_declared_stance',
    }


def _cognitive_dimension(item, observer_id, *, default_status='unassessed'):
    if not isinstance(item, dict):
        return {
            'status': default_status,
            'value': default_status,
            'source': 'cognitive',
        }
    value = item.get('statement') or item.get('content') or item.get('value')
    if isinstance(value, str):
        value = render_role_view(
            value, observer_id=observer_id, source_character_id=observer_id)
    return {
        'status': item.get('kind') or 'cognitive',
        'value': value,
        'confidence': item.get('confidence'),
        'source_event_ids': tuple(item.get('source_event_ids') or ()),
        'updated_at': item.get('updated_at'),
        'source': 'cognitive',
    }


def build_relationship_panel(user_id, character_id):
    """Build a read-only, multi-dimensional view of relationship inputs.

    No field is inferred from another.  In particular, high attachment,
    commitment, or interaction tension cannot alter the Cognitive romantic
    label; a missing label is always ``unresolved`` rather than a negative.
    """
    state = read_state(user_id, character_id)
    stances = list_declared_stances(user_id, character_id, include_inactive=True)
    cognition = read_relationship_semantic_state(user_id, character_id)
    cognition = cognition if isinstance(cognition, dict) else {}

    engagement = _cognitive_dimension(cognition.get('engagement_style'), character_id)
    openness = _cognitive_dimension(cognition.get('romantic_openness'), character_id)
    conflict_item = cognition.get('internal_conflict')
    if isinstance(conflict_item, dict):
        internal_conflict = {
            **_cognitive_dimension(conflict_item, character_id),
            'status': 'present',
        }
    else:
        internal_conflict = {
            'status': 'unassessed', 'value': 'unassessed', 'source': 'cognitive',
        }

    label_item = cognition.get('romantic_label')
    label_value = normalize_romantic_label(
        label_item.get('value') if isinstance(label_item, dict) else None,
    ) or 'unresolved'
    romantic_label = {
        'status': 'cognitive' if isinstance(label_item, dict) else 'unresolved',
        'value': label_value,
        'content': (
            render_role_view(
                label_item.get('content'), observer_id=character_id,
                source_character_id=character_id) if isinstance(label_item, dict) else
            '缺少已形成的窄结论；保持未决。'
        ),
        'source_event_ids': tuple(
            label_item.get('source_event_ids') or ()
        ) if isinstance(label_item, dict) else (),
        'updated_at': label_item.get('updated_at') if isinstance(label_item, dict) else None,
        'source': 'cognitive',
    }

    care_rows = _stance_rows(stances, {'care_admission'}, character_id)
    boundary_rows = _stance_rows(stances, {'retreat_boundary', 'boundary_stated'}, character_id)
    all_stance_rows = _stance_rows(stances, None, character_id)
    return {
        'attachment': _ledger_dimension(state, 'attachment'),
        'commitment': _ledger_dimension(state, 'commitment'),
        'care': _stance_dimension(care_rows),
        'boundary': _stance_dimension(boundary_rows),
        'declared_stances': _stance_dimension(all_stance_rows),
        'engagement_style': engagement,
        'romantic_label': romantic_label,
        'romantic_openness': openness,
        'internal_conflict': internal_conflict,
        # Direction has no score-derived fallback.  It remains unassessed until
        # a future Cognitive semantic key is explicitly introduced.
        'direction': {
            'status': 'unassessed', 'value': 'unassessed', 'source': 'cognitive',
        },
        'legacy_label': _legacy_projection(state),
        'ledger': state,
    }


def _format_stances(value):
    rows = list(value.get('active') or []) + list(value.get('historical') or [])
    if not rows:
        return 'unassessed'
    return '；'.join(
        f'[{row.get("status") or "active"}/{row.get("type")}] {row.get("content")}'
        for row in rows[:3]
    )


def _format_cognitive(value):
    status = value.get('status') or 'unassessed'
    text = value.get('value')
    if text in (None, ''):
        text = 'unassessed'
    return f'{status}；{text}'


def format_relationship_panel(panel, *, compact=False):
    """Render the panel for prompts without adding any new semantic judgment."""
    panel = panel or {}
    attachment = panel.get('attachment') or {}
    commitment = panel.get('commitment') or {}
    label = panel.get('romantic_label') or {}
    lines = [
        '【关系面板——只读组合，不现场推断】',
        'rel_state 是数值账本；Cognitive 才拥有语义结论。缺数据保持 unassessed/unresolved。',
        f'attachment：{attachment.get("status", "unassessed")}；{attachment.get("value")}',
        f'commitment：{commitment.get("status", "unassessed")}；{commitment.get("value")}',
        f'engagement_style：{_format_cognitive(panel.get("engagement_style") or {})}',
        f'romantic_label：{label.get("value") or "unresolved"}',
        f'romantic_openness：{_format_cognitive(panel.get("romantic_openness") or {})}',
        f'care：{_format_stances(panel.get("care") or {})}',
        f'boundary：{_format_stances(panel.get("boundary") or {})}',
        f'declared_stances：{_format_stances(panel.get("declared_stances") or {})}',
        f'internal_conflict：{_format_cognitive(panel.get("internal_conflict") or {})}',
        f'direction：{_format_cognitive(panel.get("direction") or {})}',
    ]
    legacy = panel.get('legacy_label') or {}
    lines.append(
        'legacy_label（兼容/调试，不能覆盖认知结论）：'
        f'{legacy.get("value") or "legacy_unavailable"}'
    )
    if not compact:
        lines.extend([
            '表达边界：romantic_label=unresolved 不是否定；不得由 attachment、commitment、'
            'passion、承诺、调情或任何旧标签替它做确认。',
            '宽泛的旧 relationship frame 也不能替代 romantic_label 的窄结论；'
            '只有同一 semantic key 的来源有效 Cognitive 项可以改变它。',
            'internal_conflict 仅在 Slow Loop 已有对应 hypothesis/belief 时显示 present。',
        ])
    return '\n'.join(lines)
