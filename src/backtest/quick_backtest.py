#!/usr/bin/env python3
"""quick_backtest.py — fast single-window walk-forward for MasterMindJohn drafts.

Takes a `strategy_staging.id`, looks up the row, maps the staging row's `name`
(which should match a canonical slug) to a signal generator, runs a single
OOS window (2019-01-01 → 2022-12-31 by default), and writes the result JSON
back to `strategy_staging.quick_backtest_json`.

Pure Python — no LLM calls. Runs in ~4-8 seconds for canonical templates
on SP500 monthly data. Designed to be invoked as:

    nohup python3 /root/openclaw/src/backtest/quick_backtest.py \\
      --staging-id <uuid> &

Success writes:
  quick_backtest_json = {
    "window": "2019-01-01..2022-12-31",
    "sharpe": 0.68,
    "max_dd": 0.18,
    "total_return_pct": 38.4,
    "trade_count": 96,
    "slug": "momentum_12_1",
    "universe_size": 454,
    "runtime_seconds": 5.2,
    "completed_at": "2026-04-23T23:17:00Z"
  }

Deferred (non-canonical slug or missing data) writes a JSON with
`{status:"deferred", reason:"..."}` so the UI can show "full pipeline pending"
rather than an error.

Error writes `quick_backtest_error = "<msg>"` and a JSON with `status:"error"`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

PG_URI = os.environ.get(
    "POSTGRES_URI", "postgresql://openclaw:password@localhost:5432/openclaw"
)
PARQUET_ROOT = os.environ.get("OPENCLAW_PARQUET_ROOT", "/root/openclaw/data/master")

# Single OOS window — fast sanity check. Full walk-forward (3 windows) comes
# from `auto_backtest.py` once StrategyCoder writes the .py file.
# Note: prices.parquet has sparse pre-2024 coverage (~38 tickers). Window is
# chosen to match the dense regime (~400 tickers from mid-2024 onward).
DEFAULT_WINDOW_START = "2024-04-01"
DEFAULT_WINDOW_END   = "2026-04-01"
MIN_CROSS_SECTION    = 20   # minimum tickers present to form a decile split

TRADING_DAYS = 252


# ── Canonical template library ───────────────────────────────────────────────
# Each template takes (wide_prices, params) → DataFrame of weights
# (index=rebalance_date, columns=ticker, values=weight in [-1, 1])
# Rebalance frequency is encoded by which dates appear in the output index.

def _tpl_momentum_n_m(wide: pd.DataFrame, params: dict, lookback: int, skip: int):
    """Generic N-M momentum: rank by total return over lookback months ending
    `skip` months ago; long top decile, short bottom decile. Monthly rebalance."""
    lookback_d = lookback * 21
    skip_d     = skip * 21
    monthly    = wide.resample("ME").last()
    # Shift by skip months, then compute lookback return
    ret = monthly.pct_change(lookback).shift(skip)
    weights = pd.DataFrame(index=monthly.index, columns=monthly.columns, dtype=float).fillna(0.0)
    for dt in monthly.index:
        row = ret.loc[dt].dropna()
        if len(row) < MIN_CROSS_SECTION:
            continue
        lo = row.quantile(0.10)
        hi = row.quantile(0.90)
        longs  = row[row >= hi]
        shorts = row[row <= lo]
        if len(longs) == 0 or len(shorts) == 0:
            continue
        w = pd.Series(0.0, index=monthly.columns)
        if len(longs):  w[longs.index]  =  1.0 / max(1, len(longs))
        if len(shorts): w[shorts.index] = -1.0 / max(1, len(shorts))
        weights.loc[dt] = w
    return weights

def tpl_momentum_12_1(wide, params): return _tpl_momentum_n_m(wide, params, 12, 1)
def tpl_momentum_6_1(wide, params):  return _tpl_momentum_n_m(wide, params, 6, 1)
def tpl_momentum_3_1(wide, params):  return _tpl_momentum_n_m(wide, params, 3, 1)

def tpl_momentum_risk_adjusted(wide, params):
    """12-1 momentum divided by realized vol (annualised 252d)."""
    monthly = wide.resample("ME").last()
    ret12   = monthly.pct_change(12).shift(1)
    daily_ret = wide.pct_change()
    vol252 = daily_ret.rolling(252, min_periods=60).std() * np.sqrt(TRADING_DAYS)
    vol_m  = vol252.resample("ME").last().shift(1)
    score  = ret12 / (vol_m + 1e-6)
    weights = pd.DataFrame(index=monthly.index, columns=monthly.columns, dtype=float).fillna(0.0)
    for dt in monthly.index:
        row = score.loc[dt].dropna()
        if len(row) < MIN_CROSS_SECTION: continue
        longs  = row[row >= row.quantile(0.90)]
        shorts = row[row <= row.quantile(0.10)]
        w = pd.Series(0.0, index=monthly.columns)
        if len(longs):  w[longs.index]  =  1.0 / len(longs)
        if len(shorts): w[shorts.index] = -1.0 / len(shorts)
        weights.loc[dt] = w
    return weights

def tpl_low_volatility_us(wide, params):
    """Long bottom decile of 60-day realized vol; monthly rebalance."""
    daily_ret = wide.pct_change()
    vol60     = daily_ret.rolling(60, min_periods=30).std() * np.sqrt(TRADING_DAYS)
    vol_m     = vol60.resample("ME").last()
    weights = pd.DataFrame(index=vol_m.index, columns=vol_m.columns, dtype=float).fillna(0.0)
    for dt in vol_m.index:
        row = vol_m.loc[dt].dropna()
        if len(row) < MIN_CROSS_SECTION: continue
        longs = row[row <= row.quantile(0.10)]
        w = pd.Series(0.0, index=vol_m.columns)
        if len(longs): w[longs.index] = 1.0 / len(longs)
        weights.loc[dt] = w
    return weights

def tpl_mean_reversion_short(wide, params):
    """Short 5-day winners, long 5-day losers; weekly rebalance."""
    ret5 = wide.pct_change(5)
    weekly = ret5.resample("W-FRI").last()
    weights = pd.DataFrame(index=weekly.index, columns=weekly.columns, dtype=float).fillna(0.0)
    for dt in weekly.index:
        row = weekly.loc[dt].dropna()
        if len(row) < MIN_CROSS_SECTION: continue
        longs  = row[row <= row.quantile(0.10)]   # biggest losers → buy
        shorts = row[row >= row.quantile(0.90)]   # biggest winners → sell
        w = pd.Series(0.0, index=weekly.columns)
        if len(longs):  w[longs.index]  =  1.0 / len(longs)
        if len(shorts): w[shorts.index] = -1.0 / len(shorts)
        weights.loc[dt] = w
    return weights

def tpl_mean_reversion_weekly(wide, params):
    """Short 20-day winners, long 20-day losers; monthly rebalance."""
    ret20   = wide.pct_change(20)
    monthly = ret20.resample("ME").last()
    weights = pd.DataFrame(index=monthly.index, columns=monthly.columns, dtype=float).fillna(0.0)
    for dt in monthly.index:
        row = monthly.loc[dt].dropna()
        if len(row) < MIN_CROSS_SECTION: continue
        longs  = row[row <= row.quantile(0.10)]
        shorts = row[row >= row.quantile(0.90)]
        w = pd.Series(0.0, index=monthly.columns)
        if len(longs):  w[longs.index]  =  1.0 / len(longs)
        if len(shorts): w[shorts.index] = -1.0 / len(shorts)
        weights.loc[dt] = w
    return weights

def tpl_trend_12m_crossover(wide, params):
    """Long if price > 12-month (252d) SMA; equal-weight monthly."""
    sma252  = wide.rolling(252, min_periods=60).mean()
    monthly = wide.resample("ME").last()
    sma_m   = sma252.resample("ME").last()
    weights = pd.DataFrame(index=monthly.index, columns=monthly.columns, dtype=float).fillna(0.0)
    for dt in monthly.index:
        p  = monthly.loc[dt].dropna()
        s  = sma_m.loc[dt].dropna()
        both = p.index.intersection(s.index)
        if len(both) < MIN_CROSS_SECTION: continue
        longs = both[p.loc[both] > s.loc[both]]
        w = pd.Series(0.0, index=monthly.columns)
        if len(longs): w[longs] = 1.0 / len(longs)
        weights.loc[dt] = w
    return weights

def tpl_trend_200d_crossover(wide, params):
    """Long if price > 200d SMA; daily rebalance (weekly in practice)."""
    sma200 = wide.rolling(200, min_periods=50).mean()
    weekly  = wide.resample("W-FRI").last()
    sma_w   = sma200.resample("W-FRI").last()
    weights = pd.DataFrame(index=weekly.index, columns=weekly.columns, dtype=float).fillna(0.0)
    for dt in weekly.index:
        p = weekly.loc[dt].dropna()
        s = sma_w.loc[dt].dropna()
        both = p.index.intersection(s.index)
        if len(both) < MIN_CROSS_SECTION: continue
        longs = both[p.loc[both] > s.loc[both]]
        w = pd.Series(0.0, index=weekly.columns)
        if len(longs): w[longs] = 1.0 / len(longs)
        weights.loc[dt] = w
    return weights

def tpl_idiosyncratic_vol(wide, params):
    """Long bottom decile of idiosyncratic vol (residual vs equal-weight mkt)."""
    daily_ret = wide.pct_change()
    mkt = daily_ret.mean(axis=1)
    # residual = stock_ret - market_ret (beta assumed 1 for speed)
    resid     = daily_ret.sub(mkt, axis=0)
    ivol60    = resid.rolling(60, min_periods=30).std() * np.sqrt(TRADING_DAYS)
    ivol_m    = ivol60.resample("ME").last()
    weights = pd.DataFrame(index=ivol_m.index, columns=ivol_m.columns, dtype=float).fillna(0.0)
    for dt in ivol_m.index:
        row = ivol_m.loc[dt].dropna()
        if len(row) < MIN_CROSS_SECTION: continue
        longs = row[row <= row.quantile(0.10)]
        w = pd.Series(0.0, index=ivol_m.columns)
        if len(longs): w[longs.index] = 1.0 / len(longs)
        weights.loc[dt] = w
    return weights

def tpl_beta_anomaly_bab(wide, params):
    """Betting-against-beta: long low-beta, short high-beta; monthly."""
    daily_ret = wide.pct_change()
    mkt = daily_ret.mean(axis=1)
    # rolling 252d beta via cov/var
    cov252 = daily_ret.rolling(252, min_periods=60).cov(mkt)
    var252 = mkt.rolling(252, min_periods=60).var()
    beta   = cov252.div(var252, axis=0)
    beta_m = beta.resample("ME").last()
    weights = pd.DataFrame(index=beta_m.index, columns=beta_m.columns, dtype=float).fillna(0.0)
    for dt in beta_m.index:
        row = beta_m.loc[dt].dropna()
        if len(row) < MIN_CROSS_SECTION: continue
        longs  = row[row <= row.quantile(0.10)]
        shorts = row[row >= row.quantile(0.90)]
        w = pd.Series(0.0, index=beta_m.columns)
        if len(longs):  w[longs.index]  =  1.0 / len(longs)
        if len(shorts): w[shorts.index] = -1.0 / len(shorts)
        weights.loc[dt] = w
    return weights


CANONICAL_TEMPLATES = {
    "momentum_12_1":              tpl_momentum_12_1,
    "momentum_6_1":               tpl_momentum_6_1,
    "momentum_3_1":               tpl_momentum_3_1,
    "momentum_risk_adjusted":     tpl_momentum_risk_adjusted,
    "low_volatility_us":          tpl_low_volatility_us,
    "mean_reversion_short":       tpl_mean_reversion_short,
    "mean_reversion_weekly":      tpl_mean_reversion_weekly,
    "trend_12m_crossover":        tpl_trend_12m_crossover,
    "trend_200d_crossover":       tpl_trend_200d_crossover,
    "idiosyncratic_vol":          tpl_idiosyncratic_vol,
    "beta_anomaly_betting_against_beta": tpl_beta_anomaly_bab,
}


# ── Backtest engine ──────────────────────────────────────────────────────────

def _load_prices() -> pd.DataFrame:
    long = pd.read_parquet(os.path.join(PARQUET_ROOT, "prices.parquet"))
    wide = long.pivot_table(index="date", columns="ticker", values="close")
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def _backtest_weights(weights: pd.DataFrame, wide_prices: pd.DataFrame,
                      start: str, end: str) -> dict:
    """Score a weights DataFrame. Returns metrics dict."""
    daily_ret = wide_prices.pct_change().fillna(0.0)
    daily_ret = daily_ret.loc[start:end]
    # Forward-fill weights to daily frequency within the window
    aligned_idx = daily_ret.index
    w_daily = weights.reindex(aligned_idx, method="ffill").fillna(0.0)
    # Align columns
    common_cols = daily_ret.columns.intersection(w_daily.columns)
    w_daily  = w_daily[common_cols]
    ret      = daily_ret[common_cols]
    # Shift weights by 1 so rebalance day i uses realised weights starting day i+1
    w_daily = w_daily.shift(1).fillna(0.0)
    port_ret = (w_daily * ret).sum(axis=1)
    if port_ret.abs().sum() < 1e-9:
        return {"sharpe": 0.0, "max_dd": 0.0, "total_return_pct": 0.0,
                "trade_count": 0, "note": "no active weights in window"}
    # Rough trade count = number of non-zero weight changes across rebalance dates
    rebal_dates = weights.loc[start:end].index
    w_at_rebal  = weights.reindex(rebal_dates).fillna(0.0)
    trade_count = int((w_at_rebal.diff().abs() > 1e-6).sum().sum())
    sharpe   = float(port_ret.mean() / (port_ret.std() + 1e-9) * np.sqrt(TRADING_DAYS))
    cum      = (1.0 + port_ret).cumprod()
    roll_max = cum.cummax()
    dd       = (cum - roll_max) / (roll_max + 1e-9)
    max_dd   = float(abs(dd.min()))
    total_ret_pct = float((cum.iloc[-1] - 1.0) * 100.0)
    return {
        "sharpe": round(sharpe, 4),
        "max_dd": round(max_dd, 4),
        "total_return_pct": round(total_ret_pct, 2),
        "trade_count": trade_count,
    }


def _universe_filter(wide: pd.DataFrame) -> pd.DataFrame:
    """Drop futures / crypto / fx symbols — keep equities only."""
    cols = [c for c in wide.columns
            if not c.startswith("^") and "-USD" not in c and "=F" not in c]
    return wide[cols]


def run_for_slug(slug: str, params: dict, start: str, end: str) -> dict:
    tpl = CANONICAL_TEMPLATES.get(slug)
    if tpl is None:
        return {
            "status": "deferred",
            "reason": "non_canonical_slug",
            "slug": slug,
            "message": "No canonical template; full backtest will run once StrategyCoder writes the .py file.",
        }
    t0 = time.time()
    wide = _universe_filter(_load_prices())
    if wide.empty:
        return {"status": "error", "reason": "no_prices", "slug": slug}
    weights = tpl(wide, params or {})
    metrics = _backtest_weights(weights, wide, start, end)
    metrics.update({
        "status":        "ok",
        "slug":          slug,
        "window":        f"{start}..{end}",
        "universe_size": int(wide.shape[1]),
        "runtime_seconds": round(time.time() - t0, 2),
        "completed_at":  datetime.now(timezone.utc).isoformat(),
    })
    return metrics


# ── DB glue ──────────────────────────────────────────────────────────────────

def _fetch_staging_row(staging_id: str) -> dict | None:
    with psycopg2.connect(PG_URI, connect_timeout=5) as conn, \
         conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, name, parameters FROM strategy_staging WHERE id = %s",
            (staging_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def _write_result(staging_id: str, result: dict, error: str | None = None) -> None:
    with psycopg2.connect(PG_URI, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            """UPDATE strategy_staging
                  SET quick_backtest_json  = %s::jsonb,
                      quick_backtest_error = %s
                WHERE id = %s""",
            (json.dumps(result), error, staging_id),
        )
        conn.commit()


def _mark_started(staging_id: str) -> None:
    with psycopg2.connect(PG_URI, connect_timeout=5) as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE strategy_staging SET quick_backtest_started_at = NOW() "
            "WHERE id = %s AND quick_backtest_json IS NULL",
            (staging_id,),
        )
        conn.commit()


def run_for_staging(staging_id: str, start: str, end: str) -> int:
    row = _fetch_staging_row(staging_id)
    if not row:
        print(f"[quick_backtest] staging_id not found: {staging_id}", file=sys.stderr)
        return 1
    _mark_started(staging_id)
    slug = row["name"]
    params = row.get("parameters") or {}
    try:
        result = run_for_slug(slug, params, start, end)
        _write_result(staging_id, result, error=None if result.get("status") == "ok" else result.get("reason"))
        print(json.dumps(result))
        return 0
    except Exception as exc:  # noqa: BLE001
        tb = traceback.format_exc()
        _write_result(
            staging_id,
            {"status": "error", "reason": str(exc), "slug": slug},
            error=f"{type(exc).__name__}: {exc}",
        )
        print(tb, file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staging-id", required=False,
                        help="strategy_staging.id UUID")
    parser.add_argument("--slug", required=False,
                        help="Canonical slug (ad-hoc mode, no DB write)")
    parser.add_argument("--start", default=DEFAULT_WINDOW_START)
    parser.add_argument("--end",   default=DEFAULT_WINDOW_END)
    args = parser.parse_args()
    if args.staging_id:
        return run_for_staging(args.staging_id, args.start, args.end)
    if args.slug:
        out = run_for_slug(args.slug, {}, args.start, args.end)
        print(json.dumps(out, indent=2))
        return 0 if out.get("status") == "ok" else 1
    parser.error("need --staging-id or --slug")


if __name__ == "__main__":
    sys.exit(main())
