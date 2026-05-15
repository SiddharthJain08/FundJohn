"""Phase 1B — bar-resolution oracle tests.

Cases adapted from kernc/backtesting.py test corpus (AGPL — code not copied,
behavior contract reproduced).  Run our backtest path against these inputs;
assert the broker resolution matches the documented expectation.

If our backtest engine's signature changes, update _run_bracket()."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import pytest
from backtest._oracle_helpers import ohlcv, Bracket

# Adapter: change ONLY this function if quick_backtest.py's signature evolves.
def _run_bracket(prices, bracket: Bracket):
    """Run one long bracket through our engine, return dict with keys:
       fill_price, exit_price, exit_reason ('stop'|'target'|'eod'), bars_held."""
    from backtest.quick_backtest import run_single_bracket
    return run_single_bracket(prices, bracket.entry, bracket.stop, bracket.target, bracket.qty)


def test_stop_before_target_when_both_in_same_bar_long():
    """Long bracket: bar where high>=target AND low<=stop must resolve as STOP, not target.
       (kernc 0.6.0 changelog: 'SL is checked before TP when both conditions met'.)"""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),  # entry bar — fill at limit
        (100.2, 105.0, 95.0, 100.0, 5000),  # spike both ways
    ])
    out = _run_bracket(p, Bracket(entry=100.2, stop=98.0, target=104.0))
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == pytest.approx(98.0)


def test_target_hit_resolves_when_stop_not_hit_same_bar():
    """High>=target but low>stop: must resolve TARGET."""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),
        (100.2, 104.5, 99.0, 102.0, 5000),
    ])
    out = _run_bracket(p, Bracket(entry=100.2, stop=98.0, target=104.0))
    assert out["exit_reason"] == "target"
    assert out["exit_price"] == pytest.approx(104.0)


def test_no_fill_when_limit_entry_never_touched():
    """Entry limit below the day's range: bracket never fires; bars_held == 0."""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),
        (100.2, 100.4, 100.1, 100.3, 5000),
    ])
    out = _run_bracket(p, Bracket(entry=95.0, stop=90.0, target=99.0))
    assert out["fill_price"] is None
    assert out["bars_held"] == 0


def test_eod_exit_when_neither_stop_nor_target_hit():
    """Bracket fills, neither barrier touched in remaining bars: exit at last close."""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),
        (100.2, 100.6, 100.0, 100.3, 1000),
        (100.3, 100.7, 100.1, 100.4, 1000),
    ])
    out = _run_bracket(p, Bracket(entry=100.2, stop=95.0, target=110.0))
    assert out["exit_reason"] == "eod"
    assert out["exit_price"] == pytest.approx(100.4)


def test_gap_open_through_stop_fills_at_open_not_stop():
    """Bar opens below stop: exit must be at the bar's OPEN, not the stop level
       (kernc broker behavior — gap losses are not slippage-protected by the stop)."""
    p = ohlcv([
        (100.0, 100.5, 99.5, 100.2, 1000),
        (95.0,  96.0,  94.0, 95.5,  5000),  # gap-down through stop=98
    ])
    out = _run_bracket(p, Bracket(entry=100.2, stop=98.0, target=104.0))
    assert out["exit_reason"] == "stop"
    assert out["exit_price"] == pytest.approx(95.0)
