"""Spec 2026-08-30 §6 (D-6): daily book-vs-SPY realized line. Pure compute on
aligned daily closes; report-only, never gates."""
import json
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))

import pytest  # noqa: E402
from execution import bench_realized as br  # noqa: E402


def _series(start_nav, daily, n, first='2026-06-01'):
    import datetime as dt
    d0 = dt.date.fromisoformat(first); out = {}; v = start_nav
    for i in range(n):
        d = d0 + dt.timedelta(days=i)
        if d.weekday() < 5:
            out[d.isoformat()] = v
            v *= (1 + daily)
    return out


def _series_noisy(start, daily, n, first='2026-06-01'):
    """Like _series but the daily return alternates daily*1.5 / daily*0.5 so
    the trailing-20 std is non-zero (a constant return has no Sharpe)."""
    import datetime as dt
    d0 = dt.date.fromisoformat(first); out = {}; v = start; k = 0
    for i in range(n):
        d = d0 + dt.timedelta(days=i)
        if d.weekday() < 5:
            out[d.isoformat()] = v
            v *= (1 + daily * (1.5 if k % 2 == 0 else 0.5)); k += 1
    return out


def test_compute_returns_since_anchor_windows_and_sharpes():
    nav = _series(100_000.0, -0.002, 120)     # book bleeds 20 bp/day (constant)
    spy = _series(500.0, +0.001, 120)         # SPY +10 bp/day (constant)
    run_date = max(nav)
    st = br.compute(nav, spy, run_date, anchor='2026-06-23')
    assert st['anchor'] == '2026-06-23' and st['n_common'] >= 60
    assert st['book_since'] < 0 < st['spy_since']
    assert st['gap_pp'] == pytest.approx((st['book_since'] - st['spy_since']) * 100)
    assert st['book_20d'] == pytest.approx((1 - 0.002) ** 20 - 1, rel=1e-6)
    assert st['spy_20d'] == pytest.approx((1 + 0.001) ** 20 - 1, rel=1e-6)
    assert st['book_60d'] < st['book_20d'] and st['spy_60d'] > st['spy_20d']
    assert st['book_sharpe_20d'] is None and st['spy_sharpe_20d'] is None   # constant returns: zero variance -> None


def test_compute_sharpes_sign_with_noisy_returns():
    nav = _series_noisy(100_000.0, -0.002, 120)
    spy = _series_noisy(500.0, +0.001, 120)
    st = br.compute(nav, spy, max(nav), anchor='2026-06-23')
    assert st['book_sharpe_20d'] < 0 < st['spy_sharpe_20d']


def test_compute_uses_only_common_dates_and_handles_gaps():
    nav = _series(100_000.0, 0.0, 40); spy = _series(500.0, 0.0, 40)
    spy.pop(sorted(spy)[-3])                                   # SPY missing a day
    st = br.compute(nav, spy, max(nav), anchor='2026-06-01')
    assert st['n_common'] == len(nav) - 1
    assert st['book_20d'] == 0.0 and st['spy_20d'] == 0.0
    assert st['book_sharpe_20d'] is None and st['spy_sharpe_20d'] is None   # zero variance -> None, never NaN


def test_compute_too_short_returns_none():
    nav = _series(1.0, 0.0, 3); spy = _series(1.0, 0.0, 3)
    assert br.compute(nav, spy, max(nav), anchor='2026-06-01') is None


def test_compute_anchor_predates_history_reports_actual_since():
    nav = _series(100_000.0, -0.001, 30, first='2026-07-22')
    spy = _series(500.0, 0.001, 30, first='2026-07-22')
    st = br.compute(nav, spy, max(nav), anchor='2026-06-23')
    assert st['anchor'] == '2026-06-23'
    assert st['since'] == '2026-07-22'
    assert st['book_since'] == pytest.approx(nav[max(nav)] / nav['2026-07-22'] - 1)


def test_format_line_shape():
    st = {'anchor': '2026-06-23', 'since': '2026-06-23', 'n_common': 48, 'run_date': '2026-08-28',
          'book_since': -0.29, 'spy_since': 0.049,
          'gap_pp': -33.9, 'book_20d': -0.05, 'spy_20d': 0.02, 'book_60d': -0.2, 'spy_60d': 0.04,
          'book_sharpe_20d': -3.1, 'spy_sharpe_20d': 1.2}
    line = br.format_line(st, 'LOW_VOL', 0.805)
    assert line.startswith('bench_realized: since=2026-06-23 book=-29.0% spy=+4.9% gap=-33.9pp')
    assert '| 20d book=-5.0% spy=+2.0% | 60d book=-20.0% spy=+4.0% |' in line
    assert 'regime=LOW_VOL book_sharpe_20d=-3.10 spy_sharpe_20d=+1.20 S_m=0.805' in line
    # Final fix wave #5: the line must say WHICH day it describes and on how many
    # aligned observations — a stale NAV file otherwise reads as today's book.
    assert line.endswith('S_m=0.805 asof=2026-08-28 n=48')
    assert br.format_line(dict(st, book_sharpe_20d=None, spy_sharpe_20d=None), 'CRISIS', None).endswith(
        'regime=CRISIS book_sharpe_20d=n/a spy_sharpe_20d=n/a S_m=n/a asof=2026-08-28 n=48')
    short_line = br.format_line(dict(st, since='2026-07-22'), 'LOW_VOL', 0.805)
    assert short_line.startswith(
        'bench_realized: since=2026-07-22 book=-29.0% spy=+4.9% gap=-33.9pp (anchor 2026-06-23, history short) | 20d')


def test_load_nav_history_reads_close(tmp_path):
    p = tmp_path / 'pnl.json'
    p.write_text(json.dumps({'days': {'2026-08-28': {'open': 1, 'high': 2, 'low': 0, 'close': 92342.81},
                                       '2026-08-27': {'close': 93000.0}}}))
    assert br.load_nav_history(p) == {'2026-08-28': 92342.81, '2026-08-27': 93000.0}


def test_bench_realized_line_is_fail_open(monkeypatch, tmp_path):
    monkeypatch.setattr(br, 'load_nav_history', lambda path=None: (_ for _ in ()).throw(OSError('no file')))
    assert br.bench_realized_line('2026-08-28', conn=object()) is None


def test_bench_realized_line_end_to_end(monkeypatch):
    nav = _series(100_000.0, -0.001, 90); spy = _series(500.0, 0.001, 90)
    monkeypatch.setattr(br, 'load_nav_history', lambda path=None: nav)
    monkeypatch.setattr(br, '_load_spy_closes', lambda start, end: spy)
    monkeypatch.setattr(br, '_load_anchor', lambda conn: '2026-06-23')
    monkeypatch.setattr(br, '_load_regime_and_s_m', lambda conn, run_date: ('LOW_VOL', 0.805))
    line = br.bench_realized_line(max(nav), conn=object())
    assert line.startswith('bench_realized: since=2026-06-23 book=-') and 'S_m=0.805' in line


# ── final fix wave (2026-08-30) ─────────────────────────────────────────────
class _FakeCur:
    def __init__(self, row, boom=False): self._row = row; self._boom = boom
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        if self._boom:
            raise RuntimeError('connection reset')
    def fetchone(self): return self._row


class _FakeConn:
    def __init__(self, row=None, boom=False): self._row = row; self._boom = boom
    def cursor(self): return _FakeCur(self._row, self._boom)


def test_load_regime_and_s_m_never_triggers_the_s_m_grid_compute(monkeypatch):
    """Final fix wave #4: the 16:15 collect step must never do the sizer's
    parquet-wide S_m grid compute (nor write its day cache) just to print a
    report line — a cache miss has to return None."""
    from execution import benchmark_sizing as bzs
    seen = {}

    def _recorder(regime_state, run_date, *, benchmark='SPY', conn=None, compute=None, horizon=None):
        seen['regime'] = regime_state
        seen['compute'] = compute
        return None

    monkeypatch.setattr(bzs, 'regime_benchmark_sharpe_for_sizing', _recorder)
    regime, s_m = br._load_regime_and_s_m(_FakeConn(('LOW_VOL',)), '2026-08-28')
    assert regime == 'LOW_VOL' and s_m is None
    assert seen['regime'] == 'LOW_VOL'
    assert callable(seen['compute']) and seen['compute']('2016-04-11', '2026-08-28', benchmark='SPY') == {}


def test_sharpe_needs_twenty_observations():
    """Final fix wave #9: a "20d Sharpe" printed off 5 observations is noise."""
    import random
    random.seed(0)
    rets = [random.gauss(0.001, 0.01) for _ in range(25)]
    assert br.SHARPE_MIN_OBS == 20
    assert br._sharpe(rets[:19]) is None
    assert br._sharpe(rets[:20]) is not None
    assert br.MIN_COMMON == 5          # the since-anchor floor is unchanged


def test_load_anchor_warns_only_on_present_but_invalid_or_db_error(caplog):
    """Final fix wave #10: a never-seeded key is normal; garbage or a dead
    connection silently reverting to the default is not."""
    import logging
    with caplog.at_level(logging.WARNING, logger='execution.bench_realized'):
        assert br._load_anchor(_FakeConn(('2026-07-01',))) == '2026-07-01'
        assert br._load_anchor(_FakeConn(None)) == br.DEFAULT_ANCHOR          # missing row: silent
    assert caplog.text == ''
    with caplog.at_level(logging.WARNING, logger='execution.bench_realized'):
        assert br._load_anchor(_FakeConn(('not-a-date',))) == br.DEFAULT_ANCHOR
    assert 'not-a-date' in caplog.text and br.ANCHOR_KEY in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='execution.bench_realized'):
        assert br._load_anchor(_FakeConn(boom=True)) == br.DEFAULT_ANCHOR
    assert 'connection reset' in caplog.text
