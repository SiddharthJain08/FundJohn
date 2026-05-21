"""Tests for regime_blended_sizer.size_positions wrapper.

Legacy mode-dispatch tests (LOW_VOL consolidate / HIGH_VOL independent /
_select_mode / tradejohn veto+scale / target_pct_nav fallback) were
deleted 2026-05-21 along with the underlying _consolidate_path and
_independent_path. Sharpe-cadence is now the sole sizer path; coverage
for its wiring lives in tests/test_regime_blended_sizer_live.py
(TestSharpeCadenceShape, TestDirectionFlipEmission,
TestExecutorFlipPriorityAndPolling). Live behavior is exercised by the
production cycle itself — _sharpe_cadence_path needs real DB rows from
strategy_weights_by_regime + execution_signals which we don't mock here.

What remains: the cadence-gate early-exit in size_positions, which is
pure list manipulation and doesn't touch the DB.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.regime_blended_sizer import size_positions  # noqa: E402


def _sig(sid, ticker='AAPL', direction=1):
    return {
        'signal_id': hash((sid, ticker, direction)),
        'strategy_id': sid, 'ticker': ticker, 'direction': direction,
        'entry_price': 100, 'stop_loss': 95,
        'take_profit_1': 110, 'target_1': 110,
        'p_t1': 0.6,
    }


def _account(equity=100_000):
    return {'equity': equity, 'regt_buying_power': 2 * equity,
            'long_market_value': 0, 'cash': equity}


def _params():
    return {'liquidity_param': 1.0,
            'min_signal_notional_usd': 100,
            'position_circuit_breaker_pct': 0.02}


def test_cadence_pending_signal_skipped():
    """Cadence gate: a signal from a strategy whose next_fire_date is in
    the future returns an empty order list without invoking the
    sharpe-cadence path or the confirmer."""
    sigs = [_sig('S1', 'AAPL', 1)]
    state = {'S1': {'last_fire_date': date(2026, 5, 11),
                    'next_fire_date': date(2026, 5, 14),
                    'avg_holding_days': 3.0, 'source': 'live_signal_pnl'}}
    # Confirmer must NOT be called when cadence gate filters everything.
    confirmer_calls = []

    def fail_if_called(proposals, runner=None):
        confirmer_calls.append(proposals)
        return {}

    orders = size_positions(
        signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
        run_date=date(2026, 5, 12), strategy_state=state,
        regime_params=_params(),
        confirmer=fail_if_called,
    )
    assert orders == []
    assert confirmer_calls == [], 'confirmer must not be invoked when all signals are cadence-skipped'
