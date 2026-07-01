from __future__ import annotations
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
import pytest  # noqa: E402
from execution import regime_blended_sizer as rbs  # noqa: E402


def _meta(strats, dirs):
    return {'AAA': {'strategies': list(strats), 'directions': list(dirs), 'brackets': []}}


def test_gate_uses_raw_sizing_uses_scaled():
    # daily_weight (raw) vs eff_weight (size_scalar folded in) must differ on sizing,
    # while the gate stays on raw. Single strategy: S_adj = w * d.
    meta = _meta(['s1'], [1])
    sim = {'s1': {'s1': 1.0}}
    raw = {'s1': 2.0}
    scaled = {'s1': 6.0}                       # size_scalar = 3x
    gate, size, nb_g, nb_s = rbs._corr_adjusted_maps(meta, raw, scaled, sim)
    assert gate['AAA'] == pytest.approx(2.0)   # raw basis
    assert size['AAA'] == pytest.approx(6.0)   # scaled basis
    assert nb_g == 0 and nb_s == 0


def test_identical_when_scalars_one():
    meta = _meta(['s1', 's2'], [1, 1])
    sim = {'s1': {'s1': 1.0, 's2': 0.0}, 's2': {'s2': 1.0, 's1': 0.0}}
    w = {'s1': 3.0, 's2': 4.0}
    gate, size, _, _ = rbs._corr_adjusted_maps(meta, w, w, sim)   # eff == raw
    assert gate['AAA'] == pytest.approx(size['AAA'])
