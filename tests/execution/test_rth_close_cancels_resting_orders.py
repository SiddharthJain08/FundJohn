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

ROOT = Path(__file__).resolve().parents[2]
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
    """Force OPENCLAW_EOD_RECONCILE=1 for the duration of a test, and stub the
    cancel→close settle poll permissive (returns a terminal 'canceled' dict
    instantly). The helper now polls each successfully-cancelled order id to a
    terminal status before returning — without this default mock the real
    `_poll_to_terminal` would spawn live `regime_liquidator._run_cli`
    subprocesses (~10s each, live-broker contact). Tests that care about poll
    behavior override this with their own patch."""
    def setUp(self):
        self._env = patch.dict(os.environ, {'OPENCLAW_EOD_RECONCILE': '1'})
        self._env.start()
        self.addCleanup(self._env.stop)
        self._poll = patch.object(
            ae, '_poll_to_terminal', return_value={'status': 'canceled'})
        self._poll.start()
        self.addCleanup(self._poll.stop)


class TestCancelHelper(unittest.TestCase):
    """_cancel_open_equity_orders unit behavior (gate-independent helper)."""

    def setUp(self):
        # The helper now settle-polls each successfully-cancelled order id to a
        # terminal status before returning. Stub it permissive so the helper
        # unit tests don't spawn live `_poll_to_terminal` subprocesses; tests
        # asserting poll behavior override this locally.
        self._poll = patch.object(
            ae, '_poll_to_terminal', return_value={'status': 'canceled'})
        self._poll.start()
        self.addCleanup(self._poll.stop)

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

    def test_listing_is_symbol_filtered_and_paginated(self):
        """2026-08-18: the open-order listing MUST carry --symbols <ticker> and
        --limit 500. The unfiltered form returns the CLI's default 50-row page;
        once the open book outgrew a page (3,696 open orders on 08-18) the
        ticker's OCO legs were almost never in it → nothing cancelled → every
        orphan close 403'd "insufficient qty available (available: 0)"
        (261/356 rejected 08-14, 89/92 on 08-17). --symbols is the load-bearing
        part: --limit alone caps at 500 < book size."""
        responses = {
            'order list': (True, [{'id': 'oid-A1', 'symbol': 'AAPL'}], None),
            'order cancel': (True, {'status': 'canceled'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            n = ae._cancel_open_equity_orders('AAPL')
        self.assertEqual(n, 1)
        list_cmds = [_cmd(c) for c in mock_cli.call_args_list if 'order list' in _cmd(c)]
        self.assertEqual(len(list_cmds), 1)
        self.assertIn('--symbols AAPL', list_cmds[0])
        self.assertIn('--limit 500', list_cmds[0])

    def test_crypto_listing_is_symbol_filtered_and_paginated(self):
        """The crypto sibling (_cancel_open_crypto_stops) had the identical
        unpaginated listing — the equity open-order flood pushed crypto stops
        off the 50-row page. Same pin: --symbols (slashed crypto form) +
        --limit 500."""
        responses = {
            'order list': (True, [{'id': 'oid-C1', 'symbol': 'ETH/USD'}], None),
            'order cancel': (True, {'status': 'canceled'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli:
            n = ae._cancel_open_crypto_stops('ETH/USD')
        self.assertEqual(n, 1)
        list_cmds = [_cmd(c) for c in mock_cli.call_args_list if 'order list' in _cmd(c)]
        self.assertEqual(len(list_cmds), 1)
        self.assertIn('--symbols ETH/USD', list_cmds[0])
        self.assertIn('--limit 500', list_cmds[0])

    def test_polls_position_qty_available_after_cancels(self):
        """2026-08-18 settle-race hardening: after cancelling + polling the OCO
        PARENT (the only top-level row `order list` returns), the helper polls
        the POSITION's qty_available. The broker releases the qty hold when the
        LEG finishes cancelling, which can lag the parent's terminal status by
        many seconds around the open — proven live 08-18: cancel ACK 13:30:09,
        close 403 13:30:22, leg release 13:30:28. qty_available is the close's
        actual precondition; order status is not."""
        seq = {'polls': 0}

        def _cli(args, timeout=30):
            joined = ' '.join(str(a) for a in args)
            if 'order list' in joined:
                return (True, [{'id': 'oid-A1', 'symbol': 'AAPL'}], None)
            if 'order cancel' in joined:
                return (True, {'status': 'canceled'}, None)
            if 'position get' in joined:
                seq['polls'] += 1
                if seq['polls'] == 1:   # first poll: leg still holds the qty
                    return (True, {'qty': '3', 'qty_available': '0'}, None)
                return (True, {'qty': '3', 'qty_available': '3'}, None)
            raise AssertionError(f'unexpected CLI call: {joined}')

        with patch.object(ae, '_run_alpaca_cli', side_effect=_cli):
            n = ae._cancel_open_equity_orders('AAPL')
        self.assertEqual(n, 1)
        # polled at least twice: held on the first pass, released on the second
        self.assertEqual(seq['polls'], 2)

    def test_order_id_only_fallback_still_cancelled(self):
        """MINOR finding: every prior fixture carried 'id', so the
        `o.get('id') or o.get('order_id')` fallback was unexercised. An order
        dict with ONLY `order_id` (no `id`) must still be cancelled — and its
        id is the one polled to terminal."""
        order_list = [{'order_id': 'oid-NOID', 'symbol': 'AAPL'}]  # no 'id' key
        responses = {
            'order list': (True, order_list, None),
            'order cancel': (True, {'status': 'canceled'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli, \
             patch.object(ae, '_poll_to_terminal',
                          return_value={'status': 'canceled'}) as mock_poll:
            n = ae._cancel_open_equity_orders('AAPL')
        self.assertEqual(n, 1)
        cancel_ids = []
        for c in mock_cli.call_args_list:
            if 'order cancel' in _cmd(c):
                cancel_ids.append(c.args[0][c.args[0].index('--order-id') + 1])
        self.assertEqual(cancel_ids, ['oid-NOID'])
        # the fallback id is the one settle-polled
        mock_poll.assert_called_once()
        self.assertEqual(mock_poll.call_args.args[0], 'oid-NOID')


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


class TestGateOnSettlePoll(_GateOnMixin, unittest.TestCase):
    """The cancel→close settle race: cancelled OCO legs must be polled to
    terminal (the async pending_cancel→canceled qty-hold release) BEFORE the
    close fires, else `position close` can still 403 'insufficient qty'."""

    def test_cancelled_ids_polled_before_close(self):
        """New test 1 (poll-order): every settle poll happens AFTER the cancels
        and BEFORE the close. `_poll_to_terminal` and `_run_alpaca_cli` are
        separate mocks with no shared clock, so both append to ONE shared
        timeline and the order is asserted on that single sequence."""
        timeline = []  # ordered ('cli'|'poll', detail) across both mocks

        order_list = [
            {'id': 'oid-A1', 'symbol': 'AAPL'},
            {'id': 'oid-A2', 'symbol': 'AAPL'},
        ]
        responses = {
            'order list': (True, order_list, None),
            'order cancel': (True, {'status': 'canceled'}, None),
            'position close': (True, {'id': 'close-P', 'qty': '10', 'notional': '2000'}, None),
        }
        base = _needle_cli(responses)

        def _cli(args, timeout=30):
            joined = ' '.join(str(a) for a in args)
            if 'order list' in joined:
                timeline.append(('cli', 'list'))
            elif 'order cancel' in joined:
                timeline.append(('cli', 'cancel'))
            elif 'position close' in joined:
                timeline.append(('cli', 'close'))
            return base(args, timeout=timeout)

        def _poll(order_id, timeout_s=10, interval_s=0.5):
            timeline.append(('poll', order_id))
            return {'status': 'canceled'}

        with patch.object(ae, '_run_alpaca_cli', side_effect=_cli), \
             patch.object(ae, '_poll_to_terminal', side_effect=_poll):
            res, oid = ae._rth_position_close(
                'AAPL', 'COID-SP', notional_usd=2000.0, current_usd=2000.0,
                equity=100000.0, pct_nav=0.02)

        self.assertEqual(res['status'], 'submitted')
        self.assertEqual(oid, 'close-P')

        kinds = [t[0] for t in timeline]
        poll_idxs = [i for i, t in enumerate(timeline) if t[0] == 'poll']
        cancel_idxs = [i for i, t in enumerate(timeline)
                       if t == ('cli', 'cancel')]
        close_idxs = [i for i, t in enumerate(timeline)
                      if t == ('cli', 'close')]
        self.assertEqual(len(poll_idxs), 2)        # both cancelled legs polled
        self.assertEqual(len(cancel_idxs), 2)
        self.assertEqual(len(close_idxs), 1)
        # load-bearing relative order: cancels → polls → close
        self.assertLess(max(cancel_idxs), min(poll_idxs))
        self.assertLess(max(poll_idxs), close_idxs[0])
        # exactly the two cancelled ids were polled
        polled = {t[1] for t in timeline if t[0] == 'poll'}
        self.assertEqual(polled, {'oid-A1', 'oid-A2'})
        # sanity: the close is the last CLI action
        self.assertEqual(kinds[-1], 'cli')

    def test_poll_timeout_none_still_closes(self):
        """New test 2 (best-effort preserved): a settle poll returning None
        (timeout) must NOT block the close — degrades to the pre-change 403,
        never aborts."""
        order_list = [{'id': 'oid-A1', 'symbol': 'AAPL'}]
        responses = {
            'order list': (True, order_list, None),
            'order cancel': (True, {'status': 'canceled'}, None),
            'position close': (True, {'id': 'close-TO', 'qty': '7', 'notional': '1400'}, None),
        }
        with patch.object(ae, '_run_alpaca_cli',
                          side_effect=_needle_cli(responses)) as mock_cli, \
             patch.object(ae, '_poll_to_terminal',
                          return_value=None) as mock_poll:
            res, oid = ae._rth_position_close(
                'AAPL', 'COID-TO', notional_usd=1400.0, current_usd=1400.0,
                equity=70000.0, pct_nav=0.02)
        self.assertEqual(res['status'], 'submitted')
        self.assertEqual(oid, 'close-TO')
        mock_poll.assert_called_once()
        cmds = [_cmd(c) for c in mock_cli.call_args_list]
        cancel_idxs = [i for i, c in enumerate(cmds) if 'order cancel' in c]
        close_idxs = [i for i, c in enumerate(cmds) if 'position close' in c]
        self.assertTrue(cancel_idxs)
        self.assertTrue(close_idxs)
        self.assertLess(cancel_idxs[0], close_idxs[0])


if __name__ == '__main__':
    unittest.main()
