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

ROOT = Path(__file__).resolve().parents[2]
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
        monkeypatch.setattr(rl, '_await_symbol_orders_gone',
                            lambda sym, budget_s=8.0: True)

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


# ── zombie pending_cancel hardening (2026-07-29, SNDK) ───────────────────────

INSUFFICIENT_QTY_ERR_AVAIL11 = {
    'exit_code': 1, 'status': 403, 'code': 40310000,
    'error': 'insufficient qty available for order (requested: 13, available: 11)',
    'error_json': {'code': 40310000},
}


class TestQtyFromInsufficientError:
    def test_parses_available_and_requested(self):
        assert rl._qty_from_insufficient_error(INSUFFICIENT_QTY_ERR_AVAIL11) == (11.0, 13.0)

    def test_zero_available(self):
        assert rl._qty_from_insufficient_error(INSUFFICIENT_QTY_ERR) == (0.0, 15952.0)

    def test_absent_numbers(self):
        assert rl._qty_from_insufficient_error({'error': 'insufficient qty available'}) == (None, None)

    def test_none_detail(self):
        assert rl._qty_from_insufficient_error(None) == (None, None)


class TestZombieOrderPartialClose:
    """SNDK 2026-07-28: a GTC stop stuck in pending_cancel for 9 days held 2
    of 13 shares. Cancel fails (n=0), so the old code never retried and never
    flattened — the breaker 403'd every 5 min while the position bled 20%
    past its dead stop. New behavior: close the broker-reported available qty."""

    def test_uncancellable_zombie_closes_available_qty(self, monkeypatch):
        calls = []

        def fake_run_cli(args, timeout=30):
            calls.append(args)
            if args[:2] == ['position', 'close'] and '--qty' not in args:
                return False, None, INSUFFICIENT_QTY_ERR_AVAIL11
            if args[:2] == ['position', 'close'] and '--qty' in args:
                return True, {'status': 'accepted'}, None
            raise AssertionError(f'unexpected CLI call: {args}')

        monkeypatch.setattr(rl, '_run_cli', fake_run_cli)
        monkeypatch.setattr(rl, '_cancel_symbol_orders', lambda sym: 0)  # zombie: nothing cancellable

        ok, payload = rl._close_symbol('SNDK', 13)

        assert ok is True
        assert payload['partial_flatten'] is True
        assert payload['closed_qty'] == 11.0
        assert payload['hostage_qty'] == 2.0
        partial = [a for a in calls if '--qty' in a]
        assert partial and partial[0][partial[0].index('--qty') + 1] == '11'

    def test_retry_still_blocked_falls_back_to_partial(self, monkeypatch):
        """Cancels 'succeed' but shares stay reserved (async cancel never
        lands) → full-close retry 403s again → partial close of available."""
        close_calls = {'n': 0}

        def fake_run_cli(args, timeout=30):
            if args[:2] == ['position', 'close'] and '--qty' not in args:
                close_calls['n'] += 1
                return False, None, INSUFFICIENT_QTY_ERR_AVAIL11
            if args[:2] == ['position', 'close'] and '--qty' in args:
                return True, {'status': 'accepted'}, None
            raise AssertionError(f'unexpected CLI call: {args}')

        monkeypatch.setattr(rl, '_run_cli', fake_run_cli)
        monkeypatch.setattr(rl, '_cancel_symbol_orders', lambda sym: 1)
        monkeypatch.setattr(rl, '_await_symbol_orders_gone', lambda sym, budget_s=8.0: False)

        ok, payload = rl._close_symbol('SNDK', 13)

        assert ok is True
        assert close_calls['n'] == 2                # full close tried twice
        assert payload['partial_flatten'] is True

    def test_partial_close_failure_surfaces_error(self, monkeypatch):
        boom = {'exit_code': 1, 'status': 500, 'error': 'internal'}

        def fake_run_cli(args, timeout=30):
            if '--qty' in args:
                return False, None, boom
            return False, None, INSUFFICIENT_QTY_ERR_AVAIL11

        monkeypatch.setattr(rl, '_run_cli', fake_run_cli)
        monkeypatch.setattr(rl, '_cancel_symbol_orders', lambda sym: 0)

        ok, payload = rl._close_symbol('SNDK', 13)
        assert ok is False
        assert payload == boom

    def test_zero_available_still_surfaces_original_error(self, monkeypatch):
        """available: 0 (the GAMB short case) — nothing to partially close;
        original error must surface unchanged."""
        monkeypatch.setattr(rl, '_run_cli',
                            lambda args, timeout=30: (False, None, INSUFFICIENT_QTY_ERR))
        monkeypatch.setattr(rl, '_cancel_symbol_orders', lambda sym: 0)

        ok, payload = rl._close_symbol('GAMB', -15952)
        assert ok is False
        assert payload == INSUFFICIENT_QTY_ERR


class TestAwaitSymbolOrdersGone:
    def test_returns_true_when_orders_clear(self, monkeypatch):
        state = {'polls': 0}

        def fake_orders():
            state['polls'] += 1
            return [{'id': 'z', 'symbol': 'SNDK'}] if state['polls'] == 1 else []

        monkeypatch.setattr(rl, '_load_open_orders', fake_orders)
        monkeypatch.setattr(rl.time, 'sleep', lambda s: None)
        assert rl._await_symbol_orders_gone('SNDK', budget_s=5.0) is True

    def test_returns_false_on_persistent_zombie(self, monkeypatch):
        monkeypatch.setattr(rl, '_load_open_orders',
                            lambda: [{'id': 'z', 'symbol': 'SNDK'}])
        monkeypatch.setattr(rl.time, 'sleep', lambda s: None)
        assert rl._await_symbol_orders_gone('SNDK', budget_s=0.2) is False
