"""cohort_base 30-minute session handling (2026-10-17).

ET sessions derived from the bar timestamp (never the stored `date` label),
RTH-only bars (13/day), `today` = latest RTH session, point-in-time trailing
stats (SPY 09:30 volume avg, per-ticker ADV30) that exclude today, and the two
cohort strategies (S_TR04 / S_TR06) selecting bars by `time_et`, not by index.
All frames are built in memory; no parquet is read.
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from src.strategies.cohort_base import CohortBaseStrategy  # noqa: E402
from src.strategies.implementations.str04_zarattini_intraday_spy import ZarattiniIntradaySPY  # noqa: E402
from src.strategies.implementations.str06_baltussen_eod_reversal import BaltussenEODReversal  # noqa: E402



class _Probe(CohortBaseStrategy):
    regime_filter = ['LOW_VOL']

    def _generate_signals_cohort(self, market_data, opts_map):
        return []


def _weekdays(start, n):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _utc_times(convention):
    """UTC bar-start clock strings for one session."""
    if convention == 'ext':      # 08:00Z-23:30Z, 32 bars
        return [f'{h:02d}:{m:02d}' for h in range(8, 24) for m in (0, 30)]
    return ['13:30', '14:00', '14:30', '15:00', '15:30', '16:00', '16:30',
                       '17:00', '17:30', '18:00', '18:30', '19:00', '19:30']


def _make_frame(sessions, convention, tickers, today_path=None, vol=None, vwap_bump=0.1):
    """Build the long 30-minute frame.

    `vol(tk, sess_idx, bar_clock_utc) -> float`; `today_path[tk](k) -> (open, close)`
    for RTH bar k of the LAST session (default flat 100).
    """
    rows = []
    last = len(sessions) - 1
    for si, d in enumerate(sessions):
        for t in _utc_times(convention):
            ts = pd.Timestamp(f'{d.isoformat()} {t}', tz='UTC')
            et = ts.tz_convert('America/New_York')
            hhmm = et.strftime('%H:%M')
            is_rth = '09:30' <= hhmm < '16:00'
            k = (et.hour * 60 + et.minute - 570) // 30 if is_rth else None
            for tk in tickers:
                o = c = 100.0
                if si == last and is_rth and today_path and tk in today_path:
                    o, c = today_path[tk](k)
                v = vol(tk, si, t) if vol else 1000.0 + si
                rows.append({
                    'date': d.isoformat(), 'datetime': ts, 'ticker': tk,
                    'open': o, 'high': max(o, c) + 0.5, 'low': min(o, c) - 0.5,
                    'close': c, 'volume': float(v), 'vwap': c + vwap_bump,
                    'transactions': 10,
                })
    df = pd.DataFrame(rows)
    df['datetime'] = df['datetime'].astype('datetime64[us, UTC]')
    return df


def _build(df, options=None):
    strat = _Probe.__new__(_Probe)
    return strat._build_cohort_dicts(
        pd.DataFrame(), {'state': 'LOW_VOL'}, ['SPY'],
        {'prices_30m': df, 'options': options or {}})


def _vol(tk, si, t):
    # distinguishable per ticker / session / bar clock
    base = {'SPY': 5000.0, 'AAA': 3000.0, 'BBB': 2000.0}.get(tk, 1000.0)
    return base + 100 * si + (int(t[:2]) * 2 + int(t[3:] == '30'))


def _hand_stats(df, today):
    """Independent pure-python recomputation (ET session from the timestamp)."""
    by_sess = {}
    for r in df.itertuples():
        et = r.datetime.tz_convert('America/New_York')
        hhmm, sess = et.strftime('%H:%M'), et.strftime('%Y-%m-%d')
        if not ('09:30' <= hhmm < '16:00') or sess >= today:
            continue
        by_sess.setdefault((r.ticker, sess), []).append((hhmm, r.volume, r.vwap, r.close))
    adv, opens = {}, []
    for (tk, sess), bars in sorted(by_sess.items(), key=lambda kv: kv[0][1]):
        adv.setdefault(tk, []).append(sum(v * (w if w and w > 0 else c) for _, v, w, c in bars))
        if tk == 'SPY':
            opens += [v for hhmm, v, _, _ in bars if hhmm == '09:30']
    return ({tk: sum(x[-30:]) / len(x[-30:]) for tk, x in adv.items()},
            sum(opens[-10:]) / len(opens[-10:]))


CASES = {
    'extended_winter': (_weekdays(date(2026, 1, 5), 13), 'ext'),   # EST: 14:30Z == 09:30 ET
    'rth_summer': (_weekdays(date(2026, 6, 15), 13), 'rth'),       # EDT: 13:30Z == 09:30 ET
}


@pytest.mark.parametrize('case', sorted(CASES))
def test_session_rth_bars_and_trailing_stats(case):
    sessions, conv = CASES[case]
    df = _make_frame(sessions, conv, ['SPY', 'AAA', 'BBB'], vol=_vol)
    today = sessions[-1].isoformat()
    # Mislabel: after-hours bars of two prior sessions carry date+1 (as in the real
    # parquet), plus every BBB bar of one prior session is labelled a day late.
    late = df['datetime'].dt.tz_convert('America/New_York').dt.hour >= 18
    df.loc[late & (df['date'] == sessions[3].isoformat()), 'date'] = sessions[3].isoformat()[:-2] + '99'
    df.loc[(df['ticker'] == 'BBB') & (df['date'] == sessions[5].isoformat()), 'date'] = '2000-01-01'

    md, opts = _build(df, options={'AAA': {'avg_dollar_volume_30d': 42.0}})

    assert md['intraday_30m_session'] == today
    spy = md['spy_30m_bars']
    assert len(spy) == 13
    assert spy[0]['time_et'] == '09:30' and spy[-1]['time_et'] == '15:30'
    assert [b['time_et'] for b in spy] == sorted(b['time_et'] for b in spy)
    assert all(b['session'] == today and b['ticker'] == 'SPY' for b in spy)
    assert isinstance(spy[0]['datetime'], pd.Timestamp) and str(spy[0]['datetime'].tz) == 'UTC'
    assert set(md['intraday_30m_bars']) == {'SPY', 'AAA', 'BBB'}
    assert all(len(v) == 13 for v in md['intraday_30m_bars'].values())

    adv, vol10 = _hand_stats(df, today)
    assert md['spy_10d_volume_avg'] == pytest.approx(vol10)
    assert opts['BBB']['avg_dollar_volume_30d'] == pytest.approx(adv['BBB'])
    assert opts['SPY']['avg_dollar_volume_30d'] == pytest.approx(adv['SPY'])
    assert opts['AAA']['avg_dollar_volume_30d'] == 42.0   # options value wins

    # the stored `date` label must not influence anything
    df2 = df.copy()
    df2['date'] = 'garbage'
    md2, opts2 = _build(df2, options={'AAA': {'avg_dollar_volume_30d': 42.0}})
    assert md2['spy_10d_volume_avg'] == md['spy_10d_volume_avg']
    assert opts2['BBB'] == opts['BBB']


def test_today_excluded_and_only_ten_sessions_used():
    sessions, conv = CASES['rth_summer']       # 12 prior sessions
    df = _make_frame(sessions, conv, ['SPY'], vol=_vol)
    md, _ = _build(df)
    # Blow up today's own 09:30 SPY volume: the trailing stat must not move.
    et_open = pd.Timestamp(f'{sessions[-1].isoformat()} 13:30', tz='UTC')
    df.loc[(df['datetime'] == et_open) & (df['ticker'] == 'SPY'), 'volume'] = 1e12
    md_b, opts_b = _build(df)
    assert md_b['spy_10d_volume_avg'] == md['spy_10d_volume_avg']
    # Only the 10 most recent prior sessions count (12 exist; first two ignored).
    expect = sum(_vol('SPY', si, '13:30') for si in range(2, 12)) / 10
    assert md['spy_10d_volume_avg'] == pytest.approx(expect)


def test_too_few_prior_sessions_leaves_stats_unset():
    sessions = _weekdays(date(2026, 6, 15), 5)   # 4 prior sessions (< 5)
    md, opts = _build(_make_frame(sessions, 'rth', ['SPY', 'BBB'], vol=_vol))
    assert 'spy_10d_volume_avg' not in md
    assert 'avg_dollar_volume_30d' not in opts.get('BBB', {})
    assert len(md['spy_30m_bars']) == 13


def test_nan_vwap_falls_back_to_close_and_nan_volume_tolerated():
    sessions, conv = CASES['rth_summer']
    df = _make_frame(sessions, conv, ['SPY', 'BBB'], vol=lambda tk, si, t: 100.0)
    df.loc[df['ticker'] == 'BBB', 'vwap'] = float('nan')
    df.loc[(df['ticker'] == 'BBB') & (df['date'] == sessions[0].isoformat()), 'volume'] = float('nan')
    _, opts = _build(df)
    # 11 prior sessions with data; 13 bars x 100 vol x close 100 each
    assert opts['BBB']['avg_dollar_volume_30d'] == pytest.approx(13 * 100 * 100.0)


def test_no_ticker_column_treated_as_spy_and_no_rth_is_safe():
    sessions, conv = CASES['rth_summer']
    df = _make_frame(sessions, conv, ['SPY'], vol=_vol).drop(columns=['ticker'])
    md, _ = _build(df)
    assert len(md['spy_30m_bars']) == 13
    # only after-hours rows -> nothing set, no exception
    ah = _make_frame(sessions, 'ext', ['SPY'])
    ah = ah[ah['datetime'].dt.tz_convert('America/New_York').dt.hour >= 17]
    md_ah, _ = _build(ah)
    assert 'spy_30m_bars' not in md_ah and 'intraday_30m_session' not in md_ah


def test_naive_datetime_treated_as_utc():
    sessions, conv = CASES['rth_summer']
    df = _make_frame(sessions, conv, ['SPY'], vol=_vol)
    df['datetime'] = df['datetime'].dt.tz_localize(None)
    md, _ = _build(df)
    assert [b['time_et'] for b in md['spy_30m_bars']][0] == '09:30'


# ---------------------------------------------------------------- strategies

def _s04_market(gap_up=True, vol_mult=3.0):
    sessions, conv = CASES['rth_summer']
    open_px = 101.0 if gap_up else 100.0
    df = _make_frame(sessions, conv, ['SPY'],
                     today_path={'SPY': lambda k: (open_px, open_px + 0.2)},
                     vol=lambda tk, si, t: 1000.0 * (vol_mult if (si == len(sessions) - 1 and t == '13:30') else 1))
    md, _ = _build(df)
    md.update(spy_prev_close=100.0, vix_close=15.0)
    return md


def test_str04_long_on_gap_up_high_volume_and_empty_without_open_bar():
    strat = ZarattiniIntradaySPY.__new__(ZarattiniIntradaySPY)
    md = _s04_market()
    sigs = strat._generate_signals_cohort(md, {})
    assert len(sigs) == 1
    assert sigs[0].ticker == 'SPY' and sigs[0].direction == 'LONG'
    assert sigs[0].entry_price == 101.0          # the 09:30 bar's open, not an old bar
    md['spy_30m_bars'] = [b for b in md['spy_30m_bars'] if b['time_et'] != '09:30']
    assert strat._generate_signals_cohort(md, {}) == []
    assert ZarattiniIntradaySPY.version == '2.1.0'


def _s06_market(drop_1500=False):
    sessions, conv = CASES['rth_summer']

    def path(open_px, mid, late):
        def f(k):
            c = {0: open_px, 6: mid, 11: late}.get(k, open_px)   # k=6 -> 12:30 bar, 11 -> 15:00
            return (open_px if k == 0 else c, c)
        return f
    df = _make_frame(sessions, conv, ['SPY', 'DNX', 'UPY', 'FLAT'],
                     today_path={'DNX': path(100.0, 98.0, 97.0), 'UPY': path(100.0, 102.0, 103.0)},
                     vol=lambda tk, si, t: 1e6)
    if drop_1500:   # remove DNX's 15:00 ET bar (19:00Z in summer)
        gone = (df['ticker'] == 'DNX') & (df['datetime'] == pd.Timestamp(f'{sessions[-1].isoformat()} 19:00', tz='UTC'))
        df = df[~gone]
    return _build(df)


def test_str06_uses_1230_and_1500_bars_and_skips_missing_1500():
    strat = BaltussenEODReversal.__new__(BaltussenEODReversal)
    md, opts = _s06_market()
    assert opts['DNX']['avg_dollar_volume_30d'] >= strat.LIQUIDITY_DOLLAR_VOL_MIN
    sigs = strat._generate_signals_cohort(md, opts)
    got = {(s.ticker, s.direction) for s in sigs}
    # FLAT has no drift. (With <5 candidates the basket overlaps, so each of the
    # two names also appears on the other side: pre-existing basket behaviour.)
    assert {('DNX', 'LONG'), ('UPY', 'SHORT')} <= got
    assert not {t for t, _ in got} - {'DNX', 'UPY'}
    dn = next(s for s in sigs if s.ticker == 'DNX')
    assert dn.signal_params['r_total_intraday'] == pytest.approx(97.0 / 100.0 - 1, abs=1e-4)
    assert dn.entry_price == 97.0                       # 15:00 bar close (~15:30)

    md2, opts2 = _s06_market(drop_1500=True)
    got2 = {s.ticker for s in strat._generate_signals_cohort(md2, opts2)}
    assert 'DNX' not in got2
    assert BaltussenEODReversal.version == '2.1.0'
