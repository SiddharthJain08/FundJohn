from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
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
    # would-survive set = {A,B}; both clamped by the per-ticker cap
    # (CAP·(|S_adj|+1)·λ·NAV since 2026-08-12):
    #   A raw 133.3k vs cap CAP·(4+1)·200k; B raw 66.7k vs cap CAP·(2+1)·200k
    #   -> 2 binds (raws exceed the caps at any CAP <= 0.11);
    #   post-cap gross = CAP·((4+1)+(2+1))·200k.
    _cap = rbs.PER_TICKER_CAP_SHARPE_FRAC
    assert m['would_keep'] == 2
    assert m['cap_binds'] == 2
    assert m['gross_after_cap_frac'] == pytest.approx(_cap * 8.0 * 2.0 * 100000.0 / 200000.0)
    assert m['sign_flips'] == []          # all longs, no direction divergence


def test_sign_flip_detected():
    # legacy ticker_w says SHORT (-3) on A but corr size_adj says LONG (+4) -> flip.
    gate_adj = {'A': 4.0}
    size_adj = {'A': 4.0}
    legacy_gate = {'A': 9.0}
    live_w = {'A': -3.0}
    m = rbs._corr_cumsharpe_shadow_metrics(gate_adj, size_adj, legacy_gate, live_w,
                                           legacy_floor=3.0, lam=2.0, nav=100000.0, nb_g=0, nb_s=0)
    assert m['sign_flips'] == ['A']
