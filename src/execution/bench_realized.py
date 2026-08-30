"""bench_realized.py — daily book-vs-buy-and-hold-SPY realized line (spec
2026-08-30 §6, D-6). Report-only: gates nothing, never raises out of
bench_realized_line (returns None on any failure, logged).

NAV history = logs/pnl_daily_ohlc.json (`days[date].close`, the live sampler's
end-of-day NAV); SPY = prices.parquet via benchmark_baseline.load_benchmark_closes
(pyarrow pushdown). Returns are computed on the COMMON dates of both series.
Sharpe over the trailing 20 common dates: (mean − rf/252)/std·√252, rf 5 %
(unified_backtest's convention); zero variance or < 5 obs -> None.
"""
from __future__ import annotations
import json
import logging
import math
import os
import statistics
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
NAV_HISTORY_PATH = ROOT / 'logs' / 'pnl_daily_ohlc.json'
ANCHOR_KEY = 'bench_realized_anchor'
DEFAULT_ANCHOR = '2026-06-23'
RISK_FREE_DAILY = 0.05 / 252
MIN_COMMON = 5
# Floor below which stdev is treated as float noise from repeated compounding
# (observed ~1e-16..1e-17 on a nominally-constant daily return), not real
# variance (real daily-return sd is >=1e-4). Keeps "constant returns -> None"
# a real contract instead of an exact-bit-for-bit-zero check.
ZERO_VAR_EPS = 1e-12


def load_nav_history(path=None) -> dict[str, float]:
    p = Path(path) if path else NAV_HISTORY_PATH
    days = json.loads(p.read_text()).get('days') or {}
    return {d: float(v['close']) for d, v in days.items() if isinstance(v, dict) and v.get('close') is not None}


def _load_spy_closes(start: str, end: str) -> dict[str, float]:
    from backtest.benchmark_baseline import load_benchmark_closes
    return load_benchmark_closes(start, end, 'SPY')


def _load_anchor(conn) -> str:
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (ANCHOR_KEY,))
            row = cur.fetchone()
        v = str(row[0]).strip() if row and row[0] else ''
        import datetime as _dt
        _dt.date.fromisoformat(v)
        return v
    except Exception:
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
            s_m = regime_benchmark_sharpe_for_sizing(regime, run_date, conn=conn)
    except Exception as e:
        logger.warning('[bench_realized] regime/S_m unavailable: %s', e)
    return regime, s_m


def _sharpe(rets: list[float]):
    if len(rets) < MIN_COMMON:
        return None
    sd = statistics.stdev(rets)
    if not math.isfinite(sd) or sd < ZERO_VAR_EPS:
        return None
    return (statistics.fmean(rets) - RISK_FREE_DAILY) / sd * math.sqrt(252)


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
        'anchor': anchor, 'n_common': len(dates), 'run_date': dates[-1],
        'book_since': book_since, 'spy_since': spy_since, 'gap_pp': (book_since - spy_since) * 100.0,
        'book_20d': _window_return(nav, 20), 'spy_20d': _window_return(spy, 20),
        'book_60d': _window_return(nav, 60), 'spy_60d': _window_return(spy, 60),
        'book_sharpe_20d': _sharpe(nav_r[-20:]), 'spy_sharpe_20d': _sharpe(spy_r[-20:]),
    }


def _pct(v):
    return 'n/a' if v is None else f'{v * 100:+.1f}%'


def _sh(v):
    return 'n/a' if v is None else f'{v:+.2f}'


def format_line(st: dict, regime, s_m) -> str:
    return (f"bench_realized: since={st['anchor']} book={_pct(st['book_since'])} spy={_pct(st['spy_since'])} "
            f"gap={st['gap_pp']:+.1f}pp | 20d book={_pct(st['book_20d'])} spy={_pct(st['spy_20d'])} | "
            f"60d book={_pct(st['book_60d'])} spy={_pct(st['spy_60d'])} | "
            f"regime={regime or 'n/a'} book_sharpe_20d={_sh(st['book_sharpe_20d'])} "
            f"spy_sharpe_20d={_sh(st['spy_sharpe_20d'])} S_m={'n/a' if s_m is None else f'{float(s_m):.3f}'}")


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
