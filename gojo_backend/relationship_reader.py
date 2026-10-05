"""Read-only prompt construction for the relationship ledger and Cognition.

``derive_label`` remains a legacy/debug compatibility projection.  It is never
the authority for relationship nature: prompt-facing conclusions come from the
multi-dimensional relationship panel and its source-valid Cognitive items.
"""
from typing import Dict, List, Optional

from db import get_conn
from relationship_config import (
    FLIRT_RESPONSE_WINDOW_SIZE,
    PURSUE_WITHDRAW_WINDOW_SIZE,
    PURSUE_WITHDRAW_IMBALANCE_THRESHOLD,
)
from relationship_initiative import initiative_guidance
from relationship_panel import build_relationship_panel, format_relationship_panel
from relationship_state import load_offline_continuity_state


def build_state_summary(user_id: str, character_id: str, *, compact=False) -> str:
    """Render the canonical panel plus non-semantic ledger telemetry.

    This reader never initializes or mutates ``rel_state``.  The panel itself
    owns source selection; extra telemetry is explicitly not a relationship
    conclusion and cannot override a Cognitive value.
    """
    panel = build_relationship_panel(user_id, character_id)
    if compact:
        return format_relationship_panel(panel, compact=True)

    state = panel.get('ledger') or {}
    lines = [format_relationship_panel(panel)]
    lines.append('')
    lines.append('【账本遥测——数值观察，不是关系性质结论】')
    if state.get('has_state_row'):
        lines.extend([
            f'- 温度：{_scale_word(state.get("warmth", 0))}',
            f'- 亲密度：{_scale_word(state.get("intimacy", 0))}',
            f'- 信任：{_scale_word(state.get("trust", 0))}',
            f'- 互动张力（passion 遥测）：{_scale_word(state.get("passion", 0))}',
        ])
        total_f = sum(float(v) for v in (state.get('friction') or {}).values())
        if total_f > 0:
            top_categories = _top_friction_categories(state.get('friction') or {}, n=3)
            lines.append(
                f'- 摩擦记录：{_scale_word(total_f)}'
                + (f'，主要来自：{", ".join(top_categories)}' if top_categories else '')
            )
    else:
        lines.append('- 账本尚未积累：这不是陌生、拒绝或任何关系结论。')

    tone = compute_tone(user_id, character_id)
    flirt = compute_flirt_response(user_id, character_id)
    pursue_withdraw = compute_pursue_withdraw(user_id, character_id)
    temporal_note = _temporal_note(user_id, character_id)
    lines.extend(['', '【最近互动遥测——仅供表达节奏参考】'])
    lines.append(f'- 对话调性：{tone or "（数据不足）"}')
    if flirt.get('flirt_sample_count', 0) > 0:
        lines.append(f'- 关系推进回应：{flirt.get("desc") or "（数据不足）"}')
    if pursue_withdraw and pursue_withdraw.get('pattern'):
        lines.append(f'- 发起分布：{pursue_withdraw.get("desc")}（不等于关系方向或结论）')
    if temporal_note:
        lines.append(f'- 时间节奏：{temporal_note}')

    continuity = _continuity_context(user_id, character_id)
    if continuity:
        lines.extend(['', continuity])

    romantic_label = ((panel.get('romantic_label') or {}).get('value') or 'unresolved')
    extra = initiative_guidance(
        character_id,
        state,
        romantic_label=romantic_label,
    )
    lines.extend([
        '',
        '【表达边界】只表达 panel 已读取的范围；未决保持未决，'
        '不从账本阈值、旧标签或短期动作自行确认或否定浪漫性质。',
    ])
    if extra:
        lines.append(extra)
    return '\n'.join(lines)


def _continuity_context(user_id, character_id) -> str:
    """Scene continuity is deliberately separate from relationship authority."""
    try:
        state = load_offline_continuity_state(user_id, character_id)
    except Exception:
        return ''
    if not state:
        return ''
    labels = {
        'action': '上一动作',
        'intent': '短期意图',
        'moodshift': '情绪延续',
        'anchor': '场景锚点',
        'from': '上一回合来源',
    }
    lines = ['【回合连续性——只接动作/情绪/场景，不能作为关系证据】']
    for key in ('anchor', 'action', 'moodshift', 'intent', 'from'):
        value = state.get(key)
        if value:
            lines.append(f'- {labels[key]}：{value}')
    return '\n'.join(lines) if len(lines) > 1 else ''


def derive_label(state: Dict) -> Dict:
    """Return a coarse numeric compatibility projection, not a relationship label.

    The previous implementation translated ledger thresholds into durable
    statements such as confirmed love or absence of romantic feeling.  This
    compatibility function intentionally exposes only a diagnostic code.  No
    reader may use it to determine what the character feels or should say.
    """
    state = state or {}
    total_f = sum(float(v) for v in (state.get('friction') or {}).values())
    w = float(state.get('warmth') or 0)
    i = float(state.get('intimacy') or 0)
    trust = float(state.get('trust') or 0)
    attach = float(state.get('attachment') or 0)
    commitment = float(state.get('commitment') or 0)
    passion = float(state.get('passion') or 0)
    if not any((total_f, w, i, trust, attach, commitment, passion)):
        code = 'ledger_unassessed'
    elif total_f >= 10 and (w <= 5 or total_f >= max(w, 1) * 2):
        code = 'friction_dominant_ledger'
    elif passion >= 40 and trust >= 55 and commitment >= 45:
        code = 'high_passion_trust_commitment_ledger'
    elif attach >= 60 and commitment >= 60:
        code = 'high_attachment_commitment_ledger'
    elif i <= 5:
        code = 'low_interaction_history_ledger'
    else:
        code = 'mixed_ledger_signals'
    return {
        'primary': f'legacy:{code}',
        'code': code,
        'complex_note': '仅为兼容/调试数值投影，不构成关系性质。',
        'expression_guidance': '不用于表达或关系定性；以 relationship_panel 为准。',
    }


def _scale_word(v: float) -> str:
    if v <= 5:
        return '几乎没有'
    if v <= 20:
        return '很浅'
    if v <= 40:
        return '有一些'
    if v <= 60:
        return '中等'
    if v <= 80:
        return '相当深'
    return '非常深'


def _top_friction_categories(friction: Dict, n: int = 3) -> List[str]:
    if not friction:
        return []
    items = sorted(friction.items(), key=lambda item: -float(item[1]))
    return [key for key, value in items[:n] if float(value) > 1.0]


def _temporal_note(user_id, character_id) -> Optional[str]:
    try:
        from temporal_awareness import get_temporal_snapshot
        snap = get_temporal_snapshot(user_id, character_id)
    except Exception:
        return None
    if not snap or not snap.get('has_history'):
        return None
    elapsed = snap.get('elapsed_seconds_since_last_interaction')
    label = snap.get('elapsed_label') or '未知'
    bucket = snap.get('gap_bucket')
    if elapsed is None:
        return None
    if bucket in ('continuous', 'short_gap'):
        return f'最近仍是连续聊天节奏，上次互动约 {label} 前'
    if bucket == 'same_day_gap':
        return f'同一天隔了约 {label}，旧话题不是刚刚发生'
    if bucket == 'overnight':
        return f'中间隔了约 {label}，可能已跨过半天或一晚'
    if bucket == 'few_days':
        return f'她隔了约 {label} 又回来；注意到断档，但不要直接判成变冷'
    if bucket in ('long_gap', 'very_long_gap'):
        return f'已有明显断档（约 {label}）；可作为久别/等待语境，不能单独当感情结论'
    return f'上次互动约 {label} 前'


def compute_tone(user_id, character_id) -> Optional[str]:
    """Return a recent style observation, never a relationship verdict."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT tone_category, COUNT(*)
                   FROM (SELECT tone_category FROM rel_interaction_stats
                         WHERE user_id = %s AND character_id = %s
                           AND tone_category IS NOT NULL
                         ORDER BY timestamp DESC
                         LIMIT 30) sub
                   GROUP BY tone_category''',
                (user_id, character_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        return None
    total = sum(row[1] for row in rows)
    if total == 0:
        return None
    rows.sort(key=lambda row: -row[1])
    top = rows[0]
    if top[1] / total >= 0.5:
        mapping = {
            'banter': '互怼型（损友调性）',
            'support': '支持型（温和陪伴）',
            'care': '照顾型（关照-被关照）',
        }
        return mapping.get(top[0], top[0])
    return '混合调性'


def compute_flirt_response(user_id, character_id) -> Dict:
    """Count recorded response evidence in a bounded recent interaction window."""
    window = FLIRT_RESPONSE_WINDOW_SIZE
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT flirt_response
                   FROM (
                       SELECT flirt_response
                       FROM rel_interaction_stats
                       WHERE user_id = %s AND character_id = %s
                       ORDER BY timestamp DESC, id DESC
                       LIMIT %s
                   ) recent''',
                (user_id, character_id, window))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    counts = {'accepted': 0, 'held': 0, 'rejected': 0, 'mixed': 0}
    for (value,) in rows:
        if value in counts:
            counts[value] += 1
    sample = sum(counts.values())
    return {
        'accepted': counts['accepted'],
        'held': counts['held'],
        'rejected': counts['rejected'],
        'mixed': counts['mixed'],
        'total_recent_turns': len(rows),
        'flirt_sample_count': sample,
        'window': window,
        'desc': _format_flirt_response_desc(counts, sample, window),
    }


def _format_flirt_response_desc(counts, sample, window) -> str:
    if sample <= 0:
        return '最近没有足够的相关互动'
    parts = [
        f'接受 {counts["accepted"]}',
        f'保留 {counts["held"]}',
        f'拒绝 {counts["rejected"]}',
    ]
    if counts['mixed']:
        parts.append(f'混合 {counts["mixed"]}')
    return f'最近 {window} 轮已记录互动中有 {sample} 次相关回应（{" / ".join(parts)}）'


def compute_pursue_withdraw(user_id, character_id) -> Dict:
    """Describe recent initiation counts without deciding relationship direction."""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT direction, is_initiator, COUNT(*)
                   FROM (SELECT direction, is_initiator FROM rel_interaction_stats
                         WHERE user_id = %s AND character_id = %s
                         ORDER BY timestamp DESC
                         LIMIT %s) sub
                   GROUP BY direction, is_initiator''',
                (user_id, character_id, PURSUE_WITHDRAW_WINDOW_SIZE))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    user_init = sum(count for direction, initiated, count in rows
                    if direction == 'user' and initiated)
    char_init = sum(count for direction, initiated, count in rows
                    if direction == 'character' and initiated)
    total = user_init + char_init
    if total < 5:
        return {'pattern': None}
    user_ratio = user_init / total
    if user_ratio >= PURSUE_WITHDRAW_IMBALANCE_THRESHOLD:
        return {
            'pattern': 'user_more_initiative',
            'desc': '记录中她发起的次数明显偏多，仅供回复节奏参考',
        }
    if user_ratio <= 1 - PURSUE_WITHDRAW_IMBALANCE_THRESHOLD:
        return {
            'pattern': 'character_more_initiative',
            'desc': '记录中我发起的次数明显偏多，仅供回复节奏参考',
        }
    return {'pattern': None}
