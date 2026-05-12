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
from execution.handoff import read_handoff

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
    run_date_str = run_date.isoformat()

    # Load the handoff written by trade_handoff_builder for this cycle.
    # The handoff contains the same signal set the production trade step saw,
    # with enriched fields like p_t1, entry, stop, t1 already computed.
    handoff = read_handoff(run_date_str, 'structured')
    if handoff is None:
        print(f'[trade_parity] no handoff for {run_date_str}; nothing to do')
        return

    # Extract signals and regime from the handoff.
    signals = handoff.get('signals', [])
    print(f'[trade_parity] loaded {len(signals)} signals from handoff for {run_date_str}')

    if not signals:
        print('[trade_parity] no signals in handoff; nothing to do')
        return

    # Prepare signals for size_positions: map direction to numeric (+1/-1),
    # add signal_id, and rename handoff field names to match consolidator expectations.
    # Handoff uses: entry, stop, t1, t2 — consolidator expects: entry_price, stop_loss, take_profit_1, t1, t2
    # ticker_consolidator.py expects direction as numeric +1/-1 (not string).
    _DIR_MAP = {'LONG': 1, 'BUY': 1, 'BUY_VOL': 1,
                'SHORT': -1, 'SELL': -1, 'SELL_VOL': -1}
    for i, s in enumerate(signals):
        raw_dir = (s.get('direction') or 'LONG').upper()
        s['direction'] = _DIR_MAP.get(raw_dir, 1)
        # Map handoff field names to consolidator field names.
        if 'entry' in s and 'entry_price' not in s:
            s['entry_price'] = s['entry']
        if 'stop' in s and 'stop_loss' not in s:
            s['stop_loss'] = s['stop']
        if 't1' in s:
            # t1 is used by enrich_with_kelly, but consolidator bracket builder needs take_profit_1.
            if 'take_profit_1' not in s:
                s['take_profit_1'] = s['t1']
        # Assign a temporary signal_id based on array index (used for tracking).
        s['signal_id'] = str(i)

    # Regime comes from the handoff. Fall back to live market_regime if missing.
    regime_state = handoff.get('regime', {}).get('state')
    if not regime_state:
        uri = os.environ.get('POSTGRES_URI')
        if not uri:
            print('[trade_parity] no POSTGRES_URI and handoff has no regime; aborting')
            return
        try:
            conn_tmp = psycopg2.connect(uri)
            cur_tmp = conn_tmp.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur_tmp.execute("SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1")
            row = cur_tmp.fetchone()
            conn_tmp.close()
            if not row:
                print('[trade_parity] no regime state available; aborting')
                return
            regime_state = row['state']
        except Exception as e:
            print(f'[trade_parity] regime fallback failed: {e}; aborting')
            return

    regime = {'state': regime_state}
    print(f'[trade_parity] regime: {regime_state}')

    # Connect to database for regime params and strategy state.
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        print('[trade_parity] POSTGRES_URI not set; aborting')
        return

    conn = psycopg2.connect(uri)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

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
    # Note: handoff signals don't have real UUID signal_ids (they're computed from handoff
    # structure, not fetched from execution_signals table), so we omit contributing_signal_ids.
    inserted = 0
    for o in orders:
        cur.execute("""
          INSERT INTO parity_orders
            (signal_date, ticker, source, qty, notional_usd, bracket_json, contributing_signal_ids)
          VALUES (%s, %s, 'regime_blended', %s, %s, %s, %s::uuid[])
        """, (run_date_str, o['ticker'], o['qty'], o['notional_usd'],
              json.dumps(o['bracket']),
              []))  # No UUID signal_ids from handoff-sourced signals
        inserted += 1
    conn.commit()
    conn.close()
    print(f'[trade_parity] wrote {inserted} parity orders for {run_date_str}')

if __name__ == '__main__':
    main()
