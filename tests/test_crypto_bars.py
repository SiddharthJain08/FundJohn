"""tests/test_crypto_bars.py — SP-3.1 Phase B hourly crypto bar feed."""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from ingestion import crypto_bars as cb  # noqa: E402


def test_parse_bars_payload():
    payload = {'bars': {'BTC/USD': [
        {'t': '2026-05-24T00:00:00Z', 'o': 1.0, 'h': 2.0, 'l': 0.5, 'c': 1.5, 'v': 10.0, 'vw': 1.2},
        {'t': '2026-05-24T01:00:00Z', 'o': 1.5, 'h': 2.5, 'l': 1.0, 'c': 2.0, 'v': 11.0, 'vw': 1.7},
    ]}}
    rows = cb._parse_bars(payload, 'BTC/USD', 'BTC-USD')
    assert len(rows) == 2
    assert rows[0]['ticker'] == 'BTC-USD'
    assert rows[0]['ts_utc'] == '2026-05-24T00:00:00Z'
    assert rows[0]['close'] == 1.5
    assert rows[1]['vwap'] == 1.7


def test_append_parquet_is_append_only_and_dedupes(tmp_path):
    p = tmp_path / 'crypto_bars_1h.parquet'
    r1 = [{'ticker': 'BTC-USD', 'ts_utc': '2026-05-24T00:00:00Z', 'open': 1, 'high': 2,
           'low': 0.5, 'close': 1.5, 'volume': 10, 'vwap': 1.2}]
    cb.append_bars(r1, parquet_path=p)
    r2 = [dict(r1[0]),
          {'ticker': 'BTC-USD', 'ts_utc': '2026-05-24T01:00:00Z', 'open': 1.5, 'high': 2.5,
           'low': 1.0, 'close': 2.0, 'volume': 11, 'vwap': 1.7}]
    cb.append_bars(r2, parquet_path=p)
    import pandas as pd
    df = pd.read_parquet(p)
    assert len(df) == 2  # dedup on (ticker, ts_utc); no row dropped
    assert set(df['ts_utc']) == {'2026-05-24T00:00:00Z', '2026-05-24T01:00:00Z'}
