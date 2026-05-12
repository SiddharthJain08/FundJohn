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

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2g-design.md
"""
from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Optional

MAX_OFF_DIAGONAL = 0.95   # clip Pearson to ±0.95 to avoid singular matrices
SPARSE_DEFAULT   = 0.3    # used when pair has <2 paired observations
DEFAULT_WINDOW_DAYS = 90
DEFAULT_BLEND_ALPHA = 0.6   # weight on PnL correlation; (1-alpha) on overlap


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
