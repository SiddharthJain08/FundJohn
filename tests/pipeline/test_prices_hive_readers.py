"""A7: the four heavy prices readers agree file-vs-hive (tmp_path only; never the real master)."""
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
sys.path.insert(0, str(ROOT / 'scripts'))

from src.data import parquet_store as ps  # noqa: E402
from execution import engine, asset_correlation as ac  # noqa: E402
from backtest import unified_backtest as ub, backtest_panel as bp  # noqa: E402
import src.pipeline.quarantine_filter as qf  # noqa: E402
from build_prices_hive import build  # noqa: E402

UNI = ['ZZTEST1', 'ZZTEST2', 'ZZTEST3']


def _write_master(path, date_major=False):
    dates = pd.bdate_range('2023-06-01', '2026-01-30')
    rng = np.random.default_rng(1)
    rows = []
    for t in UNI + ['ZZTEST9']:
        for d in dates:
            b = 50 + rng.random() * 10
            rows.append((t, d.strftime('%Y-%m-%d'), b, b + 1, b - 1, b + .3, 100))
    if date_major:
        rows.sort(key=lambda r: (r[1], r[0]))   # (date, ticker): repeated leading dates
    else:
        rng.shuffle(rows)
    df = pd.DataFrame(rows, columns=['ticker', 'date', 'open', 'high', 'low', 'close', 'volume'])
    pq.write_table(pa.Table.from_pandas(df, preserve_index=False), str(path),
                   row_group_size=500, use_dictionary=['ticker', 'date'])


@pytest.fixture(params=['shuffled', 'date_major'])
def env(tmp_path, monkeypatch, request):
    master = tmp_path / 'data' / 'master'
    master.mkdir(parents=True)
    single = master / 'prices.parquet'
    hive = master / 'prices'
    _write_master(single, date_major=request.param == 'date_major')
    build(single, hive, years=None, apply=True, spill_dir=tmp_path / 'spill')
    assert len(list(hive.glob('year=*/part-0.parquet'))) == 4
    monkeypatch.setattr(ps, 'PRICES_PATH', single)
    monkeypatch.setattr(ps, 'PRICES_HIVE_DIR', hive)
    monkeypatch.setattr(engine, 'ROOT', tmp_path, raising=False)
    monkeypatch.setattr(ub, 'PRICES_PARQUET', single)
    monkeypatch.setattr(bp, 'PRICES_PARQUET', single)
    monkeypatch.setattr(ac, 'PARQUET', str(single))
    monkeypatch.setattr(qf, '_cached', lambda master_table: set())
    monkeypatch.delenv('OPENCLAW_PRICES_HIVE', raising=False)
    monkeypatch.delenv('OPENCLAW_CLOSE_PROXY_SNAPSHOT', raising=False)
    monkeypatch.delenv('OPENCLAW_PRICES_TRAILING_WINDOW', raising=False)
    for p in (single, hive, ps.PRICES_PATH, ps.PRICES_HIVE_DIR):
        assert str(p).startswith(str(tmp_path))
    return single, hive


def _both(monkeypatch, fn):
    monkeypatch.delenv('OPENCLAW_PRICES_HIVE', raising=False)
    a = fn()
    monkeypatch.setenv('OPENCLAW_PRICES_HIVE', '1')
    b = fn()
    return a, b


def test_engine_load_prices_hive_equals_file(env, monkeypatch):
    def run():
        w = engine.load_prices(UNI)
        return w, engine.load_prices.last_parquet_max_date
    (wa, ma), (wb, mb) = _both(monkeypatch, run)
    assert len(wa) > 500
    pd.testing.assert_frame_equal(wa, wb)
    assert ma == mb
    # trailing-window path (date_axis + cutoff filter) too
    (wa, ma), (wb, mb) = _both(monkeypatch, lambda: (
        engine.load_prices(UNI, lookback_days=400, as_of='2026-01-30'),
        engine.load_prices.last_parquet_max_date))
    pd.testing.assert_frame_equal(wa, wb)
    assert ma == mb and wa.index.min() >= pd.Timestamp('2024-12-01')


@pytest.mark.parametrize('tickers', [None, ['ZZTEST2', 'ZZTEST1']])
def test_unified_panels_hive_equals_file(env, monkeypatch, tickers):
    (ca, ba), (cb, bb) = _both(monkeypatch, lambda: ub.load_prices_panels(tickers=tickers))
    pd.testing.assert_frame_equal(ca, cb)
    assert list(ca.columns) == list(cb.columns)
    assert list(ba) == list(bb)
    for k in ba:
        pd.testing.assert_frame_equal(ba[k], bb[k])


def test_backtest_panel_and_asset_corr_hive_equals_file(env, monkeypatch):
    a, b = _both(monkeypatch, lambda: bp.load_prices(['ZZTEST1'])
                 .sort_values(['ticker', 'date']).reset_index(drop=True))
    assert len(a) > 100
    pd.testing.assert_frame_equal(a, b)
    ra, rb = _both(monkeypatch, lambda: ac._load_returns(['ZZTEST1', 'ZZTEST2'], 20,
                                                         as_of='2026-01-30'))
    assert len(ra['ZZTEST1']) == 20
    assert ra == rb


def test_prices_source_log_and_empty_fallback(env, monkeypatch, capsys, caplog):
    single, hive = env
    caplog.set_level(logging.INFO)
    monkeypatch.delenv('OPENCLAW_PRICES_HIVE', raising=False)
    ps.prices_read_table(['ticker'], path=single)
    out = capsys.readouterr()
    assert '[prices_source]' not in out.err + out.out + caplog.text
    monkeypatch.setenv('OPENCLAW_PRICES_HIVE', '1')
    ps.prices_read_table(['ticker'], path=single)
    assert 'layout=hive' in capsys.readouterr().err
    # an in-flight writer's .tmp / .lock beside committed parts is never read
    (hive / 'year=2026' / 'part-0.parquet.tmp').write_bytes(b'garbage')
    (hive / 'year=2026' / 'part-0.parquet.lock').write_bytes(b'')
    assert ps.prices_read_table(['ticker']).num_rows == pq.read_metadata(single).num_rows
    capsys.readouterr()
    (hive / 'year=2026' / 'part-0.parquet.tmp').unlink()
    for f in hive.glob('year=*/part-0.parquet'):
        f.rename(f.with_name('part-0.parquet.tmp'))   # only .tmp present = empty tree
    t = ps.prices_read_table(['ticker'], path=single)
    err = capsys.readouterr().err
    assert 'layout=file reason=hive_empty' in err and t.num_rows == pq.read_metadata(single).num_rows


def test_path_override_logged_while_flag_on(env, monkeypatch, capsys, tmp_path):
    single, hive = env
    other = tmp_path / 'other.parquet'
    pq.write_table(pa.table({'ticker': ['ZZTEST1']}), other)
    monkeypatch.delenv('OPENCLAW_PRICES_HIVE', raising=False)
    ps.prices_read_table(['ticker'], path=other)
    cap = capsys.readouterr()
    assert '[prices_source]' not in cap.err + cap.out
    monkeypatch.setenv('OPENCLAW_PRICES_HIVE', '1')
    for _ in range(2):   # one line per call
        assert ps.prices_read_table(['ticker'], path=other).num_rows == 1
    err = capsys.readouterr().err
    assert err.count('[prices_source] layout=file reason=path_override') == 2
