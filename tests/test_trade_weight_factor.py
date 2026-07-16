"""Per-regime trade-count weight factor √(ln n / ln anchor) — pure.

Replaced the √(ln N) breadth factor 2026-07-16 (operator directive): trade count
is the realized, per-regime version of breadth + folds in estimation confidence.
Same functional form; the load-bearing behavioural difference from breadth is the
NEUTRAL-on-missing guard (a strategy with no bt_n must keep its weight, never be
zeroed) and the semantics of what n means.
"""
from __future__ import annotations
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import orthogonalization as og  # noqa: E402


def test_anchor_is_unity():
    # A strategy at the anchor trade count is unchanged (scale-neutral pivot).
    assert og.trade_weight_factor(1000, anchor=1000) == 1.0


def test_more_trades_upweight():
    f = og.trade_weight_factor(50000, anchor=1000)
    assert f > 1.0
    assert f == math.sqrt(math.log(50000) / math.log(1000))


def test_fewer_trades_downweight():
    # A low but valid count IS penalised — the intended low-confidence tilt.
    f = og.trade_weight_factor(100, anchor=1000)
    assert f < 1.0
    assert f == math.sqrt(math.log(100) / math.log(1000))


def test_monotone_increasing_in_n():
    xs = [og.trade_weight_factor(n, anchor=1000) for n in (2, 100, 1000, 5000, 50000)]
    assert xs == sorted(xs)


def test_tilt_is_gentle_over_the_eligible_range():
    # Eligibility forces n>=100, so the practical fleet range is bounded and mild —
    # this is why S_adj won't lurch and the floor recheck is a small move, not a reset.
    lo = og.trade_weight_factor(100, anchor=1000)      # activation floor
    hi = og.trade_weight_factor(53000, anchor=1000)    # richest observed regime
    assert 0.80 < lo < 0.83
    assert 1.20 < hi < 1.27


def test_MISSING_returns_unity_NOT_zero():
    # THE load-bearing guard. A strategy with no bt_n for the current regime (e.g.
    # its fleet re-backtest hasn't landed) must keep its raw weight. ln(1)=0 would
    # zero the weight and silently drop the strategy from the book.
    assert og.trade_weight_factor(None) == 1.0
    assert og.trade_weight_factor(1) == 1.0      # ln 1 = 0 → would be 0; guarded to 1.0
    assert og.trade_weight_factor(0) == 1.0
    assert og.trade_weight_factor(-5) == 1.0
    assert og.trade_weight_factor('x') == 1.0


def test_degenerate_anchor_returns_unity():
    assert og.trade_weight_factor(1000, anchor=1) == 1.0
    assert og.trade_weight_factor(1000, anchor=0) == 1.0


def test_float_n_coerced():
    assert og.trade_weight_factor(1000.0, anchor=1000) == 1.0


def test_folds_into_quadratic_form_degree_one_single_contributor():
    # The "sum appropriately" property the operator asked to verify: with the
    # factor folded into w, a single-contributor ticker gives
    #   num = f²w²d, q = f²w²  →  S_adj = f²w²d/√(f²w²) = d·f·|w|
    # so f enters DEGREE-1 there (linear), not squared. Verify against the real calc.
    contribs = {'T': [('S1', 1)]}
    sim = {'S1': {'S1': 1.0}}
    w = 0.8
    f = og.trade_weight_factor(50000, anchor=1000)     # some f > 1
    out, _ = og.corr_adjusted_net_sharpe(contribs, sim, {'S1': f * w})
    assert math.isclose(out['T'], f * w, rel_tol=1e-9)
