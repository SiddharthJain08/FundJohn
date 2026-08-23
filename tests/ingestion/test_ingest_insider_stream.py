"""D4 (2026-08-23): daily global Form-4 stream → insider.parquet master.

The EOD collector walked `/insider-trading/search?symbol=X` for all ~11.8k
active tickers EVERY day ("0 tickers skipped (fresh)") because its 7-day
freshness check used data_coverage date ranges, which yesterday's date_to can
never satisfy. The global `/insider-trading/latest` stream (already used by the
14:30 ET overlay) covers every new filing market-wide in a handful of pages;
this module folds that stream into the MASTER so the per-symbol walk can drop
to a weekly reconciliation.
"""
from __future__ import annotations

import pandas as pd
import pytest

from src.ingestion import ingest_insider_stream as mod


def _row(ticker, filing, name='CEO PERSON', ttype='P-Purchase', shares=100.0):
    return {
        'ticker': ticker, 'date': filing, 'transaction_date': filing,
        'insider_name': name, 'role': 'officer', 'transaction_type': ttype,
        'shares': shares, 'price_per_share': 10.0, 'net_value': shares * 10.0,
        'shares_owned_after': 1000.0, 'filing_date': filing,
    }


def test_since_overlaps_master_max_date_by_two_days():
    assert mod._since_for('2026-08-21') == '2026-08-19'
    assert mod._since_for('2026-08-21', overlap_days=0) == '2026-08-21'


def test_since_falls_back_to_lookback_when_master_empty():
    assert mod._since_for(None, today='2026-08-23', fallback_days=5) == '2026-08-18'


def test_merge_appends_only_rows_not_already_in_master(tmp_path):
    master = tmp_path / 'insider.parquet'
    pd.DataFrame([_row('AAPL', '2026-08-20'), _row('MSFT', '2026-08-20')]).to_parquet(master, index=False)

    stream = [
        _row('AAPL', '2026-08-20'),            # duplicate of master row → dropped
        _row('AAPL', '2026-08-21'),            # new filing
        _row('NVDA', '2026-08-21'),            # new ticker (append-only: tickers may be ADDED)
        _row('NVDA', '2026-08-21'),            # duplicate within the fetch → one row
    ]
    stats = mod.merge_stream_into_master(stream, master_path=master)

    out = pd.read_parquet(master)
    assert len(out) == 4, out
    assert stats['fetched'] == 4
    assert stats['new_rows'] == 2
    assert sorted(out['ticker'].unique()) == ['AAPL', 'MSFT', 'NVDA']
    # master's own key (filing_date-based) still unique
    assert not out.duplicated(subset=['ticker', 'filing_date', 'insider_name', 'transaction_type', 'shares']).any()


def test_merge_drops_rows_without_ticker_or_filing_date(tmp_path):
    master = tmp_path / 'insider.parquet'
    bad = _row('', '2026-08-21'); bad2 = _row('AAPL', None)
    stats = mod.merge_stream_into_master([bad, bad2, _row('AAPL', '2026-08-21')], master_path=master)
    assert stats['new_rows'] == 1
    assert len(pd.read_parquet(master)) == 1


def test_main_refuses_when_stream_unreadable(monkeypatch, tmp_path):
    """'The provider refused' and 'no filings today' must not both be rc=0
    with zero rows — read the counters, not the exit code."""
    from src.ingestion import intraday_insider
    monkeypatch.setattr(mod, 'MASTER_PATH', tmp_path / 'insider.parquet')

    def boom(since, **kw):
        raise intraday_insider.IntradayInsiderError('page 0 failed')
    monkeypatch.setattr(mod, 'fetch_latest_filings', boom)
    assert mod.main([]) == 1


def test_main_merges_fetched_rows(monkeypatch, tmp_path):
    master = tmp_path / 'insider.parquet'
    monkeypatch.setattr(mod, 'MASTER_PATH', master)
    monkeypatch.setattr(mod, 'fetch_latest_filings',
                        lambda since, **kw: ([_row('AAPL', '2026-08-22')], {'pages': 1, 'raw_rows': 1}))
    assert mod.main(['--since', '2026-08-20']) == 0
    assert len(pd.read_parquet(master)) == 1
