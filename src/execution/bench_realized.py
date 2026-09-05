"""bench_realized.py — daily book-vs-buy-and-hold-SPY realized line (spec
2026-08-30 §6, D-6). Report-only: gates nothing, never raises out of
bench_realized_line (returns None on any failure, logged).

NAV history = logs/pnl_daily_ohlc.json (`days[date].close`, the live sampler's
end-of-day NAV); SPY = prices.parquet via benchmark_baseline.load_benchmark_closes
(pyarrow pushdown). Returns are computed on the COMMON dates of both series.
Sharpe over the trailing 20 common dates: (mean − rf/252)/std·√252, rf from
backtest.risk_free (const 5 % by default, DGS3MO when OPENCLAW_RF_SOURCE=macro);
zero variance or fewer than SHARPE_MIN_OBS (20) observations -> None.
MIN_COMMON (5) remains the floor for the since-anchor return, which needs no
distributional estimate.
"""
from __future__ import annotations
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
NAV_HISTORY_PATH = ROOT / 'logs' / 'pnl_daily_ohlc.json'
ANCHOR_KEY = 'bench_realized_anchor'
DEFAULT_ANCHOR = '2026-06-23'
from backtest.risk_free import (RISK_FREE_ANNUAL_CONST as _RF_CONST, excess_sharpe as _rf_excess_sharpe,
                                shadow_line as _rf_shadow_line)
RISK_FREE_DAILY = _RF_CONST / 252
MIN_COMMON = 5
# Final fix wave (2026-08-30) #9: a "20d Sharpe" must rest on 20 observations.
# MIN_COMMON (5) still gates the since-anchor return — a ratio of two prices
# needs no distributional estimate; an annualized Sharpe does.
SHARPE_MIN_OBS = 20


def load_nav_history(path=None) -> dict[str, float]:
    p = Path(path) if path else NAV_HISTORY_PATH
    days = json.loads(p.read_text()).get('days') or {}
    return {d: float(v['close']) for d, v in days.items() if isinstance(v, dict) and v.get('close') is not None}


def _load_spy_closes(start: str, end: str) -> dict[str, float]:
    from backtest.benchmark_baseline import load_benchmark_closes
    return load_benchmark_closes(start, end, 'SPY')


def _load_anchor(conn) -> str:
    """pipeline_config[ANCHOR_KEY] as an ISO date, else DEFAULT_ANCHOR.
    Final fix wave (2026-08-30) #10: an ABSENT row is normal (migration 152 seeds
    it on johnbot's next restart) and stays silent; a present-but-invalid value
    or a DB error must not silently re-anchor the whole since= column."""
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (ANCHOR_KEY,))
            row = cur.fetchone()
    except Exception as e:
        logger.warning('[bench_realized] %s unreadable/invalid (%r); using %s', ANCHOR_KEY, e, DEFAULT_ANCHOR)
        return DEFAULT_ANCHOR
    if not row or row[0] is None:
        return DEFAULT_ANCHOR
    try:
        import datetime as _dt
        v = str(row[0]).strip()
        _dt.date.fromisoformat(v)
        return v
    except Exception:
        logger.warning('[bench_realized] %s unreadable/invalid (%r); using %s', ANCHOR_KEY, row[0], DEFAULT_ANCHOR)
        return DEFAULT_ANCHOR


def _load_regime_and_s_m(conn, run_date):
    regime, s_m = None, None
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT state FROM intraday_regime_states ORDER BY ts_utc DESC LIMIT 1')
            row = cur.fetchone()
            regime = row[0] if row else None
        if regime:
            from execution.benchmark_sizing import regime_benchmark_sharpe_for_sizing
            # Final fix wave (2026-08-30) #4: CACHE-ONLY read. Without a compute
            # override a cache miss (a run_date the sizer has not stamped — e.g.
            # a back-dated report, or the 16:15 collect running before the day's
            # sizing cycle) would send this report-only line into the sizer's
            # full parquet-wide S_m grid compute AND make it write the day cache.
            # An empty grid makes the provider return None, so the line just
            # prints `S_m=n/a` — the intended degradation. (The provider logs its
            # own '[bench_sizing] S_m compute returned no regimes' WARN on that
            # path; it is the cache miss, not a failure.)
            s_m = regime_benchmark_sharpe_for_sizing(regime, run_date, conn=conn,
                                                     compute=lambda *a, **k: {})
    except Exception as e:
        logger.warning('[bench_realized] regime/S_m unavailable: %s', e)
    return regime, s_m


def _sharpe(rets: list[float], dates=None):
    if len(rets) < SHARPE_MIN_OBS:
        return None
    # Spec C.4: EVERY rf site emits a shadow line, not just aggregate_metrics.
    # One line per Sharpe actually computed — two per compute() (book, SPY).
    # Diagnostics never cost the report-only line, hence the guard.
    try:
        logger.info(_rf_shadow_line('bench_realized', rets, dates))
    except Exception as e:  # noqa: BLE001
        logger.warning('[bench_realized] rf shadow line skipped (%s: %s)', type(e).__name__, e)
    return _rf_excess_sharpe(rets, dates, min_obs=SHARPE_MIN_OBS)


def _window_return(vals: list[float], n: int):
    if len(vals) < n + 1:
        return None
    return vals[-1] / vals[-n - 1] - 1.0


def compute(nav_by_date: dict, spy_by_date: dict, run_date, anchor: str):
    run_date = str(run_date)[:10]
    dates = sorted(d for d in nav_by_date if d in spy_by_date and d <= run_date)
    if len(dates) < MIN_COMMON:
        return None
    nav = [float(nav_by_date[d]) for d in dates]
    spy = [float(spy_by_date[d]) for d in dates]
    # since-anchor: first common date >= anchor
    i0 = next((i for i, d in enumerate(dates) if d >= anchor), None)
    if i0 is None or i0 == len(dates) - 1:
        return None
    book_since = nav[-1] / nav[i0] - 1.0
    spy_since = spy[-1] / spy[i0] - 1.0
    nav_r = [nav[i] / nav[i - 1] - 1.0 for i in range(1, len(nav))]
    spy_r = [spy[i] / spy[i - 1] - 1.0 for i in range(1, len(spy))]
    return {
        'anchor': anchor, 'since': dates[i0], 'n_common': len(dates), 'run_date': dates[-1],
        'book_since': book_since, 'spy_since': spy_since, 'gap_pp': (book_since - spy_since) * 100.0,
        'book_20d': _window_return(nav, 20), 'spy_20d': _window_return(spy, 20),
        'book_60d': _window_return(nav, 60), 'spy_60d': _window_return(spy, 60),
        'book_sharpe_20d': _sharpe(nav_r[-20:], dates[-20:]), 'spy_sharpe_20d': _sharpe(spy_r[-20:], dates[-20:]),
    }


def _pct(v):
    return 'n/a' if v is None else f'{v * 100:+.1f}%'


def _sh(v):
    return 'n/a' if v is None else f'{v:+.2f}'


def format_line(st: dict, regime, s_m) -> str:
    short_note = '' if st['since'] == st['anchor'] else f" (anchor {st['anchor']}, history short)"
    return (f"bench_realized: since={st['since']} book={_pct(st['book_since'])} spy={_pct(st['spy_since'])} "
            f"gap={st['gap_pp']:+.1f}pp{short_note} | 20d book={_pct(st['book_20d'])} spy={_pct(st['spy_20d'])} | "
            f"60d book={_pct(st['book_60d'])} spy={_pct(st['spy_60d'])} | "
            f"regime={regime or 'n/a'} book_sharpe_20d={_sh(st['book_sharpe_20d'])} "
            f"spy_sharpe_20d={_sh(st['spy_sharpe_20d'])} S_m={'n/a' if s_m is None else f'{float(s_m):.3f}'} "
            f"asof={st['run_date']} n={st['n_common']}")


def bench_realized_line(run_date, *, nav_path=None, conn=None):
    """The full line, or None on any failure (logged). Own connection when conn is None."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        nav = load_nav_history(nav_path)
        if not nav:
            return None
        anchor = _load_anchor(conn)
        spy = _load_spy_closes(min(min(nav), anchor), str(run_date)[:10])
        st = compute(nav, spy, run_date, anchor)
        if st is None:
            return None
        regime, s_m = _load_regime_and_s_m(conn, run_date)
        return format_line(st, regime, s_m)
    except Exception as e:
        logger.warning('[bench_realized] skipped (%s: %s)', type(e).__name__, e)
        return None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass
