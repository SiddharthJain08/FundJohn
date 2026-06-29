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
import unittest.mock as _mock
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402
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


# ---------------------------------------------------------------------------
# W3 F2c — per-ticker conviction cap: intraday path extension (C3)
#
# The cap (PER_TICKER_CAP_SHARPE_FRAC × |gate_net_sharpe| × λ × NAV) was
# previously gated to EOD mode only.  C3 extends it to the intraday-redeploy
# path via OPENCLAW_INTRADAY_REDEPLOY=1.  The plain daily lane (neither flag
# set) must remain byte-identical (uncapped).
#
# Strategy: drive size_positions → _sharpe_cadence_path with all external
# surfaces (weights DB, active-window DB, λ DB, broker, confirmer) stubbed,
# mirroring the approach in tests/test_sizer_per_ticker_cap.py.
# ---------------------------------------------------------------------------

_CAP_NAV = 100_000.0
_CAP_LAM = 2.0


def _cap_account():
    return {'equity': _CAP_NAV, 'regt_buying_power': 2 * _CAP_NAV,
            'long_market_value': 0, 'cash': _CAP_NAV}


def _cap_params():
    """Regime params with all DB-fetched fields supplied so
    _resolve_min_cumulative_sharpe never reaches the DB."""
    return {
        'liquidity_param': 1.0,
        'min_signal_notional_usd': 1,
        'min_signal_notional_pct': 0.00001,
        'position_circuit_breaker_pct': 0.02,
        'min_cumulative_sharpe': 3.0,
    }


def _carried_cap(sid, ticker, direction='LONG'):
    return {'strategy_id': sid, 'ticker': ticker, 'direction': direction,
            'signal_date': date(2026, 6, 4), 'entry_price': 100.0,
            'stop_loss': 95.0, 'target_1': 110.0, 'target_2': 120.0,
            'signal_params': {}}


def _weights_row_cap(sid, eff_sharpe, daily_weight=1.0):
    return {'strategy_id': sid, 'daily_weight': daily_weight,
            'effective_sharpe': eff_sharpe, 'cadence_days': 1.0}


def _open_by_ticker_cap(orders):
    return {o['ticker']: o for o in orders
            if o['action'] not in ('close_long', 'close_short')
            and o['strategy_id'] not in ('__close_orphan__', '__flip_close__')}


def _run_intraday_path(monkeypatch, weights_rows, signals_rows, broker=None):
    """Drive size_positions through the non-EOD sharpe_cadence path with
    OPENCLAW_INTRADAY_REDEPLOY=1.  All external surfaces (weights DB,
    active-window DB, λ DB, broker, confirmer) are stubbed.

    Key difference from test_sizer_per_ticker_cap._run_eod:
    - OPENCLAW_INTRADAY_REDEPLOY=1 (not OPENCLAW_EOD_RECONCILE)
    - _load_active_window_signals mocked (not _load_approved_carried_signals)
    - _load_lambda mock accepts intraday kwarg (C2 signature)
    """
    monkeypatch.setenv('OPENCLAW_INTRADAY_REDEPLOY', '1')
    monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
    for gate in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
                 'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE',
                 'OPENCLAW_STRATEGY_CORR_WEIGHT'):
        monkeypatch.delenv(gate, raising=False)

    monkeypatch.setattr(_sizer, '_load_active_window_signals',
                        lambda rstate, wbs, cbs: list(signals_rows))
    # Accept intraday kwarg (C2 extended _load_lambda signature).
    monkeypatch.setattr(_sizer, '_load_lambda',
                        lambda default=2.0, *, intraday=False: _CAP_LAM)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: dict(broker or {}))
    # W3 C4 — signal-set-health gate is intraday-only; stub _recent_active_counts
    # so the gate uses a baseline consistent with len(signals_rows). Callers that
    # want to test the gate itself should override this stub via their own
    # monkeypatch.setattr call after _run_intraday_path returns or by not using
    # this helper at all.
    monkeypatch.setattr(_sizer, '_recent_active_counts',
                        lambda lookback=10: [len(signals_rows)] * lookback)

    with _mock.patch('execution.strategy_weights.load_current',
                     return_value=list(weights_rows)):
        return _sizer.size_positions(
            # Pass signals so the cadence gate (unknown strategy → bootstrap
            # daily) lets them through before _sharpe_cadence_path takes over.
            signals=list(signals_rows),
            account_state=_cap_account(),
            regime={'state': 'LOW_VOL'},
            run_date=date(2026, 6, 4), strategy_state={},
            regime_params=_cap_params(), confirmer=lambda proposals: {},
        )


class TestIntradayRedeployConvictionCap:
    """W3 F2c: OPENCLAW_INTRADAY_REDEPLOY=1 must engage the per-ticker
    conviction cap identical to the existing EOD path."""

    def test_intraday_single_survivor_capped(self, monkeypatch):
        """Single over-cap survivor on the intraday path must be clamped to
        cap = PER_TICKER_CAP_SHARPE_FRAC × |sharpe| × λ × NAV.

        Without the C3 gate fix this test is RED (cap not applied on intraday
        path → target = full λ×NAV = $200k instead of $35k).

        W3 C4 note: the signal-set-health gate (floor=10) is active on the
        intraday path. Nine dummy signals with sharpe=0.5 pad len(active) to
        10 so the gate passes; they are filtered by the cum_sharpe gate (3.0)
        and never produce orders. The _recent_active_counts stub in
        _run_intraday_path returns [10]*10 → baseline=10, threshold=10 ≤
        len(active)=10 → gate passes. STX is the sole surviving ticker."""
        dummy_sids = [f'S_dummy_{i}' for i in range(9)]
        dummy_weights = [_weights_row_cap(sid, eff_sharpe=0.5) for sid in dummy_sids]
        dummy_signals = [_carried_cap(sid, f'DUMMY{i:02d}') for i, sid in enumerate(dummy_sids)]
        orders = _run_intraday_path(
            monkeypatch,
            weights_rows=[_weights_row_cap('S1', eff_sharpe=3.5)] + dummy_weights,
            signals_rows=[_carried_cap('S1', 'STX')] + dummy_signals,
        )
        opens = _open_by_ticker_cap(orders)
        assert 'STX' in opens, f'expected STX open order, got {orders}'
        expected_cap = 0.05 * 3.5 * _CAP_LAM * _CAP_NAV  # 35_000
        assert abs(opens['STX']['target_usd'] - expected_cap) < 1e-6, (
            f'intraday survivor must be capped at {expected_cap}; '
            f'got {opens["STX"]["target_usd"]}'
        )


class TestDailyLaneConvictionCapBypass:
    """Daily lane (neither flag) must remain byte-identical — no cap applied."""

    def test_daily_neither_flag_no_cap(self, monkeypatch):
        """Verify the plain daily path is uncapped: single survivor keeps
        the full λ×NAV target.  Passes before and after the C3 change
        (byte-identical guard)."""
        monkeypatch.delenv('OPENCLAW_INTRADAY_REDEPLOY', raising=False)
        monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)
        for gate in ('OPENCLAW_STRATEGY_FOLD', 'OPENCLAW_STRATEGY_ORTHO_SHADOW',
                     'OPENCLAW_STRATEGY_BRACKET_STACK', 'OPENCLAW_OPTION_DELTA_HEDGE',
                     'OPENCLAW_STRATEGY_CORR_WEIGHT'):
            monkeypatch.delenv(gate, raising=False)

        monkeypatch.setattr(_sizer, '_load_active_window_signals',
                            lambda rstate, wbs, cbs: [_carried_cap('S1', 'STX')])
        monkeypatch.setattr(_sizer, '_load_lambda',
                            lambda default=2.0, *, intraday=False: _CAP_LAM)
        monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})

        with _mock.patch('execution.strategy_weights.load_current',
                         return_value=[_weights_row_cap('S1', eff_sharpe=3.5)]):
            orders = _sizer.size_positions(
                signals=[_carried_cap('S1', 'STX')], account_state=_cap_account(),
                regime={'state': 'LOW_VOL'},
                run_date=date(2026, 6, 4), strategy_state={},
                regime_params=_cap_params(), confirmer=lambda proposals: {},
            )
        opens = _open_by_ticker_cap(orders)
        assert 'STX' in opens
        assert abs(opens['STX']['target_usd'] - _CAP_LAM * _CAP_NAV) < 1e-6, (
            f'daily lane must remain uncapped (full {_CAP_LAM * _CAP_NAV}); '
            f'got {opens["STX"]["target_usd"]}'
        )
