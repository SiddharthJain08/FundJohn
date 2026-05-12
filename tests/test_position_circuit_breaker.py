"""tests/test_position_circuit_breaker.py

Unit tests for the intraday 5-min circuit breaker that fires
consolidate-mode positions exceeding loss thresholds per regime.

Run:
    pytest tests/test_position_circuit_breaker.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.position_circuit_breaker import (  # noqa: E402
    should_fire_breaker,
    format_breaker_message,
)


def test_should_fire_when_loss_below_threshold():
    """Small loss well below threshold should NOT fire."""
    pos = {'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'mark': 95}
    nav = 100_000
    fire, ratio = should_fire_breaker(pos, nav, threshold_pct=0.02)
    # loss = (95-100) * 100 = -500 → -0.5% NAV → below 2% threshold → no fire
    assert fire is False
    assert ratio == pytest.approx(-0.005, abs=1e-6)


def test_should_fire_when_loss_clears_threshold():
    """Loss exceeding threshold should fire."""
    pos = {'ticker': 'AAPL', 'qty': 1000, 'avg_entry_price': 100, 'mark': 97.5}
    nav = 100_000
    fire, ratio = should_fire_breaker(pos, nav, threshold_pct=0.02)
    # loss = (97.5-100) * 1000 = -2500 → -2.5% NAV → exceeds 2%
    assert fire is True
    assert ratio == pytest.approx(-0.025, abs=1e-6)


def test_short_position_breaker_on_adverse_move():
    """Short position with adverse move should fire."""
    pos = {'ticker': 'AAPL', 'qty': -100, 'avg_entry_price': 100, 'mark': 105}
    nav = 100_000
    fire, ratio = should_fire_breaker(pos, nav, threshold_pct=0.001)
    # short -100 @ 100, mark 105 → (105-100)*-100 = -500 → -0.5% NAV
    assert fire is True
    assert ratio == pytest.approx(-0.005, abs=1e-6)


def test_format_breaker_message_contains_ticker_and_pct():
    """Format message should include ticker and percentage."""
    msg = format_breaker_message('AAPL', -0.025, 0.02, qty=100)
    assert 'AAPL' in msg
    assert '-2.50%' in msg or '-2.5%' in msg


def test_should_not_fire_on_gain():
    """Profitable positions should never fire."""
    pos = {'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'mark': 110}
    nav = 100_000
    fire, ratio = should_fire_breaker(pos, nav, threshold_pct=0.02)
    assert fire is False
    assert ratio == pytest.approx(0.01, abs=1e-6)


def test_zero_nav_returns_zero_ratio():
    """With zero NAV, ratio should be zero, no fire."""
    pos = {'ticker': 'AAPL', 'qty': 100, 'avg_entry_price': 100, 'mark': 95}
    nav = 0.0
    fire, ratio = should_fire_breaker(pos, nav, threshold_pct=0.02)
    assert fire is False
    assert ratio == 0.0
