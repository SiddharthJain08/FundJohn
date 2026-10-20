import datetime as dt

import pandas as pd
import pytest

from lib import trading_calendar as tc
from strategies import resample


@pytest.fixture(autouse=True)
def cal(tmp_path, monkeypatch):
    off = {dt.date(2026, 7, 3), dt.date(2026, 7, 31)}
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'session_open': '0400',
             'session_close': '2000', 'settlement_date': None, 'active': d.date() not in off,
             'source': 'alpaca', 'fetched_at': '2026-09-04T00:00:00Z'}
            for d in pd.bdate_range('2026-04-01', '2026-09-30')]
    p = tmp_path / 'trading_calendar.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda s, e: None)
    tc.clear_cache()
    yield
    tc.clear_cache()


def _wide(end='2026-09-30', col='AAA'):
    idx = pd.bdate_range('2026-04-01', end)
    return pd.DataFrame({col: range(len(idx))}, index=idx, dtype=float)


def test_month_label_skips_month_end_holiday():
    out = resample.session_bars(_wide(), 'M')
    assert pd.Timestamp('2026-07-30') in out.index
    assert pd.Timestamp('2026-07-31') not in out.index
    assert out.index.name == 'date'
    assert out.loc['2026-07-30', 'AAA'] == _wide().loc['2026-07-30', 'AAA']


def test_week_label_skips_friday_holiday():
    out = resample.session_bars(_wide(), 'W')
    assert pd.Timestamp('2026-07-02') in out.index
    assert pd.Timestamp('2026-07-03') not in out.index


def test_quarter_labels():
    out = resample.session_bars(_wide(), 'Q')
    assert list(out.index) == [pd.Timestamp('2026-06-30'), pd.Timestamp('2026-09-30')]


def test_values_are_last_non_nan_in_bucket():
    w = _wide()
    w.loc['2026-05-29', 'AAA'] = float('nan')   # last May session
    out = resample.session_bars(w, 'M')
    assert out.loc['2026-05-29', 'AAA'] == w.loc['2026-05-28', 'AAA']


def test_partial_bucket_label_never_in_future_and_no_weekend_leak():
    w = _wide(end='2026-09-16', col='AAA')
    w = w.reindex(w.index.union([pd.Timestamp('2026-09-12')]))
    w['ZZCRYPTO1'] = 1.0
    w.loc['2026-09-11', 'ZZCRYPTO1'] = 11.0
    w.loc['2026-09-12', 'ZZCRYPTO1'] = 99.0
    m = resample.session_bars(w, 'M')
    assert m.index.max() == pd.Timestamp('2026-09-16')
    assert (m.index <= w.index.max()).all()
    wk = resample.session_bars(w, 'W')
    assert wk.loc['2026-09-11', 'ZZCRYPTO1'] == 11.0
    assert pd.Timestamp('2026-09-12') not in wk.index
    assert 99.0 not in wk['ZZCRYPTO1'].tolist()


def test_fill_prev_vs_none():
    w = _wide()
    w['B'] = float('nan')
    w.loc['2026-04-30', 'B'] = 5.0
    none = resample.session_bars(w, 'M', fill='none')
    prev = resample.session_bars(w, 'M', fill='prev')
    assert none['B'].isna().sum() == len(none) - 1
    assert (prev['B'] == 5.0).all()


def test_bad_arguments_raise():
    w = _wide()
    with pytest.raises(ValueError):
        resample.session_bars(w, 'D')
    with pytest.raises(ValueError):
        resample.session_bars(w, 'M', label='first_session')
    with pytest.raises(ValueError):
        resample.session_bars(w, 'M', fill='bfill')
    with pytest.raises(ValueError):
        resample.session_bars(w.reset_index(drop=True), 'M')


def test_reexports():
    assert resample.next_session is tc.next_session
    assert resample.prev_session is tc.prev_session
