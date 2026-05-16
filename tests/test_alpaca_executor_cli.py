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


def _mock_session(quote_bid=100.0, quote_ask=100.10, quote_status=200,
                  latest_trade=None):
    """Build a mock requests.Session for the pre-flight snapshot fetch.

    Default: 200 with synthetic latestTrade (mid of bid/ask if not given)
    so the recompute path is exercised. Set quote_status=500 to force the
    snapshot-fetch failure branch (order submitted unsnapped).
    """
    sess = MagicMock()
    sess._base = 'https://paper-api.alpaca.markets'
    snap_resp = MagicMock()
    snap_resp.status_code = quote_status
    mid = (quote_bid + quote_ask) / 2.0 if quote_bid and quote_ask else (quote_bid or quote_ask)
    p = latest_trade if latest_trade is not None else mid
    snap_resp.json.return_value = {
        'latestTrade': {'p': p},
        'latestQuote': {'bp': quote_bid, 'ap': quote_ask},
    }
    sess.get.return_value = snap_resp
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
                         'expected initial submit + one recompute-retry')
        # 2026-05-14: retry now uses ratio-preserving recompute, not absolute snap.
        # Signal stop_pct = (100-95)/100 = 5%; new stop = base × (1 - 0.05) = 94.145 → 94.14
        # Signal target_pct = (110-100)/100 = 10%; new target = base × (1 + 0.10) = 109.01
        retry_argv = mock_run.call_args_list[1][0][0]
        retry_sl   = json.loads(retry_argv[retry_argv.index('--stop-loss') + 1])
        retry_tp   = json.loads(retry_argv[retry_argv.index('--take-profit') + 1])
        self.assertEqual(retry_sl['stop_price'],     '94.14')
        self.assertEqual(retry_tp['limit_price'],   '109.01')
        # Result reflects the recomputed levels
        self.assertEqual(result['status'], 'submitted')
        self.assertAlmostEqual(result['stop'],   94.14, places=2)
        self.assertAlmostEqual(result['target'], 109.01, places=2)
        self.assertEqual(result['order_id'], 'snap-uuid')

    def test_short_uses_simple_order_class_no_bracket_flags(self):
        """Fix #3 (2026-05-08): shorts (side=sell to open) submit as simple
        market orders, NOT brackets. Alpaca paper rejects bracket-on-short
        with 'bracket orders must be entry orders' when prior fills exist
        on the symbol. The bracket flags must not appear in the argv."""
        sess = _mock_session(quote_bid=199.95, quote_ask=200.05)
        order = {
            'ticker': 'TSLA', 'strategy_id': 'S5', 'direction': 'short',
            'pct_nav': 0.02, 'entry': 200.0, 'stop': 205.0, 't1': 180.0,
        }
        success_stdout = json.dumps({'id': 'short-simple', 'status': 'accepted'})
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, success_stdout, '')) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[argv.index('--side') + 1], 'sell')
        # Bracket flags MUST NOT appear — short uses simple order class.
        self.assertNotIn('--order-class', argv)
        self.assertNotIn('--take-profit', argv)
        self.assertNotIn('--stop-loss',  argv)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['order_class'], 'simple')
        self.assertEqual(result['side'], 'sell')


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


class TestPctNavPassthrough(unittest.TestCase):
    def test_pct_nav_not_clamped(self):
        """2026-05-14: MAX_ORDER_PCT_NAV cap was dropped per operator decision.
        pct_nav now flows through unchanged; high-conviction orders express
        the sizer's full allocation. Quoted entry=100.05 (mid of 100/100.10
        from _mock_session), so qty = floor((100_000 × 0.07) / 100.05) = 69."""
        sess = _mock_session()
        order = {
            'ticker':      'AAPL', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav':     0.07,
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

        # No cap: qty derived from raw pct_nav=0.07. Pre-flight recompute
        # has already re-anchored entry to base (100.05); qty computed
        # before recompute uses signal-time entry (100.00) → 70 shares.
        self.assertEqual(result['qty'],      70)
        self.assertAlmostEqual(result['notional'], 100_000 * 0.07, places=2)


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


class TestNormalizeSymbol(unittest.TestCase):
    def test_three_letter_exchange_suffix_blocked(self):
        """DX-Y.NYB, GC.CME, etc. → None (futures/index quotes)."""
        self.assertIsNone(ae._normalize_alpaca_symbol('DX-Y.NYB'))
        self.assertIsNone(ae._normalize_alpaca_symbol('GC.CME'))
        self.assertIsNone(ae._normalize_alpaca_symbol('NYB.COMEX'))

    def test_class_share_suffix_kept(self):
        """BRK.A, BRK.B, BF.A — single-letter dot suffixes are class shares."""
        self.assertEqual(ae._normalize_alpaca_symbol('BRK.A'), 'BRK.A')
        self.assertEqual(ae._normalize_alpaca_symbol('BRK.B'), 'BRK.B')
        self.assertEqual(ae._normalize_alpaca_symbol('BF.A'),  'BF.A')


class TestUnsupportedAssetCache(unittest.TestCase):
    def setUp(self):
        ae._unsupported_assets.clear()

    def test_first_rejection_caches_ticker(self):
        """A 4xx with an asset-class error message → ticker added to cache."""
        sess = _mock_session(quote_bid=20.0, quote_ask=20.10)
        order = {
            'ticker': 'HOLX', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav': 0.02, 'entry': 20.0, 'stop': 18.0, 't1': 22.0,
        }
        err = json.dumps({
            'status': 422,
            'error':  'asset HOLX is not tradable on this account',
            'code':   42210010,
        })
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(1, '', err)):
            ae.execute_single(sess, equity=100_000.0, order=order, run_date='2026-04-28')
        self.assertIn('HOLX', ae._unsupported_assets)

    def test_second_attempt_skips_pre_cli(self):
        """Once cached, a subsequent attempt SKIPs without shelling out."""
        ae._unsupported_assets.add('HOLX')
        sess = _mock_session()
        order = {
            'ticker': 'HOLX', 'strategy_id': 'S99', 'direction': 'long',
            'pct_nav': 0.02, 'entry': 20.0, 'stop': 18.0, 't1': 22.0,
        }
        with patch('execution.alpaca_executor.subprocess.run') as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        self.assertEqual(result['status'], 'SKIP')
        self.assertIn('cached unsupported', result['reason'])
        mock_run.assert_not_called()

    def test_unrelated_4xx_does_not_cache(self):
        """A bracket-validity 422 (stop_loss bound) is NOT an asset error;
        the ticker should NOT be cached as unsupported."""
        sess = _mock_session(quote_status=500)  # force pre-flight whiff
        order = {
            'ticker': 'AAPL', 'strategy_id': 'S5', 'direction': 'long',
            'pct_nav': 0.02, 'entry': 100.0, 'stop': 95.0, 't1': 110.0,
        }
        err = json.dumps({
            'status': 422, 'code': 42210099,
            'error': 'qty must be a positive integer',
        })
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(1, '', err)):
            ae.execute_single(sess, equity=100_000.0, order=order, run_date='2026-04-28')
        self.assertNotIn('AAPL', ae._unsupported_assets)


class TestPreFlightTargetSnap(unittest.TestCase):
    def test_target_too_close_to_base_snaps_up_for_long(self):
        """Long: target must be >= base + 0.01. If TradeJohn's t1 is too
        tight (close to base), the pre-flight snaps it up."""
        # quote mid = 100.05 (bid 100.00, ask 100.10). Original target=100.02 is
        # 0.02% above entry — target_pct floors to MIN_BRACKET_GAP_PCT=1%,
        # so new_target = base × 1.01 = 100.05 × 1.01 = 101.0505 → 101.05.
        sess = _mock_session(quote_bid=100.00, quote_ask=100.10)
        order = {
            'ticker': 'AAPL', 'strategy_id': 'S15', 'direction': 'long',
            'pct_nav': 0.02, 'entry': 100.00, 'stop': 95.00,
            't1': 100.02,    # 0.02% target — gets floored to 1% gap from base
        }
        success_stdout = json.dumps({'id': 'tgt-uuid', 'status': 'accepted'})
        with patch.object(ae, 'in_market_hours', return_value=True), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, success_stdout, '')) as mock_run:
            result = ae.execute_single(sess, equity=100_000.0,
                                       order=order, run_date='2026-04-28')
        argv = mock_run.call_args[0][0]
        tp_json = json.loads(argv[argv.index('--take-profit') + 1])
        # Re-anchored to base 100.05 with 1% MIN floor → target = 101.05
        self.assertEqual(tp_json['limit_price'], '101.05')
        self.assertEqual(result['target'], 101.05)


if __name__ == '__main__':
    unittest.main()
