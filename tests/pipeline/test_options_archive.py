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


def test_append_parquet_write_is_atomic(tmp_path, monkeypatch):
    """2026-06-29 regression: a kill mid-write must leave the previous master
    intact. The write path is parquet_store.append_dedup (DuckDB streaming
    merge since 2026-07-20) — its commit boundary is the final os.replace, so
    the kill is simulated there."""
    from src.pipeline.backfillers.alpaca_options import _append_parquet
    from src.data import parquet_store
    parquet = tmp_path / 'options_eod.parquet'
    _append_parquet(
        [{'date': '2026-05-21', 'contract_symbol': 'A', 'delta': 0.5}],
        parquet_path=parquet,
    )
    before = parquet.read_bytes()

    def dying_replace(src, dst):
        # Simulate SIGTERM at the commit boundary: tmp written, replace never lands.
        raise KeyboardInterrupt('killed before os.replace')

    monkeypatch.setattr(parquet_store.os, 'replace', dying_replace)
    with pytest.raises(KeyboardInterrupt):
        _append_parquet(
            [{'date': '2026-05-22', 'contract_symbol': 'B', 'delta': 0.6}],
            parquet_path=parquet,
        )
    monkeypatch.undo()

    assert parquet.read_bytes() == before, 'master must be untouched by a failed write'
    df = pd.read_parquet(parquet)  # still readable
    assert len(df) == 1


def test_main_fail_fast_on_corrupt_master(tmp_path, monkeypatch):
    """A corrupt master previously produced ~5k per-ticker read failures per
    run; main() must now abort before touching the universe or the pool."""
    from src.pipeline.backfillers import alpaca_options
    corrupt = tmp_path / 'options_eod.parquet'
    corrupt.write_bytes(b'PAR1' + b'\x00' * 128)  # header ok, no footer
    monkeypatch.setattr(alpaca_options, 'PARQUET_PATH', corrupt)
    with patch.object(alpaca_options, '_load_universe') as mock_uni, \
         patch.object(alpaca_options, '_resolver_archive_universe') as mock_res:
        rc = alpaca_options.main('2026-07-02')
        assert rc == 1
        mock_uni.assert_not_called()
        mock_res.assert_not_called()


def test_main_single_merge_write_and_deferred_checkpoints(tmp_path, monkeypatch):
    """The run must rewrite the master ONCE (not once per ticker) and set
    Redis checkpoints only after the flush lands."""
    from src.pipeline.backfillers import alpaca_options
    parquet = tmp_path / 'options_eod.parquet'
    monkeypatch.setattr(alpaca_options, 'PARQUET_PATH', parquet)

    def fake_page(ticker, page_token=None):
        return {
            'next_page_token': None,
            'snapshots': {
                f'{ticker}260618C00100000':
                    SAMPLE_CHAIN_PAGE_1['snapshots']['SPY260618C00742000'],
            },
        }

    merge_calls = []
    real_merge = alpaca_options._merge_write

    def counting_merge(new_df, **kw):
        merge_calls.append(len(new_df))
        kw.setdefault('parquet_path', parquet)
        return real_merge(new_df, **kw)

    checkpoints = []
    with patch.object(alpaca_options, '_fetch_chain_page', side_effect=fake_page), \
         patch.object(alpaca_options, '_resolver_archive_universe', return_value=None), \
         patch.object(alpaca_options, '_load_universe', return_value=['SPY', 'QQQ', 'IWM']), \
         patch.object(alpaca_options, '_redis_checkpoint_done', return_value=False), \
         patch.object(alpaca_options, '_redis_checkpoint_set',
                      side_effect=lambda t, d: checkpoints.append(t)), \
         patch.object(alpaca_options, '_merge_write', side_effect=counting_merge):
        rc = alpaca_options.main('2026-07-02')

    assert rc == 0
    assert len(merge_calls) == 1, f'expected ONE master rewrite, got {len(merge_calls)}'
    assert merge_calls[0] == 3
    assert sorted(checkpoints) == ['IWM', 'QQQ', 'SPY']
    df = pd.read_parquet(parquet)
    assert len(df) == 3


def test_main_no_checkpoint_when_flush_fails(tmp_path, monkeypatch):
    """Flush failure must return rc=1 and checkpoint NOTHING — otherwise the
    day is marked done while its rows evaporate with the process."""
    from src.pipeline.backfillers import alpaca_options
    monkeypatch.setattr(alpaca_options, 'PARQUET_PATH', tmp_path / 'options_eod.parquet')

    def fake_page(ticker, page_token=None):
        return {
            'next_page_token': None,
            'snapshots': {
                f'{ticker}260618C00100000':
                    SAMPLE_CHAIN_PAGE_1['snapshots']['SPY260618C00742000'],
            },
        }

    with patch.object(alpaca_options, '_fetch_chain_page', side_effect=fake_page), \
         patch.object(alpaca_options, '_resolver_archive_universe', return_value=None), \
         patch.object(alpaca_options, '_load_universe', return_value=['SPY']), \
         patch.object(alpaca_options, '_redis_checkpoint_done', return_value=False), \
         patch.object(alpaca_options, '_redis_checkpoint_set') as mock_ckpt, \
         patch.object(alpaca_options, '_merge_write', side_effect=OSError('disk full')):
        rc = alpaca_options.main('2026-07-02')

    assert rc == 1
    mock_ckpt.assert_not_called()


def test_decode_occ_rejects_adjusted_symbol():
    """SP-1: GME1-style adjusted symbols must not produce bogus expiries."""
    from src.pipeline.backfillers.alpaca_options import _decode_occ
    occ = _decode_occ('GME1260618C00003000')
    # Should NOT parse mm='60' — flag for operator review by returning None fields
    assert occ['expiry'] is None
    # Strike also None since we bail before strike parse
    assert occ['strike'] is None


def test_flatten_snapshot_writes_legacy_field_aliases():
    """SP-1 deploy regression: engine.py hardcodes 'option_type', 'ticker', and
    'implied_volatility' column names. The new archiver must write those legacy
    aliases alongside the new names, or engine.py reads of new rows yield NaN
    where master rows yielded actual values."""
    from src.pipeline.backfillers.alpaca_options import _flatten_snapshot
    snap = SAMPLE_CHAIN_PAGE_1['snapshots']['SPY260618C00742000']
    row = _flatten_snapshot('SPY260618C00742000', snap, date='2026-05-21', underlying='SPY')
    # New names (canonical)
    assert row['type'] == 'call'
    assert row['underlying'] == 'SPY'
    assert row['iv_implied'] == 0.13
    # Legacy aliases (engine.py compat)
    assert row['option_type'] == row['type']
    assert row['ticker'] == row['underlying']
    assert row['implied_volatility'] == row['iv_implied']


def test_load_universe_uses_canonical_sp500_table(monkeypatch):
    """SP-1 deploy regression: _load_universe must hit universe_config, not
    alpaca_tradable_universe. The original implementer mis-read the spec and
    pointed at the 12k-ticker broker table; SP-1 stays on S&P 500."""
    from src.pipeline.backfillers import alpaca_options
    captured = {}

    class FakeCursor:
        def execute(self, sql):
            captured['sql'] = sql

        def fetchall(self):
            return [('AAPL',), ('MSFT',)]

        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeConn:
        def cursor(self): return FakeCursor()
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setenv('POSTGRES_URI', 'postgresql://noop')
    monkeypatch.setattr('psycopg2.connect', lambda *_a, **_kw: FakeConn())
    universe = alpaca_options._load_universe()
    assert universe == ['AAPL', 'MSFT']
    assert 'universe_config' in captured['sql']
    assert 'alpaca_tradable_universe' not in captured['sql']
