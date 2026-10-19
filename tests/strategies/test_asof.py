import pathlib

import numpy as np
import pandas as pd
import pytest

from strategies.asof import asof_align

D = pd.Timestamp


def _r(dates, vals, **kw):
    return pd.DataFrame({'available_at': pd.to_datetime(dates), 'v': vals, **kw})


def _vals(df):
    return df['v'].tolist()


def test_after_close_excludes_same_day():
    out = asof_align(_r(['2026-03-02'], [1.0]), pd.DatetimeIndex(['2026-03-02', '2026-03-03']))
    assert np.isnan(out['v'].iloc[0]) and out['v'].iloc[1] == 1.0


def test_same_bar_includes_same_day():
    out = asof_align(_r(['2026-03-02'], [1.0]), pd.DatetimeIndex(['2026-03-02', '2026-03-03']),
                     same_day='same_bar')
    assert _vals(out) == [1.0, 1.0]


def test_backward_latest_wins():
    out = asof_align(_r(['2026-03-02', '2026-03-04'], [1.0, 2.0]),
                     pd.DatetimeIndex(['2026-03-03', '2026-03-05']))
    assert _vals(out) == [1.0, 2.0]


def test_tolerance_expires_match():
    out = asof_align(_r(['2026-03-02'], [1.0]), pd.DatetimeIndex(['2026-03-04', '2026-03-12']),
                     tolerance=pd.Timedelta(days=5))
    assert out['v'].iloc[0] == 1.0 and np.isnan(out['v'].iloc[1])


def test_by_key_isolated():
    right = _r(['2026-03-02', '2026-03-02'], [1.0, 2.0], ticker=['ZZTEST1', 'ZZTEST2'])
    right = right.iloc[:1]  # only ZZTEST1 has data
    li = pd.MultiIndex.from_tuples(
        [('ZZTEST2', D('2026-03-05')), ('ZZTEST1', D('2026-03-05')), ('ZZTEST2', D('2026-03-03'))],
        names=['ticker', 'date'])
    out = asof_align(right, li, by='ticker')
    assert out.index.equals(li)
    assert np.isnan(out['v'].iloc[0]) and out['v'].iloc[1] == 1.0 and np.isnan(out['v'].iloc[2])


def test_unsorted_and_duplicate_left_index_preserved():
    li = pd.DatetimeIndex(['2026-03-05', '2026-03-03', '2026-03-05'], name='date')
    out = asof_align(_r(['2026-03-02', '2026-03-04'], [1.0, 2.0]), li)
    assert _vals(out) == [2.0, 1.0, 2.0] and out.index.equals(li) and out.index.name == 'date'


def test_nat_right_keys_dropped_nat_left_rows_nan():
    right = _r(['2026-03-02', None], [1.0, 9.0])
    li = pd.DatetimeIndex(['2026-03-03', None, '2026-03-04'])
    out = asof_align(right, li)
    assert out['v'].iloc[0] == 1.0 and np.isnan(out['v'].iloc[1]) and out['v'].iloc[2] == 1.0
    assert out.isna().iloc[1].all()


def test_tie_on_available_at_last_input_row_wins():
    out = asof_align(_r(['2026-03-02', '2026-03-02'], [1.0, 2.0]), pd.DatetimeIndex(['2026-03-03']))
    assert _vals(out) == [2.0]


def test_bad_same_day_raises():
    with pytest.raises(ValueError):
        asof_align(_r(['2026-03-02'], [1.0]), pd.DatetimeIndex(['2026-03-03']), same_day='x')
    with pytest.raises(KeyError):
        asof_align(_r(['2026-03-02'], [1.0]), pd.DatetimeIndex(['2026-03-03']), on='nope')


def test_tz_mismatch_raises_typeerror():
    right = _r(['2026-03-02'], [1.0])
    right['available_at'] = right['available_at'].dt.tz_localize('UTC')
    with pytest.raises(TypeError, match=r'datetime64.*UTC.*datetime64|UTC.*datetime64'):
        asof_align(right, pd.DatetimeIndex(['2026-03-03']))


def test_strategycoder_prompt_names_asof_helper():
    p = pathlib.Path(__file__).resolve().parents[2] / 'src/agent/prompts/subagents/strategycoder.md'
    t = p.read_text()
    assert 'strategies.asof' in t and 'unguarded_parquet_read' in t
