"""SP-7 Phase B — deterministic tier selection (no LLM).

Eligibility: blended sharpe non-None AND trades_n >= MIN_TRADES.
Winner: narrowest eligible tier; walking broader, a tier displaces the
current winner iff sharpe >= winner.sharpe + DELTA_SHARPE (parsimony).
"""
from __future__ import annotations

LADDER_TIERS = ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')
DELTA_SHARPE = 0.10   # mirrors the weekend-coupling auto-apply threshold
MIN_TRADES = 30       # mirrors the weekend-coupling trade floor


def _eligible(m) -> bool:
    return (m is not None and m.get('sharpe') is not None
            and int(m.get('trades_n') or 0) >= MIN_TRADES)


def select_tier(metrics_by_tier: dict) -> dict:
    """metrics_by_tier: tier name -> metrics dict (or None for
    timeout/error/skipped cells). Returns a verdict dict."""
    eligible = [t for t in LADDER_TIERS if _eligible(metrics_by_tier.get(t))]
    comparisons = []
    if not eligible:
        return {'verdict': 'no_signal', 'choice': None,
                'eligible': [], 'comparisons': comparisons}
    winner = eligible[0]
    for t in eligible[1:]:
        w_s = float(metrics_by_tier[winner]['sharpe'])
        t_s = float(metrics_by_tier[t]['sharpe'])
        # epsilon guard: 1.1 + 0.10 = 1.2000000000000002 in IEEE754 —
        # without it, exact-threshold displacements are silently denied
        # while the audit log shows delta=0.10 (review catch, Task 8).
        displaced = (t_s - w_s) >= DELTA_SHARPE - 1e-9
        comparisons.append({'challenger': t, 'incumbent': winner,
                            'delta': round(t_s - w_s, 4),
                            'displaced': displaced})
        if displaced:
            winner = t
    return {'verdict': 'winner', 'choice': winner,
            'eligible': eligible, 'comparisons': comparisons}
