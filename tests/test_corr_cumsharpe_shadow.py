from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest  # noqa: E402
from execution import regime_blended_sizer as rbs  # noqa: E402


def test_metrics_shape_and_rec_floor():
    gate_adj = {'A': 4.0, 'B': 2.0, 'C': 0.5}
    size_adj = {'A': 4.0, 'B': 2.0, 'C': 0.5}
    legacy_gate = {'A': 9.0, 'B': 5.0, 'C': 1.0}    # legacy floor 3.0 keeps A,B (2 names)
    live_w = {'A': 3.0, 'B': 1.0, 'C': 0.2}
    m = rbs._corr_cumsharpe_shadow_metrics(gate_adj, size_adj, legacy_gate, live_w,
                                           legacy_floor=3.0, lam=2.0, nav=100000.0, nb_g=0, nb_s=1)
    assert m['live_keep'] == 2
    # rec_floor keeps ~2 names under |S_adj| -> the 2nd largest = 2.0
    assert m['rec_floor'] == pytest.approx(2.0)
    assert m['backstop_fires'] == {'gate': 0, 'size': 1}
    assert set(m['dist'].keys()) == {'min', 'p25', 'median', 'p75', 'max'}
    assert m['dist']['max'] == pytest.approx(4.0)
