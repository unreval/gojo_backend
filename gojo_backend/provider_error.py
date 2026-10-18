"""Safe provider failure metadata shared by generation and durable jobs."""
import hashlib
import re
from urllib.parse import urlsplit


_TOKEN = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,95}$')
_HOST = re.compile(r'^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$')
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_ERROR_CODES = frozenset({
    'authentication_error', 'permission_error', 'invalid_request_error',
    'rate_limit_error', 'api_error', 'overloaded_error',
    'permission_denied', 'authentication_failed', 'invalid_api_key',
})


def model_for_provider_detection(model):
    """Remove one nonempty relay label for provider checks only."""
    if model.startswith('['):
        return re.sub(r'^\[[^\[\]\r\n]+\]', '', model, count=1)
    return model


def _token(value):
    if not isinstance(value, str) or not _TOKEN.fullmatch(value):
        return None
    if re.match(r'(?i)^(?:sk|pk|key|token|bearer)[_-]', value):
        return None
    return value


def _host(value):
    if value is None:
        return None
    try:
        host = urlsplit(str(value)).hostname
    except Exception:
        return None
    return host if host and _HOST.fullmatch(host) else None


def _error_code(value):
    code = _token(value)
    return code if code in _ERROR_CODES else None


def _request_id(value):
    if not isinstance(value, str) or not value:
        return None
    if re.fullmatch(r'sha256:[0-9a-f]{16}', value):
        return value
    if re.fullmatch(r'req_[A-Za-z0-9_-]{1,80}', value):
        return value
    return 'sha256:' + hashlib.sha256(value.encode('utf-8')).hexdigest()[:16]


def _message(status):
    # Provider prose can echo prompts or credentials. Log a fixed reason only.
    return {
        401: 'authentication failed',
        403: 'permission denied',
        429: 'rate limited',
    }.get(status, 'provider unavailable' if isinstance(status, int) and status >= 500 else None)


class ProviderHTTPError(RuntimeError):
    """HTTP failure whose string representation contains no response body."""

    def __init__(self, status_code, *, provider, model, base_url=None,
                 code=None, message=None, request_id=None):
        self.status_code = status_code
        self.provider = provider
        self.model = model
        self.base_url = base_url
        self.error_code = _error_code(code)
        self.error_message = _message(status_code)
        self.request_id = _request_id(request_id)
        super().__init__(f'{provider} API request failed (HTTP {status_code})')


def is_auth_error(error):
    """401/403 cannot recover through an unchanged job retry."""
    try:
        status = getattr(error, 'status_code', None)
    except Exception:
        status = None
    return status in (401, 403) or type(error).__name__ in (
        'AuthenticationError', 'PermissionDeniedError')


def is_retryable_transport(error):
    """Allow bounded retries for selected HTTP or connection setup failures.

    HTTP failures do not prove that the upstream did no work or billing.
    Read/write failures and unknown connection phases are excluded here;
    an SDK APIConnectionError alone does not identify the failed phase.
    SDK timeouts are excluded here; this does not disable SDK-level retries.
    """
    for _ in range(3):
        if error is None:
            return False
        name = type(error).__name__
        if (isinstance(error, TimeoutError) or name in (
                'APITimeoutError', 'Timeout', 'ReadTimeout', 'WriteTimeout',
                'ReadError', 'WriteError', 'RemoteProtocolError')):
            return False
        status = getattr(error, 'status_code', None)
        if isinstance(status, int) and not isinstance(status, bool):
            if status in _RETRYABLE_STATUSES:
                return True
            if 400 <= status <= 599:
                return False
        if name in ('ConnectError', 'ConnectTimeout'):
            return True
        error = getattr(error, '__cause__', None)
    return False


def call_with_transport_retry(call, *, attempts, backoff_seconds,
                              sleep=None, on_retry=None, random_=None):
    """Repeat ``call`` only for failures allowed by the transport policy.

    ``sleep``/``random_`` are injectable so the backoff is testable without
    real waiting. The final attempt always re-raises.
    """
    import random as _random
    import time as _time
    sleep = sleep or _time.sleep
    random_ = random_ or _random
    for attempt in range(max(1, attempts)):
        try:
            return call()
        except Exception as exc:
            if attempt + 1 >= max(1, attempts) or not is_retryable_transport(exc):
                raise
            delay = backoff_seconds * (2 ** attempt)
            delay += random_.uniform(0, delay / 2) if delay else 0
            if on_retry:
                on_retry(attempt + 1, delay, exc)
            sleep(delay)


def retry_delay_seconds(error):
    """Bound retries for temporary provider failures, including wrapped transport errors."""
    for _ in range(3):
        if error is None:
            break
        status = getattr(error, 'status_code', None)
        if status == 429:
            return 120
        if isinstance(status, int) and not isinstance(status, bool) and 500 <= status <= 599:
            return 60
        if isinstance(error, TimeoutError) or type(error).__name__ in (
                'Timeout', 'ConnectTimeout', 'ReadTimeout', 'APITimeoutError',
                'ConnectionError', 'APIConnectionError'):
            return 60
        error = getattr(error, '__cause__', None)
    return None


def diagnostics(error, *, model=None, provider=None, base_url=None):
    """Return bounded transport fields and a fixed, content-free reason."""
    try:
        return _diagnostics(error, model=model, provider=provider,
                            base_url=base_url)
    except Exception:
        return {
            'provider': _token(provider), 'status': None, 'code': None,
            'message': None, 'model': _token(model),
            'base_url_host': _host(base_url), 'request_id': None,
        }


def _diagnostics(error, *, model, provider, base_url):
    body = getattr(error, 'body', None)
    body_error = body.get('error') if isinstance(body, dict) else None
    body_error = body_error if isinstance(body_error, dict) else {}
    response = getattr(error, 'response', None)
    headers = getattr(response, 'headers', None) or {}
    status = getattr(error, 'status_code', None)
    if not isinstance(status, int) or isinstance(status, bool) or not 100 <= status <= 599:
        status = None
    resolved_model = model or getattr(error, 'model', None)
    resolved_provider = provider or getattr(error, 'provider', None)
    if not resolved_provider and isinstance(resolved_model, str):
        routing_model = model_for_provider_detection(resolved_model)
        resolved_provider = ('anthropic' if routing_model.startswith(('claude-', 'anthropic-'))
                             else 'deepseek' if routing_model.startswith('deepseek-') else None)
    request = getattr(error, 'request', None)
    request_url = getattr(request, 'url', None)
    return {
        'provider': _token(resolved_provider),
        'status': status,
        'code': _error_code(getattr(error, 'error_code', None)
                            or body_error.get('code') or body_error.get('type')
                            or getattr(error, 'type', None)),
        'message': _message(status),
        'model': _token(resolved_model),
        'base_url_host': (_host(request_url) or _host(getattr(error, 'base_url', None))
                          or _host(base_url)),
        'request_id': _request_id(getattr(error, 'request_id', None)
                                  or headers.get('request-id') or headers.get('x-request-id')),
    }
