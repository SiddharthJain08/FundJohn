#!/usr/bin/env python3
"""Phase 2 parity wrapper — runs regime_blended_sizer in DRY-RUN
and writes its output to parity_orders. Does NOT submit to broker.

Pipeline orchestrator invokes this AFTER the production deterministic_sizer
'trade' step. The two sizers' outputs are diffed nightly by parity_diff.py.
"""
import argparse, json, os, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2, psycopg2.extras
from execution.regime_blended_sizer import size_positions

def _fetch_account_state_safe():
    """Best-effort account fetch; returns sane defaults if Alpaca call fails (DRY-RUN)."""
    try:
        from execution.alpaca_trader import _fetch_account_state
        import requests
        return _fetch_account_state(requests.Session())
    except Exception as e:
        print(f'[trade_parity] account fetch failed ({e}); using $100k default for DRY-RUN', file=sys.stderr)
        return {'equity': 100_000.0, 'regt_buying_power': 400_000.0,
                'long_market_value': 0.0, 'cash': 100_000.0}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=date.today().isoformat())
    args = ap.parse_args()
    run_date = date.fromisoformat(args.date)

    uri = os.environ['POSTGRES_URI']
    conn = psycopg2.connect(uri)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Load same signal set the production trade step saw.
    # Sub-task C (Path 1): extract p_t1 + strategy_memo_mult from signal_params JSONB.
    # Also alias target_1 twice: as take_profit_1 (for _independent_path / consolidator
    # bracket) and as t1 (for enrich_with_kelly which checks 't1 in s' first).
    cur.execute("""
      SELECT id AS signal_id, strategy_id, ticker, direction,
             entry_price, stop_loss,
             target_1 AS take_profit_1,
             target_1 AS t1,
             COALESCE((signal_params->>'p_t1')::float, 0.0) AS p_t1,
             COALESCE((signal_params->>'strategy_memo_mult')::float, 1.0) AS strategy_memo_mult,
             signal_params,
             regime_state
        FROM execution_signals
       WHERE signal_date = %s
    """, (run_date,))
    signals = [dict(r) for r in cur.fetchall()]
    # ticker_consolidator.py expects direction as numeric +1/-1 (not string).
    # enrich_with_kelly now handles numeric directions (+1→LONG, -1→SHORT).
    _DIR_MAP = {'LONG': 1, 'BUY': 1, 'BUY_VOL': 1,
                'SHORT': -1, 'SELL': -1, 'SELL_VOL': -1}
    for s in signals:
        raw_dir = (s.get('direction') or 'LONG').upper()
        s['direction'] = _DIR_MAP.get(raw_dir, 1)
    print(f'[trade_parity] loaded {len(signals)} signals for {run_date}')

    if not signals:
        print('[trade_parity] no signals; nothing to do')
        conn.close(); return

    # Current regime from market_regime table.
    cur.execute("SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        print('[trade_parity] no market_regime row; aborting')
        conn.close(); return
    regime = {'state': row['state']}
    print(f'[trade_parity] current regime: {regime["state"]}')

    # Regime sizing params for current regime.
    cur.execute("SELECT * FROM regime_sizer_params WHERE regime_state = %s", (regime['state'],))
    params = dict(cur.fetchone() or {'liquidity_param': 1.0,
                                       'min_signal_notional_usd': 100,
                                       'position_circuit_breaker_pct': 0.02})

    # Strategy state for cadence gate.
    cur.execute("SELECT * FROM strategy_state")
    strategy_state = {r['strategy_id']: dict(r) for r in cur.fetchall()}

    # Sub-task D: Fetch latest recommended_size_pct per strategy for HIGH_VOL/CRISIS
    # independent-mode sizing. recommended_size_pct is stored as a decimal fraction
    # (e.g. 0.02 = 2%), not as a percentage integer — do NOT divide by 100.
    cur.execute("""
      SELECT DISTINCT ON (strategy_id) strategy_id, recommended_size_pct
        FROM strategy_sizing_recommendations
       ORDER BY strategy_id, rec_date DESC
    """)
    target_by_strategy = {r['strategy_id']: float(r['recommended_size_pct'])
                          for r in cur.fetchall() if r['recommended_size_pct'] is not None}

    # Inject target_pct_nav into each signal (used by _independent_path).
    for sig in signals:
        sig['target_pct_nav'] = target_by_strategy.get(sig['strategy_id'])

    account = _fetch_account_state_safe()

    # In DRY-RUN, skip the LLM call (would cost real tokens) — pass a stub confirmer
    # that approves everything at multiplier=1.0. The parity comparison is about
    # the FORMULA output, not TradeJohn's overrides.
    def stub_confirmer(proposals, runner=None):
        return {p['ticker']: {'action': 'approve', 'multiplier': 1.0,
                              'rationale': 'parity_dry_run_stub'} for p in proposals}

    orders = size_positions(
        signals=signals, account_state=account, regime=regime,
        run_date=run_date, strategy_state=strategy_state,
        regime_params=params,
        confirmer=stub_confirmer,
    )
    print(f'[trade_parity] generated {len(orders)} orders')

    # Write to parity_orders only — DO NOT submit.
    # Sub-task B: contributing_signal_ids is now UUID[] (migration 070 fixed the column).
    # Pass UUIDs directly with ::uuid[] cast; remove the old bracket_json stashing workaround.
    inserted = 0
    for o in orders:
        cur.execute("""
          INSERT INTO parity_orders
            (signal_date, ticker, source, qty, notional_usd, bracket_json, contributing_signal_ids)
          VALUES (%s, %s, 'regime_blended', %s, %s, %s, %s::uuid[])
        """, (run_date, o['ticker'], o['qty'], o['notional_usd'],
              json.dumps(o['bracket']),
              [str(c['contributing_signal_id'])
               for c in o['contributions']
               if c.get('contributing_signal_id')]))
        inserted += 1
    conn.commit()
    conn.close()
    print(f'[trade_parity] wrote {inserted} parity orders for {run_date}')

if __name__ == '__main__':
    main()
