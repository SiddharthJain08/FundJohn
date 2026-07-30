"""Intraday financials adapter — tier-1 acting-set ingest (2026-07-30).

Mock-only: FMP's calendar and statement endpoints are stubbed. Pins the scope
rule (today's in-universe reporters, not the universe) and the period dedup,
which cannot key on `period` because the master's labels are polluted.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from ingestion import intraday_financials as fi  # noqa: E402

AS_OF = pd.Timestamp('2026-07-30')


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


def _stub_calendar(monkeypatch, records):
    class _Req:
        @staticmethod
        def get(url, timeout=None, params=None):
            return _Resp(records)
    monkeypatch.setitem(sys.modules, 'requests', _Req)
    monkeypatch.setenv('FMP_API_KEY', 'x')


def _cal(sym, eps_actual=1.0):
    return {'symbol': sym, 'date': '2026-07-30', 'epsActual': eps_actual}


class TestReporterScope:
    def test_only_in_universe_reporters(self, monkeypatch):
        _stub_calendar(monkeypatch, [_cal('AAPL'), _cal('ZZZZ')])
        assert fi.reporters(AS_OF, ['AAPL', 'MSFT']) == ['AAPL']

    def test_scheduled_but_unreleased_is_skipped(self, monkeypatch):
        """epsActual=None means the release has not happened — there is
        nothing new to fetch yet."""
        _stub_calendar(monkeypatch, [_cal('AAPL', eps_actual=None), _cal('MSFT')])
        assert fi.reporters(AS_OF, ['AAPL', 'MSFT']) == ['MSFT']

    def test_window_covers_yesterday_and_today(self, monkeypatch):
        """An after-close reporter on T-1 publishes too late for T-1's 14:30
        pass, so T has to pick it up."""
        seen = {}

        class _Req:
            @staticmethod
            def get(url, timeout=None, params=None):
                seen.update(params)
                return _Resp([])
        monkeypatch.setitem(sys.modules, 'requests', _Req)
        monkeypatch.setenv('FMP_API_KEY', 'x')
        fi.reporters(AS_OF, ['AAPL'])
        assert seen['from'] == '2026-07-29' and seen['to'] == '2026-07-30'

    def test_calendar_failure_raises(self, monkeypatch):
        class _Req:
            @staticmethod
            def get(*a, **k):
                raise RuntimeError('503')
        monkeypatch.setitem(sys.modules, 'requests', _Req)
        monkeypatch.setenv('FMP_API_KEY', 'x')
        with pytest.raises(fi.IntradayFinancialsError):
            fi.reporters(AS_OF, ['AAPL'])

    def test_missing_api_key_raises(self, monkeypatch):
        monkeypatch.delenv('FMP_API_KEY', raising=False)
        with pytest.raises(fi.IntradayFinancialsError):
            fi.reporters(AS_OF, ['AAPL'])


class TestBuildOverlay:
    def _wire(self, monkeypatch, *, rows, known=frozenset(), reporters=('AAPL',)):
        monkeypatch.setattr(fi, 'reporters', lambda a, u: list(reporters))
        monkeypatch.setattr(fi, 'master_period_keys', lambda t: set(known))
        monkeypatch.setattr(fi, 'PACING_S', 0.0)
        fake_fmp = type(sys)('fmp')
        for name in ('get_financial_statements', 'get_key_metrics',
                     'get_ratios', 'get_balance_sheet'):
            setattr(fake_fmp, name, lambda *a, **k: [])
        monkeypatch.setitem(sys.modules, 'fmp', fake_fmp)
        fake_bf = type(sys)('pipeline.backfillers.fmp')
        fake_bf.build_financial_rows = lambda tk, s, m, r, b: list(rows)
        monkeypatch.setitem(sys.modules, 'pipeline.backfillers.fmp', fake_bf)

    def test_new_period_is_kept(self, monkeypatch):
        self._wire(monkeypatch, rows=[{'ticker': 'AAPL', 'date': '2026-06-30',
                                       'period': 'Q3', 'revenue': 1.0}])
        df, stats = fi.build_overlay(['AAPL'], AS_OF)
        assert len(df) == 1 and stats['rows'] == 1

    def test_period_already_in_master_is_dropped(self, monkeypatch):
        self._wire(monkeypatch,
                   rows=[{'ticker': 'AAPL', 'date': '2026-03-28', 'period': 'Q2'}],
                   known={('AAPL', '2026-03-28')})
        df, stats = fi.build_overlay(['AAPL'], AS_OF)
        assert df.empty and stats['dup_in_master'] == 1

    def test_dedup_keys_on_the_period_end_date_not_the_label(self, monkeypatch):
        """The master carries 'Q2', '2025Q4' and 'undefinedQ2' — AAPL holds the
        same 2026-03-28 quarter under two of them. A label-keyed anti-join
        would re-add quarters the master already has."""
        self._wire(monkeypatch,
                   rows=[{'ticker': 'AAPL', 'date': '2026-03-28',
                          'period': 'undefinedQ2'}],
                   known={('AAPL', '2026-03-28')})
        df, _ = fi.build_overlay(['AAPL'], AS_OF)
        assert df.empty

    def test_no_reporters_is_a_quiet_day_not_a_failure(self, monkeypatch):
        self._wire(monkeypatch, rows=[], reporters=())
        df, stats = fi.build_overlay(['AAPL'], AS_OF)
        assert df.empty and stats['reporters'] == 0 and stats['rows'] == 0

    def test_budget_stops_the_sweep_and_reports_the_shortfall(self, monkeypatch):
        import time as _t
        self._wire(monkeypatch, rows=[{'ticker': 'X', 'date': '2026-06-30'}],
                   reporters=tuple(f'T{i}' for i in range(20)))
        monkeypatch.setattr(fi, 'PACING_S', 0.05)
        df, stats = fi.build_overlay(['X'], AS_OF, budget_s=0.1)
        assert stats['skipped_budget'] > 0
        assert stats['fetched'] + stats['skipped_budget'] == 20

    def test_per_ticker_failure_does_not_abort_the_sweep(self, monkeypatch):
        self._wire(monkeypatch, rows=[{'ticker': 'A', 'date': '2026-06-30'}],
                   reporters=('A', 'B'))
        fake_fmp = sys.modules['fmp']
        calls = {'n': 0}

        def flaky(tk, *a, **k):
            calls['n'] += 1
            if tk == 'A':
                raise RuntimeError('429')
            return []
        fake_fmp.get_financial_statements = flaky
        df, stats = fi.build_overlay(['A', 'B'], AS_OF)
        assert stats['failed'] == 1 and stats['fetched'] == 1
        assert any('A:' in s for s in stats['failed_sample'])


class TestDriverOrdering:
    """The order is a risk decision, not a detail (2026-07-30 stress run)."""

    def test_open_ended_options_runs_last(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            'rai', ROOT / 'scripts' / 'run_acting_ingest.py')
        rai = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rai)
        assert rai.ADAPTER_ORDER[-1] == 'options_eod', (
            'options is the only adapter whose duration is unbounded by its '
            'work list (294s quiet, 856s under contention). Placed before '
            'financials it truncated it at 192/270 reporters; last, it '
            'absorbs the remaining budget and degrades per-ticker.')
        assert rai.ADAPTER_ORDER[0] == 'insider'
        assert set(rai.ADAPTER_ORDER) == set(rai.ADAPTERS)
