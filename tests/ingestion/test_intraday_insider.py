"""Intraday insider (Form 4) adapter — tier-1 acting-set ingest (2026-07-30).

Mock-only: the FMP stream is stubbed. Pins the properties that make the
overlay safe to splice into a transaction LIST (unlike the options overlay,
where the engine takes chain['date'].max() and a duplicate is inert — here a
duplicate is double-counted by S_insider_drawdown_confirmation).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from ingestion import intraday_insider as ii  # noqa: E402

AS_OF = pd.Timestamp('2026-07-30')


def _rec(sym='AAPL', filed='2026-07-30', name='Doe Jane', ttype='S-Sale',
         shares=100, price=2.5, txn='2026-07-29', owned=900):
    return {'symbol': sym, 'filingDate': filed, 'transactionDate': txn,
            'reportingName': name, 'typeOfOwner': 'officer: CFO',
            'transactionType': ttype, 'securitiesTransacted': shares,
            'price': price, 'securitiesOwned': owned}


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _stub_pages(monkeypatch, pages):
    calls = {'n': 0}

    class _Req:
        @staticmethod
        def get(url, timeout=None, params=None):
            i = params['page']
            calls['n'] += 1
            return _Resp(pages[i] if i < len(pages) else [])

    monkeypatch.setitem(sys.modules, 'requests', _Req)
    monkeypatch.setenv('FMP_API_KEY', 'x')
    return calls


class TestRowMapping:
    def test_fields_land_in_master_shape(self, monkeypatch):
        _stub_pages(monkeypatch, [[_rec()]])
        rows, _ = ii.fetch_latest_filings('2026-07-30')
        r = rows[0]
        assert r['ticker'] == 'AAPL'
        assert r['transaction_type'] == 'S-Sale'
        assert r['shares'] == 100.0 and r['price_per_share'] == 2.5
        assert r['net_value'] == 250.0
        assert r['shares_owned_after'] == 900.0

    def test_date_mirrors_filing_date(self, monkeypatch):
        """The dedup key uses `date`, not `filing_date`: they are equal
        wherever both are present across 243,541 master rows, but filing_date
        is NULL in 7.1% of history and NULL never matches in a join."""
        _stub_pages(monkeypatch, [[_rec()]])
        rows, _ = ii.fetch_latest_filings('2026-07-30')
        assert rows[0]['date'] == rows[0]['filing_date'] == '2026-07-30'

    def test_records_without_a_symbol_or_date_are_dropped(self, monkeypatch):
        _stub_pages(monkeypatch, [[_rec(sym=''), _rec(filed=None), _rec()]])
        rows, _ = ii.fetch_latest_filings('2026-07-30')
        assert len(rows) == 1


class TestPaging:
    def test_stops_once_a_page_is_entirely_older(self, monkeypatch):
        calls = _stub_pages(monkeypatch, [
            [_rec(filed='2026-07-30')],
            [_rec(filed='2026-07-20')],     # all older than `since` → stop
            [_rec(filed='2026-07-30')],     # must never be requested
        ])
        rows, stats = ii.fetch_latest_filings('2026-07-29')
        assert calls['n'] == 2
        assert len(rows) == 1 and stats['pages'] == 2

    def test_page_zero_failure_raises(self, monkeypatch):
        """"the provider refused" and "no filings today" must not both surface
        as an empty list."""
        class _Req:
            @staticmethod
            def get(*a, **k):
                raise RuntimeError('503')
        monkeypatch.setitem(sys.modules, 'requests', _Req)
        monkeypatch.setenv('FMP_API_KEY', 'x')
        with pytest.raises(ii.IntradayInsiderError):
            ii.fetch_latest_filings('2026-07-30')

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv('FMP_API_KEY', raising=False)
        with pytest.raises(ii.IntradayInsiderError):
            ii.fetch_latest_filings('2026-07-30')


class TestDedup:
    def test_rows_already_in_the_master_are_dropped(self, monkeypatch):
        """The engine builds a LIST per ticker, so a row present in both would
        be counted twice by the consuming strategy."""
        _stub_pages(monkeypatch, [[_rec(), _rec(name='Roe Ray')]])
        monkeypatch.setattr(ii, 'master_keys',
                            lambda since: {('AAPL', '2026-07-30', 'Doe Jane',
                                            'S-Sale', 100.0)})
        monkeypatch.setattr(ii, '_master_max_date', lambda: '2026-07-30')
        df, stats = ii.build_overlay(['AAPL'], AS_OF)
        assert stats['dup_in_master'] == 1
        assert list(df['insider_name']) == ['Roe Ray']

    def test_repeats_within_the_fetch_are_dropped(self, monkeypatch):
        """FMP paging repeats rows across page boundaries."""
        _stub_pages(monkeypatch, [[_rec()], [_rec()]])
        monkeypatch.setattr(ii, 'master_keys', lambda since: set())
        monkeypatch.setattr(ii, '_master_max_date', lambda: '2026-07-30')
        df, _ = ii.build_overlay(['AAPL'], AS_OF)
        assert len(df) == 1

    def test_master_read_failure_refuses_rather_than_skipping_dedup(self, monkeypatch, tmp_path):
        """Silently skipping the anti-join would double-count every filing the
        master already holds — worse than no overlay."""
        monkeypatch.setattr(ii, 'ROOT', tmp_path)
        (tmp_path / 'data' / 'master').mkdir(parents=True)
        (tmp_path / 'data' / 'master' / 'insider.parquet').write_text('not parquet')
        with pytest.raises(ii.IntradayInsiderError):
            ii.master_keys('2026-07-30')


class TestScope:
    def test_filtered_to_the_acting_universe(self, monkeypatch):
        _stub_pages(monkeypatch, [[_rec(sym='AAPL'), _rec(sym='ZZZZ')]])
        monkeypatch.setattr(ii, 'master_keys', lambda since: set())
        monkeypatch.setattr(ii, '_master_max_date', lambda: '2026-07-30')
        df, stats = ii.build_overlay(['AAPL'], AS_OF)
        assert set(df['ticker']) == {'AAPL'}
        assert stats['in_universe'] == 1

    def test_quiet_day_is_an_empty_frame_not_an_error(self, monkeypatch):
        """No new filings in the window is the common case, not a failure."""
        _stub_pages(monkeypatch, [[]])
        monkeypatch.setattr(ii, 'master_keys', lambda since: set())
        monkeypatch.setattr(ii, '_master_max_date', lambda: '2026-07-30')
        df, stats = ii.build_overlay(['AAPL'], AS_OF)
        assert df.empty and stats['rows'] == 0
        assert list(df.columns) == ii.RAW_COLS
