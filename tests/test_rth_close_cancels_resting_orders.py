"""tests/test_rth_close_cancels_resting_orders.py — SP-6 Phase C Task 2.

Verify that the shared RTH `position close` site cancels any resting OPEN
equity orders (GTC OCO take-profit/stop legs reserve the full position qty →
`position close` 403s "insufficient qty available", proven 2026-06-04 on
AAPL/LEN/PTC/SPGI) BEFORE submitting the close — but ONLY when the
OPENCLAW_EOD_RECONCILE gate is ON. When OFF the legacy path is byte-identical
(zero new CLI calls). Cancellation is best-effort: a CLI error or a subprocess
hang (TimeoutExpired) must never abort the close attempt.

All Alpaca CLI access is mocked; no production Postgres / live Alpaca contact.
"""
from __future__ import annotations
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ae  # noqa: E402


def _needle_cli(responses):
    """Build a _run_alpaca_cli side_effect that matches the first positional
    arg list against substrings in `responses` keys and returns the mapped
    (ok, payload, err) tuple. The MagicMock wrapping this records the full
    call sequence in .call_args_list so tests can assert ORDER, not just
    presence."""
    def _call(args, timeout=30):
        joined = ' '.join(str(a) for a in args)
        for needle, resp in responses.items():
            if needle in joined:
                return resp
        raise AssertionError(f'unexpected CLI call: {joined}')
    return _call


def _cmd(call):
    """Extract the joined CLI command string from a mock call record."""
    args = call.args[0] if call.args else call.kwargs.get('args')
    return ' '.join(str(a) for a in args)


class _GateOnMixin:
    """Force OPENCLAW_EOD_RECONCILE=1 for the duration of a test."""
    def setUp(self):
        self._env = patch.dict(os.environ, {'OPENCLAW_EOD_RECONCILE': '1'})
        self._env.start()
        self.addCleanup(self._env.stop)


class TestCancelHelper(unittest.TestCase):
    """_cancel_open_equity_orders unit behavior (gate-independent helper)."""

    def test_cancels_only_matching_symbol_orders(self):
        order_list = [
            {'id': 'oid-A1', 'symbol': 'AAPL', 'side': 'sell'},
            {'id': 'oid-A2', 'symbol': 'AAPL', 'side': 'buy'},
            {'id': 'oid-X1', 'symbol': 'MSFT', 'side': 'sell'},
        ]
        responses = {
            'order list': (True, order_list, None),
            'order cancel': (True, {'status': 'canceled'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            n = ae._cancel_open_equity_orders('AAPL')
        self.assertEqual(n, 2)
        cancel_ids = []
        for c in mock_cli.call_args_list:
            cmd = _cmd(c)
            if 'order cancel' in cmd:
                cancel_ids.append(c.args[0][c.args[0].index('--order-id') + 1])
        self.assertEqual(set(cancel_ids), {'oid-A1', 'oid-A2'})
        self.assertNotIn('oid-X1', cancel_ids)

    def test_returns_zero_on_list_cli_error(self):
        err = {'exit_code': 1, 'error': 'boom'}
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli({'order list': (False, None, err)})) as mock_cli:
            n = ae._cancel_open_equity_orders('AAPL')
        self.assertEqual(n, 0)
        # never reaches a cancel call when the list fails
        self.assertTrue(all('order cancel' not in _cmd(c) for c in mock_cli.call_args_list))

    def test_no_matching_orders_no_cancels(self):
        responses = {'order list': (True, [{'id': 'x', 'symbol': 'TSLA'}], None)}
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            n = ae._cancel_open_equity_orders('AAPL')
        self.assertEqual(n, 0)
        self.assertTrue(all('order cancel' not in _cmd(c) for c in mock_cli.call_args_list))


class TestGateOnFullClose(_GateOnMixin, unittest.TestCase):
    def test_cancels_resting_then_closes_in_order(self):
        """Case 1: 2 resting orders on the target ticker + 1 on another ⇒
        exactly 2 `order cancel`, all BEFORE `position close`; other untouched."""
        order_list = [
            {'id': 'oid-A1', 'symbol': 'AAPL'},
            {'id': 'oid-A2', 'symbol': 'AAPL'},
            {'id': 'oid-X1', 'symbol': 'MSFT'},
        ]
        responses = {
            'order list': (True, order_list, None),
            'order cancel': (True, {'status': 'canceled'}, None),
            'position close': (True, {'id': 'close-1', 'qty': '10', 'notional': '2000'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            res, oid = ae._rth_position_close(
                'AAPL', 'COID1', notional_usd=2000.0, current_usd=2000.0,
                equity=100000.0, pct_nav=0.02)
        self.assertEqual(res['status'], 'submitted')
        self.assertEqual(oid, 'close-1')
        cmds = [_cmd(c) for c in mock_cli.call_args_list]
        cancel_idxs = [i for i, c in enumerate(cmds) if 'order cancel' in c]
        close_idxs = [i for i, c in enumerate(cmds) if 'position close' in c]
        self.assertEqual(len(cancel_idxs), 2)
        self.assertEqual(len(close_idxs), 1)
        # ORDER: every cancel strictly before the close
        for ci in cancel_idxs:
            self.assertLess(ci, close_idxs[0])
        # the other ticker's order id never appears in any cancel call
        self.assertFalse(any('oid-X1' in c for c in cmds))
        # exactly the two target ids were cancelled
        cancelled = [c for c in cmds if 'order cancel' in c]
        self.assertTrue(any('oid-A1' in c for c in cancelled))
        self.assertTrue(any('oid-A2' in c for c in cancelled))

    def test_no_resting_orders_proceeds_to_close(self):
        """Case 4: gate ON, no resting orders ⇒ no cancel calls, close runs."""
        responses = {
            'order list': (True, [{'id': 'z', 'symbol': 'TSLA'}], None),
            'position close': (True, {'id': 'close-2', 'qty': '5', 'notional': '1000'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            res, oid = ae._rth_position_close(
                'AAPL', 'COID4', notional_usd=1000.0, current_usd=1000.0,
                equity=50000.0, pct_nav=0.02)
        self.assertEqual(res['status'], 'submitted')
        cmds = [_cmd(c) for c in mock_cli.call_args_list]
        self.assertTrue(all('order cancel' not in c for c in cmds))
        self.assertTrue(any('position close' in c for c in cmds))


class TestGateOffByteIdentical(unittest.TestCase):
    def test_no_list_no_cancel_close_directly(self):
        """Case 2: gate OFF ⇒ NO order list / order cancel; close invoked directly."""
        # Ensure the gate is OFF regardless of ambient env.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop('OPENCLAW_EOD_RECONCILE', None)
            responses = {
                'position close': (True, {'id': 'close-3', 'qty': '8', 'notional': '1600'}, None),
            }
            with patch.object(ae, '_run_alpaca_cli',
                              side_effect=_needle_cli(responses)) as mock_cli:
                res, oid = ae._rth_position_close(
                    'AAPL', 'COID2', notional_usd=1600.0, current_usd=1600.0,
                    equity=80000.0, pct_nav=0.02)
        self.assertEqual(res['status'], 'submitted')
        self.assertEqual(oid, 'close-3')
        cmds = [_cmd(c) for c in mock_cli.call_args_list]
        self.assertEqual(len(cmds), 1)
        self.assertIn('position close', cmds[0])
        self.assertTrue(all('order list' not in c for c in cmds))
        self.assertTrue(all('order cancel' not in c for c in cmds))


class TestGateOnBestEffort(_GateOnMixin, unittest.TestCase):
    def test_list_cli_failure_still_closes(self):
        """Case 3a: order-list CLI failure ⇒ still proceeds to close."""
        responses = {
            'order list': (False, None, {'exit_code': 1, 'error': 'list failed'}),
            'position close': (True, {'id': 'close-4', 'qty': '3', 'notional': '600'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            res, oid = ae._rth_position_close(
                'AAPL', 'COID3', notional_usd=600.0, current_usd=600.0,
                equity=30000.0, pct_nav=0.02)
        self.assertEqual(res['status'], 'submitted')
        cmds = [_cmd(c) for c in mock_cli.call_args_list]
        self.assertTrue(any('position close' in c for c in cmds))

    def test_cancel_cli_failure_still_closes(self):
        """Case 3b: cancel CLI failure ⇒ still proceeds to close."""
        order_list = [{'id': 'oid-A1', 'symbol': 'AAPL'}]
        responses = {
            'order list': (True, order_list, None),
            'order cancel': (False, None, {'exit_code': 1, 'error': 'cancel failed'}),
            'position close': (True, {'id': 'close-5', 'qty': '4', 'notional': '800'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            res, oid = ae._rth_position_close(
                'AAPL', 'COID3b', notional_usd=800.0, current_usd=800.0,
                equity=40000.0, pct_nav=0.02)
        self.assertEqual(res['status'], 'submitted')
        cmds = [_cmd(c) for c in mock_cli.call_args_list]
        # a cancel WAS attempted (and failed), yet the close still ran after it
        cancel_idxs = [i for i, c in enumerate(cmds) if 'order cancel' in c]
        close_idxs = [i for i, c in enumerate(cmds) if 'position close' in c]
        self.assertTrue(cancel_idxs)
        self.assertTrue(close_idxs)
        self.assertLess(cancel_idxs[0], close_idxs[0])

    def test_timeout_in_cancel_helper_does_not_propagate(self):
        """A subprocess hang (TimeoutExpired) raised from the cancel helper
        must NOT propagate — the close is still attempted."""
        def _boom(ticker):
            raise subprocess.TimeoutExpired(cmd=['order', 'cancel'], timeout=10)

        responses = {
            'position close': (True, {'id': 'close-6', 'qty': '2', 'notional': '400'}, None),
        }
        with patch.object(ae, '_cancel_open_equity_orders', side_effect=_boom), \
             patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            res, oid = ae._rth_position_close(
                'AAPL', 'COID-T', notional_usd=400.0, current_usd=400.0,
                equity=20000.0, pct_nav=0.02)
        self.assertEqual(res['status'], 'submitted')
        self.assertEqual(oid, 'close-6')
        cmds = [_cmd(c) for c in mock_cli.call_args_list]
        self.assertTrue(any('position close' in c for c in cmds))


class TestGateOnPartialReduce(_GateOnMixin, unittest.TestCase):
    def test_partial_reduce_cancels_first(self):
        """Case 5: partial reduce (notional < current ⇒ --percentage path)
        also cancels resting orders first under gate ON."""
        order_list = [{'id': 'oid-R1', 'symbol': 'AAPL'}]
        responses = {
            'order list': (True, order_list, None),
            'order cancel': (True, {'status': 'canceled'}, None),
            'position close': (True, {'id': 'close-7', 'qty': '5', 'notional': '1000'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            res, oid = ae._rth_position_close(
                'AAPL', 'COID5', notional_usd=1000.0, current_usd=2000.0,
                equity=100000.0, pct_nav=0.01)
        self.assertEqual(res['status'], 'submitted')
        cmds = [_cmd(c) for c in mock_cli.call_args_list]
        # confirm this is the --percentage path
        self.assertTrue(any('--percentage' in c for c in cmds))
        cancel_idxs = [i for i, c in enumerate(cmds) if 'order cancel' in c]
        close_idxs = [i for i, c in enumerate(cmds) if 'position close' in c]
        self.assertEqual(len(cancel_idxs), 1)
        self.assertLess(cancel_idxs[0], close_idxs[0])


if __name__ == '__main__':
    unittest.main()
