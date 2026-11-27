"""Phantom cleanup must compare ENGINE tickers (2026-11-27).

The broker reports crypto as 'BTCUSD'; execution_signals stores 'BTC-USD'.
Before this fix every crypto intent row older than today was closed as a
phantom by the next reconcile, which is why BTC-USD vanished from the
Portfolio page's benchmark section the day after each S_beta_btc signal.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import alpaca_reconcile as ar  # noqa: E402

POSITIONS = [
    {'symbol': 'BTCUSD', 'asset_class': 'crypto', 'qty': '0.137'},
    {'symbol': 'SPY', 'asset_class': 'us_equity', 'qty': '0.63'},
    {'symbol': 'BF.B', 'asset_class': 'us_equity', 'qty': '3'},
]


def test_positions_map_to_engine_tickers_and_keep_raw():
    got = ar.broker_tickers_from_positions(POSITIONS)
    assert {'BTC-USD', 'BTCUSD', 'SPY', 'BF.B'} <= got


def test_slash_form_and_unknown_crypto_pair_normalise():
    got = ar.broker_tickers_from_positions([
        {'symbol': 'ETH/USD', 'asset_class': 'crypto'},
        {'symbol': 'ZZZUSD', 'asset_class': 'crypto'},
        {'symbol': 'ZZZUSD', 'asset_class': 'us_equity'},   # an equity that merely ends in USD
    ])
    assert 'ETH-USD' in got
    assert 'ZZZ-USD' in got
    assert 'ZZZUSD' in got


def test_fetch_broker_tickers_returns_engine_spelling():
    proc = MagicMock(returncode=0, stdout=json.dumps(POSITIONS))
    with patch.object(ar.subprocess, 'run', return_value=proc):
        got = ar.fetch_broker_tickers()
    assert 'BTC-USD' in got and 'SPY' in got


def test_cleanup_spares_the_held_crypto_row():
    """The SQL excludes every ticker in the set, so BTC-USD must be in it."""
    cur = MagicMock()
    cur.fetchall.return_value = []
    conn = MagicMock(); conn.cursor.return_value = cur
    tickers = ar.broker_tickers_from_positions(POSITIONS)
    ar.cleanup_phantom_signals(conn, dry_run=True, broker_tickers=tickers)
    args = cur.execute.call_args[0]
    assert 'BTC-USD' in args[1][0]
