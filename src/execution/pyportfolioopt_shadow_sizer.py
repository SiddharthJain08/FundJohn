"""PyPortfolioOpt shadow alt-sizer (Phase 1G + P2 strategy-level extension).

Reads the same handoff regime_blended_sizer_live consumes; computes HRP
allocation; persists to pyportfolioopt_shadow_runs; never routes to broker.

Default OFF.  Enable by running scripts/run_pyportfolioopt_shadow.py
explicitly OR setting OPENCLAW_PYPORTFOLIOOPT_SHADOW=1 to wire it into the
daily pipeline as a step *after* the live sizer.

Two methods, two rows per run date:
  * method='hrp'          — ticker-level HRP over the day's universe returns
                            (shadow_run). Fixed 2026-08-24 (P2): the HRP
                            weight vector is scaled to the LIVE book's
                            realized gross before diffing, so a fully-invested
                            HRP allocation is never diffed against a
                            fractionally-deployed live book at face value.
  * method='hrp_strategy' — strategy-level HRP over a dates x strategies
                            daily-return panel for the current regime's
                            weighted strategy set (shadow_run_strategy, P2).

Pure compute lives here — no DB, no file I/O, no Discord.  The runner at
scripts/run_pyportfolioopt_shadow.py (and, for the strategy panel, the
loaders it calls in src/execution/strategy_similarity.py) own all I/O.
"""
from __future__ import annotations

import os
import pandas as pd
from pypfopt import HRPOpt


def allocate_hrp(returns: pd.DataFrame) -> dict[str, float]:
    """Hierarchical Risk Parity weights summing to 1."""
    hrp = HRPOpt(returns)
    return dict(hrp.optimize())


def compute_diff(target_dollars: dict[str, float], live_dollars: dict[str, float]) -> dict[str, float]:
    """Per-key difference (target - live).  Keys in either side appear in output.
    Despite the name (kept for the ticker-level $ use case), this is a plain
    dict-diff and is reused as-is for strategy-level weight-space diffs."""
    keys = set(target_dollars) | set(live_dollars)
    return {k: target_dollars.get(k, 0.0) - live_dollars.get(k, 0.0) for k in keys}


def scale_weights_to_live_gross(weights: dict[str, float], live_dollars: dict[str, float],
                                equity: float) -> tuple[dict[str, float], float]:
    """Scale unit-sum HRP weights (fraction-of-equity) so their implied dollar
    deployment matches the live book's realized gross (sum(|live notional|)),
    expressed as a fraction of equity. Returns (scaled_weights, live_gross_usd).

    Fixes the documented 2026-05-14 flaw: diffing a fully-invested (100%-gross)
    HRP allocation against a live book running at a fraction of that gross
    made diff_dollars/diff_weights dominated by the deployment-level mismatch
    rather than the allocation-shape difference. Scaling HRP to the SAME
    realized gross puts both sides on the same footing — e.g. live gross
    $10k vs equity $100k -> factor 0.10, so a raw HRP weight of 0.40 becomes
    a scaled weight of 0.04 (i.e. a $4,000 target, directly comparable to a
    $4,000 live position instead of a $40,000 one)."""
    live_gross = sum(abs(v) for v in live_dollars.values())
    if equity <= 0:
        return {k: 0.0 for k in weights}, live_gross
    factor = live_gross / equity
    return {k: w * factor for k, w in weights.items()}, live_gross


def shadow_run(
    handoff: dict,
    returns: pd.DataFrame,
    live_dollars: dict[str, float],
    equity: float | None = None,
) -> dict:
    """Compute one ticker-level shadow allocation (method='hrp').

    `equity` overrides the handoff-extracted equity if provided.  Defaults
    to handoff['portfolio']['portfolio_value'] (matches the structured
    handoff shape produced by trade_handoff_builder.py); falls back to
    handoff['equity_usd'] for the spec's nominal contract.

    The raw (unit-sum) HRP weights are kept under 'hrp_weights_unit'; the
    'weights' key (and everything derived from it — target_dollars,
    diff_dollars, diff_weights) is scaled to the live book's realized gross
    per scale_weights_to_live_gross() so the comparison is apples-to-apples.
    """
    if equity is None:
        portfolio = handoff.get("portfolio") or {}
        equity = float(portfolio.get("portfolio_value") or handoff.get("equity_usd") or 0.0)
    else:
        equity = float(equity)
    if equity <= 0:
        raise ValueError(f"shadow_run: non-positive equity ({equity}); cannot allocate.")

    hrp_weights_unit = allocate_hrp(returns)  # raw HRP output, sums to 1
    scaled_weights, live_gross = scale_weights_to_live_gross(hrp_weights_unit, live_dollars, equity)
    target_dollars = {tkr: equity * w for tkr, w in scaled_weights.items()}
    diff = compute_diff(target_dollars, live_dollars)

    # Weight-space diff, both sides expressed as a fraction of the SAME
    # denominator (equity) so the comparison sits at the live book's actual
    # deployment level rather than each side being independently renormalised
    # to sum to 1 (which would silently cancel the gross-scaling fix above).
    live_weights = ({t: d / equity for t, d in live_dollars.items()} if equity > 0
                    else {t: 0.0 for t in live_dollars})
    diff_weights = compute_diff(scaled_weights, live_weights)

    # Volatility / diversification stats — annualised, computed on the RAW
    # (fully-invested) HRP shape: these describe the allocation's intrinsic
    # risk structure and are meant to answer "how well-diversified is HRP's
    # suggested shape", independent of how much of it is actually deployed
    # live. Guard against the single-asset edge case (port_vol == 0).
    asset_vols = returns.std() * (252 ** 0.5)
    w_series = pd.Series(hrp_weights_unit).reindex(returns.columns).fillna(0.0)
    port_var = float(w_series @ returns.cov() @ w_series.T) * 252
    port_vol = port_var ** 0.5 if port_var > 0 else 0.0
    div_ratio = float((w_series * asset_vols).sum() / port_vol) if port_vol > 0 else None

    return {
        "method":                "hrp",
        "handoff_signals_n":     len(handoff.get("signals", [])),
        "equity_usd":            equity,
        "weights":               scaled_weights,
        "hrp_weights_unit":      hrp_weights_unit,
        "target_dollars":        target_dollars,
        "live_dollars":          live_dollars,
        "diff_dollars":          diff,
        "diff_weights":          diff_weights,
        "diversification_ratio": div_ratio,
        "expected_vol_pct":      port_vol * 100,
        "live_gross_usd":        live_gross,
    }


def shadow_run_strategy(
    handoff: dict,
    returns_panel: pd.DataFrame,
    live_strategy_weights: dict[str, float],
    equity: float | None = None,
    obs_in_window: dict[str, int] | None = None,
) -> dict | None:
    """Compute one strategy-level shadow allocation (method='hrp_strategy').

    `returns_panel` is a dates x strategies daily-return panel (missing days
    already filled 0.0 by the caller — flat-day convention) restricted to
    strategies that (a) carry current weight in the resolved regime and
    (b) cleared the >=60 observation floor **within this same window**
    (task-P2 review finding 1 — a strategy can clear a >=60 *lifetime*
    floor while having only a handful of in-window observations, which
    zero-fill turns into near-zero variance and HRP rewards with an
    oversized weight; the caller, `_build_strategy_return_panel`, now
    recounts non-NaN rows on the trimmed window before fillna and excludes
    anything short of the floor there too). `live_strategy_weights` is the
    raw (possibly signed) daily_weight per strategy for that same set — the
    S_adj-proportional allocation the live sizer implies; this function
    normalizes it to |daily_weight| / sum(|daily_weight|) for the comparison.

    `obs_in_window` (optional) is the per-strategy in-window observation
    count computed by the caller for the same column set as `returns_panel`
    — passed through unchanged into the result under the `obs_in_window`
    key so `_persist` can write it into the row's JSON and an operator can
    judge data density when reading the accumulated rows. Callers that omit
    it (e.g. existing tests constructing a panel directly) get `{}` back.

    Returns None (caller should log + skip persisting the row) when fewer
    than 2 strategies are present — HRP is undefined below that, matching
    the ticker-level MIN_UNIVERSE_SIZE gate.

    Schema note: strategies carry no dollar notional of their own (only the
    tickers they route to do), so target_dollars/diff_dollars — columns
    named for the ticker-level use case — duplicate weights/diff_weights
    here rather than fabricating a dollar figure. weights/live_dollars/
    diff_weights are the meaningful fields for this method.
    """
    if returns_panel.shape[1] < 2:
        return None

    if equity is None:
        portfolio = handoff.get("portfolio") or {}
        equity = float(portfolio.get("portfolio_value") or handoff.get("equity_usd") or 0.0)
    else:
        equity = float(equity)

    weights = allocate_hrp(returns_panel)

    abs_live = {s: abs(float(live_strategy_weights.get(s, 0.0))) for s in weights}
    total_abs = sum(abs_live.values())
    live_norm = ({s: v / total_abs for s, v in abs_live.items()} if total_abs > 0
                else {s: 0.0 for s in weights})
    diff_weights = compute_diff(weights, live_norm)

    asset_vols = returns_panel.std() * (252 ** 0.5)
    w_series = pd.Series(weights).reindex(returns_panel.columns).fillna(0.0)
    port_var = float(w_series @ returns_panel.cov() @ w_series.T) * 252
    port_vol = port_var ** 0.5 if port_var > 0 else 0.0
    div_ratio = float((w_series * asset_vols).sum() / port_vol) if port_vol > 0 else None

    return {
        "method":                "hrp_strategy",
        "handoff_signals_n":     len(handoff.get("signals", [])),
        "equity_usd":            equity,
        "weights":               weights,
        "target_dollars":        dict(weights),
        "live_dollars":          live_norm,
        "diff_dollars":          dict(diff_weights),
        "diff_weights":          diff_weights,
        "diversification_ratio": div_ratio,
        "expected_vol_pct":      port_vol * 100,
        "obs_in_window":         dict(obs_in_window) if obs_in_window else {},
    }


def is_enabled() -> bool:
    """Default-OFF gate.  Mirrors the OPENCLAW_ALPACA_LIVE_REPLACE rollout pattern."""
    return os.environ.get("OPENCLAW_PYPORTFOLIOOPT_SHADOW") == "1"
