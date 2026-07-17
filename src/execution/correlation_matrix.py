#!/usr/bin/env python3
"""Per-cycle correlation matrix construction for the Phase 2G sidecar.

Two sources blended (alpha-weighted):
  1. Pearson correlation on realized PnL pct from signal_pnl over rolling
     window (default 90d), joined by (signal_date, ticker).
  2. Strategy-overlap-projected ticker correlation: Jaccard of strategies
     that fired on a given (date, ticker), projected to ticker pairs.

Caveats (read before relying on output):
- Pearson on sparse data is unstable. We clip off-diagonal at ±0.95 and
  default insufficient-pair entries to SPARSE_DEFAULT (0.3).
- Strategy-overlap correlation is a PROXY: same strategies firing on same
  tickers ≠ same realized returns. The blend (alpha=0.6 PnL, 0.4 overlap)
  reflects this — PnL is the truer signal when it exists.
- No regime conditioning. Future Phase 2H could compute per-regime
  matrices and interpolate by current state.

Spec: docs/archive/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2g-design.md
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Optional

MAX_OFF_DIAGONAL = 0.95   # clip Pearson to ±0.95 to avoid singular matrices
# Phase 2G-v2 (2026-05-12): SPARSE_DEFAULT lowered from 0.3 → 0.05 after
# first-cycle smoke produced phi=0.218 across 104 tickers. With most pairs
# lacking joint realized-PnL history in the 90-day window, a 0.3 baseline
# stacked across thousands of pair entries inflated portfolio variance
# 30× above independent. 0.05 treats unknown pairs as essentially
# uncorrelated (the math's null hypothesis); pairs WITH data still
# produce real Pearson values clipped at ±0.95.
SPARSE_DEFAULT   = 0.05
DEFAULT_WINDOW_DAYS = 90
DEFAULT_BLEND_ALPHA = 0.6   # weight on PnL correlation; (1-alpha) on overlap

# Phase 2H (2026-05-13): per-regime correlation matrices + state-probability blend.
# Defaults are env-overridable; documented in
# docs/archive/superpowers/specs/2026-05-13-regime-blended-sizer-phase-2h-design.md
REGIME_STATES = ('LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS')
CRISIS_CORRELATION_PRIOR = float(os.environ.get('OPENCLAW_CRISIS_CORRELATION_PRIOR', '0.7'))
# Phase 2H coverage gate: regimes with > 0 trades among requested tickers
# get classified 'real' — Pearson + SPARSE_DEFAULT handles within-regime
# sparsity at the pair level. The 'fallback_global' / 'stress_prior'
# branches fire only when *zero* trades exist in a regime.
CRISIS_FALLBACK_USES_STRESS_PRIOR = True   # off => CRISIS-with-no-data behaves like other-with-no-data


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _pearson(xs: list[float], ys: list[float]) -> Optional[float]:
    """Pearson correlation; None when fewer than 2 paired observations or
    when either series has zero variance."""
    n = len(xs)
    if n != len(ys) or n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    num = sum((xs[i] - mx) * (ys[i] - my) for i in range(n))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return None
    return num / (dx * dy)


def correlation_from_pnl(pnls_by_ticker_date: dict[str, dict[str, float]],
                          tickers: list[str]) -> dict[str, dict[str, float]]:
    """Build correlation matrix from per-ticker-per-date PnL data.

    pnls_by_ticker_date[ticker][date_str] = pnl_pct

    Off-diagonals are clipped to [-MAX_OFF_DIAGONAL, +MAX_OFF_DIAGONAL].
    Pairs with insufficient overlap default to SPARSE_DEFAULT.
    Diagonal is always 1.0.
    """
    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for i, ti in enumerate(tickers):
        out[ti][ti] = 1.0
        for tj in tickers[i + 1:]:
            dates_i = set(pnls_by_ticker_date.get(ti, {}).keys())
            dates_j = set(pnls_by_ticker_date.get(tj, {}).keys())
            paired = sorted(dates_i & dates_j)
            if len(paired) < 2:
                rho = SPARSE_DEFAULT
            else:
                xs = [pnls_by_ticker_date[ti][d] for d in paired]
                ys = [pnls_by_ticker_date[tj][d] for d in paired]
                rho = _pearson(xs, ys)
                if rho is None:
                    rho = SPARSE_DEFAULT
                else:
                    rho = max(-MAX_OFF_DIAGONAL, min(MAX_OFF_DIAGONAL, rho))
            out[ti][tj] = rho
            out[tj][ti] = rho
    return out


def _load_pnls_by_ticker_date(tickers: list[str],
                                window_days: int) -> dict[str, dict[str, float]]:
    """Load realized PnLs from DB grouped by (ticker, signal_date)."""
    if not tickers:
        return {}
    sql = """
        SELECT es.ticker, es.signal_date::text AS signal_date,
               sp.realized_pnl_pct::float AS realized_pnl_pct
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE es.ticker = ANY(%s::text[])
           AND sp.realized_pnl_pct IS NOT NULL
           AND sp.closed_at >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
    """
    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (list(tickers), window_days))
            for ticker, sig_date, pnl in cur.fetchall():
                # If a ticker has multiple signals on the same date, average them.
                if sig_date in out[ticker]:
                    out[ticker][sig_date] = (out[ticker][sig_date] + float(pnl)) / 2.0
                else:
                    out[ticker][sig_date] = float(pnl)
    return out


def correlation_pnl(tickers: list[str],
                     window_days: int = DEFAULT_WINDOW_DAYS) -> dict[str, dict[str, float]]:
    """End-to-end: load PnL by (ticker, date), compute correlation matrix."""
    pnls = _load_pnls_by_ticker_date(tickers, window_days)
    return correlation_from_pnl(pnls, tickers)


def _load_strategy_overlap_for_tickers(tickers: list[str],
                                        window_days: int) -> dict[str, dict[str, float]]:
    """Project strategy_signal_overlap to ticker pairs.

    Heuristic: for each ticker pair (ti, tj), count execution_signals where
    different strategies fired on overlapping dates. Use Jaccard of the
    sets of strategies that have ever traded each ticker as a proxy.
    """
    if not tickers:
        return {t: {} for t in tickers}
    sql = """
        SELECT ticker, strategy_id
          FROM execution_signals
         WHERE ticker = ANY(%s::text[])
           AND signal_date >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
    """
    strats_by_ticker: dict[str, set] = {t: set() for t in tickers}
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (list(tickers), window_days))
            for ticker, strategy_id in cur.fetchall():
                strats_by_ticker[ticker].add(strategy_id)

    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for i, ti in enumerate(tickers):
        out[ti][ti] = 1.0
        for tj in tickers[i + 1:]:
            a = strats_by_ticker[ti]
            b = strats_by_ticker[tj]
            if not a or not b:
                rho = SPARSE_DEFAULT
            else:
                u = len(a | b)
                if u == 0:
                    rho = SPARSE_DEFAULT
                else:
                    j = len(a & b) / u
                    # Jaccard is in [0, 1]; map to [-MAX_OFF_DIAGONAL, MAX_OFF_DIAGONAL]
                    # but heuristically keep it in [0, MAX_OFF_DIAGONAL] since "shared
                    # strategies → likely positive correlation in returns".
                    rho = min(j, MAX_OFF_DIAGONAL)
            out[ti][tj] = rho
            out[tj][ti] = rho
    return out


def blend(sigma_a: dict[str, dict[str, float]],
          sigma_b: dict[str, dict[str, float]],
          alpha: float = DEFAULT_BLEND_ALPHA) -> dict[str, dict[str, float]]:
    """Convex combination of two correlation matrices. Same key set required."""
    tickers = sorted(set(sigma_a.keys()) | set(sigma_b.keys()))
    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for ti in tickers:
        for tj in tickers:
            if ti == tj:
                out[ti][tj] = 1.0
                continue
            a = sigma_a.get(ti, {}).get(tj, SPARSE_DEFAULT)
            b = sigma_b.get(ti, {}).get(tj, SPARSE_DEFAULT)
            out[ti][tj] = alpha * a + (1.0 - alpha) * b
    return out


def effective_correlation(tickers: list[str],
                           window_days: int = DEFAULT_WINDOW_DAYS,
                           alpha: float = DEFAULT_BLEND_ALPHA
                           ) -> dict[str, dict[str, float]]:
    """End-to-end: PnL correlation + overlap-projected correlation, blended."""
    sigma_pnl     = correlation_pnl(tickers, window_days=window_days)
    sigma_overlap = _load_strategy_overlap_for_tickers(tickers, window_days=window_days)
    return blend(sigma_pnl, sigma_overlap, alpha=alpha)


# ─── Phase 2H — per-regime correlation matrices ────────────────────────── #

def _load_pnls_by_regime_ticker_date(tickers: list[str], window_days: int
                                       ) -> dict[str, dict[str, dict[str, float]]]:
    """Returns {regime_state: {ticker: {date: pnl_pct}}}. One slice per regime."""
    if not tickers:
        return {r: {t: {} for t in tickers} for r in REGIME_STATES}
    sql = """
        SELECT es.regime_state, es.ticker, es.signal_date::text AS signal_date,
               sp.realized_pnl_pct::float AS realized_pnl_pct
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE es.ticker = ANY(%s::text[])
           AND sp.realized_pnl_pct IS NOT NULL
           AND sp.closed_at >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
           AND es.regime_state = ANY(%s::text[])
    """
    out: dict[str, dict[str, dict[str, float]]] = {
        r: {t: {} for t in tickers} for r in REGIME_STATES}
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (list(tickers), window_days, list(REGIME_STATES)))
            for regime, ticker, sig_date, pnl in cur.fetchall():
                if regime not in out or ticker not in out[regime]:
                    continue
                slot = out[regime][ticker]
                if sig_date in slot:
                    slot[sig_date] = (slot[sig_date] + float(pnl)) / 2.0
                else:
                    slot[sig_date] = float(pnl)
    return out


def _trade_counts_by_regime(tickers: list[str], window_days: int) -> dict[str, int]:
    """Returns {regime_state: total_trade_count} for sparsity classification."""
    if not tickers:
        return {r: 0 for r in REGIME_STATES}
    sql = """
        SELECT es.regime_state, count(*)
          FROM signal_pnl sp
          JOIN execution_signals es ON es.id = sp.signal_id
         WHERE es.ticker = ANY(%s::text[])
           AND sp.realized_pnl_pct IS NOT NULL
           AND sp.closed_at >= CURRENT_DATE - (%s::int * INTERVAL '1 day')
      GROUP BY es.regime_state
    """
    out = {r: 0 for r in REGIME_STATES}
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (list(tickers), window_days))
            for regime, n in cur.fetchall():
                if regime in out:
                    out[regime] = int(n)
    return out


def correlation_from_pnl_by_regime(tickers: list[str],
                                     window_days: int = DEFAULT_WINDOW_DAYS
                                     ) -> dict[str, dict[str, dict[str, float]]]:
    """One Pearson correlation matrix per regime, from that regime's slice
    of realized PnL. Diagonals 1.0, off-diagonals clipped, sparse pairs
    get SPARSE_DEFAULT."""
    by_regime = _load_pnls_by_regime_ticker_date(tickers, window_days)
    return {r: correlation_from_pnl(by_regime[r], tickers) for r in REGIME_STATES}


def crisis_stress_prior(tickers: list[str],
                          rho: Optional[float] = None
                          ) -> dict[str, dict[str, float]]:
    """Flat-off-diagonal correlation matrix used when CRISIS has no realized
    data. rho defaults to CRISIS_CORRELATION_PRIOR (env-overridable).
    Clipped at MAX_OFF_DIAGONAL."""
    r = float(rho) if rho is not None else CRISIS_CORRELATION_PRIOR
    r = max(-MAX_OFF_DIAGONAL, min(MAX_OFF_DIAGONAL, r))
    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for ti in tickers:
        for tj in tickers:
            out[ti][tj] = 1.0 if ti == tj else r
    return out


def current_state_probabilities() -> dict[str, float]:
    """Reads market_regime.state_probabilities for the latest row.
    Returns dict over the 4 regimes summing to 1.0. Missing regimes
    default to 0.0. If no row exists, returns full weight on the single
    `state` column (HMM fallback)."""
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT state, regime_data
                  FROM market_regime
                 ORDER BY id DESC LIMIT 1
            """)
            row = cur.fetchone()
    out = {r: 0.0 for r in REGIME_STATES}
    if not row:
        return out
    state, data = row
    if isinstance(data, dict):
        probs = data.get('state_probabilities') or {}
        for r in REGIME_STATES:
            if r in probs:
                try:
                    out[r] = float(probs[r])
                except (TypeError, ValueError):
                    pass
    total = sum(out.values())
    if total <= 0:
        # No state_probabilities — fall back to hard one-hot on `state`
        if state in out:
            out[state] = 1.0
        return out
    return {r: out[r] / total for r in REGIME_STATES}


def _per_regime_matrices_with_coverage(tickers: list[str],
                                         window_days: int,
                                         alpha: float
                                         ) -> tuple[dict[str, dict[str, dict[str, float]]],
                                                     dict[str, str]]:
    """Returns ({regime: matrix}, {regime: classification}).

    Classification ∈ {'real', 'fallback_global', 'stress_prior'}:
      'real'            — n_trades_in_regime > 0 for the requested tickers.
                          Per-regime Pearson runs; pair-level sparsity is
                          handled by SPARSE_DEFAULT (same as the global path).
      'fallback_global' — n_trades_in_regime == 0 AND regime != CRISIS.
                          Use the global 2G blended matrix.
      'stress_prior'    — n_trades_in_regime == 0 AND regime == CRISIS.
                          Use crisis_stress_prior (flat off-diagonal at
                          CRISIS_CORRELATION_PRIOR). Operator-selected
                          option from the 2H scope question.
    """
    counts = _trade_counts_by_regime(tickers, window_days)
    global_matrix: Optional[dict[str, dict[str, float]]] = None
    stress_matrix: Optional[dict[str, dict[str, float]]] = None
    by_regime_pnls: Optional[dict[str, dict[str, dict[str, float]]]] = None

    coverage: dict[str, str] = {}
    matrices: dict[str, dict[str, dict[str, float]]] = {}
    for r in REGIME_STATES:
        n = counts.get(r, 0)
        if n > 0:
            if by_regime_pnls is None:
                by_regime_pnls = _load_pnls_by_regime_ticker_date(tickers, window_days)
            matrices[r] = correlation_from_pnl(by_regime_pnls[r], tickers)
            coverage[r] = 'real'
        elif r == 'CRISIS' and CRISIS_FALLBACK_USES_STRESS_PRIOR:
            if stress_matrix is None:
                stress_matrix = crisis_stress_prior(tickers)
            matrices[r] = stress_matrix
            coverage[r] = 'stress_prior'
        else:
            if global_matrix is None:
                global_matrix = effective_correlation(
                    tickers, window_days=window_days, alpha=alpha)
            matrices[r] = global_matrix
            coverage[r] = 'fallback_global'
    return matrices, coverage


def blended_correlation_by_state(tickers: list[str],
                                   window_days: int = DEFAULT_WINDOW_DAYS,
                                   alpha: float = DEFAULT_BLEND_ALPHA
                                   ) -> tuple[dict[str, dict[str, float]],
                                               dict[str, float],
                                               dict[str, str]]:
    """End-to-end: builds per-regime matrices with coverage classification,
    reads current state probabilities, returns the convex-combination
    correlation matrix used by the 2H sidecar.

    Returns (sigma_eff, blend_weights, coverage).
      sigma_eff[i][j] = sum_r p(r) · sigma_r[i][j]   (off-diagonal)
      sigma_eff[i][i] = 1.0
    """
    if not tickers:
        probs = current_state_probabilities()
        return {}, probs, {r: 'no_orders' for r in REGIME_STATES}

    matrices, coverage = _per_regime_matrices_with_coverage(
        tickers, window_days=window_days, alpha=alpha)
    probs = current_state_probabilities()

    out: dict[str, dict[str, float]] = {t: {} for t in tickers}
    for ti in tickers:
        for tj in tickers:
            if ti == tj:
                out[ti][tj] = 1.0
                continue
            acc = 0.0
            for r in REGIME_STATES:
                w = probs.get(r, 0.0)
                if w <= 0:
                    continue
                acc += w * matrices[r].get(ti, {}).get(tj, SPARSE_DEFAULT)
            out[ti][tj] = max(-MAX_OFF_DIAGONAL,
                                min(MAX_OFF_DIAGONAL, acc))
    return out, probs, coverage


def main():
    import argparse
    import json
    p = argparse.ArgumentParser()
    p.add_argument('--tickers', required=True,
                    help='comma-separated ticker list')
    p.add_argument('--window-days', type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument('--alpha', type=float, default=DEFAULT_BLEND_ALPHA)
    args = p.parse_args()
    tickers = [t.strip().upper() for t in args.tickers.split(',') if t.strip()]
    matrix = effective_correlation(tickers, window_days=args.window_days,
                                    alpha=args.alpha)
    print(json.dumps(matrix, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
