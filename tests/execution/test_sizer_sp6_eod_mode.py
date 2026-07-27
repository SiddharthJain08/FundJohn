"""tests/test_sizer_sp6_eod_mode.py — SP-6 Phase A gated sizer changes.

Two invariants:
  1. legacy-loading-unchanged: with OPENCLAW_EOD_RECONCILE unset, the
     cadence gate and active-window signal loading run exactly as before —
     a cadence-pending signal is still skipped; _load_active_window_signals
     is called (not _load_approved_carried_signals).

  2. eod-mode loads APPROVED + bypasses cadence: with gate ON, a signal
     whose strategy's next_fire_date is in the future is NOT cadence-skipped
     (it passes through to _sharpe_cadence_path), and _load_approved_carried_signals
     is called instead of _load_active_window_signals.

Both tests are pure-logic: they monkeypatch _sharpe_cadence_path to a
capturing stub and assert on whether/how it is called, so no real DB or
Postgres fixture is required and the migration-12x columns don't need to
be present in a test DB.
"""
from __future__ import annotations

import sys
import os
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer


def _sig(sid='S1', ticker='AAPL', direction=1):
    return {
        'signal_id': hash((sid, ticker, direction)),
        'strategy_id': sid, 'ticker': ticker, 'direction': direction,
        'entry_price': 100, 'stop_loss': 95, 'target_1': 110,
    }


def _account(equity=100_000):
    return {'equity': equity, 'regt_buying_power': 2 * equity,
            'long_market_value': 0, 'cash': equity}


def _params():
    return {'liquidity_param': 1.0,
            'min_signal_notional_usd': 100,
            'position_circuit_breaker_pct': 0.02}


# ---------------------------------------------------------------------------
# Test 1: gate OFF — legacy cadence behaviour unchanged
# ---------------------------------------------------------------------------

class TestLegacyLoadingUnchanged:
    """With OPENCLAW_EOD_RECONCILE unset, the legacy code path runs
    byte-identically: a cadence-pending signal is skipped and
    _load_active_window_signals (not _load_approved_carried_signals)
    would be called by _sharpe_cadence_path."""

    def test_cadence_pending_still_skipped_gate_off(self, monkeypatch):
        """A signal whose next_fire_date is future must be cadence-skipped
        when OPENCLAW_EOD_RECONCILE is unset — same as the legacy test
        test_cadence_pending_signal_skipped in test_regime_blended_sizer.py."""
        # Ensure the gate is NOT set.
        monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)

        sigs = [_sig('S1')]
        state = {'S1': {'last_fire_date': date(2026, 5, 11),
                        'next_fire_date': date(2026, 5, 14),
                        'avg_holding_days': 3.0, 'source': 'live_signal_pnl'}}
        confirmer_calls = []

        def fail_if_called(proposals, runner=None):
            confirmer_calls.append(proposals)
            return {}

        orders = _sizer.size_positions(
            signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
            run_date=date(2026, 5, 12), strategy_state=state,
            regime_params=_params(), confirmer=fail_if_called,
        )
        assert orders == [], 'cadence-pending signal must be skipped with gate OFF'
        assert confirmer_calls == [], 'confirmer must not be called when all signals cadence-skipped'

    def test_active_window_loader_called_not_approved_gate_off(self, monkeypatch):
        """_sharpe_cadence_path calls _load_active_window_signals when gate is OFF,
        never _load_approved_carried_signals."""
        monkeypatch.delenv('OPENCLAW_EOD_RECONCILE', raising=False)

        active_window_calls = []
        approved_calls = []

        def fake_active_window(regime_state, weight_by_strat, cadence_by_strat):
            active_window_calls.append({'regime': regime_state, 'weights': weight_by_strat})
            return []  # empty → path falls through to today's signals

        def fake_approved(weight_by_strat, cadence_by_strat=None, **_kw):
            approved_calls.append(weight_by_strat)
            return []

        monkeypatch.setattr(_sizer, '_load_active_window_signals', fake_active_window)
        monkeypatch.setattr(_sizer, '_load_approved_carried_signals', fake_approved)

        # Provide a non-empty load_current result so weight_by_strat is populated
        # and the signal-loading branch is reached. Return a minimal row with all
        # required numeric fields so _sharpe_cadence_path proceeds past the early
        # return at "if not rows: return []".
        import unittest.mock as _mock
        fake_weights_row = {
            'strategy_id': 'S1', 'daily_weight': 1.0,
            'effective_sharpe': 1.0, 'cadence_days': 1.0,
        }
        with _mock.patch('execution.strategy_weights.load_current', return_value=[fake_weights_row]):
            sigs = [_sig('S1')]  # strategy S1 passes cadence gate (no state → bootstrap)
            state: dict = {}     # unknown strategy → bootstrap daily → always pass
            _sizer.size_positions(
                signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
                run_date=date(2026, 5, 12), strategy_state=state,
                regime_params=_params(), confirmer=None,
            )

        assert len(active_window_calls) == 1, '_load_active_window_signals must be called with gate OFF'
        assert len(approved_calls) == 0, '_load_approved_carried_signals must NOT be called with gate OFF'


# ---------------------------------------------------------------------------
# Test 2: gate ON — EOD mode bypasses cadence + loads APPROVED set
# ---------------------------------------------------------------------------

class TestEodModeLoadsApprovedBypassesCadence:
    """With OPENCLAW_EOD_RECONCILE=1:
      • A signal whose strategy's next_fire_date is in the future is NOT
        cadence-skipped — _sharpe_cadence_path is reached.
      • _load_approved_carried_signals is called instead of
        _load_active_window_signals.
    """

    def test_cadence_pending_signal_not_skipped_gate_on(self, monkeypatch):
        """A carried signal with a future next_fire_date must NOT be skipped
        in EOD mode — it was already gated at 4PM[T] + 9:15AM pre-market."""
        monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')

        sharpe_cadence_calls = []

        def capturing_sharpe_cadence(signals, account_state, regime_state,
                                     params, confirmer):
            sharpe_cadence_calls.append({'signals': signals, 'regime': regime_state})
            return []

        monkeypatch.setattr(_sizer, '_sharpe_cadence_path', capturing_sharpe_cadence)

        # Signal with cadence-pending strategy (would be skipped in legacy mode).
        sigs = [_sig('S1')]
        state = {'S1': {'last_fire_date': date(2026, 5, 11),
                        'next_fire_date': date(2026, 5, 20),  # far future
                        'avg_holding_days': 9.0, 'source': 'live_signal_pnl'}}

        _sizer.size_positions(
            signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
            run_date=date(2026, 5, 12), strategy_state=state,
            regime_params=_params(), confirmer=None,
        )

        assert len(sharpe_cadence_calls) == 1, \
            '_sharpe_cadence_path must be reached in EOD mode even for cadence-pending signals'
        # The cadence-pending signal is present in what was passed to _sharpe_cadence_path
        passed_sids = [s['strategy_id'] for s in sharpe_cadence_calls[0]['signals']]
        assert 'S1' in passed_sids, \
            'Cadence-pending signal must reach _sharpe_cadence_path in EOD mode'

    def test_empty_approved_returns_empty_not_handoff_signals(self, monkeypatch):
        """In EOD mode, when _load_approved_carried_signals returns [] (every
        carried signal was vetoed or nothing was computed), size_positions must
        return [] — it must NOT fall back to sizing the handoff's signals.

        Without this guard, a vetoed handoff would silently re-enter the sizer
        via the legacy fallback. The flatten path belongs to run_reconcile (Task 8b).
        """
        monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')

        sharpe_cadence_calls = []

        def capturing_sharpe_cadence_for_empty_test(signals, account_state, regime_state,
                                                      params, confirmer):
            # We DON'T want _sharpe_cadence_path to return early before reaching
            # the loader, so use a minimal but real path. Instead, monkeypatch
            # the loader to return empty and let the real fallback guard run.
            sharpe_cadence_calls.append(signals)
            return []

        # Don't patch _sharpe_cadence_path here — let it run for real to exercise
        # the empty-approved early-return. Patch only strategy_weights and the loader.
        import unittest.mock as _mock
        fake_weights_row = {
            'strategy_id': 'S1', 'daily_weight': 1.0,
            'effective_sharpe': 1.0, 'cadence_days': 1.0,
        }

        def empty_approved_loader(weight_by_strat, cadence_by_strat=None, **_kw):
            return []  # every signal vetoed

        monkeypatch.setattr(_sizer, '_load_approved_carried_signals', empty_approved_loader)

        # Provide non-empty handoff signals — these must NOT get sized.
        sigs = [_sig('S1'), _sig('S2', ticker='TSLA')]

        with _mock.patch('execution.strategy_weights.load_current', return_value=[fake_weights_row]):
            result = _sizer.size_positions(
                signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
                run_date=date(2026, 5, 12), strategy_state={},
                regime_params=_params(), confirmer=None,
            )

        assert result == [], (
            'EOD mode with empty APPROVED set must return [] '
            '— must not fall back to sizing handoff signals'
        )

    def test_approved_loader_called_not_active_window_gate_on(self, monkeypatch):
        """_load_approved_carried_signals must be called (not _load_active_window_signals)
        when OPENCLAW_EOD_RECONCILE=1."""
        monkeypatch.setenv('OPENCLAW_EOD_RECONCILE', '1')

        active_window_calls = []
        approved_calls = []

        def fake_active_window(regime_state, weight_by_strat, cadence_by_strat):
            active_window_calls.append({'regime': regime_state})
            return []

        def fake_approved(weight_by_strat, cadence_by_strat=None, **_kw):
            approved_calls.append(list(weight_by_strat.keys()))
            return []  # empty → fallback to signals; ok for this loader-routing test

        monkeypatch.setattr(_sizer, '_load_active_window_signals', fake_active_window)
        monkeypatch.setattr(_sizer, '_load_approved_carried_signals', fake_approved)

        import unittest.mock as _mock
        # Provide a non-empty load_current result so weight_by_strat is populated
        # and the signal-loading branch is reached.
        fake_weights_row = {
            'strategy_id': 'S1', 'daily_weight': 1.0,
            'effective_sharpe': 1.0, 'cadence_days': 1.0,
        }
        with _mock.patch('execution.strategy_weights.load_current', return_value=[fake_weights_row]):
            sigs = [_sig('S1')]
            _sizer.size_positions(
                signals=sigs, account_state=_account(), regime={'state': 'LOW_VOL'},
                run_date=date(2026, 5, 12), strategy_state={},
                regime_params=_params(), confirmer=None,
            )

        assert len(approved_calls) == 1, \
            '_load_approved_carried_signals must be called in EOD mode'
        assert len(active_window_calls) == 0, \
            '_load_active_window_signals must NOT be called in EOD mode'
