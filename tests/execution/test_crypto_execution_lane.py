"""tests/test_crypto_execution_lane.py — SP-3.1 Phase A crypto execution lane."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_executor as ae  # noqa: E402


def test_is_crypto_ticker():
    assert ae._is_crypto_ticker('BTC-USD') is True
    assert ae._is_crypto_ticker('eth-usd') is True
    assert ae._is_crypto_ticker('AAPL') is False
    assert ae._is_crypto_ticker('BRK-B') is False
    assert ae._is_crypto_ticker('') is False
    assert ae._is_crypto_ticker(None) is False


def test_alpaca_crypto_symbol():
    assert ae._alpaca_crypto_symbol('BTC-USD') == 'BTC/USD'
    assert ae._alpaca_crypto_symbol('eth-usd') == 'ETH/USD'


def _fake_cli(responses):
    """Return a fake _run_alpaca_cli that pops (ok, payload, err) by matching
    the first positional arg list against substrings in `responses` keys."""
    def _call(args, timeout=30):
        joined = ' '.join(args)
        for needle, resp in responses.items():
            if needle in joined:
                return resp
        raise AssertionError(f'unexpected CLI call: {joined}')
    return _call


def test_route_returns_none_for_equity():
    # Equity ticker must fall through (byte-identical guard).
    assert ae._route_crypto_order({'ticker': 'AAPL', 'pct_nav': 0.1}, 100000.0, 'COID1') is None


def test_route_crypto_open_buy():
    responses = {
        'data crypto latest-trades': (True, {'trades': {'BTC/USD': {'p': 50000.0}}}, None),
        'order submit': (True, {'id': 'ord-1', 'qty': '0.02'}, None),
    }
    order = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.01}
    with patch.object(ae, '_run_alpaca_cli', side_effect=_fake_cli(responses)):
        res = ae._route_crypto_order(order, 100000.0, 'COID2')
    assert res['status'] == 'submitted'
    assert res['ticker'] == 'BTC/USD'
    assert res['order_id'] == 'ord-1'
    # notional = 100000 * 0.01 = 1000; qty = 1000/50000 = 0.02
    assert abs(res['qty'] - 0.02) < 1e-9
    assert res['entry'] == 50000.0
    assert res['order_class'] == 'simple'


def test_route_crypto_open_skips_when_no_price():
    responses = {'data crypto latest-trades': (True, {'trades': {}}, None)}
    order = {'ticker': 'ETH-USD', 'direction': 'long', 'pct_nav': 0.01}
    with patch.object(ae, '_run_alpaca_cli', side_effect=_fake_cli(responses)):
        res = ae._route_crypto_order(order, 100000.0, 'COID3')
    assert res['status'] == 'SKIP'
    assert 'price' in res['reason']


def test_route_crypto_open_skips_on_zero_notional():
    order = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.0}
    res = ae._route_crypto_order(order, 100000.0, 'COID4')
    assert res['status'] == 'SKIP'
    assert 'notional' in res['reason']


def test_route_crypto_full_close():
    responses = {'order list': (True, [], None),
                 'position close': (True, {'id': 'close-1', 'qty': '0.5', 'notional': '25000'}, None)}
    order = {'ticker': 'BTC-USD', 'close_only': True,
             'notional_usd': 25000.0, 'current_usd': 25000.0}
    with patch.object(ae, '_run_alpaca_cli', side_effect=_fake_cli(responses)):
        res = ae._route_crypto_order(order, 100000.0, 'COID5')
    assert res['status'] == 'submitted'
    assert res['order_id'] == 'close-1'
    assert res['ticker'] == 'BTC/USD'


def test_route_crypto_partial_reduce_sends_percentage():
    captured = {}
    def _cap(args, timeout=30):
        captured['args'] = args
        return (True, {'id': 'close-2', 'qty': '0.25', 'notional': '12500'}, None)
    # reduce to half: notional_usd 12500 of current_usd 25000 -> ~50%
    order = {'ticker': 'BTC-USD', 'close_only': True,
             'notional_usd': 12500.0, 'current_usd': 25000.0}
    with patch.object(ae, '_run_alpaca_cli', side_effect=_cap):
        res = ae._route_crypto_order(order, 100000.0, 'COID6')
    assert res['status'] == 'submitted'
    assert '--percentage' in captured['args']
    pct = captured['args'][captured['args'].index('--percentage') + 1]
    assert abs(float(pct) - 50.0) < 0.5


def test_route_crypto_close_rejected():
    responses = {'order list': (True, [], None),
                 'position close': (False, None, {'exit_code': 1, 'status': 404,
                                                  'error': 'position does not exist'})}
    order = {'ticker': 'ETH-USD', 'close_only': True,
             'notional_usd': 1000.0, 'current_usd': 1000.0}
    with patch.object(ae, '_run_alpaca_cli', side_effect=_fake_cli(responses)):
        res = ae._route_crypto_order(order, 100000.0, 'COID7')
    assert res['status'] == 'rejected'
    assert res['http'] == 404


def test_route_crypto_full_close_sends_no_percentage():
    captured = {}
    def _cap(args, timeout=30):
        captured['args'] = args
        return (True, {'id': 'close-3', 'qty': '0.5', 'notional': '25000'}, None)
    order = {'ticker': 'BTC-USD', 'close_only': True,
             'notional_usd': 25000.0, 'current_usd': 25000.0}
    with patch.object(ae, '_run_alpaca_cli', side_effect=_cap):
        res = ae._route_crypto_order(order, 100000.0, 'COID8')
    assert res['status'] == 'submitted'
    assert '--percentage' not in captured['args']


def test_route_crypto_close_notional_null_fallback():
    # Alpaca returns notional=null on position-close; the result must fall
    # back to the order's own notional_usd (load-bearing per Task 0 snapshot).
    responses = {'order list': (True, [], None),
                 'position close': (True, {'id': 'close-4', 'qty': '0.3', 'notional': None}, None)}
    order = {'ticker': 'BTC-USD', 'close_only': True,
             'notional_usd': 25000.0, 'current_usd': 25000.0}
    with patch.object(ae, '_run_alpaca_cli', side_effect=_fake_cli(responses)):
        res = ae._route_crypto_order(order, 100000.0, 'COID9')
    assert res['status'] == 'submitted'
    assert res['notional'] == 25000.0


def test_execute_single_routes_crypto():
    responses = {
        'data crypto latest-trades': (True, {'trades': {'BTC/USD': {'p': 40000.0}}}, None),
        'order submit': (True, {'id': 'es-1', 'qty': '0.025'}, None),
    }
    order = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.01,
             'strategy_id': 'S_test_crypto'}
    with patch.dict('os.environ', {'OPENCLAW_INSTRUMENT_CLASS_ROUTING': '1'}), \
         patch.object(ae, '_run_alpaca_cli', side_effect=_fake_cli(responses)):
        res = ae.execute_single(sess=None, equity=100000.0, order=order, run_date='2026-05-26')
    assert res['status'] == 'submitted'
    assert res['ticker'] == 'BTC/USD'
    assert res['order_id'] == 'es-1'
    assert res['client_order_id']  # coid populated


def test_execute_single_crypto_falls_through_when_gate_off():
    # Gate OFF: the crypto intercept must NOT fire — a -USD order falls through
    # to the equity path, which SKIPs it as unsupported. Proves the SP-3
    # gate-OFF equity-byte-identical invariant holds at the executor too.
    order = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.01,
             'strategy_id': 'S_test_crypto'}
    with patch.dict('os.environ', {'OPENCLAW_INSTRUMENT_CLASS_ROUTING': '0'}):
        res = ae.execute_single(sess=None, equity=100000.0, order=order, run_date='2026-05-26')
    assert res['status'] == 'SKIP'
    assert 'unsupported on Alpaca paper' in res['reason']


def test_execute_single_equity_unaffected_missing_levels():
    # Equity order with no entry/stop/target still hits the existing
    # 'missing levels' SKIP — proves the crypto intercept did not alter it.
    order = {'ticker': 'AAPL', 'direction': 'long', 'pct_nav': 0.01,
             'strategy_id': 'S_test_equity'}
    res = ae.execute_single(sess=None, equity=100000.0, order=order, run_date='2026-05-26')
    assert res['status'] == 'SKIP'
    assert res['reason'] == 'missing levels'
