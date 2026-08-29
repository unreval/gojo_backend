"""relationship_signals.py —— 感情判断系统 v4 · Observer LLM #1

【★ 铁律 1】此模块调用 LLM 时，输入里绝对不能有：
    - 当前 W/F/I/Trust/Attachment/C/P
    - pending_hypothesis
    - relationship_label
    - perceived_user_attitude
不能让 LLM"带着结论去做题"。允许输入：
    - 对话内容（当前用户消息 + 角色回复）
    - 该角色的 Core（人设，为了理解语言习惯，不是为了引导判断）
    - 短期对话上下文（可选，最近几轮，帮 LLM 消歧义）

Observer 的输出是原子事件（signal），不是结论。
"""
import json
import re
from typing import Dict, List, Optional

from ai_client import create_chat
from config import MODEL_MAIN
from relationship_config import (
    SIGNAL_EXTRACTOR_MAX_TOKENS,
)


# ══════════════════════════════════════════════════════════════
# Observer prompt
# ══════════════════════════════════════════════════════════════
_OBSERVER_SYSTEM_PROMPT = '''你是一个中立的对话观察员。你的任务是从下面这段对话里，提取【发生了什么事】——
只描述【事件本身】，不推断【意味着什么】。

【严禁做的事】
- 不要判断"角色喜不喜欢用户"或"关系变深了还是变浅了"
- 不要引用任何关系状态、感情标签、心理术语
- 不要基于角色人设"合理化"用户行为（比如"因为角色高冷所以用户示好其实是..."）
- 不要基于对话之外的假设推断

【要做的事】
按下面 JSON schema 输出，只输出 JSON，不要解释、不要 markdown 代码块围栏。

{
  "signals": [
    {
      "signal_type": string,       // 见下面枚举
      "actor": "user" | "character",
      "confidence": "high" | "medium" | "low",
      "brief": string,             // 一句话说这件事到底是什么（不含推断）
      "attributes": {}             // 可选：signal_type 相关的额外字段
    }
  ]
}

【signal_type 枚举（只用这些，不要发明新的）】

用户对角色的：
- small_care              问候、注意休息这类小关心
- genuine_care            记住细节、主动关注具体情况
- self_disclosure         用户主动分享自己的事（attributes: {"depth": "outer"|"middle"|"core"}）
- flirt_signal            调情/暧昧信号（attributes: {"explicit": bool}）
- positive_reciprocal     对角色暧昧信号的对等回应（一起延伸话题，不是笑而不答）
- explicit_rejection      明确拒绝暧昧信号
- ambiguous_response      笑而不答/沉默/生硬转移话题
- promise_kept            承诺兑现（说到做到）
- promise_broken          承诺违反
- boundary_hit            触碰雷区（attributes: {"topic_hint": string, "severity": "low"|"medium"|"high", "intentional": "yes"|"no"|"unclear"}）
- boundary_respected      角色表态过后主动收手/尊重（attributes: {"topic_hint": string}）
- repair_attempt          冲突后修复尝试（attributes: {"acknowledgment": bool, "responsibility": bool, "corrective_action": bool}）
- offensive_content       冒犯性内容（辱骂、贬低、脏话；attributes: {"target": "character"|"third_party"}）

角色的（同样从对话文本里观察）：
- character_stance_declared 角色明确说出了一个【重大立场】（attributes: {"stance_type": "...", "content": string}）
  ★ 这个信号的门槛非常高——不是每句关心的话都算！只有以下情况才能触发：
    stance_type 枚举：
    · "care_admission"    = 角色承认自己在意用户，而且说的话有分量（比如"想哭随时说，我在这"）
                           ❌ 不算的：日常催吃饭（"吃了没"）、随口关心（"早点睡"）、顺口答应（"嗯行吧"）
                           ★ 判断标准：如果这句话换一个普通朋友也会随口说，那就不算 stance
    · "promise"           = 角色做出了明确的、有具体内容的承诺（比如"下次一定回你消息"）
                           ❌ 不算的：语气词式的敷衍（"嗯会的"）、模糊的安慰（"会好起来的"）
    · "relationship_confirm" = 角色明确定义了关系性质（比如"我们在一起了"或"你是我最好的朋友"）
                           ❌ 不算的：回避式的自我保护（"就当朋友吧"这种退缩语气不算 confirm）
                           ★ 如果角色是"靠近了又退缩"，用 "retreat_boundary" 而不是 "relationship_confirm"
    · "retreat_boundary"  = 角色刚流露了深层感情后，用理性/次元/身份差异来给自己找台阶下
                           比如："友達でいいんじゃないの、次元も違うし"（就当朋友吧，次元不同嘛）
                           这是自我保护的退缩，不是关系的最终定论——角色可以改变想法
    · "boundary_stated"   = 角色明确划了一条底线（比如"这个话题我不想再谈"）
  ★★ 每轮对话最多提取 1 条 stance！大部分对话不应该提取任何 stance！
  ★★ content 字段必须用中文简短归纳（不超过 30 字），不要塞角色的日语原文
- character_boundary_stated 角色明确表态某话题是底线（attributes: {"topic_hint": string}）
- character_reciprocal      角色对用户暧昧信号的对等回应

【confidence 判定标准】
- high：话说得很直接，理解成别的意思很难
- medium：明显朝这个方向，但有一定解读空间
- low：只是模糊迹象，可能只是随口一说

【必须避免的常见错误】
- "哈哈"、"……"、"嗯"这种不算 positive_reciprocal，应该是 ambiguous_response
- 用户没生气但字面上说"我讨厌你"（明显调侃）不算 offensive_content
- 你不确定的东西写 low confidence，不要瞎猜成 medium/high
- 如果对话里没有任何值得提取的事件，返回 {"signals": []}
'''


def _build_user_prompt(
    user_message: str, character_reply: Optional[str],
    character_core_snippet: Optional[str] = None,
    recent_context: Optional[List[Dict]] = None,
) -> str:
    parts = []
    if character_core_snippet:
        parts.append(
            f'【角色语言习惯参考】（只用于理解语气，不用于判断关系）\n{character_core_snippet}\n'
        )
    if recent_context:
        parts.append('【最近对话上下文】（帮你消歧义，不用于推断）')
        for msg in recent_context[-6:]:
            role = '用户' if msg.get('role') == 'user' else '角色'
            parts.append(f'{role}: {msg.get("content", "")}')
        parts.append('')
    parts.append('【本轮对话】')
    parts.append(f'用户: {user_message}')
    if character_reply:
        parts.append(f'角色: {character_reply}')
    parts.append('\n请按 schema 输出 JSON，只输出 JSON。')
    return '\n'.join(parts)


def _extract_json(text: str) -> Optional[Dict]:
    """从 LLM 返回里挖出 JSON。宽容处理围栏和前后缀。"""
    text = text.strip()
    # 剥掉可能的 markdown 围栏
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        # 尝试抓第一个 {...} 块
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def extract_signals(
    user_message: str,
    character_reply: Optional[str] = None,
    character_core_snippet: Optional[str] = None,
    recent_context: Optional[List[Dict]] = None,
    model: Optional[str] = None,
) -> Dict:
    """从一轮对话里提取 signal 列表。

    Args:
        user_message: 用户本轮消息
        character_reply: 角色本轮回复（可选；分析用户单独发言时可传 None）
        character_core_snippet: 角色 core prompt 的前若干字（帮理解语气；不要塞太多）
        recent_context: 最近若干轮的 {role, content} 用于消歧义
        model: 覆盖默认 model；默认走 MODEL_MAIN（跟主聊天一样，走中转 Opus 4.6）

    Returns:
        dict: {"signals": [...], "raw": str, "model": str, "error": Optional[str]}
    """
    user_prompt = _build_user_prompt(
        user_message, character_reply,
        character_core_snippet, recent_context,
    )

    try:
        # ★ 注意：不传 temperature —— 中转 API 的 create_chat 不接受该参数
        #   Observer 的判断本来就靠 prompt 的严格约束，不靠低 temperature
        raw_text, _usage = create_chat(
            model=model or MODEL_MAIN,
            messages=[{'role': 'user', 'content': user_prompt}],
            system=_OBSERVER_SYSTEM_PROMPT,
            max_tokens=SIGNAL_EXTRACTOR_MAX_TOKENS,
        )
    except Exception as e:
        return {'signals': [], 'raw': '', 'model': model or MODEL_MAIN,
                'error': f'llm_call_failed: {e}'}

    parsed = _extract_json(raw_text)
    if parsed is None or 'signals' not in parsed:
        return {'signals': [], 'raw': raw_text, 'model': model or MODEL_MAIN,
                'error': 'json_parse_failed'}

    # 简单清洗：确保每个 signal 有 signal_type/actor/confidence 三个必填
    valid_signals = []
    for s in parsed.get('signals', []):
        if not isinstance(s, dict):
            continue
        if not s.get('signal_type') or not s.get('actor') or not s.get('confidence'):
            continue
        if s['confidence'] not in ('high', 'medium', 'low'):
            continue
        if s['actor'] not in ('user', 'character'):
            continue
        s.setdefault('brief', '')
        s.setdefault('attributes', {})
        valid_signals.append(s)

    return {'signals': valid_signals, 'raw': raw_text,
            'model': model or MODEL_MAIN, 'error': None}