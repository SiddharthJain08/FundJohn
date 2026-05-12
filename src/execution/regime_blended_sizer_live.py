#!/usr/bin/env python3
"""Phase 3 LIVE wrapper — regime_blended_sizer producing real broker submissions.

Pipeline orchestrator's `trade` step calls this when OPENCLAW_REGIME_BLENDED_LIVE=1.
Reads the same structured handoff trade_agent_llm reads, runs the new sizer (with real
TradeJohn confirmer + 25% NAV cap), and writes the sized handoff via
_finalize_sized_payload — same path the existing alpaca_executor reads from.

Output format: the sized handoff uses the same payload['orders'] shape as
deterministic_sizer/trade_agent_llm:
  {ticker, strategy_id, direction, entry, stop, t1, t2, pct_nav, shares,
   notional_usd, kelly_final, ev, p_t1, source_mode, contributing_strategies}

alpaca_executor reads payload['orders'] and uses pct_nav for daily-cap math
and strategy_id for the already_executed() idempotency check.

Spec: docs/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md §"Phase 3"
"""
import argparse, json, os, sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2, psycopg2.extras
from execution.regime_blended_sizer import size_positions
from execution.handoff import read_handoff
from execution.tradejohn_confirmer import confirm as real_confirmer
from execution.trade_agent_llm import _finalize_sized_payload


def _build_sized_payload(orders: list[dict], handoff: dict,
                         equity: float = 100_000.0) -> dict:
    """Convert size_positions() output into the sized-handoff format
    that _finalize_sized_payload + alpaca_executor expect.

    Key mapping from regime_blended_sizer output → alpaca_executor input:
      ticker                             → ticker
      direction (int +1/-1)              → direction (lowercase 'long'/'short')
      bracket.entry_price                → entry
      bracket.stop_loss                  → stop
      bracket.take_profit_1              → t1
      qty (signed float)                 → shares (int, absolute value)
      abs(notional_usd) / equity         → pct_nav  (REQUIRED by alpaca_executor daily-cap math)
      notional_usd                       → notional_usd
      pct_nav                            → kelly_final (best proxy; carry through)
      contributions[0].strategy_id      → strategy_id (REQUIRED by already_executed())
      contributions (full list)          → contributions  (attribution)
      source_mode                        → source_mode
    """
    payload = {
        'cycle_date': handoff.get('cycle_date'),
        'regime':     handoff.get('regime', {}),
        'orders':     [],
        'vetoed':     handoff.get('prefiltered', []),  # carry over upstream filters
    }

    nav = max(equity, 1.0)  # guard against zero-division

    for o in orders:
        dir_int = o['direction']
        dir_str = 'long' if dir_int > 0 else 'short'

        entry       = float(o['bracket']['entry_price'])
        notional    = abs(float(o['notional_usd']))
        pct_nav     = round(notional / nav, 6)
        shares_raw  = abs(float(o['qty']))
        shares      = max(1, int(shares_raw)) if shares_raw >= 0.5 else 0

        # strategy_id: for consolidate-mode orders, contributions may have
        # multiple strategies. Use the first contributing strategy_id so
        # alpaca_executor's already_executed() check works correctly.
        # Downstream attribution uses the full contributing_strategies list.
        contributions = o.get('contributions', [])
        strategy_id = (contributions[0].get('strategy_id')
                       if contributions else 'unknown')

        order = {
            'ticker':                  o['ticker'],
            'strategy_id':             strategy_id,
            'direction':               dir_str,
            'entry':                   entry,
            'stop':                    float(o['bracket']['stop_loss']),
            't1':                      float(o['bracket']['take_profit_1']),
            't2':                      None,   # regime_blended_sizer does not produce t2
            'pct_nav':                 pct_nav,
            'shares':                  shares,
            'notional_usd':            round(float(o['notional_usd']), 2),
            'kelly_final':             pct_nav,  # best proxy; not a true Kelly calculation
            'ev':                      None,     # not propagated through consolidator path
            'p_t1':                    None,     # not propagated through consolidator path
            'source_mode':             o.get('source_mode'),
            'contributing_strategies': [c.get('strategy_id') for c in contributions],
            'contributions':           contributions,
        }

        # Carry TradeJohn confirmer metadata if present.
        if 'tradejohn_decision' in o:
            order['tradejohn_decision'] = o['tradejohn_decision']

        payload['orders'].append(order)

    return payload


def main():
    ap = argparse.ArgumentParser(
        description='Phase 3 LIVE sizer — use regime_blended_sizer with real LLM confirmer',
    )
    ap.add_argument('--date', default=date.today().isoformat())
    args = ap.parse_args()
    run_date     = date.fromisoformat(args.date)
    run_date_str = run_date.isoformat()

    uri = os.environ.get('POSTGRES_URI', '')
    if not uri:
        print('[regime_blended_sizer_live] POSTGRES_URI not set; aborting', file=sys.stderr)
        return 2

    handoff = read_handoff(run_date_str, 'structured')
    if handoff is None:
        print(f'[regime_blended_sizer_live] no handoff for {run_date_str}; nothing to size')
        return 1

    signals = handoff.get('signals', []) if isinstance(handoff, dict) else []
    print(f'[regime_blended_sizer_live] loaded {len(signals)} signals from handoff for {run_date_str}')
    if not signals:
        print('[regime_blended_sizer_live] no signals in handoff; nothing to do')
        return 0

    # Field aliasing — handoff uses entry/stop/t1; consolidator+sizer uses
    # entry_price/stop_loss/take_profit_1.  Also convert direction to numeric.
    _DIR_MAP = {'LONG': 1, 'BUY': 1, 'BUY_VOL': 1,
                'SHORT': -1, 'SELL': -1, 'SELL_VOL': -1}
    for i, s in enumerate(signals):
        raw_dir = str(s.get('direction') or 'LONG').upper()
        if isinstance(s.get('direction'), str):
            s['direction'] = _DIR_MAP.get(raw_dir, 1)
        if 'entry' in s and 'entry_price' not in s:
            s['entry_price'] = s['entry']
        if 'stop' in s and 'stop_loss' not in s:
            s['stop_loss'] = s['stop']
        if 't1' in s and 'take_profit_1' not in s:
            s['take_profit_1'] = s['t1']
        if 'signal_id' not in s:
            s['signal_id'] = str(i)

    conn = psycopg2.connect(uri)
    cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    # Regime from handoff, fall back to DB.
    regime_state = None
    handoff_regime = handoff.get('regime') or {}
    if isinstance(handoff_regime, dict):
        regime_state = handoff_regime.get('state')
    if not regime_state:
        cur.execute("SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            print('[regime_blended_sizer_live] no market_regime; aborting')
            conn.close()
            return 2
        regime_state = row['state']

    regime = {'state': regime_state}
    print(f'[regime_blended_sizer_live] regime: {regime_state}')

    cur.execute("SELECT * FROM regime_sizer_params WHERE regime_state = %s", (regime_state,))
    params_row = cur.fetchone()
    params = dict(params_row) if params_row else {
        'liquidity_param': 1.0,
        'min_signal_notional_usd': 100,
        'position_circuit_breaker_pct': 0.02,
    }

    cur.execute("SELECT * FROM strategy_state")
    strategy_state = {r['strategy_id']: dict(r) for r in cur.fetchall()}

    # Inject target_pct_nav from latest strategy_sizing_recommendations.
    cur.execute("""
        SELECT DISTINCT ON (strategy_id) strategy_id, recommended_size_pct
          FROM strategy_sizing_recommendations
         ORDER BY strategy_id, rec_date DESC
    """)
    target_by = {r['strategy_id']: float(r['recommended_size_pct'])
                 for r in cur.fetchall() if r['recommended_size_pct'] is not None}
    for sig in signals:
        sig['target_pct_nav'] = target_by.get(sig.get('strategy_id'))

    conn.close()

    # Account snapshot — same path as parity wrapper.
    try:
        from execution.alpaca_trader import _fetch_account_state
        import requests
        account = _fetch_account_state(requests.Session())
    except Exception as e:
        print(f'[regime_blended_sizer_live] account fetch failed ({e}); using defaults',
              file=sys.stderr)
        account = {'equity': 100_000.0, 'regt_buying_power': 400_000.0,
                   'long_market_value': 0.0, 'cash': 100_000.0}

    equity = float(account.get('equity', 100_000.0))

    # REAL confirmer (LLM call) — this is the LIVE path.
    orders = size_positions(
        signals=signals,
        account_state=account,
        regime=regime,
        run_date=run_date,
        strategy_state=strategy_state,
        regime_params=params,
        confirmer=real_confirmer,
    )
    print(f'[regime_blended_sizer_live] size_positions produced {len(orders)} orders')

    if not orders:
        print('[regime_blended_sizer_live] no orders after sizing; nothing to submit')
        return 0

    payload = _build_sized_payload(orders, handoff, equity=equity)
    ok = _finalize_sized_payload(run_date_str, payload, source='regime_blended_sizer_live')
    print(f'[regime_blended_sizer_live] _finalize_sized_payload returned {ok}')
    return 0 if ok else 3


if __name__ == '__main__':
    sys.exit(main())
