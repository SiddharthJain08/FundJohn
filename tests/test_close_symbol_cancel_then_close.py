"""tests/test_close_symbol_cancel_then_close.py

_close_symbol() is the shared flatten path used by both the position circuit
breaker and operator forced-liquidation. 2026-06-08: it 403'd repeatedly on a
GAMB short whose full qty was reserved by a resting GTC OCO take-profit —
`alpaca position close` returned code 40310000 "insufficient qty available for
order (requested: 15952, available: 0)", so the breaker fired every 5 min for
~90 min without ever flattening.

Fix: try `position close` first; ONLY when it fails with the insufficient-qty
error (qty held by resting orders) cancel that symbol's open orders and retry
once. A position that closes cleanly keeps its protective brackets untouched —
we strip protection only when it actually blocks the flatten.

Run:
    pytest tests/test_close_symbol_cancel_then_close.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import regime_liquidator as rl  # noqa: E402


INSUFFICIENT_QTY_ERR = {
    'exit_code': 1, 'status': 403, 'code': 40310000,
    'error': 'insufficient qty available for order (requested: 15952, available: 0)',
    'error_json': {'code': 40310000},
}
MARKET_CLOSED_ERR = {
    'exit_code': 1, 'status': 403, 'code': 42210000,
    'error': 'order submission is not allowed outside of market hours',
}


# ── _is_insufficient_qty ─────────────────────────────────────────────────────

class TestIsInsufficientQty:
    def test_true_on_code(self):
        assert rl._is_insufficient_qty(INSUFFICIENT_QTY_ERR) is True

    def test_true_on_message_without_code(self):
        assert rl._is_insufficient_qty({'error': 'insufficient qty available'}) is True

    def test_false_on_other_error(self):
        assert rl._is_insufficient_qty(MARKET_CLOSED_ERR) is False

    def test_false_on_non_dict(self):
        assert rl._is_insufficient_qty(None) is False


# ── _cancel_symbol_orders ────────────────────────────────────────────────────

class TestCancelSymbolOrders:
    def test_cancels_only_matching_symbol(self, monkeypatch):
        open_orders = [
            {'id': 'gamb-1', 'symbol': 'GAMB', 'order_class': 'oco'},
            {'id': 'aapl-1', 'symbol': 'AAPL', 'order_class': 'simple'},
            {'id': 'gamb-2', 'symbol': 'GAMB', 'order_class': 'simple'},
        ]
        cancelled = []
        monkeypatch.setattr(rl, '_load_open_orders', lambda: open_orders)
        monkeypatch.setattr(rl, '_cancel_order', lambda oid: cancelled.append(oid) or (True, {}))

        n = rl._cancel_symbol_orders('GAMB')

        assert n == 2
        assert cancelled == ['gamb-1', 'gamb-2']   # AAPL untouched

    def test_counts_only_successful_cancels(self, monkeypatch):
        open_orders = [{'id': 'a', 'symbol': 'GAMB'}, {'id': 'b', 'symbol': 'GAMB'}]
        monkeypatch.setattr(rl, '_load_open_orders', lambda: open_orders)
        monkeypatch.setattr(rl, '_cancel_order',
                            lambda oid: (oid == 'a', {}))   # 'b' fails
        assert rl._cancel_symbol_orders('GAMB') == 1


# ── _close_symbol: cancel-then-close ─────────────────────────────────────────

class TestCloseSymbol:
    def test_clean_close_keeps_brackets(self, monkeypatch):
        """A position that closes outright must NOT have its orders cancelled."""
        monkeypatch.setattr(rl, '_run_cli',
                            lambda args, timeout=30: (True, {'status': 'accepted'}, None))
        def _boom(symbol):
            raise AssertionError('cancelled orders on a clean close')
        monkeypatch.setattr(rl, '_cancel_symbol_orders', _boom)

        ok, payload = rl._close_symbol('GAMB', -15952)
        assert ok is True
        assert payload == {'status': 'accepted'}

    def test_insufficient_qty_cancels_then_retries(self, monkeypatch):
        """First close 403s insufficient-qty → cancel orders → retry succeeds."""
        close_calls = {'n': 0}

        def fake_run_cli(args, timeout=30):
            if args[:2] == ['position', 'close']:
                close_calls['n'] += 1
                if close_calls['n'] == 1:
                    return False, None, INSUFFICIENT_QTY_ERR
                return True, {'status': 'accepted'}, None
            raise AssertionError(f'unexpected CLI call: {args}')

        cancelled = []
        monkeypatch.setattr(rl, '_run_cli', fake_run_cli)
        monkeypatch.setattr(rl, '_cancel_symbol_orders',
                            lambda sym: cancelled.append(sym) or 1)

        ok, payload = rl._close_symbol('GAMB', -15952)

        assert ok is True
        assert close_calls['n'] == 2            # retried after cancel
        assert cancelled == ['GAMB']

    def test_other_error_does_not_cancel(self, monkeypatch):
        """A non-qty failure (e.g. market closed) must NOT strip protective orders."""
        monkeypatch.setattr(rl, '_run_cli',
                            lambda args, timeout=30: (False, None, MARKET_CLOSED_ERR))
        def _boom(symbol):
            raise AssertionError('cancelled orders on an unrelated close failure')
        monkeypatch.setattr(rl, '_cancel_symbol_orders', _boom)

        ok, payload = rl._close_symbol('GAMB', -15952)
        assert ok is False
        assert payload == MARKET_CLOSED_ERR

    def test_insufficient_qty_but_no_orders_to_cancel_returns_error(self, monkeypatch):
        """If nothing was cancellable, don't loop a pointless retry — surface
        the original error."""
        def fake_run_cli(args, timeout=30):
            return False, None, INSUFFICIENT_QTY_ERR
        monkeypatch.setattr(rl, '_run_cli', fake_run_cli)
        monkeypatch.setattr(rl, '_cancel_symbol_orders', lambda sym: 0)

        ok, payload = rl._close_symbol('GAMB', -15952)
        assert ok is False
        assert payload == INSUFFICIENT_QTY_ERR
