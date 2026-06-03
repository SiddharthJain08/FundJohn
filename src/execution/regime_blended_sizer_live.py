#!/usr/bin/env python3
"""LIVE sizer — regime_blended_sizer producing real broker submissions.

Pipeline orchestrator's `trade` step calls this. Reads the structured
handoff, runs the sizer with the real TradeJohn confirmer, and persists
the sized handoff via finalize_sized_payload — the path alpaca_executor
reads from.

Output format: payload['orders'] shape:
  {ticker, strategy_id, direction, entry, stop, t1, t2, pct_nav, shares,
   notional_usd, kelly_final, ev, p_t1, source_mode, contributing_strategies}

alpaca_executor reads payload['orders']; strategy_id drives the
already_executed() idempotency check.
"""
import argparse, json, math, os, sys
from datetime import date
from pathlib import Path


def _finite(x) -> bool:
    if x is None:
        return False
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2, psycopg2.extras
from execution.regime_blended_sizer import size_positions, _derive_action
from execution.handoff import read_handoff
from execution.tradejohn_confirmer import confirm as real_confirmer
from execution.sized_handoff import finalize_sized_payload
from strategies.instrument_class import instrument_class_for
from execution.instrument_class_sizer import apply_instrument_class_sizing


def _build_sized_payload(orders: list[dict], handoff: dict,
                         equity: float = 100_000.0) -> dict:
    """Convert size_positions() output into the sized-handoff format
    that finalize_sized_payload + alpaca_executor expect.

    Key mapping from regime_blended_sizer output → alpaca_executor input:
      ticker                             → ticker
      direction (int +1/-1)              → direction (lowercase 'long'/'short')
      bracket.entry_price                → entry
      bracket.stop_loss                  → stop
      bracket.take_profit_1              → t1
      qty (signed float)                 → shares (int, absolute value)
      abs(notional_usd) / equity         → pct_nav
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
        # Direction: legacy consolidate/independent paths emit int (+1/-1);
        # sharpe_cadence path emits 'long'/'short' string.
        raw_dir = o.get('direction')
        if isinstance(raw_dir, (int, float)):
            dir_str = 'long' if raw_dir > 0 else 'short'
        else:
            dir_str = str(raw_dir or 'long').lower()

        # Bracket: legacy paths nest under o['bracket']; sharpe_cadence
        # exposes entry/stop/t1/t2 at the top level.
        bracket = o.get('bracket') or {}
        entry_raw = bracket.get('entry_price') if bracket else o.get('entry')
        stop_raw  = bracket.get('stop_loss')   if bracket else o.get('stop')
        t1_raw    = bracket.get('take_profit_1') if bracket else o.get('t1')
        t2_raw    = bracket.get('take_profit_2') if bracket else o.get('t2')
        if not (_finite(entry_raw) and _finite(stop_raw) and _finite(t1_raw)):
            # No usable bracket. Two cases both handled the same way:
            #  1. Orphan-close: position held but no longer signalled
            #     (strategy_id = '__close_orphan__')
            #  2. Position-reduce: delta is opposite to all contributing
            #     signal directions (e.g., LONG signals but delta is negative
            #     because target < current) — no SHORT bracket available
            # Both need close_only=True so the executor uses `position close`
            # (RTH) or a simple limit order (ext-hours) against the snapshot.
            notional_oc = abs(float(o.get('notional_usd') or o.get('current_usd') or 0))
            pct_nav_oc  = round(notional_oc / nav, 6)
            sid_oc = o.get('strategy_id') or '__close_orphan__'
            order_oc = {
                'ticker':                  o['ticker'],
                'strategy_id':             sid_oc,
                'direction':               dir_str,
                'entry':                   None,
                'stop':                    None,
                't1':                      None,
                't2':                      None,
                'pct_nav':                 pct_nav_oc,
                'shares':                  0,
                'notional_usd':            round(notional_oc, 2),
                'kelly_final':             pct_nav_oc,
                'ev':                      0.0,
                'p_t1':                    0.5,
                'source_mode':             o.get('source_mode'),
                'close_only':              True,
                'contributing_strategies': o.get('contributing_strategies') or [sid_oc],
                'contributions':           o.get('contributions') or [
                    {'strategy_id': sid_oc, 'attribution_weight': 1.0}],
                'current_usd':             o.get('current_usd', 0.0),
                'target_usd':              o.get('target_usd', 0.0),
                'action':                  o.get('action') or _derive_action(
                    'orphan_close' if not o.get('target_usd') else 'delta',
                    o.get('current_usd', 0.0), o.get('target_usd', 0.0),
                    -1 if str(dir_str).lower() == 'short' else 1),
            }
            payload['orders'].append(order_oc)
            continue

        entry      = float(entry_raw)
        notional   = abs(float(o['notional_usd']))
        # SP-3: route by instrument_class. Default-OFF kill-switch forces the
        # equity path (byte-identical) until soak. No walrus — read into a local
        # so we don't shadow the `contributions` re-bound below.
        if os.environ.get('OPENCLAW_INSTRUMENT_CLASS_ROUTING') == '1':
            _contribs = o.get('contributions') or []
            _sid_for_class = (_contribs[0].get('strategy_id')
                              if _contribs else o.get('strategy_id'))
            if _sid_for_class:
                _ic = instrument_class_for(_sid_for_class)
                if _ic not in ('equity', 'etp'):
                    o = apply_instrument_class_sizing(o, _ic)
                    notional = abs(float(o['notional_usd']))
        pct_nav    = round(notional / nav, 6)
        # Shares: legacy paths put a signed float in `qty`; sharpe_cadence
        # carries `shares=0` (compute from notional/entry here).
        if 'qty' in o and o['qty'] is not None:
            shares_raw = abs(float(o['qty']))
        else:
            shares_raw = (notional / entry) if entry > 0 else 0
        shares = max(1, int(shares_raw)) if shares_raw >= 0.5 else 0

        # strategy_id: for consolidate-mode orders, contributions may have
        # multiple strategies. Use the first contributing strategy_id so
        # alpaca_executor's already_executed() check works correctly.
        # Downstream attribution uses the full contributing_strategies list.
        contributions = o.get('contributions') or []
        if contributions:
            strategy_id = contributions[0].get('strategy_id') or 'unknown'
        else:
            # sharpe_cadence path carries strategy_id at top level (joined IDs)
            strategy_id = o.get('strategy_id') or 'unknown'

        order = {
            'ticker':                  o['ticker'],
            'strategy_id':             strategy_id,
            'direction':               dir_str,
            'entry':                   entry,
            'stop':                    float(stop_raw),
            't1':                      float(t1_raw),
            't2':                      float(t2_raw) if t2_raw is not None else None,
            'pct_nav':                 pct_nav,
            'shares':                  shares,
            'notional_usd':            round(float(o['notional_usd']), 2),
            'kelly_final':             pct_nav,  # best proxy; not a true Kelly calculation
            'ev':                      o.get('ev'),
            'p_t1':                    o.get('p_t1'),
            'source_mode':             o.get('source_mode'),
            'contributing_strategies': (o.get('contributing_strategies')
                                        or [c.get('strategy_id') for c in contributions]),
            'contributions':           contributions,
        }

        # Carry TradeJohn confirmer metadata if present.
        if 'tradejohn_decision' in o:
            order['tradejohn_decision'] = o['tradejohn_decision']

        # SP-5.1c: option orders carry spec (reconstructed to OptionSpec) +
        # normalized direction + contracts so alpaca_executor._route_option_order
        # can route the structure. Guarded on instrument_class=='option' ->
        # equity orders are byte-identical (zero new keys injected).
        if o.get('instrument_class') == 'option' and o.get('option_spec'):
            from strategies.option_direction import normalize_option_direction
            from strategies.base import OptionSpec
            _spec_in = o['option_spec']
            _spec = OptionSpec.from_dict(_spec_in) if isinstance(_spec_in, dict) else _spec_in
            if _spec is not None:
                order['instrument_class'] = 'option'
                order['option_spec'] = _spec
                _nd = normalize_option_direction(o.get('direction'))
                if _nd:
                    order['direction'] = _nd
                if o.get('contracts') is not None:
                    order['contracts'] = o['contracts']

        payload['orders'].append(order)

    return payload


def main():
    ap = argparse.ArgumentParser(
        description='Phase 3 LIVE sizer — use regime_blended_sizer with real LLM confirmer',
    )
    ap.add_argument('--date', default=date.today().isoformat())
    ap.add_argument('--dry-run', action='store_true',
                    help='Skip sizing work and exit 0 — for PIPELINE_DRY_RUN=1 cycles')
    args = ap.parse_args()
    if args.dry_run:
        print(f'[regime_blended_sizer_live] dry-run skip for {args.date}')
        return 0
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

    # Account snapshot — same path as parity wrapper. _alpaca_session()
    # attaches auth headers + sess._base required by _fetch_account_state;
    # a bare requests.Session() yields 'Session has no attribute _base'.
    try:
        from execution.alpaca_trader import _alpaca_session, _fetch_account_state
        account = _fetch_account_state(_alpaca_session())
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
    ok = finalize_sized_payload(run_date_str, payload, source='regime_blended_sizer_live')
    print(f'[regime_blended_sizer_live] finalize_sized_payload returned {ok}')
    return 0 if ok else 3


if __name__ == '__main__':
    sys.exit(main())
