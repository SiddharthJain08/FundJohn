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

P2 (2026-08-24) extension — a second row, method='hrp_strategy': HRP over a
dates x strategies daily-return panel for the current regime's weighted
strategy set, compared against the normalized |daily_weight| allocation the
live sizer implies. Reuses src/execution/strategy_similarity.py's regime
weight-row and strategy-return loaders (no SQL duplicated here). This half
is entirely best-effort/non-fatal — any failure logs a reason and the
ticker-level ('hrp') row still persists.
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
sys.path.insert(0, str(ROOT / "src"))  # so `execution.xxx` bare imports (the
                                       # codebase-wide convention used inside
                                       # strategy_similarity.py and friends)
                                       # resolve when this script runs standalone.

from src.execution.pyportfolioopt_shadow_sizer import shadow_run, shadow_run_strategy  # noqa: E402

LIVE_SOURCE = "regime_blended_sizer_live"
RETURNS_LOOKBACK_DAYS = 252
RAW_PRICE_LOOKBACK_DAYS = 400  # ~252 trading + slack for weekends/holidays
MIN_OBS_FRAC = 0.9             # drop tickers with <90% non-NaN observations
MIN_UNIVERSE_SIZE = 2          # HRP requires ≥2 assets

# --- Strategy-level (method='hrp_strategy') constants -----------------------
STRATEGY_RETURNS_LOOKBACK_DAYS = 252   # trading days targeted in the final panel
STRATEGY_CALENDAR_LOOKBACK_DAYS = 400  # calendar days requested from the SQL
                                       # loaders (~252 trading + weekend/holiday slack,
                                       # mirrors RAW_PRICE_LOOKBACK_DAYS above)
STRATEGY_MIN_OBS = 60                  # per-strategy floor to enter the panel
MIN_STRATEGY_UNIVERSE_SIZE = 2         # HRP requires >=2 strategies


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
    # No `hrp_weights_unit` column exists (and none is needed — see report:
    # the `method` column has no CHECK constraint to relax either). The raw
    # unit-sum HRP weights ride inside the existing `weights` JSONB column,
    # nested alongside the live-gross-scaled weights that method='hrp' uses
    # for its diff. The column has ONE shape regardless of method —
    # {"weights": {...}, "hrp_weights_unit": {...}?, "obs_in_window": {...}?}
    # — so a reader never has to branch on `method` to know how to unwrap
    # it; method='hrp_strategy' simply omits the optional second key (it has
    # no raw/scaled split) and carries `obs_in_window` instead (task-P2
    # review finding 1) so an operator can judge data density per strategy
    # when reading the accumulated rows.
    weights_payload = {"weights": result["weights"]}
    if "hrp_weights_unit" in result:
        weights_payload["hrp_weights_unit"] = result["hrp_weights_unit"]
    if result.get("obs_in_window"):
        weights_payload["obs_in_window"] = result["obs_in_window"]
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
         json.dumps(weights_payload), json.dumps(result["target_dollars"]),
         json.dumps(result["live_dollars"]), json.dumps(result["diff_dollars"]),
         json.dumps(result["diff_weights"]),
         result["diversification_ratio"], result["expected_vol_pct"], notes),
    )
    conn.commit()


def _resolve_regime_state(handoff: dict, pg_uri: str) -> str | None:
    """Same precedence regime_blended_sizer_live.py uses: handoff.regime.state
    first (the regime the live sizer actually ran under that day), falling
    back to the latest market_regime row. Returns None if neither resolves."""
    regime = handoff.get("regime") or {}
    state = regime.get("state") if isinstance(regime, dict) else None
    if state:
        return state
    import psycopg2
    conn = psycopg2.connect(pg_uri)
    try:
        cur = conn.cursor()
        cur.execute("SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1")
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _build_strategy_return_panel(
    regime_state: str,
) -> tuple["pd.DataFrame", dict[str, float], dict[str, int]]:
    """dates x strategies daily-return panel (up to STRATEGY_RETURNS_LOOKBACK_DAYS
    trading days) for strategies that (a) carry a current weight row for
    regime_state (strategy_similarity._current_weight_rows — the same
    acting/weighted-set resolution strategy_similarity.shadow_report uses)
    and (b) clear the STRATEGY_MIN_OBS floor on return observations, live
    `strategy_daily_returns` preferred per-strategy, backtest-trades
    (strategy_backtest_trades) as the fallback when live is too sparse.

    Missing days for an included strategy are filled 0.0 (flat-day
    convention: a day with no observation for that strategy is treated as a
    no-trade day, not as a missing value — this keeps the shared date axis
    complete so HRP's covariance estimate is well-defined).

    IN-WINDOW observation floor (task-P2 review finding 1): step (b) above
    only checks each strategy's LIFETIME observation count (over up to
    STRATEGY_CALENDAR_LOOKBACK_DAYS calendar days) to decide which source
    (live vs. backtest) to use. That is not sufficient on its own — a
    strategy entering via the backtest fallback can clear 60 lifetime
    observations while having only a handful of them actually fall inside
    the final, most-recent STRATEGY_RETURNS_LOOKBACK_DAYS-day window (e.g.
    a strategy whose trade history is mostly older than the window). Once
    the rest of that strategy's column is zero-filled it has artificially
    near-zero variance, and HRP (inverse-variance) hands it the LARGEST
    weight — exactly backwards from the low data confidence it deserves.
    So after reindexing onto the trimmed window, non-NaN rows are recounted
    PER STRATEGY *before* fillna, and any strategy short of STRATEGY_MIN_OBS
    **in this window** is dropped (logged as
    `[hrp_strategy] dropped <sid>: <n>/<window> obs in window`) — this
    supersedes the old "drop fully-zero columns" guard, which only caught
    the 0-obs extreme and let a few-observations-in-window strategy through.

    Returns (panel, live_daily_weight_by_strategy, obs_in_window_by_strategy)
    restricted to whichever strategies actually end up in the panel — the
    third element is meant to be persisted into the row's JSON (see
    _persist) so an operator can judge data density when reading the
    accumulated rows. Both loaders below are imported from
    strategy_similarity — their SQL is not duplicated here.
    """
    import pandas as pd
    from execution import strategy_similarity as ssim

    weight_rows = ssim._current_weight_rows(regime_state)
    if not weight_rows:
        return pd.DataFrame(), {}, {}

    live_returns = ssim._returns_by_regime(STRATEGY_CALENDAR_LOOKBACK_DAYS).get(regime_state, {})
    bt_returns = None  # loaded lazily — only strategies short on live data need it

    series_by_strat: dict[str, dict[str, float]] = {}
    for sid in weight_rows:
        live_series = live_returns.get(sid, {})
        if len(live_series) >= STRATEGY_MIN_OBS:
            series_by_strat[sid] = live_series
            continue
        if bt_returns is None:
            bt_returns = ssim._returns_by_regime_backtest().get(regime_state, {})
        bt_series = bt_returns.get(sid, {})
        if len(bt_series) >= STRATEGY_MIN_OBS:
            series_by_strat[sid] = bt_series
        # else: excluded — neither source clears the observation floor.

    if len(series_by_strat) < MIN_STRATEGY_UNIVERSE_SIZE:
        return pd.DataFrame(), {}, {}

    # Vectorized panel build: pd.DataFrame on a dict-of-dicts unions all inner
    # (date) keys as the index automatically — avoids a Python-level
    # (strategy x date) double loop, which at ~100+ strategies x ~400 obs
    # each is 40k+ scalar .loc writes on this box's 2-core/8GB budget.
    cols = sorted(series_by_strat)
    wide = pd.DataFrame(series_by_strat).sort_index()
    all_dates = list(wide.index)[-STRATEGY_RETURNS_LOOKBACK_DAYS:]
    reindexed = wide.reindex(index=all_dates, columns=cols)  # NaN, not yet
                                                             # fillna'd — needed
                                                             # for the in-window
                                                             # obs count below.

    # In-window observation floor (see docstring): count non-NaN rows per
    # strategy on THIS trimmed window, before fillna turns a sparse column
    # into a mostly-zero one indistinguishable (variance-wise) from a
    # genuinely low-vol, well-observed strategy. Replaces the old
    # "drop fully-zero columns" guard — that only caught the 0-obs extreme.
    obs_in_window = reindexed.notna().sum()
    qualifying_cols = [c for c in cols if obs_in_window[c] >= STRATEGY_MIN_OBS]
    for c in cols:
        if c not in qualifying_cols:
            print(f"[hrp_strategy] dropped {c}: {int(obs_in_window[c])}/"
                  f"{len(all_dates)} obs in window", file=sys.stderr)

    if len(qualifying_cols) < MIN_STRATEGY_UNIVERSE_SIZE:
        return pd.DataFrame(), {}, {}

    panel = reindexed[qualifying_cols].fillna(0.0)
    live_weights = {sid: weight_rows[sid]["daily_weight"] for sid in panel.columns}
    obs_in_window_out = {sid: int(obs_in_window[sid]) for sid in panel.columns}
    return panel, live_weights, obs_in_window_out


def _run_strategy_level(conn, run_date: str, handoff: dict, pg_uri: str) -> str:
    """Strategy-level HRP shadow (method='hrp_strategy') — best-effort.

    Called by `_run_ticker_then_strategy` strictly AFTER the ticker-level
    ('hrp') row has already been persisted (on the same `conn`) — that
    caller-side ordering, not just this function's own try/except, is what
    makes "the ticker row persists even when strategy-level fails" hold
    (task-P2 review finding 2). tests/execution/test_hrp_strategy_level.py
    exercises the ordering directly via `_run_ticker_then_strategy` rather
    than relying on code order alone.

    Any failure here (regime resolution, panel build, HRP compute, or the
    strategy-row persist itself) is caught and logged; this function is
    designed to never raise — `_run_ticker_then_strategy` adds one more
    layer of defense on top of that in case this contract ever regresses.

    Returns the ` | strat_hrp ...` suffix for the run's summary print line.
    """
    strat_result = None
    strat_skip_reason = None
    strat_panel_n = 0
    regime_state = None  # bound up-front: strat_result stays None on any path
                        # that leaves this unresolved, so the persist block
                        # below (which reads it) is never reached unbound.
    try:
        regime_state = _resolve_regime_state(handoff, pg_uri)
        if not regime_state:
            strat_skip_reason = "no regime resolved (handoff + market_regime both empty)"
        else:
            panel, live_strategy_weights, obs_in_window = _build_strategy_return_panel(regime_state)
            strat_panel_n = 0 if panel.empty else panel.shape[1]
            if strat_panel_n < MIN_STRATEGY_UNIVERSE_SIZE:
                strat_skip_reason = (
                    f"insufficient qualifying strategies ({strat_panel_n}) "
                    f"for regime={regime_state}")
            else:
                strat_result = shadow_run_strategy(handoff, panel, live_strategy_weights,
                                                    obs_in_window=obs_in_window)
                if strat_result is None:
                    strat_skip_reason = f"shadow_run_strategy returned None (n={strat_panel_n})"
    except Exception as exc:
        strat_skip_reason = f"error: {exc!s}"
        print(f"[PyPortfolioOpt-shadow] strategy-level HRP failed (non-fatal): {exc!s}",
              file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)

    if strat_result is not None:
        try:
            strat_notes = f"regime={regime_state}, n_strategies={strat_panel_n}"
            _persist(conn, run_date, strat_result, strat_notes)
        except Exception as exc:
            print(f"[PyPortfolioOpt-shadow] strategy-level HRP persist failed (non-fatal): "
                  f"{exc!s}", file=sys.stderr)
            strat_skip_reason = f"persist error: {exc!s}"
            strat_result = None

    if strat_result is not None:
        strat_top3 = sorted(strat_result["diff_weights"].items(),
                            key=lambda kv: abs(kv[1]), reverse=True)[:3]
        return (
            f" | strat_hrp n={len(strat_result['weights'])} top3="
            + ",".join(f"{s} {w:+.3f}" for s, w in strat_top3)
        )
    print(f"Strategy-level HRP shadow skipped: {strat_skip_reason}", file=sys.stderr)
    return f" | strat_hrp n=0 (skipped: {strat_skip_reason})"


def _run_ticker_then_strategy(
    pg_uri: str, run_date: str, handoff: dict, result: dict, notes: str,
) -> str:
    """Persist the ticker-level ('hrp') row, then attempt the strategy-level
    ('hrp_strategy') addition — in that strict order, unconditionally.

    task-P2 review finding 2: "the ticker row persists even when
    strategy-level fails" previously held only by code order in main(), with
    no test exercising it. This function IS that seam: the ticker `_persist`
    call below always runs first and its outcome is not conditioned on
    anything after it succeeding, and the call to `_run_strategy_level` is
    wrapped in its own guard so that even a complete, unexpected failure
    there (not just the failure modes `_run_strategy_level`'s internal
    try/except already anticipates — e.g. a fully-replaced/monkeypatched
    implementation, as exercised in
    tests/execution/test_hrp_strategy_level.py) can never take the
    already-persisted ticker row down with it or propagate out of the
    pipeline step.

    Returns the ` | strat_hrp ...` suffix for the run's summary print line.
    """
    import psycopg2
    conn = psycopg2.connect(pg_uri)
    try:
        _persist(conn, run_date, result, notes)  # ticker row — FIRST, always.
        try:
            return _run_strategy_level(conn, run_date, handoff, pg_uri)
        except Exception as exc:
            print(f"[PyPortfolioOpt-shadow] strategy-level HRP orchestration "
                  f"failed unexpectedly (non-fatal): {exc!s}", file=sys.stderr)
            return f" | strat_hrp n=0 (skipped: unexpected error: {exc!s})"
    finally:
        conn.close()


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

    # hrp_gross is now computed, not hardcoded: since 'weights' (and hence
    # target_dollars) is scaled to the live book's realized gross
    # (scale_weights_to_live_gross), this should land at ~= live_gross by
    # construction — the notes line documents that the two sides are on the
    # same footing rather than asserting a fixed "fully invested" 100%.
    live_gross = result.get("live_gross_usd", sum(abs(v) for v in live_dollars.values()))
    hrp_gross = sum(abs(v) for v in result["target_dollars"].values())
    notes = (
        f"live_gross={(live_gross / result['equity_usd']):.1%}, "
        f"hrp_gross={(hrp_gross / result['equity_usd']):.1%} (scaled to live gross), "
        f"n_universe={returns.shape[1]}"
    )

    pg_uri = os.environ.get("POSTGRES_URI", "postgresql://openclaw:password@localhost:5432/openclaw")
    # Ticker row persists FIRST, unconditionally; strategy-level ('hrp_strategy')
    # is attempted strictly after, guarded so it can never take the ticker row
    # down with it (see _run_ticker_then_strategy docstring — task-P2 finding 2).
    strat_suffix = _run_ticker_then_strategy(pg_uri, run_date, handoff, result, notes)

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
        + strat_suffix
    )
    print(msg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
