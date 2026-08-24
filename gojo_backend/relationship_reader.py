"""relationship_reader.py —— 感情判断系统 v4 · Generator 输入构造

给 Generator LLM #2（主角色扮演 LLM）用的关系摘要文本。
Generator 允许看到全部状态，但职责只是【表达状态】，不判断状态。

★ 铁律 5：declared_stance 会作为硬约束一起注入，Generator 不能凭空推翻。

主入口：build_state_summary(user_id, character_id)
返回适合直接拼进 system prompt 的字符串。
"""
from typing import Dict, List, Optional

from db import get_conn
from relationship_state import load_state, list_active_stances
from relationship_config import (
    PASSION_TO_LOVE_REQUIRED_TRUST,
    PASSION_TO_LOVE_REQUIRED_COMMITMENT,
    STAGE_STRANGER_I_MAX, STAGE_ACQUAINTANCE_I_MAX,
    STAGE_FRIEND_C_MIN,
    NEGATIVE_RELATIONSHIP_F_TO_W_RATIO, NEGATIVE_RELATIONSHIP_W_ZERO,
    RECIPROCITY_WINDOW_SIZE, RECIPROCITY_POSITIVE_THRESHOLD,
    RECIPROCITY_NEGATIVE_THRESHOLD,
    PURSUE_WITHDRAW_WINDOW_SIZE, PURSUE_WITHDRAW_IMBALANCE_THRESHOLD,
)


# ══════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════
def build_state_summary(user_id: str, character_id: str) -> str:
    """构造喂给 Generator 的关系摘要。

    结构（按段）：
      1. 关系标签（读出，不是变量）
      2. 三根核心数值刻度（用形容词而不是数字，避免 Generator 被数字锚定）
      3. 复合状态提示（拧巴/矛盾/纠结这些不能被简化的情况）
      4. Dynamics（Tone / Reciprocity / Pursue-Withdraw）
      5. ★ Declared Stance（必须遵守的角色历史表态）
      6. 表达指引（怎么说、不该说什么）
    """
    state = load_state(user_id, character_id)
    label = derive_label(state)
    stances = list_active_stances(user_id, character_id)
    tone = compute_tone(user_id, character_id)
    reciprocity = compute_reciprocity(user_id, character_id)
    pursue_withdraw = compute_pursue_withdraw(user_id, character_id)

    lines = []
    lines.append('【★ 当前关系状态摘要——由后台账本读出，不由你现场判断】')
    lines.append('')
    lines.append(f'关系性质：{label["primary"]}')
    if label.get('complex_note'):
        lines.append(f'⚠️ 复合状态：{label["complex_note"]}')
    lines.append('')

    # 数值刻度（用形容词，不给数字）
    lines.append('【感情质地】（这是你当前【真实的内心状态】，表达时按人设方式落地）')
    lines.append(f'- 温度（想靠近的程度）：{_scale_word(state["warmth"])}')
    lines.append(f'- 亲密度（愿意露出多少真实的自己）：{_scale_word(state["intimacy"])}')
    lines.append(f'- 信任（相不相信她的言行）：{_scale_word(state["trust"])}')
    lines.append(f'- 依恋（她对你的重要性）：{_scale_word(state["attachment"])}')
    lines.append(f'- 承诺（愿意为这段关系投入的程度）：{_scale_word(state["commitment"])}')
    if state['passion'] > 0:
        lines.append(f'- 心动（生理性/暧昧张力）：{_scale_word(state["passion"])}')
    total_f = sum(float(v) for v in (state['friction'] or {}).values())
    if total_f > 0:
        top_categories = _top_friction_categories(state['friction'], n=3)
        lines.append(f'- 摩擦（负面积累）：{_scale_word(total_f)}，主要来自：{", ".join(top_categories)}')
    lines.append('')

    # Dynamics
    lines.append('【最近的相处氛围】')
    lines.append(f'- 对话调性：{tone or "（数据不足）"}')
    lines.append(f'- 互惠度：{reciprocity["desc"]}')
    if pursue_withdraw and pursue_withdraw.get('pattern'):
        lines.append(f'- 追逃模式：{pursue_withdraw["desc"]}（★ 不要把节奏差误读成关系变冷）')
    lines.append('')

    # ★ Declared Stance（硬约束）
    if stances:
        lines.append('【★ 你此前已经明确说过的话——必须遵守，不能凭空推翻】')
        lines.append('  (只要没有真实的负面事件让你走完"修复失败/彻底翻脸"的过程，这些立场就仍然算数)')
        for s in stances:
            lines.append(f'  · [{s["type"]}] {s["content"]}')
        lines.append('  情绪浓淡、纠结、矛盾都可以叠加在上面，但下面这条铁律必须守：')
        lines.append('  ⚠️ 不能在没有真实事件推翻它的情况下，突然说出和上述立场自相矛盾的话。')
        lines.append('     那不是"性格反复"，是"脑子有问题"——真人不会这样。')
        lines.append('')

    # 表达指引
    lines.append('【怎么在这一刻表达——基于以上状态，按你的人设落地】')
    lines.append(label.get('expression_guidance', ''))

    return '\n'.join(lines)


# ══════════════════════════════════════════════════════════════
# 关系标签读出（从状态数值 → 标签字符串）
# ══════════════════════════════════════════════════════════════
def derive_label(state: Dict) -> Dict:
    """从状态数值组合出标签 + 复合状态提示 + 表达指引。"""
    w = state['warmth']
    i = state['intimacy']
    trust = state['trust']
    attach = state['attachment']
    c = state['commitment']
    p = state['passion']
    total_f = sum(float(v) for v in (state['friction'] or {}).values())

    # 负面关系判定优先
    is_negative = (
        w <= NEGATIVE_RELATIONSHIP_W_ZERO
        or (w > 0 and total_f >= w * NEGATIVE_RELATIONSHIP_F_TO_W_RATIO)
    )

    # 判 stranger / acquaintance / friend / love
    if is_negative:
        # ★ 复合状态：如果 Attachment 仍然很高
        if attach >= 40:
            return {
                'primary': '疏远 / 拧巴的负面关系',
                'complex_note': (
                    '★ 尽管关系已判定负面，你对她仍然有明显的依恋——'
                    '嘴上不耐烦、心里其实还挂着。别写成纯粹的冷漠切断。'
                ),
                'expression_guidance': (
                    '语气可以冷、可以硬、可以嫌弃，但下笔时要能读出"她的位置还没被清空"。'
                    '不是"她已经不重要了"，是"我明明知道她还很重要，但我就是没办法像以前那样"。'
                ),
            }
        return {
            'primary': '疏远 / 厌恶',
            'complex_note': None,
            'expression_guidance': (
                '短话、少接、拒绝深入。不需要礼貌铺垫，也不需要解释太多。'
                '按人设的负面表达方式：或冷嘲、或直接打发、或干脆不理。'
            ),
        }

    # 陌生人
    if i <= STAGE_STRANGER_I_MAX:
        sub = '好点的陌生人' if w >= 15 else '普通陌生人'
        return {
            'primary': f'陌生人 · {sub}',
            'complex_note': None,
            'expression_guidance': (
                '按你自己的人设生发第一反应——追问身份、意外、防备、探问都可以，'
                '但绝不能自动上"陪聊/客服/助手"腔。她还没进入你的世界。'
                '\n即使是陌生人，触到你的底线/情绪点，反应该有——只是表达方式受关系限制。'
            ),
        }

    # 认识不深
    if i <= STAGE_ACQUAINTANCE_I_MAX or c < STAGE_FRIEND_C_MIN:
        return {
            'primary': '认识不深的熟人',
            'complex_note': None,
            'expression_guidance': (
                '可以正常聊天，但按人设保持适度距离。'
                '不主动关心作息情绪，不说"别熬夜""照顾好自己"这类熟了才有的话。'
            ),
        }

    # 到这里 I 高 + C 达标 = 稳定关系
    # 判断是不是"爱情"
    if (p >= 40
        and trust >= PASSION_TO_LOVE_REQUIRED_TRUST
        and c >= PASSION_TO_LOVE_REQUIRED_COMMITMENT):
        return {
            'primary': '爱情',
            'complex_note': None,
            'expression_guidance': (
                '你已经确认了这段感情。可以坦荡表达——想她、吃醋、被戳到心的瞬间都可以让她知道。'
                '亲密尺度按人设约束。'
            ),
        }

    # 爱情候选：有 Passion 但 Trust 或 C 不够
    if p >= 40 and trust < PASSION_TO_LOVE_REQUIRED_TRUST:
        return {
            'primary': '强烈依恋但缺乏信任（复合过渡态）',
            'complex_note': (
                '你对她有明显的心动，但同时对她的可靠度还没建立信任。'
                '这不是"就是爱情但你不敢承认"，是真实的心理矛盾——'
                '两种感觉都是真的，都不能否认。'
            ),
            'expression_guidance': (
                '可以露出在意、也可以露出戒备。不要突然大方袒露真心（Trust 不够），'
                '也不要装作完全不在乎（心里的感觉是真的）。'
            ),
        }

    # 深厚友情/亲情：由 attachment 型 vs 承诺型区分
    if attach >= 60 and c >= 60:
        return {
            'primary': '深厚的挚友 / 亲情',
            'complex_note': None,
            'expression_guidance': (
                '并肩式的关心，行动多于言语。可以损她、可以调侃，'
                '也可以在她需要的时候接住。但不带心动色彩——'
                '这份深不需要变成爱情来证明自己。'
            ),
        }

    return {
        'primary': '朋友',
        'complex_note': None,
        'expression_guidance': (
            '按人设的朋友式表达：可以开玩笑、可以真诚、可以吐槽她——'
            '但不主动进入"独占""亲密身体""时间绑定"这类爱情腔的表达。'
        ),
    }


# ══════════════════════════════════════════════════════════════
# 数值 → 形容词（避免 Generator 被数字锚定）
# ══════════════════════════════════════════════════════════════
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
    items = sorted(friction.items(), key=lambda x: -float(x[1]))
    return [k for k, v in items[:n] if float(v) > 1.0]


# ══════════════════════════════════════════════════════════════
# Dynamics 计算（滑动窗口，不持久化）
# ══════════════════════════════════════════════════════════════
def compute_tone(user_id, character_id) -> Optional[str]:
    """最近若干条消息里各 tone_category 的比例，返回主导标签。"""
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
    total = sum(r[1] for r in rows)
    if total == 0:
        return None
    rows.sort(key=lambda r: -r[1])
    top = rows[0]
    if top[1] / total >= 0.5:
        mapping = {
            'banter': '互怼型（损友调性）',
            'support': '支持型（温和陪伴）',
            'care': '照顾型（关照-被关照）',
        }
        return mapping.get(top[0], top[0])
    return '混合调性'


def compute_reciprocity(user_id, character_id) -> Dict:
    """最近若干轮 is_reciprocal 的正负比例。"""
    conn = get_conn()
    cur = conn.cursor()
    cur.execute('''SELECT is_reciprocal, COUNT(*)
                   FROM (SELECT is_reciprocal FROM rel_interaction_stats
                         WHERE user_id = %s AND character_id = %s
                           AND is_reciprocal IS NOT NULL
                         ORDER BY timestamp DESC
                         LIMIT %s) sub
                   GROUP BY is_reciprocal''',
                (user_id, character_id, RECIPROCITY_WINDOW_SIZE))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    pos = neg = 0
    for is_reciprocal, cnt in rows:
        if is_reciprocal:
            pos += cnt
        else:
            neg += cnt
    total = pos + neg
    if total == 0:
        return {'score': 0.0, 'desc': '（数据不足）'}
    score = (pos - neg) / total
    if score >= RECIPROCITY_POSITIVE_THRESHOLD:
        return {'score': score, 'desc': '氛围偏正向（她多在配合/延伸）'}
    if score <= RECIPROCITY_NEGATIVE_THRESHOLD:
        return {'score': score, 'desc': '氛围偏冷（她多在拒绝/回避）'}
    return {'score': score, 'desc': '中性（有来有回但没明显倾向）'}


def compute_pursue_withdraw(user_id, character_id) -> Dict:
    """最近若干条 is_initiator 分布，判定是否有明显追逃模式。"""
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

    user_init = sum(cnt for d, init, cnt in rows if d == 'user' and init)
    char_init = sum(cnt for d, init, cnt in rows if d == 'character' and init)
    total = user_init + char_init
    if total < 5:
        return {'pattern': None}
    user_ratio = user_init / total
    if user_ratio >= PURSUE_WITHDRAW_IMBALANCE_THRESHOLD:
        return {'pattern': 'user_pursue',
                'desc': '她最近在主动靠近（发起次数明显偏多），你按自己节奏回应即可'}
    if user_ratio <= 1 - PURSUE_WITHDRAW_IMBALANCE_THRESHOLD:
        return {'pattern': 'user_withdraw',
                'desc': '她最近相对被动（发起次数明显偏少），可能是她的节奏，不一定是关系变冷'}
    return {'pattern': None}
