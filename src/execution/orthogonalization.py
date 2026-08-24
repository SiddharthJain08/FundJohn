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
import os
import sys

SPARSE_DEFAULT = 0.05   # unknown strategy-pair similarity (matches strategy_similarity)


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


TANGENCY_SHRINK_DEFAULT = 0.10   # R = (1-γ)R_raw + γI — tames the heuristic,
                                 # non-PSD-guaranteed similarity matrix before inversion
TANGENCY_MAX_ENUM = 10           # per-side subset-enumeration cap (2^n solves)


def resolve_tangency_gamma(artifact_gamma: float | None) -> float:
    """Resolve the tangency shrinkage intensity gamma (task P1, shadow-first —
    docs/.superpowers/sdd/2026-08-24-five-repo-adoptions/task-P1-brief.md).

    Precedence:
      1. env OPENCLAW_TANGENCY_SHRINK (float), if set -> that value, always.
      2. artifact_gamma, if env OPENCLAW_TANGENCY_LW == '1' and artifact_gamma
         is not None.
      3. TANGENCY_SHRINK_DEFAULT (0.10).

    With every new env var unset, this ALWAYS returns TANGENCY_SHRINK_DEFAULT —
    the tangency solve's default behavior is unchanged (binding requirement:
    the live sizer's conviction gate must stay byte-identical until the LW
    flag is deliberately armed).

    Range validation (task P1b review fix, 2026-08-24): a candidate value from
    ANY source (env override or artifact_gamma) is REJECTED if it falls
    outside [0.0, 1.0] — R = (1-γ)R_raw + γI is only a convex blend inside
    that range; outside it the "shrinkage" isn't shrinkage (e.g. γ=1.5 makes
    the raw-matrix coefficient negative). A rejected candidate falls through
    to the next precedence level exactly as a malformed (non-numeric) one
    does, but — since a numeric value COULD be logged, unlike a parse
    failure — also logs `[tangency_lw] rejected gamma=<x> outside [0,1];
    using <fallback>` so a bad override or a corrupted artifact is visible,
    not silently swapped for the default. artifact_gamma is already bounded
    to [0,1] by pypfopt's own delta clamp in practice, but this validates it
    too (defense in depth against a corrupted/unexpected stored value).

    Shadow visibility: when artifact_gamma is available but the LW flag is NOT
    armed (i.e. it would be ignored) AND no valid OPENCLAW_TANGENCY_SHRINK
    override is in play, log once per call so an operator can see what the LW
    estimate would resolve to before flipping the flag. A live, IN-RANGE
    OPENCLAW_TANGENCY_SHRINK override short-circuits before this log: since it
    unconditionally wins precedence #1, arming the LW flag would NOT change
    the resolved value in that case, so "would_use_gamma" would be a
    misleading thing to print. (A rejected out-of-range override does NOT
    short-circuit — precedence continues past it, so would_use_gamma can
    still fire alongside the rejection line.)
    (De-dup to "once per sizer invocation" is the caller's responsibility —
    this resolver is pure and stateless.)
    """
    lw_armed = os.environ.get('OPENCLAW_TANGENCY_LW') == '1'

    # Precedence #1: env override — only an in-range numeric value wins
    # outright. A parse failure falls through silently (unchanged from
    # before); an out-of-range numeric value falls through too, but is
    # reported once the fallback below is known.
    rejected_override = None
    override = os.environ.get('OPENCLAW_TANGENCY_SHRINK')
    if override not in (None, ''):
        try:
            override_val = float(override)
        except (TypeError, ValueError):
            pass   # malformed override -> fall through to the remaining precedence
        else:
            if 0.0 <= override_val <= 1.0:
                return override_val
            rejected_override = override_val

    # Precedence #2/#3: LW-armed artifact_gamma, else TANGENCY_SHRINK_DEFAULT.
    if artifact_gamma is not None and not lw_armed:
        print(f"[tangency_lw] would_use_gamma={float(artifact_gamma):.3f} "
              f"(current={TANGENCY_SHRINK_DEFAULT:.2f})", file=sys.stderr)

    resolved = TANGENCY_SHRINK_DEFAULT
    if lw_armed and artifact_gamma is not None:
        try:
            ag = float(artifact_gamma)
        except (TypeError, ValueError):
            ag = None
        if ag is not None and 0.0 <= ag <= 1.0:
            resolved = ag
        elif ag is not None:
            print(f"[tangency_lw] rejected gamma={ag:.3f} outside [0,1]; "
                  f"using {TANGENCY_SHRINK_DEFAULT:.2f}", file=sys.stderr)

    if rejected_override is not None:
        print(f"[tangency_lw] rejected gamma={rejected_override:.3f} outside [0,1]; "
              f"using {resolved:.2f}", file=sys.stderr)

    return resolved


def _tangency_side_sharpe(sids: list[str], s: list[float],
                          sim: dict, shrink: float,
                          gamma: float | None = None) -> tuple[float, bool]:
    """Max attainable combination Sharpe over one SAME-DIRECTION contributor set,
    with NON-NEGATIVE tangency weights: max over non-empty subsets T of
    √(s_Tᵀ R_T⁻¹ s_T) where x = R_T⁻¹ s_T has every component >= 0.

    The non-negativity constraint is load-bearing: unconstrained R⁻¹ lets the
    optimizer short a correlated contributor's signal, inflating the score from
    intra-side spreads that a per-ticker NET position cannot express. Subsets
    make the quantity monotone (adding a contributor only adds subsets, never
    removes them) and every singleton is feasible (x = s_i > 0), so the result
    is always >= max(s_i) — a confirming strategy can never LOWER conviction.

    `gamma`, when not None, overrides `shrink` (task P1b: the resolved
    Ledoit-Wolf/env shrinkage intensity from resolve_tangency_gamma). Left
    None, behavior is unchanged — `shrink` alone controls the blend, exactly
    as before this parameter existed.

    Returns (best_sharpe, degraded) — degraded=True when any multi-member solve
    failed numerically (result fell back to smaller subsets; still valid)."""
    import numpy as _np
    n = len(s)
    if n == 0:
        return 0.0, False
    if n == 1:
        return float(s[0]), False
    eff_shrink = shrink if gamma is None else float(gamma)
    if n > TANGENCY_MAX_ENUM:
        # keep the heaviest contributors for enumeration; the rest can only
        # have added, never removed, so this is a conservative floor
        order = sorted(range(n), key=lambda i: -s[i])[:TANGENCY_MAX_ENUM]
        sids = [sids[i] for i in order]
        s = [s[i] for i in order]
        n = TANGENCY_MAX_ENUM
    R = _np.eye(n)
    for i in range(n):
        row_i = sim.get(sids[i], {})
        for j in range(i + 1, n):
            rho = row_i.get(sids[j])
            if rho is None:
                rho = sim.get(sids[j], {}).get(sids[i], SPARSE_DEFAULT)
            R[i, j] = R[j, i] = float(rho)
    R = (1.0 - eff_shrink) * R + eff_shrink * _np.eye(n)
    sv = _np.asarray(s, dtype=float)
    best = float(max(s))                       # singletons always feasible
    degraded = False
    for mask in range(1, 1 << n):
        idx = [i for i in range(n) if mask >> i & 1]
        if len(idx) < 2:
            continue
        try:
            x = _np.linalg.solve(R[_np.ix_(idx, idx)], sv[idx])
        except _np.linalg.LinAlgError:
            degraded = True
            continue
        if (x < -1e-9).any():
            continue                           # would bet against a contributor
        q = float(sv[idx] @ x)
        if q > 1e-12:
            best = max(best, math.sqrt(q))
    return best, degraded


def tangency_net_sharpe(contribs_by_ticker: dict[str, list[tuple]],
                        sim: dict[str, dict[str, float]],
                        weight_by_strat: dict[str, float],
                        shrink: float = TANGENCY_SHRINK_DEFAULT,
                        gamma: float | None = None) -> tuple[dict[str, float], int]:
    """Signed per-ticker conviction via the OPTIMAL-weights (tangency) combination
    Sharpe (operator directive 2026-07-27, replaces the fixed-proportional
    corr_adjusted_net_sharpe as the live S_adj).

    Per ticker: contributors split by direction; each side combines via
    _tangency_side_sharpe (non-negative tangency, subset-enumerated, shrunk R);
    S_net = S*_long − S*_short. Properties, by construction:
      • adding a CONFIRMING contributor never lowers |S_net| (monotone) — the
        BE-vs-WW anomaly (weak correlated confirmer diluting a fixed-weight
        combo) cannot occur;
      • a redundant near-clone adds ~nothing (no naive-sum double count) —
        which is why the ρ>=0.85 FOLD pre-filter is retired alongside this;
      • DISAGREEMENT still cancels: opposing sides subtract, so contested
        tickers gate out (the mig-140 near-cancellation intent survives);
      • never rewards disagreement: without the non-negativity constraint an
        opposed pair at ρ=0.3 would score ABOVE either solo (spread arbitrage
        a net per-ticker position cannot hold).
    `gamma` (task P1b — orthogonalization.resolve_tangency_gamma), when not
    None, overrides `shrink` for this call's shrinkage intensity. Left at the
    default None, behavior is byte-identical to before this parameter
    existed: `shrink` (itself defaulting to TANGENCY_SHRINK_DEFAULT) is used
    unchanged. No math changes — this only plumbs an already-resolved gamma
    through to the two side-solves.

    Same signature/return shape as corr_adjusted_net_sharpe; the second value
    counts tickers whose multi-member solves degraded to smaller subsets."""
    out: dict[str, float] = {}
    n_degraded = 0
    for ticker, contribs in contribs_by_ticker.items():
        longs_s, longs_id, shorts_s, shorts_id = [], [], [], []
        for sid, d in contribs:
            w = weight_by_strat.get(sid)
            if w is None or not d:
                continue
            if int(d) > 0:
                longs_id.append(sid)
                longs_s.append(float(w))
            else:
                shorts_id.append(sid)
                shorts_s.append(float(w))
        if not longs_s and not shorts_s:
            continue
        s_long, deg_l = _tangency_side_sharpe(longs_id, longs_s, sim, shrink, gamma=gamma)
        s_short, deg_s = _tangency_side_sharpe(shorts_id, shorts_s, sim, shrink, gamma=gamma)
        if deg_l or deg_s:
            n_degraded += 1
        out[ticker] = s_long - s_short
    return out, n_degraded


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
