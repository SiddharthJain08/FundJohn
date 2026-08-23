"""2026-08-23: three new masters (short_interest, earnings_calendar_nasdaq,
cboe_chain_aggregates) use settlement_date / report_date / underlying as their
axis columns; the ledger must see their coverage, and the provider labels
must stop saying 'polygon' for Alpaca-fed files."""
import datetime as dt
import pandas as pd

from src.strategies import sync_data_ledger as mod


def test_stats_detects_settlement_report_dates_and_underlying(tmp_path):
    p = tmp_path / 'short_interest.parquet'
    pd.DataFrame({'settlement_date': [dt.date(2026, 7, 15), dt.date(2026, 7, 31)],
                  'ticker': ['AAPL', 'MSFT'], 'short_interest': [1, 2]}).to_parquet(p, index=False)
    s = mod._stats(p)
    assert (s['row_count'], s['ticker_count']) == (2, 2)
    assert str(s['min_date'])[:10] == '2026-07-15' and str(s['max_date'])[:10] == '2026-07-31'

    q = tmp_path / 'cboe_chain_aggregates.parquet'
    pd.DataFrame({'date': [dt.date(2026, 8, 21)] * 2, 'underlying': ['SPX', 'AAPL'], 'iv30': [12.0, 24.0]}).to_parquet(q, index=False)
    s = mod._stats(q)
    assert (s['row_count'], s['ticker_count']) == (2, 2)

    r = tmp_path / 'earnings_calendar_nasdaq.parquet'
    pd.DataFrame({'report_date': [dt.date(2026, 8, 20)], 'ticker': ['NVDA']}).to_parquet(r, index=False)  # ledger counts historical dates only
    assert str(mod._stats(r)['max_date'])[:10] == '2026-08-20'


def test_new_masters_registered_with_honest_providers():
    for k in ('short_interest', 'earnings_calendar_nasdaq', 'cboe_chain_aggregates'):
        assert k in mod.PARQUET_MAP and k in mod.PROVIDERS
    assert mod.PROVIDERS['short_interest'] == 'finra'
    assert mod.PROVIDERS['cboe_chain_aggregates'] == 'cboe'
    assert mod.PROVIDERS['earnings_calendar_nasdaq'] == 'nasdaq'
    assert mod.PROVIDERS['prices'] == 'alpaca' and mod.PROVIDERS['options_eod'] == 'alpaca'
    assert mod.PROVIDERS['earnings'] == 'fmp'
    assert 'fred' in mod.PROVIDERS['macro']


def test_stats_streams_without_materialising_rows(tmp_path, monkeypatch):
    """2026-08-23: the pushdown read still called .to_pandas() on date+ticker of
    every master — 48M options_eod rows → 5.3 GB RSS → OOM-killed (and took
    a co-tenant job with it). _stats must aggregate in DuckDB and never
    materialise the frame."""
    import pyarrow.parquet as pq
    p = tmp_path / 'big.parquet'
    pd.DataFrame({'date': [dt.date(2026, 8, 20), dt.date(2026, 8, 21), dt.date(2099, 1, 1)],
                  'ticker': ['A', 'B', 'B'], 'x': [1.0, 2.0, 3.0]}).to_parquet(p, index=False)

    def boom(*a, **k):
        raise AssertionError('to_pandas() must not be called by _stats')
    monkeypatch.setattr(pq, 'read_table', boom)
    s = mod._stats(p)
    assert s == {'min_date': '2026-08-20', 'max_date': '2026-08-21', 'row_count': 3, 'ticker_count': 2}
