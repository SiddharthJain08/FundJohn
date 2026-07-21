"""universe_shrink.py — universe ladder campaign W3 core (2026-07-21).

Derives every ladder tier's metrics for a strategy by FILTERING its stored
full-universe primary-run trades (`strategy_backtest_trades`) with
point-in-time tier membership — NO re-run (operator directive: the
full-universe fleet re-backtest is a superset; a sub-universe's metrics are
the metrics of the trade subset whose tickers were tier members at entry).

Membership source: the frozen tier-membership artifact built by
scripts/build_tier_membership.py (monthly snapshots × 4 tiers, predicate +
60-bar coverage floor — the SAME artifact contract the SP-7 grid cells
consume, so a trade is bucketed to a tier iff a real backtest on that tier
would have been offered its ticker that month).

Aggregation reuses the canonical helpers so shrunk numbers are shaped
exactly like grid-cell metrics: aggregate_per_regime (per-sleeve) +
universe_grid_cli.blend_metrics (day-frequency-blended TOTAL, the number
select_tier compares). Sub-universe daily marks are not persisted, so all
tiers AND the full baseline aggregate via the pnl smear path — tier-vs-tier
and tier-vs-full comparisons are methodologically identical.
"""
from __future__ import annotations

from bisect import bisect_right
from typing import Optional

from backtest.universe_ladder_selection import LADDER_TIERS, select_tier

FULL_TIER = 'full'
TOTAL_KEY = 'TOTAL'


# ── membership artifact ─────────────────────────────────────────────────────
def load_membership(artifact_path) -> tuple[list[str], dict[str, dict[str, frozenset]]]:
    """Load the tier-membership parquet → (sorted snapshot ISO dates,
    {tier: {snapshot_iso: frozenset(symbols)}})."""
    import pandas as pd
    df = pd.read_parquet(artifact_path)
    members: dict[str, dict[str, frozenset]] = {t: {} for t in LADDER_TIERS}
    for r in df.itertuples(index=False):
        if r.tier in members:
            members[r.tier][str(r.snapshot_date)] = frozenset(r.symbols)
    snaps = sorted({s for m in members.values() for s in m})
    return snaps, members


def snap_for(entry_iso: str, snaps: list[str]) -> Optional[str]:
    """Latest snapshot on/before entry date (bars resolve from the prior
    month-end snapshot — same convention as the resolver/grid)."""
    i = bisect_right(snaps, entry_iso) - 1
    return snaps[i] if i >= 0 else None


def bucket_trades(trades: list[dict], snaps: list[str],
                  members: dict[str, dict[str, frozenset]]) -> dict[str, list[dict]]:
    """tier -> the subset of trades whose ticker was a member at entry.
    Tiers are nested by construction, so a trade kept by a narrow tier is
    kept by every broader one."""
    out: dict[str, list[dict]] = {t: [] for t in LADDER_TIERS}
    for tr in trades:
        s = snap_for(str(tr['entry_date']), snaps)
        if s is None:
            continue
        tkr = tr['ticker']
        for tier in LADDER_TIERS:
            m = members[tier].get(s)
            if m and tkr in m:
                out[tier].append(tr)
    return out


def mean_tier_sizes(snaps: list[str], members, window_start: str,
                    window_end: str) -> dict[str, Optional[float]]:
    """Mean member count per tier over the snapshots inside the run window
    (grid-display convenience — mirrors mean_universe_size)."""
    inside = [s for s in snaps if window_start <= s <= window_end]
    out: dict[str, Optional[float]] = {}
    for t in LADDER_TIERS:
        sizes = [len(members[t][s]) for s in inside if s in members[t]]
        out[t] = round(sum(sizes) / len(sizes), 2) if sizes else None
    return out


# ── metrics + selection ─────────────────────────────────────────────────────
def shrink_and_select(trades: list[dict], snaps: list[str], members,
                      regimes, day_freq: dict, *,
                      instrument_class: str = 'equity',
                      sizes: Optional[dict] = None) -> dict:
    """The full W3 computation for one strategy's primary-run trades.

    Returns {
      'metrics_by_tier':  tier -> blended grid dict (incl. 'full'),
      'regime_by_tier':   tier -> {regime: sleeve dict} (incl. 'full'),
      'verdict':          select_tier(...) output (prefer-largest +
                          maintain-constraint vs the full-universe baseline),
    }
    `regimes` is the historical regime series sliced to the run window
    (oos-day counts only); `day_freq` from regime_day_frequency.
    """
    from backtest.unified_backtest import aggregate_metrics, aggregate_per_regime
    from backtest.universe_grid_cli import blend_metrics

    sizes = sizes or {}
    by_tier = bucket_trades(trades, snaps, members)
    by_tier[FULL_TIER] = list(trades)

    metrics_by_tier: dict[str, dict] = {}
    regime_by_tier: dict[str, dict] = {}
    totals_by_tier: dict[str, dict] = {}
    for tier, kept in by_tier.items():
        per_regime = aggregate_per_regime(kept, regimes)
        regime_by_tier[tier] = per_regime
        metrics_by_tier[tier] = blend_metrics(
            per_regime, day_freq, mean_universe_size=sizes.get(tier))
        # whole-window equity-curve aggregate (return_pct is not expressible
        # as a regime blend — the dashboard's chosen-universe overlay needs it)
        totals_by_tier[tier] = aggregate_metrics(kept)

    verdict = select_tier(
        {t: metrics_by_tier[t] for t in LADDER_TIERS},
        regime_metrics_by_tier={t: regime_by_tier[t] for t in LADDER_TIERS},
        baseline_regime_metrics=regime_by_tier[FULL_TIER],
        instrument_class=instrument_class,
    )
    return {'metrics_by_tier': metrics_by_tier,
            'regime_by_tier': regime_by_tier,
            'totals_by_tier': totals_by_tier,
            'verdict': verdict}


def db_rows(strategy_id: str, run_id, result: dict, *,
            candidate_set_id: Optional[str] = None) -> list[tuple]:
    """Flatten a shrink_and_select result into universe_shrink_metrics rows:
    (strategy_id, run_id, tier, regime_state, sharpe, max_dd_pct, trade_count,
     win_rate, sortino, calmar, mean_holding_days, return_pct,
     candidate_set_id)."""
    rows: list[tuple] = []
    for tier, blended in result['metrics_by_tier'].items():
        total = (result.get('totals_by_tier') or {}).get(tier) or {}
        rows.append((strategy_id, run_id, tier, TOTAL_KEY,
                     blended.get('sharpe'), blended.get('max_dd_pct'),
                     blended.get('trades_n'), blended.get('win_rate'),
                     blended.get('sortino'), blended.get('calmar'),
                     blended.get('mean_holding_days'), total.get('return_pct'),
                     candidate_set_id))
        for regime, m in result['regime_by_tier'][tier].items():
            rows.append((strategy_id, run_id, tier, regime,
                         m.get('sharpe'), m.get('max_dd_pct'),
                         m.get('trade_count'), m.get('hit_rate'),
                         m.get('sortino'), m.get('calmar'),
                         m.get('avg_holding_days'), m.get('return_pct'),
                         candidate_set_id))
    return rows
