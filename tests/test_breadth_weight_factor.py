"""Grinold breadth weight factor √(ln N / ln anchor) — pure."""
from __future__ import annotations
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

from execution import orthogonalization as og  # noqa: E402


def test_anchor_is_unity():
    # A strategy at the anchor breadth is unchanged.
    assert og.breadth_weight_factor(500, anchor=500) == 1.0


def test_broader_universe_upweights():
    # Direction (operator 2026-07-01): broader universe -> MORE weight.
    f = og.breadth_weight_factor(5180, anchor=500)
    assert f > 1.0
    assert f == math.sqrt(math.log(5180) / math.log(500))


def test_narrower_universe_downweights():
    f = og.breadth_weight_factor(50, anchor=500)
    assert f < 1.0
    assert f == math.sqrt(math.log(50) / math.log(500))


def test_monotone_increasing_in_n():
    xs = [og.breadth_weight_factor(n, anchor=500) for n in (2, 50, 500, 1000, 5180)]
    assert xs == sorted(xs)


def test_guards_return_unity():
    # ln(N)<=0 or missing N or degenerate anchor -> fail-safe 1.0 (no reweight).
    assert og.breadth_weight_factor(1) == 1.0
    assert og.breadth_weight_factor(0) == 1.0
    assert og.breadth_weight_factor(-5) == 1.0
    assert og.breadth_weight_factor(None) == 1.0
    assert og.breadth_weight_factor('x') == 1.0
    assert og.breadth_weight_factor(500, anchor=1) == 1.0
    assert og.breadth_weight_factor(500, anchor=0) == 1.0


def test_float_n_coerced():
    assert og.breadth_weight_factor(500.0, anchor=500) == 1.0
