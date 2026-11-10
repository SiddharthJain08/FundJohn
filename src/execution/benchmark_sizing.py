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

from backtest.risk_free import rf_source as _rf_source

logger = logging.getLogger(__name__)

BENCH_SIZING_ENV = 'OPENCLAW_BENCH_RELATIVE_SIZING'
BETA_BUDGET_ENV = 'OPENCLAW_BENCH_BETA_BUDGET'   # spec 2026-08-30: '1' = redirect rule C's removed conviction to the benchmark
MAX_NAV_FRAC_KEY = 'benchmark_max_nav_frac'       # pipeline_config: benchmark ticker |target| <= frac * NAV under the budget (D-4)
# Crypto engine Phase 3 (2026-11-10, spec §9 operator decisions): per-class caps.
MAX_NAV_FRAC_KEY_CRYPTO = 'benchmark_max_nav_frac_crypto'   # BTC-USD sleeve |target| <= frac * NAV
MAX_NAV_FRAC_CRYPTO_DEFAULT = 0.25
CRYPTO_GROSS_MAX_NAV_FRAC_KEY = 'crypto_gross_max_nav_frac'  # Σ|crypto targets| <= frac * NAV
CRYPTO_GROSS_MAX_NAV_FRAC_DEFAULT = 0.50
CORR_REMOVAL_ENV = 'OPENCLAW_BENCH_CORR_REMOVAL'  # '0' kills the benchmark-correlation removal (operator directive 2026-09-04)
CORR_REMOVAL_THR_KEY = 'benchmark_corr_removal_thr'  # pipeline_config: |corr(t, SPY)| >= thr => remove t outright; <=0 or >=1 disables
CONFIG_KEY = 'benchmark_regime_sharpe'
CANONICAL_REGIMES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')

# Schema 3 (crypto engine Phase 3, 2026-11-10): one entry PER BENCHMARK,
#   {'schema': 3, 'rf_source': <source of the last write>,
#    'benchmarks': {'SPY': {as_of, benchmark, start, horizons, by_regime, rf_source,
#                           regime_source, trading_days}, 'BTC-USD': {...}}}
# A schema-2 payload (Amendment 1: one by-horizon grid for ONE benchmark) is
# read as benchmarks={payload.benchmark: payload} and rewritten as 3 on the
# next write; schema-1 (contemporaneous, no 'schema' key) is a miss. Entries
# are validated individually (as_of, rf_source, regime_source), so one
# benchmark's refresh never evicts or invalidates another's.
CACHE_SCHEMA = 3
# Benchmark -> regime partition its S_m is computed on (and the crypto class).
BENCHMARK_REGIME_SOURCE = {'BTC-USD': 'crypto'}
HORIZON_KEY = 'benchmark_horizon_days'
DEFAULT_HORIZON = 1


def bench_relative_sizing_enabled() -> bool:
    return os.environ.get(BENCH_SIZING_ENV) == '1'


def beta_budget_enabled() -> bool:
    return os.environ.get(BETA_BUDGET_ENV) == '1'


def _positive_config_float(key: str, default: float, conn=None) -> float:
    """pipeline_config[key] as a positive finite float; missing -> default,
    garbage/non-positive -> default (logged). Own connection when conn is None."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (key,))
            row = cur.fetchone()
        if not row or row[0] is None:
            return float(default)
        v = float(str(row[0]).strip())
        if not math.isfinite(v) or v <= 0:
            logger.warning('[bench_sizing] %s=%r not positive; using %s', key, row[0], default)
            return float(default)
        return v
    except Exception as e:
        logger.warning('[bench_sizing] %s unreadable (%s: %s); using %s', key, type(e).__name__, e, default)
        return float(default)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def crypto_gross_max_nav_frac(default: float = CRYPTO_GROSS_MAX_NAV_FRAC_DEFAULT, conn=None) -> float:
    """pipeline_config.crypto_gross_max_nav_frac (Σ|crypto target| / NAV cap,
    default 0.50); missing/garbage/non-positive -> default."""
    return _positive_config_float(CRYPTO_GROSS_MAX_NAV_FRAC_KEY, default, conn)


def benchmark_max_nav_frac(default: float = 1.0, conn=None, *, instrument_class: str = 'equity') -> float:
    """pipeline_config.benchmark_max_nav_frac as a positive float; anything
    missing/garbage/non-positive -> default (logged). Own connection when
    conn is None.

    instrument_class='crypto' (Phase 3) reads benchmark_max_nav_frac_crypto
    instead, defaulting to 0.25 regardless of `default` (which is the EQUITY
    default — existing no-arg callers are unchanged); the equity key never
    applies to the crypto class."""
    if instrument_class == 'crypto':
        return _positive_config_float(MAX_NAV_FRAC_KEY_CRYPTO, MAX_NAV_FRAC_CRYPTO_DEFAULT, conn)
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
        if v > 1.0:
            # Spec §8 allows this as a config value (it is not a code path), but
            # it silently un-does the "the reference portfolio is UNLEVERED"
            # rationale of the §3.4 cap — never let it pass unannounced.
            logger.warning('[bench_sizing] %s=%.3f > 1.0: the reference portfolio is being levered',
                           MAX_NAV_FRAC_KEY, v)
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


def bench_corr_removal_thr(default: float = 0.60, conn=None) -> float:
    """pipeline_config.benchmark_corr_removal_thr as a float in (0, 1);
    missing/garbage -> default. A stored value <= 0 or >= 1 is an explicit
    OPERATOR DISABLE and is returned as-is (the caller treats it as off)."""
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        with conn.cursor() as cur:
            cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (CORR_REMOVAL_THR_KEY,))
            row = cur.fetchone()
        if not row or row[0] is None:
            return float(default)
        v = float(str(row[0]).strip())
        if not math.isfinite(v):
            logger.warning('[bench_sizing] %s=%r not finite; using %s', CORR_REMOVAL_THR_KEY, row[0], default)
            return float(default)
        return v
    except Exception as e:
        logger.warning('[bench_sizing] %s unreadable (%s: %s); using %s', CORR_REMOVAL_THR_KEY, type(e).__name__, e, default)
        return float(default)
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def remove_benchmark_correlated(target_usd: dict, bench: str, corr: dict, thr: float) -> tuple[dict, dict]:
    """Pure (operator directive 2026-09-04). With the beta budget holding the
    market through the benchmark ticker, an alpha name highly correlated with
    it is redundant beta, not diversification — and since benchmark tickers
    are excluded from the correlation clustering (spec 2026-08-29 D6), the
    20% cluster budget can no longer see that redundancy. So: remove every
    alpha ticker with |corr(t, bench)| >= thr OUTRIGHT (held names become
    orphan-closes downstream; conviction is NOT redirected). corr is the
    {ticker: rho-vs-bench} map from asset_correlation.benchmark_corr — a
    ticker absent from it (no return history) is never removed.
    Returns (kept_targets, removed_targets). Never mutates its input."""
    removed = {t: v for t, v in target_usd.items()
               if t != bench and v and abs(corr.get(t, 0.0)) >= thr}
    kept = {t: v for t, v in target_usd.items() if t not in removed}
    return kept, removed


def apply_benchmark_hurdle(ticker_w: dict, s_m, bench_tickers: set, *,
                           floor_zero: bool = False) -> tuple[dict, list]:
    """Pure. Returns (hurdled_weights, dropped_tickers). Never mutates ticker_w.
    s_m None -> copy of the input, nothing dropped (fail-open).

    floor_zero (crypto class, Phase 3): the hurdle is max(S_m, 0) — a negative
    BTC S_m (the market losing to cash) must not INFLATE every alt by |S_m|.
    Off = equity semantics, unchanged."""
    if s_m is None:
        return dict(ticker_w), []
    s_m = float(s_m)
    if floor_zero:
        s_m = max(s_m, 0.0)
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
    # Final fix wave (2026-08-30) #1: `bench_tickers` is qualified on the sizer's
    # GATE map sign (_net_signs(gate_net_sharpe, ...)) while `before` is the SIZE
    # map (_size_adj); with OPENCLAW_STRATEGY_SIZE_SCALAR=1 the two can disagree,
    # so a "net-long-qualified" benchmark ticker can be net-SHORT here. The pool
    # is unsigned and would then flip it long — a silent sign inversion of the
    # beta base. Skip the budget for the cycle instead (rule C unchanged).
    _short = sorted(b for b in bench_tickers if float(before.get(b, 0.0)) < 0.0)
    if _short:
        logger.warning('[bench_sizing] benchmark ticker net-short in the size map (%s); '
                       'beta budget skipped this cycle (rule C unchanged)', _short)
        return dict(hurdled), 0.0
    s_m = float(s_m)
    pool = sum(min(abs(float(s)), s_m) for t, s in before.items() if t not in bench_tickers)
    out = dict(hurdled)
    share = pool / len(bench_tickers)
    for b in bench_tickers:
        out[b] = out.get(b, 0.0) + share
    return out, pool


def _empty_cache() -> dict:
    return {'schema': CACHE_SCHEMA, 'rf_source': None, 'benchmarks': {}}


def _read_cache(conn) -> dict:
    """The cache as schema 3, always a dict: absent/unparseable/schema-1/
    unknown schema -> {'schema': 3, 'rf_source': None, 'benchmarks': {}};
    schema 2 -> {'schema': 3, 'rf_source': payload.rf_source,
    'benchmarks': {payload.benchmark: payload}}. Read-only (the upgrade is
    persisted by the next _write_cache)."""
    with conn.cursor() as cur:
        cur.execute('SELECT value FROM pipeline_config WHERE key = %s', (CONFIG_KEY,))
        row = cur.fetchone()
    if not row or not row[0]:
        return _empty_cache()
    try:
        raw = json.loads(row[0]) if isinstance(row[0], str) else dict(row[0])
    except Exception:
        return _empty_cache()
    if not isinstance(raw, dict):
        return _empty_cache()
    schema = raw.get('schema')
    if schema == CACHE_SCHEMA and isinstance(raw.get('benchmarks'), dict):
        return {'schema': CACHE_SCHEMA, 'rf_source': raw.get('rf_source'),
                'benchmarks': {str(b): e for b, e in raw['benchmarks'].items() if isinstance(e, dict)}}
    if schema == 2 and raw.get('benchmark'):
        return {'schema': CACHE_SCHEMA, 'rf_source': raw.get('rf_source'),
                'benchmarks': {str(raw['benchmark']): raw}}
    return _empty_cache()


def _entry_valid(entry, as_of: str, regime_source: str) -> bool:
    """A per-benchmark cache entry is a hit iff it is for this as_of, this rf
    source and this regime partition (an upgraded schema-2 entry carries no
    regime_source: it was computed on the equity partition)."""
    return (isinstance(entry, dict) and entry.get('as_of') == as_of
            and entry.get('rf_source') == _rf_source()
            and (entry.get('regime_source') or 'equity') == regime_source
            and isinstance(entry.get('by_regime'), dict))


def _write_cache(conn, benchmark: str, payload: dict) -> None:
    """Merge `payload` into benchmarks[benchmark] of the current cache (read
    fresh, schema-2 upgraded) and persist it as schema 3. Every other
    benchmark's entry is carried over untouched."""
    cache = _read_cache(conn)
    benchmarks = dict(cache.get('benchmarks') or {})
    benchmarks[str(benchmark)] = payload
    doc = {'schema': CACHE_SCHEMA, 'rf_source': _rf_source(), 'benchmarks': benchmarks}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO pipeline_config (key, value, description, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (key) DO UPDATE
               SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (CONFIG_KEY, json.dumps(doc, sort_keys=True),
             'Benchmark forward entry-tagged excess Sharpe by regime × horizon, keyed by '
             'benchmark (schema 3, crypto engine Phase 3 2026-11-10: SPY on the equity regime '
             'partition / 252 d, BTC-USD on the crypto partition / 365 d) used by the sizer '
             'hurdle S_adj − S_m; column selected by pipeline_config.benchmark_horizon_days. '
             'Each entry refreshed once per run_date; window = '
             'unified_backtest.DEFAULT_START_DATE .. as_of.'))
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


def benchmark_regime_source(benchmark: str) -> str:
    """'crypto' for BTC-USD, else 'equity'."""
    return BENCHMARK_REGIME_SOURCE.get(str(benchmark), 'equity')


def regime_benchmark_sharpe_for_sizing(regime_state: str, run_date, *, benchmark: str = 'SPY',
                                       conn=None, compute=None, horizon: int | None = None,
                                       regime_source: str | None = None):
    """S_m for regime_state as of run_date at horizon H, or None. Reuses this
    benchmark's entry of the pipeline_config cache when its as_of == run_date
    (and rf source / regime partition match) — the 5-minute intraday lane must
    not re-read the parquet; otherwise computes the whole (regime × horizon)
    grid, merges it into the cache under `benchmark`, returns the selected
    column. H = `horizon` or pipeline_config['benchmark_horizon_days'] (default 1).

    regime_source defaults by benchmark ('crypto' for BTC-USD — S_m on the
    crypto partition, 365-day — else 'equity'); regime_state is then a label
    of THAT partition. The raw value is returned (may be negative): the
    crypto class's max(S_m, 0) floor is applied by its consumers
    (apply_benchmark_hurdle floor_zero / the sleeve weight)."""
    as_of = run_date.strftime('%Y-%m-%d') if hasattr(run_date, 'strftime') else str(run_date)[:10]
    src = regime_source or benchmark_regime_source(benchmark)
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        entry = (_read_cache(conn).get('benchmarks') or {}).get(benchmark)
        if _entry_valid(entry, as_of, src):
            by_regime = entry.get('by_regime') or {}
        else:
            from backtest.unified_backtest import DEFAULT_START_DATE
            from backtest.benchmark_baseline import (BENCH_HORIZONS, CRYPTO_TRADING_DAYS_PER_YEAR,
                                                     TRADING_DAYS_PER_YEAR)
            if compute is None:
                from backtest.benchmark_baseline import regime_benchmark_sharpe_by_horizon as compute
            # regime_source is passed only off the equity default so equity
            # callers (and their fakes) see the pre-Phase-3 call unchanged.
            kw = {} if src == 'equity' else {'regime_source': src}
            by_h = compute(DEFAULT_START_DATE, as_of, benchmark=benchmark, **kw) or {}
            by_regime = {r: {str(int(h)): (float(v) if v is not None else None)
                             for h, v in (by_h.get(r) or {}).items()}
                         for r in CANONICAL_REGIMES}
            if any(v is not None for hv in by_regime.values() for v in hv.values()):
                _write_cache(conn, benchmark, {
                    'as_of': as_of, 'benchmark': benchmark, 'start': DEFAULT_START_DATE,
                    'horizons': list(BENCH_HORIZONS), 'by_regime': by_regime,
                    'rf_source': _rf_source(), 'regime_source': src,
                    'trading_days': (CRYPTO_TRADING_DAYS_PER_YEAR if src == 'crypto'
                                     else TRADING_DAYS_PER_YEAR)})
            else:
                logger.warning('[bench_sizing] S_m compute returned no regimes for %s %s..%s',
                               benchmark, DEFAULT_START_DATE, as_of)
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
                h: int | None = None, budgeted: dict | None = None,
                beta_pool: float = 0.0, budget_mode: str = 'shadow',
                beta_usd_cap: float | None = None) -> str:
    """One line per cycle. Dollar moves are computed by normalizing BOTH books
    to lam_nav (the sizer's Σ|target| = λ·NAV rule) so the diff is in the units
    the book will actually move. `budgeted` (spec 2026-08-30 §3.3) appends the
    beta-budget fields; omitted -> byte-identical to the pre-budget format.
    `beta_usd_cap` (final fix wave, 2026-08-30) is the §3.4 NAV cap in dollars
    (benchmark_max_nav_frac × NAV): given, the capped figure the book will
    actually carry is printed beside the pre-cap one; omitted -> field absent."""
    g0, beta0 = _shares(before, bench_tickers)
    g1, beta1 = _shares(after, bench_tickers)
    usd0 = {t: (v / g0) * lam_nav for t, v in before.items()} if g0 > 0 else {}
    usd1 = {t: (v / g1) * lam_nav for t, v in after.items()} if g1 > 0 else {}
    moves = sorted(((t, round(usd1.get(t, 0.0) - usd0.get(t, 0.0), 2)) for t in set(usd0) | set(usd1)),
                   key=lambda kv: -abs(kv[1]))
    moved = sum(abs(m) for _, m in moves) / (2.0 * lam_nav) if lam_nav > 0 else 0.0
    s_m_txt = 'None' if s_m is None else f'{float(s_m):.2f}'
    h_txt = '' if h is None else f' h={int(h)}'
    budget_txt = ''
    if budgeted is not None:
        _, beta_b = _shares(budgeted, bench_tickers)
        budget_txt = (f' beta_budget={budget_mode} pool={float(beta_pool):.1f} '
                      f'beta_share_budget={beta_b:.3f} beta_usd_budget={beta_b * lam_nav:.0f}')
        if beta_usd_cap is not None:
            budget_txt += f' beta_usd_budget_capped={min(beta_b * lam_nav, float(beta_usd_cap)):.0f}'
    return (f'bench_sizing.{mode}[{regime_state}]: S_m={s_m_txt}{h_txt} bench={sorted(bench_tickers)} '
            f'dropped={len(dropped)}/{len(before)} beta_share_before={beta0:.3f} beta_share_after={beta1:.3f} '
            f'gross_moved_frac={moved:.3f}{budget_txt} dropped_tickers={sorted(dropped)[:15]} top_moves={moves[:10]}')
