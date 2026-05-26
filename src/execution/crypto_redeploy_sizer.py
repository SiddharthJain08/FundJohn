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
    Alpaca position list returns crypto as 'BTC/USD' (slash; confirmed Task 0 +
    Phase B smoke); signals/engine use 'BTC-USD' (dash). '/' never appears in an
    equity ticker, so this can never mis-map an equity position into crypto — the
    equity-untouched invariant is preserved. (A no-separator 'BTCUSD' form, if
    Alpaca ever returns one, stays excluded = fail-closed; confirm at Phase D.)"""
    return (t or '').strip().upper().replace('/', '-')


def _dir_to_int(d) -> int:
    s = str(d or '').lower()
    return 1 if s in ('long', 'buy', '1') else (-1 if s in ('short', 'sell', '-1') else 0)


def size_crypto_positions(account_state: dict, crypto_regime_state: dict, *,
                          broker_loader=None, weights_loader=None, signals_loader=None,
                          crypto_strategy_ids=None, all_live_weight_sum=None) -> list[dict]:
    """Return crypto order dicts (ticker, direction, notional_usd, strategy_id, close_only).
    All loaders are injectable for testing; defaults wire to the live sources."""
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
        orders.append({'ticker': tkr, 'direction': 'long' if delta > 0 else 'short',
                       'notional_usd': abs(delta), 'strategy_id': 'crypto_redeploy',
                       'close_only': False})
    for tkr, cur in broker_crypto.items():
        if tkr in target_usd or cur == 0.0:
            continue
        orders.append({'ticker': tkr, 'direction': 'short' if cur > 0 else 'long',
                       'notional_usd': abs(cur), 'strategy_id': '__close_orphan_crypto__',
                       'close_only': True, 'current_usd': cur})
    return orders
