"""Regime-blended sizer.

Pipeline orchestrator's `trade` step calls this via regime_blended_sizer_live
for production submission. Single path: sharpe_cadence — the per-(strategy,
regime) daily_weight × direction aggregator with broker-position netting.

Legacy mode dispatch (consolidate / independent paths) and the
OPENCLAW_SHARPE_CADENCE_SIZER feature flag were removed 2026-05-21 once
sharpe_cadence had been LIVE since 2026-05-12 and validated through
multiple production cycles. The historical paths and the ticker_consolidator
module they used live in git history only.
"""
from __future__ import annotations
import logging
import os
from datetime import date

from execution.signal_cadence_gate import filter_by_cadence, advance_last_fire
from execution.tradejohn_confirmer import confirm as default_confirmer

logger = logging.getLogger(__name__)


def size_positions(
    signals: list[dict],
    account_state: dict,
    regime: dict,
    run_date: date,
    strategy_state: dict,
    regime_params: dict,
    confirmer=None,
) -> list[dict]:
    """Returns list of sized orders for the cycle.

    Pipeline:
      1. Cadence gate (filter_by_cadence) — drops signals from strategies
         whose next_fire_date is still in the future. Bypassed for one
         cycle when regime_liquidator sets regime:transition:fresh in Redis.
      2. Sharpe-cadence path — pulls active-window signals from the DB,
         aggregates ticker_w across contributing strategies, normalizes
         to λ × NAV, delta-rebalances against current broker positions,
         and routes flip cases through paired close/open emissions.

    Caller (regime_blended_sizer_live) handles persistence to
    execution_signals and the sized handoff downstream.
    """
    regime_state = regime['state']

    # Cadence gate. Bypassed for one cycle when the regime liquidator
    # has set regime:transition:fresh in Redis (fresh post-transition
    # cycle should run every eligible strategy without per-strategy gating).
    force_all = _check_force_fire_flag()
    passed, skipped = filter_by_cadence(signals, strategy_state, run_date, force_all=force_all)
    if skipped:
        logger.info('regime_blended_sizer: cadence skipped %d signals', len(skipped))
    if force_all:
        logger.info('regime_blended_sizer: force_all=True — cadence bypassed (regime transition)')

    if not passed:
        return []

    return _sharpe_cadence_path(passed, account_state, regime_state,
                                regime_params, confirmer or default_confirmer)


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


def _derive_action(kind: str, out_current: float, out_target: float, dir_sign: int) -> str:
    """Human-readable trade action, derived from the order's emission kind and
    the current/target position USD. The wire field `direction` (long/short)
    stays the order SIDE (buy/sell) the executor reads; `action` is for humans
    (so a long-*reduction* reads `reduce_long`, not `short`)."""
    cur = out_current or 0.0
    tgt = out_target or 0.0
    if kind in ('orphan_close', 'flip_close'):
        return 'close_long' if cur > 0 else 'close_short'
    if kind == 'flip_open':
        return 'flip_to_long' if tgt > 0 else 'flip_to_short'
    # delta
    if cur == 0.0:
        return 'open_long' if tgt > 0 else 'open_short'
    if (cur > 0) == (tgt > 0):                      # same side
        side = 'long' if cur > 0 else 'short'
        return f'add_{side}' if abs(tgt) > abs(cur) else f'reduce_{side}'
    return 'flip_to_long' if tgt > 0 else 'flip_to_short'   # opposite-sign delta (defensive)


def _load_lambda(default: float = 2.0) -> float:
    """Read position_sizing_lambda from pipeline_config; fall back to default
    on any error. Bounded clamp guards against operator-pasted garbage.

    Cap = 2.0 (Reg T overnight max). 2026-05-19: tightened from 3.50 to
    2.00 to match Reg T overnight rule (50% initial margin → 2× equity
    gross). Pre-existing values above 2.0 silently clamp on next cycle —
    intentional, since trading over 2× overnight would violate Reg T."""
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = 'position_sizing_lambda'")
                row = cur.fetchone()
                v = float(row[0]) if row else default
                return max(0.10, min(2.00, v))
    except Exception:
        return default


def _resolve_min_cumulative_sharpe(params: dict | None, default: float = 3.0) -> float:
    """Pick the conviction floor for the active regime.

    Used by _sharpe_cadence_path to drop tickers where the SIGNED sum of
    contributing strategies' effective_sharpe falls below this threshold.
    Naturally kills (a) low-conviction single-strategy bets and (b) tickers
    where opposing strategies nearly cancel out — the operator's
    'conflicting strategy information will cancel out' invariant.

    Per-regime as of 2026-05-21 (migration 108) — stored on
    regime_sizer_params.min_cumulative_sharpe, bound [1.0, 10.0] by the
    table's CHECK constraint. The sizer reads the value from the
    `params` dict already loaded for the active regime, so no extra DB
    round-trip is needed. Falls back to the legacy
    pipeline_config['min_cumulative_sharpe'] global if the per-regime
    field is missing (e.g. brand-new DB before the migration runs),
    then to `default` if that's also absent.
    """
    if isinstance(params, dict):
        v = params.get('min_cumulative_sharpe')
        if v is not None:
            try:
                return max(1.0, min(10.0, float(v)))
            except (TypeError, ValueError):
                pass
    try:
        import psycopg2
        with psycopg2.connect(os.environ['POSTGRES_URI']) as c:
            with c.cursor() as cur:
                cur.execute("SELECT value FROM pipeline_config WHERE key = 'min_cumulative_sharpe'")
                row = cur.fetchone()
                if row is not None:
                    return max(1.0, min(10.0, float(row[0])))
    except Exception:
        pass
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
    # Earliest signal_date that could still be active: today - max cadence.
    # No weekend buffer: cadence_days is now sourced from live avg_holding_days
    # (in strategy_weights._load_active_strategies), so a strategy that
    # appears stale on Monday morning genuinely hasn't generated new info.
    # The user's invariant (2026-05-19): only fresh information from the
    # cycle's actual cadence window — no expired information.
    max_cad = max(cadence_by_strat.get(s, 1) for s in weight_by_strat) if cadence_by_strat else 1
    earliest = today - _timedelta(days=max_cad)

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
    dropped_stale = 0
    for r in rows:
        sid = r['strategy_id']
        cad = cadence_by_strat.get(sid, 1)
        age = (today - r['signal_date']).days
        # Strict cadence: a cad=1 (daily) strategy contributes ONLY today's
        # signal; a cad=4 (4-day-holding) strategy contributes the last
        # 4 days of signals (age in [0,3]). Drop everything older.
        if age >= cad:
            dropped_stale += 1
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
    if dropped_stale:
        logger.info('sharpe_cadence: dropped %d stale signals (age >= cadence_days)', dropped_stale)
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
      4. Normalize so Σ |target_usd| = λ × NAV exactly. No per-ticker
         caps and no minimum-notional floor — high-conviction tickers
         (multi-strategy agreement) receive proportional allocation.
      5. **Rebalance step**: read broker positions; the order_delta for
         each ticker = target_usd − current_position_usd. Tickers held
         but no longer signalled → emit close orders. This keeps daily
         PV consumption = λ × NAV instead of stacking.
      6. TradeJohn keep|cancel on the surviving deltas.
      7. Emit orders in the sized-handoff payload shape.

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
    # Effective leverage = global λ × per-regime liquidity_param.
    # liquidity_param is a per-regime DAMPENER (∈ [0, 1.0]); paired with
    # lam_global ∈ [0.10, 2.00] this guarantees effective lam ≤ 2.0 (Reg T
    # overnight rule, 50% initial margin). The 1.0 cap matches the DB CHECK
    # constraint on regime_sizer_params.liquidity_param so a misconfigured
    # row can't slip past the sizer. Historically sharpe_cadence skipped
    # liquidity_param entirely; operator added it 2026-05-16 so per-regime
    # target gross matches the documented intent (TRANSITIONING 1.5×, etc.).
    lam_global   = _load_lambda()
    liq_regime   = float(params.get('liquidity_param', 1.0)) if params else 1.0
    lam = lam_global * max(0.0, min(1.0, liq_regime))
    min_cum_sharpe = _resolve_min_cumulative_sharpe(params)

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
    # below the configured floor (default 3.0, from pipeline_config). This
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

    # Rebalance against current broker positions. Each ticker is classified as
    # one of four emission kinds:
    #   • delta         — single order; close_only auto-detected downstream
    #                     when the delta direction has no aligned bracket.
    #   • orphan_close  — ticker held but absent from current targets;
    #                     strategy_id='__close_orphan__', tier-0 in executor.
    #   • flip_close    — current and target have OPPOSITE signs. Liquidates
    #                     the existing position fully. Tier-1 in executor.
    #                     Paired with flip_open below; flip_open must wait
    #                     for the close to fill (executor polls). Alpaca
    #                     bracket orders do not support auto-reverse so we
    #                     CANNOT submit a single oversize bracket — the
    #                     legacy delta path emitted exactly that and Alpaca
    #                     silently rejected it (or filled only the close
    #                     portion).
    #   • flip_open     — the paired new-direction open following flip_close.
    #                     Tier-2 (short) or tier-3 (long) in executor.
    broker = _load_broker_positions_usd()

    flip_tickers: set[str] = set()
    for tkr, target in target_usd.items():
        current = broker.get(tkr, 0.0)
        if current == 0.0 or target == 0.0:
            continue
        # Opposite-sign check
        if (target > 0 > current) or (target < 0 < current):
            flip_tickers.add(tkr)

    emissions: list[tuple[str, float, str]] = []
    for tkr, target in target_usd.items():
        current = broker.get(tkr, 0.0)
        if tkr in flip_tickers:
            emissions.append((tkr, -current, 'flip_close'))
            emissions.append((tkr,  target,  'flip_open'))
        else:
            delta = target - current
            if delta != 0.0:
                emissions.append((tkr, delta, 'delta'))
    for tkr, current in broker.items():
        if tkr in target_usd or current == 0.0:
            continue
        emissions.append((tkr, -current, 'orphan_close'))
        if tkr not in ticker_meta:
            ticker_meta[tkr] = {'strategies': ['__close_orphan__'], 'directions': [0],
                                'brackets': []}
    logger.info(
        'regime_blended_sizer.sharpe_cadence: targets=%d, broker=%d, emissions=%d (flips=%d)',
        len(target_usd), len(broker), len(emissions), len(flip_tickers))

    # TradeJohn keep|cancel — proposed ONLY for new-exposure emissions
    # (delta, flip_open). Closes (orphan_close, flip_close) always execute:
    # orphan closes have no current signal to veto; flip closes are the
    # first leg of an atomic flip and a cancel on the matched flip_open
    # drops the flip_close below so the existing position is preserved.
    exposure_emissions = [(tkr, usd, kind) for (tkr, usd, kind) in emissions
                          if kind in ('delta', 'flip_open')]
    proposals = [{
        'ticker': tkr,
        'preliminary_size_usd': usd,
        'direction': 1 if usd >= 0 else -1,
        'contributions': [{'strategy_id': sid, 'attribution_weight': 1.0 / max(1, len(ticker_meta[tkr]['strategies']))}
                          for sid in ticker_meta[tkr]['strategies']],
        'bracket': {'entry_price': None, 'stop_loss': None, 'take_profit_1': None},
        'context': {'news_headlines': [], '30d_veto_history_for_ticker': 0,
                    'sector': '', 'hv30d': None},
    } for (tkr, usd, kind) in exposure_emissions]
    actions = {}
    if confirmer:
        try:
            actions = confirmer(proposals) or {}
        except Exception as e:
            logger.warning('regime_blended_sizer.sharpe_cadence: confirmer failed (%s); keeping all', e)

    canceled_deltas = {tkr for (tkr, _, kind) in emissions
                       if kind == 'delta' and (actions.get(tkr) or {}).get('action') == 'cancel'}
    canceled_flips = {tkr for tkr in flip_tickers
                      if (actions.get(tkr) or {}).get('action') == 'cancel'}
    emissions = [e for e in emissions
                 if not (e[2] == 'delta' and e[0] in canceled_deltas)
                 and not (e[2] in ('flip_close', 'flip_open') and e[0] in canceled_flips)]
    if canceled_flips:
        logger.info('regime_blended_sizer.sharpe_cadence: %d flips canceled by confirmer; existing positions preserved',
                    sorted(canceled_flips))

    # Emit orders. flip_close uses a dedicated strategy_id ('__flip_close__')
    # so the (run_date, strategy_id, ticker) UNIQUE constraint on
    # alpaca_submissions never collides with the matched flip_open row
    # (whose strategy_id is the real joined contributing IDs). The executor
    # detects flip_close by strategy_id, polls Alpaca until the close fills,
    # and only then submits the flip_open — Alpaca cannot transition
    # long↔short in a single bracket order.
    orders = []
    for tkr, usd, kind in emissions:
        dir_sign = 1 if usd >= 0 else -1
        if kind in ('orphan_close', 'flip_close'):
            bracket = {}     # forces close_only=True downstream
        else:
            bracket = _select_bracket(ticker_meta[tkr].get('brackets', []), dir_sign)
        real_sid = '|'.join(sorted(set(ticker_meta[tkr]['strategies'])))[:120]
        if kind == 'flip_close':
            sid_out = '__flip_close__'
            out_target = 0.0
            out_current = broker.get(tkr, 0.0)
        elif kind == 'flip_open':
            sid_out = real_sid
            out_target = target_usd.get(tkr, 0.0)
            out_current = 0.0          # post-close perspective; matches the broker state the open will see
        else:
            sid_out = real_sid
            out_target = target_usd.get(tkr, 0.0)
            out_current = broker.get(tkr, 0.0)
        orders.append({
            'ticker':                  tkr,
            'strategy_id':             sid_out,
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
            'target_usd':              out_target,
            'current_usd':             out_current,
            'contributing_strategies': ticker_meta[tkr]['strategies'],
            'flip_action':             kind if kind in ('flip_close', 'flip_open') else None,
            'action':                  _derive_action(kind, out_current, out_target, dir_sign),
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
