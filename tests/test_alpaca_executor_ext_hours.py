"""tests/test_alpaca_executor_ext_hours.py

Unit tests for Phase 3 of the 2026-05-19 redeploy-not-liquidate plan:
session-aware execution in alpaca_executor.py.

Covers:
  * _alpaca_session_kind classifier (rth / premarket / afterhours / closed)
  * _pick_limit_price (bid/ask + offset, last-trade fallback, no-data → None)
  * execute_single ext-hours path (limit + --extended-hours, asset filter)
  * execute_single closed path (refusal, zero submit)
  * RTH regression: market + bracket + no --extended-hours

All subprocess.run + requests.Session calls are mocked. No live Alpaca API.
"""
from __future__ import annotations

import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ae  # noqa: E402


def _mock_proc(returncode=0, stdout='', stderr=''):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def _mock_session(quote_bid=100.0, quote_ask=100.10, quote_status=200,
                  latest_trade=None):
    """Reuse the same session-mock shape as test_alpaca_executor_cli."""
    sess = MagicMock()
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


def _reset_caches():
    ae._market_hours_cache.update({'is_open': None, 'cached_at': 0.0})
    ae._session_kind_cache.update({'session': None, 'cached_at': 0.0})
    ae._unsupported_assets.clear()
    ae._asset_cache.clear()


# ── _alpaca_session_kind tests ──────────────────────────────────────────────

class TestSessionKind(unittest.TestCase):
    def setUp(self):
        _reset_caches()

    def test_session_rth(self):
        """is_open=True → trivially 'rth' regardless of UTC clock."""
        clock = json.dumps({
            'is_open':    True,
            'next_open':  '2026-05-19T09:30:00-04:00',
            'next_close': '2026-05-19T16:00:00-04:00',
            'timestamp':  '2026-05-19T11:00:00-04:00',
        })
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, clock, '')):
            self.assertEqual(ae._alpaca_session_kind(), 'rth')

    def test_session_premarket(self):
        """is_open=False + UTC 13:00 (= 09:00 ET) + next_open later today → premarket."""
        clock = json.dumps({
            'is_open':    False,
            # next_open is later today (09:30 ET, same calendar date)
            'next_open':  '2026-05-19T09:30:00-04:00',
            'next_close': '2026-05-19T16:00:00-04:00',
            'timestamp':  '2026-05-19T09:00:00-04:00',
        })
        # Mock current time to 13:00 UTC = 09:00 ET on 2026-05-19
        fixed_et = datetime(2026, 5, 19, 9, 0, 0)  # 09:00 ET
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, clock, '')), \
             patch('execution.alpaca_executor.datetime') as mock_dt:
            mock_dt.now.return_value = fixed_et
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(ae._alpaca_session_kind(), 'premarket')

    def test_session_afterhours(self):
        """is_open=False + UTC 22:00 (= 18:00 ET) + next_open tomorrow → afterhours."""
        clock = json.dumps({
            'is_open':    False,
            'next_open':  '2026-05-20T09:30:00-04:00',
            'next_close': '2026-05-20T16:00:00-04:00',
            'timestamp':  '2026-05-19T18:00:00-04:00',
        })
        fixed_et = datetime(2026, 5, 19, 18, 0, 0)  # 18:00 ET = 22:00 UTC
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, clock, '')), \
             patch('execution.alpaca_executor.datetime') as mock_dt:
            mock_dt.now.return_value = fixed_et
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(ae._alpaca_session_kind(), 'afterhours')

    def test_session_closed_overnight(self):
        """is_open=False + 04:00 UTC (= midnight ET) → 'closed' (pre-04:00 ET cutoff)."""
        clock = json.dumps({
            'is_open':    False,
            'next_open':  '2026-05-19T09:30:00-04:00',
            'next_close': '2026-05-19T16:00:00-04:00',
            'timestamp':  '2026-05-19T00:00:00-04:00',
        })
        fixed_et = datetime(2026, 5, 19, 0, 0, 0)  # midnight ET
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, clock, '')), \
             patch('execution.alpaca_executor.datetime') as mock_dt:
            mock_dt.now.return_value = fixed_et
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(ae._alpaca_session_kind(), 'closed')


# ── _pick_limit_price tests ─────────────────────────────────────────────────

class TestPickLimitPrice(unittest.TestCase):
    def test_pick_limit_price_buy_uses_ask_plus_offset(self):
        """side=buy uses ask × (1 + offset). 100.5 × 1.005 = 101.0025 → 101.00."""
        quote_resp = json.dumps({
            'symbol': 'AAPL',
            'quote': {'bp': 100.0, 'ap': 100.5},
        })
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, quote_resp, '')):
            px = ae._pick_limit_price('AAPL', 'buy')
        self.assertEqual(px, 101.00)

    def test_pick_limit_price_sell_uses_bid_minus_offset(self):
        """side=sell uses bid × (1 - offset). 100.0 × 0.995 = 99.50."""
        quote_resp = json.dumps({
            'symbol': 'AAPL',
            'quote': {'bp': 100.0, 'ap': 100.5},
        })
        with patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, quote_resp, '')):
            px = ae._pick_limit_price('AAPL', 'sell')
        self.assertEqual(px, 99.50)

    def test_pick_limit_price_fallback_to_last_trade(self):
        """bid/ask missing or zero → fall back to last trade ± offset."""
        # Quote returns 0/0; trade returns p=100.
        quote_resp = json.dumps({'symbol': 'AAPL', 'quote': {'bp': 0.0, 'ap': 0.0}})
        trade_resp = json.dumps({'symbol': 'AAPL', 'trade': {'p': 100.0}})
        with patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(0, quote_resp, ''),
                       _mock_proc(0, trade_resp, ''),
                   ]):
            px_buy = ae._pick_limit_price('AAPL', 'buy')
        # 100 × 1.005 = 100.50
        self.assertEqual(px_buy, 100.50)

        with patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(0, quote_resp, ''),
                       _mock_proc(0, trade_resp, ''),
                   ]):
            px_sell = ae._pick_limit_price('AAPL', 'sell')
        # 100 × 0.995 = 99.50
        self.assertEqual(px_sell, 99.50)

    def test_pick_limit_price_returns_none_when_no_data(self):
        """Both CLI calls return empty/error → None (caller must skip submit)."""
        # Both quote and trade return empty/no data.
        empty_quote = json.dumps({'symbol': 'AAPL', 'quote': {'bp': 0.0, 'ap': 0.0}})
        empty_trade = json.dumps({'symbol': 'AAPL', 'trade': {}})
        with patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(0, empty_quote, ''),
                       _mock_proc(0, empty_trade, ''),
                   ]):
            px = ae._pick_limit_price('AAPL', 'buy')
        self.assertIsNone(px)

        # CLI errors: both calls non-zero rc → still None.
        err = json.dumps({'status': 422, 'error': 'subscription required', 'code': 999})
        with patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(1, '', err),
                       _mock_proc(1, '', err),
                   ]):
            px = ae._pick_limit_price('AAPL', 'buy')
        self.assertIsNone(px)


# ── execute_single ext-hours / closed / RTH path tests ──────────────────────

class TestExecuteSingleSessions(unittest.TestCase):
    def setUp(self):
        _reset_caches()

    def _order(self, ticker='AAPL', direction='long'):
        return {
            'ticker': ticker, 'strategy_id': 'S_TEST', 'direction': direction,
            'pct_nav': 0.02, 'entry': 100.0, 'stop': 95.0, 't1': 110.0,
        }

    def test_executor_skips_options_in_ext_hours(self):
        """asset class=us_option in afterhours → SKIP, no submit, skip logged."""
        sess = _mock_session()
        asset_meta = json.dumps({
            'symbol': 'AAPL250119C00200000',
            'class': 'us_option',
            'tradable': True,
        })
        with patch.object(ae, '_alpaca_session_kind', return_value='afterhours'), \
             patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[_mock_proc(0, asset_meta, '')]) as mock_run:
            result = ae.execute_single(
                sess, equity=100_000.0,
                order=self._order(ticker='AAPL250119C00200000'),
                run_date='2026-05-19',
            )
        # Only the asset get call should have fired — no order submit.
        self.assertEqual(mock_run.call_count, 1)
        argv = mock_run.call_args_list[0][0][0]
        self.assertEqual(argv[1], 'asset')
        self.assertEqual(argv[2], 'get')
        self.assertEqual(result['status'], 'SKIP')
        self.assertIn('us_option', result['reason'])

    def test_executor_submits_limit_in_premarket(self):
        """premarket session + us_equity asset + good quote → limit + --extended-hours submit."""
        sess = _mock_session()
        asset_meta = json.dumps({
            'symbol': 'MSFT', 'class': 'us_equity', 'tradable': True,
            'shortable': True, 'easy_to_borrow': True,
        })
        quote_resp = json.dumps({'symbol': 'MSFT', 'quote': {'bp': 399.95, 'ap': 400.05}})
        submit_resp = json.dumps({'id': 'pre-uuid', 'status': 'accepted'})
        with patch.object(ae, '_alpaca_session_kind', return_value='premarket'), \
             patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(0, asset_meta, ''),
                       _mock_proc(0, quote_resp, ''),
                       _mock_proc(0, submit_resp, ''),
                   ]) as mock_run:
            result = ae.execute_single(
                sess, equity=100_000.0,
                order={'ticker': 'MSFT', 'strategy_id': 'S5', 'direction': 'long',
                       'pct_nav': 0.01, 'entry': 400.0, 'stop': 380.0, 't1': 420.0},
                run_date='2026-05-19',
            )
        self.assertEqual(mock_run.call_count, 3)
        submit_argv = mock_run.call_args_list[-1][0][0]
        self.assertEqual(submit_argv[1:3], ['order', 'submit'])
        self.assertEqual(submit_argv[submit_argv.index('--type') + 1], 'limit')
        self.assertEqual(submit_argv[submit_argv.index('--time-in-force') + 1], 'day')
        self.assertIn('--extended-hours', submit_argv)
        self.assertIn('--limit-price', submit_argv)
        # ask × 1.005 = 400.05 × 1.005 = 402.05025 → 402.05
        self.assertEqual(submit_argv[submit_argv.index('--limit-price') + 1], '402.05')
        # No bracket flags in ext-hours.
        self.assertNotIn('--order-class', submit_argv)
        self.assertNotIn('--take-profit', submit_argv)
        self.assertNotIn('--stop-loss', submit_argv)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['order_type'], 'limit')
        self.assertTrue(result['extended_hours'])

    def test_executor_skips_closed_session(self):
        """session=closed → SKIP, no subprocess invocation."""
        sess = _mock_session()
        with patch.object(ae, '_alpaca_session_kind', return_value='closed'), \
             patch('execution.alpaca_executor.subprocess.run') as mock_run:
            result = ae.execute_single(
                sess, equity=100_000.0, order=self._order(),
                run_date='2026-05-19',
            )
        mock_run.assert_not_called()
        self.assertEqual(result['status'], 'SKIP')
        self.assertIn('closed', result['reason'])

    def test_executor_rth_unchanged(self):
        """RTH regression: submit has type=market, extended_hours=False, bracket.

        The order shape must be bit-for-bit identical to the pre-Phase-3
        RTH submission: --type market, --time-in-force day,
        --order-class bracket, with take-profit + stop-loss JSON sub-objects.
        No --extended-hours flag. No --limit-price flag.
        """
        sess = _mock_session(quote_bid=99.95, quote_ask=100.05)
        submit_resp = json.dumps({'id': 'rth-uuid', 'status': 'accepted'})
        with patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, submit_resp, '')) as mock_run:
            result = ae.execute_single(
                sess, equity=100_000.0, order=self._order(),
                run_date='2026-05-19',
            )
        # Only the order submit call — no asset get, no latest-quote shell-outs.
        self.assertEqual(mock_run.call_count, 1)
        argv = mock_run.call_args_list[-1][0][0]
        self.assertEqual(argv[argv.index('--type') + 1], 'market')
        self.assertEqual(argv[argv.index('--time-in-force') + 1], 'day')
        self.assertEqual(argv[argv.index('--order-class') + 1], 'bracket')
        self.assertNotIn('--extended-hours', argv)
        self.assertNotIn('--limit-price', argv)
        self.assertEqual(result['status'], 'submitted')
        self.assertEqual(result['order_class'], 'bracket')
        self.assertFalse(result.get('extended_hours'))
        self.assertEqual(result.get('order_type'), 'market')

    def test_bracket_only_rth(self):
        """RTH submit includes bracket flags; premarket submit does not."""
        # RTH leg.
        sess = _mock_session(quote_bid=99.95, quote_ask=100.05)
        submit_resp = json.dumps({'id': 'rth-id', 'status': 'accepted'})
        with patch.object(ae, '_alpaca_session_kind', return_value='rth'), \
             patch('execution.alpaca_executor.subprocess.run',
                   return_value=_mock_proc(0, submit_resp, '')) as mock_run:
            ae.execute_single(sess, equity=100_000.0, order=self._order(),
                              run_date='2026-05-19')
        argv = mock_run.call_args_list[-1][0][0]
        self.assertIn('--order-class', argv)
        self.assertIn('--take-profit', argv)
        self.assertIn('--stop-loss', argv)

        _reset_caches()

        # Premarket leg.
        asset_meta = json.dumps({'symbol': 'AAPL', 'class': 'us_equity', 'tradable': True})
        quote_resp = json.dumps({'symbol': 'AAPL', 'quote': {'bp': 99.95, 'ap': 100.05}})
        submit_resp_pre = json.dumps({'id': 'pre-id', 'status': 'accepted'})
        with patch.object(ae, '_alpaca_session_kind', return_value='premarket'), \
             patch('execution.alpaca_executor.subprocess.run',
                   side_effect=[
                       _mock_proc(0, asset_meta, ''),
                       _mock_proc(0, quote_resp, ''),
                       _mock_proc(0, submit_resp_pre, ''),
                   ]) as mock_run:
            ae.execute_single(sess, equity=100_000.0, order=self._order(),
                              run_date='2026-05-19')
        argv = mock_run.call_args_list[-1][0][0]
        self.assertNotIn('--order-class', argv)
        self.assertNotIn('--take-profit', argv)
        self.assertNotIn('--stop-loss', argv)


if __name__ == '__main__':
    unittest.main()
