"""tests/test_crypto_redeploy.py — SP-3.1 Phase C crypto redeploy (equity-untouched)."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
