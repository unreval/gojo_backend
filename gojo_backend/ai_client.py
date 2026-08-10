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


def create_chat(model, messages, system=None, max_tokens=1000, temperature=None):
    """统一接口,自动按 model 前缀分发到 Anthropic 或 DeepSeek。

    Args:
        model: 'claude-*' 走 Anthropic,'deepseek-*' 走 DS
        messages: [{'role': 'user'|'assistant', 'content': str}]
        system: str 或 None(简化版,不支持 blocks + cache_control)
        max_tokens: 输出上限
        temperature: None = provider 默认

    Returns:
        (raw_text: str, usage_info: dict {input_tokens, output_tokens})

    Raises:
        RuntimeError: provider 报错时抛出
    """
    if model.startswith('claude-') or model.startswith('anthropic-'):
        return _call_anthropic(model, messages, system, max_tokens, temperature)
    elif model.startswith('deepseek-'):
        return _call_deepseek(model, messages, system, max_tokens, temperature)
    else:
        raise ValueError(f'未知的 model 前缀: {model}')


def _call_anthropic(model, messages, system, max_tokens, temperature):
    client = _get_anthropic()
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
    text = resp.content[0].text if resp.content else ''
    return text, {
        'input_tokens': getattr(resp.usage, 'input_tokens', 0),
        'output_tokens': getattr(resp.usage, 'output_tokens', 0),
        'provider': 'anthropic',
    }


def _salvage_from_reasoning(reasoning: str) -> str:
    """从 reasoning_content 里抠出 JSON。

    推理模型有时会正常结束(finish=stop)但 content 留空,
    把结论写在了思考过程里。这时候答案其实还在,只是位置不对。
    与其整轮丢掉,不如把 JSON 捞回来。

    找【最外层完整的花括号块】,用括号配平判断,能扛住嵌套。
    """
    if not reasoning:
        return ''
    start = reasoning.find('{')
    if start < 0:
        return ''
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(reasoning)):
        ch = reasoning[i]
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                candidate = reasoning[start:i + 1]
                try:
                    json.loads(candidate)      # 能解析才算数
                    return candidate
                except Exception:
                    return ''
    return ''


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

        # ★ 推理模型(deepseek-v4-flash 等)会把思考过程放在 reasoning_content。
        #   有两种失败方式,处理方式完全不同:
        #     finish=length → 真的被 max_tokens 截断,要调大额度
        #     finish=stop   → 正常结束,但模型把答案写进了 reasoning、content 留空
        #                     这时候答案其实还在,去 reasoning 里捞回来
        reasoning = msg.get('reasoning_content') or ''

        if reasoning and not text.strip():
            if finish == 'length':
                print(f'[ai_client] ⚠️ {model} 被 max_tokens({max_tokens}) 截断,'
                      f'思考占了 {len(reasoning)} 字、正文 0 字 → 需要调大额度')
            else:
                # 正常结束却没正文 → 答案多半藏在思考里,尝试抠出来
                salvaged = _salvage_from_reasoning(reasoning)
                if salvaged:
                    print(f'[ai_client] ♻️ {model} 正文为空(finish={finish}),'
                          f'已从思考内容里救回 {len(salvaged)} 字')
                    text = salvaged
                else:
                    print(f'[ai_client] ⚠️ {model} 正文为空且思考里也没有可用结果'
                          f'(finish={finish}, 思考 {len(reasoning)} 字): {reasoning[:200]}')
        elif finish == 'length':
            print(f'[ai_client] ⚠️ {model} 输出被 max_tokens 截断'
                  f'(正文 {len(text)} 字{", 思考 " + str(len(reasoning)) + " 字" if reasoning else ""})'
                  f' → 请调大 max_tokens')
        elif not text.strip():
            # ★ 既不是截断也没有思考内容,却什么都没返回 —— 把原始响应整个打出来,
            #   不然完全没法判断是内容过滤、限流、还是别的什么
            print(f'[ai_client] ❌ {model} 返回空内容且原因不明,'
                  f'finish={finish},完整响应: {json.dumps(data, ensure_ascii=False)[:800]}')
    except (KeyError, IndexError):
        raise RuntimeError(f'DeepSeek 响应结构异常: {json.dumps(data)[:300]}')
    usage = data.get('usage', {})
    return text, {
        'input_tokens': usage.get('prompt_tokens', 0),
        'output_tokens': usage.get('completion_tokens', 0),
        'finish_reason': finish,
        'provider': 'deepseek',
    }