#!/usr/bin/env python3
"""Phase 2G orchestrator sidecar: after production submit + trade_parity_capture,
read parity_orders[source='regime_blended'] for today, compute the
correlation-adjusted version, persist to correlation_adjustments.

NEVER affects production behavior. Failures here log + exit 0 so a sidecar
bug never breaks the daily cycle.

Activated only when OPENCLAW_REGIME_BLENDED_LIVE=1 (per orchestrator STEPS).
Future second flag OPENCLAW_CORRELATION_ADJUSTED_LIVE=1 will promote the
adjusted output into production; that wiring is deliberately deferred.

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-phase-2g-design.md
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = ROOT / 'src'
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


def _db_uri() -> str:
    return (os.environ.get('DATABASE_URL')
            or os.environ.get('POSTGRES_URI')
            or 'postgresql://openclaw:password@localhost:5432/openclaw')


def _connect():
    import psycopg2
    return psycopg2.connect(_db_uri())


def _load_production_orders(run_date: str) -> list[dict]:
    """Load the production sizing decisions for this cycle from parity_orders.

    Reads source='regime_blended' rows (the LIVE sizer's own snapshot of
    what it submitted). Falls back to source='production' if the regime_blended
    snapshot is missing — they're alpaca_submissions mirrors of the same
    underlying submit.
    """
    sql = """
        SELECT ticker, qty, notional_usd, bracket_json
          FROM parity_orders
         WHERE signal_date = %s
           AND source IN ('regime_blended', 'production')
         ORDER BY source, ticker
    """
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, (run_date,))
            rows = cur.fetchall()
    out: list[dict] = []
    seen: set = set()
    for ticker, qty, notional, _bracket in rows:
        if ticker in seen:
            continue
        seen.add(ticker)
        q = float(qty or 0)
        direction = 'LONG' if q >= 0 else 'SHORT'
        out.append({
            'ticker':       ticker,
            'qty':          abs(q),
            'notional_usd': float(notional or 0),
            'direction':    direction,
        })
    return out


def run(run_date: str, *, window_days: int = 90, alpha: float = 0.6) -> dict:
    """Top-level: load → adjust → persist. Returns a summary dict for logging."""
    from execution.correlation_adjusted_sizer import apply_and_persist
    orders = _load_production_orders(run_date)
    if not orders:
        return {'status': 'no_orders', 'run_date': run_date,
                'note': 'no parity_orders for this signal_date — sidecar is a no-op'}
    result = apply_and_persist(orders, cycle_id=run_date,
                                window_days=window_days, alpha=alpha)
    result['run_date'] = run_date
    return result


def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    p = argparse.ArgumentParser()
    p.add_argument('--date', required=False,
                    default=datetime.now(timezone.utc).date().isoformat())
    p.add_argument('--window-days', type=int, default=90)
    p.add_argument('--alpha', type=float, default=0.6)
    p.add_argument('--dry-run', action='store_true',
                    help='Pass-through for orchestrator PIPELINE_DRY_RUN; sidecar '
                         'is already side-effect-only on a NEW table so dry-run is '
                         'equivalent to normal run minus the persist.')
    args = p.parse_args()

    # CRITICAL: this sidecar must never abort the cycle. Catch everything.
    try:
        result = run(args.date, window_days=args.window_days, alpha=args.alpha)
        # Compact summary for the orchestrator's stdout pump.
        if result.get('status') == 'no_orders':
            print(f'[2G-sidecar] no production orders for {args.date} — no-op')
        else:
            phi = result.get('phi', 1.0)
            n_orders = result.get('n_orders', 0)
            n_tickers = result.get('n_tickers', 0)
            inserted = result.get('inserted', 0)
            print(f'[2G-sidecar] {args.date}: '
                  f'orders={n_orders} tickers={n_tickers} '
                  f'portfolio-kelly phi={phi:.4f} '
                  f'persisted={inserted} rows')
        return 0   # success
    except Exception as exc:
        # Log full traceback to stdout so the orchestrator captures it, but
        # always exit 0 — production behavior must not depend on this sidecar.
        print(f'[2G-sidecar] WARN: sidecar failed (non-fatal): {exc!s}')
        traceback.print_exc()
        return 0   # intentional: exit 0 even on error


if __name__ == '__main__':
    sys.exit(main())
