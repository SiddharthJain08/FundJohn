"""SP-5.1c Task 5 — sizer reads+carries option_spec through _build_sized_payload.

Tests _build_sized_payload (5b carry) only — the SQL + ticker_meta carry in
regime_blended_sizer.py (5a) is DB-dependent and cannot be exercised here;
equity-safety for 5a is by-construction + code-review (see report).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from execution.regime_blended_sizer_live import _build_sized_payload


def test_option_order_carries_spec_and_normalizes_dir():
    """Option order: option_spec reconstructed to OptionSpec, direction normalized."""
    spec = {'underlying': 'SPY', 'structure': 'straddle', 'hedge': 'delta', 'strike_rule': 'atm'}
    orders = [{
        'ticker': 'SPY',
        'direction': 'BUY_VOL',
        'instrument_class': 'option',
        'option_spec': spec,
        'contracts': 2,
        'entry': 1.0,
        'stop': 0.5,
        't1': 2.0,
        'notional_usd': 5000.0,
        'contributing_strategies': ['S_long_straddle_delta_hedged'],
    }]
    payload = _build_sized_payload(
        orders,
        {'cycle_date': '2026-06-03', 'regime': {}},
        equity=100_000.0,
    )
    o = payload['orders'][0]
    assert o['instrument_class'] == 'option'
    from strategies.base import OptionSpec
    assert isinstance(o['option_spec'], OptionSpec)
    assert o['option_spec'].structure == 'straddle'
    assert o['direction'] == 'long'   # BUY_VOL normalized to 'long'
    assert o['contracts'] == 2


def test_option_order_no_contracts_key_absent():
    """When contracts is None the key should not appear on the finalized order."""
    spec = {'underlying': 'SPY', 'structure': 'single', 'hedge': 'none', 'strike_rule': 'atm'}
    orders = [{
        'ticker': 'SPY',
        'direction': 'BUY_VOL',
        'instrument_class': 'option',
        'option_spec': spec,
        'contracts': None,
        'entry': 1.0,
        'stop': 0.5,
        't1': 2.0,
        'notional_usd': 5000.0,
        'contributing_strategies': ['S_long_straddle_delta_hedged'],
    }]
    payload = _build_sized_payload(
        orders,
        {'cycle_date': '2026-06-03', 'regime': {}},
        equity=100_000.0,
    )
    o = payload['orders'][0]
    assert o['instrument_class'] == 'option'
    # contracts key should NOT be injected when value is None
    assert 'contracts' not in o


def test_equity_order_unchanged():
    """Equity order: no option_spec/instrument_class keys injected; direction unchanged."""
    orders = [{
        'ticker': 'AAPL',
        'direction': 'long',
        'entry': 100.0,
        'stop': 95.0,
        't1': 110.0,
        'notional_usd': 1000.0,
        'contributing_strategies': ['S22_quality_momentum'],
    }]
    payload = _build_sized_payload(
        orders,
        {'cycle_date': '2026-06-03', 'regime': {}},
        equity=100_000.0,
    )
    o = payload['orders'][0]
    assert o.get('instrument_class') is None
    assert o.get('option_spec') is None
    assert o.get('contracts') is None
    assert o['direction'] == 'long'
