"""PyPortfolioOpt shadow alt-sizer (Phase 1G).

Reads the same handoff regime_blended_sizer_live consumes; computes HRP
allocation; persists to pyportfolioopt_shadow_runs; never routes to broker.

Default OFF.  Enable by running scripts/run_pyportfolioopt_shadow.py
explicitly OR setting OPENCLAW_PYPORTFOLIOOPT_SHADOW=1 to wire it into the
daily pipeline as a step *after* the live sizer.

Pure compute lives here — no DB, no file I/O, no Discord.  The runner at
scripts/run_pyportfolioopt_shadow.py and the orchestrator-callable wrapper
at src/execution/pyportfolioopt_shadow.py own all I/O.
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
    """Per-ticker $-difference (target - live).  Tickers in either side appear in output."""
    keys = set(target_dollars) | set(live_dollars)
    return {k: target_dollars.get(k, 0.0) - live_dollars.get(k, 0.0) for k in keys}


def shadow_run(
    handoff: dict,
    returns: pd.DataFrame,
    live_dollars: dict[str, float],
    equity: float | None = None,
) -> dict:
    """Compute one shadow allocation.

    `equity` overrides the handoff-extracted equity if provided.  Defaults
    to handoff['portfolio']['portfolio_value'] (matches the structured
    handoff shape produced by trade_handoff_builder.py); falls back to
    handoff['equity_usd'] for the spec's nominal contract.
    """
    if equity is None:
        portfolio = handoff.get("portfolio") or {}
        equity = float(portfolio.get("portfolio_value") or handoff.get("equity_usd") or 0.0)
    else:
        equity = float(equity)
    if equity <= 0:
        raise ValueError(f"shadow_run: non-positive equity ({equity}); cannot allocate.")

    weights = allocate_hrp(returns)
    target_dollars = {tkr: equity * w for tkr, w in weights.items()}
    diff = compute_diff(target_dollars, live_dollars)

    # Volatility / diversification stats — annualised.  Guard against the
    # single-asset edge case (port_vol == 0 -> div_ratio undefined).
    asset_vols = returns.std() * (252 ** 0.5)
    w_series = pd.Series(weights).reindex(returns.columns).fillna(0.0)
    port_var = float(w_series @ returns.cov() @ w_series.T) * 252
    port_vol = port_var ** 0.5 if port_var > 0 else 0.0
    div_ratio = float((w_series * asset_vols).sum() / port_vol) if port_vol > 0 else None

    return {
        "method":                "hrp",
        "handoff_signals_n":     len(handoff.get("signals", [])),
        "equity_usd":            equity,
        "weights":               weights,
        "target_dollars":        target_dollars,
        "live_dollars":          live_dollars,
        "diff_dollars":          diff,
        "diversification_ratio": div_ratio,
        "expected_vol_pct":      port_vol * 100,
    }


def is_enabled() -> bool:
    """Default-OFF gate.  Mirrors the OPENCLAW_ALPACA_LIVE_REPLACE rollout pattern."""
    return os.environ.get("OPENCLAW_PYPORTFOLIOOPT_SHADOW") == "1"
