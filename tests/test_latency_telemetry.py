# -*- coding: utf-8 -*-
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(__file__))
BACKEND = os.path.join(ROOT, 'gojo_backend')
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

import latency_telemetry as lt  # noqa: E402


class LatencyTelemetryTests(unittest.TestCase):
    def test_emit_has_required_fields_without_prompt(self):
        trace = lt.LatencyTrace('chat:text')
        with trace.span('availability'):
            pass
        with trace.span('hot'):
            pass
        with trace.span('recall'):
            pass
        with trace.span('collapse'):
            pass
        with trace.span('budget'):
            pass
        with trace.span('prompt'):
            pass
        trace.mark('first_token', 12.5)
        with trace.span('llm'):
            pass
        line = trace.emit()
        self.assertIn('[latency][chat:text]', line)
        for key in (
            'availability=', 'hot=', 'recall=', 'collapse=',
            'budget=', 'prompt=', 'first_token=', 'llm=', 'total=',
        ):
            self.assertIn(key, line)
        self.assertNotIn('system', line.lower())
        self.assertNotIn('user:', line)

    def test_bind_does_not_raise_without_trace(self):
        with lt.mark_span('hot'):
            pass
