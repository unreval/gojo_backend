"""统一 LLM 客户端 adapter —— 让代码不用管走 Anthropic 还是 DeepSeek

设计原则:
- 主体角色扮演(chat/voice/story)—— 走 Anthropic,直接用它 SDK,保留 prompt cache
- 中文辅助任务(记忆提取/日记/记账短评)—— 通过这里,可以走 DeepSeek 省钱

用法:
    from ai_client import create_chat
    from config import MODEL_CN_AUX
    text, usage = create_chat(
        model=MODEL_CN_AUX,
        messages=[{'role': 'user', 'content': '...'}],
        max_tokens=400,
    )

按 model 前缀分发:
- 'claude-*' → Anthropic
- 'deepseek-*' → DeepSeek(OpenAI 兼容 API)
"""
import json
import requests
import anthropic
from config import ANTHROPIC_KEY, DEEPSEEK_KEY, DEEPSEEK_BASE_URL

_anthropic_client = None


def _get_anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    return _anthropic_client


def extract_text(response, sep=''):
    """从 Anthropic Messages 响应中只拼接 type=text 的块。

    Extended Thinking 开启后 content 可能是:
        [ThinkingBlock(...), TextBlock(text="..."), ToolUseBlock(...), ...]
    ThinkingBlock 没有 .text，直接访问会 AttributeError。
    所有 Claude 响应解析都应走这里，不要再用 response.content[0].text。
    """
    if response is None:
        return ''
    content = getattr(response, 'content', None)
    if not content:
        return ''
    parts = []
    for block in content:
        if isinstance(block, dict):
            if block.get('type') == 'text' and block.get('text'):
                parts.append(block['text'])
            continue
        if getattr(block, 'type', None) == 'text':
            text = getattr(block, 'text', None)
            if text:
                parts.append(text)
    return sep.join(parts)


def create_chat(model, messages, system=None, max_tokens=1000, temperature=None, *, max_retries=None):
    """统一接口,自动按 model 前缀分发到 Anthropic 或 DeepSeek。

    Args:
        model: 'claude-*' 走 Anthropic,'deepseek-*' 走 DS
        messages: [{'role': 'user'|'assistant', 'content': str}]
        system: str 或 None(简化版,不支持 blocks + cache_control)
        max_tokens: 输出上限
        temperature: None = provider 默认
        max_retries: 可选 SDK 重试上限；Slow Loop 使用 0，其他调用保持默认。

    Returns:
        (raw_text: str, usage_info: dict {input_tokens, output_tokens})

    Raises:
        RuntimeError: provider 报错时抛出
    """
    if model.startswith('claude-') or model.startswith('anthropic-'):
        if max_retries is None:
            return _call_anthropic(model, messages, system, max_tokens, temperature)
        return _call_anthropic(
            model, messages, system, max_tokens, temperature, max_retries=max_retries)
    elif model.startswith('deepseek-'):
        return _call_deepseek(model, messages, system, max_tokens, temperature)
    else:
        raise ValueError(f'未知的 model 前缀: {model}')


def response_metadata(response):
    """Diagnostic fields only: never include image data or thinking content."""
    usage = getattr(response, 'usage', None)
    return {
        'input_tokens': getattr(usage, 'input_tokens', 0),
        'output_tokens': getattr(usage, 'output_tokens', 0),
        'stop_reason': getattr(response, 'stop_reason', None),
        'response_id': getattr(response, 'id', None),
        'content_types': [
            block.get('type') if isinstance(block, dict) else getattr(block, 'type', None)
            for block in (getattr(response, 'content', None) or [])
        ],
    }


def _call_anthropic(model, messages, system, max_tokens, temperature, *, max_retries=None):
    client = _get_anthropic()
    if max_retries is not None:
        client = client.with_options(max_retries=max_retries)
    kwargs = {
        'model': model,
        'max_tokens': max_tokens,
        'messages': messages,
    }
    if system:
        kwargs['system'] = system
    if temperature is not None:
        kwargs['temperature'] = temperature
    resp = client.messages.create(**kwargs)
    text = extract_text(resp)
    return text, {
        **response_metadata(resp),
        'provider': 'anthropic',
    }


def _call_deepseek(model, messages, system, max_tokens, temperature):
    if not DEEPSEEK_KEY:
        raise RuntimeError('DEEPSEEK_KEY 未配置,无法调用 DeepSeek')

    payload_messages = []
    if system:
        payload_messages.append({'role': 'system', 'content': system})
    payload_messages.extend(messages)

    payload = {
        'model': model,
        'messages': payload_messages,
        'max_tokens': max_tokens,
    }
    if temperature is not None:
        payload['temperature'] = temperature

    try:
        resp = requests.post(
            f'{DEEPSEEK_BASE_URL.rstrip("/")}/chat/completions',
            headers={
                'Authorization': f'Bearer {DEEPSEEK_KEY}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=90,
        )
    except requests.RequestException as e:
        raise RuntimeError(f'DeepSeek 网络异常: {e}')

    if resp.status_code != 200:
        raise RuntimeError(f'DeepSeek API {resp.status_code}: {resp.text[:300]}')
    data = resp.json()
    try:
        choice = data['choices'][0]
        msg = choice.get('message', {})
        text = msg.get('content') or ''
        finish = choice.get('finish_reason', '')

        # Reasoning is not a final answer. Preserve the actual content so the
        # canonical structured-output boundary sees empty/truncated responses.
        reasoning = msg.get('reasoning_content') or ''
        if finish == 'length':
            print(f'[ai_client] ⚠️ {model} 输出被 max_tokens 截断'
                  f'(正文 {len(text)} 字{", 思考 " + str(len(reasoning)) + " 字" if reasoning else ""})'
                  f' → 请调大 max_tokens')
        elif not text.strip():
            print(f'[ai_client] empty_response model={model} '
                  f'finish={finish} reasoning_chars={len(reasoning)}')
    except (KeyError, IndexError):
        raise RuntimeError(f'DeepSeek 响应结构异常: {json.dumps(data)[:300]}')
    usage = data.get('usage', {})
    return text, {
        'input_tokens': usage.get('prompt_tokens', 0),
        'output_tokens': usage.get('completion_tokens', 0),
        'finish_reason': finish,
        'response_id': data.get('id'),
        'provider': 'deepseek',
    }
