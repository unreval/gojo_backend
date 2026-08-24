"""relationship_repair.py —— 感情判断系统 v4 · 冲突修复处理

用三信号判定修复质量，替代"道歉=修复"的机械判断：
    acknowledgment    是否承认错误
    responsibility    是否担责（不甩锅、不推给情绪/环境）
    corrective_action 是否包含具体改正承诺或行动

按加权得分算 quality_tier，各档对 F/Trust 有不同影响：
    high   → F 大幅消解 + Trust 加成（"高质量修复后关系反而更牢"）
    medium → F 部分下降 + Trust 小幅加成
    low    → F 维持甚至略增，Trust 小幅减

三信号本身由 Observer LLM #1 提取，本模块只做纯代码结算。
"""
from typing import Dict, Optional
from db import get_conn
from relationship_config import (
    REPAIR_SIGNAL_WEIGHTS,
    REPAIR_QUALITY_HIGH_MIN, REPAIR_QUALITY_MEDIUM_MIN,
    REPAIR_EFFECT,
)
from relationship_state import apply_friction, apply_trust, load_state


def _score_repair(ack: bool, resp: bool, action: bool) -> float:
    return (
        (REPAIR_SIGNAL_WEIGHTS['acknowledgment']    if ack    else 0.0) +
        (REPAIR_SIGNAL_WEIGHTS['responsibility']    if resp   else 0.0) +
        (REPAIR_SIGNAL_WEIGHTS['corrective_action'] if action else 0.0)
    )


def _tier(score: float) -> str:
    if score >= REPAIR_QUALITY_HIGH_MIN:
        return 'high'
    if score >= REPAIR_QUALITY_MEDIUM_MIN:
        return 'medium'
    return 'low'


def handle_repair_attempt(
    user_id: str, character_id: str,
    acknowledgment: bool,
    responsibility: bool,
    corrective_action: bool,
    conflict_ref: Optional[str] = None,
    confidence: str = 'medium',
) -> Dict:
    """处理一次修复尝试。

    做三件事：
    1. 按三信号算 quality_tier
    2. 按 tier 结算 F（按当前 F 总额比例削减，非绝对值）+ Trust delta
    3. 写 rel_repair_log

    Returns:
        dict 描述本次修复结果，供 engine 记录/生成参考
    """
    score = _score_repair(acknowledgment, responsibility, corrective_action)
    tier = _tier(score)
    effect = REPAIR_EFFECT[tier]

    # 按当前 F 总额比例削减（不是死数字，避免 F 很低时减成负数）
    state = load_state(user_id, character_id)
    total_f = sum(float(v) for v in (state['friction'] or {}).values())

    # 只削减 conflict 相关的 friction 类目；简化起见按比例整体缩：
    # 更精细的做法是根据 conflict_ref 找到对应 category 单独缩
    f_reduce_ratio = effect['f_reduce']
    trust_bonus = effect['trust_bonus']

    if f_reduce_ratio > 0 and total_f > 0:
        # 对每个 category 按比例缩
        friction = dict(state['friction'])
        for category, val in list(friction.items()):
            reduction = float(val) * f_reduce_ratio
            if reduction > 0.01:
                apply_friction(
                    user_id, character_id,
                    category=category,
                    delta=-reduction,
                    signal_type='repair_attempt',
                    confidence=confidence,
                    rule=f'repair_{tier}_f_reduce',
                    evidence_refs=[conflict_ref] if conflict_ref else [],
                    note=f'ack={acknowledgment} resp={responsibility} action={corrective_action}',
                )

    if trust_bonus != 0:
        apply_trust(
            user_id, character_id,
            delta=trust_bonus,
            signal_type='repair_attempt',
            confidence=confidence,
            rule=f'repair_{tier}_trust',
            evidence_refs=[conflict_ref] if conflict_ref else [],
            note=f'quality_tier={tier} score={score:.2f}',
        )

    # 写 repair_log
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO rel_repair_log
                   (user_id, character_id, conflict_ref,
                    acknowledgment, responsibility, corrective_action,
                    quality_score, quality_tier,
                    f_reduce_ratio, trust_delta, confidence)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                (user_id, character_id, conflict_ref,
                 acknowledgment, responsibility, corrective_action,
                 score, tier,
                 f_reduce_ratio, trust_bonus, confidence))
    conn.commit()
    cur.close()
    conn.close()

    return {
        'quality_score': score,
        'quality_tier': tier,
        'f_reduce_ratio': f_reduce_ratio,
        'trust_delta': trust_bonus,
    }
