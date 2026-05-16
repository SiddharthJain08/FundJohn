"""Regime-blended sizer: orchestrator + mode dispatcher.

Pipeline orchestrator's `trade` step calls this. Replaces deterministic_sizer
as the primary sizer (which is kept for parity DRY-RUN per Task 16).

Mode dispatch (binary, by regime state):
  LOW_VOL, TRANSITIONING → consolidate path (formula + ticker_consolidator + tradejohn_confirmer)
  HIGH_VOL, CRISIS       → independent path (mechanical: target_pct_nav × NAV × λ / entry)
"""
from __future__ import annotations
import logging
import os
from datetime import date

from execution._kelly import enrich_with_kelly
from execution.signal_cadence_gate import filter_by_cadence, advance_last_fire
from execution.ticker_consolidator import consolidate
from execution.tradejohn_confirmer import confirm as default_confirmer

logger = logging.getLogger(__name__)

CONSOLIDATE_REGIMES = ('LOW_VOL', 'TRANSITIONING')
INDEPENDENT_REGIMES = ('HIGH_VOL', 'CRISIS')
HIGH_VOL_FALLBACK_TARGET_PCT = 0.01  # 1% NAV when strategy missing from sizing recs


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

    # Aggregate cap (legacy 25% available_NAV) dropped 2026-05-14 per operator
    # decision — the new sharpe_cadence sizer governs deployment via λ × NAV
    # gross, per-ticker 25% cap, and $25 minimum. Executor's daily cap +
    # per-order minimum likewise removed downstream.
    return orders


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


def _load_min_cumulative_sharpe(default: float = 4.0) -> float:
    """Read min_cumulative_sharpe from pipeline_config; fall back to default.

    Used by _sharpe_cadence_path to drop tickers where the SIGNED sum of
    contributing strategies' effective_sharpe falls below this threshold.
    Naturally kills (a) low-conviction single-strategy bets and (b) tickers
    where opposing strategies nearly cancel out — the operator's
    'conflicting strategy information will cancel out' invariant.
    """
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = 'min_cumulative_sharpe'")
                row = cur.fetchone()
                v = float(row[0]) if row else default
                return max(0.0, min(50.0, v))
    except Exception:
        return default


def _load_active_window_signals(regime_state: str, weight_by_strat: dict[str, float],
                                cadence_by_strat: dict[str, int]):
    """Pull every open signal still within its strategy's cadence window.

    "Information staying relevant in the period" per design spec: a
    monthly strategy's signal contributes for ~21 trading days; a weekly
    strategy's for 5; a daily strategy's for 1. We aggregate across the
    window, not just today's emissions.

    For each (strategy, ticker) pair we keep the **most recent** open
    signal — if a strategy re-fires earlier than expected, the latest
    direction supersedes the prior one.

    Returns list of {strategy_id, ticker, direction} dicts ready for
    the ticker_weight aggregation loop.
    """
    import psycopg2
    import psycopg2.extras
    from datetime import date as _date, timedelta as _timedelta

    if not weight_by_strat:
        return []

    today = _date.today()
    # Earliest signal_date that could still be active: today - max cadence
    max_cad = max(cadence_by_strat.get(s, 1) for s in weight_by_strat) if cadence_by_strat else 1
    # +2 calendar-day buffer to absorb weekend gaps when cadence is small
    earliest = today - _timedelta(days=max_cad + 7)

    sids = list(weight_by_strat.keys())
    out = []
    tradable_symbols: set[str] = set()
    try:
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute('''
                    SELECT DISTINCT ON (strategy_id, ticker)
                           strategy_id, ticker, direction, signal_date,
                           entry_price, stop_loss, target_1, target_2
                    FROM execution_signals
                    WHERE status = 'open'
                      AND strategy_id = ANY(%s)
                      AND signal_date >= %s
                    ORDER BY strategy_id, ticker, signal_date DESC
                ''', (sids, earliest))
                rows = cur.fetchall()
                # Load the Alpaca tradable universe so delisted / halted /
                # unsupported tickers (FX, indexes, crypto) get dropped at
                # the active-window boundary — trade_handoff_builder's
                # filter doesn't apply here because the sharpe_cadence path
                # reads execution_signals directly. Fail-open on empty
                # set so a missing universe table can't halt trading.
                cur.execute("""SELECT symbol FROM alpaca_tradable_universe
                              WHERE status='active' AND tradable=TRUE""")
                tradable_symbols = {r[0] for r in cur.fetchall()}
    except Exception as e:
        logger.warning('sharpe_cadence: active-window fetch failed (%s); falling back to today-only', e)
        return []
    # Per-strategy cadence filter: drop signals older than the strategy's
    # own window. The SQL query above used max_cad as a coarse upper
    # bound; here we tighten per strategy. Also drop tickers not in the
    # tradable universe (fail-open when set is empty).
    dropped_untradable = 0
    for r in rows:
        sid = r['strategy_id']
        cad = cadence_by_strat.get(sid, 1)
        age = (today - r['signal_date']).days
        if age > cad + 2:        # 2-day weekend buffer
            continue
        if tradable_symbols and r['ticker'] not in tradable_symbols:
            dropped_untradable += 1
            continue
        out.append({'strategy_id': sid, 'ticker': r['ticker'],
                    'direction': r['direction'], 'signal_date': r['signal_date'],
                    'entry_price': r['entry_price'], 'stop_loss': r['stop_loss'],
                    'target_1': r['target_1'], 'target_2': r['target_2']})
    if dropped_untradable:
        logger.info('sharpe_cadence: dropped %d untradable signals (universe=%d)',
                    dropped_untradable, len(tradable_symbols))
    return out


def _load_broker_positions_usd():
    """Read current broker positions as {ticker: signed_market_value_usd}.

    Positive = long, negative = short. Returns empty dict on any failure
    (fail-safe: the sizer then behaves as if book is flat — emits target
    orders, executor's idempotency catches duplicates).
    """
    import subprocess
    import json as _json
    try:
        env = {**os.environ}
        # Alpaca CLI auto-detects ALPACA_API_KEY/SECRET via env.
        proc = subprocess.run(['/root/go/bin/alpaca', 'position', 'list'],
                              capture_output=True, env=env, timeout=15)
        if proc.returncode != 0:
            logger.warning('sharpe_cadence: broker position fetch failed (%s)', proc.stderr.decode()[:200])
            return {}
        positions = _json.loads(proc.stdout)
        out = {}
        for p in positions:
            try:
                qty = float(p.get('qty', 0))
                mkt = float(p.get('market_value', 0))
                if qty == 0:
                    continue
                # market_value is signed (negative for shorts); use that.
                out[p['symbol']] = mkt
            except (TypeError, ValueError):
                continue
        return out
    except Exception as e:
        logger.warning('sharpe_cadence: broker fetch error (%s)', e)
        return {}


def _sharpe_cadence_path(signals, account_state, regime_state, params, confirmer):
    """Sharpe × cadence × direction sizer with cadence-window aggregation
    AND broker-position netting.

    Steps:
      1. Pull per-(strategy, regime) daily_weight from
         strategy_weights_by_regime (load_current).
      2. Pull every still-relevant open signal from the DB (any open
         signal whose age is within its strategy's cadence_days). Latest
         signal per (strategy, ticker) wins.
      3. For each ticker, sum daily_weight × direction → ticker_weight.
      4. Normalize so Σ |target_usd| = λ × NAV exactly.
      5. Apply per-ticker 25% NAV cap + $25 minimum, bucket-aware
         redistribute (long-pool, short-pool stay separate).
      6. **Rebalance step**: read broker positions; the order_delta for
         each ticker = target_usd − current_position_usd. Tickers held
         but no longer signalled → emit close orders. This keeps daily
         PV consumption = λ × NAV instead of stacking.
      7. TradeJohn keep|cancel on the surviving deltas.
      8. Emit orders in the deterministic_sizer payload shape.

    The `signals` parameter (today's emissions) is now ignored in favour
    of the DB query — anything emitted today is already persisted to
    execution_signals by the signals step earlier in the pipeline.
    """
    from execution import strategy_weights as _sw

    nav = float(account_state.get('equity') or 100_000.0)
    rows = _sw.load_current(regime_state)
    if not rows:
        logger.info('regime_blended_sizer.sharpe_cadence: no current weights for %s', regime_state)
        return []
    weight_by_strat   = {r['strategy_id']: float(r['daily_weight']) for r in rows}
    sharpe_by_strat   = {r['strategy_id']: float(r['effective_sharpe']) for r in rows}
    cadence_by_strat  = {r['strategy_id']: int(r['cadence_days'])  for r in rows}
    # Effective leverage = global λ × per-regime liquidity_param. Mirrors
    # how _consolidate_path / _independent_path apply liquidity_param —
    # historically sharpe_cadence skipped it, so target gross was 2×NAV in
    # TRANSITIONING instead of the operator-intended 1.5×. Operator flipped
    # this 2026-05-16: prefer principled-target over execution-friction
    # workaround, friction to be reduced separately.
    lam_global   = _load_lambda()
    liq_regime   = float(params.get('liquidity_param', 1.0)) if params else 1.0
    lam = lam_global * max(0.0, min(2.0, liq_regime))
    min_cum_sharpe = _load_min_cumulative_sharpe()

    # Fix A: aggregate across cadence-window, not today-only.
    active = _load_active_window_signals(regime_state, weight_by_strat, cadence_by_strat)
    if not active:
        # Fallback: use today's `signals` parameter (e.g. force_all day-1
        # of regime, before signals are persisted)
        active = signals or []
        logger.info('regime_blended_sizer.sharpe_cadence: active-window empty, using today\'s signals (%d)', len(active))
    else:
        logger.info('regime_blended_sizer.sharpe_cadence: %d active-window signals across all cadences', len(active))

    # Aggregate ticker_weight across signalling strategies. ticker_net_sharpe
    # is the SIGNED sum of effective_sharpe — opposing strategies cancel
    # (so a long_sharpe=5 + short_sharpe=4 ticker collapses to |1|, dropped
    # by the gate below). Unsigned sum would let conflicting tickers slip
    # through.
    from collections import defaultdict
    ticker_w = defaultdict(float)
    ticker_net_sharpe = defaultdict(float)
    ticker_meta = defaultdict(lambda: {'strategies': [], 'directions': [], 'brackets': []})
    for s in active:
        sid = s.get('strategy_id')
        tkr = s.get('ticker')
        if not sid or not tkr or sid not in weight_by_strat:
            continue
        d = _dir_to_int(s.get('direction'))
        if d == 0:
            continue
        ticker_w[tkr] += weight_by_strat[sid] * d
        ticker_net_sharpe[tkr] += sharpe_by_strat.get(sid, 0.0) * d
        ticker_meta[tkr]['strategies'].append(sid)
        ticker_meta[tkr]['directions'].append(d)
        # Direction-leader bracket pick (Phase 1 spec #6): the largest-weight
        # contribution in the *winning* direction wins. We keep every
        # (direction, weight, bracket) tuple here; final selection happens
        # after ticker_w sign is known.
        ticker_meta[tkr]['brackets'].append({
            'direction':  d,
            'weight':     weight_by_strat[sid],
            'entry':      s.get('entry_price'),
            'stop':       s.get('stop_loss'),
            't1':         s.get('target_1'),
            't2':         s.get('target_2'),
        })

    if not ticker_w:
        logger.info('regime_blended_sizer.sharpe_cadence: no eligible signals after weight filter')
        return []

    # Cumulative-sharpe gate: drop tickers whose signed net_sharpe falls
    # below the configured floor (default 4.0, from pipeline_config). This
    # is the operator's primary conviction filter — kills single-strategy
    # bets AND near-cancellation tickers in one rule.
    if min_cum_sharpe > 0:
        gated_out = [tkr for tkr in list(ticker_w.keys())
                     if abs(ticker_net_sharpe.get(tkr, 0.0)) < min_cum_sharpe]
        for tkr in gated_out:
            ticker_w.pop(tkr, None)
            ticker_meta.pop(tkr, None)
        if gated_out:
            logger.info('regime_blended_sizer.sharpe_cadence: dropped %d tickers below min_cum_sharpe=%.2f (kept=%d)',
                        len(gated_out), min_cum_sharpe, len(ticker_w))

    if not ticker_w:
        logger.info('regime_blended_sizer.sharpe_cadence: no tickers cleared cum_sharpe gate')
        return []

    gross = sum(abs(w) for w in ticker_w.values())
    if gross <= 0:
        return []
    scale = (lam * nav) / gross

    # Pure formulation (2026-05-14): target_usd = ticker_w × (λ × NAV / Σ|ticker_w|).
    # High-conviction tickers (sum of contributing strategy weights × direction)
    # get proportionally more allocation; no per-ticker cap, no redistribute
    # pool. The operator explicitly accepts the concentration trade-off in
    # exchange for honest expression of conviction.
    target_usd: dict[str, float] = {tkr: w * scale for tkr, w in ticker_w.items()}

    # Per-regime min-notional gate (2026-05-16). Drop tickers whose target
    # falls below NAV × min_signal_notional_pct, then renormalize the
    # survivors so total gross still equals λ × NAV. This pushes the
    # freed capital into higher-conviction names — matches operator's
    # "fewer, larger positions" goal. Falls back to the legacy USD column
    # if the new pct column isn't present yet (pre-migration safety).
    min_notional_pct = float(params.get('min_signal_notional_pct') or 0.0) if params else 0.0
    if min_notional_pct <= 0 and params and params.get('min_signal_notional_usd'):
        min_notional_pct = float(params['min_signal_notional_usd']) / max(nav, 1.0)
    min_notional_dollars = nav * min_notional_pct
    if min_notional_dollars > 0:
        dropped_small = [t for t, v in target_usd.items() if abs(v) < min_notional_dollars]
        for tkr in dropped_small:
            target_usd.pop(tkr, None)
            ticker_w.pop(tkr, None)
            ticker_meta.pop(tkr, None)
        if dropped_small:
            new_gross = sum(abs(w) for w in ticker_w.values())
            if new_gross > 0:
                new_scale = (lam * nav) / new_gross
                target_usd = {t: w * new_scale for t, w in ticker_w.items()}
            logger.info('regime_blended_sizer.sharpe_cadence: min-notional gate dropped %d (<$%.0f = %.3f%% NAV); renormalized %d survivors',
                        len(dropped_small), min_notional_dollars, min_notional_pct * 100, len(target_usd))

    # Rebalance against current broker positions: delta = target − current.
    # Tickers held but no longer in target → close (delta = −current).
    broker = _load_broker_positions_usd()
    delta_usd: dict[str, float] = {}
    for tkr, target in target_usd.items():
        delta = target - broker.get(tkr, 0.0)
        if delta != 0.0:
            delta_usd[tkr] = delta
    for tkr, current in broker.items():
        if tkr in target_usd or current == 0.0:
            continue
        delta_usd[tkr] = -current
        if tkr not in ticker_meta:
            ticker_meta[tkr] = {'strategies': ['__close_orphan__'], 'directions': [0],
                                'brackets': []}
    logger.info('regime_blended_sizer.sharpe_cadence: targets=%d, broker=%d, deltas=%d',
                len(target_usd), len(broker), len(delta_usd))

    # TradeJohn keep|cancel (news veto only — never adjusts size).
    # Operates on DELTAS now, not targets; cancels mean "leave the
    # current position alone for this cycle".
    proposals = [{
        'ticker': tkr,
        'preliminary_size_usd': usd,
        'direction': 1 if usd >= 0 else -1,
        'contributions': [{'strategy_id': sid, 'attribution_weight': 1.0 / max(1, len(ticker_meta[tkr]['strategies']))}
                          for sid in ticker_meta[tkr]['strategies']],
        'bracket': {'entry_price': None, 'stop_loss': None, 'take_profit_1': None},
        'context': {'news_headlines': [], '30d_veto_history_for_ticker': 0,
                    'sector': '', 'hv30d': None},
    } for tkr, usd in delta_usd.items()]
    actions = {}
    if confirmer:
        try:
            actions = confirmer(proposals) or {}
        except Exception as e:
            logger.warning('regime_blended_sizer.sharpe_cadence: confirmer failed (%s); keeping all', e)
    for tkr in list(delta_usd.keys()):
        a = (actions.get(tkr) or {}).get('action', 'keep')
        if a == 'cancel':
            delta_usd.pop(tkr)

    # Emit orders for the deltas. entry/stop/t1/t2 are picked from the
    # winning-direction contributor's signal bracket (direction-leader
    # rule, mirrors _consolidate_path). Orders whose direction has no
    # matching contributor (e.g. orphan-closes) get no bracket and will
    # be filtered by the live wrapper.
    orders = []
    for tkr, usd in delta_usd.items():
        dir_sign = 1 if usd >= 0 else -1
        bracket = _select_bracket(ticker_meta[tkr].get('brackets', []), dir_sign)
        orders.append({
            'ticker':                  tkr,
            'strategy_id':             '|'.join(sorted(set(ticker_meta[tkr]['strategies'])))[:120],
            'direction':               'long' if dir_sign > 0 else 'short',
            'notional_usd':            abs(usd),
            'pct_nav':                 abs(usd) / nav,
            'shares':                  0,
            'entry':                   bracket.get('entry'),
            'stop':                    bracket.get('stop'),
            't1':                      bracket.get('t1'),
            't2':                      bracket.get('t2'),
            'kelly_final':             abs(usd) / nav,
            'ev':                      0.0,
            'p_t1':                    0.5,
            'source_mode':             'sharpe_cadence',
            'target_usd':              target_usd.get(tkr, 0.0),  # audit: what we wanted
            'current_usd':             broker.get(tkr, 0.0),       # audit: what we had
            'contributing_strategies': ticker_meta[tkr]['strategies'],
        })
    return orders


def _select_bracket(candidates: list[dict], dir_sign: int) -> dict:
    """Direction-leader bracket pick. From the contributing-signal pool,
    keep entries with matching direction, then pick the largest-weight
    bracket whose entry/stop/t1 are all populated *and finite* (NaN
    rejected — some strategies write Decimal('NaN') into execution_signals
    when their entry/stop math degenerates). Returns an empty dict if no
    usable bracket exists.
    """
    import math
    def _finite(x) -> bool:
        if x is None:
            return False
        try:
            return math.isfinite(float(x))
        except (TypeError, ValueError):
            return False

    if not candidates:
        return {}
    aligned = [b for b in candidates if b.get('direction') == dir_sign]
    usable  = [b for b in aligned
               if _finite(b.get('entry')) and _finite(b.get('stop'))
               and _finite(b.get('t1'))]
    if not usable:
        return {}
    usable.sort(key=lambda b: float(b.get('weight') or 0.0), reverse=True)
    return usable[0]

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
