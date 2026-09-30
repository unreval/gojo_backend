"""Canonical deterministic boundary for structured LLM output.

This module deliberately does not repair model output or call another model.
It can unwrap one complete JSON container from harmless presentation text, then
hands the decoded object to the caller's existing schema/domain validator.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from typing import Any, Callable, Dict, Mapping, Optional


PARSER_VERSION = 'v1'


class StructuredOutputError(ValueError):
    """A failed canonical structured-output parse."""

    def __init__(self, code: str, *, result=None):
        super().__init__(code)
        self.code = code
        self.result = result


@dataclass(frozen=True)
class StructuredParseResult:
    """Safe parse metadata; it intentionally never retains response text."""

    value: Any = None
    error_code: Optional[str] = None
    error_detail: Optional[str] = None
    schema_details: Optional[Dict[str, Any]] = None
    candidate_count: int = 0
    distinct_candidate_count: int = 0
    selected_candidate: Optional[int] = None
    extraction_mode: Optional[str] = None
    schema_name: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error_code is None

    def telemetry(self) -> Dict[str, Any]:
        return {
            'parser_version': PARSER_VERSION,
            'ok': self.ok,
            'error_code': self.error_code,
            'error_detail': self.error_detail,
            'candidate_count': self.candidate_count,
            'distinct_candidate_count': self.distinct_candidate_count,
            'selected_candidate': self.selected_candidate,
            'extraction_mode': self.extraction_mode,
            'schema_name': self.schema_name,
        }


@dataclass(frozen=True)
class StructuredLLMCall:
    """One LLM call plus the canonical parse result and safe telemetry."""

    raw_text: str
    usage: Dict[str, Any]
    parsed: StructuredParseResult
    telemetry: Dict[str, Any]


@dataclass(frozen=True)
class _Candidate:
    index: int
    start: int
    end: int
    value: Any
    extraction_mode: str


def _reject_nonstandard_constant(value):
    raise ValueError(f'nonstandard_json_constant:{value}')


def _finite_float(value):
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError('nonfinite_json_number')
    return parsed


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate_json_key:{key}')
        result[key] = value
    return result


_STRICT_DECODER = json.JSONDecoder(
    object_pairs_hook=_reject_duplicate_keys,
    parse_constant=_reject_nonstandard_constant,
    parse_float=_finite_float,
)


def _decode_container(text: str):
    return _STRICT_DECODER.decode(text)


def _balanced_container_end(text: str, start: int) -> Optional[int]:
    """Find one full object/array while respecting JSON strings and escapes."""
    if start < 0 or start >= len(text) or text[start] not in '{[':
        return None
    pairs = {'}': '{', ']': '['}
    stack = []
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in '{[':
            stack.append(char)
        elif char in '}]':
            if not stack or stack[-1] != pairs[char]:
                return None
            stack.pop()
            if not stack:
                return index + 1
    return None


def _skip_quoted_text(text: str, start: int) -> int:
    """Skip a quoted prose/JSON string so braces inside it are not candidates."""
    escaped = False
    for index in range(start + 1, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == '"':
            return index + 1
    return len(text)


def _markdown_fence_ranges(text: str):
    ranges = []
    opened_at = None
    for match in re.finditer(r'```[^`\r\n]*', text):
        if opened_at is None:
            opened_at = match.end()
        else:
            ranges.append((opened_at, match.start()))
            opened_at = None
    return ranges


def _extraction_mode(text: str, start: int, end: int, fence_ranges) -> str:
    if any(left <= start and end <= right for left, right in fence_ranges):
        return 'markdown_fence'
    if not text[:start].strip() and not text[end:].strip():
        return 'raw'
    return 'balanced_object'


def _top_level_containers(text: str):
    """Decode only outer containers; nested objects never become candidates."""
    candidates = []
    invalid_count = 0
    incomplete = False
    fence_ranges = _markdown_fence_ranges(text)
    index = 0
    while index < len(text):
        char = text[index]
        if char == '"':
            index = _skip_quoted_text(text, index)
            continue
        if char not in '{[':
            if char in '}]':
                invalid_count += 1
            index += 1
            continue
        end = _balanced_container_end(text, index)
        if end is None:
            incomplete = True
            break
        candidate_text = text[index:end]
        try:
            value = _decode_container(candidate_text)
        except (TypeError, ValueError, RecursionError):
            invalid_count += 1
        else:
            candidates.append(_Candidate(
                index=len(candidates),
                start=index,
                end=end,
                value=value,
                extraction_mode=_extraction_mode(text, index, end, fence_ranges),
            ))
        index = end
    return candidates, invalid_count, incomplete


def _canonical_json(value) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        allow_nan=False,
    )


def _schema_error_detail(exc: Exception) -> str:
    # Arbitrary validator messages may contain conversation text. Only stable
    # error codes belong in telemetry; details stay with the domain exception.
    code = getattr(exc, 'code', None)
    if isinstance(code, str) and re.fullmatch(r'[a-zA-Z0-9_.:-]{1,180}', code):
        return code
    return exc.__class__.__name__


def _schema_error_details(exc: Exception) -> Optional[Dict[str, Any]]:
    details = getattr(exc, 'details', None)
    return dict(details) if isinstance(details, dict) else None


def parse_structured_output(
    raw_text,
    *,
    schema_validator: Optional[Callable[[Dict[str, Any]], Any]] = None,
    schema_name: str = 'object',
    root_type: str = 'object',
) -> StructuredParseResult:
    """Parse exactly one distinct top-level JSON value without repairing it.

    Presentation-only wrappers (a markdown fence or prose before/after one
    object) are accepted.  Two different valid roots are ambiguous: choosing
    one would let a caller silently ignore model output, so the result is
    fail-closed.
    """
    text = raw_text if isinstance(raw_text, str) else ''
    text = text.strip()
    if not text:
        return StructuredParseResult(
            error_code='empty_response', schema_name=schema_name)

    candidates, invalid_count, incomplete = _top_level_containers(text)
    candidate_count = len(candidates)
    if incomplete:
        return StructuredParseResult(
            error_code='incomplete_json', candidate_count=candidate_count,
            schema_name=schema_name)
    if invalid_count:
        return StructuredParseResult(
            error_code='invalid_json', candidate_count=candidate_count,
            schema_name=schema_name)
    if not candidates:
        return StructuredParseResult(
            error_code='no_top_level_json_object', schema_name=schema_name)

    canonical_values = {}
    for candidate in candidates:
        canonical_values.setdefault(_canonical_json(candidate.value), candidate)
    distinct_count = len(canonical_values)
    if distinct_count != 1:
        return StructuredParseResult(
            error_code='multiple_distinct_json_objects',
            candidate_count=candidate_count,
            distinct_candidate_count=distinct_count,
            schema_name=schema_name)

    selected = next(iter(canonical_values.values()))
    value = selected.value
    if ((root_type == 'object' and not isinstance(value, dict))
            or (root_type == 'array' and not isinstance(value, list))):
        return StructuredParseResult(
            error_code='root_not_' + root_type,
            candidate_count=candidate_count,
            distinct_candidate_count=distinct_count,
            schema_name=schema_name)
    if schema_validator is not None:
        try:
            validated = schema_validator(value)
        except Exception as exc:
            return StructuredParseResult(
                error_code='schema_validation_failed',
                error_detail=_schema_error_detail(exc),
                schema_details=_schema_error_details(exc),
                candidate_count=candidate_count,
                distinct_candidate_count=distinct_count,
                selected_candidate=selected.index,
                extraction_mode=selected.extraction_mode,
                schema_name=schema_name)
        if validated is not None:
            value = validated
    return StructuredParseResult(
        value=value,
        candidate_count=candidate_count,
        distinct_candidate_count=distinct_count,
        selected_candidate=selected.index,
        extraction_mode=selected.extraction_mode,
        schema_name=schema_name)


def _safe_usage(usage) -> Dict[str, Any]:
    return dict(usage) if isinstance(usage, Mapping) else {}


def invoke_structured_llm(
    *,
    domain: str,
    create_chat_fn,
    model: str,
    messages,
    system: Optional[str] = None,
    max_tokens: Optional[int] = None,
    schema_validator: Optional[Callable[[Dict[str, Any]], Any]] = None,
    schema_name: str = 'object',
    root_type: str = 'object',
    attempt: int = 1,
) -> StructuredLLMCall:
    """Call an LLM once and attach canonical parser telemetry to its usage."""
    kwargs = {
        'model': model,
        'messages': messages,
    }
    if system is not None:
        kwargs['system'] = system
    if max_tokens is not None:
        kwargs['max_tokens'] = max_tokens
    try:
        raw_text, raw_usage = create_chat_fn(**kwargs)
    except Exception as exc:
        print(f'[structured_output] domain={domain} attempt={attempt} '
              f'ok=False error=llm_call_failed exception={type(exc).__name__}')
        raise
    raw_text = raw_text if isinstance(raw_text, str) else ''
    usage = _safe_usage(raw_usage)
    stop_reason = usage.get('stop_reason') or usage.get('finish_reason')
    if stop_reason in {'refusal', 'content_filter'}:
        parsed = StructuredParseResult(
            error_code='model_refused', schema_name=schema_name)
    elif stop_reason in {'length', 'max_tokens'}:
        parsed = StructuredParseResult(
            error_code='truncated_response', schema_name=schema_name)
    else:
        parsed = parse_structured_output(
            raw_text,
            schema_validator=schema_validator,
            schema_name=schema_name,
            root_type=root_type,
        )
    telemetry = {
        'domain': domain,
        **parsed.telemetry(),
        'stop_reason': stop_reason,
        'output_tokens': usage.get('output_tokens'),
        'response_id': usage.get('response_id'),
        'chars': len(raw_text),
    }
    usage['structured_output'] = telemetry
    call = StructuredLLMCall(
        raw_text=raw_text,
        usage=usage,
        parsed=parsed,
        telemetry=telemetry,
    )
    emit_structured_output_telemetry(call, attempt=attempt)
    return call


def emit_structured_output_telemetry(call: StructuredLLMCall, *, attempt: int,
                                     logger=None):
    """Log shape-only diagnostics; raw model output is deliberately omitted."""
    logger = logger or print
    telemetry = call.telemetry
    logger(
        '[structured_output] '
        f'domain={telemetry.get("domain") or "unknown"} '
        f'attempt={attempt} ok={telemetry.get("ok")} '
        f'error={telemetry.get("error_code") or "-"} '
        f'reason={telemetry.get("error_detail") or "-"} '
        f'candidate_count={telemetry.get("candidate_count", 0)} '
        f'distinct_candidate_count={telemetry.get("distinct_candidate_count", 0)} '
        f'extraction_mode={telemetry.get("extraction_mode") or "-"} '
        f'schema={telemetry.get("schema_name") or "-"} '
        f'stop_reason={telemetry.get("stop_reason") or "unknown"} '
        f'output_tokens={telemetry.get("output_tokens")} '
        f'chars={telemetry.get("chars", 0)} '
        f'response_id={telemetry.get("response_id") or "-"}'
    )
