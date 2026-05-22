"""SP-1: tests for Alpaca options chain self-archive (replaces Massive)."""
import json
import pandas as pd
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.pipeline.backfillers.alpaca_options import (
    archive_ticker_chain,
    _flatten_snapshot,
    _redis_checkpoint_done,
    _redis_checkpoint_set,
)


SAMPLE_CHAIN_PAGE_1 = {
    'next_page_token': 'TOKEN_PAGE2',
    'snapshots': {
        'SPY260618C00742000': {
            'dailyBar': {'o': 13.5, 'h': 14.0, 'l': 13.2, 'c': 13.05, 'v': 5000, 'vw': 13.6, 'n': 200},
            'greeks': {'delta': 0.548, 'gamma': 0.0137, 'theta': -0.2428, 'vega': 0.8148, 'rho': 0.3025},
            'latestQuote': {'bp': 12.98, 'ap': 13.05},
            'impliedVolatility': 0.13,
        },
    },
}
SAMPLE_CHAIN_PAGE_2 = {
    'next_page_token': None,
    'snapshots': {
        'SPY260618C00743000': {
            'dailyBar': {'o': 13.0, 'h': 13.4, 'l': 12.6, 'c': 12.44, 'v': 4500, 'vw': 12.95, 'n': 180},
            'greeks': {'delta': 0.535, 'gamma': 0.014, 'theta': -0.241, 'vega': 0.82, 'rho': 0.295},
            'latestQuote': {'bp': 12.37, 'ap': 12.44},
            'impliedVolatility': 0.129,
        },
    },
}


def test_flatten_snapshot_extracts_all_fields():
    snap = SAMPLE_CHAIN_PAGE_1['snapshots']['SPY260618C00742000']
    row = _flatten_snapshot('SPY260618C00742000', snap, date='2026-05-21', underlying='SPY')
    assert row['underlying'] == 'SPY'
    assert row['contract_symbol'] == 'SPY260618C00742000'
    assert row['strike'] == 742.0
    assert row['expiry'] == '2026-06-18'
    assert row['type'] == 'call'
    assert row['delta'] == 0.548
    assert row['close'] == 13.05
    assert row['iv_implied'] == 0.13
    assert row['data_source'] == 'alpaca_aat_plus'


def test_pagination_concatenates_two_pages():
    calls = [SAMPLE_CHAIN_PAGE_1, SAMPLE_CHAIN_PAGE_2]
    with patch('src.pipeline.backfillers.alpaca_options._fetch_chain_page', side_effect=calls), \
         patch('src.pipeline.backfillers.alpaca_options._append_parquet') as mock_append, \
         patch('src.pipeline.backfillers.alpaca_options._redis_checkpoint_done', return_value=False), \
         patch('src.pipeline.backfillers.alpaca_options._redis_checkpoint_set'):
        archive_ticker_chain('SPY', date='2026-05-21')
        rows = mock_append.call_args[0][0]
        assert len(rows) == 2
        assert {r['contract_symbol'] for r in rows} == {
            'SPY260618C00742000', 'SPY260618C00743000',
        }


def test_idempotent_skip_when_checkpoint_done():
    with patch('src.pipeline.backfillers.alpaca_options._redis_checkpoint_done', return_value=True), \
         patch('src.pipeline.backfillers.alpaca_options._fetch_chain_page') as mock_fetch:
        archive_ticker_chain('SPY', date='2026-05-21')
        mock_fetch.assert_not_called()


def test_dedupe_on_date_and_contract(tmp_path):
    """Re-running for the same (date, contract_symbol) does not duplicate rows."""
    from src.pipeline.backfillers.alpaca_options import _append_parquet
    parquet = tmp_path / 'options_eod.parquet'
    rows1 = [{'date': '2026-05-21', 'contract_symbol': 'SPY260618C00742000', 'delta': 0.5}]
    rows2 = [{'date': '2026-05-21', 'contract_symbol': 'SPY260618C00742000', 'delta': 0.6}]
    _append_parquet(rows1, parquet_path=parquet)
    _append_parquet(rows2, parquet_path=parquet)
    df = pd.read_parquet(parquet)
    assert len(df) == 1
    assert df.iloc[0]['delta'] == 0.6  # last write wins on the duplicate


def test_concurrent_append_preserves_all_rows(tmp_path):
    """Critical regression: parallel _append_parquet calls must not overwrite each other."""
    from concurrent.futures import ThreadPoolExecutor
    from src.pipeline.backfillers.alpaca_options import _append_parquet
    parquet = tmp_path / 'options_eod.parquet'

    # 8 threads, each writes 100 unique contracts. If any thread loses its writes
    # to a race, the final parquet will have fewer than 800 rows.
    def worker(tid):
        rows = [
            {'date': '2026-05-21', 'contract_symbol': f'AAPL2606{tid}{i:04d}C00100000', 'delta': 0.1 * tid}
            for i in range(100)
        ]
        _append_parquet(rows, parquet_path=parquet)

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(worker, range(8)))

    df = pd.read_parquet(parquet)
    assert len(df) == 800, f'expected 800 rows after 8 concurrent writers, got {len(df)}'


def test_decode_occ_rejects_adjusted_symbol():
    """SP-1: GME1-style adjusted symbols must not produce bogus expiries."""
    from src.pipeline.backfillers.alpaca_options import _decode_occ
    occ = _decode_occ('GME1260618C00003000')
    # Should NOT parse mm='60' — flag for operator review by returning None fields
    assert occ['expiry'] is None
    # Strike also None since we bail before strike parse
    assert occ['strike'] is None
