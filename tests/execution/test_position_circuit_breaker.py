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

ROOT = Path(__file__).resolve().parents[2]
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


# ── R2 (spec 2026-09-12): NO regime exemption, in code or in the docstring ──

import inspect                                        # noqa: E402
import re                                             # noqa: E402

from execution import position_circuit_breaker as pcb  # noqa: E402

_MIGRATION = (ROOT / 'src' / 'database' / 'migrations'
              / '069_regime_blended_sizer.sql')


def test_module_docstring_does_not_claim_a_regime_skip():
    """Ruling R2: HIGH_VOL/CRISIS are NOT exempt. The module docstring at
    :8-9 was the claim the brief pre-flight found; main()'s own docstring
    and its regime-read comment repeated the same claim in complementary
    ("consolidate-mode") vocabulary and are pinned here too (2026-09-12)."""
    doc = (pcb.__doc__ or '').lower()
    assert 'are skipped' not in doc
    assert 'independent-mode positions' not in doc
    assert 'all four regimes' in doc

    main_doc = (pcb.main.__doc__ or '').lower()
    assert 'consolidate-mode' not in main_doc
    assert 'consolidate-mode' not in inspect.getsource(pcb.main).lower()


def test_main_has_no_regime_conditional_around_the_fire_path():
    """A `regime_state == 'CRISIS'` style early return must never come back.
    regime_state may only be used as the threshold lookup key and in the
    summary print."""
    src = inspect.getsource(pcb.main)
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith('#'):
            continue
        for regime in ('HIGH_VOL', 'CRISIS'):
            assert regime not in stripped, f'regime literal in main(): {stripped!r}'
    assert not re.search(r'if\s+regime_state', src)


def test_all_four_regimes_have_a_positive_breaker_threshold_seeded():
    """The breaker aborts when regime_sizer_params has no row for the live
    regime, so "no exemption" also means "every regime is seeded". Asserted
    against the migration text — no DB needed."""
    sql = _MIGRATION.read_text()
    rows = re.findall(
        r"\('(LOW_VOL|TRANSITIONING|HIGH_VOL|CRISIS)',\s*[\d.]+,\s*[\d.]+,\s*([\d.]+)\)",
        sql)
    seeded = {r: float(v) for r, v in rows}
    assert set(seeded) == {'LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'}
    assert all(v > 0 for v in seeded.values()), seeded


@pytest.mark.parametrize('regime', ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS'])
def test_breaker_fires_identically_in_every_regime(regime):
    """should_fire_breaker takes no regime argument at all — the threshold is
    the only per-regime input. Parameterised so the intent shows in the test
    names."""
    thresholds = {'LOW_VOL': 0.020, 'TRANSITIONING': 0.015,
                  'HIGH_VOL': 0.010, 'CRISIS': 0.005}
    pos = {'ticker': 'AAPL', 'qty': 1000, 'avg_entry_price': 100, 'mark': 97.5}
    fire, ratio = should_fire_breaker(pos, 100_000, thresholds[regime])
    assert fire is True
    assert ratio == pytest.approx(-0.025, abs=1e-6)


# ── fractional shares: get_positions must keep qty fractional ────────────────
def test_fractional_losing_position_fires_like_whole_share_position():
    """0.4 sh bought at 1000, now 890: loss $44 on a $2,000 NAV = -2.2 %.
    With qty truncated to int (0) this read as zero loss and never fired."""
    from execution import alpaca_trader as at
    from unittest.mock import MagicMock
    sess = MagicMock(); sess._base = 'http://x'
    sess.get.return_value.json.return_value = [{
        'symbol': 'BIG', 'qty': '0.4', 'avg_entry_price': '1000',
        'current_price': '890', 'unrealized_plpc': '-0.1', 'side': 'long',
        'market_value': '360'}]
    p = at.get_positions(sess)[0]
    assert p['qty'] == pytest.approx(0.4) and isinstance(p['qty'], float)
    pos = {'ticker': p['symbol'], 'qty': p['qty'],
           'avg_entry_price': p['avg_entry_price'], 'mark': p['current_price']}
    fire, ratio = should_fire_breaker(pos, 2_000, threshold_pct=0.02)
    assert ratio == pytest.approx(-0.022, abs=1e-9)
    assert fire is True
    # same loss/NAV ratio on a whole-share position fires at the same threshold
    whole = {'ticker': 'W', 'qty': 4, 'avg_entry_price': 100, 'mark': 89}
    assert should_fire_breaker(whole, 2_000, threshold_pct=0.02)[0] is True


def test_get_positions_keeps_fractional_short_qty_and_whole():
    from execution import alpaca_trader as at
    from unittest.mock import MagicMock
    sess = MagicMock(); sess._base = 'http://x'
    sess.get.return_value.json.return_value = [
        {'symbol': 'A', 'qty': '2.7', 'side': 'long'},
        {'symbol': 'B', 'qty': '-3', 'side': 'short'}]
    out = at.get_positions(sess)
    assert out[0]['qty'] == pytest.approx(2.7) and out[1]['qty'] == -3.0


def test_reduce_third_and_fmt_qty_stay_fractional():
    from execution import alpaca_trader as at
    assert at._reduce_third(0.9) == pytest.approx(0.3)
    assert at._reduce_third(2.7) == pytest.approx(0.9)
    assert at._reduce_third(9.0) == 3.0 and at._reduce_third(2.0) == 1.0
    assert at._fmt_qty(0.3) == '0.3' and at._fmt_qty(3.0) == '3'
