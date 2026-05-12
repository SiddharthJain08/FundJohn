#!/usr/bin/env python3
"""Manual smoke test — runs regime_blended_sizer against today's signals
in pure DRY-RUN. Pretty-prints planned order book. No DB writes, no broker
submit, no LLM call.

Use after each PR before deploying:
  python3 scripts/dry_run_new_sizer.py [--date YYYY-MM-DD]

Compared to src/execution/regime_blended_sizer_parity.py (which writes to
parity_orders for nightly diff comparison), this script is purely read-only
and human-readable.
"""
import argparse, json, os, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2, psycopg2.extras
from execution.regime_blended_sizer import size_positions
from execution.handoff import read_handoff

def _account_state_safe():
    """Best-effort account fetch; defaults if Alpaca unreachable."""
    try:
        from execution.alpaca_trader import _fetch_account_state
        import requests
        return _fetch_account_state(requests.Session())
    except Exception as e:
        print(f'[dry_run] account fetch failed ({e}); using $100k default', file=sys.stderr)
        return {'equity': 100_000.0, 'regt_buying_power': 400_000.0,
                'long_market_value': 0.0, 'cash': 100_000.0}

def _stub_confirmer(proposals, runner=None):
    """No LLM call in DRY-RUN — formula-result rides through."""
    return {p['ticker']: {'action': 'approve', 'multiplier': 1.0,
                          'rationale': 'dry_run_stub'} for p in proposals}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=date.today().isoformat())
    args = ap.parse_args()
    run_date = date.fromisoformat(args.date)

    uri = os.environ.get('POSTGRES_URI', 'postgresql://openclaw:password@localhost:5432/openclaw')

    # Load same handoff trade_agent_llm reads
    handoff = read_handoff(args.date, 'structured')
    if handoff is None:
        print(f'[dry_run] no handoff for {args.date}; nothing to size', file=sys.stderr)
        return 1
    signals = handoff.get('signals', []) if isinstance(handoff, dict) else []
    print(f'[dry_run] loaded {len(signals)} signals from handoff for {run_date}')

    if not signals:
        return 0

    # Field aliasing — handoff uses entry/stop/t1, parity wrapper aliases.
    # Mirror the same aliasing here. Also map direction to numeric (+1/-1).
    _DIR_MAP = {'LONG': 1, 'BUY': 1, 'BUY_VOL': 1,
                'SHORT': -1, 'SELL': -1, 'SELL_VOL': -1}
    for i, s in enumerate(signals):
        # Map direction string to numeric.
        raw_dir = (s.get('direction') or 'LONG').upper()
        s['direction'] = _DIR_MAP.get(raw_dir, 1)
        # Map handoff field names to consolidator field names.
        if 'entry' in s and 'entry_price' not in s: s['entry_price'] = s['entry']
        if 'stop' in s and 'stop_loss' not in s: s['stop_loss'] = s['stop']
        if 't1' in s and 'take_profit_1' not in s: s['take_profit_1'] = s['t1']
        # Assign a temporary signal_id.
        s['signal_id'] = str(i)

    conn = psycopg2.connect(uri)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        print('[dry_run] no market_regime; aborting', file=sys.stderr)
        conn.close(); return 2
    regime = {'state': row['state']}
    print(f'[dry_run] regime: {regime["state"]}')

    cur.execute("SELECT * FROM regime_sizer_params WHERE regime_state=%s", (regime['state'],))
    params = dict(cur.fetchone() or {'liquidity_param': 1.0, 'min_signal_notional_usd': 100,
                                       'position_circuit_breaker_pct': 0.02})

    cur.execute("SELECT * FROM strategy_state")
    strategy_state = {r['strategy_id']: dict(r) for r in cur.fetchall()}

    # Inject target_pct_nav from strategy_sizing_recommendations (used by HIGH_VOL/CRISIS path).
    cur.execute("""
      SELECT DISTINCT ON (strategy_id) strategy_id, recommended_size_pct
        FROM strategy_sizing_recommendations
       ORDER BY strategy_id, rec_date DESC
    """)
    target_by_strategy = {r['strategy_id']: float(r['recommended_size_pct'])
                          for r in cur.fetchall() if r['recommended_size_pct'] is not None}
    for sig in signals:
        sig['target_pct_nav'] = target_by_strategy.get(sig['strategy_id'])

    conn.close()

    account = _account_state_safe()

    orders = size_positions(
        signals=signals, account_state=account, regime=regime,
        run_date=run_date, strategy_state=strategy_state,
        regime_params=params, confirmer=_stub_confirmer,
    )

    print(f'\n=== DRY-RUN regime_blended_sizer ({run_date}, regime={regime["state"]}) ===')
    print(f'Account equity: ${account["equity"]:,.0f}')
    print(f'Regt buying power: ${account["regt_buying_power"]:,.0f}')
    print(f'Liquidity param λ: {params["liquidity_param"]:.2f}')
    print(f'Input signals: {len(signals)}')
    print(f'Output orders: {len(orders)}')

    if orders:
        total_notional = sum(o['notional_usd'] for o in orders)
        leverage = total_notional / account['equity'] if account['equity'] > 0 else 0
        print(f'Total notional: ${total_notional:,.0f} ({leverage:.1f}x equity)')
        print()
        print(f'{"Ticker":<8} {"Dir":>4} {"Qty":>12} {"Notional":>14}  Source')
        print('-' * 60)
        for o in sorted(orders, key=lambda x: -x['notional_usd'])[:50]:
            print(f'{o["ticker"]:<8} {o["direction"]:>4} {o["qty"]:>12,.2f} ${o["notional_usd"]:>12,.0f}  {o["source_mode"]}')
        if len(orders) > 50:
            print(f'... and {len(orders) - 50} more orders')
    else:
        print('(no orders generated)')

    return 0

if __name__ == '__main__':
    sys.exit(main())
