"""Regime-blended sizer: orchestrator + mode dispatcher.

Pipeline orchestrator's `trade` step calls this. Replaces deterministic_sizer
as the primary sizer (which is kept for parity DRY-RUN per Task 16).

Mode dispatch (binary, by regime state):
  LOW_VOL, TRANSITIONING → consolidate path (formula + ticker_consolidator + tradejohn_confirmer)
  HIGH_VOL, CRISIS       → independent path (mechanical: target_pct_nav × NAV × λ / entry)
"""
from __future__ import annotations
import logging
from datetime import date

from execution._kelly import enrich_with_kelly
from execution.signal_cadence_gate import filter_by_cadence, advance_last_fire
from execution.ticker_consolidator import consolidate
from execution.tradejohn_confirmer import confirm as default_confirmer

logger = logging.getLogger(__name__)

MAX_DAILY_NEW_NOTIONAL_PCT = 0.25  # Aggregate cap: total new notional ≤ 25% of available NAV
AVAILABLE_NAV_FLOOR_PCT = 0.05     # Floor: even when fully invested, allocate ≥5% NAV

CONSOLIDATE_REGIMES = ('LOW_VOL', 'TRANSITIONING')
INDEPENDENT_REGIMES = ('HIGH_VOL', 'CRISIS')
HIGH_VOL_FALLBACK_TARGET_PCT = 0.01  # 1% NAV when strategy missing from sizing recs

def _apply_aggregate_cap(orders: list[dict], account_state: dict) -> list[dict]:
    """Cap total notional at MAX_DAILY_NEW_NOTIONAL_PCT × available_nav.

    available_nav = max(NAV × 5%, NAV - long_market_value).
    If sum(order.notional) ≤ cap, return orders unchanged.
    Otherwise scale every order's qty + notional by (cap / total_notional).
    Mutates each order dict in place AND returns the (same) list.
    """
    if not orders:
        return orders
    nav = float(account_state.get('equity', 0))
    if nav <= 0:
        return orders
    long_mv = float(account_state.get('long_market_value', 0))
    available_nav = max(nav * AVAILABLE_NAV_FLOOR_PCT, nav - long_mv)
    cap = MAX_DAILY_NEW_NOTIONAL_PCT * available_nav

    total = sum(abs(o['notional_usd']) for o in orders)
    if total <= cap or cap <= 0:
        return orders

    scale = cap / total
    logger.info('regime_blended_sizer: aggregate cap binding — scaling %d orders by %.4f '
                '(total=$%.0f → cap=$%.0f, NAV=$%.0f, long_mv=$%.0f, available_nav=$%.0f)',
                len(orders), scale, total, cap, nav, long_mv, available_nav)
    for o in orders:
        o['notional_usd'] = o['notional_usd'] * scale
        o['qty'] = o['qty'] * scale
        o['cap_scale_applied'] = scale
    return orders


def _select_mode(regime_state: str) -> str:
    if regime_state in CONSOLIDATE_REGIMES:
        return 'consolidate'
    if regime_state in INDEPENDENT_REGIMES:
        return 'independent'
    logger.warning('regime_blended_sizer: unknown regime %r; defaulting to independent (safest)', regime_state)
    return 'independent'

def size_positions(
    signals: list[dict],
    account_state: dict,
    regime: dict,
    run_date: date,
    strategy_state: dict,
    regime_params: dict,
    confirmer=None,
) -> list[dict]:
    """Returns list of {ticker, direction, qty, notional_usd, bracket, contributions, source_mode}.

    Caller is responsible for writing orders to execution_signals,
    consolidation_contributions to its table, and advancing strategy_state in DB.
    """
    regime_state = regime['state']

    # 1. Cadence gate (every path). When OPENCLAW_SHARPE_CADENCE_SIZER=1
    # and the regime_liquidator just set regime:transition:fresh, the gate
    # is bypassed for one cycle so every eligible strategy fires fresh.
    force_all = _check_force_fire_flag() if os.environ.get('OPENCLAW_SHARPE_CADENCE_SIZER') == '1' else False
    passed, skipped = filter_by_cadence(signals, strategy_state, run_date, force_all=force_all)
    if skipped:
        logger.info('regime_blended_sizer: cadence skipped %d signals', len(skipped))
    if force_all:
        logger.info('regime_blended_sizer: force_all=True — cadence bypassed (regime transition)')

    if not passed:
        return []

    # 2. Kelly enrichment — computes kelly_p from bracket geometry + p_t1.
    passed = enrich_with_kelly(passed)

    # 3. Path selection. New (flag-gated) path replaces the regime-routed
    # consolidate/independent split: it uses the Sharpe×cadence weights
    # engine from src/execution/strategy_weights.py for ALL regimes.
    if os.environ.get('OPENCLAW_SHARPE_CADENCE_SIZER') == '1':
        orders = _sharpe_cadence_path(passed, account_state, regime_state,
                                      regime_params, confirmer or default_confirmer)
    else:
        mode = _select_mode(regime_state)
        if mode == 'consolidate':
            orders = _consolidate_path(passed, account_state, regime_params, confirmer or default_confirmer)
        else:
            orders = _independent_path(passed, account_state, regime_params)

    return _apply_aggregate_cap(orders, account_state)


def _check_force_fire_flag() -> bool:
    """Consume the regime:transition:fresh Redis key. Returns True once
    per regime transition; subsequent calls return False until the next
    liquidation re-sets it."""
    try:
        import redis as _redis
        r = _redis.from_url(os.environ.get('REDIS_URL', 'redis://localhost:6379/0'),
                            socket_connect_timeout=3, decode_responses=True)
        if r.get('regime:transition:fresh'):
            r.delete('regime:transition:fresh')
            return True
    except Exception:
        pass
    return False


def _dir_to_int(d) -> int:
    """Normalize a signal direction to +1 (long-side) or -1 (short-side).
    Handles all six raw values + integer / float / 'long' / 'short' forms.
    Returns 0 if unparseable — such signals are excluded from sizing.
    """
    if d is None:
        return 0
    if isinstance(d, (int, float)):
        return 1 if d > 0 else (-1 if d < 0 else 0)
    u = str(d).strip().upper()
    if u in ('LONG', 'BUY', 'BUY_VOL', '1', '+1'):
        return 1
    if u in ('SHORT', 'SELL', 'SELL_VOL', '-1'):
        return -1
    return 0


def _load_lambda(default: float = 2.0) -> float:
    """Read position_sizing_lambda from pipeline_config; fall back to default
    on any error. Bounded clamp guards against operator-pasted garbage."""
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = 'position_sizing_lambda'")
                row = cur.fetchone()
                v = float(row[0]) if row else default
                return max(0.10, min(3.50, v))
    except Exception:
        return default


def _sharpe_cadence_path(signals, account_state, regime_state, params, confirmer):
    """Sharpe × cadence × direction sizer.

    1. Look up per-(strategy, regime) daily_weight from
       strategy_weights_by_regime via execution.strategy_weights.load_current.
    2. For each ticker, sum daily_weight × direction across signalling
       strategies. ticker_weight is signed.
    3. Normalize so Σ |position_usd| = λ × NAV exactly.
    4. Apply per-ticker 25% NAV cap + $25 minimum trade threshold;
       redistribute excess proportionally across surviving tickers.
    5. Pass surviving tickers through TradeJohn (keep/cancel only).
    6. Emit orders in the deterministic_sizer / trade_agent_llm payload
       shape: list of {ticker, strategy_id, direction, notional_usd,
       pct_nav, shares (filled downstream), entry/stop/t1/t2 (filled
       downstream), kelly_final, ev, p_t1, source_mode='sharpe_cadence',
       contributing_strategies}.
    """
    from execution import strategy_weights as _sw

    nav = float(account_state.get('equity') or 100_000.0)
    rows = _sw.load_current(regime_state)
    if not rows:
        logger.info('regime_blended_sizer.sharpe_cadence: no current weights for %s', regime_state)
        return []
    weight_by_strat = {r['strategy_id']: float(r['daily_weight']) for r in rows}
    lam = _load_lambda()

    # Aggregate ticker_weight across signalling strategies. A strategy not
    # in weight_by_strat is excluded (sharpe ≤ 0 in this regime, or not
    # active). Its signals are dropped.
    from collections import defaultdict
    ticker_w = defaultdict(float)
    ticker_meta = defaultdict(lambda: {'strategies': [], 'directions': []})
    for s in signals:
        sid = s.get('strategy_id')
        tkr = s.get('ticker')
        if not sid or not tkr or sid not in weight_by_strat:
            continue
        d = _dir_to_int(s.get('direction'))
        if d == 0:
            continue
        ticker_w[tkr] += weight_by_strat[sid] * d
        ticker_meta[tkr]['strategies'].append(sid)
        ticker_meta[tkr]['directions'].append(d)

    if not ticker_w:
        logger.info('regime_blended_sizer.sharpe_cadence: no eligible signals after weight filter')
        return []

    gross = sum(abs(w) for w in ticker_w.values())
    if gross <= 0:
        return []
    scale = (lam * nav) / gross

    PER_TICKER_CAP = 0.25 * nav
    MIN_TRADE_USD  = 25.0
    final_usd: dict[str, float] = {}
    # Track excess by SIDE so long-cap-excess stays on the long side
    # (and same for shorts). Mixing the two would flip a short-side
    # cap into an additional long allocation — bug we hit in v1.
    long_excess  = 0.0
    short_excess = 0.0  # held as a positive magnitude
    for tkr, w in ticker_w.items():
        usd = w * scale
        if abs(usd) < MIN_TRADE_USD:
            # Sub-threshold tickers donate their (signed) notional to
            # their side's redistribution pool.
            if usd > 0: long_excess  += usd
            else:        short_excess += -usd
            continue
        if abs(usd) > PER_TICKER_CAP:
            sign = 1 if usd > 0 else -1
            excess = abs(usd) - PER_TICKER_CAP
            if sign > 0: long_excess  += excess
            else:        short_excess += excess
            usd = sign * PER_TICKER_CAP
        final_usd[tkr] = usd

    # Bucket-aware redistribution. Long-excess is spread proportionally
    # across long survivors (those with usd > 0) by |usd|; same for
    # shorts. One pass — newly-capped tickers stay capped.
    def _redistribute_side(pool: float, side: int):
        if pool <= 0.01:
            return
        survivors = [t for t, v in final_usd.items() if (v > 0) == (side > 0)]
        total_abs = sum(abs(final_usd[t]) for t in survivors)
        if total_abs <= 0:
            return
        for t in survivors:
            share = pool * (abs(final_usd[t]) / total_abs)
            final_usd[t] += side * share
            if abs(final_usd[t]) > PER_TICKER_CAP:
                final_usd[t] = side * PER_TICKER_CAP
    _redistribute_side(long_excess,  +1)
    _redistribute_side(short_excess, -1)

    # TradeJohn keep/cancel (news veto only — never adjusts size).
    proposals = [{
        'ticker': tkr,
        'preliminary_size_usd': usd,
        'direction': 1 if usd >= 0 else -1,
        'contributions': [{'strategy_id': sid, 'attribution_weight': 1.0 / len(ticker_meta[tkr]['strategies'])}
                          for sid in ticker_meta[tkr]['strategies']],
        'bracket': {'entry_price': None, 'stop_loss': None, 'take_profit_1': None},
        'context': {'news_headlines': [], '30d_veto_history_for_ticker': 0,
                    'sector': '', 'hv30d': None},
    } for tkr, usd in final_usd.items()]
    actions = {}
    if confirmer:
        try:
            actions = confirmer(proposals) or {}
        except Exception as e:
            logger.warning('regime_blended_sizer.sharpe_cadence: confirmer failed (%s); keeping all', e)
    for tkr in list(final_usd.keys()):
        a = (actions.get(tkr) or {}).get('action', 'keep')
        if a == 'cancel':
            final_usd.pop(tkr)

    # Emit orders. entry/stop/t1/t2 are filled downstream when the executor
    # fetches the current quote; we only commit notional + direction here.
    orders = []
    for tkr, usd in final_usd.items():
        orders.append({
            'ticker':                  tkr,
            'strategy_id':             '|'.join(sorted(set(ticker_meta[tkr]['strategies'])))[:120],
            'direction':               'long' if usd >= 0 else 'short',
            'notional_usd':            abs(usd),
            'pct_nav':                 abs(usd) / nav,
            'shares':                  0,
            'entry':                   None,
            'stop':                    None,
            't1':                      None,
            't2':                      None,
            'kelly_final':             abs(usd) / nav,
            'ev':                      0.0,
            'p_t1':                    0.5,
            'source_mode':             'sharpe_cadence',
            'contributing_strategies': ticker_meta[tkr]['strategies'],
        })
    return orders

def _consolidate_path(signals, account_state, params, confirmer):
    regt_bp = float(account_state['regt_buying_power'])
    proposals = consolidate(signals, regt_buying_power=regt_bp, params=params)
    if not proposals:
        return []

    for p in proposals:
        p.setdefault('context', {'news_headlines': [], '30d_veto_history_for_ticker': 0,
                                  'sector': None, 'hv30d': None})

    decisions = confirmer(proposals)

    orders = []
    for p in proposals:
        d = decisions.get(p['ticker'], {'action': 'approve', 'multiplier': 1.0})
        multiplier = float(d.get('multiplier', 1.0))
        if d.get('action') == 'veto' or multiplier == 0:
            continue
        notional = p['preliminary_size_usd'] * multiplier
        entry = p['bracket']['entry_price']
        qty = (notional / entry) if entry > 0 else 0
        orders.append({
            'ticker': p['ticker'],
            'direction': p['direction'],
            'qty': qty,
            'notional_usd': notional,
            'bracket': p['bracket'],
            'contributions': p['contributions'],
            'source_mode': 'consolidate',
            'tradejohn_decision': d,
        })
    return orders

def _independent_path(signals, account_state, params):
    nav = float(account_state['equity'])
    lambda_regime = params['liquidity_param']
    orders = []
    for sig in signals:
        target_pct = sig.get('target_pct_nav')
        if target_pct is None:
            logger.warning('regime_blended_sizer: missing_strategy_sizing for %s; falling back 1%% NAV',
                           sig['strategy_id'])
            target_pct = HIGH_VOL_FALLBACK_TARGET_PCT
        if target_pct <= 0:
            logger.info('regime_blended_sizer: opus_sized_to_zero %s; skipping', sig['strategy_id'])
            continue

        notional = target_pct * nav * lambda_regime
        entry = sig['entry_price']
        qty = (notional / entry) if entry > 0 else 0
        orders.append({
            'ticker': sig['ticker'],
            'direction': sig['direction'],
            'qty': qty,
            'notional_usd': notional,
            'bracket': {'entry_price': entry, 'stop_loss': sig['stop_loss'],
                        'take_profit_1': sig['take_profit_1']},
            'contributions': [{'contributing_signal_id': sig.get('signal_id'),
                                'strategy_id': sig['strategy_id'],
                                'signal_position_size_usd': notional,
                                'attribution_weight': 1.0,
                                'contributed_direction': sig['direction']}],
            'source_mode': 'independent',
        })
    return orders
