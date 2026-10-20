"""A6 task 9: every options_eod reader + freshness consumer goes through the resolver.

All data is tiny tmp parquet; the real masters are never touched.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from src.data import parquet_store as ps  # noqa: E402

TODAY = pd.Timestamp('2026-10-20')
COLS14 = ['ticker', 'date', 'expiry', 'strike', 'option_type', 'implied_volatility',
          'delta', 'gamma', 'theta', 'vega', 'open_interest', 'volume', 'open', 'close']


def _frame(dates, tickers=('ZZTEST1', 'ZZTEST2')):
    rows = []
    for d in dates:
        for t in tickers:
            for ot, dl in (('call', 0.5), ('put', -0.5)):
                rows.append({'ticker': t, 'date': d, 'expiry': '2026-11-20',
                             'strike': 100.0, 'option_type': ot,
                             'implied_volatility': 0.3, 'delta': dl, 'gamma': 0.01,
                             'theta': -0.02, 'vega': 0.1, 'open_interest': 5.0,
                             'volume': 3.0, 'open': 1.0, 'close': 1.1,
                             'market_price': 1.0})
    df = pd.DataFrame(rows)
    # object (not pandas-3 `str`) so parquet pandas-metadata matches the live files
    return df.astype({c: object for c in ('ticker', 'date', 'expiry', 'option_type')})


def _write_single(path, df):
    df.to_parquet(path, index=False)


def _write_months(root, df):
    root.mkdir(parents=True, exist_ok=True)
    for ym, g in df.groupby(df['date'].astype(str).str[:7]):
        g.reset_index(drop=True).to_parquet(root / f'{ym}.parquet', index=False)


REAL_MASTER = '/root/openclaw/data/master'


_PATH_CONSTS = ('PRICES_PATH', 'OPTIONS_PATH', 'OPTIONS_DIR', 'MACRO_PATH',
                'INSIDER_PATH', 'FUNDAMENTALS_PATH')


@pytest.fixture(autouse=True)
def _no_real_master(tmp_path_factory, monkeypatch):
    """No ps.*_PATH / options_path() / PRICES_PATH may resolve under the real master.

    Setup repoints every path constant at a non-existent tmp location (a test that
    forgets to patch one errors on a missing file instead of scanning the real
    master). Teardown runs BEFORE monkeypatch's undo (monkeypatch is set up first),
    so it sees the test's final values and fails loudly if any is real again."""
    dead = tmp_path_factory.mktemp('unpatched')
    for n in _PATH_CONSTS:
        monkeypatch.setattr(ps, n, dead / n)
    yield
    for env in (None, '1'):
        if env is None:
            monkeypatch.delenv('OPENCLAW_OPTIONS_MONTHLY', raising=False)
        else:
            monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', env)
        probes = {n: getattr(ps, n) for n in _PATH_CONSTS}
        probes['options_path()'] = ps.options_path()
        bad = {k: str(v) for k, v in probes.items() if str(v).startswith(REAL_MASTER)}
        assert not bad, f'test left real-master paths live: {bad}'


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, 'OPTIONS_PATH', tmp_path / 'options_eod.parquet')
    monkeypatch.setattr(ps, 'OPTIONS_DIR', tmp_path / 'options_eod')
    monkeypatch.delenv('OPENCLAW_OPTIONS_MONTHLY', raising=False)
    return tmp_path


@pytest.fixture
def both_layouts(store):
    df = _frame(['2026-09-28', '2026-10-08', '2026-10-19'])
    _write_single(ps.OPTIONS_PATH, df)
    _write_months(ps.OPTIONS_DIR, df)
    return df


# ── freshness consumers (Review Focus 2) ────────────────────────────────────

def test_freshness_consumers_follow_monthly_dir(store, monkeypatch):
    from maintenance import doctor
    from src.system_checks.checks import master_freshness as chk
    from src.system_checks.types import Status

    master = store / 'data' / 'master'
    master.mkdir(parents=True)
    _write_single(master / 'options_eod.parquet', _frame(['2026-09-01']))   # frozen
    fresh = (date.today() - timedelta(days=1)).isoformat()
    _write_months(master / 'options_eod', _frame(['2026-09-01', fresh]))
    monkeypatch.setattr(doctor, 'ROOT', store)
    monkeypatch.setattr(chk, '_MASTER_DIR', master)

    # flag off: the frozen single file is judged -> stale
    assert doctor._check_options_archive_freshness()['severity'] == doctor.FAIL
    status, detail = chk._master_freshness()
    assert status is Status.FAIL and 'options_eod.parquet' in detail

    # flag on: the monthly directory is judged -> fresh
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    assert doctor._check_options_archive_freshness()['severity'] == doctor.PASS
    status, detail = chk._master_freshness()
    assert status is not Status.FAIL
    assert 'options_eod.parquet' not in detail or 'stale' not in detail


def test_doctor_last_date_accepts_directory(store):
    from maintenance import doctor
    _write_months(ps.OPTIONS_DIR, _frame(['2026-09-28', '2026-10-19']))
    assert doctor._parquet_last_date(str(ps.OPTIONS_DIR)) == date(2026, 10, 19)


# ── engine ──────────────────────────────────────────────────────────────────

def test_engine_options_window_reads_dir(both_layouts, monkeypatch):
    from execution import engine
    off = engine._load_options_window(ps.options_path(), COLS14, 30, TODAY)
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    calls = []
    real = ps.options_read_table
    monkeypatch.setattr(ps, 'options_read_table',
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    on = engine._load_options_window(ps.options_path(), COLS14, 30, TODAY)
    assert calls, 'engine window read must go through ps.options_read_table'
    assert len(off) == 12   # 3 dates x 2 tickers x call/put
    keys = ['ticker', 'date', 'option_type']
    pd.testing.assert_frame_equal(
        off.sort_values(keys).reset_index(drop=True),
        on.sort_values(keys).reset_index(drop=True), check_dtype=False)
    assert sorted(on['date'].dt.strftime('%Y-%m-%d').unique()) == \
        ['2026-09-28', '2026-10-08', '2026-10-19']


def test_engine_aux_loader_uses_monthly_dir(store, monkeypatch):
    """load_aux's `opts_path` must be the directory when the flag is on (source check:
    the aux loader is too heavy to drive end-to-end on tmp data)."""
    src = (ROOT / 'src' / 'execution' / 'engine.py').read_text()
    assert "opts_path = master_dir / 'options_eod.parquet'\n" not in src
    assert 'options_path()' in src


# ── parquet_reader CLI ops ──────────────────────────────────────────────────

def test_parquet_reader_ops_dir_aware(store, monkeypatch):
    from src.data import parquet_reader as pr
    # frozen single file stops at 10-19; the monthly dir carries one newer ZZTEST1 day
    base = _frame(['2026-09-28', '2026-10-08', '2026-10-19'])
    _write_single(ps.OPTIONS_PATH, base)
    _write_months(ps.OPTIONS_DIR, pd.concat([base, _frame(['2026-10-20'], ('ZZTEST1',))]))
    monkeypatch.setattr(ps, 'PRICES_PATH', store / 'prices.parquet')
    monkeypatch.setattr(ps, 'MACRO_PATH', store / 'macro.parquet')
    monkeypatch.setattr(ps, 'INSIDER_PATH', store / 'insider.parquet')
    monkeypatch.setattr(ps, 'FUNDAMENTALS_PATH', store / 'financials.parquet')
    pd.DataFrame({'ticker': ['ZZTEST1'], 'date': ['2026-10-19'], 'close': [1.0]}
                 ).to_parquet(ps.PRICES_PATH)
    pd.DataFrame({'series': ['ZZ'], 'date': ['2026-10-19'], 'value': [1.0]}
                 ).to_parquet(ps.MACRO_PATH)
    pd.DataFrame({'ticker': ['ZZTEST1'], 'transaction_date': ['2026-10-19']}
                 ).to_parquet(ps.INSIDER_PATH)
    pd.DataFrame({'ticker': ['ZZTEST1'], 'date': ['2026-10-19']}
                 ).to_parquet(ps.FUNDAMENTALS_PATH)

    def probe():
        opts = pr.op_options({'ticker': 'ZZTEST1', 'limit': 100})
        fresh = pr.op_freshness_per_ticker({'tickers': ['ZZTEST1']})
        od = next(r for r in pr.op_freshness({}) if r['dataset'] == 'options_data')
        return (len(opts), str(fresh[0]['latest_options'])[:10],
                od['row_count'], str(od['max_date'])[:10])

    assert probe() == (6, '2026-10-19', 12, '2026-10-19')
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    assert probe() == (8, '2026-10-20', 14, '2026-10-20')


# ── ledger + backfiller stats (no full read) ────────────────────────────────

def test_sync_ledger_and_backfiller_stats_dir_aware(both_layouts, monkeypatch):
    from pipeline import backfillers
    from strategies import sync_data_ledger as led

    def boom(*a, **k):
        raise AssertionError('full pd.read_parquet is forbidden')
    monkeypatch.setattr(pd, 'read_parquet', boom)

    st = led._stats(ps.OPTIONS_DIR)
    assert st['row_count'] == 12 and st['ticker_count'] == 2
    assert st['min_date'] == '2026-09-28' and st['max_date'] == '2026-10-19'
    assert led._stats(ps.OPTIONS_PATH)['row_count'] == 12

    master = ps.OPTIONS_PATH.parent
    (master / 'data' / 'master').mkdir(parents=True)
    # backfillers resolve master/<stem> under OPENCLAW_DIR
    monkeypatch.setenv('OPENCLAW_DIR', str(master))
    (master / 'data').mkdir(exist_ok=True)
    _write_single(master / 'data' / 'master' / 'options_eod.parquet', _frame(['2026-09-28', '2026-10-08', '2026-10-19']))
    _write_months(master / 'data' / 'master' / 'options_eod', _frame(['2026-09-28', '2026-10-08', '2026-10-19']))
    off = backfillers._parquet_stats('options_eod')
    monkeypatch.setenv('OPENCLAW_OPTIONS_MONTHLY', '1')
    on = backfillers._parquet_stats('options_eod')
    assert off == on == {'min_date': '2026-09-28', 'max_date': '2026-10-19', 'row_count': 12}


# ── scripts (rows 7-11) ─────────────────────────────────────────────────────

def _load_script(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    def fake(columns, filters=None, read_dictionary=None):
        calls.append((list(columns), read_dictionary))
        return pa.table({c: pa.array([], type=pa.string()) for c in columns})
    monkeypatch.setattr(ps, 'options_read_table', fake)
    return calls


def test_scripts_use_resolver_aggregates(recorder):
    m = _load_script('boa_t', 'scripts/build_options_aggregates.py')
    m._read_range(pd.Timestamp('2026-10-01'), pd.Timestamp('2026-10-02'))
    assert recorder


def test_scripts_use_resolver_surface(recorder):
    m = _load_script('bos_t', 'scripts/build_options_surface.py')
    m._read_range(pd.Timestamp('2026-10-01'), pd.Timestamp('2026-10-02'))
    assert recorder and recorder[0][1] == ['ticker', 'option_type']


def test_scripts_use_resolver_iv_history(store, recorder, monkeypatch, tmp_path):
    from ingestion import ingest_iv_history as iv
    _write_single(ps.OPTIONS_PATH, _frame(['2026-10-19']))
    monkeypatch.setattr(iv, 'DATA_DIR', tmp_path / 'm')
    monkeypatch.setattr(iv, 'OUT_PATH', tmp_path / 'm' / 'iv_history.parquet')
    monkeypatch.setattr(sys, 'argv', ['x', '--rebuild', '--dry-run'])
    try:
        iv.main()
    except KeyError:  # empty fake table -> no rows to collapse; only the read matters
        pass
    assert recorder and 'implied_volatility' in recorder[0][0]


def test_scripts_use_resolver_parity(recorder):
    m = _load_script('opc_t', 'scripts/options_parity_check.py')
    monkeypatch_closes = lambda: {}  # noqa: E731
    m._load_underlying_closes = monkeypatch_closes
    try:
        m.run_iv_gate()
    except Exception:
        pass
    assert recorder and 'implied_volatility' in recorder[0][0]


def test_scripts_use_resolver_acting_ingest(store, recorder, monkeypatch):
    m = _load_script('rai_t', 'scripts/run_acting_ingest.py')
    # the recorder returns an empty table; path existence must come from the resolver
    monkeypatch.setattr(Path, 'exists', lambda self: True)
    out = m._master_coverage('options_eod', ['AAA'])
    assert recorder and recorder[0][0] == ['ticker']
    assert out['in_master'] == 0
