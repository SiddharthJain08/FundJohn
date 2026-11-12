"""SP-3.1 Phase C: crypto-scoped position sizer for the dedicated crypto redeploy.

Reuses the regime-blended weight model (strategy_weights.load_current → daily_weight
→ ticker_weight → normalize) but (a) scoped to crypto strategies, (b) sized to crypto's
PROPORTIONAL weight slice of the leverage budget (margin-funded, not the full λ×NAV),
and (c) **broker positions filtered to crypto tickers before any delta/orphan logic**,
so equity positions are structurally invisible to this path (the load-bearing safety
invariant). Mirrors regime_blended_sizer._sharpe_cadence_path steps 1-5; equity orphan
logic is never invoked here."""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)

LAMBDA_GLOBAL = 2.0  # matches the equity sizer's default leverage cap


def _is_crypto_ticker(t: str) -> bool:
    return bool(t) and t.strip().upper().endswith('-USD')


def _normalize_broker_symbol(t: str) -> str:
    """Map a broker crypto symbol to the engine BASE-USD (dash) convention.

    Thin alias over the ONE normaliser (crypto engine Phase 3, D-1):
    ``ingestion.crypto_flow.universe.internal_symbol``. 'BTCUSD' (broker
    position form), 'BTC/USD' (asset/order form) and 'BTC-USD' all map to
    'BTC-USD'; an equity ticker is only stripped + upper-cased, so an equity
    position can never be mis-mapped into crypto (the equity-untouched
    invariant)."""
    from ingestion.crypto_flow import universe as _u
    return _u.internal_symbol(t)


def _dir_to_int(d) -> int:
    s = str(d or '').lower()
    return 1 if s in ('long', 'buy', '1') else (-1 if s in ('short', 'sell', '-1') else 0)


def _read_structured_handoff(run_date_str: str):
    from execution.handoff import read_handoff
    return read_handoff(run_date_str, 'structured')


def _load_equity_regime_and_params(run_date_str: str | None = None, *, conn=None,
                                   handoff_reader=None) -> tuple[str | None, dict | None]:
    """Equity regime + its regime_sizer_params row, resolved EXACTLY as the live
    trade step does (regime_blended_sizer_live.main): the day's structured
    handoff regime, falling back to the newest market_regime row. Fail-closed:
    (None, None) ⇒ the pooled redeploy emits nothing (crypto HELD) rather than
    sizing against an unknown equity regime."""
    import os
    from datetime import date as _date
    run_date_str = run_date_str or _date.today().isoformat()
    state = None
    try:
        h = (handoff_reader or _read_structured_handoff)(run_date_str)
        hr = (h or {}).get('regime') if isinstance(h, dict) else None
        if isinstance(hr, dict):
            state = hr.get('state')
    except Exception as e:  # noqa: BLE001 — no handoff ⇒ DB fallback
        logger.info('[crypto_redeploy] structured handoff unreadable (%s); market_regime fallback', e)
    own = conn is None
    try:
        if own:
            import psycopg2
            conn = psycopg2.connect(os.environ['POSTGRES_URI'])
        try:
            import psycopg2.extras as _ex
            cur = conn.cursor(cursor_factory=_ex.RealDictCursor)
        except Exception:  # noqa: BLE001 — fakes
            cur = conn.cursor()
        if not state:
            cur.execute("SELECT state FROM market_regime ORDER BY updated_at DESC LIMIT 1")
            row = cur.fetchone()
            state = (row or {}).get('state') if row else None
        if not state:
            logger.warning('[crypto_redeploy] no equity regime (handoff + market_regime empty)')
            return None, None
        cur.execute("SELECT * FROM regime_sizer_params WHERE regime_state = %s", (state,))
        prow = cur.fetchone()
    except Exception as e:  # noqa: BLE001
        logger.warning('[crypto_redeploy] equity regime/params load failed (%s)', e)
        return None, None
    finally:
        if own and conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    params = dict(prow) if prow else {'liquidity_param': 1.0,
                                      'position_circuit_breaker_pct': 0.02}
    return state, params


def _default_pooled_runner(account_state, equity_regime, equity_params,
                           crypto_regime, crypto_params) -> list[dict]:
    """One pass of the pooled sizer body (regime_blended_sizer._sharpe_cadence_path).

    Deliberately ONE level below _size_positions_impl: in the same-day lane
    _size_positions_impl runs _check_force_fire_flag(), which DELETES the
    equity pipeline's one-shot regime:transition:fresh key — a 24/7 crypto
    redeploy must never consume it. signals=[] because the body self-loads
    every signal from execution_signals (the parameter is only the legacy
    fallback when the equity active window is empty; there is no handoff
    here). Runs QUIET (regime_blended_sizer._REDEPLOY_QUIET)."""
    from execution import regime_blended_sizer as _rbs
    # Quiet pass (fix round 1): the equity orders are discarded, so no equity
    # alert/line may post and the QP shadow is skipped; crypto posts still fire.
    _prev = _rbs._REDEPLOY_QUIET
    _rbs._REDEPLOY_QUIET = True
    try:
        return _rbs._sharpe_cadence_path([], account_state, equity_regime, equity_params, None,
                                         crypto_regime_state=crypto_regime,
                                         crypto_params=crypto_params)
    finally:
        _rbs._REDEPLOY_QUIET = _prev


def _to_executor_orders(raw: list[dict], nav: float) -> list[dict]:
    """Raw sizer orders → the executor contract (close_only on bracket-less
    orders, pct_nav, …) through the SAME adapter the 15:00 trade step uses."""
    from execution.regime_blended_sizer_live import _build_sized_payload
    return _build_sized_payload(raw, {'regime': {}}, equity=nav)['orders']


def _size_crypto_pooled(account_state: dict, crypto_regime_state: dict, *,
                        pooled_runner=None, regime_loader=None, crypto_loader=None,
                        hold_state: dict | None = None) -> list[dict]:
    """OPENCLAW_CRYPTO_POOLED_SIZING=1: the redeploy is a thin re-run of the
    pooled sizer. The full pooled book is computed (so crypto keeps exactly
    the share of λ·NAV the 15:00 pooled normaliser gives it next to today's
    equity book), then ONLY crypto orders are kept; equity orders are
    discarded and counted. Hold-flat is applied inside the pooled block
    (crypto_hold.hold_state) and re-checked here (belt and braces)."""
    from execution import pooled_sizing as _ps
    from execution.regime_blended_sizer import _is_crypto_key
    nav = float(account_state.get('equity') or 0.0)
    if nav <= 0:
        return []
    cr_state, cr_params = (crypto_loader or _ps.load_crypto_regime_and_params)()
    if not cr_state:
        logger.info('[crypto_redeploy] pooled: no crypto regime — no orders')
        return []
    if crypto_regime_state.get('state') and crypto_regime_state.get('state') != cr_state:
        logger.info('[crypto_redeploy] pooled: crypto regime moved %s -> %s between reads; using %s',
                    crypto_regime_state.get('state'), cr_state, cr_state)
    eq_state, eq_params = (regime_loader or _load_equity_regime_and_params)()
    if not eq_state:
        logger.warning('[crypto_redeploy] pooled: no equity regime — crypto HELD (no orders)')
        return []
    raw = (pooled_runner or _default_pooled_runner)(
        account_state, eq_state, eq_params, cr_state, cr_params) or []
    crypto_raw = [o for o in raw if _is_crypto_key(o.get('ticker'))]
    n_eq = len(raw) - len(crypto_raw)
    logger.info('crypto_redeploy: equity_orders_discarded=%d crypto_orders=%d', n_eq, len(crypto_raw))
    print(f'crypto_redeploy: equity_orders_discarded={n_eq} crypto_orders={len(crypto_raw)}')
    orders = _to_executor_orders(crypto_raw, nav)

    if hold_state is None:
        from execution import crypto_hold as _ch
        hold_state = _ch.hold_state()
    sleeve_hold = bool((hold_state or {}).get('sleeve'))
    held = {_normalize_broker_symbol(t) for t in ((hold_state or {}).get('tickers') or {})}
    if sleeve_hold or held:
        kept = [o for o in orders
                if o.get('close_only')
                or str(o.get('direction') or '').lower() != 'long'
                or not (sleeve_hold or _normalize_broker_symbol(o.get('ticker') or '') in held)]
        if len(kept) != len(orders):
            logger.info('[crypto_redeploy] hold_flat active: %d crypto buy(s) dropped',
                        len(orders) - len(kept))
        orders = kept
    return orders


def size_crypto_positions(account_state: dict, crypto_regime_state: dict, *,
                          broker_loader=None, weights_loader=None, signals_loader=None,
                          crypto_strategy_ids=None, all_live_weight_sum=None,
                          hold_state: dict | None = None,
                          pooled_runner=None, regime_loader=None,
                          crypto_loader=None) -> list[dict]:
    """Return crypto order dicts (ticker, direction, notional_usd, strategy_id, close_only).
    All loaders are injectable for testing; defaults wire to the live sources.

    OPENCLAW_CRYPTO_POOLED_SIZING=1 ⇒ _size_crypto_pooled (the pooled sizer,
    crypto orders only); unset ⇒ the legacy budget model below, unchanged."""
    from execution.pooled_sizing import pooled_sizing_enabled
    if pooled_sizing_enabled():
        return _size_crypto_pooled(account_state, crypto_regime_state,
                                   pooled_runner=pooled_runner, regime_loader=regime_loader,
                                   crypto_loader=crypto_loader, hold_state=hold_state)
    regime = crypto_regime_state.get('state')
    if not regime:
        logger.info('[crypto_sizer] no crypto regime — no orders')
        return []
    nav = float(account_state.get('equity') or 0.0)
    if nav <= 0:
        return []

    if weights_loader is None:
        from execution import strategy_weights as _sw
        weights_loader = _sw.load_current
    rows = [r for r in (weights_loader(regime) or []) if r.get('strategy_id')]
    if crypto_strategy_ids is None:
        from strategies.instrument_class import instrument_class_for
        crypto_strategy_ids = {r['strategy_id'] for r in rows
                               if instrument_class_for(r['strategy_id']) == 'crypto'}
    weight_by_strat = {r['strategy_id']: float(r.get('daily_weight') or 0.0) for r in rows
                       if r['strategy_id'] in crypto_strategy_ids}
    if not weight_by_strat:
        return []

    if signals_loader is None:
        from execution import strategy_weights as _sw
        signals_loader = lambda rg, wbs: _sw.load_active_signals(rg, wbs) if hasattr(_sw, 'load_active_signals') else []
    signals = signals_loader(regime, weight_by_strat) or []

    ticker_w: dict[str, float] = {}
    for s in signals:
        sid, tkr = s.get('strategy_id'), s.get('ticker')
        if sid not in weight_by_strat or not _is_crypto_ticker(tkr or ''):
            continue
        ticker_w[tkr] = ticker_w.get(tkr, 0.0) + weight_by_strat[sid] * _dir_to_int(s.get('direction'))
    ticker_w = {t: w for t, w in ticker_w.items() if w != 0.0}

    if all_live_weight_sum is None:
        all_live_weight_sum = sum(float(r.get('daily_weight') or 0.0) for r in rows) or 1.0
    crypto_weight_sum = sum(weight_by_strat.values())
    # crypto_share is a fraction of the leverage budget — clamp to [0,1] so a
    # misconfigured/over-allocated weight set can never exceed LAMBDA_GLOBAL×NAV.
    crypto_share = min(1.0, max(0.0, (crypto_weight_sum / all_live_weight_sum) if all_live_weight_sum else 0.0))
    crypto_budget = LAMBDA_GLOBAL * nav * crypto_share
    abs_w = sum(abs(w) for w in ticker_w.values())
    target_usd = {t: (w * crypto_budget / abs_w) for t, w in ticker_w.items()} if abs_w else {}

    # Crypto breaker hold-flat: zero targets (sleeve hold) or the held tickers'.
    if hold_state is None:
        from execution import crypto_hold as _ch
        hold_state = _ch.hold_state()
    sleeve_hold = bool((hold_state or {}).get('sleeve'))
    held_tickers = set((hold_state or {}).get('tickers') or {})
    if sleeve_hold:
        logger.info('[crypto_redeploy] hold_flat active: targets zeroed (sleeve)')
        target_usd = {t: 0.0 for t in target_usd}
    elif held_tickers & set(target_usd):
        logger.info('[crypto_redeploy] hold_flat active: targets zeroed (tickers: %s)',
                    sorted(held_tickers & set(target_usd)))
        target_usd = {t: (0.0 if t in held_tickers else v) for t, v in target_usd.items()}

    if broker_loader is None:
        from execution.regime_blended_sizer import _load_broker_positions_usd
        broker_loader = _load_broker_positions_usd
    # Normalize broker symbols to the dash convention BEFORE filtering, so a real
    # Alpaca 'BTC/USD' position nets against a 'BTC-USD' target. Equity symbols
    # have no '/', stay unchanged, and remain excluded by the -USD filter.
    broker_crypto = {}
    for _t, _v in (broker_loader() or {}).items():
        _norm = _normalize_broker_symbol(_t)
        if _is_crypto_ticker(_norm):
            broker_crypto[_norm] = _v

    orders: list[dict] = []
    for tkr, tgt in target_usd.items():
        cur = broker_crypto.get(tkr, 0.0)
        delta = tgt - cur
        if abs(delta) < 1.0:
            continue
        if tgt == 0.0 and (sleeve_hold or tkr in held_tickers):
            # held: only a close, tagged so the audit shows the breaker hold
            orders.append({'ticker': tkr, 'direction': 'short' if cur > 0 else 'long',
                           'notional_usd': abs(cur), 'strategy_id': '__crypto_breaker_hold__',
                           'close_only': True, 'current_usd': cur})
            continue
        # Emit BOTH notional_usd AND pct_nav (matches the equity sizer contract,
        # regime_blended_sizer.py): the executor's crypto OPEN path computes
        # notional = equity × pct_nav, so a missing pct_nav would SKIP the order.
        orders.append({'ticker': tkr, 'direction': 'long' if delta > 0 else 'short',
                       'notional_usd': abs(delta), 'pct_nav': abs(delta) / nav,
                       'strategy_id': 'crypto_redeploy', 'close_only': False})
    for tkr, cur in broker_crypto.items():
        if tkr in target_usd or cur == 0.0:
            continue
        orders.append({'ticker': tkr, 'direction': 'short' if cur > 0 else 'long',
                       'notional_usd': abs(cur), 'strategy_id': ('__crypto_breaker_hold__' if (sleeve_hold or tkr in held_tickers)
                                        else '__close_orphan_crypto__'),
                       'close_only': True, 'current_usd': cur})
    return orders
