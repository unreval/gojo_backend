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
from typing import Dict, List, Optional

from ai_client import create_chat
from config import MODEL_MAIN
from relationship_config import (
    SIGNAL_EXTRACTOR_MAX_TOKENS,
)
from structured_output import invoke_structured_llm


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
- 角色转述旧日记或过去的主观反思（如“我当时觉得喜欢她”）不是本轮新发生的关系事件，不能仅据此提取立场、对等回应或其他关系强化信号。
- 旧日记被再次提及不增加 hypothesis/confidence 或 relationship state；必须有本轮新的独立外部证据。用户对旧事的明确纠正优先于日记推测。
- 日程主动分享、日记正文/留言反应、群聊生成角色文本都只是表达或连续性，不能单独成为 care、romance、立场或关系性质的新证据；不得由它们提取关系强化信号。

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

【合法 JSON 示例】
- 即使只有一条事件，也必须放在 signals 数组里：
{"signals":[{"signal_type":"small_care","actor":"user","confidence":"high","brief":"用户提醒角色早点休息","attributes":{}}]}
- 没有事件时：{"signals":[]}

【signal_type 枚举（只用这些，不要发明新的）】

用户对角色的：
- small_care              问候、注意休息这类小关心
- genuine_care            记住细节、主动关注具体情况
- self_disclosure         用户主动分享自己的事（attributes: {"depth": "outer"|"middle"|"core"}）
- flirt_signal            调情/暧昧信号（attributes: {"explicit": bool, "flirt_interpretation": "playful_flirt"|"habitual_flirt"|"social_flirt"|"romantic_probe"|"romantic_admission"|"ambiguous_flirt", "frame_break": bool, "meta_serious": bool, "exclusive_to_character": bool, "habitual_with_others": bool}）
- positive_reciprocal     对角色暧昧信号的对等回应（一起延伸话题，不是笑而不答；同样可带 flirt_interpretation / frame_break / meta_serious）
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
- character_reciprocal      角色对用户暧昧信号的对等回应（attributes 可含 flirt_interpretation / frame_break / meta_serious）

【flirt_interpretation 只描述这一句的互动形态，不判断关系是不是爱情】
- playful_flirt / habitual_flirt / social_flirt：玩笑、习惯互叫、社交起哄
- romantic_probe：认真试探对方是否把这当浪漫
- romantic_admission：认真承认心动/喜欢
- ambiguous_flirt：分不清玩笑还是认真，不要强行分类
- 若出现“这次不是开玩笑 / 这次我是认真的”，设 frame_break=true 且 meta_serious=true
- 若这人对谁都这样叫，habitual_with_others=true；若几乎只对这个角色这样，exclusive_to_character=true

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


_SINGLE_EVENT_EXAMPLE = (
    '{"signals":[{"signal_type":"small_care","actor":"user",'
    '"confidence":"high","brief":"用户提醒角色早点休息",'
    '"attributes":{}}]}'
)


def _build_user_prompt(
    user_message: str, character_reply: Optional[str],
    character_core_snippet: Optional[str] = None,
    recent_context: Optional[List[Dict]] = None,
    temporal_context: Optional[Dict] = None,
) -> str:
    parts = []
    if character_core_snippet:
        parts.append(
            f'【角色语言习惯参考】（只用于理解语气，不用于判断关系）\n{character_core_snippet}\n'
        )
    if temporal_context:
        try:
            from temporal_awareness import build_relationship_context
            temporal_text = build_relationship_context(temporal_context)
            if temporal_text:
                parts.append(temporal_text)
        except Exception:
            pass
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


class _SignalsSchemaError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _signals_value_type(value) -> str:
    if value is None:
        return 'null'
    if isinstance(value, bool):
        return 'boolean'
    if isinstance(value, list):
        return 'array'
    if isinstance(value, dict):
        return 'object'
    if isinstance(value, str):
        return 'string'
    if isinstance(value, (int, float)):
        return 'number'
    return 'other'


def _validate_signals_envelope(value):
    """Domain schema only; extraction itself stays in structured_output."""
    if 'signals' not in value:
        raise _SignalsSchemaError('signals_missing')
    signals = value.get('signals')
    if isinstance(signals, list):
        return value
    value_type = _signals_value_type(signals)
    detail = {
        'null': 'signals_null',
        'object': 'signals_object_not_array',
        'string': 'signals_string_not_array',
    }.get(value_type, 'signals_not_array')
    raise _SignalsSchemaError(detail)


def _relationship_parse_error(result):
    """Keep the observer's public error categories while using one parser."""
    code = result.error_code or 'structured_output_failed'
    if code == 'multiple_distinct_json_objects':
        return 'signals_envelope_ambiguous', 'multiple_distinct_signals_envelopes'
    if code == 'schema_validation_failed':
        return 'signals_schema_invalid', result.error_detail or 'signals_invalid'
    return 'json_parse_failed', code


def _retry_instruction(error: str, error_reason: str) -> str:
    reason = error_reason or error or 'unknown_format_error'
    return (
        f'\n上次输出未通过结构校验，检测到的错误是：{reason}。'
        '请重新只输出一个完整 JSON 对象。signals 必须是数组；'
        '即使只有一条事件也必须放进数组。合法单事件示例：'
        f'{_SINGLE_EVENT_EXAMPLE}；没有事件时返回 {{"signals":[]}}。'
        '不要输出解释、metadata 或 markdown 围栏；保持 brief 简短。'
    )


def extract_signals(
    user_message: str,
    character_reply: Optional[str] = None,
    character_core_snippet: Optional[str] = None,
    recent_context: Optional[List[Dict]] = None,
    temporal_context: Optional[Dict] = None,
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
        character_core_snippet, recent_context, temporal_context,
    )

    messages = [{'role': 'user', 'content': user_prompt}]
    max_tokens = SIGNAL_EXTRACTOR_MAX_TOKENS
    for attempt in range(2):
        try:
            call = invoke_structured_llm(
                domain='relationship_signals',
                create_chat_fn=create_chat,
                model=model or MODEL_MAIN,
                messages=messages,
                system=_OBSERVER_SYSTEM_PROMPT,
                max_tokens=max_tokens,
                schema_validator=_validate_signals_envelope,
                schema_name='relationship_signals',
                attempt=attempt + 1,
            )
        except Exception as e:
            return {'signals': [], 'raw': '', 'model': model or MODEL_MAIN,
                    'error': f'llm_call_failed: {e}'}
        raw_text = call.raw_text
        usage = call.usage
        stop = usage.get('stop_reason') or usage.get('finish_reason')
        parsed_result = call.parsed
        error = None
        error_reason = parsed_result.error_detail
        if stop in {'refusal', 'content_filter'}:
            error = 'model_refused'
            error_reason = f'stop_reason_{stop}'
        elif stop in {'max_tokens', 'length'}:
            error = 'truncated_response'
            error_reason = f'stop_reason_{stop}'
            max_tokens = min(max_tokens * 2, 2400)
        elif not raw_text or not raw_text.strip():
            error = 'empty_response'
            error_reason = 'empty_response'
        elif not parsed_result.ok:
            error, error_reason = _relationship_parse_error(parsed_result)
        if error is None:
            parsed = parsed_result.value
            break
        if attempt == 1 or stop in {'refusal', 'content_filter'}:
            return {'signals': [], 'raw': raw_text, 'model': model or MODEL_MAIN,
                    'error': error, 'error_reason': error_reason}
        messages = [{
            'role': 'user',
            'content': user_prompt + _retry_instruction(error, error_reason),
        }]

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
            'model': model or MODEL_MAIN, 'error': None,
            'error_reason': None}
