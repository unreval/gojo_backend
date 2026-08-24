"""relationship_boundary.py —— 感情判断系统 v4 · 边界处理

替代 v3 里"次数×固定倍率"的机械设计。用三维判定：
    known         = 角色是否已明确表态过这是底线？  yes/no/unclear
    intentional   = 用户是否清楚在踩这条线？        yes/no/unclear
    severity      = 单次事件本身的严重程度          low/medium/high

处理路径：
- known=no  → 首次触碰，按正常 F 处理，写"表态"到 declared_stance
- known=yes, intentional=yes → 大权重 F + 大幅 Trust 下降（"明知故犯"）
- known=yes, intentional=no  → 中等权重（"无意重复"）
- known=yes, respected=yes   → 即使这次仍算一次 F，Trust 反而 +（"听劝"是信任证据）

雷区 topic_id 由 canon_lock 里定义，本模块不管定义、只管累积和结算。
"""
from typing import Dict, Optional
from db import get_conn
from relationship_config import (
    BASE_DELTA, BOUNDARY_INTENT_WEIGHTS,
    BOUNDARY_RESPECTED_TRUST_BONUS,
)
from relationship_state import apply_friction, apply_trust, declare_stance


# ══════════════════════════════════════════════════════════════
# 严重度基础系数（叠在 base_delta 上）
# ══════════════════════════════════════════════════════════════
SEVERITY_MULTIPLIER = {
    'low':    1.0,
    'medium': 1.7,
    'high':   2.8,
}


def _get_boundary_record(user_id, character_id, topic_id) -> Optional[Dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT hit_count, known_by_user, last_severity,
                          last_intentional, last_hit_at, first_hit_at
                   FROM rel_boundary_hits
                   WHERE user_id = %s AND character_id = %s AND topic_id = %s''',
                (user_id, character_id, topic_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return None
    return {
        'hit_count': row[0], 'known_by_user': row[1],
        'last_severity': row[2], 'last_intentional': row[3],
        'last_hit_at': row[4], 'first_hit_at': row[5],
    }


def _upsert_boundary_record(
    user_id, character_id, topic_id,
    known_by_user: bool, severity: str, intentional: str,
):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO rel_boundary_hits
                   (user_id, character_id, topic_id,
                    hit_count, known_by_user, last_severity, last_intentional)
                   VALUES (%s, %s, %s, 1, %s, %s, %s)
                   ON CONFLICT (user_id, character_id, topic_id)
                   DO UPDATE SET
                       hit_count = rel_boundary_hits.hit_count + 1,
                       known_by_user = EXCLUDED.known_by_user OR rel_boundary_hits.known_by_user,
                       last_severity = EXCLUDED.last_severity,
                       last_intentional = EXCLUDED.last_intentional,
                       last_hit_at = CURRENT_TIMESTAMP''',
                (user_id, character_id, topic_id,
                 known_by_user, severity, intentional))
    conn.commit()
    cur.close()
    conn.close()


def handle_boundary_hit(
    user_id: str, character_id: str,
    topic_id: str,
    severity: str = 'medium',        # low / medium / high
    intentional: str = 'unclear',    # yes / no / unclear
    confidence: str = 'medium',
    evidence_ref: str = None,
) -> Dict:
    """处理一次雷区触碰。返回一个 dict 描述做了什么，供 engine 记录/生成参考。

    典型调用：
        handle_boundary_hit(
            user_id, character_id,
            topic_id='past_lover',
            severity='high',
            intentional='yes',   # 前面刚被明确说过"别问"，现在又问
        )
    """
    record = _get_boundary_record(user_id, character_id, topic_id)
    known_before = bool(record and record['known_by_user'])

    intent_weight = BOUNDARY_INTENT_WEIGHTS.get(
        ('yes' if known_before else 'no', intentional),
        1.0,
    )
    severity_mult = SEVERITY_MULTIPLIER.get(severity, 1.5)
    base = BASE_DELTA['boundary_violated']
    f_delta = base * intent_weight * severity_mult

    apply_friction(
        user_id, character_id,
        category='boundary_violation',
        delta=f_delta,
        signal_type='boundary_hit',
        confidence=confidence,
        rule=f'boundary/{intent_weight:.1f}x{severity_mult:.1f}',
        evidence_refs=[evidence_ref] if evidence_ref else [],
        note=f'topic={topic_id} known={known_before} intent={intentional} sev={severity}',
    )

    # known+intentional 组合下额外打 Trust
    if known_before and intentional == 'yes':
        trust_delta = -f_delta * 0.4
        apply_trust(
            user_id, character_id,
            delta=trust_delta,
            signal_type='boundary_hit_intentional',
            confidence=confidence,
            rule='trust_penalty_intentional_boundary',
            note=f'topic={topic_id}',
        )
    elif known_before and intentional == 'no':
        # 无意重复，只小幅打信任
        apply_trust(
            user_id, character_id,
            delta=-1.0,
            signal_type='boundary_hit_unintentional',
            confidence=confidence,
            rule='trust_penalty_unintentional_boundary',
            note=f'topic={topic_id}',
        )

    # 更新计数并标记 known（如果这次角色明确表态过，engine 会跟着调 mark_boundary_known）
    _upsert_boundary_record(
        user_id, character_id, topic_id,
        known_by_user=known_before,
        severity=severity, intentional=intentional,
    )

    return {
        'topic_id': topic_id,
        'known_before': known_before,
        'f_delta': f_delta,
        'first_time': record is None,
    }


def mark_boundary_known(user_id, character_id, topic_id: str,
                        stance_content: str = None):
    """角色明确表态"这是我的底线"后调用：
    - 将 boundary_hits.known_by_user 置为 true
    - 同时在 rel_declared_stance 里写一条 stance，作为未来判 known=yes 的证据来源
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_boundary_hits
                   SET known_by_user = TRUE
                   WHERE user_id = %s AND character_id = %s AND topic_id = %s''',
                (user_id, character_id, topic_id))
    conn.commit()
    cur.close()
    conn.close()

    if stance_content:
        declare_stance(
            user_id, character_id,
            stance_type='boundary_stated',
            content=stance_content,
            source_event_ref=f'boundary/{topic_id}',
        )


def handle_boundary_respected(user_id, character_id, topic_id: str,
                              confidence: str = 'medium'):
    """已知的雷区之后，用户主动收手/尊重：给 Trust 加一点。
    "她能听劝"本身是信任的证据。
    """
    apply_trust(
        user_id, character_id,
        delta=BOUNDARY_RESPECTED_TRUST_BONUS,
        signal_type='boundary_respected',
        confidence=confidence,
        rule='trust_bonus_boundary_respected',
        note=f'topic={topic_id}',
    )
