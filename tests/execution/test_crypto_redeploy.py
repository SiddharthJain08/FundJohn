"""tests/test_crypto_redeploy.py — SP-3.1 Phase C crypto redeploy (equity-untouched)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from execution import crypto_redeploy_sizer as crs  # noqa: E402


def test_equity_positions_never_touched():
    broker = {'SPY': 150_000.0, 'AAPL': 80_000.0, 'BTC-USD': 20_000.0}
    weights = [{'strategy_id': 'S_btc', 'daily_weight': 1.0, 'effective_sharpe': 2.0, 'cadence_days': 5}]
    signals = [{'strategy_id': 'S_btc', 'ticker': 'BTC-USD', 'direction': 'long'}]
    orders = crs.size_crypto_positions(
        {'equity': 100_000.0}, {'state': 'LOW_VOL'},
        broker_loader=lambda: broker,
        weights_loader=lambda regime: weights,
        signals_loader=lambda regime, wbs: signals,
        crypto_strategy_ids={'S_btc'},
        all_live_weight_sum=2.0)
    tickers = {o['ticker'] for o in orders}
    assert 'SPY' not in tickers and 'AAPL' not in tickers
    assert all(o['ticker'].upper().endswith('-USD') for o in orders)


def test_no_orders_on_empty_crypto_signals():
    orders = crs.size_crypto_positions(
        {'equity': 100_000.0}, {'state': 'LOW_VOL'},
        broker_loader=lambda: {'BTC-USD': 20_000.0},
        weights_loader=lambda regime: [],
        signals_loader=lambda regime, wbs: [],
        crypto_strategy_ids=set(), all_live_weight_sum=1.0)
    assert orders == []


def test_crypto_orphan_close_only_crypto():
    broker = {'SPY': 150_000.0, 'BTC-USD': 20_000.0}
    weights = [{'strategy_id': 'S_eth', 'daily_weight': 1.0, 'effective_sharpe': 2.0, 'cadence_days': 5}]
    signals = [{'strategy_id': 'S_eth', 'ticker': 'ETH-USD', 'direction': 'long'}]
    orders = crs.size_crypto_positions(
        {'equity': 100_000.0}, {'state': 'LOW_VOL'},
        broker_loader=lambda: broker,
        weights_loader=lambda regime: weights,
        signals_loader=lambda regime, wbs: signals,
        crypto_strategy_ids={'S_eth'}, all_live_weight_sum=1.0)
    tickers = {o['ticker'] for o in orders}
    assert 'BTC-USD' in tickers
    assert 'ETH-USD' in tickers
    assert 'SPY' not in tickers


def test_production_format_broker_symbol_nets_to_zero():
    # Alpaca returns crypto positions as 'BTC/USD' (slash); the sizer must
    # normalize to 'BTC-USD' and NET against the 'BTC-USD' target — not double up.
    broker = {'SPY': 150_000.0, 'BTC/USD': 20_000.0}  # slash form (real Alpaca)
    weights = [{'strategy_id': 'S_btc', 'daily_weight': 1.0, 'effective_sharpe': 2.0, 'cadence_days': 5}]
    signals = [{'strategy_id': 'S_btc', 'ticker': 'BTC-USD', 'direction': 'long'}]
    orders = crs.size_crypto_positions(
        {'equity': 100_000.0}, {'state': 'LOW_VOL'},
        broker_loader=lambda: broker,
        weights_loader=lambda regime: weights,
        signals_loader=lambda regime, wbs: signals,
        crypto_strategy_ids={'S_btc'}, all_live_weight_sum=10.0)  # share 0.1 → budget 20k == held
    assert orders == []  # matched position nets to ~0 → no double-up order


def test_short_signal_emits_short():
    weights = [{'strategy_id': 'S_btc', 'daily_weight': 1.0, 'effective_sharpe': 2.0, 'cadence_days': 5}]
    signals = [{'strategy_id': 'S_btc', 'ticker': 'BTC-USD', 'direction': 'short'}]
    orders = crs.size_crypto_positions(
        {'equity': 100_000.0}, {'state': 'LOW_VOL'},
        broker_loader=lambda: {},
        weights_loader=lambda regime: weights,
        signals_loader=lambda regime, wbs: signals,
        crypto_strategy_ids={'S_btc'}, all_live_weight_sum=10.0)
    assert len(orders) == 1
    assert orders[0]['ticker'] == 'BTC-USD'
    assert orders[0]['direction'] == 'short'


def test_driver_gate_off_is_noop(monkeypatch):
    monkeypatch.delenv('OPENCLAW_CRYPTO_REDEPLOY', raising=False)
    import importlib
    sys.path.insert(0, str(ROOT / 'scripts'))
    rc = importlib.import_module('redeploy_crypto')
    assert rc.main(['--reason', 'TEST']) == 0  # no-op, returns 0 without DB/broker


def test_cooldown_key_is_crypto_namespaced():
    sys.path.insert(0, str(ROOT / 'scripts'))
    import redeploy_crypto as rc
    assert rc._cooldown_key('2026-05-26') == 'redeploy:crypto:cooldown:2026-05-26'
    assert rc._sentinel_key('2026-05-26', 'X') == 'redeploy:crypto:fired:2026-05-26:X'


def test_spawn_crypto_redeploy_builds_detached_cmd(monkeypatch):
    sys.path.insert(0, str(ROOT / 'scripts'))
    import run_crypto_market_state as rcms
    captured = {}
    class _FakePopen:
        def __init__(self, cmd, **kw): captured['cmd'] = cmd; captured['kw'] = kw
    monkeypatch.setattr('subprocess.Popen', _FakePopen)
    monkeypatch.setenv('OPENCLAW_CRYPTO_REDEPLOY', '1')
    rcms._spawn_crypto_redeploy('LOW_VOL', 'CRISIS', '2026-05-26')
    assert 'redeploy_crypto.py' in ' '.join(captured['cmd'])
    assert '--reason' in captured['cmd']
    assert captured['kw'].get('start_new_session') is True   # detached


def test_emitted_open_order_submits_through_executor(monkeypatch):
    # Integration: a sizer-emitted OPEN order must carry pct_nav so the executor's
    # crypto path computes a positive notional and SUBMITS (not SKIP). Catches the
    # notional_usd/pct_nav contract mismatch.
    from unittest.mock import patch
    weights = [{'strategy_id': 'S_btc', 'daily_weight': 1.0, 'effective_sharpe': 2.0, 'cadence_days': 5}]
    signals = [{'strategy_id': 'S_btc', 'ticker': 'BTC-USD', 'direction': 'long'}]
    orders = crs.size_crypto_positions(
        {'equity': 100_000.0}, {'state': 'LOW_VOL'},
        broker_loader=lambda: {},
        weights_loader=lambda regime: weights,
        signals_loader=lambda regime, wbs: signals,
        crypto_strategy_ids={'S_btc'}, all_live_weight_sum=10.0)  # budget 20k
    assert len(orders) == 1 and orders[0]['ticker'] == 'BTC-USD'
    assert 'pct_nav' in orders[0] and orders[0]['pct_nav'] > 0

    from execution import alpaca_executor as ae
    responses = {
        'data crypto latest-trades': (True, {'trades': {'BTC/USD': {'p': 40000.0}}}, None),
        'order submit': (True, {'id': 'ord-x', 'qty': '0.5'}, None),
    }
    def _fake(args, timeout=30):
        j = ' '.join(args)
        for k, v in responses.items():
            if k in j:
                return v
        raise AssertionError(f'unexpected CLI call: {j}')
    with patch.object(ae, '_run_alpaca_cli', side_effect=_fake):
        res = ae._route_crypto_order(orders[0], 100_000.0, 'COID')
    assert res['status'] == 'submitted'   # NOT 'SKIP' (non-positive notional)
