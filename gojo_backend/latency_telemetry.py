"""O(1) chat latency marks. Never stores prompt text or user content."""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Optional


_TRACE: ContextVar[Optional['LatencyTrace']] = ContextVar('gojo_latency_trace', default=None)

LATENCY_LOG_ENABLED = os.getenv('GOJO_LATENCY_LOG', '1').strip() not in ('0', 'false', 'False')
LATENCY_LOG_LEVEL = os.getenv('GOJO_LATENCY_LEVEL', 'INFO').strip().upper()


class LatencyTrace:
    def __init__(self, channel: str = 'chat:text'):
        self.channel = channel or 'chat:text'
        self.t0 = time.perf_counter()
        self.marks = {}
        self._open = {}

    def mark(self, name: str, ms=None):
        if ms is None:
            ms = (time.perf_counter() - self.t0) * 1000.0
        self.marks[name] = round(float(ms), 1)

    @contextmanager
    def span(self, name: str):
        start = time.perf_counter()
        self._open[name] = start
        try:
            yield self
        finally:
            self.marks[name] = round((time.perf_counter() - start) * 1000.0, 1)
            self._open.pop(name, None)

    def emit(self):
        self.marks.setdefault(
            'request_total_ms',
            round((time.perf_counter() - self.t0) * 1000.0, 1),
        )
        if not LATENCY_LOG_ENABLED:
            return
        order = (
            'availability', 'hot', 'recall', 'collapse', 'budget',
            'prompt', 'first_token', 'llm', 'total',
        )
        aliases = {
            'availability': 'availability_ms',
            'hot': 'hot_context_ms',
            'recall': 'recall_ms',
            'collapse': 'collapse_ms',
            'budget': 'context_budget_ms',
            'prompt': 'prompt_build_ms',
            'first_token': 'llm_first_token_ms',
            'llm': 'llm_total_ms',
            'total': 'request_total_ms',
        }
        parts = []
        for key in order:
            value = self.marks.get(key)
            if value is None and key == 'total':
                value = self.marks.get('request_total_ms')
            if value is None:
                continue
            parts.append(f'{key}={value:.1f}')
        line = f"[latency][{self.channel}] " + ' '.join(parts)
        print(line)
        return line


def current_trace() -> Optional[LatencyTrace]:
    return _TRACE.get()


def bind_trace(trace: Optional[LatencyTrace]):
    return _TRACE.set(trace)


def reset_trace(token):
    try:
        _TRACE.reset(token)
    except Exception:
        _TRACE.set(None)


@contextmanager
def chat_latency(channel: str):
    trace = LatencyTrace(channel)
    token = bind_trace(trace)
    try:
        yield trace
    finally:
        try:
            trace.emit()
        except Exception:
            pass
        reset_trace(token)


def mark_span(name: str):
    """Context manager if a trace is bound; otherwise a no-op."""
    trace = current_trace()
    if trace is None:
        @contextmanager
        def _noop():
            yield None
        return _noop()
    return trace.span(name)
