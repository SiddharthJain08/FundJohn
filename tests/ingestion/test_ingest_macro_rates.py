"""Free macro/rates stream (2026-08-23): FRED keyless CSV + NY Fed reference
rates → data/master/macro.parquet (long format date/series/value/source).

No network: every fetch is stubbed; every write goes to a tmp master built
with the production schema (date32 / string / double / string) so the
"schema UNCHANGED + VIX rows preserved" invariant is asserted, not assumed.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from src.ingestion import ingest_macro_rates as mod

MACRO_SCHEMA = pa.schema([
    ('date', pa.date32()),
    ('series', pa.string()),
    ('value', pa.float64()),
    ('source', pa.string()),
])

FRED_CSV = (
    "observation_date,DGS10\n"
    "2026-08-17,4.30\n"
    "2026-08-18,.\n"          # FRED's missing-value marker
    "2026-08-19,4.33\n"
    "2026-08-20,4.69\n"
)

NYFED_JSON = {"refRates": [
    {"effectiveDate": "2026-08-20", "type": "EFFR", "percentRate": 3.63, "revisionIndicator": ""},
    {"effectiveDate": "2026-08-20", "type": "SOFR", "percentRate": 3.61, "percentPercentile1": 3.58},
    {"effectiveDate": "2026-08-21", "type": "SOFRAI", "average30day": 3.64319, "index": 1.2556},
    {"effectiveDate": "2026-08-20", "type": "OBFR", "percentRate": 3.62},
    {"effectiveDate": "2026-08-20", "type": "TGCR", "percentRate": 3.60},
    {"effectiveDate": "2026-08-20", "type": "BGCR", "percentRate": 3.60},
    {"effectiveDate": "2026-08-20", "type": "WEIRD", "percentRate": 9.99},   # unknown type
]}


def _seed_master(path, rows):
    """Write a tmp master with EXACTLY the production arrow schema."""
    tbl = pa.Table.from_pylist(rows, schema=MACRO_SCHEMA)
    pq.write_table(tbl, path, compression='snappy')
    return path


def _vix_rows():
    return [
        {'date': date(2026, 8, 20), 'series': 'VIX', 'value': 16.1, 'source': 'yfinance'},
        {'date': date(2026, 8, 21), 'series': 'VIX', 'value': 15.9, 'source': 'yfinance'},
        {'date': date(2026, 8, 21), 'series': 'VVIX', 'value': 88.2, 'source': 'yfinance'},
    ]


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parse_fred_csv_drops_missing_and_types_dates():
    rows = mod.parse_fred_csv(FRED_CSV, 'DGS10')
    assert [r['date'] for r in rows] == [date(2026, 8, 17), date(2026, 8, 19), date(2026, 8, 20)]
    assert all(isinstance(r['date'], date) for r in rows)
    assert [r['value'] for r in rows] == [4.30, 4.33, 4.69]
    assert all(isinstance(r['value'], float) for r in rows)
    assert {r['series'] for r in rows} == {'DGS10'}
    assert {r['source'] for r in rows} == {'fred'}


def test_parse_fred_csv_rejects_non_csv_body():
    # An HTML error page / empty body must be a FAILURE for that series, not 0 rows.
    with pytest.raises(mod.MacroSourceError):
        mod.parse_fred_csv('<html>blocked</html>', 'DGS10')
    with pytest.raises(mod.MacroSourceError):
        mod.parse_fred_csv('', 'DGS10')


def test_parse_nyfed_rates_maps_percent_rate_rows_only():
    rows = mod.parse_nyfed_rates(NYFED_JSON)
    by_series = {r['series']: r for r in rows}
    assert set(by_series) == {'NYFED_EFFR', 'NYFED_SOFR', 'NYFED_OBFR', 'NYFED_TGCR', 'NYFED_BGCR'}
    assert 'NYFED_SOFRAI' not in by_series and 'NYFED_WEIRD' not in by_series
    assert by_series['NYFED_EFFR']['value'] == 3.63
    assert by_series['NYFED_EFFR']['date'] == date(2026, 8, 20)
    assert isinstance(by_series['NYFED_SOFR']['value'], float)
    assert {r['source'] for r in rows} == {'nyfed'}


# ── incremental window ────────────────────────────────────────────────────────

def test_master_max_dates_and_fred_window_from_tmp_master(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows() + [
        {'date': date(2026, 8, 1), 'series': 'DGS10', 'value': 4.2, 'source': 'fred'},
        {'date': date(2026, 8, 15), 'series': 'DGS10', 'value': 4.3, 'source': 'fred'},
    ])
    mx = mod.master_max_dates(master)
    assert mx['DGS10'] == date(2026, 8, 15)
    assert mx['VIX'] == date(2026, 8, 21)
    # present → max − 7d ; absent → None (= take the full history)
    assert mod.fred_window_start('DGS10', mx) == date(2026, 8, 8)
    assert mod.fred_window_start('DGS2', mx) is None
    assert mod.master_max_dates(tmp_path / 'missing.parquet') == {}


def test_filter_since_keeps_boundary_and_passes_through_when_none():
    rows = mod.parse_fred_csv(FRED_CSV, 'DGS10')
    assert [r['date'] for r in mod.filter_since(rows, date(2026, 8, 19))] == [date(2026, 8, 19), date(2026, 8, 20)]
    assert mod.filter_since(rows, None) == rows


# ── write: schema + VIX rows preserved ────────────────────────────────────────

def test_write_preserves_vix_rows_and_exact_dtypes(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows())
    before_schema = pq.read_schema(master)
    rows = mod.parse_fred_csv(FRED_CSV, 'DGS10') + mod.parse_nyfed_rates(NYFED_JSON)
    before, after = mod.write_rows(rows, master_path=master)
    assert before == 3 and after == 3 + 3 + 5
    assert pq.read_schema(master).equals(before_schema), pq.read_schema(master)
    got = pd.read_parquet(master)
    vix = got[got['series'].isin(['VIX', 'VVIX'])].sort_values(['series', 'date'])
    assert len(vix) == 3 and vix['value'].tolist() == [16.1, 15.9, 88.2]
    assert set(got.loc[got['series'] == 'DGS10', 'source']) == {'fred'}
    assert set(got.loc[got['series'].str.startswith('NYFED_'), 'source']) == {'nyfed'}
    # re-writing the same rows is idempotent (replace on (date, series))
    assert mod.write_rows(rows, master_path=master)[1] == after


def test_write_rows_empty_is_noop(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows())
    assert mod.write_rows([], master_path=master) == (3, 3)


# ── run(): counters, per-series failure accounting, rc ────────────────────────

def _fred_stub(fail=(), csv_by_series=None):
    def fetch(series, **_):
        if series in fail:
            raise RuntimeError(f'boom {series}')
        if csv_by_series and series in csv_by_series:
            return csv_by_series[series]
        return FRED_CSV.replace('DGS10', series)
    return fetch


def _nyfed_stub(fail=False, calls=None):
    def fetch(start, end, **_):
        if calls is not None:
            calls.append((start, end))
        if fail:
            raise RuntimeError('nyfed down')
        return NYFED_JSON
    return fetch


def test_run_one_series_failing_does_not_stop_the_others(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows())
    stats = mod.run(series=['DGS10', 'DGS2', 'DGS5'], full=True, master_path=master,
                    fetch_fred=_fred_stub(fail={'DGS2'}), fetch_nyfed=_nyfed_stub(),
                    today=date(2026, 8, 23), sleep_s=0)
    assert stats['rc'] == 0
    assert stats['series_ok'] == 2 + 5          # 2 FRED + 5 NY Fed types
    assert stats['series_failed'] == 1
    assert stats['failed'] == {'DGS2': 'RuntimeError: boom DGS2'}
    assert stats['rows_fetched'] == 3 + 3 + 5
    assert stats['rows_written'] == 3 + 3 + 5
    assert stats['max_date']['DGS10'] == date(2026, 8, 20)
    assert stats['max_date']['NYFED_EFFR'] == date(2026, 8, 20)
    assert 'DGS2' not in stats['max_date']
    got = pd.read_parquet(master)
    assert set(got['series']) == {'VIX', 'VVIX', 'DGS10', 'DGS5',
                                  'NYFED_EFFR', 'NYFED_SOFR', 'NYFED_OBFR', 'NYFED_TGCR', 'NYFED_BGCR'}


def test_run_rc1_when_every_source_fails_and_master_untouched(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows())
    mtime = master.stat().st_mtime_ns
    stats = mod.run(series=['DGS10', 'DGS2'], full=True, master_path=master,
                    fetch_fred=_fred_stub(fail={'DGS10', 'DGS2'}), fetch_nyfed=_nyfed_stub(fail=True),
                    today=date(2026, 8, 23), sleep_s=0)
    assert stats['rc'] == 1
    assert stats['series_ok'] == 0
    assert stats['series_failed'] == 2 + 5
    assert stats['rows_written'] == 0
    assert master.stat().st_mtime_ns == mtime


def test_run_nyfed_alone_failing_keeps_rc0(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows())
    stats = mod.run(series=['DGS10'], full=True, master_path=master,
                    fetch_fred=_fred_stub(), fetch_nyfed=_nyfed_stub(fail=True),
                    today=date(2026, 8, 23), sleep_s=0)
    assert stats['rc'] == 0 and stats['series_ok'] == 1 and stats['series_failed'] == 5
    assert set(stats['failed']) == {'NYFED_EFFR', 'NYFED_SOFR', 'NYFED_OBFR', 'NYFED_TGCR', 'NYFED_BGCR'}


def test_run_incremental_window_filters_fred_and_bounds_nyfed(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows() + [
        {'date': date(2026, 8, 25), 'series': 'DGS10', 'value': 4.0, 'source': 'fred'},
    ])
    calls = []
    stats = mod.run(series=['DGS10', 'DGS2'], full=False, master_path=master,
                    fetch_fred=_fred_stub(), fetch_nyfed=_nyfed_stub(calls=calls),
                    today=date(2026, 8, 30), sleep_s=0)
    # DGS10 master max = 08-25 → window ≥ 08-18 → only 08-19, 08-20 of the 3 CSV rows
    # DGS2 absent from master → full CSV (3 rows)
    assert stats['rows_fetched'] == 3 + 3 + 5
    assert stats['rows_submitted'] == 2 + 3 + 5
    assert stats['rows_written'] == 2 + 3 + 5
    assert stats['window_start']['DGS10'] == date(2026, 8, 18)
    assert stats['window_start']['DGS2'] is None
    assert calls == [(date(2026, 8, 16), date(2026, 8, 30))]      # today − 14d


def test_run_full_mode_asks_nyfed_for_full_history(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows())
    calls = []
    mod.run(series=['DGS10'], full=True, master_path=master,
            fetch_fred=_fred_stub(), fetch_nyfed=_nyfed_stub(calls=calls),
            today=date(2026, 8, 23), sleep_s=0)
    assert calls == [(date.fromisoformat(mod.NYFED_FULL_START), date(2026, 8, 23))]


def test_run_dry_run_fetches_but_never_writes(tmp_path):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows())
    mtime = master.stat().st_mtime_ns
    stats = mod.run(series=['DGS10'], full=True, master_path=master,
                    fetch_fred=_fred_stub(), fetch_nyfed=_nyfed_stub(),
                    today=date(2026, 8, 23), sleep_s=0, dry_run=True)
    assert stats['rc'] == 0 and stats['rows_submitted'] == 8 and stats['rows_written'] == 0
    assert master.stat().st_mtime_ns == mtime


def test_main_prints_counters_and_honours_series_override(tmp_path, monkeypatch, capsys):
    master = _seed_master(tmp_path / 'macro.parquet', _vix_rows())
    monkeypatch.setattr(mod, 'MACRO_PATH', master)
    monkeypatch.setattr(mod, 'fetch_fred_csv', _fred_stub())
    monkeypatch.setattr(mod, 'fetch_nyfed_rates', _nyfed_stub())
    monkeypatch.setattr(mod, 'SLEEP_BETWEEN_CALLS_S', 0)
    rc = mod.main(['--full', '--series', 'DGS10,DGS2'])
    out = capsys.readouterr().out
    assert rc == 0
    assert 'series_ok=7' in out and 'series_failed=0' in out
    assert 'rows_written=11' in out
    assert 'DGS10=2026-08-20' in out
    assert set(pd.read_parquet(master)['series']) >= {'DGS10', 'DGS2', 'NYFED_SOFR'}


def test_fred_series_constant_covers_the_requested_ids_and_indpro():
    wanted = {'DGS1MO', 'DGS3MO', 'DGS6MO', 'DGS1', 'DGS2', 'DGS5', 'DGS10', 'DGS30', 'DTB3',
              'T10Y2Y', 'T10Y3M', 'DFF', 'SOFR', 'BAMLH0A0HYM2', 'BAMLC0A0CM', 'DTWEXBGS',
              'DCOILWTICO', 'DEXUSEU', 'DEXJPUS', 'INDPRO'}
    assert wanted <= set(mod.FRED_SERIES)
    assert len(mod.FRED_SERIES) == len(set(mod.FRED_SERIES))
