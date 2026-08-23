"""FMP /stable/earnings-calendar as the PRIMARY earnings-master feed (D2).

No network: requests.get is monkeypatched, parquets live in tmp_path.
Guards:
  * 7-day window chunking covers [start, end] with no gaps / no overlap
  * FMP payload -> master column mapping
  * merge semantics: new rows appended, existing rows NaN-filled in place,
    nothing dropped, no new duplicate keys
  * 429 -> Retry-After honoured; a failed window is counted, not fatal
  * yfinance fallback taken only when FMP fails outright
  * universe filter: master tickers + active universe, nothing else
  * --dry-run exercises fetch + merge + tmp serialisation, skips os.replace
  * both sources failing -> non-zero exit, master untouched
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from src.ingestion import ingest_earnings_master as iem  # noqa: E402

TODAY = date(2026, 8, 23)

# Verbatim row shape from the 2026-08-23 probe of /stable/earnings-calendar.
FMP_SAMPLE = {'symbol': 'BYDDF', 'date': '2026-08-28', 'epsActual': None,
              'epsEstimated': 0.1251, 'revenueActual': None,
              'revenueEstimated': 25689080000, 'lastUpdated': '2026-08-23'}


def _master_df():
    return pd.DataFrame({
        'ticker':            ['AAPL', 'AAPL', 'ZTS'],
        'date':              pd.to_datetime(['2026-04-30', '2026-08-04',
                                             '2025-12-31']),
        'eps_actual':        [2.01, float('nan'), 1.48],
        'eps_estimated':     [1.94, 1.89, 1.40],
        'revenue_actual':    [float('nan')] * 3,
        'revenue_estimated': [float('nan')] * 3,
        'last_updated':      ['2026-04-30'] * 3,
    })


class _Resp:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.headers = headers or {}
        self.text = json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def paths(tmp_path, monkeypatch):
    m = tmp_path / 'earnings.parquet'
    c = tmp_path / 'earnings_calendar.parquet'
    monkeypatch.setattr(iem, 'MASTER_PATH', m)
    monkeypatch.setattr(iem, 'CALENDAR_PATH', c)
    _master_df().to_parquet(m, index=False)
    # No local forward calendar -> forward_rows() contributes nothing, so
    # every row delta below is attributable to FMP / the fallback.
    monkeypatch.setattr(iem, '_today', lambda: TODAY)
    monkeypatch.setattr(iem, '_active_universe', lambda: [])
    monkeypatch.setattr(iem, '_record_call', lambda *a, **k: None)
    monkeypatch.setattr(iem.time, 'sleep', lambda s: None)
    monkeypatch.setenv('FMP_API_KEY', 'test-key')
    return m, c


# ── 1. window chunking ──────────────────────────────────────────────────────

def test_fmp_windows_cover_range_contiguously():
    start = TODAY - timedelta(days=10)
    end = TODAY + timedelta(days=120)
    wins = iem.fmp_windows(start, end, window_days=7)
    assert wins[0][0] == start
    assert wins[-1][1] == end
    for a, b in wins:
        assert a <= b
        assert (b - a).days <= 6            # inclusive 7-day window
    for (_, b_prev), (a_next, _) in zip(wins, wins[1:]):
        assert a_next == b_prev + timedelta(days=1)   # no gap, no overlap
    assert len(wins) == 19                  # ceil(131 / 7)


def test_fmp_windows_single_short_range():
    assert iem.fmp_windows(TODAY, TODAY, window_days=7) == [(TODAY, TODAY)]


# ── 2. payload -> master mapping ────────────────────────────────────────────

def test_fmp_rows_to_master_mapping():
    out = iem.fmp_rows_to_master([FMP_SAMPLE], TODAY)
    assert list(out.columns) == iem.MASTER_COLUMNS
    r = out.iloc[0]
    assert r['ticker'] == 'BYDDF'
    assert r['date'] == pd.Timestamp('2026-08-28')
    assert pd.isna(r['eps_actual'])
    assert r['eps_estimated'] == pytest.approx(0.1251)
    assert pd.isna(r['revenue_actual'])
    assert r['revenue_estimated'] == pytest.approx(25689080000)
    assert r['last_updated'] == TODAY.isoformat()
    assert str(out['date'].dtype).startswith('datetime64')


def test_fmp_rows_to_master_drops_junk_and_dedups():
    rows = [FMP_SAMPLE,
            {**FMP_SAMPLE, 'epsEstimated': 0.2},          # same key, later wins
            {'symbol': None, 'date': '2026-08-28'},        # no ticker
            {'symbol': 'X', 'date': 'not-a-date'},         # bad date
            {'symbol': 'BRK.B', 'date': '2026-09-01', 'epsEstimated': 1.0}]
    out = iem.fmp_rows_to_master(rows, TODAY)
    assert len(out) == 2
    byd = out[out['ticker'] == 'BYDDF'].iloc[0]
    assert byd['eps_estimated'] == pytest.approx(0.2)
    assert 'BRK-B' in set(out['ticker'])   # master uses '-' for share classes


# ── 3. merge semantics ──────────────────────────────────────────────────────

def test_merge_rows_fills_in_place_appends_new_never_drops(paths):
    master = iem._load_master()
    n0 = len(master)
    rows = iem.fmp_rows_to_master([
        # AAPL 08-04 already in master unreported -> actuals filled IN PLACE
        {'symbol': 'AAPL', 'date': '2026-08-04', 'epsActual': 2.02,
         'epsEstimated': 1.89, 'revenueActual': 9.4e10,
         'revenueEstimated': 9.3e10},
        # brand-new forward event -> appended
        {'symbol': 'NVDA', 'date': '2026-08-27', 'epsActual': None,
         'epsEstimated': 1.01, 'revenueActual': None,
         'revenueEstimated': 4.6e10},
        # already-complete row, identical values -> untouched
        {'symbol': 'ZTS', 'date': '2025-12-31', 'epsActual': 1.48,
         'epsEstimated': 1.40},
    ], TODAY)
    out, stats = iem.merge_rows(master, rows, TODAY)
    assert len(out) == n0 + 1
    assert stats['rows_new'] == 1
    assert stats['rows_updated'] == 1
    assert stats['actuals_filled'] == 1
    assert stats['tickers_touched'] == {'AAPL', 'NVDA'}
    aapl = out[(out['ticker'] == 'AAPL')
               & (out['date'] == pd.Timestamp('2026-08-04'))]
    assert len(aapl) == 1, 'must not create a duplicate key'
    aapl = aapl.iloc[0]
    assert aapl['eps_actual'] == 2.02
    assert aapl['revenue_actual'] == pytest.approx(9.4e10)
    assert aapl['revenue_estimated'] == pytest.approx(9.3e10)
    assert aapl['last_updated'] == TODAY.isoformat()
    zts = out[out['ticker'] == 'ZTS'].iloc[0]
    assert zts['last_updated'] == '2026-04-30'
    apr = out[(out['ticker'] == 'AAPL')
              & (out['date'] == pd.Timestamp('2026-04-30'))].iloc[0]
    assert apr['eps_actual'] == 2.01 and apr['last_updated'] == '2026-04-30'
    assert not out.duplicated(['ticker', 'date']).any()
    # every original row survives
    orig = set(zip(master['ticker'], master['date']))
    assert orig <= set(zip(out['ticker'], out['date']))


def test_merge_rows_day_skew_matches_existing_row(paths):
    """FMP says 08-05, master's yfinance forward row says 08-04 (bmo/amc
    skew): fill the existing row rather than appending a near-duplicate."""
    master = iem._load_master()
    rows = iem.fmp_rows_to_master([
        {'symbol': 'AAPL', 'date': '2026-08-05', 'epsActual': 2.02,
         'epsEstimated': 1.89}], TODAY)
    out, stats = iem.merge_rows(master, rows, TODAY)
    assert len(out) == len(master)
    assert stats['rows_new'] == 0 and stats['actuals_filled'] == 1
    assert out[(out['ticker'] == 'AAPL')
               & (out['date'] == pd.Timestamp('2026-08-04'))].iloc[0]['eps_actual'] == 2.02


def test_merge_rows_refreshes_estimate_on_unreported_forward_row(paths):
    """Consensus revisions: an unreported FUTURE row takes the latest
    estimate; a REPORTED row's estimate is frozen (SUE reproducibility)."""
    master = iem._load_master()
    master.loc[len(master)] = ['MSFT', pd.Timestamp('2026-10-27'), float('nan'),
                               3.20, float('nan'), float('nan'), '2026-08-06']
    rows = iem.fmp_rows_to_master([
        {'symbol': 'MSFT', 'date': '2026-10-27', 'epsEstimated': 3.25},
        {'symbol': 'AAPL', 'date': '2026-04-30', 'epsActual': 2.01,
         'epsEstimated': 1.90}], TODAY)
    out, stats = iem.merge_rows(master, rows, TODAY)
    msft = out[out['ticker'] == 'MSFT'].iloc[0]
    assert msft['eps_estimated'] == pytest.approx(3.25)
    assert msft['last_updated'] == TODAY.isoformat()
    apr = out[(out['ticker'] == 'AAPL')
              & (out['date'] == pd.Timestamp('2026-04-30'))].iloc[0]
    assert apr['eps_estimated'] == 1.94 and apr['last_updated'] == '2026-04-30'
    assert stats['rows_updated'] == 1 and stats['rows_new'] == 0


def test_merge_rows_preserves_preexisting_duplicate_keys(paths):
    """The real master carries 101 legacy duplicate (ticker,date) keys; the
    merge must not dedupe them away (append-only)."""
    master = iem._load_master()
    master = pd.concat([master, master.iloc[[0]]], ignore_index=True)
    rows = iem.fmp_rows_to_master([
        {'symbol': 'AAPL', 'date': '2026-04-30', 'epsActual': 2.01}], TODAY)
    out, _ = iem.merge_rows(master, rows, TODAY)
    assert len(out) == len(master)


# ── 4. fetch: window accounting, 429 handling ───────────────────────────────

def test_fetch_fmp_calendar_counts_windows_and_honours_retry_after(paths, monkeypatch):
    calls = []
    slept = []
    monkeypatch.setattr(iem.time, 'sleep', lambda s: slept.append(s))
    state = {'w2_429_served': False}

    def fake_get(url, params=None, timeout=None):
        calls.append((url, dict(params), timeout))
        assert url == iem.FMP_CALENDAR_URL
        assert params['apikey'] == 'test-key'
        frm = params['from']
        if frm == '2026-08-13' and not state['w2_429_served']:
            state['w2_429_served'] = True
            return _Resp(429, headers={'Retry-After': '3'})
        if frm == '2026-08-20':
            return _Resp(500)
        return _Resp(200, [{**FMP_SAMPLE, 'date': frm}])

    monkeypatch.setattr(iem.requests, 'get', fake_get)
    start, end = date(2026, 8, 13), date(2026, 9, 2)   # 3 windows
    raw, n_ok, n_fail = iem.fetch_fmp_calendar(start, end, 'test-key',
                                               pace_s=0.0)
    assert (n_ok, n_fail) == (2, 1)
    assert len(raw) == 2
    assert 3 in slept                      # Retry-After honoured
    assert all(t == 30 for _, _, t in calls)
    assert sum(1 for _, p, _ in calls if p['from'] == '2026-08-13') == 2


def test_fetch_fmp_calendar_raises_when_all_windows_fail(paths, monkeypatch):
    monkeypatch.setattr(iem.requests, 'get', lambda *a, **k: _Resp(403))
    with pytest.raises(iem.FmpUnavailable):
        iem.fetch_fmp_calendar(TODAY, TODAY + timedelta(days=13), 'k', pace_s=0)


def test_fetch_fmp_calendar_raises_without_key(paths):
    with pytest.raises(iem.FmpUnavailable):
        iem.fetch_fmp_calendar(TODAY, TODAY, '', pace_s=0)


# ── 5. main(): primary path, fallback, universe filter, dry-run, exit codes ─

def _stub_fmp(monkeypatch, rows, n_ok=19, n_fail=0):
    seen = []

    def fake_fetch(start, end, api_key, **kw):
        seen.append((start, end, api_key))
        return rows, n_ok, n_fail
    monkeypatch.setattr(iem, 'fetch_fmp_calendar', fake_fetch)
    return seen


def test_main_fmp_primary_writes_and_skips_yfinance(paths, monkeypatch):
    m, _ = paths
    seen = _stub_fmp(monkeypatch, [
        {'symbol': 'AAPL', 'date': '2026-08-04', 'epsActual': 2.02,
         'epsEstimated': 1.89},
        {'symbol': 'ZTS', 'date': '2026-11-05', 'epsEstimated': 1.5}])
    yf_called = []
    monkeypatch.setattr(iem, '_fetch_actuals',
                        lambda *a, **k: yf_called.append(1))
    rc = iem.main([])
    assert rc == 0
    assert not yf_called, 'yfinance must not be consulted when FMP succeeded'
    start, end, key = seen[0]
    assert (start, end) == (TODAY - timedelta(days=10), TODAY + timedelta(days=120))
    assert key == 'test-key'
    out = pd.read_parquet(m)
    assert len(out) == 4
    assert out[(out['ticker'] == 'AAPL')
               & (out['date'] == pd.Timestamp('2026-08-04'))]['eps_actual'].iloc[0] == 2.02
    assert out['last_updated'].max() == TODAY.isoformat()


def test_main_filters_to_master_or_active_universe(paths, monkeypatch):
    m, _ = paths
    monkeypatch.setattr(iem, '_active_universe', lambda: ['NEWCO'])
    _stub_fmp(monkeypatch, [
        {'symbol': 'AAPL', 'date': '2026-10-29', 'epsEstimated': 1.98},   # master
        {'symbol': 'NEWCO', 'date': '2026-10-01', 'epsEstimated': 0.5},   # universe
        {'symbol': 'JUNK', 'date': '2026-10-02', 'epsEstimated': 0.1}])   # neither
    assert iem.main([]) == 0
    out = pd.read_parquet(m)
    assert set(out['ticker']) == {'AAPL', 'ZTS', 'NEWCO'}
    assert len(out) == 5


def test_main_universe_file_extends_allowed_set(paths, monkeypatch, tmp_path):
    m, _ = paths
    uf = tmp_path / 'u.txt'
    uf.write_text('# comment\nJUNK\n')
    _stub_fmp(monkeypatch, [
        {'symbol': 'JUNK', 'date': '2026-10-02', 'epsEstimated': 0.1}])
    assert iem.main(['--universe-file', str(uf)]) == 0
    assert 'JUNK' in set(pd.read_parquet(m)['ticker'])


def test_main_falls_back_to_yfinance_when_fmp_raises(paths, monkeypatch):
    m, _ = paths

    def boom(*a, **k):
        raise iem.FmpUnavailable('HTTP 403')
    monkeypatch.setattr(iem, 'fetch_fmp_calendar', boom)
    monkeypatch.setattr(iem, 'ACTUALS_LOOKBACK_DAYS', 30)   # 08-04 is 19d back
    yf_called = []

    def fake_yf(tickers, throttle_s):
        yf_called.append(list(tickers))
        return pd.DataFrame({'ticker': ['AAPL'], 'date': [date(2026, 8, 4)],
                             'eps_estimated': [1.89], 'eps_actual': [2.02]})
    monkeypatch.setattr(iem, '_fetch_actuals', fake_yf)
    rc = iem.main([])
    assert rc == 0
    assert yf_called == [['AAPL']]
    out = pd.read_parquet(m)
    assert out[(out['ticker'] == 'AAPL')
               & (out['date'] == pd.Timestamp('2026-08-04'))]['eps_actual'].iloc[0] == 2.02


def test_main_falls_back_when_fmp_returns_empty(paths, monkeypatch):
    _stub_fmp(monkeypatch, [], n_ok=19)
    monkeypatch.setattr(iem, 'ACTUALS_LOOKBACK_DAYS', 30)
    yf_called = []
    monkeypatch.setattr(iem, '_fetch_actuals', lambda t, throttle_s: (
        yf_called.append(1) or pd.DataFrame(
            {'ticker': ['AAPL'], 'date': [date(2026, 8, 4)],
             'eps_estimated': [1.89], 'eps_actual': [2.02]})))
    assert iem.main([]) == 0
    assert yf_called


def test_main_exit_nonzero_when_both_sources_fail(paths, monkeypatch):
    m, _ = paths
    before = m.read_bytes()

    def boom(*a, **k):
        raise iem.FmpUnavailable('HTTP 403')
    monkeypatch.setattr(iem, 'fetch_fmp_calendar', boom)
    monkeypatch.setattr(iem, '_fetch_actuals',
                        lambda *a, **k: pd.DataFrame(
                            columns=['ticker', 'date', 'eps_estimated', 'eps_actual']))
    rc = iem.main([])
    assert rc != 0
    assert m.read_bytes() == before
    assert not Path(str(m) + '.tmp').exists()


def test_main_dry_run_fetches_merges_but_does_not_replace(paths, monkeypatch):
    m, _ = paths
    before = m.read_bytes()
    seen = _stub_fmp(monkeypatch, [
        {'symbol': 'AAPL', 'date': '2026-08-04', 'epsActual': 2.02}])
    replaced = []
    monkeypatch.setattr(iem.os, 'replace',
                        lambda *a, **k: replaced.append(a))
    rc = iem.main(['--dry-run'])
    assert rc == 0
    assert seen, '--dry-run must exercise the real fetch path'
    assert not replaced, '--dry-run must never os.replace the master'
    assert m.read_bytes() == before
    assert not Path(str(m) + '.tmp').exists(), 'tmp must be cleaned up'


def test_main_prints_counters(paths, monkeypatch, capsys):
    _stub_fmp(monkeypatch, [
        {'symbol': 'AAPL', 'date': '2026-08-04', 'epsActual': 2.02},
        {'symbol': 'ZTS', 'date': '2026-11-05', 'epsEstimated': 1.5}],
        n_ok=18, n_fail=1)
    assert iem.main([]) == 0
    out = capsys.readouterr().out
    for key in ('fmp_rows_fetched=2', 'fmp_windows_ok=18',
                'fmp_windows_failed=1', 'rows_new=1', 'rows_updated=1',
                'tickers_touched=2', f'master_max_last_updated={TODAY.isoformat()}'):
        assert key in out, f'missing counter {key!r} in:\n{out}'
