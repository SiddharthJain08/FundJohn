"""benchmark_baseline.py — regime-conditioned benchmark (SPY) Sharpe baseline.

The module computes the benchmark's (SPY) forward, entry-tagged excess Sharpe
(rf 5 %) per regime and holding horizon — the engine's own sleeve estimator
applied to synthetic benchmark lots (Amendment 1, 2026-08-29). Since
2026-08-29 its ONLY consumer is sizing: the per-ticker hurdle `S_adj − S_m`
in execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing, plus the
informational `strategy_backtest_regimes.benchmark_sharpe` column written
by unified_backtest.py. It gates nothing (pre-2026-08-29 promotion gate removed).

Data sources (inspected 2026-08-24):
  - data/master/historical_regimes.parquet — columns: date (date32[day]),
    vix (double), vix_smoothed (double), regime (large_string). ~2.6k rows
    total (one row per trading day since 2016-04-11) — small enough that a
    full read (like unified_backtest.load_regimes) is the established
    pattern; no pyarrow predicate pushdown needed for this file.
  - data/master/prices.parquet — columns include ticker/date (both string)
    and close (double). This file IS the multi-GB master panel, so the
    benchmark close series is read via pyarrow predicate pushdown (ticker +
    date range), the same pattern established in
    src/execution/asset_correlation.py — never load the full panel.

Loaders (load_regime_tags / load_benchmark_closes) are separate top-level
functions specifically so tests can monkeypatch them without touching real
data/master/*.parquet files.

Fail-open contract (binding, per task brief): every failure mode here
resolves to either `{}` (whole-window load failure — logged) or `None` for
an individual regime (thin/no data for that regime), NEVER a raised
exception past regime_benchmark_sharpe. Callers
(execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing /
unified_backtest.py) MUST treat both as "skip the benchmark criterion for
this regime": regime_benchmark_sharpe_for_sizing returns None (the sizer
sizes on raw S_adj) and unified_backtest.py writes a NULL
strategy_backtest_regimes.benchmark_sharpe column — a sleeve with no
benchmark value never blocks on infra absence.
"""
from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

REGIMES_PARQUET = "/root/openclaw/data/master/historical_regimes.parquet"
PRICES_PARQUET = "/root/openclaw/data/master/prices.parquet"

# Mirrors strategies.base.CANONICAL_REGIMES. Duplicated as a plain literal
# (rather than imported) to keep this module dependency-free / independently
# importable — it has no other reason to touch the strategies package.
CANONICAL_REGIMES = ("LOW_VOL", "TRANSITIONING", "HIGH_VOL", "CRISIS")

TRADING_DAYS_PER_YEAR = 252
# Extra calendar-day lookback pulled before start_date so the FIRST tagged
# trading day in-window still yields a close-to-close return (weekends/
# holidays mean a handful of trading days can span >7 calendar days).
_LOOKBACK_CALENDAR_DAYS = 10

# Amendment 1 (spec docs/specs/2026-08-29-bench-sizing-amendment-1-spec.md D-A1..A3).
# rf mirrors unified_backtest.RISK_FREE_DAILY (declared locally: this module is
# deliberately import-free of the backtest engine; equality is unit-tested).
RISK_FREE_ANNUAL = 0.05
RISK_FREE_DAILY = RISK_FREE_ANNUAL / TRADING_DAYS_PER_YEAR
# Horizon grid (trading days a synthetic benchmark lot is held). The sizer
# selects one column via pipeline_config['benchmark_horizon_days'] (default 1).
BENCH_HORIZONS = (1, 2, 3, 5, 10, 21)
DEFAULT_HORIZON = 1


def _to_date_str(d) -> str:
    """Normalize a date-like (str / datetime.date / datetime.datetime /
    pandas.Timestamp) to 'YYYY-MM-DD'."""
    import pandas as pd
    return pd.Timestamp(d).strftime("%Y-%m-%d")


def load_regime_tags(start_date, end_date) -> dict[str, str]:
    """{'YYYY-MM-DD': regime} for every historical_regimes.parquet row in
    [start_date, end_date] inclusive.

    historical_regimes.parquet is small (~2.6k rows, one per trading day
    since 2016) so a full read is the established pattern for this
    particular file (see unified_backtest.load_regimes) — unlike
    prices.parquet, it never needs pyarrow predicate pushdown.
    """
    import pandas as pd
    r = pd.read_parquet(REGIMES_PARQUET, columns=["date", "regime"])
    r["date"] = pd.to_datetime(r["date"])
    lo, hi = pd.Timestamp(_to_date_str(start_date)), pd.Timestamp(_to_date_str(end_date))
    r = r[(r["date"] >= lo) & (r["date"] <= hi)]
    return dict(zip(r["date"].dt.strftime("%Y-%m-%d"), r["regime"].astype(str)))


def load_benchmark_closes(start_date, end_date, benchmark: str) -> dict[str, float]:
    """{'YYYY-MM-DD': close} for `benchmark` in prices.parquet, sliced via
    pyarrow predicate pushdown (ticker + date range) — never loads the full
    panel. See src/execution/asset_correlation.py for the established
    pattern this follows. Pulls _LOOKBACK_CALENDAR_DAYS of extra history
    before start_date so the first in-window tagged day still gets a
    close-to-close return.
    """
    import datetime
    import pyarrow.parquet as pq
    import pyarrow.compute as pc
    lo_dt = datetime.date.fromisoformat(_to_date_str(start_date)) - datetime.timedelta(
        days=_LOOKBACK_CALENDAR_DAYS)
    hi = _to_date_str(end_date)
    flt = ((pc.field("ticker") == benchmark)
           & (pc.field("date") >= lo_dt.isoformat())
           & (pc.field("date") <= hi))
    tbl = pq.read_table(PRICES_PARQUET, columns=["ticker", "date", "close"], filters=flt)
    df = tbl.to_pandas()
    return dict(zip(df["date"].astype(str), df["close"].astype(float)))


def _excess_sharpe(rets: list[float], min_obs: int) -> float | None:
    """(mean − rf_daily) / std(ddof=1) · √252 — the estimator
    unified_backtest.aggregate_metrics applies to a sleeve's daily-marks
    equity curve. None when thin (< min_obs) or degenerate (zero variance)."""
    n = len(rets)
    if n < max(min_obs, 2):
        return None
    mean = sum(rets) / n
    var = sum((x - mean) ** 2 for x in rets) / (n - 1)
    std = math.sqrt(var)
    if std <= 1e-9:
        return None
    return (mean - RISK_FREE_DAILY) / std * math.sqrt(TRADING_DAYS_PER_YEAR)


def regime_benchmark_sharpe_by_horizon(start_date, end_date,
                                       benchmark: str = "SPY",
                                       min_obs: int = 40,
                                       horizons=BENCH_HORIZONS) -> dict[str, dict[int, float | None]]:
    """Forward, entry-tagged benchmark Sharpe per (regime, horizon).

    For each canonical regime and each H in `horizons`: a synthetic lot of
    `benchmark` is entered at the close of EVERY day tagged with that regime
    and held exactly H trading days (no stop/target/cost). The statistic is
    the engine's sleeve estimator (see _excess_sharpe) over the benchmark's
    close-to-close return on every trading day on which at least one such
    lot is open (the equal-weight daily-marks union — identical lots make the
    equal-weight average the plain return). For H = 1 the day set is exactly
    {t+1 : regime(t) = R}: the return a close-of-day decision can capture.

    Pre-amendment this module scored the return ON the tagged day (same-day
    VIX tag ⇒ selection on the outcome, corr(SPY ret, ΔVIX) ≈ −0.79) with
    rf = 0; that statistic (LOW_VOL ≈ 2.0) is not tradeable and is gone.

    Returns {} on any load failure (logged) — callers fail open. Every
    canonical regime is present in the result; a (regime, H) with fewer than
    `min_obs` mark-days (or zero variance) is None.
    """
    try:
        tags = load_regime_tags(start_date, end_date)
        closes = load_benchmark_closes(start_date, end_date, benchmark)
    except Exception as e:
        logger.warning("[bench_baseline] benchmark load failed: %s: %s", type(e).__name__, e)
        return {}
    if not tags or not closes:
        logger.warning("[bench_baseline] benchmark load returned no data "
                        "(regime_tags=%d benchmark_closes=%d)", len(tags), len(closes))
        return {}

    dates = sorted(closes)
    n = len(dates)
    # rets[i] = close-to-close return INTO dates[i]; None for the first day / bad closes.
    rets: list[float | None] = [None] * n
    for i in range(1, n):
        p0, p1 = closes[dates[i - 1]], closes[dates[i]]
        if p0 and p0 == p0 and p1 == p1 and p0 > 0:
            rets[i] = p1 / p0 - 1.0

    hs = sorted({int(h) for h in horizons if int(h) >= 1})
    out: dict[str, dict[int, float | None]] = {r: {} for r in CANONICAL_REGIMES}
    for regime in CANONICAL_REGIMES:
        entries = [i for i, d in enumerate(dates) if tags.get(d) == regime]
        for h in hs:
            marked: set[int] = set()
            for i in entries:
                for k in range(1, h + 1):
                    j = i + k
                    if j >= n:
                        break
                    marked.add(j)
            xs = [rets[j] for j in sorted(marked) if rets[j] is not None]
            out[regime][h] = _excess_sharpe(xs, min_obs)
    return out


def regime_benchmark_sharpe(start_date, end_date,
                            benchmark: str = "SPY",
                            min_obs: int = 40) -> dict[str, float | None]:
    """Flat {regime: Sharpe | None} = the DEFAULT_HORIZON (H = 1) column of
    regime_benchmark_sharpe_by_horizon. Signature and shape unchanged for
    unified_backtest's informational strategy_backtest_regimes.benchmark_sharpe
    write. {} on load failure."""
    by_h = regime_benchmark_sharpe_by_horizon(start_date, end_date, benchmark=benchmark,
                                              min_obs=min_obs, horizons=(DEFAULT_HORIZON,))
    if not by_h:
        return {}
    return {r: (by_h.get(r) or {}).get(DEFAULT_HORIZON) for r in CANONICAL_REGIMES}
