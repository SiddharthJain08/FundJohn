"""tests/test_redeploy_steps.py — lock the collect-free redeploy.

Intraday-regime redeploys must NOT run `collect`: the prior EOD is already
filled (strategies use it), so re-collecting only slows intraday execution.
This guards against `collect` being reintroduced into the redeploy step list.
"""
from __future__ import annotations
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'scripts' / 'redeploy_pipeline.py').read_text()


class TestRedeploySteps(unittest.TestCase):
    def test_redeploy_steps_exclude_collect(self):
        m = re.search(r"REDEPLOY_STEPS\s*=\s*'([^']+)'", SRC)
        self.assertIsNotNone(m, "REDEPLOY_STEPS constant not found")
        steps = [s.strip() for s in m.group(1).split(',')]
        self.assertNotIn('collect', steps)
        self.assertEqual(set(steps), {'signals', 'handoff', 'trade', 'alpaca', 'reconcile'})


if __name__ == '__main__':
    unittest.main()
