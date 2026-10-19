"""A3: trailing-window load_prices behind OPENCLAW_PRICES_TRAILING_WINDOW."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from execution import engine
from execution.engine import load_prices, trailing_window_days

UNI = ['ZZTEST1', 'ZZTEST2', 'ZZTEST3']


def _today():
    return pd.Timestamp.today().normalize()


def _write(tmp_path, with_outlier=False):
    end = _today()
    dates = pd.bdate_range(end=end, periods=6 * 252)
    rows = []
    for i, d in enumerate(dates):
        for k, t in enumerate(UNI):
            rows.append({'ticker': t, 'date': d.strftime('%Y-%m-%d'),
                         'close': 50.0 + i * 0.01 + k})
    if with_outlier:
        # ZZTEST9 trades alone on one date inside and one outside the window,
        # on weekend dates no universe ticker trades on.
        inside = end - pd.Timedelta(days=400)
        outside = end - pd.Timedelta(days=2000)
        for d in (inside, outside):
            while d.weekday() < 5:
                d -= pd.Timedelta(days=1)
            rows.append({'ticker': 'ZZTEST9', 'date': d.strftime('%Y-%m-%d'),
                         'close': 9.0})
    df = pd.DataFrame(rows)
    master = tmp_path / 'data' / 'master'
    master.mkdir(parents=True)
    df.to_parquet(master / 'prices.parquet', index=False)
    return master / 'prices.parquet'


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'ROOT', tmp_path, raising=False)
    monkeypatch.delenv('OPENCLAW_CLOSE_PROXY_SNAPSHOT', raising=False)
    monkeypatch.delenv('OPENCLAW_PRICES_TRAILING_WINDOW', raising=False)
    return tmp_path


def _old_path(path, universe):
    df = pd.read_parquet(path, columns=['ticker', 'date', 'close'])
    wide = df.pivot(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide.sort_index(inplace=True)
    cols = [c for c in universe if c in wide.columns]
    if cols:
        wide = wide[cols]
    return wide


def test_flag_unset_is_byte_identical(env):
    p = _write(env)
    assert trailing_window_days([SimpleNamespace(max_lookback_days=5000)]) is None
    old = _old_path(p, UNI)
    pd.testing.assert_frame_equal(load_prices(UNI), old, check_freq=False, check_names=False)
    pd.testing.assert_frame_equal(load_prices(UNI, lookback_days=None), old,
                                  check_freq=False, check_names=False)


def test_trailing_window_cuts_rows(env):
    _write(env)
    full = load_prices(UNI)
    w = load_prices(UNI, lookback_days=1095)
    assert w.index.min() >= _today() - pd.Timedelta(days=1095)
    assert len(w) < len(full)
    pd.testing.assert_frame_equal(w, full.loc[w.index], check_freq=False, check_names=False)


def test_trailing_window_axis_is_windowed(env):
    p = _write(env, with_outlier=True)
    end = _today()
    inside = pd.read_parquet(p)
    inside = inside[inside.ticker == 'ZZTEST9']['date'].sort_values()
    out_d, in_d = pd.Timestamp(inside.iloc[0]), pd.Timestamp(inside.iloc[1])
    w = load_prices(UNI, lookback_days=1095)
    assert in_d in w.index and w.loc[in_d].isna().all()
    assert out_d not in w.index
    assert w.index.min() >= end - pd.Timedelta(days=1095)
    assert load_prices.last_parquet_max_date == pd.Timestamp(pd.read_parquet(p)['date'].max()).date()


def test_window_days_is_max_of_declared_and_floor(monkeypatch):
    monkeypatch.setenv('OPENCLAW_PRICES_TRAILING_WINDOW', '1')
    mk = lambda n: SimpleNamespace(max_lookback_days=n)
    assert trailing_window_days([mk(0), mk(400), mk(2000)]) == 2000
    assert trailing_window_days([mk(0), mk(0)]) == 1095
    assert trailing_window_days([SimpleNamespace()]) == 1095


def test_prices_window_log_line(env, caplog):
    _write(env)
    with caplog.at_level(logging.INFO):
        load_prices(UNI)
        recs = [r for r in caplog.records if '[prices_window]' in r.getMessage()]
        assert len(recs) == 1 and 'mode=full' in recs[0].getMessage()
        caplog.clear()
        load_prices(UNI, lookback_days=1095)
        recs = [r for r in caplog.records if '[prices_window]' in r.getMessage()]
        assert len(recs) == 1
        assert 'mode=trailing days=1095 cutoff=' in recs[0].getMessage()
