"""
Tests for S5 Max Pain Gravity signal module.

Target metrics (from LeadDev backtest):
  Sharpe >= 4.0, Win Rate >= 85%, MaxDD >= -5%
"""

import pytest
import math
from src.strategies.implementations.s5_max_pain import (
    Bar,
    Direction,
    MaxPainSignal,
    compute_max_pain,
    generate_signal,
    backtest,
    make_synthetic_bars,
    BacktestMetrics,
)


# ---------------------------------------------------------------------------
# Helper factories
# ---------------------------------------------------------------------------

def _make_bar(
    spot: float,
    mp_center: float = 420.0,
    dte: int = 7,
    iv_rank: float = 40.0,
    base_date: str = '2024-01-01',
    expiry_date: str = None,
) -> Bar:
    from datetime import date, timedelta
    bar_d    = date.fromisoformat(base_date)
    exp_d    = bar_d + timedelta(days=dte)
    exp_str  = exp_d.isoformat()
    oi: dict = {}
    for k_int in range(390, 455, 5):
        k = float(k_int)
        oi[k] = max(1.0, 10000.0 * math.exp(-0.5 * ((k - mp_center) / 8) ** 2))
    return Bar(
        close=spot,
        open_interest_by_strike=oi,
        expiry_date=exp_str,
        iv_rank=iv_rank,
        timestamp=base_date,
    )


# ---------------------------------------------------------------------------
# Test 1: max_pain calculation
# ---------------------------------------------------------------------------

def test_max_pain_calculation():
    """Max pain should be the strike minimising weighted L1 distance."""
    # Simple case: equal OI at 400 and 440, heavy OI at 420 → 420 is max pain
    oi = {400.0: 100.0, 420.0: 10000.0, 440.0: 100.0}
    mp = compute_max_pain(oi)
    assert mp == 420.0, f"Expected 420.0, got {mp}"

    # Asymmetric case: OI peaks at 410
    oi2 = {400.0: 50.0, 410.0: 5000.0, 420.0: 100.0}
    mp2 = compute_max_pain(oi2)
    assert mp2 == 410.0, f"Expected 410.0, got {mp2}"

    # Single strike
    oi3 = {500.0: 999.0}
    mp3 = compute_max_pain(oi3)
    assert mp3 == 500.0

    # Empty should raise
    with pytest.raises(ValueError):
        compute_max_pain({})


# ---------------------------------------------------------------------------
# Test 2: LONG signal fires when spot < max_pain * 0.985
# ---------------------------------------------------------------------------

def test_long_signal_fires():
    """Spot meaningfully below max pain within DTE window → LONG signal."""
    mp_center = 420.0
    spot      = mp_center * 0.980   # 2% below → below 0.985 threshold

    bar = _make_bar(spot=spot, mp_center=mp_center, dte=7, iv_rank=40.0)
    sig = generate_signal(bar)

    assert sig is not None, "Expected LONG signal"
    assert sig.direction == Direction.LONG
    assert sig.entry_price == pytest.approx(spot, rel=1e-4)
    assert sig.max_pain    == pytest.approx(mp_center, rel=0.01)
    assert sig.stop_loss   < sig.entry_price
    assert sig.target      > sig.entry_price


# ---------------------------------------------------------------------------
# Test 3: SHORT signal fires when spot > max_pain * 1.015
# ---------------------------------------------------------------------------

def test_short_signal_fires():
    """Spot meaningfully above max pain within DTE window → SHORT signal."""
    mp_center = 420.0
    spot      = mp_center * 1.020   # 2% above → above 1.015 threshold

    bar = _make_bar(spot=spot, mp_center=mp_center, dte=7, iv_rank=40.0)
    sig = generate_signal(bar)

    assert sig is not None, "Expected SHORT signal"
    assert sig.direction == Direction.SHORT
    assert sig.entry_price == pytest.approx(spot, rel=1e-4)
    assert sig.stop_loss   > sig.entry_price
    assert sig.target      < sig.entry_price


# ---------------------------------------------------------------------------
# Test 4: IV rank gate blocks signal when iv_rank >= 70
# ---------------------------------------------------------------------------

def test_iv_rank_gate():
    """Signal must be suppressed when IV rank >= 70."""
    mp_center = 420.0
    spot      = mp_center * 0.980   # would otherwise trigger LONG

    # At the boundary
    bar_blocked = _make_bar(spot=spot, mp_center=mp_center, dte=7, iv_rank=70.0)
    assert generate_signal(bar_blocked) is None, "iv_rank=70 should be blocked"

    bar_blocked2 = _make_bar(spot=spot, mp_center=mp_center, dte=7, iv_rank=85.0)
    assert generate_signal(bar_blocked2) is None, "iv_rank=85 should be blocked"

    # Just below threshold — should pass gate
    bar_ok = _make_bar(spot=spot, mp_center=mp_center, dte=7, iv_rank=69.9)
    assert generate_signal(bar_ok) is not None, "iv_rank=69.9 should be allowed"


# ---------------------------------------------------------------------------
# Test 5: Expiry gate blocks signal when DTE > 14
# ---------------------------------------------------------------------------

def test_expiry_gate():
    """Signal must be suppressed when more than 14 DTE remain."""
    mp_center = 420.0
    spot      = mp_center * 0.980

    # Too far from expiry
    bar_far = _make_bar(spot=spot, mp_center=mp_center, dte=15, iv_rank=40.0)
    assert generate_signal(bar_far) is None, "DTE=15 should be blocked"

    bar_far2 = _make_bar(spot=spot, mp_center=mp_center, dte=30, iv_rank=40.0)
    assert generate_signal(bar_far2) is None, "DTE=30 should be blocked"

    # At boundary — should pass
    bar_ok = _make_bar(spot=spot, mp_center=mp_center, dte=14, iv_rank=40.0)
    assert generate_signal(bar_ok) is not None, "DTE=14 should be allowed"

    # DTE=0 (expiry day itself) should also be blocked
    bar_zero = _make_bar(spot=spot, mp_center=mp_center, dte=0, iv_rank=40.0)
    assert generate_signal(bar_zero) is None, "DTE=0 should be blocked"


# ---------------------------------------------------------------------------
# Test 6: Backtest metrics on synthetic 630-bar dataset
# ---------------------------------------------------------------------------

def test_backtest_metrics():
    """
    On the canonical 630-bar synthetic dataset:
      - Sharpe >= 4.0
      - Win Rate >= 85%
      - Max Drawdown >= -5%  (i.e., drawdown never exceeds -5%)
    """
    bars    = make_synthetic_bars(n=630, seed=42)
    metrics = backtest(bars)

    assert metrics.total_trades > 0, "No trades generated — check synthetic bar factory"

    assert metrics.sharpe >= 4.0, (
        f"Sharpe {metrics.sharpe:.2f} below target 4.0 "
        f"(trades={metrics.total_trades}, win_rate={metrics.win_rate:.1%})"
    )

    assert metrics.win_rate >= 0.85, (
        f"Win rate {metrics.win_rate:.1%} below target 85% "
        f"(trades={metrics.total_trades})"
    )

    assert metrics.max_drawdown >= -0.05, (
        f"Max drawdown {metrics.max_drawdown:.2%} exceeds -5% limit"
    )
