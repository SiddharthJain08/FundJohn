#!/usr/bin/env python3
"""LIVE sizer — regime_blended_sizer producing real broker submissions.

Pipeline orchestrator's `trade` step calls this. Reads the structured
handoff, runs the sizer, and persists the sized handoff via
finalize_sized_payload — the path alpaca_executor reads from.
(News-veto now lives in the pre-market gate; the inline TradeJohn LLM
confirmer was retired 2026-07-20.)

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


def _is_option_order(o: dict) -> bool:
    """SP-5 Phase 1b (G3): the incoming order is option-class iff it carries either
    marker. Both _consolidate_option_orders and the G4b orphan-close set BOTH."""
    return o.get('instrument_class') == 'option' or bool(o.get('option_spec'))


def _resolve_option_markers(o: dict, *, close_only: bool):
    """SP-5 Phase 1b (G3) — fail-closed option-marker resolution.

    Returns (instrument_class, OptionSpec, normalized_direction|None, error_str|None).
    On error the order MUST be dropped (never routed) — an option order that fell
    through WITHOUT these markers would route the UNDERLYING as equity shares.

    Fail-closed when EITHER:
      • spec reconstruction fails (OptionSpec.from_dict → None: None/non-dict/no underlying), OR
      • the order is NOT close_only AND direction normalization returns None.
    A close_only order is EXEMPT from the direction check — a held-legs close is
    direction-free (it closes whatever legs are on the book).
    """
    from strategies.option_direction import normalize_option_direction
    from strategies.base import OptionSpec
    spec_in = o.get('option_spec')
    spec = OptionSpec.from_dict(spec_in) if isinstance(spec_in, dict) else spec_in
    if spec is None or not getattr(spec, 'underlying', None):
        return None, None, None, 'option_spec failed OptionSpec.from_dict (malformed/missing underlying)'
    nd = normalize_option_direction(o.get('direction'))
    if not close_only and nd is None:
        return None, None, None, f'direction {o.get("direction")!r} not normalizable (non-close order)'
    return 'option', spec, nd, None

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

import psycopg2, psycopg2.extras
from execution.regime_blended_sizer import size_positions, _derive_action
from execution.handoff import read_handoff
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
            #
            # SP-5 Phase 1b (G3 + G4b close-carry): option-class orders with NONE
            # brackets land here too — _consolidate_option_orders emits None brackets
            # for opens AND the G4b orphan-close emits a None-bracket close. This branch
            # is where the option_spec/instrument_class MUST be carried for the close to
            # route (the equity executor would otherwise route the underlying as shares).
            # Fail-closed: a malformed spec (or, for a non-close option order, an
            # unnormalizable direction) drops the order entirely. `target_usd == 0`
            # discriminates a true close (direction-exempt) from an open.
            if _is_option_order(o):
                _is_close = not _finite(o.get('target_usd')) or float(o.get('target_usd') or 0) == 0.0
                _ic_o, _spec_o, _nd_o, _err_o = _resolve_option_markers(o, close_only=_is_close)
                if _err_o is not None:
                    import logging as _logging
                    _logging.getLogger(__name__).error(
                        '[sp5-G3] DROPPING option order ticker=%s strategy_id=%s — %s',
                        o.get('ticker'), o.get('strategy_id'), _err_o)
                    continue
                if not _is_close:
                    # SP-5 Phase 1b follow-up (the pre-promotion gap): an option
                    # OPEN (_consolidate_option_orders emits None brackets by
                    # design, target_usd != 0) must NOT be close_only — emit an
                    # open order; the executor sizes qty from notional via
                    # _resolve_option_qty. Greeks-aware delta-dollar refinement
                    # (apply_instrument_class_sizing) is deferred to a real
                    # candidate's promotion — notional sizing is the proven path.
                    notional_op = abs(float(o.get('notional_usd') or 0))
                    pct_nav_op = round(notional_op / nav, 6)
                    sid_op = o.get('strategy_id') or '__option__'
                    order_op = {
                        'ticker':                  o['ticker'],
                        'strategy_id':             sid_op,
                        'direction':               _nd_o or dir_str,
                        'entry':                   None,
                        'stop':                    None,
                        't1':                      None,
                        't2':                      None,
                        'pct_nav':                 pct_nav_op,
                        'shares':                  0,
                        'notional_usd':            round(notional_op, 2),
                        'kelly_final':             pct_nav_op,
                        'ev':                      0.0,
                        'p_t1':                    0.5,
                        'source_mode':             o.get('source_mode'),
                        'contributing_strategies': o.get('contributing_strategies') or [sid_op],
                        'contributions':           o.get('contributions') or [
                            {'strategy_id': sid_op, 'attribution_weight': 1.0}],
                        'current_usd':             o.get('current_usd', 0.0),
                        'target_usd':              o.get('target_usd', 0.0),
                        'action':                  o.get('action') or 'open_long',
                        'instrument_class':        _ic_o,
                        'option_spec':             _spec_o,
                    }
                    if o.get('contracts') is not None:
                        order_op['contracts'] = o['contracts']
                    payload['orders'].append(order_op)
                    continue
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
            # G3/G4b: carry the reconstructed option markers (the guard above already
            # dropped malformed/unnormalizable orders). Equity close orders never enter
            # this block → no option keys injected → byte-identical.
            if _is_option_order(o):
                order_oc['instrument_class'] = _ic_o
                order_oc['option_spec'] = _spec_o
                if _nd_o:
                    order_oc['direction'] = _nd_o
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


        # SP-5.1c/5-Phase-1b (G3): option orders carry spec (reconstructed to OptionSpec)
        # + normalized direction + contracts so alpaca_executor._route_option_order can
        # route the structure. This is the FINITE-bracket path → the order is an open, so
        # direction normalization is REQUIRED (close_only=False). Fail-closed: a malformed
        # spec OR an unnormalizable direction DROPS the order entirely (a `continue` guard)
        # rather than letting it fall through WITHOUT markers → routed as equity shares.
        # Guarded on the option markers → equity orders are byte-identical (untouched).
        if _is_option_order(o):
            _ic_f, _spec_f, _nd_f, _err_f = _resolve_option_markers(o, close_only=False)
            if _err_f is not None:
                import logging as _logging
                _logging.getLogger(__name__).error(
                    '[sp5-G3] DROPPING option order ticker=%s strategy_id=%s — %s',
                    o.get('ticker'), o.get('strategy_id'), _err_f)
                continue
            order['instrument_class'] = _ic_f
            order['option_spec'] = _spec_f
            if _nd_f:
                order['direction'] = _nd_f
            if o.get('contracts') is not None:
                order['contracts'] = o['contracts']

        payload['orders'].append(order)

    return payload


def _collapse_contributing(orders) -> dict:
    """Collapse sized orders → {ticker: sorted unique contributing strategies}.
    Drops synthetic close pseudo-strategies (e.g. __close_option_expiry__) and
    orders with no ticker / no real strategies. Falls back to [strategy_id] when
    contributing_strategies is absent. Pure — the DB upsert is separate."""
    by_ticker: dict = {}
    for o in orders:
        tk = o.get('ticker')
        strats = (o.get('contributing_strategies')
                  or ([o.get('strategy_id')] if o.get('strategy_id') else []))
        strats = [s for s in strats if s and not str(s).startswith('__')]
        if not tk or not strats:
            continue
        by_ticker.setdefault(tk, set()).update(strats)
    return {tk: sorted(s) for tk, s in by_ticker.items()}


def _collapse_contributions(orders) -> dict:
    """Collapse sized orders → {ticker: [{strategy_id, contribution, direction}]}.
    Keeps only orders that carry a non-empty `contributions` list (sizing
    emissions; orphan/flip closes have none). Pure — DB upsert is separate."""
    by_ticker: dict = {}
    for o in orders:
        tk = o.get('ticker')
        contribs = o.get('contributions') or []
        if not tk or not contribs:
            continue
        by_ticker[tk] = contribs
    return by_ticker


def _persist_contributing_strategies(run_date_str, orders) -> int:
    """Upsert this cycle's per-ticker corr-gate contributing strategies into
    cycle_contributing_strategies (read by the dashboard ticker-alpha). Best-
    effort: never fails the cycle. Returns rows upserted."""
    by_ticker = _collapse_contributing(orders)
    contribs_by_ticker = _collapse_contributions(orders)
    if not by_ticker:
        return 0
    # Per-ticker signed S_adj stamped by the sizer (2026-07-14) — surfaced on
    # the dashboard position tiles. Closes of out-of-target tickers carry None.
    sadj_by_ticker = {}
    for o in orders:
        tk = o.get('ticker')
        v = o.get('corr_cum_sharpe')
        if tk and v is not None:
            sadj_by_ticker[tk] = v
    uri = os.environ.get('POSTGRES_URI')
    if not uri:
        return 0
    n = 0
    try:
        conn = psycopg2.connect(uri)
        cur = conn.cursor()
        for tk, strats in by_ticker.items():
            contribs = contribs_by_ticker.get(tk)
            cur.execute(
                """
                INSERT INTO cycle_contributing_strategies
                    (run_date, ticker, strategies, contributions,
                     corr_cum_sharpe, updated_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (run_date, ticker) DO UPDATE SET
                    strategies = EXCLUDED.strategies,
                    contributions = EXCLUDED.contributions,
                    corr_cum_sharpe = COALESCE(EXCLUDED.corr_cum_sharpe,
                                               cycle_contributing_strategies.corr_cum_sharpe),
                    updated_at = NOW()
                """,
                (run_date_str, tk, list(strats),
                 json.dumps(contribs) if contribs is not None else None,
                 sadj_by_ticker.get(tk)),
            )
            n += 1
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f'[regime_blended_sizer_live] persist contributing_strategies '
              f'failed (non-fatal): {e}')
    return n


def _account_state_violations(account, params=None):
    """Pre-trade account-state assertion (fix 6, 2026-07-27). Returns a list of
    violation strings — empty means sane. The 2026-07-23 incident: paper account
    multiplier silently flipped 4→1 with shorting disabled; the sizer planned a
    long/short book against a long-only account and the short leg vanished with
    no alert. Checks (each tunable via pipeline_config, fail-open defaults):
      - multiplier >= min_account_multiplier   (default 2 — paper=4, live RegT=2)
      - shorting_enabled                        (require_shorting_enabled=1 default)
      - trading_blocked / account_blocked false, status ACTIVE when reported
    `params` injectable for tests; production reads pipeline_config."""
    if params is None:
        params = {}
        try:
            import psycopg2
            with psycopg2.connect(os.environ['POSTGRES_URI']) as c, c.cursor() as cur:
                cur.execute("SELECT key, value FROM pipeline_config WHERE key IN "
                            "('min_account_multiplier', 'require_shorting_enabled')")
                params = {k: v for k, v in cur.fetchall()}
        except Exception:
            params = {}
    try:
        min_mult = float(params.get('min_account_multiplier', 2.0))
    except (TypeError, ValueError):
        min_mult = 2.0
    require_short = str(params.get('require_shorting_enabled', '1')) != '0'
    v = []
    mult = account.get('multiplier')
    if mult is not None and float(mult) < min_mult:
        v.append(f'multiplier {mult} < required {min_mult} (degraded margin — '
                 f'planned book will not fit)')
    if require_short and account.get('shorting_enabled') is False:
        v.append('shorting_enabled=false (short leg would silently vanish)')
    if account.get('trading_blocked') is True:
        v.append('trading_blocked=true')
    if account.get('account_blocked') is True:
        v.append('account_blocked=true')
    status = (account.get('status') or '').upper()
    if status and status != 'ACTIVE':
        v.append(f'account status {status} != ACTIVE')
    return v


def _resolve_account_or_none(session_fn=None, fetch_fn=None):
    """Fetch the Alpaca account dict, returning None on any failure.

    Accepts injectable session_fn / fetch_fn for testing.  In production
    both default to the real _alpaca_session / _fetch_account_state.

    Returns:
        dict  — account state on success
        None  — on any exception (caller MUST abort, never fabricate equity)
    """
    try:
        from execution.alpaca_trader import _alpaca_session, _fetch_account_state
        _sfn = session_fn if session_fn is not None else _alpaca_session
        _ffn = fetch_fn if fetch_fn is not None else _fetch_account_state
        return _ffn(_sfn())
    except Exception as e:
        print(f'[regime_blended_sizer_live] account fetch failed ({e})', file=sys.stderr)
        return None


def main():
    ap = argparse.ArgumentParser(
        description='Phase 3 LIVE sizer — use regime_blended_sizer',
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

    # EOD signal-register lane, NOT "the reconcile job is enabled" (2026-07-29):
    # the same-day lane keeps the premarket reconcile on for protective closes
    # while producing signals via the 15:00 structured handoff. Keying off
    # OPENCLAW_EOD_RECONCILE here would send the same-day chain down the
    # self-load branch and size an EMPTY carried set against a live book.
    eod_mode = os.environ.get('OPENCLAW_EOD_SIGNAL_REGISTER') == '1'
    if eod_mode:
        # SP-6 Phase A — EOD reconcile mode. eod-signal-register persists the
        # carried set to execution_signals (APPROVED, target_date=T+1); it does
        # NOT write a structured handoff. size_positions self-loads that APPROVED
        # set when OPENCLAW_EOD_RECONCILE=1 (regime_blended_sizer._sharpe_cadence_path
        # -> _load_approved_carried_signals) and ignores the passed signals, so we
        # pass an empty list + a minimal handoff. Regime is resolved from
        # market_regime below (handoff regime is empty), then backfilled into the
        # handoff so the persisted payload records the real regime.
        handoff = {'cycle_date': run_date_str, 'regime': {}, 'signals': []}
        signals = []
        print(f'[regime_blended_sizer_live] EOD mode — size_positions self-loads the '
              f'APPROVED carried set for {run_date_str}')
    else:
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

    if eod_mode:
        # The synthetic EOD handoff carried regime={}; backfill the resolved
        # regime so the persisted sized payload (and the trade report's
        # sized.get('regime')) record the real state rather than '?'.
        handoff['regime'] = regime

    cur.execute("SELECT * FROM regime_sizer_params WHERE regime_state = %s", (regime_state,))
    params_row = cur.fetchone()
    params = dict(params_row) if params_row else {
        'liquidity_param': 1.0,
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
    # W3 F1: on fetch failure return None so we abort (zero orders) rather
    # than sizing against fabricated $100k equity (over-leverage risk).
    account = _resolve_account_or_none()
    if account is None or account.get('fetched') is False:
        msg = ('[regime_blended_sizer_live] ABORT: account fetch failed — '
               'emitting ZERO orders (no sizing against fabricated equity)')
        print(msg, file=sys.stderr)
        try:
            from execution.pipeline_orchestrator import post_channel
            post_channel(
                os.environ.get('OPENCLAW_TRADE_ALERT_WEBHOOK_NAME', 'trade-reports'),
                '\U0001f6d1 ' + msg,
            )
        except Exception as _e:
            print(f'  (alert post failed: {_e})', file=sys.stderr)
        return

    # Pre-trade account-state assertion (fix 6): a degraded account (multiplier
    # flip, shorting revoked, blocked) must HALT new sizing loudly — never plan
    # a book the account cannot hold. Resting GTC protective exits at the broker
    # are untouched by this halt; only new order emission stops.
    _violations = _account_state_violations(account)
    if _violations:
        msg = ('[regime_blended_sizer_live] HALT: account-state assertion failed — '
               + '; '.join(_violations) + ' — emitting ZERO orders (protective '
               'exits at broker unaffected). Operator: verify the account, then '
               're-run the trade step.')
        print(msg, file=sys.stderr)
        try:
            from execution.pipeline_orchestrator import post_channel
            post_channel(
                os.environ.get('OPENCLAW_TRADE_ALERT_WEBHOOK_NAME', 'trade-reports'),
                '\U0001f6d1 ' + msg,
            )
        except Exception as _e:
            print(f'  (alert post failed: {_e})', file=sys.stderr)
        return 0

    equity = float(account.get('equity', 100_000.0))

    # Per-ticker news-veto is owned by the pre-market news gate (premarket_gate.py),
    # not the sizer — the inline TradeJohn LLM confirmer was retired 2026-07-20.
    orders = size_positions(
        signals=signals,
        account_state=account,
        regime=regime,
        run_date=run_date,
        strategy_state=strategy_state,
        regime_params=params,
    )
    print(f'[regime_blended_sizer_live] size_positions produced {len(orders)} orders')

    if not orders:
        print('[regime_blended_sizer_live] no orders after sizing; nothing to submit')
        return 0

    # Persist the corr-gate contributing strategies per ticker so the dashboard
    # ticker-alpha shows only contributing strategies (best-effort, non-fatal).
    _ncs = _persist_contributing_strategies(run_date_str, orders)
    if _ncs:
        print(f'[regime_blended_sizer_live] persisted contributing strategies for {_ncs} ticker(s)')

    payload = _build_sized_payload(orders, handoff, equity=equity)
    ok = finalize_sized_payload(run_date_str, payload, source='regime_blended_sizer_live')
    print(f'[regime_blended_sizer_live] finalize_sized_payload returned {ok}')
    return 0 if ok else 3


if __name__ == '__main__':
    sys.exit(main())
