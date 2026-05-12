#!/usr/bin/env python3
"""Phase 2G correlation-adjusted portfolio sizer (DRY-RUN sidecar).

Computes a portfolio-Kelly downsize factor for each ticker by comparing
the correlated portfolio variance against the independent-baseline
variance. Never upsizes (phi clamped at 1.0). Writes a sidecar log to
`correlation_adjustments` for parity diffing against production.

ZERO production behavior change. Caller wires this in AFTER production
submit; failures here log WARN but don't affect the cycle.

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2g-design.md
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SRC = Path(__file__).resolve().parents[1]
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _signed_exposure(order: dict) -> float:
    """Notional with +/- sign based on direction. LONG=+, SHORT=-."""
    notional = float(order.get('notional_usd') or 0)
    direction = (order.get('direction') or 'LONG').upper()
    sign = 1.0 if direction == 'LONG' else -1.0
    return sign * notional


def _portfolio_variance(orders: list[dict],
                          sigma: dict[str, dict[str, float]]) -> tuple[float, float]:
    """Returns (sigma_p_squared, sigma_independent_squared).

    sigma_p² = w^T Σ w  with w_i = signed notional of ticker i
    sigma_ind² = Σ w_i² (treats every ticker as uncorrelated)
    """
    weights: dict[str, float] = {}
    for o in orders:
        ticker = o.get('ticker')
        if not ticker:
            continue
        weights[ticker] = weights.get(ticker, 0.0) + _signed_exposure(o)

    sigma_p_sq = 0.0
    sigma_ind_sq = 0.0
    for ti, wi in weights.items():
        sigma_ind_sq += wi * wi
        for tj, wj in weights.items():
            rho = sigma.get(ti, {}).get(tj, 0.0 if ti != tj else 1.0)
            sigma_p_sq += wi * wj * rho
    return sigma_p_sq, sigma_ind_sq


def _portfolio_kelly_phi(sigma_p_sq: float, sigma_ind_sq: float) -> float:
    """phi = min(1.0, sqrt(sigma_ind² / sigma_p²))

    When correlations stack risk (sigma_p > sigma_ind), phi < 1 → downsize.
    When correlations cancel risk (sigma_p < sigma_ind), phi would be > 1
    → clamp at 1.0 (never upsize).
    """
    if sigma_p_sq <= 0:
        return 1.0
    raw = math.sqrt(sigma_ind_sq / sigma_p_sq)
    return min(1.0, raw)


TOP_N_FOR_CORRELATION = 20   # Phase 2G-v2: only build matrix among the
                              # largest TOP_N orders by |notional|; smaller
                              # orders bypass adjustment (phi=1.0). Avoids
                              # the N² SPARSE_DEFAULT stacking that produced
                              # phi=0.218 on a 104-order portfolio.


def adjust(orders: list[dict],
           sigma: dict[str, dict[str, float]],
           top_n: int = TOP_N_FOR_CORRELATION) -> tuple[list[dict], list[dict]]:
    """Returns (adjusted_orders, per_ticker_log).

    Phase 2G-v2: orders are partitioned into top-N (by |notional|) +
    rest. The portfolio-Kelly phi is computed from the top-N portfolio
    variance and applied to those orders. The rest pass through unchanged
    (phi=1.0) — they're small enough that whatever correlation they have
    with the rest of the book doesn't move overall risk meaningfully.

    adjusted_orders: same shape as input but with qty + notional_usd
                     scaled by phi for orders in the top-N. Order is
                     preserved.
    per_ticker_log:  one row per order including the phi applied (1.0
                     for orders outside the top-N).
    """
    if not orders:
        return [], []
    # Partition: top-N by |notional|, rest passes through.
    indexed = list(enumerate(orders))
    indexed.sort(key=lambda x: abs(float(x[1].get('notional_usd') or 0)), reverse=True)
    top_idx = {i for i, _ in indexed[:top_n]}
    top_orders = [orders[i] for i in sorted(top_idx)]

    if top_orders:
        sigma_p_sq, sigma_ind_sq = _portfolio_variance(top_orders, sigma)
        phi_top = _portfolio_kelly_phi(sigma_p_sq, sigma_ind_sq)
    else:
        phi_top = 1.0

    adjusted: list[dict] = []
    log: list[dict] = []
    for i, o in enumerate(orders):
        in_top = i in top_idx
        phi = phi_top if in_top else 1.0
        adj_qty = (o.get('qty') or 0) * phi
        adj_notional = (o.get('notional_usd') or 0) * phi
        adj_qty_floor = math.floor(adj_qty) if adj_qty >= 0 else math.ceil(adj_qty)
        adj_o = dict(o)
        adj_o['qty'] = adj_qty_floor
        adj_o['notional_usd'] = adj_notional
        adj_o['cap_scale_applied'] = (o.get('cap_scale_applied') or 1.0) * phi
        adjusted.append(adj_o)
        log.append({
            'ticker':              o.get('ticker'),
            'direction':           o.get('direction'),
            'production_qty':      o.get('qty'),
            'production_notional': o.get('notional_usd'),
            'phi':                 phi,
            'in_top_n':            in_top,
            'adjusted_qty':        adj_qty_floor,
            'adjusted_notional':   adj_notional,
        })
    return adjusted, log


def apply_and_persist(orders: list[dict], cycle_id: Optional[str] = None,
                       window_days: int = 90,
                       alpha: float = 0.6) -> dict:
    """End-to-end: build correlation matrix from production orders'
    tickers, compute adjustment, persist to correlation_adjustments."""
    from execution.correlation_matrix import effective_correlation
    tickers = sorted({o['ticker'] for o in orders if o.get('ticker')})
    if not tickers:
        return {'status': 'no_orders', 'adjustments': []}
    sigma = effective_correlation(tickers, window_days=window_days, alpha=alpha)
    adjusted, log = adjust(orders, sigma)

    # Persist rows (one per order log entry)
    inserted = 0
    if log:
        with _connect() as conn:
            with conn.cursor() as cur:
                for entry in log:
                    # Capture the relevant slice of sigma for audit
                    sigma_slice = {k: v for k, v in sigma.get(entry['ticker'], {}).items()
                                    if k in tickers}
                    cur.execute("""
                        INSERT INTO correlation_adjustments
                            (cycle_id, ticker, production_qty, production_notional,
                             direction, portfolio_kelly_phi,
                             adjusted_qty, adjusted_notional, correlation_input)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """, (cycle_id, entry['ticker'],
                          entry['production_qty'], entry['production_notional'],
                          entry['direction'], entry['phi'],
                          entry['adjusted_qty'], entry['adjusted_notional'],
                          json.dumps(sigma_slice, default=str)))
                    inserted += 1
            conn.commit()

    return {
        'status':           'OK',
        'cycle_id':         cycle_id,
        'n_tickers':        len(tickers),
        'n_orders':         len(orders),
        'phi':              log[0]['phi'] if log else 1.0,
        'inserted':         inserted,
        'adjustments':      log,
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--cycle', default=None,
                    help='Cycle id to tag the persisted rows with')
    p.add_argument('--orders-json', default=None,
                    help='Path to JSON file with production orders list')
    p.add_argument('--window-days', type=int, default=90)
    p.add_argument('--alpha', type=float, default=0.6)
    args = p.parse_args()
    if not args.orders_json:
        print('--orders-json required (path to JSON list of {ticker, qty, notional_usd, direction})',
              file=sys.stderr)
        return 2
    orders = json.loads(Path(args.orders_json).read_text())
    result = apply_and_persist(orders, cycle_id=args.cycle,
                                window_days=args.window_days, alpha=args.alpha)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == '__main__':
    sys.exit(main())
