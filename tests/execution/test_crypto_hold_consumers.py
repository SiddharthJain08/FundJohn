"""Hold-flat consumers: shared reader, crypto redeploy sizer, crypto executor buy path."""
from __future__ import annotations
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import alpaca_executor as ae  # noqa: E402
from execution import crypto_hold as ch  # noqa: E402
from execution import crypto_redeploy_sizer as crs  # noqa: E402
from tests.execution.test_crypto_execution_lane import _fake_cli  # noqa: E402

EMPTY = {'sleeve': None, 'tickers': {}}


@pytest.fixture(autouse=True)
def _no_hold_redis(monkeypatch):
    monkeypatch.setenv('OPENCLAW_CRYPTO_HOLD_DISABLE', '1')


class FakeRedis:
    def __init__(self, data):
        self.data = data

    def get(self, k):
        return self.data.get(k)

    def scan_iter(self, match=None):
        pre = match.rstrip('*')
        return iter([k for k in self.data if k.startswith(pre)])


class BrokenRedis:
    def get(self, k):
        raise ConnectionError('down')


class FakeConn:
    def __init__(self, value):
        self.value = value

    def cursor(self):
        outer = self

        class C:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def execute(s, q, p): pass
            def fetchone(s): return (outer.value,)
        return C()


def test_hold_state_prefers_redis_then_pipeline_config(monkeypatch):
    monkeypatch.delenv('OPENCLAW_CRYPTO_HOLD_DISABLE')
    sleeve = {'since': 'x', 'reason': 'r', 'scope': 'sleeve'}
    r = FakeRedis({'crypto_breaker:hold_flat': json.dumps(sleeve),
                   'crypto_breaker:hold_ticker:ETH-USD': json.dumps({'since': 'y', 'ticker': 'ETH-USD'})})
    mirror = FakeConn(json.dumps({'since': 'mirror'}))
    st = ch.hold_state(conn=mirror, redis=r)
    assert st['sleeve'] == sleeve and list(st['tickers']) == ['ETH-USD']
    assert ch.is_held('ETH-USD', st) and ch.is_held('BTC-USD', st)  # sleeve holds all
    # Redis down -> mirror (sleeve only)
    st = ch.hold_state(conn=mirror, redis=BrokenRedis())
    assert st == {'sleeve': {'since': 'mirror'}, 'tickers': {}}
    # mirror '' -> empty; nothing available -> empty
    assert ch.hold_state(conn=FakeConn(''), redis=BrokenRedis()) == EMPTY
    # {sleeve, tickers} mirror shape: both halves read; an empty-state dict is NOT a hold
    tk = {'since': 'y', 'ticker': 'ETH-USD'}
    st = ch.hold_state(conn=FakeConn(json.dumps({'sleeve': None, 'tickers': {'ETH-USD': tk}})),
                       redis=BrokenRedis())
    assert st == {'sleeve': None, 'tickers': {'ETH-USD': tk}}
    assert ch.is_held('ETH-USD', st) and not ch.is_held('BTC-USD', st)
    st = ch.hold_state(conn=FakeConn({'sleeve': None, 'tickers': {}}), redis=BrokenRedis())
    assert st == EMPTY and not ch.is_held('BTC-USD', st)
    st = ch.hold_state(conn=FakeConn(json.dumps({'sleeve': sleeve, 'tickers': {}})), redis=BrokenRedis())
    assert st == {'sleeve': sleeve, 'tickers': {}}
    assert not ch.is_held('BTC-USD', EMPTY)


def _size(hold, broker):
    weights = [{'strategy_id': 'S_c', 'daily_weight': 1.0}]
    signals = [{'strategy_id': 'S_c', 'ticker': 'BTC-USD', 'direction': 'long'},
               {'strategy_id': 'S_c', 'ticker': 'ETH-USD', 'direction': 'long'}]
    return crs.size_crypto_positions(
        {'equity': 100_000.0}, {'state': 'LOW_VOL'},
        broker_loader=lambda: broker, weights_loader=lambda rg: weights,
        signals_loader=lambda rg, w: signals, crypto_strategy_ids={'S_c'},
        all_live_weight_sum=1.0, hold_state=hold)


def test_sizer_zeroes_targets_under_sleeve_hold():
    assert any(o['direction'] == 'long' for o in _size(EMPTY, {}))
    assert _size({'sleeve': {'since': 'x'}, 'tickers': {}}, {}) == []
    orders = _size({'sleeve': {'since': 'x'}, 'tickers': {}}, {'BTC/USD': 5000.0})
    assert len(orders) == 1 and orders[0]['close_only'] and orders[0]['direction'] == 'short'
    assert orders[0]['strategy_id'] == '__crypto_breaker_hold__'


def test_sizer_zeroes_only_held_ticker():
    orders = _size({'sleeve': None, 'tickers': {'ETH-USD': {}}}, {'ETH/USD': 4000.0})
    by = {o['ticker']: o for o in orders}
    assert by['BTC-USD']['direction'] == 'long' and not by['BTC-USD']['close_only']
    assert by['ETH-USD']['close_only'] and by['ETH-USD']['strategy_id'] == '__crypto_breaker_hold__'


def test_executor_refuses_crypto_buy_under_hold_but_allows_close():
    held = {'sleeve': {'since': 'x'}, 'tickers': {}}
    buy = {'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.01}
    close = {'ticker': 'BTC-USD', 'close_only': True, 'notional_usd': 25000.0, 'current_usd': 25000.0}
    responses = {'order list': (True, [], None),
                 'position close': (True, {'id': 'close-1'}, None)}
    fake = _fake_cli(responses)
    calls = []

    def _rec(args, timeout=30):
        calls.append(args)
        return fake(args, timeout)
    with patch.object(ch, 'hold_state', return_value=held), \
            patch.object(ae, '_run_alpaca_cli', side_effect=_rec):
        res = ae._route_crypto_order(buy, 100000.0, 'C1')
        assert calls == []
        assert res['status'] == 'refused' and res['reason'] == 'hold_flat'
        assert res['ticker'] == 'BTC/USD' and res['client_order_id'] == 'C1'
        res = ae._route_crypto_order(close, 100000.0, 'C2')
        assert res['status'] == 'submitted'
    # read error -> fail-open (allowed through to normal path)
    with patch.object(ch, 'hold_state', side_effect=RuntimeError('boom')):
        res = ae._route_crypto_order({'ticker': 'BTC-USD', 'direction': 'long', 'pct_nav': 0.0},
                                     100000.0, 'C3')
    assert res['status'] == 'SKIP'


def test_disable_flag_ignored_outside_pytest(monkeypatch):
    sleeve = {'since': 'x', 'reason': 'r', 'scope': 'sleeve'}
    r = FakeRedis({'crypto_breaker:hold_flat': json.dumps(sleeve)})
    monkeypatch.setenv('OPENCLAW_CRYPTO_HOLD_DISABLE', '1')
    monkeypatch.delenv('PYTEST_CURRENT_TEST')
    assert ch.hold_state(redis=r)['sleeve'] == sleeve
