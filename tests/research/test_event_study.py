import datetime as dt
import json

import numpy as np
import pandas as pd
import pytest

from lib import trading_calendar as tc
from research import event_study as es


@pytest.fixture(autouse=True)
def cal(tmp_path, monkeypatch):
    rows = [{'date': d.date(), 'open': '09:30', 'close': '16:00', 'session_open': '0400',
             'session_close': '2000', 'settlement_date': None, 'active': True,
             'source': 'alpaca', 'fetched_at': '2026-09-04T00:00:00Z'}
            for d in pd.bdate_range('2026-01-01', '2026-12-31')]
    p = tmp_path / 'trading_calendar.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv(tc.MASTER_PATH_ENV, str(p))
    monkeypatch.setattr(tc, '_alpaca_sessions', lambda s, e: None)
    tc.clear_cache()
    yield p
    tc.clear_cache()


IDX = pd.bdate_range('2026-03-02', '2026-04-30')


def _wide():
    n = len(IDX)
    return pd.DataFrame({
        'ZZTEST1': 100 * 1.01 ** np.arange(n),
        'ZZTEST2': 50 * 0.99 ** np.arange(n),
        'ZZTEST3': np.full(n, 20.0),
    }, index=IDX)


def _ev(rows):
    return pd.DataFrame(rows, columns=['ticker', 'event_date'])


def test_markout_hand_computed():
    ev = _ev([('ZZTEST1', '2026-03-16'), ('ZZTEST2', '2026-03-16'), ('ZZTEST3', '2026-03-16')])
    out = es.markout(ev, _wide(), offsets=range(-2, 4))
    assert list(out.columns) == ['offset', 'mean_ret', 'median_ret', 'n']
    assert list(out['offset']) == [-2, -1, 0, 1, 2, 3]
    assert (out['n'] == 3).all()
    for _, r in out.iterrows():
        k = int(r['offset'])
        rets = [1.01 ** k - 1, 0.99 ** k - 1, 0.0]
        assert r['mean_ret'] == pytest.approx(np.mean(rets))
        assert r['median_ret'] == pytest.approx(np.median(rets))
    assert out.loc[out['offset'] == 0, 'mean_ret'].iloc[0] == 0.0


def test_weekend_event_anchors_to_prior_session():
    sat = es.markout(_ev([('ZZTEST1', '2026-03-14')]), _wide(), offsets=[0, 1])
    fri = es.markout(_ev([('ZZTEST1', '2026-03-13')]), _wide(), offsets=[0, 1])
    assert sat['mean_ret'].iloc[1] == pytest.approx(0.01)
    pd.testing.assert_frame_equal(sat, fri)


def test_offsets_beyond_panel_reduce_n():
    ev = _ev([('ZZTEST1', '2026-03-10'), ('ZZTEST1', '2026-04-28')])
    out = es.markout(ev, _wide(), offsets=[0, 1, 2, 3]).set_index('offset')
    assert list(out['n']) == [2, 2, 2, 1]
    nan = es.markout(_ev([('ZZTEST1', '2026-04-30')]), _wide(), offsets=[0, 5])
    assert nan['n'].iloc[1] == 0 and np.isnan(nan['mean_ret'].iloc[1]) and np.isnan(nan['median_ret'].iloc[1])


def test_duplicates_and_unpriced_events():
    ev = _ev([('ZZTEST1', '2026-03-16'), ('ZZTEST1', '2026-03-16'),
              ('ZZTEST1', '2026-03-15'),  # Sunday -> Fri 03-13
              ('ZZMISSING', '2026-03-16'), ('ZZTEST2', '2026-03-16')])
    out = es.markout(ev, _wide(), offsets=[1])
    assert out['n'].iloc[0] == 3  # (1,03-16) once, (1,03-13) once, (2,03-16)
    ev2 = _ev([('ZZTEST1', '2026-03-16'), ('ZZTEST1', '2026-03-14'), ('ZZTEST1', '2026-03-15')])
    assert es.markout(ev2, _wide(), offsets=[1])['n'].iloc[0] == 2  # Sat+Sun share an anchor


def test_string_and_datetime_event_dates_equivalent():
    a = es.markout(_ev([('ZZTEST1', '2026-03-16')]), _wide(), offsets=range(-1, 3))
    b = es.markout(_ev([('ZZTEST1', dt.datetime(2026, 3, 16, 17, 30))]), _wide(), offsets=range(-1, 3))
    c = es.markout(_ev([('ZZTEST1', dt.date(2026, 3, 16))]), _wide(), offsets=range(-1, 3))
    pd.testing.assert_frame_equal(a, b)
    pd.testing.assert_frame_equal(a, c)


def test_cli_on_tmp_parquets(tmp_path, capsys, cal):
    w = _wide()
    long = (w.rename_axis('date').reset_index().melt(id_vars='date', var_name='ticker', value_name='close'))
    long['date'] = long['date'].dt.strftime('%Y-%m-%d')
    pp = tmp_path / 'prices.parquet'
    long.to_parquet(pp, index=False)
    ep = tmp_path / 'earnings.parquet'
    pd.DataFrame({'ticker': ['ZZTEST1', 'ZZTEST2', 'ZZMISSING'],
                  'report_date': ['2026-03-16', '2026-03-17', '2026-03-17']}).to_parquet(ep, index=False)
    base = ['--master', 'earnings', '--since', '2026-03-01', '--events-path', str(ep),
            '--prices-path', str(pp), '--calendar-path', str(cal)]
    assert es.main(base) == 0
    cap = capsys.readouterr()
    assert cap.err.startswith('[event_study]')
    j = json.loads(cap.out)
    assert len(j['rows']) == 26 and j['events'] == 3 and j['master'] == 'earnings'
    assert [r['n'] for r in j['rows'] if r['offset'] == 1] == [2]
    assert es.main(base + ['--offsets=-2:3']) == 0
    assert len(json.loads(capsys.readouterr().out)['rows']) == 6


def test_event_after_panel_dropped(capsys):
    out = es.markout(_ev([('ZZTEST1', '2026-06-01')]), _wide(), offsets=range(-2, 3))
    assert (out['n'] == 0).all() and out['mean_ret'].isna().all()
    assert 'dropped_after_panel=1' in capsys.readouterr().err
    out2 = es.markout(_ev([('ZZTEST1', '2026-06-01'), ('ZZTEST1', '2026-03-16')]), _wide(), offsets=[0, 1])
    assert list(out2['n']) == [1, 1]


def _cli_files(tmp_path, ev_name, ev_df, dup_prices=False):
    w = _wide()
    long = w.rename_axis('date').reset_index().melt(id_vars='date', var_name='ticker', value_name='close')
    long['date'] = long['date'].dt.strftime('%Y-%m-%d')
    if dup_prices:
        long = pd.concat([long, long])
    pp = tmp_path / 'prices.parquet'
    long.to_parquet(pp, index=False)
    ep = tmp_path / ev_name
    ev_df.to_parquet(ep, index=False)
    return ep, pp


def _args(master, since, ep, pp, cal):
    return ['--master', master, '--since', since, '--events-path', str(ep),
            '--prices-path', str(pp), '--calendar-path', str(cal), '--offsets=0:1']


def test_cli_missing_date_column_exit_2(tmp_path, capsys, cal):
    ep, pp = _cli_files(tmp_path, 'earnings.parquet', pd.DataFrame({'ticker': ['ZZTEST1'], 'x': ['2026-03-16']}))
    assert es.main(_args('earnings', '2026-03-01', ep, pp, cal)) == 2
    assert 'no event date column' in capsys.readouterr().err


def test_cli_insider_since_and_dup_prices(tmp_path, capsys, cal):
    ev = pd.DataFrame({'ticker': ['ZZTEST1', 'ZZTEST2'], 'date': ['2026-03-05', '2026-03-16']})
    ep, pp = _cli_files(tmp_path, 'insider.parquet', ev, dup_prices=True)
    assert es.main(_args('insider', '2026-03-10', ep, pp, cal)) == 0
    j = json.loads(capsys.readouterr().out)
    assert j['events'] == 1 and [r['n'] for r in j['rows']] == [1, 1]
    assert j['rows'][1]['mean_ret'] == pytest.approx(0.99 - 1)


def test_cli_drops_event_after_panel(tmp_path, capsys, cal):
    ev = pd.DataFrame({'ticker': ['ZZTEST1'], 'report_date': ['2026-06-01']})
    ep, pp = _cli_files(tmp_path, 'earnings.parquet', ev)
    assert es.main(_args('earnings', '2026-03-01', ep, pp, cal)) == 0
    cap = capsys.readouterr()
    assert 'dropped_after_panel=1' in cap.err
    assert [r['n'] for r in json.loads(cap.out)['rows']] == [0, 0]
