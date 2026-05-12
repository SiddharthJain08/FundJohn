#!/usr/bin/env python3
"""Phase 2 — mirror production-submitted orders into parity_orders.

Pipeline step `trade_parity_capture` runs after `alpaca`. It reads
the day's alpaca_submissions rows (whatever trade_agent_llm submitted —
LLM path or its deterministic_sizer fallback) and inserts equivalent
rows into parity_orders with source='production'.

Combined with regime_blended_sizer_parity (Task 12 wrapper writing
source='regime_blended'), this gives parity_diff.py (Task 16) two
sources to compare per signal_date.

Only rows with qty > 0 AND notional_usd IS NOT NULL are mirrored — zero-qty
rows represent rejected/unsized submissions that never hit the broker and
would inflate parity noise.

Spec: docs/superpowers/specs/2026-05-12-regime-blended-sizer-revision.md
      §"Correction 1" + §"Correction 5 — Parity contract"
"""
import argparse, json, os, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2, psycopg2.extras


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=date.today().isoformat(),
                    help='Run date in YYYY-MM-DD format (default: today)')
    args = ap.parse_args()
    run_date = date.fromisoformat(args.date)

    uri = os.environ['POSTGRES_URI']
    conn = psycopg2.connect(uri)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Read all alpaca_submissions for the run_date that actually went to
    # the broker (qty > 0 AND notional_usd IS NOT NULL).
    # Zero-qty rows are rejected/unsized submissions — mirroring them as
    # notional=0 would inflate only_in_production noise in the diff.
    cur.execute("""
      SELECT ticker, qty, entry_price, stop_price, target_price, notional_usd, id
        FROM alpaca_submissions
       WHERE run_date = %s
         AND qty > 0
         AND notional_usd IS NOT NULL
    """, (run_date,))
    rows = list(cur.fetchall())
    print(f'[trade_parity_capture] loaded {len(rows)} alpaca_submissions for {run_date}'
          f' (qty>0 and notional_usd IS NOT NULL)')

    if not rows:
        conn.close()
        return

    # Idempotency: skip if production rows already exist for this date.
    cur.execute(
        "SELECT COUNT(*) AS n FROM parity_orders WHERE signal_date=%s AND source='production'",
        (run_date,))
    existing = cur.fetchone()['n']
    if existing > 0:
        print(f'[trade_parity_capture] {existing} production rows already exist for {run_date}; skipping')
        conn.close()
        return

    inserted = 0
    for r in rows:
        # bracket_json matches the shape used by regime_blended_sizer_parity:
        # entry_price / stop_loss / take_profit_1 (Correction 3 renames apply to
        # regime_blended only; production mirrors actual alpaca_submissions columns).
        bracket = {
            'entry_price':   float(r['entry_price'] or 0),
            'stop_loss':     float(r['stop_price'] or 0),
            'take_profit_1': float(r['target_price'] or 0),
        }
        notional = float(r['notional_usd'])

        cur.execute("""
          INSERT INTO parity_orders
            (signal_date, ticker, source, qty, notional_usd, bracket_json, contributing_signal_ids)
          VALUES (%s, %s, 'production', %s, %s, %s, %s::uuid[])
        """, (run_date, r['ticker'], float(r['qty']), notional,
              json.dumps(bracket),
              [str(r['id'])]))  # alpaca_submissions.id is UUID
        inserted += 1

    conn.commit()
    conn.close()
    print(f'[trade_parity_capture] inserted {inserted} production rows into parity_orders')


if __name__ == '__main__':
    main()
