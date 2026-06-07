"""SP-7 Phase B Task 12 — √ln(N) union-N proposal math."""
from __future__ import annotations
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.universe_threshold_proposals import breadth_factor, propose_values


def test_factor_identity_at_sp500():
    assert breadth_factor(503, 503) == 1.0


def test_factor_curve():
    f = breadth_factor(5113, 503)
    assert abs(f - math.sqrt(math.log(5113) / math.log(503))) < 1e-12
    assert 1.15 < f < 1.20  # spec's ≈×1.17


def test_factor_guards():
    assert breadth_factor(0, 503) == 1.0      # degenerate → no scaling
    assert breadth_factor(503, 0) == 1.0
    assert breadth_factor(1, 1) == 1.0


def test_propose_values_clamped():
    bases = {'LOW_VOL': 3.0, 'TRANSITIONING': 4.0,
             'HIGH_VOL': 9.5, 'CRISIS': 6.0}
    out = propose_values(bases, factor=1.17)
    assert abs(out['LOW_VOL'] - 3.51) < 0.01
    assert out['HIGH_VOL'] == 10.0  # clamped at DB CHECK ceiling
    assert all(1.0 <= v <= 10.0 for v in out.values())
