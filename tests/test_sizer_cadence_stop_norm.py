"""tests/test_sizer_cadence_stop_norm.py — √cadence stop/TP normalization.

Drives size_positions -> _sharpe_cadence_path with the mock harness from
test_sizer_sp6_eod_mode.py (load_current patched, loaders + broker
monkeypatched), controlling cadence_days via the fake weights row. Asserts
the emitted order's stop/t1 are √cadence-normalized when the gate is ON and
byte-identical (raw) when OFF.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
import unittest.mock as _mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer


def _sig(sid='S1', ticker='AAPL', direction=1, entry=100.0, stop=95.0, t1=110.0):
    return {
        'signal_id': hash((sid, ticker, direction)),
        'strategy_id': sid, 'ticker': ticker, 'direction': direction,
        'entry_price': entry, 'stop_loss': stop, 'target_1': t1, 'target_2': None,
    }


def _account(equity=100_000):
    return {'equity': equity, 'regt_buying_power': 2 * equity,
            'long_market_value': 0, 'cash': equity}


def _params():
    return {'liquidity_param': 1.0, 'min_signal_notional_usd': 100,
            'position_circuit_breaker_pct': 0.02}


def _weights_row(cadence=4.0):
    return {'strategy_id': 'S1', 'daily_weight': 5.0,
            'effective_sharpe': 5.0, 'cadence_days': cadence}


def _drive(monkeypatch, gate_on: bool, cadence=4.0):
    monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    monkeypatch.delenv('OPENCLAW_STRATEGY_BRACKET_STACK', raising=False)
    monkeypatch.delenv('OPENCLAW_STRATEGY_FOLD', raising=False)
    if gate_on:
        monkeypatch.setenv('OPENCLAW_STRATEGY_CADENCE_STOP_NORM', '1')
    else:
        monkeypatch.delenv('OPENCLAW_STRATEGY_CADENCE_STOP_NORM', raising=False)
    monkeypatch.setattr(_sizer, '_load_active_window_signals', lambda *a, **k: [])
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    with _mock.patch('execution.strategy_weights.load_current',
                     return_value=[_weights_row(cadence)]):
        return _sizer.size_positions(
            signals=[_sig('S1')], account_state=_account(),
            regime={'state': 'LOW_VOL'}, run_date=date(2026, 5, 12),
            strategy_state={}, regime_params=_params(), confirmer=None,
        )


def _order_for(orders, ticker='AAPL'):
    hits = [o for o in orders if o.get('ticker') == ticker and not o.get('close_only')]
    assert hits, f'expected an opening order for {ticker}, got {orders}'
    return hits[0]


def test_gate_off_is_byte_identical_raw_levels(monkeypatch):
    orders = _drive(monkeypatch, gate_on=False)
    o = _order_for(orders)
    assert o['entry'] == 100.0
    assert o['stop'] == 95.0
    assert o['t1'] == 110.0


def test_gate_on_normalizes_stop_and_t1(monkeypatch):
    # cadence 4 -> f = 0.5: stop 95 -> 97.5, t1 110 -> 105.0; entry unchanged
    orders = _drive(monkeypatch, gate_on=True, cadence=4.0)
    o = _order_for(orders)
    assert o['entry'] == 100.0
    assert abs(o['stop'] - 97.5) < 1e-6
    assert abs(o['t1'] - 105.0) < 1e-6


def test_gate_on_daily_cadence_is_noop(monkeypatch):
    orders = _drive(monkeypatch, gate_on=True, cadence=1.0)
    o = _order_for(orders)
    assert o['stop'] == 95.0
    assert o['t1'] == 110.0
