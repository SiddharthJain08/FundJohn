"""benchmark_sizing.py — benchmark-relative sizing rule C (spec §2.5).

Per ticker, after the sizer has rebuilt its sizing basis from the tangency
S_adj (regime_blended_sizer._sharpe_cadence_path, `ticker_w = defaultdict(float,
_size_adj)`):

    benchmark ticker :  w = S_adj                       (beta base, exempt)
    alpha ticker     :  ex = |S_adj| − S_m
                        w = sign(S_adj) · ex   if ex > 0
                        dropped                otherwise (ties drop: "not above the market")

S_m is the benchmark's (SPY) forward, entry-tagged excess Sharpe (rf 5 %, the
engine's sleeve estimator) after closes tagged with the sizer's regime-of-
record, held `benchmark_horizon_days` trading days (default 1 = the daily
decision cadence), computed by backtest.benchmark_baseline.
regime_benchmark_sharpe_by_horizon over the canonical fleet window
(unified_backtest.DEFAULT_START_DATE .. run_date) so it is unit-for-unit with
the sleeve Sharpes S_adj is built from (Amendment 1, 2026-08-29). Cached per
day in pipeline_config['benchmark_regime_sharpe'] for the dashboard and the
intraday lane. Any failure -> None -> the sizer sizes on raw S_adj (fail-open,
logged).

Flag: OPENCLAW_BENCH_RELATIVE_SIZING. Unset/0 = SHADOW (the sizer logs what the
rule would do every cycle, changes nothing). '1' = APPLY.
"""
from __future__ import annotations
import json
import logging
import math
import os

logger = logging.getLogger(__name__)

BENCH_SIZING_ENV = 'OPENCLAW_BENCH_RELATIVE_SIZING'
BETA_BUDGET_ENV = 'OPENCLAW_BENCH_BETA_BUDGET'   # spec 2026-08-30: '1' = redirect rule C's removed conviction to the benchmark
MAX_NAV_FRAC_KEY = 'benchmark_max_nav_frac'       # pipeline_config: benchmark ticker |target| <= frac * NAV under the budget (D-4)
CONFIG_KEY = 'benchmark_regime_sharpe'
CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

CACHE_SCHEMA = 2                      # Amendment 1: by-horizon grid; schema-1 (contemporaneous) payloads are a miss
HORIZON_KEY = 'benchmark_horizon_days'
DEFAULT_HORIZON = 1


def bench_relative_sizing_enabled() -> bool:
    return os.environ.get(BENCH_SIZING_ENV) == '1'


def beta_budget_enabled() -> bool:
    return os.environ.get(BETA_BUDGET_ENV) == '1'


def benchmark_max_nav_frac(default: float = 1.0, conn=None) -> float:
    """pipeline_config.benchmark_max_nav_frac as a positive float; anything
    missing/garbage/non-positive -> default (logged). Own connection when
    conn is None."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (MAX_NAV_FRAC_KEY,))
            row = cur.fetchone()
        if not row or row[0] is None:
            return float(default)
        v = float(str(row[0]).strip())
        if not math.isfinite(v) or v <= 0:
            logger.warning('[bench_sizing] %s=%r not positive; using %s', MAX_NAV_FRAC_KEY, row[0], default)
            return float(default)
        return v
    except Exception as e:
        logger.warning('[bench_sizing] %s unreadable (%s: %s); using %s', MAX_NAV_FRAC_KEY, type(e).__name__, e, default)
        return float(default)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def apply_benchmark_hurdle(ticker_w: dict, s_m, bench_tickers: set) -> tuple[dict, list]:
    """Pure. Returns (hurdled_weights, dropped_tickers). Never mutates ticker_w.
    s_m None -> copy of the input, nothing dropped (fail-open)."""
    if s_m is None:
        return dict(ticker_w), []
    s_m = float(s_m)
    out: dict = {}
    dropped: list = []
    for tkr, s in ticker_w.items():
        s = float(s)
        if tkr in bench_tickers:
            out[tkr] = s
            continue
        ex = abs(s) - s_m
        if ex > 0.0:
            out[tkr] = math.copysign(ex, s)
        else:
            dropped.append(tkr)
    return out, dropped


def apply_beta_budget(before: dict, hurdled: dict, s_m, bench_tickers: set) -> tuple[dict, float]:
    """Pure (spec 2026-08-30 §3.1). Redirect the conviction rule C removed to
    the benchmark tickers so Σ|w| is conserved: every alpha ticker hands
    min(|S_i|, S_m) to the pool (a survivor exactly S_m, a dropped ticker its
    whole |S_i|; shorts too, D-2); the pool is split equally across
    bench_tickers (D-3) on top of their own raw weight.
    before  = the ticker_w handed to apply_benchmark_hurdle
    hurdled = its first return value
    Returns (budgeted_weights, pool). s_m None or no bench_tickers ->
    (dict(hurdled), 0.0). Never mutates its inputs."""
    if s_m is None or not bench_tickers:
        return dict(hurdled), 0.0
    s_m = float(s_m)
    pool = sum(min(abs(float(s)), s_m) for t, s in before.items() if t not in bench_tickers)
    out = dict(hurdled)
    share = pool / len(bench_tickers)
    for b in bench_tickers:
        out[b] = out.get(b, 0.0) + share
    return out, pool


def _read_cache(cur) -> dict | None:
    cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (CONFIG_KEY,))
    row = cur.fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception:
        return None


def _write_cache(conn, payload: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (CONFIG_KEY, json.dumps(payload, sort_keys=True),
             'Benchmark (SPY) forward entry-tagged excess Sharpe (rf 5%) by regime × horizon '
             '(schema 2, amendment 1 2026-08-29) used by the sizer hurdle S_adj − S_m; column '
             'selected by pipeline_config.benchmark_horizon_days. Refreshed once per run_date '
             'by the sizer; window = unified_backtest.DEFAULT_START_DATE .. as_of.'))
    conn.commit()


def load_benchmark_horizon(default: int = DEFAULT_HORIZON, conn=None) -> int:
    """pipeline_config[HORIZON_KEY] as an int on benchmark_baseline.BENCH_HORIZONS.
    Absent, unparseable or off-grid -> `default` (logged). Mirrors
    regime_blended_sizer._load_lambda's read-with-fallback pattern."""
    own = conn is None
    try:
        from backtest.benchmark_baseline import BENCH_HORIZONS
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (HORIZON_KEY,))
            row = cur.fetchone()
        if not row or row[0] is None:
            return int(default)
        raw = str(row[0]).strip().strip('"')
        h = int(float(raw))
        if h not in BENCH_HORIZONS or float(raw) != h:
            logger.warning('[bench_sizing] %s=%r is not on the grid %s; using %d',
                           HORIZON_KEY, row[0], BENCH_HORIZONS, default)
            return int(default)
        return h
    except Exception as e:
        logger.warning('[bench_sizing] %s unreadable (%s: %s); using %d',
                       HORIZON_KEY, type(e).__name__, e, default)
        return int(default)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def regime_benchmark_sharpe_for_sizing(regime_state: str, run_date, *, benchmark: str = 'SPY',
                                       conn=None, compute=None, horizon: int | None = None):
    """S_m for regime_state as of run_date at horizon H, or None. Reuses the
    pipeline_config cache when it is schema-2 and its as_of == run_date (the
    5-minute intraday lane must not re-read the parquet); otherwise computes the
    whole (regime × horizon) grid, persists it, returns the selected column.
    H = `horizon` or pipeline_config['benchmark_horizon_days'] (default 1)."""
    as_of = run_date.strftime('%Y-%m-%d') if hasattr(run_date, 'strftime') else str(run_date)[:10]
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cached = _read_cache(cur)
        if (cached and cached.get('schema') == CACHE_SCHEMA
                and cached.get('as_of') == as_of and cached.get('benchmark') == benchmark):
            by_regime = cached.get('by_regime') or {}
        else:
            from backtest.unified_backtest import DEFAULT_START_DATE
            from backtest.benchmark_baseline import BENCH_HORIZONS
            if compute is None:
                from backtest.benchmark_baseline import regime_benchmark_sharpe_by_horizon as compute
            by_h = compute(DEFAULT_START_DATE, as_of, benchmark=benchmark) or {}
            by_regime = {r: {str(int(h)): (float(v) if v is not None else None)
                             for h, v in (by_h.get(r) or {}).items()}
                         for r in CANONICAL_REGIMES}
            if any(v is not None for hv in by_regime.values() for v in hv.values()):
                _write_cache(conn, {'schema': CACHE_SCHEMA, 'as_of': as_of, 'benchmark': benchmark,
                                    'start': DEFAULT_START_DATE, 'horizons': list(BENCH_HORIZONS),
                                    'by_regime': by_regime})
            else:
                logger.warning('[bench_sizing] S_m compute returned no regimes for %s..%s',
                               DEFAULT_START_DATE, as_of)
                return None
        h = int(horizon) if horizon is not None else load_benchmark_horizon(conn=conn)
        v = (by_regime.get(regime_state) or {}).get(str(h))
        if v is None or not math.isfinite(float(v)):
            return None
        return float(v)
    except Exception as e:
        logger.warning('[bench_sizing] S_m unavailable (%s: %s); sizing on raw S_adj', type(e).__name__, e)
        return None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _shares(w: dict, bench: set) -> tuple[float, float]:
    gross = sum(abs(v) for v in w.values())
    if gross <= 0:
        return 0.0, 0.0
    return gross, sum(abs(v) for t, v in w.items() if t in bench) / gross


def shadow_line(regime_state: str, s_m, before: dict, after: dict, dropped: list,
                bench_tickers: set, lam_nav: float, *, mode: str = 'shadow',
                h: int | None = None) -> str:
    """One line per cycle. Dollar moves are computed by normalizing BOTH books
    to lam_nav (the sizer's Σ|target| = λ·NAV rule) so the diff is in the units
    the book will actually move."""
    g0, beta0 = _shares(before, bench_tickers)
    g1, beta1 = _shares(after, bench_tickers)
    usd0 = {t: (v / g0) * lam_nav for t, v in before.items()} if g0 > 0 else {}
    usd1 = {t: (v / g1) * lam_nav for t, v in after.items()} if g1 > 0 else {}
    moves = sorted(((t, round(usd1.get(t, 0.0) - usd0.get(t, 0.0), 2)) for t in set(usd0) | set(usd1)),
                   key=lambda kv: -abs(kv[1]))
    moved = sum(abs(m) for _, m in moves) / (2.0 * lam_nav) if lam_nav > 0 else 0.0
    s_m_txt = 'None' if s_m is None else f'{float(s_m):.2f}'
    h_txt = '' if h is None else f' h={int(h)}'
    return (f'bench_sizing.{mode}[{regime_state}]: S_m={s_m_txt}{h_txt} bench={sorted(bench_tickers)} '
            f'dropped={len(dropped)}/{len(before)} beta_share_before={beta0:.3f} beta_share_after={beta1:.3f} '
            f'gross_moved_frac={moved:.3f} dropped_tickers={sorted(dropped)[:15]} top_moves={moves[:10]}')
