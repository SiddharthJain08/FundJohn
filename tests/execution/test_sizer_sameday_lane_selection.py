"""Which SIGNAL LOADER the sizer picks per execution lane.

2026-07-30/31 both traded ZERO despite 173 and 1 computed signals, and
alpaca_submissions had no rows at all for either day. Cause: the three signal
loading branches in _sharpe_cadence_path were still spelled
`OPENCLAW_EOD_RECONCILE == '1'`. d573e45 (2026-07-29) fixed exactly this
conflation in regime_blended_sizer_live.py and never propagated it here, so the
same-day lane self-loaded the APPROVED carried set — which the same-day lane
NEVER writes (its 15:00 chain writes COMPUTED; premarket_gate is the only
APPROVED writer and in protect mode it scores the BOOK, and ic_gate is
default-OFF). The book survived only on a decaying inventory of 07-27 EOD-era
approvals: 121 in-window on 07-30 -> 6 on 07-31.

The old tests could not catch this: they `delenv('OPENCLAW_EOD_RECONCILE')`
and mock `_load_active_window_signals`, so they take the else-branch under
BOTH spellings. These pin the branch itself.

The distinction that matters — in same-day mode BOTH of these are true at once,
which is the whole trap:
    OPENCLAW_EOD_RECONCILE=1        (premarket reconcile ON, for protective closes)
    OPENCLAW_EOD_SIGNAL_REGISTER=0  (the EOD *timing model* is NOT driving signals)
"""
from __future__ import annotations

import logging
import sys
import unittest.mock as _mock
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

import execution.regime_blended_sizer as _sizer  # noqa: E402


def _weights_row(sid='S_demo'):
    return {'strategy_id': sid, 'daily_weight': 1.0, 'effective_sharpe': 2.0,
            'cadence_days': 3.0, 'bt_n': 500}


@pytest.fixture
def loader_spies(monkeypatch):
    """Record which loader ran; both return [] so the path exits early and
    never reaches the DB, the broker, or the conviction gate."""
    calls = []
    monkeypatch.setattr(_sizer, '_load_approved_carried_signals',
                        lambda wbs, cbs, regime_state=None: calls.append('approved') or [])
    monkeypatch.setattr(_sizer, '_load_active_window_signals',
                        lambda rstate, wbs, cbs: calls.append('active_window') or [])
    monkeypatch.setattr(_sizer, '_load_lambda',
                        lambda default=2.0, *, intraday=False: 1.0)
    monkeypatch.setattr(_sizer, '_load_broker_positions_usd', lambda: {})
    return calls


def _drive(monkeypatch, loader_spies, *, reconcile, signal_register):
    for name, val in (('OPENCLAW_EOD_RECONCILE', reconcile),
                      ('OPENCLAW_EOD_SIGNAL_REGISTER', signal_register)):
        if val is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, val)
    monkeypatch.delenv('OPENCLAW_INTRADAY_REDEPLOY', raising=False)
    with _mock.patch('execution.strategy_weights.load_current',
                     return_value=[_weights_row()]):
        _sizer._sharpe_cadence_path(
            signals=[], account_state={'equity': 100_000.0},
            regime_state='LOW_VOL', params={'liquidity_param': 1.0},
            confirmer=None)
    return loader_spies


class TestLaneSelection:
    def test_sameday_lane_uses_active_window_not_approved(self, monkeypatch, loader_spies):
        """THE REGRESSION. Same-day: reconcile ON, signal-register OFF."""
        calls = _drive(monkeypatch, loader_spies, reconcile='1', signal_register='0')
        assert calls == ['active_window'], (
            'same-day lane must read the active-window (status=open) signals it '
            'actually writes; reading the APPROVED carried set it never writes '
            'is the 2026-07-30/31 zero-trade bug')

    def test_eod_lane_still_uses_approved_carried(self, monkeypatch, loader_spies):
        """EOD mode sets BOTH flags — must stay byte-identical."""
        calls = _drive(monkeypatch, loader_spies, reconcile='1', signal_register='1')
        assert calls == ['approved']

    def test_neither_flag_uses_active_window(self, monkeypatch, loader_spies):
        calls = _drive(monkeypatch, loader_spies, reconcile=None, signal_register=None)
        assert calls == ['active_window']

    def test_reconcile_alone_never_selects_the_eod_loader(self, monkeypatch, loader_spies):
        """OPENCLAW_EOD_RECONCILE means 'the premarket reconcile job is enabled'.
        It must never by itself imply the EOD timing model drives signals."""
        calls = _drive(monkeypatch, loader_spies, reconcile='1', signal_register=None)
        assert 'approved' not in calls


class TestSizerLoggingIsConfigured:
    """The sizer's diagnostics were being written and thrown away: the `trade`
    step runs as a bare script, nothing called basicConfig, so every
    logger.info in regime_blended_sizer.py hit a handler-less root logger.
    Production showed `size_positions produced 0 orders` with no reason.
    Same defect+fix as premarket_gate._ensure_logging (b464747)."""

    def test_ensure_logging_installs_a_handler(self, monkeypatch):
        from execution import regime_blended_sizer_live as live
        root = logging.getLogger()
        saved = root.handlers[:]
        try:
            root.handlers = []
            live._ensure_logging()
            assert root.handlers, 'sizer diagnostics would be silently discarded'
        finally:
            root.handlers = saved

    def test_ensure_logging_is_idempotent(self):
        from execution import regime_blended_sizer_live as live
        root = logging.getLogger()
        saved = root.handlers[:]
        try:
            sentinel = logging.NullHandler()
            root.handlers = [sentinel]
            live._ensure_logging()
            assert root.handlers == [sentinel], 'must not stomp a caller\'s handlers'
        finally:
            root.handlers = saved
