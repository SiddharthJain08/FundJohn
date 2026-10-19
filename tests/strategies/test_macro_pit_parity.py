"""D1 parity: backtest macro slice vs live engine.load_aux_data macro dict.

Synthetic frames only; engine.ROOT -> tmp dir; needed_kinds={'macro'} keeps
Postgres untouched."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies import aux_data_loader as adl

T = '2026-10-15'


def _rows(series, dates, v=1.0):
    return [(d, series, v + i) for i, d in enumerate(dates)]


# Strict after_close: bar T sees obs <= T - lag - 1 (VIX lag 0 -> 10-14; DGS10 lag 1 -> 10-13).
_PUB = (_rows('VIX', pd.date_range('2026-10-01', '2026-10-14').strftime('%Y-%m-%d'), 15.0)
        + _rows('DGS10', pd.date_range('2026-10-01', '2026-10-13').strftime('%Y-%m-%d'), 4.0)
        + _rows('INDPRO', ['2026-07-01', '2026-08-01'], 100.0))
_UNPUB = [('2026-10-14', 'DGS10', 4.8), ('2026-10-15', 'DGS10', 4.9),
          ('2026-10-15', 'VIX', 20.0), ('2026-09-01', 'INDPRO', 103.0)]
PUB = pd.DataFrame(_PUB, columns=['date', 'series', 'value'])
FULL = pd.DataFrame(_PUB + _UNPUB, columns=['date', 'series', 'value'])


@pytest.fixture()
def env(monkeypatch, tmp_path):
    (tmp_path / 'full').mkdir()
    (tmp_path / 'pub').mkdir()
    FULL.to_parquet(tmp_path / 'full' / 'macro.parquet', index=False)
    PUB.to_parquet(tmp_path / 'pub' / 'macro.parquet', index=False)
    from execution import engine
    monkeypatch.setattr(adl, '_AVAIL_FRAMES', {})
    monkeypatch.setattr(adl, '_MACRO_PIT_ANNOUNCED', False)

    def run(adl_frame, engine_frame, flag):
        monkeypatch.setenv('OPENCLAW_MACRO_PIT', flag)
        monkeypatch.setattr(adl, 'MACRO_PATH', tmp_path / adl_frame / 'macro.parquet')
        adl._reset_macro_caches()
        d = tmp_path / f'eng_{engine_frame}'
        (d / 'data' / 'master').mkdir(parents=True, exist_ok=True)
        src = tmp_path / engine_frame / 'macro.parquet'
        (d / 'data' / 'master' / 'macro.parquet').write_bytes(src.read_bytes())
        monkeypatch.setattr(engine, 'ROOT', d)
        live = engine.load_aux_data(['ZZTEST1'], as_of=T, needed_kinds={'macro'}).get('macro', {})
        return adl._macro_slice(T), live
    return run


def _same(bt, live):
    assert set(bt) == set(live)
    for k in bt:
        pd.testing.assert_series_equal(bt[k], live[k], check_names=True, check_freq=False)


def test_flag_on_backtest_on_full_equals_live_on_published(env):
    bt, live = env('full', 'pub', '1')
    _same(bt, live)


def test_flag_off_backtest_equals_live_when_everything_published(env):
    bt, live = env('pub', 'pub', '0')
    _same(bt, live)


def test_flag_off_on_full_leaks_unpublished_rows(env):
    bt, _ = env('full', 'pub', '0')
    assert pd.Timestamp('2026-09-01') in bt['INDPRO'].index


def test_live_engine_ignores_the_flag(env):
    _, live0 = env('full', 'full', '0')
    _, live1 = env('full', 'full', '1')
    _same(live0, live1)
