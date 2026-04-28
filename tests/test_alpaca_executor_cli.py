"""tests/test_alpaca_executor_cli.py

Unit tests for src/execution/alpaca_executor.py CLI subprocess path
(Phase 1.1 of alpaca-cli integration). All subprocess.run calls and
the requests.Session for the pre-flight quote fetch are mocked — no
live Alpaca API calls. Burning paper trades on test runs is forbidden.

Run:
    pytest tests/test_alpaca_executor_cli.py -v
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ae  # noqa: E402


def _mock_proc(returncode=0, stdout='', stderr=''):
    """Build a mock subprocess.CompletedProcess-like object."""
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _mock_session(quote_bid=100.0, quote_ask=100.10, quote_status=200):
    """Build a mock requests.Session for the pre-flight quote fetch.

    Default: returns a 200 with synthetic mid-price quote so the snap
    path is exercised. Set quote_status=500 to force the quote-fetch
    failure branch (order submitted unsnapped).
    """
    sess = MagicMock()
    sess._base = 'https://paper-api.alpaca.markets'
    quote_resp = MagicMock()
    quote_resp.status_code = quote_status
    quote_resp.json.return_value = {'quote': {'bp': quote_bid, 'ap': quote_ask}}
    sess.get.return_value = quote_resp
    return sess


class TestSubmitHappyPath(unittest.TestCase):
    def test_submit_happy_path_shells_correct_args(self):
        # Mock quote near entry so the pre-flight snap doesn't fire.
        # (AAPL near $150 → quote needs to be near $150 too.)
        sess = _mock_session(quote_bid=149.95, quote_ask=150.05)
        order = {
            'ticker':       'AAPL',
            'strategy_id':  'S5_max_pain',
            'direction':    'long',
            'pct_nav':      0.02,
            'entry':        150.00,
            'stop':         140.00,    # below quote → no snap
            't1':           160.00,
        }
        success_stdout = json.dumps({'id': 'order-uuid-123', 'status': 'accepted'})
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, success_stdout, '')) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')

        self.assertTrue(mock_run.called)
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[0], ae.ALPACA_CLI)
        self.assertEqual(argv[1:3], ['order', 'submit'])
        # Required flags
        self.assertEqual(argv[argv.index('--symbol') + 1], 'AAPL')
        self.assertEqual(argv[argv.index('--side') + 1], 'buy')
        self.assertEqual(argv[argv.index('--type') + 1], 'market')
        self.assertEqual(argv[argv.index('--time-in-force') + 1], 'day')
        self.assertEqual(argv[argv.index('--order-class') + 1], 'bracket')
        coid = argv[argv.index('--client-order-id') + 1]
        self.assertTrue(coid.startswith('AX20260428_AAPL_S5_max_pain'),
                        f'unexpected coid {coid}')
        # take-profit + stop-loss are JSON-encoded sub-objects
        tp_json = json.loads(argv[argv.index('--take-profit') + 1])
        sl_json = json.loads(argv[argv.index('--stop-loss')   + 1])
        self.assertEqual(tp_json, {'limit_price': '160.00'})
        self.assertEqual(sl_json, {'stop_price':  '140.00'})

        # Result shape
        self.assertEqual(result['status'],   'submitted')
        self.assertEqual(result['order_id'], 'order-uuid-123')
        self.assertEqual(result['ticker'],   'AAPL')
        self.assertEqual(result['side'],     'buy')
        self.assertEqual(result['tif'],      'day')
        self.assertEqual(result['order_class'], 'bracket')

    def test_submit_off_hours_uses_opg_simple(self):
        """Off-hours: TIF=opg, order_class=simple (no take-profit / stop-loss)."""
        sess = _mock_session()
        order = {
            'ticker': 'MSFT', 'strategy_id': 'S15', 'direction': 'long',
            'pct_nav': 0.01, 'entry': 400.0, 'stop': 380.0, 't1': 420.0,
        }
        success_stdout = json.dumps({'id': 'opg-uuid', 'status': 'accepted'})
        with patch.object(ae, 'in_market_hours', return_value=False), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, success_stdout, '')) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[argv.index('--time-in-force') + 1], 'opg')
        # simple order_class → no order-class / take-profit / stop-loss
        self.assertNotIn('--order-class',  argv)
        self.assertNotIn('--take-profit', argv)
        self.assertNotIn('--stop-loss',   argv)
        self.assertEqual(result['status'],     'submitted')
        self.assertEqual(result['tif'],        'opg')
        self.assertEqual(result['order_class'],'simple')


class TestBasePriceRetry(unittest.TestCase):
    def test_422_base_price_snaps_and_retries(self):
        sess = _mock_session(quote_status=500)   # pre-flight snap fails
        order = {
            'ticker':       'AAPL', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav':      0.02,
            'entry':        100.00,
            'stop':          95.00,    # original (will be too high)
            't1':           110.00,
        }
        first_err  = json.dumps({
            'status':      422,
            'error':       'stop_loss.stop_price must be <= base_price - 0.01',
            'base_price':  '99.10',
            'code':        42210000,
        })
        second_ok = json.dumps({'id': 'snap-uuid', 'status': 'accepted'})

        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(1, '', first_err),
                       _mock_proc(0, second_ok, ''),
                   ]) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')

        self.assertEqual(mock_run.call_count, 2,
                         'expected initial submit + one snap-retry')
        # The retry's --stop-loss should be 99.10 - max(0.02, 99.10*0.005) = 98.60
        retry_argv = mock_run.call_args_list[1][0][0]
        retry_sl   = json.loads(retry_argv[retry_argv.index('--stop-loss') + 1])
        self.assertEqual(retry_sl['stop_price'], '98.60')
        # Result reflects the snapped stop
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['stop'],   98.60)
        self.assertEqual(result['order_id'], 'snap-uuid')

    def test_422_base_price_short_side_snaps_above(self):
        """Short positions snap stop to base_price + buffer (above, not below)."""
        sess = _mock_session(quote_status=500)
        order = {
            'ticker': 'TSLA', 'strategy_id': 'S5', 'direction': 'short',
            'pct_nav': 0.02, 'entry': 200.0, 'stop': 205.0, 't1': 180.0,
        }
        first_err = json.dumps({
            'status': 422,
            'error':  'stop_loss.stop_price must be >= base_price + 0.01',
            'base_price': '199.50',
            'code': 42210000,
        })
        second_ok = json.dumps({'id': 'short-snap', 'status': 'accepted'})
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(1, '', first_err),
                       _mock_proc(0, second_ok, ''),
                   ]) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        retry_argv = mock_run.call_args_list[1][0][0]
        retry_sl   = json.loads(retry_argv[retry_argv.index('--stop-loss') + 1])
        # 199.50 + max(0.02, 199.50*0.005) = 199.50 + 1.00 = 200.50
        self.assertEqual(retry_sl['stop_price'], '200.50')
        self.assertEqual(result['stop'], 200.50)


class TestDupCoidRecovery(unittest.TestCase):
    def test_422_dup_coid_recovers_via_get_by_client_id(self):
        sess = _mock_session()
        order = {
            'ticker': 'AAPL', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav': 0.02, 'entry': 150.0, 'stop': 140.0, 't1': 160.0,
        }
        first_err = json.dumps({
            'status': 422,
            'error':  'client_order_id already exists for this account',
            'code':   42210001,
        })
        recovery = json.dumps({'id': 'existing-order', 'status': 'accepted'})
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(1, '', first_err),
                       _mock_proc(0, recovery, ''),
                   ]) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        self.assertEqual(mock_run.call_count, 2)
        recovery_argv = mock_run.call_args_list[1][0][0]
        # Second call: alpaca order get-by-client-id --client-order-id <coid>
        self.assertEqual(recovery_argv[1], 'order')
        self.assertEqual(recovery_argv[2], 'get-by-client-id')
        self.assertEqual(recovery_argv[3], '--client-order-id')
        self.assertEqual(result['status'],   'recovered')
        self.assertEqual(result['order_id'], 'existing-order')


class TestPctNavCap(unittest.TestCase):
    def test_pct_nav_capped_to_max_order_pct(self):
        """pct_nav=0.07 should be CAPPED to MAX_ORDER_PCT_NAV=0.05.
        execute_single still calls the CLI but with the clamped sizing."""
        sess = _mock_session()
        order = {
            'ticker':      'AAPL', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav':     0.07,            # above 5% cap
            'entry':       100.00,
            'stop':         90.00,
            't1':          110.00,
        }
        success_stdout = json.dumps({'id': 'cap-uuid', 'status': 'accepted'})
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, success_stdout, '')) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')

        # qty = floor((100_000 * 0.05) / 100.00) = floor(50)  = 50
        self.assertEqual(result['qty'],      50)
        self.assertEqual(result['notional'], 100_000 * 0.05)
        # Verify the CLI was called with --qty 50 (proves cap was applied)
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[argv.index('--qty') + 1], '50')


class TestPreCliSkips(unittest.TestCase):
    def test_missing_levels_skips_pre_cli(self):
        sess = _mock_session()
        order = {
            'ticker': 'AAPL', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav': 0.02,
            # no entry/stop/t1 — should skip
        }
        with patch('execution.alpaca_executor.subprocess.run') as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        self.assertEqual(result['status'],  'SKIP')
        self.assertEqual(result['reason'],  'missing levels')
        mock_run.assert_not_called()

    def test_unsupported_symbol_skips_pre_cli(self):
        sess = _mock_session()
        order = {
            'ticker': '^VIX', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav': 0.02, 'entry': 20.0, 'stop': 18.0, 't1': 22.0,
        }
        with patch('execution.alpaca_executor.subprocess.run') as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        self.assertEqual(result['status'], 'SKIP')
        self.assertIn('unsupported on Alpaca', result['reason'])
        mock_run.assert_not_called()


class TestErrorPath(unittest.TestCase):
    def test_non_recoverable_422_returns_error(self):
        """422 not matching base_price or client_order_id → error result, no retry."""
        sess = _mock_session()
        order = {
            'ticker': 'AAPL', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav': 0.02, 'entry': 150.0, 'stop': 140.0, 't1': 160.0,
        }
        err = json.dumps({
            'status': 422,
            'error':  'qty must be a positive integer',
            'code':   42210099,
        })
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(1, '', err)) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        # No retry should have been attempted
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(result['status'], 'error')
        self.assertEqual(result['http'],   422)
        self.assertIn('qty', result['body'])

    def test_subprocess_timeout_marks_exception(self):
        sess = _mock_session()
        order = {
            'ticker': 'AAPL', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav': 0.02, 'entry': 150.0, 'stop': 140.0, 't1': 160.0,
        }
        import subprocess as sp
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   side_effect=sp.TimeoutExpired(cmd='alpaca', timeout=30)):
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        self.assertEqual(result['status'], 'exception')
        self.assertIn('cli timeout', result['reason'])


class TestInMarketHours(unittest.TestCase):
    def setUp(self):
        # Reset the module-level cache between tests
        ae._market_hours_cache.update({'is_open': None, 'cached_at': 0.0})

    def test_in_market_hours_shells_alpaca_clock(self):
        clock_json = json.dumps({
            'is_open':    True,
            'next_open':  '2026-04-28T09:30:00-04:00',
            'next_close': '2026-04-28T16:00:00-04:00',
            'timestamp':  '2026-04-28T10:30:00-04:00',
        })
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, clock_json, '')) as mock_run:
            self.assertTrue(ae.in_market_hours())
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv, [ae.ALPACA_CLI, 'clock'])

    def test_in_market_hours_handles_closed(self):
        clock_json = json.dumps({
            'is_open':    False,
            'next_open':  '2026-04-29T09:30:00-04:00',
            'next_close': '2026-04-29T16:00:00-04:00',
            'timestamp':  '2026-04-28T18:00:00-04:00',
        })
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, clock_json, '')):
            self.assertFalse(ae.in_market_hours())

    def test_in_market_hours_caches_60s(self):
        """Two consecutive calls within 60s → only one subprocess invocation."""
        clock_json = json.dumps({'is_open': True})
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, clock_json, '')) as mock_run:
            ae.in_market_hours()
            ae.in_market_hours()
            ae.in_market_hours()
        self.assertEqual(mock_run.call_count, 1,
                         'second/third call should hit cache, not subprocess')

    def test_in_market_hours_falls_back_on_cli_error(self):
        """CLI error → static ET-window fallback, no exception."""
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(1, '', 'no profile')):
            # Whatever the static fallback returns is fine; just must not throw
            result = ae.in_market_hours()
            self.assertIsInstance(result, bool)


if __name__ == '__main__':
    unittest.main()
