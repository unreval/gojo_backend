"""relationship_state.py —— 感情判断系统 v4 · 状态账本读写

职责（纯代码，确定性，不调用 LLM）：
- 从 rel_state 表读写 W/F/I/Trust/Attachment/C/P
- 更新 pending_passion / pending_hypothesis
- 每次状态变化写 rel_provenance_log
- 提供 declared_stance 的 CRUD

★ 铁律 3：state 是估计值，任何变化必须写 provenance
★ 所有 apply_* 函数都是"接收 delta，写入并留痕"的原子操作
"""
import json
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Dict, List

from relationship_db import ensure_state_row, work_get_conn as get_conn
from relationship_config import (
    STATE_MIN, STATE_MAX,
    ATTACHMENT_MIN, ATTACHMENT_MAX,
    WARMTH_MIN, WARMTH_MAX,
    TRUST_POSITIVE_MULTIPLIER, TRUST_NEGATIVE_MULTIPLIER,
)
from relationship_semantics import is_new_pending_relationship_semantic_key


_PROVENANCE_EVIDENCE_REFS = ContextVar(
    'relationship_provenance_evidence_refs', default=(),
)


@contextmanager
def provenance_source_event(source_event_id):
    """Attach one Raw Event id to ledger provenance written in this scope."""
    event_id = str(source_event_id or '').strip()
    refs = ({'source_event_id': event_id},) if event_id else ()
    token = _PROVENANCE_EVIDENCE_REFS.set(refs)
    try:
        yield
    finally:
        _PROVENANCE_EVIDENCE_REFS.reset(token)


def current_provenance_source_event():
    for ref in _PROVENANCE_EVIDENCE_REFS.get() or ():
        source_event_id = ref.get('source_event_id') if isinstance(ref, dict) else None
        if source_event_id:
            return source_event_id
    return None


# ══════════════════════════════════════════════════════════════
# 读
# ══════════════════════════════════════════════════════════════
def load_state(user_id: str, character_id: str) -> Dict:
    """读取全部状态。返回 dict，如果没有行则先建行再返回默认值。"""
    ensure_state_row(user_id, character_id)
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT warmth, friction, intimacy, trust, attachment,
                          commitment, passion, pending_passion,
                          pending_hypothesis, banter_baseline, last_updated
                   FROM rel_state
                   WHERE user_id = %s AND character_id = %s''',
                (user_id, character_id))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        # ensure_state_row 已插入，理论不会到这
        return _default_state()

    return _state_from_row(row, has_state_row=True)


def read_state(user_id: str, character_id: str) -> Dict:
    """Read the ledger without creating a row.

    Prompt construction and relationship panels must be observational.  Writers
    continue using ``load_state`` so their first mutation can initialize the
    row atomically.
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT warmth, friction, intimacy, trust, attachment,
                          commitment, passion, pending_passion,
                          pending_hypothesis, banter_baseline, last_updated
                   FROM rel_state
                   WHERE user_id = %s AND character_id = %s''',
                (user_id, character_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return _default_state(has_state_row=False)
    return _state_from_row(row, has_state_row=True)


def _state_from_row(row, *, has_state_row: bool) -> Dict:
    return {
        'warmth': float(row[0] or 0),
        'friction': row[1] or {},
        'intimacy': float(row[2] or 0),
        'trust': float(row[3] or 0),
        'attachment': float(row[4] or 0),
        'commitment': float(row[5] or 0),
        'passion': float(row[6] or 0),
        'pending_passion': int(row[7] or 0),
        'pending_hypothesis': row[8] or [],
        'banter_baseline': row[9] or 'reserved',
        'last_updated': row[10],
        'has_state_row': has_state_row,
    }


def _default_state(*, has_state_row=False) -> Dict:
    return {
        'warmth': 0.0, 'friction': {}, 'intimacy': 0.0,
        'trust': 0.0, 'attachment': 0.0, 'commitment': 0.0,
        'passion': 0.0, 'pending_passion': 0,
        'pending_hypothesis': [], 'banter_baseline': 'reserved',
        'last_updated': None, 'has_state_row': has_state_row,
    }


# ══════════════════════════════════════════════════════════════
# 写：通用底层
# ══════════════════════════════════════════════════════════════
def _write_provenance(
    user_id: str, character_id: str,
    state_field: str, before: float, after: float,
    signal_type: str = None, confidence: str = None,
    rule: str = None, evidence_refs: List = None, note: str = None,
):
    """写一条 provenance 日志。所有状态变化都必须调用。"""
    if evidence_refs is None:
        evidence_refs = list(_PROVENANCE_EVIDENCE_REFS.get() or ())
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO rel_provenance_log
                   (user_id, character_id, state_field,
                    value_before, value_after, delta,
                    signal_type, confidence, rule,
                    evidence_refs, note)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)''',
                (user_id, character_id, state_field,
                 before, after, after - before,
                 signal_type, confidence, rule,
                 json.dumps(evidence_refs or []), note))
    conn.commit()
    cur.close()
    conn.close()


def _clip(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# ══════════════════════════════════════════════════════════════
# 写：各维度 apply（每个 delta 变化必留痕）
# ══════════════════════════════════════════════════════════════
def apply_warmth(user_id, character_id, delta: float, **prov_kwargs) -> float:
    """W += delta，返回新值。"""
    state = load_state(user_id, character_id)
    before = state['warmth']
    after = _clip(before + delta, WARMTH_MIN, WARMTH_MAX)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET warmth = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (after, user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()

    _write_provenance(user_id, character_id, 'warmth', before, after, **prov_kwargs)
    return after


def apply_friction(user_id, character_id, category: str, delta: float, **prov_kwargs) -> Dict:
    """F[category] += delta。F 是 JSONB dict，按类型分开计数。"""
    state = load_state(user_id, character_id)
    friction = dict(state['friction']) if state['friction'] else {}
    before = float(friction.get(category, 0))
    after = _clip(before + delta, 0, STATE_MAX)
    friction[category] = after

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET friction = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (json.dumps(friction), user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()

    _write_provenance(
        user_id, character_id, f'friction.{category}', before, after, **prov_kwargs
    )
    return friction


def apply_intimacy(user_id, character_id, delta: float, **prov_kwargs) -> float:
    """I += delta。I 基本单调递增，负 delta 需要非常明确的证据。"""
    state = load_state(user_id, character_id)
    before = state['intimacy']
    after = _clip(before + delta, STATE_MIN, STATE_MAX)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET intimacy = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (after, user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()

    _write_provenance(user_id, character_id, 'intimacy', before, after, **prov_kwargs)
    return after


def apply_trust(user_id, character_id, delta: float, **prov_kwargs) -> float:
    """Trust += delta，★ 不对称：负 delta 乘以更高系数（一次背叛≈多次好行为）。"""
    state = load_state(user_id, character_id)
    before = state['trust']

    if delta >= 0:
        effective_delta = delta * TRUST_POSITIVE_MULTIPLIER
    else:
        effective_delta = delta * TRUST_NEGATIVE_MULTIPLIER

    after = _clip(before + effective_delta, STATE_MIN, STATE_MAX)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET trust = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (after, user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()

    _write_provenance(
        user_id, character_id, 'trust', before, after,
        note=(prov_kwargs.pop('note', None) or '') + f' [asym x{TRUST_NEGATIVE_MULTIPLIER if delta<0 else TRUST_POSITIVE_MULTIPLIER}]',
        **prov_kwargs,
    )
    return after


def apply_attachment(user_id, character_id, delta: float, **prov_kwargs) -> float:
    """Attachment += delta。valence 独立于 W——不因 W 为负而减。"""
    state = load_state(user_id, character_id)
    before = state['attachment']
    after = _clip(before + delta, ATTACHMENT_MIN, ATTACHMENT_MAX)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET attachment = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (after, user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()

    _write_provenance(user_id, character_id, 'attachment', before, after, **prov_kwargs)
    return after


def apply_commitment(user_id, character_id, delta: float, **prov_kwargs) -> float:
    """C += delta，重"成本行为"，不由频率触发。"""
    state = load_state(user_id, character_id)
    before = state['commitment']
    after = _clip(before + delta, STATE_MIN, STATE_MAX)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET commitment = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (after, user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()

    _write_provenance(user_id, character_id, 'commitment', before, after, **prov_kwargs)
    return after


def apply_passion(user_id, character_id, delta: float, **prov_kwargs) -> float:
    """P += delta。直接调用一般是"已经通过阶段门控"的正式转化，不是原始信号。"""
    state = load_state(user_id, character_id)
    before = state['passion']
    after = _clip(before + delta, STATE_MIN, STATE_MAX)

    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET passion = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (after, user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()

    _write_provenance(user_id, character_id, 'passion', before, after, **prov_kwargs)
    return after


# ══════════════════════════════════════════════════════════════
# pending_passion 累积器
# ══════════════════════════════════════════════════════════════
def update_pending_passion(user_id, character_id, new_value: int, **prov_kwargs):
    state = load_state(user_id, character_id)
    before = state['pending_passion']
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET pending_passion = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (new_value, user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()
    _write_provenance(
        user_id, character_id, 'pending_passion',
        float(before), float(new_value), **prov_kwargs,
    )


# ══════════════════════════════════════════════════════════════
# pending_hypothesis（JSONB 列表操作）
# ══════════════════════════════════════════════════════════════
def push_hypothesis_evidence(user_id, character_id, hypothesis_type: str, evidence: Dict):
    """向指定类型的 hypothesis 追加一条 evidence。
    如果这个类型不存在则新建。evidence 是任意 dict，会被 append 到 evidence 数组。
    """
    # New relationship semantics are owned by Cognitive questions / hypotheses
    # / beliefs. Existing legacy cues can remain for compatibility only.
    if is_new_pending_relationship_semantic_key(hypothesis_type):
        return False
    state = load_state(user_id, character_id)
    plist = list(state['pending_hypothesis']) if state['pending_hypothesis'] else []

    found = None
    for h in plist:
        if h.get('type') == hypothesis_type and h.get('status') != 'confirmed':
            found = h
            break

    if found is None:
        found = {
            'type': hypothesis_type,
            'status': 'pending',
            'evidence': [],
            'created_at': _now_iso(),
            'last_seen_at': _now_iso(),
        }
        plist.append(found)

    found['evidence'].append(evidence)
    found['last_seen_at'] = _now_iso()

    _save_hypotheses(user_id, character_id, plist)
    return True


def promote_hypothesis(user_id, character_id, hypothesis_type: str, new_status: str):
    """把某个 hypothesis 的状态改成 active/confirmed/dismissed。"""
    if is_new_pending_relationship_semantic_key(hypothesis_type):
        return False
    state = load_state(user_id, character_id)
    plist = list(state['pending_hypothesis']) if state['pending_hypothesis'] else []
    for h in plist:
        if h.get('type') == hypothesis_type and h.get('status') != 'confirmed':
            h['status'] = new_status
            h['last_seen_at'] = _now_iso()
            break
    _save_hypotheses(user_id, character_id, plist)
    return True


def cleanup_hypotheses(user_id, character_id, max_age_days: int):
    """清理超期未确认的 pending。"""
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
    state = load_state(user_id, character_id)
    plist = list(state['pending_hypothesis']) if state['pending_hypothesis'] else []
    kept = [h for h in plist
            if h.get('status') == 'confirmed'
            or h.get('last_seen_at', '9999') > cutoff]
    if len(kept) != len(plist):
        _save_hypotheses(user_id, character_id, kept)


def _save_hypotheses(user_id, character_id, plist: List[Dict]):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_state SET pending_hypothesis = %s, last_updated = CURRENT_TIMESTAMP
                   WHERE user_id = %s AND character_id = %s''',
                (json.dumps(plist), user_id, character_id))
    conn.commit()
    cur.close()
    conn.close()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ══════════════════════════════════════════════════════════════
# Declared Stance CRUD
# ══════════════════════════════════════════════════════════════
def declare_stance(user_id, character_id, stance_type: str, content: str,
                   source_event_ref: str = None) -> int:
    """新增一条角色的明确表态。返回新记录 id。
    典型 stance_type: 'care_admission' / 'promise' / 'relationship_confirm' / 'boundary_stated'
    """
    source_event_ref = source_event_ref or current_provenance_source_event()
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''INSERT INTO rel_declared_stance
                   (user_id, character_id, stance_type, content, source_event_ref)
                   VALUES (%s, %s, %s, %s, %s)
                   RETURNING id''',
                (user_id, character_id, stance_type, content, source_event_ref))
    new_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return new_id


def revoke_stance(user_id, character_id, stance_id: int, reason: str) -> bool:
    """废除某条表态。只有走完负面证据流程后才应该调这个。返回是否成功废除。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''UPDATE rel_declared_stance
                   SET status = 'revoked',
                       revoked_at = CURRENT_TIMESTAMP,
                       revoke_reason = %s
                   WHERE id = %s AND user_id = %s AND character_id = %s
                     AND status = 'active' ''',
                (reason, stance_id, user_id, character_id))
    ok = cur.rowcount > 0
    conn.commit()
    cur.close()
    conn.close()

    if ok:
        # 也写进 provenance，方便审计
        _write_provenance(
            user_id, character_id,
            state_field=f'declared_stance[{stance_id}]',
            before=1.0, after=0.0,
            rule='stance_revoke',
            note=reason,
        )
    return ok


def list_active_stances(user_id, character_id) -> List[Dict]:
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT id, stance_type, content, declared_at
                   FROM rel_declared_stance
                   WHERE user_id = %s AND character_id = %s AND status = 'active'
                   ORDER BY declared_at ASC''',
                (user_id, character_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [{'id': r[0], 'type': r[1], 'content': r[2], 'declared_at': r[3]}
            for r in rows]


def list_declared_stances(user_id, character_id, *, include_inactive=True) -> List[Dict]:
    """Read active and historical stances for a read-only relationship panel."""
    conn = get_conn()
    cur = conn.cursor()
    if include_inactive:
        cur.execute('''SELECT id, stance_type, content, source_event_ref, status,
                              declared_at, revoked_at, revoke_reason
                       FROM rel_declared_stance
                       WHERE user_id = %s AND character_id = %s
                       ORDER BY declared_at ASC, id ASC''',
                    (user_id, character_id))
    else:
        cur.execute('''SELECT id, stance_type, content, source_event_ref, status,
                              declared_at, revoked_at, revoke_reason
                       FROM rel_declared_stance
                       WHERE user_id = %s AND character_id = %s AND status = 'active'
                       ORDER BY declared_at ASC, id ASC''',
                    (user_id, character_id))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return [
        {
            'id': row[0], 'type': row[1], 'content': row[2],
            'source_event_ref': row[3], 'status': row[4],
            'declared_at': row[5], 'revoked_at': row[6],
            'revoke_reason': row[7],
        }
        for row in rows
    ]


def save_offline_character_state(user_id: str, character_id: str, state: Dict):
    """Persist only short-term continuity cues, never relationship authority."""
    if not user_id or not character_id or not isinstance(state, dict):
        return
    payload = _offline_continuity_payload(state)
    if not payload:
        return
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''INSERT INTO rel_offline_character_state
               (user_id, character_id, payload, updated_at)
           VALUES (%s, %s, %s::jsonb, CURRENT_TIMESTAMP)
           ON CONFLICT (user_id, character_id) DO UPDATE
           SET payload = EXCLUDED.payload,
               updated_at = CURRENT_TIMESTAMP''',
        (user_id, character_id, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    cur.close()
    conn.close()


def load_offline_character_state(user_id: str, character_id: str) -> Dict:
    """Read filtered short-term continuity cues.  Historical rows are filtered too."""
    if not user_id or not character_id:
        return {}
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        '''SELECT payload FROM rel_offline_character_state
           WHERE user_id = %s AND character_id = %s''',
        (user_id, character_id),
    )
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row or not row[0]:
        return {}
    payload = row[0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            return {}
    return _offline_continuity_payload(payload) if isinstance(payload, dict) else {}


_OFFLINE_CONTINUITY_KEYS = ('action', 'intent', 'moodshift', 'anchor', 'from')
_OFFLINE_RELATIONSHIP_TERMS = (
    '爱情', '恋爱', '心动', '喜欢', '不喜欢', '告白', '表白', '拒绝',
    'relationship', 'romantic', 'love',
)


def _offline_continuity_payload(state: Dict) -> Dict:
    """Keep scene/action continuity while dropping hidden relationship claims."""
    if not isinstance(state, dict):
        return {}
    payload = {}
    for key in _OFFLINE_CONTINUITY_KEYS:
        value = state.get(key)
        if value in (None, ''):
            continue
        text = str(value).strip()[:300]
        if not text:
            continue
        # A short-term intent may remain useful, but it cannot smuggle a
        # romance/rejection conclusion back into the next prompt.
        if key in {'action', 'intent'} and any(
                term in text.casefold() for term in _OFFLINE_RELATIONSHIP_TERMS):
            continue
        payload[key] = text
    return payload


def load_offline_continuity_state(user_id: str, character_id: str) -> Dict:
    """Explicit name for consumers that need scene continuity only."""
    return load_offline_character_state(user_id, character_id)
