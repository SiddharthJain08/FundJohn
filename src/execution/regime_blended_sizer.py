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
    mode = _select_mode(regime_state)

    # 1. Cadence gate (both modes).
    passed, skipped = filter_by_cadence(signals, strategy_state, run_date)
    if skipped:
        logger.info('regime_blended_sizer: cadence skipped %d signals', len(skipped))

    if not passed:
        return []

    # 2. Kelly enrichment — computes kelly_p from bracket geometry + p_t1.
    passed = enrich_with_kelly(passed)

    if mode == 'consolidate':
        orders = _consolidate_path(passed, account_state, regime_params, confirmer or default_confirmer)
    else:
        orders = _independent_path(passed, account_state, regime_params)

    return _apply_aggregate_cap(orders, account_state)

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
