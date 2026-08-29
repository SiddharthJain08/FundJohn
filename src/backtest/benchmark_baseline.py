"""benchmark_baseline.py — regime-conditioned benchmark (SPY) Sharpe baseline.

The module computes the benchmark's (SPY) annualized Sharpe over days tagged
with each regime. Since 2026-08-29 (spec docs/specs/2026-08-29-benchmark-
relative-sizing-spec.md D1/§2.5), its ONLY consumer is sizing: the per-ticker
hurdle `S_adj − S_m` in execution.benchmark_sizing.regime_benchmark_sharpe_for_sizing,
plus the informational `strategy_backtest_regimes.benchmark_sharpe` column written
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


def regime_benchmark_sharpe(start_date, end_date,
                            benchmark: str = "SPY",
                            min_obs: int = 40) -> dict[str, float | None]:
    """Annualized Sharpe (mean/std * sqrt(252), rf=0) of the benchmark's
    close-to-close daily returns computed separately over the days tagged
    with each regime in [start_date, end_date].

    Regime tags: data/master/historical_regimes.parquet. Prices:
    data/master/prices.parquet sliced via pyarrow filters. Regimes with
    < min_obs tagged days (i.e. fewer than min_obs usable close-to-close
    returns falling on that regime's tagged days) -> None.

    Returns {} on any load failure (logged) — callers fail open.
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
    by_regime: dict[str, list[float]] = {r: [] for r in CANONICAL_REGIMES}
    for i in range(1, len(dates)):
        d0, d1 = dates[i - 1], dates[i]
        p0, p1 = closes[d0], closes[d1]
        if not (p0 and p0 == p0 and p1 == p1):     # skip zero/NaN closes
            continue
        regime = tags.get(d1)
        if regime in by_regime:
            by_regime[regime].append(p1 / p0 - 1.0)

    out: dict[str, float | None] = {}
    for regime, rets in by_regime.items():
        n = len(rets)
        if n < min_obs:
            out[regime] = None
            continue
        mean = sum(rets) / n
        var = sum((x - mean) ** 2 for x in rets) / (n - 1) if n > 1 else 0.0
        std = math.sqrt(var)
        out[regime] = (mean / std * math.sqrt(TRADING_DAYS_PER_YEAR)) if std > 1e-9 else None
    return out
