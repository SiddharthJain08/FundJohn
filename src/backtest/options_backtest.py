"""Synthetic options backtest path (SP-4 Phase 0). Dispatched from
unified_backtest.run_backtest when instrument_class='option'. Returns the SAME
dict shape as unified_backtest._per_bar_simulate so the downstream metrics +
DB-write are reused unchanged.

Contracts are synthesized from underlying closes via Black-Scholes-Merton with
a trailing-year dividend yield (backtest.dividends), American exercise on a
CRR tree by default (OptionSpec.exercise), and an IV anchored to the real
surface master when it covers the date, else the VIX9D/VIX term point, else
realized-vol × VRP (backtest.synthetic_iv) — spec 2026-09-06 Part B.
"""
from __future__ import annotations
import logging
import pandas as pd
from collections import Counter
from datetime import date

logger = logging.getLogger(__name__)
_UNSUPPORTED_WARNED: set = set()

from backtest.options_pricing import (price as _price, delta as _delta, strike_for_target_delta,
                                      nearest_monthly_expiry)
from backtest.synthetic_iv import synthetic_iv_detail
from backtest.dividends import dividend_yield_asof, backfill_reference_date

MULTIPLIER = 100.0
COST_PER_CONTRACT_BPS = 5.0  # mirrors INSTRUMENT_COST_BPS['option']; on premium notional
HEDGE_COST_PER_SHARE_BPS = 1.0  # bps charged on hedge notional traded each rebalance + closeout


def _as_date(ts) -> date:
    return ts.date() if hasattr(ts, 'date') else ts


def _new_stats() -> dict:
    return {'iv_sources': Counter(), 'q_positive': 0, 'exercise': set()}


def _iv(close_to_dt: pd.Series, spec, dt, dte: int, vrp_factor: float, window: int, stats: dict) -> float:
    """IV for `spec.underlying` on `dt` at the contract's REMAINING life (spec B.4):
    surface → vix_term → realized; the tier is counted for the summary line."""
    iv, src = synthetic_iv_detail(close_to_dt, vrp_factor=vrp_factor, window=window,
                                  underlying=spec.underlying, as_of=_as_date(dt), dte=max(int(dte), 1))
    stats['iv_sources'][src] += 1
    return iv


def _q(spec, close: pd.Series, dt, S: float, stats: dict) -> float:
    """Trailing-year dividend yield; the close AT the backfill reference date is
    passed when the series reaches it (never the panel's last close) (spec B.1 /
    ruling G6)."""
    ref = backfill_reference_date()
    ref_spot = None
    if ref is not None and len(close.index) and close.index[-1] >= ref:
        upto = close.loc[:ref]
        if len(upto):
            ref_spot = float(upto.iloc[-1])
    q = dividend_yield_asof(spec.underlying, _as_date(dt), S, ref_spot=ref_spot)
    if q > 0:
        stats['q_positive'] += 1
    return q


def _select_strike(spec, S: float, t_years: float, sigma: float, flag: str, q: float = 0.0) -> float:
    if spec.strike_rule == 'atm':
        return float(S)
    if spec.strike_rule == 'fixed_moneyness' and spec.moneyness:
        return float(S * spec.moneyness)
    return strike_for_target_delta(flag, S, t_years, sigma, spec.target_delta, q=q)


def _flag_for(right: str) -> str:
    return 'p' if str(right).lower().startswith('p') else 'c'


def _legs_for(spec, S, t_years, sigma, q: float = 0.0):
    """Return list of (flag, K) legs for a straddle/strangle."""
    if spec.structure == 'straddle':
        return [('c', S), ('p', S)]  # ATM call + put
    Kc = strike_for_target_delta('c', S, t_years, sigma, spec.target_delta, q=q)
    Kp = strike_for_target_delta('p', S, t_years, sigma, spec.target_delta, q=q)
    return [('c', Kc), ('p', Kp)]


def _price_multileg_cycle(spec, close, entry_dt, sign, vrp_factor, window, max_hold_days, stats=None):
    """sign +1 long the structure, -1 short. Delta-hedged daily when spec.hedge=='delta'.
    Option PnL and hedge PnL are tracked separately; pnl_pct sums both over the cycle."""
    stats = _new_stats() if stats is None else stats
    ex = spec.exercise
    stats['exercise'].add(ex)
    idx = close.index
    fut = idx[idx > entry_dt]
    if len(fut) == 0:
        return None
    S0 = float(close.loc[entry_dt])
    expiry = nearest_monthly_expiry(_as_date(entry_dt), spec.dte_target)
    dte0 = (expiry - _as_date(entry_dt)).days
    t0 = max(dte0 / 365.0, 1e-6)
    sigma0 = _iv(close.loc[:entry_dt], spec, entry_dt, dte0, vrp_factor, window, stats)
    q0 = _q(spec, close, entry_dt, S0, stats)
    legs = _legs_for(spec, S0, t0, sigma0, q0)
    entry_prem = sum(_price(f, S0, K, t0, sigma0, as_of=_as_date(entry_dt), q=q0, exercise=ex) for f, K in legs)
    if entry_prem <= 0:
        return None

    def net_delta(S, t, sig, dt, q):
        return sum(_delta(f, S, K, max(t, 1e-6), sig, as_of=_as_date(dt), q=q, exercise=ex) for f, K in legs)

    hedge_units = 0.0
    hedge_pnl = 0.0
    prev_S = S0
    if spec.hedge == 'delta':
        target_units = -sign * net_delta(S0, t0, sigma0, entry_dt, q0) * MULTIPLIER
        hedge_pnl -= abs(target_units - hedge_units) * S0 * (HEDGE_COST_PER_SHARE_BPS / 1e4)
        hedge_units = target_units

    exit_dt = exit_prem = reason = None
    held = 0
    for dt in fut[:max_hold_days]:
        held += 1
        cur = _as_date(dt); S = float(close.loc[dt]); dte = (expiry - cur).days
        hedge_pnl += hedge_units * (S - prev_S)
        prev_S = S
        if dte <= 0:
            exit_prem = sum(max(0.0, (S - K) if f == 'c' else (K - S)) for f, K in legs)
            exit_dt, reason = dt, 'expiry'; break
        sig_t = _iv(close.loc[:dt], spec, dt, dte, vrp_factor, window, stats)
        q_t = _q(spec, close, dt, S, stats)
        if (not spec.hold_to_expiry) and dte <= spec.roll_dte:
            exit_prem = sum(_price(f, S, K, dte / 365.0, sig_t, as_of=cur, q=q_t, exercise=ex) for f, K in legs)
            exit_dt, reason = dt, 'roll'; break
        if spec.hedge == 'delta':
            target_units = -sign * net_delta(S, dte / 365.0, sig_t, dt, q_t) * MULTIPLIER
            hedge_pnl -= abs(target_units - hedge_units) * S * (HEDGE_COST_PER_SHARE_BPS / 1e4)
            hedge_units = target_units
    if exit_dt is None:
        dt = fut[:max_hold_days][-1]; S = float(close.loc[dt]); cur = _as_date(dt)
        dte = max((expiry - cur).days, 0)
        sig_t = _iv(close.loc[:dt], spec, dt, dte, vrp_factor, window, stats)
        q_t = _q(spec, close, dt, S, stats)
        exit_prem = (sum(_price(f, S, K, max(dte / 365.0, 1e-6), sig_t, as_of=cur, q=q_t, exercise=ex) for f, K in legs)
                     if dte > 0 else sum(max(0.0, (S - K) if f == 'c' else (K - S)) for f, K in legs))
        exit_dt, reason = dt, 'max_hold'

    # Liquidate the residual hedge at the exit bar (closeout friction).
    hedge_pnl -= abs(hedge_units) * S * (HEDGE_COST_PER_SHARE_BPS / 1e4)
    cost = (entry_prem + exit_prem) * (COST_PER_CONTRACT_BPS / 1e4)
    option_pnl = sign * (exit_prem - entry_prem) * MULTIPLIER - cost * MULTIPLIER
    cycle_pnl = option_pnl + hedge_pnl
    base = S0 * MULTIPLIER
    return {
        'entry_date': _as_date(entry_dt), 'exit_date': _as_date(exit_dt),
        'entry_price': round(entry_prem, 4), 'exit_price': round(exit_prem, 4),
        'exit_reason': reason, 'holding_days': held,
        'pnl_pct': float(cycle_pnl / base),
        'option_pnl_pct': float(option_pnl / base),
        'hedge_pnl_pct': float(hedge_pnl / base),
        'expiry': expiry.isoformat(), 'iv_entry': round(sigma0, 4),
        'signal_stop': None, 'signal_target': None,
    }


def _price_single_cycle(spec, close: pd.Series, entry_dt, sign: int,
                        vrp_factor: float, window: int, max_hold_days: int, stats=None) -> dict:
    """Price ONE single-leg cycle from entry_dt forward. Returns a trade dict
    or None if it can't be priced. sign +1 = long the option, -1 = short."""
    stats = _new_stats() if stats is None else stats
    ex = spec.exercise
    stats['exercise'].add(ex)
    idx = close.index
    fut = idx[idx > entry_dt]
    if len(fut) == 0:
        return None
    S0 = float(close.loc[entry_dt])
    flag = _flag_for(spec.right)
    expiry = nearest_monthly_expiry(_as_date(entry_dt), spec.dte_target)
    dte0 = (expiry - _as_date(entry_dt)).days
    t0 = max(dte0 / 365.0, 1e-6)
    sigma0 = _iv(close.loc[:entry_dt], spec, entry_dt, dte0, vrp_factor, window, stats)
    q0 = _q(spec, close, entry_dt, S0, stats)
    K = _select_strike(spec, S0, t0, sigma0, flag, q0)
    entry_premium = _price(flag, S0, K, t0, sigma0, as_of=_as_date(entry_dt), q=q0, exercise=ex)
    if entry_premium <= 0:
        return None

    exit_dt, exit_premium, reason = None, None, None
    held = 0
    for dt in fut[:max_hold_days]:
        held += 1
        cur_date = _as_date(dt)
        dte = (expiry - cur_date).days
        S = float(close.loc[dt])
        if dte <= 0:
            exit_premium = max(0.0, (S - K) if flag == 'c' else (K - S))  # intrinsic
            exit_dt, reason = dt, 'expiry'
            break
        if (not spec.hold_to_expiry) and dte <= spec.roll_dte:
            sig_t = _iv(close.loc[:dt], spec, dt, dte, vrp_factor, window, stats)
            q_t = _q(spec, close, dt, S, stats)
            exit_premium = _price(flag, S, K, max(dte / 365.0, 1e-6), sig_t, as_of=cur_date, q=q_t, exercise=ex)
            exit_dt, reason = dt, 'roll'
            break
    if exit_dt is None:
        dt = fut[:max_hold_days][-1]
        S = float(close.loc[dt]); cur_date = _as_date(dt)
        dte = max((expiry - cur_date).days, 0)
        sig_t = _iv(close.loc[:dt], spec, dt, dte, vrp_factor, window, stats)
        q_t = _q(spec, close, dt, S, stats)
        exit_premium = (_price(flag, S, K, max(dte / 365.0, 1e-6), sig_t, as_of=cur_date, q=q_t, exercise=ex)
                        if dte > 0 else max(0.0, (S - K) if flag == 'c' else (K - S)))
        exit_dt, reason = dt, 'max_hold'

    cost = (entry_premium + exit_premium) * (COST_PER_CONTRACT_BPS / 1e4)
    cycle_pnl = sign * (exit_premium - entry_premium) * MULTIPLIER - cost * MULTIPLIER
    pnl_pct = cycle_pnl / (S0 * MULTIPLIER)
    return {
        'entry_date': _as_date(entry_dt), 'exit_date': _as_date(exit_dt),
        'entry_price': round(entry_premium, 4), 'exit_price': round(exit_premium, 4),
        'exit_reason': reason, 'holding_days': held, 'pnl_pct': float(pnl_pct),
        'strike': round(K, 2), 'expiry': expiry.isoformat(), 'iv_entry': round(sigma0, 4),
        'signal_stop': None, 'signal_target': None,
    }


def simulate(instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt, *,
             strategy_id=None, resolver=None, param_override=None, max_hold_days=21,
             vrp_factor=None, window=None):
    # ``param_override`` is accepted for call-site parity with the equity
    # _per_bar_simulate but deliberately IGNORED here: option brackets are
    # priced contracts, so an equity stop/target-pct override does not apply.
    from backtest.synthetic_iv import DEFAULT_VRP_FACTOR, DEFAULT_WINDOW
    vrp_factor = DEFAULT_VRP_FACTOR if vrp_factor is None else vrp_factor
    window = DEFAULT_WINDOW if window is None else window

    min_lookback = getattr(instance, 'min_lookback', 20)
    static_universe = list(close_wide.columns)
    oos_dates = close_wide.loc[start_dt:end_dt].index

    trades, days_processed, days_with_signals = [], 0, 0
    stats = _new_stats()
    SIGN = {'LONG': 1, 'BUY': 1, 'BUY_VOL': 1, 'SHORT': -1, 'SELL': -1, 'SELL_VOL': -1}

    for current_date in oos_dates:
        prices_to_date = close_wide.loc[:current_date]
        if len(prices_to_date) < min_lookback + 5:
            continue
        regime_state = regimes.get(current_date, None)
        if regime_state is None or pd.isna(regime_state):
            continue
        regime_payload = {'state': str(regime_state), 'date': _as_date(current_date).isoformat()}
        try:
            signals = instance.generate_signals(prices_to_date, regime_payload,
                                                static_universe, aux_data={'options': {}})
        except TypeError:
            signals = instance.generate_signals(prices_to_date, regime_payload, static_universe)
        except Exception:
            logger.warning('[options_backtest] %s generate_signals failed on %s',
                           getattr(instance, 'id', '?'), _as_date(current_date), exc_info=True)
            continue
        days_processed += 1
        if not signals:
            continue
        days_with_signals += 1

        for sig in signals[:instance.MAX_SIGNALS]:
            spec = getattr(sig, 'option_spec', None)
            if spec is None:
                continue
            sign = SIGN.get(str(sig.direction).upper(), 0)
            if sign == 0:
                continue
            ul = spec.underlying
            if ul not in close_wide.columns:
                continue
            from backtest.vol_index import is_supported_option_underlying
            if not is_supported_option_underlying(ul) and ul not in _UNSUPPORTED_WARNED:
                _UNSUPPORTED_WARNED.add(ul)
                logger.warning('[options_backtest] underlying %s is NOT in VALID_OPTION_UNDERLYINGS '
                               '— IV uses the low-fidelity realized-vol fallback (~25%% off real IV); '
                               'backtest metrics are NOT trustworthy for promotion.', ul)
            # roll-then-reopen: keep re-entering while the prior cycle ended on a roll
            cursor = current_date
            remaining = max_hold_days
            while remaining > 0 and cursor is not None:
                series = close_wide[ul].dropna()
                if spec.structure == 'single':
                    cyc = _price_single_cycle(spec, series, cursor, sign,
                                              vrp_factor, window, remaining, stats=stats)
                else:
                    cyc = _price_multileg_cycle(spec, series, cursor, sign,
                                                vrp_factor, window, remaining, stats=stats)
                if cyc is None:
                    break
                cyc.update({'ticker': ul, 'direction': 'long' if sign > 0 else 'short',
                            'entry_regime': str(regime_state)})
                trades.append(cyc)
                remaining -= cyc['holding_days']
                if cyc['exit_reason'] != 'roll':
                    break
                # next cycle starts at the bar AFTER this cycle's exit
                later = series.index[series.index > pd.Timestamp(cyc['exit_date'])]
                cursor = later[0] if len(later) else None

    src = stats['iv_sources']
    logger.info('[options_backtest] iv sources: surface=%d vix_term=%d realized=%d; exercise=%s; q>0 on %d prices',
                src.get('surface', 0), src.get('vix_term', 0), src.get('realized', 0),
                ','.join(sorted(stats['exercise'])) or 'n/a', stats['q_positive'])
    return {'trades': trades, 'universe_sizes': [], 'days_processed': days_processed,
            'days_with_signals': days_with_signals, 'static_universe': static_universe,
            'min_lookback': min_lookback}
