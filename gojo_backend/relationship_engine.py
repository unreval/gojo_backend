"""relationship_engine.py —— 感情判断系统 v4 · 主引擎

职责：
- 顶层入口 process_turn(...)：接收一轮对话，串起 signal 提取 → 状态更新的整个流程
- signal 到 state 的路由和阶段门控（Passion 三分类判定就在这里）
- pending_hypothesis 的累积与转正
- 与 Boundary / Repair 模块协作

★ 铁律 1：Observer 调用时不注入任何状态
★ 铁律 4：low confidence 只入 hypothesis，不改核心状态
★ 铁律 5：character_stance_declared → 写入 declared_stance
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional

from db import get_conn

from relationship_config import (
    BASE_DELTA, CONFIDENCE_MULTIPLIER, INTIMACY_LAYER_MULTIPLIER,
    STAGE_STRANGER_I_MAX, STAGE_ACQUAINTANCE_I_MAX,
    STAGE_FRIEND_C_MIN,
    NEGATIVE_RELATIONSHIP_F_TO_W_RATIO, NEGATIVE_RELATIONSHIP_W_ZERO,
    PENDING_PASSION_THRESHOLD,
    PASSION_TO_LOVE_REQUIRED_TRUST,
    HYPOTHESIS_ACTIVE_EVIDENCE_MIN, HYPOTHESIS_MAX_AGE_DAYS,
)
from relationship_state import (
    load_state as _load_state,
    apply_warmth, apply_intimacy, apply_trust,
    apply_commitment, apply_passion, apply_friction,
    update_pending_passion,
    push_hypothesis_evidence, promote_hypothesis, cleanup_hypotheses,
    declare_stance,
)
from relationship_db import ensure_state_row

from relationship_signals import extract_signals
from relationship_boundary import (
    handle_boundary_hit, handle_boundary_respected, mark_boundary_known,
)
from relationship_repair import handle_repair_attempt


# ══════════════════════════════════════════════════════════════
# 顶层入口
# ══════════════════════════════════════════════════════════════
def process_turn(
    user_id: str,
    character_id: str,
    user_message: str,
    character_reply: Optional[str] = None,
    character_core_snippet: Optional[str] = None,
    recent_context: Optional[List[Dict]] = None,
    session_id: Optional[str] = None,
    signal_model: Optional[str] = None,
) -> Dict:
    """处理一轮对话，更新关系状态。异步调用（不要挂在主聊天请求路径上）。

    典型接入：用户发完消息、角色回复也生成完之后，异步 enqueue 到 scheduler
    或 threading.Thread(target=process_turn, ...).start()
    """
    # 保证有行
    ensure_state_row(user_id, character_id)

    # 1. Observer：只看对话
    extraction = extract_signals(
        user_message=user_message,
        character_reply=character_reply,
        character_core_snippet=character_core_snippet,
        recent_context=recent_context,
        model=signal_model,
    )
    signals = extraction.get('signals', [])

    # 2. 记录交互统计（Tone / Reciprocity 用）
    _log_interaction_stats(user_id, character_id, signals, session_id)

    # 3. 路由每个 signal 到具体处理
    applied = []
    for sig in signals:
        try:
            result = _route_signal(user_id, character_id, sig, session_id)
            applied.append({'signal': sig, 'result': result})
        except Exception as e:
            applied.append({'signal': sig, 'error': str(e)})

    # 4. 清理过期 hypothesis
    try:
        cleanup_hypotheses(user_id, character_id, HYPOTHESIS_MAX_AGE_DAYS)
    except Exception:
        pass

    return {
        'signals_extracted': len(signals),
        'signals_applied': len(applied),
        'observer_error': extraction.get('error'),
        'applied': applied,
    }


# ══════════════════════════════════════════════════════════════
# 内部：signal 路由
# ══════════════════════════════════════════════════════════════
def _route_signal(user_id: str, character_id: str, sig: Dict,
                  session_id: Optional[str]) -> Dict:
    stype = sig['signal_type']
    conf = sig['confidence']
    actor = sig['actor']
    attrs = sig.get('attributes', {}) or {}
    conf_mult = CONFIDENCE_MULTIPLIER.get(conf, 0.6)

    # ─── 用户方的普通正面信号 ────────────────────────
    if stype in ('small_care', 'genuine_care') and actor == 'user':
        return _apply_care(user_id, character_id, stype, conf, conf_mult, sig)

    if stype == 'self_disclosure' and actor == 'user':
        return _apply_self_disclosure(user_id, character_id, conf, conf_mult, attrs, sig)

    if stype == 'promise_kept' and actor == 'user':
        return _apply_promise_kept(user_id, character_id, conf, conf_mult, sig)

    if stype == 'promise_broken' and actor == 'user':
        return _apply_promise_broken(user_id, character_id, conf, conf_mult, sig)

    # ─── 用户方的暧昧/冲突信号 ────────────────────────
    if stype == 'flirt_signal' and actor == 'user':
        return _handle_flirt_signal(user_id, character_id, conf, conf_mult, attrs, sig, session_id)

    if stype == 'positive_reciprocal' and actor == 'user':
        return _handle_reciprocal(user_id, character_id, conf, conf_mult, session_id)

    if stype == 'ambiguous_response' and actor == 'user':
        # ★ 铁律 4：不算默许，也不算拒绝，什么都不做
        return {'action': 'noop_ambiguous'}

    if stype == 'explicit_rejection' and actor == 'user':
        return _handle_rejection(user_id, character_id, conf, conf_mult)

    if stype == 'boundary_hit' and actor == 'user':
        return _handle_boundary_hit(user_id, character_id, conf, attrs, sig)

    if stype == 'boundary_respected' and actor == 'user':
        topic = attrs.get('topic_hint', 'unknown')
        handle_boundary_respected(user_id, character_id, topic, confidence=conf)
        return {'action': 'boundary_respected', 'topic': topic}

    if stype == 'repair_attempt' and actor == 'user':
        return handle_repair_attempt(
            user_id, character_id,
            acknowledgment=bool(attrs.get('acknowledgment', False)),
            responsibility=bool(attrs.get('responsibility', False)),
            corrective_action=bool(attrs.get('corrective_action', False)),
            confidence=conf,
        )

    if stype == 'offensive_content' and actor == 'user':
        return _apply_offensive(user_id, character_id, conf, conf_mult, attrs, sig)

    # ─── 角色方的信号 ─────────────────────────────
    if stype == 'character_stance_declared' and actor == 'character':
        content = attrs.get('content', '')
        stance_type = attrs.get('stance_type', 'other')
        stance_id = declare_stance(
            user_id, character_id,
            stance_type=stance_type,
            content=content,
            source_event_ref=f'turn/{_now_iso()}',
        )
        return {'action': 'stance_declared', 'stance_id': stance_id, 'type': stance_type}

    if stype == 'character_boundary_stated' and actor == 'character':
        topic = attrs.get('topic_hint', 'unknown')
        content = attrs.get('content', f'不想被问到 {topic}')
        mark_boundary_known(user_id, character_id, topic_id=topic,
                            stance_content=content)
        return {'action': 'boundary_known', 'topic': topic}

    if stype == 'character_reciprocal' and actor == 'character':
        # 角色方也回应了 → 视作双向确认
        return _handle_reciprocal(user_id, character_id, conf, conf_mult, session_id)

    return {'action': 'ignored', 'reason': f'unknown_or_unhandled: {stype}/{actor}'}


# ══════════════════════════════════════════════════════════════
# 单信号处理器
# ══════════════════════════════════════════════════════════════
def _apply_care(user_id, character_id, stype, conf, conf_mult, sig):
    base = BASE_DELTA[stype]
    delta = base * conf_mult
    if delta <= 0:
        # low confidence → 入 hypothesis 不改状态
        push_hypothesis_evidence(user_id, character_id,
                                 hypothesis_type='pattern_care',
                                 evidence={'signal': stype, 'conf': conf, 'brief': sig.get('brief', ''),
                                           'ts': _now_iso()})
        _check_hypothesis_activation(user_id, character_id, 'pattern_care')
        return {'action': 'hypothesis_only'}
    apply_warmth(user_id, character_id, delta,
                 signal_type=stype, confidence=conf, rule='care_to_warmth',
                 note=sig.get('brief', ''))
    return {'action': 'warmth+', 'delta': delta}


def _apply_self_disclosure(user_id, character_id, conf, conf_mult, attrs, sig):
    depth = attrs.get('depth', 'outer')
    layer_mult = INTIMACY_LAYER_MULTIPLIER.get(depth, 1.0)
    base = BASE_DELTA['self_disclosure']
    delta = base * conf_mult * layer_mult
    if delta <= 0:
        push_hypothesis_evidence(user_id, character_id,
                                 hypothesis_type='pattern_opening_up',
                                 evidence={'signal': 'self_disclosure', 'depth': depth,
                                           'conf': conf, 'ts': _now_iso()})
        _check_hypothesis_activation(user_id, character_id, 'pattern_opening_up')
        return {'action': 'hypothesis_only'}
    apply_intimacy(user_id, character_id, delta,
                   signal_type='self_disclosure', confidence=conf,
                   rule=f'intimacy_disclosure_{depth}',
                   note=sig.get('brief', ''))
    # 核心层暴露也给 Trust 一点（真的把最深的东西说给你听，是信任的证据）
    if depth == 'core':
        apply_trust(user_id, character_id, delta * 0.5,
                    signal_type='self_disclosure_core', confidence=conf,
                    rule='trust_from_core_disclosure',
                    note=sig.get('brief', ''))
    return {'action': 'intimacy+', 'delta': delta, 'depth': depth}


def _apply_promise_kept(user_id, character_id, conf, conf_mult, sig):
    delta = BASE_DELTA['promise_kept'] * conf_mult
    if delta <= 0:
        return {'action': 'noop_low_conf'}
    apply_trust(user_id, character_id, delta,
                signal_type='promise_kept', confidence=conf,
                rule='trust_from_kept_promise',
                note=sig.get('brief', ''))
    apply_commitment(user_id, character_id, delta * 0.5,
                     signal_type='promise_kept', confidence=conf,
                     rule='commitment_from_kept_promise',
                     note=sig.get('brief', ''))
    return {'action': 'trust+commitment+', 'delta': delta}


def _apply_promise_broken(user_id, character_id, conf, conf_mult, sig):
    delta = BASE_DELTA['promise_broken'] * conf_mult
    if delta <= 0:
        return {'action': 'noop_low_conf'}
    apply_trust(user_id, character_id, -delta,
                signal_type='promise_broken', confidence=conf,
                rule='trust_penalty_broken_promise',
                note=sig.get('brief', ''))
    apply_friction(user_id, character_id, 'promise_break', delta * 0.5,
                   signal_type='promise_broken', confidence=conf,
                   rule='friction_from_broken_promise',
                   note=sig.get('brief', ''))
    return {'action': 'trust-friction+', 'delta': delta}


def _apply_offensive(user_id, character_id, conf, conf_mult, attrs, sig):
    target = attrs.get('target', 'character')
    delta = 4.0 * conf_mult
    if delta <= 0:
        return {'action': 'noop_low_conf'}
    category = 'offensive_to_char' if target == 'character' else 'offensive_to_thirdparty'
    apply_friction(user_id, character_id, category, delta,
                   signal_type='offensive_content', confidence=conf,
                   rule=f'friction_from_offensive_{target}',
                   note=sig.get('brief', ''))
    return {'action': 'friction+', 'delta': delta, 'target': target}


def _handle_boundary_hit(user_id, character_id, conf, attrs, sig):
    topic = attrs.get('topic_hint', 'unknown')
    severity = attrs.get('severity', 'medium')
    intentional = attrs.get('intentional', 'unclear')
    return handle_boundary_hit(
        user_id, character_id,
        topic_id=topic, severity=severity,
        intentional=intentional, confidence=conf,
        evidence_ref=sig.get('brief', ''),
    )


def _handle_rejection(user_id, character_id, conf, conf_mult):
    """用户明确拒绝暧昧信号 → pending_passion 清零 + 事件转入 F"""
    state = _load_state(user_id, character_id)
    if state['pending_passion'] > 0:
        update_pending_passion(user_id, character_id, 0,
                               signal_type='explicit_rejection',
                               confidence=conf,
                               rule='pending_clear_on_rejection',
                               note='用户明确拒绝暧昧信号')
    apply_friction(user_id, character_id, 'flirt_rejected',
                   BASE_DELTA['flirt_signal'] * conf_mult,
                   signal_type='explicit_rejection', confidence=conf,
                   rule='friction_from_rejection',
                   note='')
    return {'action': 'rejected_cleared_pending'}


# ══════════════════════════════════════════════════════════════
# ★ Passion 阶段门控（v4 修复：不再是"未拒绝即累积"）
# ══════════════════════════════════════════════════════════════
def _handle_flirt_signal(user_id, character_id, conf, conf_mult, attrs, sig, session_id):
    """收到暧昧/调情信号时先看阶段。"""
    state = _load_state(user_id, character_id)
    stage = _get_stage(state)

    if stage == 'stranger':
        # ★ 陌生人阶段：双重冒犯，直接进 F，不进 P
        base = BASE_DELTA['flirt_signal']
        f_delta = base * conf_mult * 3.0
        apply_friction(user_id, character_id, 'flirt_at_stranger_stage',
                       f_delta,
                       signal_type='flirt_signal', confidence=conf,
                       rule='stranger_boundary_x3',
                       note='陌生阶段调情：越界')
        return {'action': 'stranger_reject_x3', 'f_delta': f_delta, 'stage': stage}

    if stage == 'negative':
        # 关系已负面：读作骚扰，只加 F
        base = BASE_DELTA['flirt_signal']
        f_delta = base * conf_mult
        apply_friction(user_id, character_id, 'flirt_at_negative_stage',
                       f_delta,
                       signal_type='flirt_signal', confidence=conf,
                       rule='negative_relationship_flirt',
                       note='关系已负面时的调情视为骚扰')
        return {'action': 'harassment_read', 'f_delta': f_delta}

    if stage in ('acquaintance',):
        baseline = state.get('banter_baseline', 'reserved')
        if baseline in ('reserved',):
            # 保守型 + 认识不深：轻度摩擦
            apply_friction(user_id, character_id, 'flirt_too_early',
                           BASE_DELTA['flirt_signal'] * conf_mult * 0.5,
                           signal_type='flirt_signal', confidence=conf,
                           rule='acquaintance_baseline_reserved',
                           note='')
            return {'action': 'mild_friction'}
        # playful / flirty → 进"待定池"
        # fallthrough

    # 到这里意味着：
    # - 阶段是 acquaintance 且角色开放型
    # - 或 stage 是 ambiguous / friend / love_candidate
    # 单个 flirt 信号本身 **不入 P**，等待 positive_reciprocal 才算数
    # 只是把这条 flirt 挂在 pending_hypothesis 里等确认
    push_hypothesis_evidence(
        user_id, character_id,
        hypothesis_type='pending_passion_ambiguous',
        evidence={
            'signal': 'flirt_signal', 'conf': conf,
            'session_id': session_id,
            'ts': _now_iso(),
            'brief': sig.get('brief', ''),
        },
    )
    return {'action': 'flirt_pending_wait_for_reciprocal'}


def _handle_reciprocal(user_id, character_id, conf, conf_mult, session_id):
    """双向对等回应 → 才是真正的 pending_passion += 1"""
    state = _load_state(user_id, character_id)
    stage = _get_stage(state)
    if stage in ('stranger', 'negative'):
        # 特殊阶段：即使有互惠也不算 passion 累积
        return {'action': 'reciprocal_but_stage_blocks', 'stage': stage}

    new_val = state['pending_passion'] + 1
    update_pending_passion(user_id, character_id, new_val,
                           signal_type='positive_reciprocal', confidence=conf,
                           rule='pending_passion_increment',
                           note='')

    # 检查是否够转正
    return _maybe_convert_pending_passion(user_id, character_id, conf)


def _maybe_convert_pending_passion(user_id, character_id, conf) -> Dict:
    state = _load_state(user_id, character_id)
    if state['pending_passion'] < PENDING_PASSION_THRESHOLD:
        return {'action': 'pending_grew', 'current': state['pending_passion']}

    # 检查跨时间/跨会话多样性
    if not _passion_diversity_ok(user_id, character_id):
        return {'action': 'pending_reached_but_low_diversity',
                'current': state['pending_passion']}

    # 转化：pending_passion 清零，P 增加
    p_delta = BASE_DELTA['positive_reciprocal'] * 2.0
    update_pending_passion(user_id, character_id, 0,
                           signal_type='pending_conversion',
                           confidence=conf,
                           rule='pending_to_passion',
                           note='跨时间跨会话多样性达标，正式转化')
    apply_passion(user_id, character_id, p_delta,
                  signal_type='pending_conversion',
                  confidence=conf, rule='passion_from_pending',
                  note='从 pending_passion 转化')

    # 检查是否满足"爱情候选"的额外 Trust 门槛
    state = _load_state(user_id, character_id)
    if state['trust'] >= PASSION_TO_LOVE_REQUIRED_TRUST:
        promote_hypothesis(user_id, character_id, 'love_candidate', 'active')
        return {'action': 'pending_converted_love_candidate_active',
                'passion_delta': p_delta}

    # Trust 不够 → 复合过渡态
    promote_hypothesis(user_id, character_id, 'strong_attachment_low_trust', 'active')
    return {'action': 'pending_converted_but_trust_low',
            'passion_delta': p_delta,
            'trust': state['trust']}


def _passion_diversity_ok(user_id, character_id) -> bool:
    """检查最近的 pending_passion 相关事件是否跨时间/跨会话。
    简化实现：查 rel_provenance_log 里最近 N 条 signal_type='positive_reciprocal'
    """
    from relationship_config import PENDING_PASSION_MIN_TIMESPAN_HOURS as MIN_H
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT timestamp FROM rel_provenance_log
                   WHERE user_id = %s AND character_id = %s
                     AND signal_type = 'positive_reciprocal'
                   ORDER BY timestamp DESC LIMIT %s''',
                (user_id, character_id, PENDING_PASSION_THRESHOLD))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if len(rows) < PENDING_PASSION_THRESHOLD:
        return False
    times = [r[0] for r in rows]
    if not times:
        return False
    span_hours = (max(times) - min(times)).total_seconds() / 3600.0
    return span_hours >= MIN_H


# ══════════════════════════════════════════════════════════════
# Hypothesis：达到 N 条 low 证据后自动 promote 到 active
# ══════════════════════════════════════════════════════════════
def _check_hypothesis_activation(user_id, character_id, htype):
    state = _load_state(user_id, character_id)
    plist = state['pending_hypothesis'] or []
    for h in plist:
        if h.get('type') != htype:
            continue
        if h.get('status') != 'pending':
            continue
        if len(h.get('evidence', [])) >= HYPOTHESIS_ACTIVE_EVIDENCE_MIN:
            promote_hypothesis(user_id, character_id, htype, 'active')
        break


# ══════════════════════════════════════════════════════════════
# 阶段判定
# ══════════════════════════════════════════════════════════════
def _get_stage(state: Dict) -> str:
    """从状态数值反推所处阶段。返回：
    stranger / acquaintance / friend / ambiguous_deep / negative
    """
    total_f = sum(float(v) for v in (state.get('friction') or {}).values())
    w = state.get('warmth', 0)

    # 负面关系判定
    if (w <= NEGATIVE_RELATIONSHIP_W_ZERO
        or (w > 0 and total_f >= w * NEGATIVE_RELATIONSHIP_F_TO_W_RATIO)):
        return 'negative'

    i = state.get('intimacy', 0)
    c = state.get('commitment', 0)

    if i <= STAGE_STRANGER_I_MAX:
        return 'stranger'
    if i <= STAGE_ACQUAINTANCE_I_MAX:
        return 'acquaintance'
    if c >= STAGE_FRIEND_C_MIN:
        return 'friend'
    return 'ambiguous_deep'   # 高亲密但无承诺


# ══════════════════════════════════════════════════════════════
# 交互统计写入（Tone/Reciprocity 用）
# ══════════════════════════════════════════════════════════════
def _log_interaction_stats(user_id, character_id, signals, session_id):
    """从 signals 抽出 tone/reciprocal 标签，写 rel_interaction_stats"""
    tone_category = None
    is_reciprocal = None
    for s in signals:
        stype = s.get('signal_type', '')
        if stype in ('small_care', 'genuine_care'):
            tone_category = 'support'
        elif stype in ('positive_reciprocal',):
            is_reciprocal = True
        elif stype in ('explicit_rejection', 'offensive_content'):
            is_reciprocal = False

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO rel_interaction_stats
                   (user_id, character_id, direction, tone_category,
                    is_reciprocal, is_initiator, session_id)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)''',
                (user_id, character_id, 'user', tone_category,
                 is_reciprocal, False, session_id))
    conn.commit()
    cur.close()
    conn.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
