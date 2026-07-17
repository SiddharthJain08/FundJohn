"""SP-7 Phase B Task 8 — selection: narrowest eligible + ΔSharpe≥0.10 displacement."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.universe_ladder_selection import select_tier, LADDER_TIERS


def _m(sharpe, trades=100):
    return {'sharpe': sharpe, 'trades_n': trades}


def test_ladder_tiers_order():
    assert LADDER_TIERS == ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')


def test_narrowest_wins_on_tie_band():
    v = select_tier({'sp500': _m(1.00), 'tier_r1000': _m(1.05),
                     'tier_r3000': _m(1.09), 'tier_liquid': _m(0.5)})
    assert v['verdict'] == 'winner' and v['choice'] == 'sp500'  # +0.09 < 0.10


def test_broader_displaces_at_threshold():
    v = select_tier({'sp500': _m(1.00), 'tier_r1000': _m(1.10),
                     'tier_r3000': _m(1.15), 'tier_liquid': _m(1.12)})
    # r1000 displaces sp500 (Δ=0.10 vs sp500); r3000 does NOT displace r1000 (Δ=0.05)
    assert v['choice'] == 'tier_r1000'


def test_chained_displacement():
    v = select_tier({'sp500': _m(1.0), 'tier_r1000': _m(1.10),
                     'tier_r3000': _m(1.20), 'tier_liquid': _m(1.31)})
    assert v['choice'] == 'tier_liquid'


def test_none_and_low_trades_ineligible():
    v = select_tier({'sp500': _m(None), 'tier_r1000': _m(2.0, trades=10),
                     'tier_r3000': _m(1.0), 'tier_liquid': None})
    assert v['choice'] == 'tier_r3000'  # only eligible tier


def test_all_ineligible_is_no_signal():
    v = select_tier({'sp500': _m(None), 'tier_r1000': None,
                     'tier_r3000': _m(1.0, trades=5), 'tier_liquid': _m(None)})
    assert v['verdict'] == 'no_signal' and v['choice'] is None


def test_missing_tier_keys_treated_ineligible():
    v = select_tier({'sp500': _m(1.4)})
    assert v['choice'] == 'sp500'


def test_chained_through_r3000_float_boundary():
    """r3000 at exactly winner+0.10 must displace despite IEEE754 (1.1+0.1>1.2)."""
    v = select_tier({'sp500': _m(1.0), 'tier_r1000': _m(1.10),
                     'tier_r3000': _m(1.20), 'tier_liquid': _m(0.5)})
    assert v['choice'] == 'tier_r3000'
