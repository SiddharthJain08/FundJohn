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
    assert abs(o['stop'] - 95.0) < 1e-9
    assert abs(o['t1'] - 110.0) < 1e-9   # float noise from pct-space round-trip


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


# ---------------------------------------------------------------------------
# Multi-strategy mixed-cadence stacked-combine test (Item 1)
# ---------------------------------------------------------------------------
# Strategy A: cadence 1  (daily),   entry=100, stop=95  (5% gap),  t1=110
# Strategy B: cadence 21 (monthly), entry=100, stop=90  (10% gap), t1=110
#
# Combine = effective-Sharpe-weighted mean over singleton blocks (2026-07-14);
# equal eff Sharpe here → plain mean of the (possibly normalized) pct gaps.
#
# Gate OFF: stop = mean(5%, 10%) = 7.5% → 92.5;  tp = mean(10%, 10%) = 10% → 110.
# Gate ON:  A unchanged (cadence 1); B gaps ÷√21 → stop 2.1822%, tp 2.1822%.
#           stop = mean(5%, 10/√21 %) → 100·(1 − (0.05 + 0.10/√21)/2) ≈ 96.4089
#           tp   = mean(10%, 10/√21 %) → ≈ 106.0911
#           Normalization shifts the weighted stop toward B's tighter gap.

def _weights_row_for(sid, cadence, daily_weight=5.0, effective_sharpe=5.0):
    return {'strategy_id': sid, 'daily_weight': daily_weight,
            'effective_sharpe': effective_sharpe, 'cadence_days': cadence}


def _drive_two(monkeypatch, gate_on: bool):
    """Drive size_positions with two same-ticker same-direction strategies,
    different cadence, through the STACKED min-stop combine."""
    monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    monkeypatch.delenv('OPENCLAW_STRATEGY_FOLD', raising=False)
    monkeypatch.setenv('OPENCLAW_STRATEGY_BRACKET_STACK', '1')
    if gate_on:
        monkeypatch.setenv('OPENCLAW_STRATEGY_CADENCE_STOP_NORM', '1')
    else:
        monkeypatch.delenv('OPENCLAW_STRATEGY_CADENCE_STOP_NORM', raising=False)

    monkeypatch.setattr(_sizer, '_load_active_window_signals', lambda *a, **k: [])
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})

    weights = [
        _weights_row_for('A', cadence=1.0),   # daily
        _weights_row_for('B', cadence=21.0),  # monthly
    ]
    signals = [
        _sig(sid='A', ticker='AAPL', direction=1, entry=100.0, stop=95.0,  t1=110.0),
        _sig(sid='B', ticker='AAPL', direction=1, entry=100.0, stop=90.0,  t1=110.0),
    ]
    with _mock.patch('execution.strategy_weights.load_current', return_value=weights), \
         _mock.patch('execution.strategy_similarity.load_groups',
                     return_value={'fold_map': {}, 'rep_map': {}, 'block_map': {}, 'matrix': {}}):
        return _sizer.size_positions(
            signals=signals, account_state=_account(),
            regime={'state': 'LOW_VOL'}, run_date=date(2026, 5, 12),
            strategy_state={}, regime_params=_params(), confirmer=None,
        )


def test_stacked_combine_weights_normalized_stops(monkeypatch):
    import math

    # Gate OFF: equal-Sharpe weighted mean — stop mean(5%,10%)=7.5% → 92.5;
    # tp mean(10%,10%)=10% → 110. (A 92.5 stop proves the stacked combine fired:
    # a single-bracket pick would give 95.0 or 90.0.)
    o_off = _order_for(_drive_two(monkeypatch, gate_on=False))
    assert abs(o_off['stop'] - 92.5) < 1e-6, f"gate-OFF stop should be 92.5, got {o_off['stop']}"
    assert abs(o_off['t1'] - 110.0) < 1e-6, (
        f"gate-OFF t1 should be 110.0 (weighted tp mean), got {o_off['t1']} — "
        "if not, stacking didn't fire (check BRACKET_STACK env or load_groups patch)"
    )

    # Gate ON: B's gaps normalize ÷√21 → stop mean(5%, 10/√21 %) ≈ 3.5911%
    o_on = _order_for(_drive_two(monkeypatch, gate_on=True))
    expected_stop_on = 100.0 * (1.0 - (0.05 + 0.10 / math.sqrt(21.0)) / 2.0)  # ≈ 96.4089
    expected_t1_on = 100.0 * (1.0 + (0.10 + 0.10 / math.sqrt(21.0)) / 2.0)    # ≈ 106.0911
    assert abs(o_on['stop'] - expected_stop_on) < 1e-6, (
        f"gate-ON stop should be {expected_stop_on:.6f}, got {o_on['stop']}"
    )
    assert abs(o_on['t1'] - expected_t1_on) < 1e-6, (
        f"gate-ON t1 should be {expected_t1_on:.6f}, got {o_on['t1']}"
    )

    # Normalization changed the combine outcome (the shift)
    assert o_on['stop'] != o_off['stop'], (
        f"gate-ON and gate-OFF should differ; both gave {o_off['stop']}"
    )
