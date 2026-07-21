"""Universe ladder tier selection (deterministic, no LLM).

Eligibility: blended sharpe non-None AND trades_n >= MIN_TRADES.

Direction (operator directive 2026-07-21, universe ladder campaign): PREFER
THE LARGEST universe. Winner seeds at the BROADEST eligible tier; walking
narrower, a tier displaces the current winner iff
sharpe >= winner.sharpe + DELTA_SHARPE. (Flipped from the original SP-7
Phase B parsimony rule, which seeded narrowest and broadened.)

Maintain-constraint (same directive): on any shrink, max_dd <= class ceiling
AND trades >= min_trades (100) must be MAINTAINED in every regime where the
FULL-universe backtest met them. Enforced only when per-(tier, regime)
metrics are supplied — the shrink orchestrator passes them; the SP-7 grid
driver calls without them (grid cells carry no per-regime split, so the
constraint is skipped there).
"""
from __future__ import annotations

LADDER_TIERS = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
DELTA_SHARPE = 0.10   # mirrors the weekend-coupling auto-apply threshold
MIN_TRADES = 30       # mirrors the weekend-coupling trade floor


def _eligible(m) -> bool:
    return (m is not None and m.get('sharpe') is not None
            and int(m.get('trades_n') or 0) >= MIN_TRADES)


def _regime_trades(m: dict):
    """Trade count under any of the producer key spellings (regime sleeves
    from strategy_backtest_regimes use trade_count; aggregate_metrics emits
    total_trades; grid cells use trades_n)."""
    for k in ('trade_count', 'total_trades', 'trades_n'):
        if m.get(k) is not None:
            return int(m[k])
    return None


def _meets_regime_bar(m, thr: dict) -> bool:
    """True iff one regime sleeve meets the maintain bar: trades >=
    thr.min_trades AND max_dd_pct <= thr.max_dd_pct. Missing sleeve or
    missing metric fails closed (a shrink may not hide a regime)."""
    if not m:
        return False
    trades = _regime_trades(m)
    dd = m.get('max_dd_pct')
    if trades is None or dd is None:
        return False
    return trades >= thr['min_trades'] and float(dd) <= thr['max_dd_pct']


def select_tier(metrics_by_tier: dict, *,
                regime_metrics_by_tier: dict | None = None,
                baseline_regime_metrics: dict | None = None,
                instrument_class: str | None = 'equity') -> dict:
    """metrics_by_tier: tier name -> metrics dict (or None for
    timeout/error/skipped cells).

    Optional maintain-constraint inputs:
      regime_metrics_by_tier: tier -> {regime -> {trade_count|total_trades,
          max_dd_pct}}. Enables the constraint.
      baseline_regime_metrics: {regime -> {...}} from the FULL-universe run —
          defines which regimes must be maintained. Defaults to the broadest
          eligible tier's map when omitted.
      instrument_class: sets the DD ceiling / trade floor via
          regime_qualification.class_thresholds (equity 20 / option 30 /
          crypto 70; min_trades 100).

    Returns a verdict dict: {verdict, choice, eligible, comparisons,
    maintained_regimes}. Each comparison row carries blocked_regimes — the
    maintained regimes a challenger would have dropped below the bar."""
    eligible = [t for t in LADDER_TIERS if _eligible(metrics_by_tier.get(t))]
    comparisons = []
    if not eligible:
        return {'verdict': 'no_signal', 'choice': None, 'eligible': [],
                'comparisons': comparisons, 'maintained_regimes': []}

    maintained: list[str] = []
    thr = None
    if regime_metrics_by_tier is not None:
        from backtest.regime_qualification import class_thresholds
        thr = class_thresholds(instrument_class)
        base = baseline_regime_metrics
        if base is None:
            base = regime_metrics_by_tier.get(eligible[-1]) or {}
        maintained = sorted(r for r, m in base.items()
                            if _meets_regime_bar(m, thr))

    winner = eligible[-1]              # broadest eligible seeds the winner
    for t in reversed(eligible[:-1]):  # walk broadest → narrowest
        w_s = float(metrics_by_tier[winner]['sharpe'])
        t_s = float(metrics_by_tier[t]['sharpe'])
        # epsilon guard: 1.1 + 0.10 = 1.2000000000000002 in IEEE754 —
        # without it, exact-threshold displacements are silently denied
        # while the audit log shows delta=0.10 (review catch, Task 8).
        displaced = (t_s - w_s) >= DELTA_SHARPE - 1e-9
        blocked: list[str] = []
        if displaced and maintained:
            trm = (regime_metrics_by_tier or {}).get(t) or {}
            blocked = [r for r in maintained
                       if not _meets_regime_bar(trm.get(r), thr)]
            if blocked:
                displaced = False
        comparisons.append({'challenger': t, 'incumbent': winner,
                            'delta': round(t_s - w_s, 4),
                            'displaced': displaced,
                            'blocked_regimes': blocked})
        if displaced:
            winner = t
    return {'verdict': 'winner', 'choice': winner, 'eligible': eligible,
            'comparisons': comparisons, 'maintained_regimes': maintained}
