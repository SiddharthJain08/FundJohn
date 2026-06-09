"""tests/test_reconcile_broker_closes.py

Part ② of the trade-output accuracy work (2026-06-08). engine.update_pnl only
writes signal_pnl 'closed' rows on price-crossing stop/target, so the cycle's
manual closes (liquidations, circuit-breaker fires) leave signal_pnl 'open' →
#trade-reports + dashboard undercount them.

reconcile_broker_closes closes still-open signals for tickers the cycle closed
(breaker/liquidation) that are now FLAT at the broker, via the existing
drop_signal_close upsert, with a derived close_reason.

Scoped to THIS cycle's close events (never the historical phantom backlog).
SAFETY: empty broker ⇒ no mass-close; a FAILED close (still held) stays open.

Run:
    pytest tests/test_reconcile_broker_closes.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import open_reconcile  # noqa: E402


class _DispatchCursor:
    """Scripted fetchone/fetchall keyed by the table named in the SQL."""
    def __init__(self, breaker=False, liquidation=False,
                 breaker_tickers=(), liq_tickers=()):
        self._breaker = breaker
        self._liq = liquidation
        self._breaker_tickers = list(breaker_tickers)
        self._liq_tickers = list(liq_tickers)
        self._last = ''

    def execute(self, sql, params=None):
        self._last = sql

    def fetchone(self):
        if 'circuit_breaker_fires' in self._last:
            return (1,) if self._breaker else None
        if 'alpaca_liquidations' in self._last:
            return (1,) if self._liq else None
        return None

    def fetchall(self):
        if 'circuit_breaker_fires' in self._last:
            return [(t,) for t in self._breaker_tickers]
        if 'alpaca_liquidations' in self._last:
            return [(t,) for t in self._liq_tickers]
        return []


class TestDeriveCloseReason:
    def test_circuit_breaker(self):
        assert open_reconcile._derive_close_reason(
            _DispatchCursor(breaker=True), 'GAMB', '2026-06-08') == 'circuit_breaker'

    def test_liquidation(self):
        assert open_reconcile._derive_close_reason(
            _DispatchCursor(liquidation=True), 'X', 'd') == 'liquidation'

    def test_breaker_takes_precedence(self):
        assert open_reconcile._derive_close_reason(
            _DispatchCursor(breaker=True, liquidation=True), 'X', 'd') == 'circuit_breaker'

    def test_fallback_manual_close(self):
        assert open_reconcile._derive_close_reason(
            _DispatchCursor(), 'X', 'd') == 'manual_close'


class TestClosedTodayTickers:
    def test_unions_breaker_and_liquidation(self):
        cur = _DispatchCursor(breaker_tickers=['GAMB', 'TSLA'], liq_tickers=['TSLA', 'NVDA'])
        assert open_reconcile._closed_today_tickers(cur, '2026-06-08') == {'GAMB', 'TSLA', 'NVDA'}

    def test_empty_when_no_events(self):
        assert open_reconcile._closed_today_tickers(_DispatchCursor(), 'd') == set()


class TestReconcileBrokerCloses:
    def _wire(self, monkeypatch, open_sigs, closed_today=('GAMB',), reason='liquidation'):
        monkeypatch.setattr(open_reconcile, '_closed_today_tickers',
                            lambda cur, rd: set(closed_today))
        monkeypatch.setattr(open_reconcile, '_open_signals_for_close_check',
                            lambda cur, rd, scope: open_sigs)
        monkeypatch.setattr(open_reconcile, '_derive_close_reason',
                            lambda cur, t, rd: reason)
        calls = []
        monkeypatch.setattr(open_reconcile, 'drop_signal_close',
                            lambda cur, sid, tk, px, reason=None: calls.append((sid, tk, px, reason)))
        return calls

    def test_flat_signal_for_closed_ticker_is_closed(self, monkeypatch):
        sigs = [{'signal_id': 'g', 'ticker': 'GAMB', 'close_price': 2.40}]
        calls = self._wire(monkeypatch, sigs, closed_today=('GAMB',))
        closed = open_reconcile.reconcile_broker_closes(
            cur=None, run_date='2026-06-08',
            broker_loader=lambda: {'AAPL': 18000.0})       # GAMB flat
        assert closed == {'g': 'liquidation'}
        assert calls == [('g', 'GAMB', 2.40, 'liquidation')]

    def test_failed_close_still_held_stays_open(self, monkeypatch):
        """GAMB was breaker/liquidation-targeted today but the close FAILED — it's
        still held at the broker (the OCO-blocked GAMB case). Must NOT be marked
        closed."""
        sigs = [{'signal_id': 'g', 'ticker': 'GAMB', 'close_price': 2.40}]
        calls = self._wire(monkeypatch, sigs, closed_today=('GAMB',))
        closed = open_reconcile.reconcile_broker_closes(
            cur=None, run_date='2026-06-08',
            broker_loader=lambda: {'GAMB': 39000.0})       # GAMB STILL HELD
        assert closed == {}
        assert calls == []

    def test_no_cycle_closes_is_noop(self, monkeypatch):
        sigs = [{'signal_id': 'g', 'ticker': 'GAMB', 'close_price': 2.40}]
        calls = self._wire(monkeypatch, sigs, closed_today=())   # nothing closed today
        closed = open_reconcile.reconcile_broker_closes(
            cur=None, run_date='d', broker_loader=lambda: {'X': 9999.0})
        assert closed == {}
        assert calls == []

    def test_empty_broker_does_not_mass_close(self, monkeypatch):
        """Closes happened this cycle but the broker fetch returned nothing —
        fail-safe: close nothing rather than risk a phantom mass-close."""
        sigs = [{'signal_id': 'g', 'ticker': 'GAMB', 'close_price': 2.40}]
        calls = self._wire(monkeypatch, sigs, closed_today=('GAMB',))
        closed = open_reconcile.reconcile_broker_closes(
            cur=None, run_date='d', broker_loader=lambda: {})
        assert closed == {}
        assert calls == []

    def test_skips_signal_with_no_mark(self, monkeypatch):
        sigs = [{'signal_id': 'x', 'ticker': 'GAMB', 'close_price': None}]
        calls = self._wire(monkeypatch, sigs, closed_today=('GAMB',))
        closed = open_reconcile.reconcile_broker_closes(
            cur=None, run_date='d', broker_loader=lambda: {'Y': 9999.0})
        assert closed == {}
        assert calls == []

    def test_tiny_broker_value_treated_as_flat(self, monkeypatch):
        sigs = [{'signal_id': 'g', 'ticker': 'GAMB', 'close_price': 2.40}]
        calls = self._wire(monkeypatch, sigs, closed_today=('GAMB',))
        closed = open_reconcile.reconcile_broker_closes(
            cur=None, run_date='d', broker_loader=lambda: {'OTHER': 5000.0, 'GAMB': 0.10})
        assert closed == {'g': 'liquidation'}              # GAMB 0.10 < $1 → flat
