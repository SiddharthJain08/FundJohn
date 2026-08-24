"""Phase 1G — daily shadow-sizer entry.

Runs after the live trade step.  Reads today's structured handoff, today's
live sizer decisions (sized.json), and ~252 trading days of daily returns
from data/master/prices.parquet for the candidate universe.  Persists to
pyportfolioopt_shadow_runs, prints a one-liner summary.

Default OFF — gated on OPENCLAW_PYPORTFOLIOOPT_SHADOW=1 unless --force.

Schema deviations from the original 1G spec (documented in commit msg):
  * No `regime_blended_sizer_live` table — the LIVE sizer writes its
    decisions to output/handoffs/{date}_sized.json (orders[] with
    ticker + notional_usd; source field = 'regime_blended_sizer_live').
  * No `prices_daily` table — daily OHLCV lives in
    data/master/prices.parquet (cols: ticker, date, close).
  * Equity comes from handoff['portfolio']['portfolio_value'] (the
    structured-handoff field), not handoff['equity_usd'].
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.execution.pyportfolioopt_shadow_sizer import shadow_run  # noqa: E402

LIVE_SOURCE = "regime_blended_sizer_live"
RETURNS_LOOKBACK_DAYS = 252
RAW_PRICE_LOOKBACK_DAYS = 400  # ~252 trading + slack for weekends/holidays
MIN_OBS_FRAC = 0.9             # drop tickers with <90% non-NaN observations
MIN_UNIVERSE_SIZE = 2          # HRP requires ≥2 assets


def _aggregate_live_dollars(sized_orders: list[dict]) -> dict[str, float]:
    """Sum notional_usd by ticker.  Defends against future multi-strategy
    splits that could put the same ticker in multiple order rows."""
    out: dict[str, float] = defaultdict(float)
    for o in sized_orders:
        tkr = o.get("ticker")
        if not tkr:
            continue
        out[tkr] += float(o.get("notional_usd") or 0.0)
    return dict(out)


def _load_returns_for_universe(universe: list[str]) -> "pd.DataFrame":
    """Load daily returns from data/master/prices.parquet for the given
    universe; return last RETURNS_LOOKBACK_DAYS rows after dropping
    columns with too many NaNs."""
    import pandas as pd
    parquet_path = ROOT / "data" / "master" / "prices.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(f"prices parquet missing: {parquet_path}")
    df = pd.read_parquet(parquet_path, columns=["ticker", "date", "close"])
    df["date"] = pd.to_datetime(df["date"])
    cutoff = df["date"].max() - pd.Timedelta(days=RAW_PRICE_LOOKBACK_DAYS)
    df = df[(df["ticker"].isin(universe)) & (df["date"] >= cutoff)]
    if df.empty:
        return pd.DataFrame()
    # pivot_table with aggfunc='last' protects against accidental dup
    # (ticker, date) rows that pivot() would crash on.
    wide = df.pivot_table(index="date", columns="ticker",
                          values="close", aggfunc="last").sort_index()
    # Restrict to weekdays — including 24/7 crypto rows alongside weekday
    # equities pushes equity coverage below the 90% threshold (each equity
    # ticker is NaN on Sat/Sun) and starves the universe down to crypto-only.
    wide = wide[wide.index.dayofweek < 5]
    returns = wide.pct_change().dropna(how="all").iloc[-RETURNS_LOOKBACK_DAYS:]
    if returns.empty:
        return returns
    returns = returns.dropna(axis=1, thresh=int(MIN_OBS_FRAC * len(returns)))
    return returns


def _persist(conn, run_date: str, result: dict, notes: str) -> None:
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO pyportfolioopt_shadow_runs
             (run_date, method, handoff_signals_n, equity_usd, weights,
              target_dollars, live_dollars, diff_dollars, diff_weights,
              diversification_ratio, expected_vol_pct, notes)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (run_date, method) DO UPDATE SET
             handoff_signals_n=EXCLUDED.handoff_signals_n,
             equity_usd=EXCLUDED.equity_usd,
             weights=EXCLUDED.weights,
             target_dollars=EXCLUDED.target_dollars,
             live_dollars=EXCLUDED.live_dollars,
             diff_dollars=EXCLUDED.diff_dollars,
             diff_weights=EXCLUDED.diff_weights,
             diversification_ratio=EXCLUDED.diversification_ratio,
             expected_vol_pct=EXCLUDED.expected_vol_pct,
             notes=EXCLUDED.notes,
             created_at=NOW()""",
        (run_date, result["method"], result["handoff_signals_n"], result["equity_usd"],
         json.dumps(result["weights"]), json.dumps(result["target_dollars"]),
         json.dumps(result["live_dollars"]), json.dumps(result["diff_dollars"]),
         json.dumps(result["diff_weights"]),
         result["diversification_ratio"], result["expected_vol_pct"], notes),
    )
    conn.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="PyPortfolioOpt HRP shadow-sizer (Phase 1G)")
    parser.add_argument("--date", default=date.today().isoformat(),
                        help="Run date YYYY-MM-DD (default: today)")
    parser.add_argument("--force", action="store_true",
                        help="Bypass OPENCLAW_PYPORTFOLIOOPT_SHADOW gate (smoke testing)")
    args = parser.parse_args()

    # The orchestrator child env is frozen at johnbot service start; read
    # .env directly (no override) so flag flips apply without a restart.
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    if not args.force and os.environ.get("OPENCLAW_PYPORTFOLIOOPT_SHADOW") != "1":
        print("OPENCLAW_PYPORTFOLIOOPT_SHADOW not set; skipping.", file=sys.stderr)
        return 0

    run_date = args.date

    structured_path = ROOT / "output" / "handoffs" / f"{run_date}_structured.json"
    sized_path      = ROOT / "output" / "handoffs" / f"{run_date}_sized.json"
    if not structured_path.exists():
        print(f"No structured handoff at {structured_path}; skipping.", file=sys.stderr)
        return 0
    if not sized_path.exists():
        print(f"No sized handoff at {sized_path}; skipping.", file=sys.stderr)
        return 0

    with open(structured_path) as f:
        handoff = json.load(f)
    with open(sized_path) as f:
        sized = json.load(f)

    # Sanity-check the live source matches what we think we're shadowing.
    src = sized.get("source", "")
    if src and src != LIVE_SOURCE:
        print(f"WARN: sized handoff source='{src}' (expected '{LIVE_SOURCE}')", file=sys.stderr)

    live_dollars = _aggregate_live_dollars(sized.get("orders", []))
    if not live_dollars:
        print(f"No live sizer rows for {run_date}; skipping shadow run.", file=sys.stderr)
        return 0

    # Universe = handoff signal tickers ∪ live order tickers.
    sig_tickers = {s.get("ticker") for s in handoff.get("signals", []) if s.get("ticker")}
    universe = sorted(sig_tickers | set(live_dollars))

    returns = _load_returns_for_universe(universe)
    if returns.empty or returns.shape[1] < MIN_UNIVERSE_SIZE:
        n = 0 if returns.empty else returns.shape[1]
        print(f"Insufficient price history ({n} tickers after dropna); skipping.",
              file=sys.stderr)
        return 0

    result = shadow_run(handoff, returns, live_dollars)

    sum_live = sum(live_dollars.values())
    notes = (
        f"live_gross={(sum_live / result['equity_usd']):.1%}, "
        f"hrp_gross=100.0%, n_universe={returns.shape[1]}"
    )

    import psycopg2
    pg_uri = os.environ.get("POSTGRES_URI", "postgresql://openclaw:password@localhost:5432/openclaw")
    conn = psycopg2.connect(pg_uri)
    try:
        _persist(conn, run_date, result, notes)
    finally:
        conn.close()

    dollar_diffs = sorted(result["diff_dollars"].items(),
                          key=lambda kv: abs(kv[1]), reverse=True)[:3]
    weight_diffs = sorted(result["diff_weights"].items(),
                          key=lambda kv: abs(kv[1]), reverse=True)[:3]
    div = result["diversification_ratio"]
    div_str = f"{div:.2f}" if div is not None else "n/a"
    msg = (
        f"[PyPortfolioOpt-shadow] {run_date} HRP allocation; "
        f"div_ratio={div_str}, "
        f"expected_vol={result['expected_vol_pct']:.1f}%; "
        f"top weight diffs: " + ", ".join(f"{t} {w:+.3f}" for t, w in weight_diffs)
        + " | top dollar diffs (placeholder equity): "
        + ", ".join(f"{t} ${d:+,.0f}" for t, d in dollar_diffs)
        + f" | {notes}"
    )
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
