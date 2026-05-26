"""tests/test_crypto_execution_lane.py — SP-3.1 Phase A crypto execution lane."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
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
