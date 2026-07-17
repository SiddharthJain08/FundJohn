#!/usr/bin/env python3
"""Pure live transforms for strategy orthogonalization, consumed by the sizer.

Tier-1 (fold): collapse same-fold-group / same-direction / same-ticker contributions
to a single representative BEFORE the ticker_w sums.
Conviction gate: corr_adjusted_net_sharpe (the live Sharpe-weighted combination Sharpe).
The legacy Tier-2 deflation gate (k_eff / block_conviction / deflated_net_sharpe) was
retired 2026-07-01.

Spec: docs/archive/superpowers/specs/2026-05-29-strategy-orthogonalization-design.md
"""
from __future__ import annotations
import math

SPARSE_DEFAULT = 0.05   # unknown strategy-pair similarity (matches strategy_similarity/correlation_matrix)


def _dir_to_int(direction) -> int:
    """Map a Signal.direction to {+1, -1, 0}, matching the sizer's _DIR_MAP
    (LONG/BUY/BUY_VOL -> +1, SHORT/SELL/SELL_VOL -> -1, FLAT/unknown -> 0)."""
    d = str(direction or '').upper()
    if d in ('LONG', 'BUY', 'BUY_VOL'):
        return 1
    if d in ('SHORT', 'SELL', 'SELL_VOL'):
        return -1
    if d == 'FLAT':
        return 0
    # Defensive fallback for unanticipated variants
    if d.startswith('L'):
        return 1
    if d.startswith('S'):
        return -1
    return 0


def fold_active_contributions(active: list[dict], fold_map: dict[str, int],
                              rep_map: dict[int, str], eff_sharpe: dict[str, float]) -> list[dict]:
    """For each (ticker, direction, fold_group) bucket of grouped contributions, keep ONE:
    the representative if it fired, else the highest-effective_sharpe member that fired.
    Ungrouped (singleton) contributions pass through untouched."""
    kept: list[dict] = []
    buckets: dict[tuple, list[dict]] = {}
    for s in active:
        sid = s.get('strategy_id')
        gid = fold_map.get(sid)
        if gid is None:
            kept.append(s)                                  # ungrouped -> keep
            continue
        key = (s.get('ticker'), _dir_to_int(s.get('direction')), gid)
        buckets.setdefault(key, []).append(s)
    for (ticker, d, gid), members in buckets.items():
        rep = rep_map.get(gid)
        chosen = next((m for m in members if m.get('strategy_id') == rep), None)
        if chosen is None:
            chosen = max(members, key=lambda m: eff_sharpe.get(m.get('strategy_id'), float('-inf')))
        kept.append(chosen)
    return kept


# Tier-2 (k_eff / block_conviction / _mean_pairwise / deflated_net_sharpe) was the
# legacy within-block heuristic deflation gate. Removed 2026-07-01 with the legacy
# cum-Sharpe machinery — superseded live by corr_adjusted_net_sharpe below.


def corr_adjusted_net_sharpe(contribs_by_ticker: dict[str, list[tuple]],
                             sim: dict[str, dict[str, float]],
                             weight_by_strat: dict[str, float],
                             eps: float = 1e-9) -> tuple[dict[str, float], int]:
    """Signed, correlation-adjusted (Sharpe-weighted) combination Sharpe per ticker.

    contribs_by_ticker: {ticker: [(strategy_id, direction_int), ...]} (post-fold survivors).
    sim:   per-regime strategy x strategy similarity matrix {sid: {sid: rho}}.
    weight_by_strat: the w_i basis (cadence-normalized daily_weight).
    Returns ({ticker: signed S_adj}, n_backstop_fires).

    APPROXIMATE (similarity-proxy): `sim` is a heuristic Jaccard-return-corr blend, not a true
    return-correlation matrix and NOT PSD-guaranteed -> the signed quadratic form q can go <= 0.
    The inert non-PSD backstop then falls back to the diagonal ("assume independent") denominator
    and is counted; it is a NaN guard, NOT a deflating floor.

        num = sum_i  w_i^2 * d_i                              (signed: opposing strategies cancel)
        q   = sum_ij w_i * w_j * d_i * d_j * rho_ij           (rho_ii = 1; missing -> SPARSE_DEFAULT)
        S_adj = num / sqrt(q)            if q >  eps          (no floor; full diversification credit)
              = num / sqrt(sum_i w_i^2)  if q <= eps          (backstop; counted)
    """
    out: dict[str, float] = {}
    n_backstop = 0
    for ticker, contribs in contribs_by_ticker.items():
        rows = []
        for sid, d in contribs:
            w = weight_by_strat.get(sid)
            if w is None or not d:
                continue
            rows.append((sid, int(d), float(w)))
        if not rows:
            continue
        num = sum(w * w * d for (_s, d, w) in rows)
        diag = sum(w * w for (_s, _d, w) in rows)
        q = diag                                  # i == j terms (rho_ii = 1)
        n = len(rows)
        for i in range(n):
            sid_i, d_i, w_i = rows[i]
            a_i = w_i * d_i
            row_i = sim.get(sid_i, {})
            for j in range(i + 1, n):
                sid_j, d_j, w_j = rows[j]
                rho = row_i.get(sid_j)
                if rho is None:
                    rho = sim.get(sid_j, {}).get(sid_i, SPARSE_DEFAULT)
                q += 2.0 * a_i * (w_j * d_j) * float(rho)
        if q > eps:
            den = math.sqrt(q)
        else:
            den = math.sqrt(diag) if diag > 0 else 0.0
            n_backstop += 1
        out[ticker] = (num / den) if den > 0 else 0.0
    return out, n_backstop


# ---------------------------------------------------------------------------
# Per-regime trade-count weight factor (2026-07-16, operator directive):
# √(ln n / ln anchor), applied to each strategy's weight in the corr-adjusted
# cum-Sharpe calc. n = that strategy's backtest trade count IN THE CURRENT REGIME
# (strategy_backtest_regimes.trade_count → strategy_weights_by_regime.bt_n).
#
# REPLACES the regime-independent Grinold breadth factor √(ln N_universe / ln 500).
# Rationale (operator): trade count is the REALIZED, PER-REGIME version of breadth
# — universe size N is only the opportunity set (a strategy can span 12k names yet
# rarely fire), whereas n counts the bets it actually made, and it varies by regime
# (trust a strategy's CRISIS Sharpe less if it made 40 CRISIS trades vs 5000 in
# LOW_VOL). It also folds in estimation confidence: a Sharpe over n heavily
# CORRELATED trades carries far fewer than n independent observations, so log(n)
# deflates the raw count to ~effective independent bets and √(·) converts that to a
# Sharpe-confidence multiplier — the same √(log·) / fundamental-law lineage as the
# breadth factor it replaces.
#
# Anchored (default ≈ median regime trade count) so factors STRADDLE 1 and the
# aggregate S_adj scale — and thus the conviction floors — are approximately
# preserved (a uniform factor c scales S_adj by c: num→c², den→c). The tilt is
# gentle: with eligibility forcing n≥100, the practical range is ~0.82–1.25.
# The anchor is the calibration knob (pipeline_config), paired with a floor recheck.
# ---------------------------------------------------------------------------
TRADE_FACTOR_ANCHOR_N = 1000   # ≈ median per-regime trade count; scale anchor


def trade_weight_factor(n, anchor: int = TRADE_FACTOR_ANCHOR_N) -> float:
    """√(ln n / ln anchor). Fail-safe to 1.0 (NEUTRAL — never 0) when n is
    missing/None/non-numeric/≤1 or the anchor is degenerate. The neutral-on-missing
    guard is load-bearing: a strategy with no bt_n for the current regime (e.g. its
    fleet re-backtest hasn't landed) must keep its raw weight, NOT be silently zeroed
    (ln 1 = 0 → factor 0 would drop it from the book). A genuinely low but valid n
    (≥2) DOES down-weight, which is the intended low-confidence penalty."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return 1.0
    if n <= 1 or anchor <= 1:
        return 1.0
    return math.sqrt(math.log(n) / math.log(anchor))
