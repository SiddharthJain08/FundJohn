"""tests/test_pipeline_orchestrator_sentiment_step.py

D1 plan Task 9: wire the sentiment step into pipeline_orchestrator.py,
gated on OPENCLAW_SENTIMENT_INGEST=1. The step lives between `collect`
and `signals` so news/social scores are written before strategies read
them.

These tests reload the module to exercise the env-gated `_build_steps()`
factory. Cleanup (try/finally) ensures the gate env var doesn't leak
between tests.
"""
from __future__ import annotations

import importlib
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import pipeline_orchestrator as po  # noqa: E402


class SentimentStepGatingTests(unittest.TestCase):
    def setUp(self):
        # Snapshot the gate so the test is hermetic regardless of how the
        # outer environment was configured.
        self._had_gate = 'OPENCLAW_SENTIMENT_INGEST' in os.environ
        self._prev_gate = os.environ.get('OPENCLAW_SENTIMENT_INGEST')
        os.environ.pop('OPENCLAW_SENTIMENT_INGEST', None)

    def tearDown(self):
        os.environ.pop('OPENCLAW_SENTIMENT_INGEST', None)
        if self._had_gate:
            os.environ['OPENCLAW_SENTIMENT_INGEST'] = self._prev_gate
        # Reload back to the env-snapshot state so other tests see the
        # production list as it would be at import time.
        importlib.reload(po)

    def test_sentiment_step_present_when_gate_on(self):
        os.environ['OPENCLAW_SENTIMENT_INGEST'] = '1'
        try:
            mod = importlib.reload(po)
            keys = [k for k, _ in mod.STEPS]
            self.assertIn('sentiment', keys,
                          'sentiment step should be inserted when gate=1')
            collect_idx = keys.index('collect')
            sentiment_idx = keys.index('sentiment')
            signals_idx = keys.index('signals')
            self.assertEqual(sentiment_idx, collect_idx + 1,
                             'sentiment must sit immediately after collect')
            self.assertEqual(sentiment_idx, signals_idx - 1,
                             'sentiment must sit immediately before signals')
        finally:
            os.environ.pop('OPENCLAW_SENTIMENT_INGEST', None)

    def test_sentiment_step_absent_when_gate_off(self):
        # setUp already popped the env var; reload to be explicit.
        os.environ.pop('OPENCLAW_SENTIMENT_INGEST', None)
        mod = importlib.reload(po)
        keys = [k for k, _ in mod.STEPS]
        self.assertNotIn('sentiment', keys,
                         'sentiment step should NOT be present when gate is unset')


if __name__ == '__main__':
    unittest.main()
