"""Synthetic options backtest path (SP-4 Phase 0). Dispatched from
unified_backtest.run_backtest when instrument_class='option'. Returns the SAME
dict shape as unified_backtest._per_bar_simulate so the downstream metrics +
DB-write are reused unchanged.

Contracts are synthesized from underlying closes via Black-Scholes
(options_pricing) + a calibrated realized-vol×VRP IV (synthetic_iv). One
contract per signal on a 100x multiplier; pnl_pct = cycle_pnl / (entry_S*100)
(see plan "PnL convention"). Single-leg here; straddle/strangle + delta-hedge
+ roll/expiry land in later tasks.
"""
from __future__ import annotations
import pandas as pd
from datetime import date

from backtest.options_pricing import (bs_price, bs_greeks, strike_for_target_delta,
                                       nearest_monthly_expiry, RISK_FREE)
from backtest.synthetic_iv import synthetic_iv

MULTIPLIER = 100.0
COST_PER_CONTRACT_BPS = 5.0  # mirrors INSTRUMENT_COST_BPS['option']; on premium notional


def _as_date(ts) -> date:
    return ts.date() if hasattr(ts, 'date') else ts


def _select_strike(spec, S: float, t_years: float, sigma: float, flag: str) -> float:
    if spec.strike_rule == 'atm':
        return float(S)
    if spec.strike_rule == 'fixed_moneyness' and spec.moneyness:
        return float(S * spec.moneyness)
    return strike_for_target_delta(flag, S, t_years, sigma, spec.target_delta)


def _flag_for(right: str) -> str:
    return 'p' if str(right).lower().startswith('p') else 'c'


def _legs_for(spec, S, t_years, sigma):
    """Return list of (flag, K) legs for a straddle/strangle."""
    if spec.structure == 'straddle':
        return [('c', S), ('p', S)]  # ATM call + put
    # strangle: OTM call + OTM put at target_delta
    Kc = strike_for_target_delta('c', S, t_years, sigma, spec.target_delta)
    Kp = strike_for_target_delta('p', S, t_years, sigma, spec.target_delta)
    return [('c', Kc), ('p', Kp)]


def _price_multileg_cycle(spec, close, entry_dt, sign, vrp_factor, window, max_hold_days):
    """sign +1 long the structure, -1 short. Delta-hedged daily when spec.hedge=='delta'.
    Option PnL and hedge PnL are tracked separately; pnl_pct sums both over the cycle."""
    idx = close.index
    fut = idx[idx > entry_dt]
    if len(fut) == 0:
        return None
    S0 = float(close.loc[entry_dt])
    sigma0 = synthetic_iv(close.loc[:entry_dt], vrp_factor=vrp_factor, window=window)
    expiry = nearest_monthly_expiry(_as_date(entry_dt), spec.dte_target)
    t0 = max((expiry - _as_date(entry_dt)).days / 365.0, 1e-6)
    legs = _legs_for(spec, S0, t0, sigma0)
    entry_prem = sum(bs_price(f, S0, K, t0, sigma0) for f, K in legs)
    if entry_prem <= 0:
        return None

    def net_delta(S, t, sig):
        return sum(bs_greeks(f, S, K, max(t, 1e-6), sig)['delta'] for f, K in legs)

    hedge_units = 0.0
    hedge_cost_bps = 1.0
    hedge_pnl = 0.0
    prev_S = S0
    if spec.hedge == 'delta':
        target_units = -sign * net_delta(S0, t0, sigma0) * MULTIPLIER
        hedge_pnl -= abs(target_units - hedge_units) * S0 * (hedge_cost_bps / 1e4)
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
        sig_t = synthetic_iv(close.loc[:dt], vrp_factor=vrp_factor, window=window)
        if (not spec.hold_to_expiry) and dte <= spec.roll_dte:
            exit_prem = sum(bs_price(f, S, K, dte / 365.0, sig_t) for f, K in legs)
            exit_dt, reason = dt, 'roll'; break
        if spec.hedge == 'delta':
            target_units = -sign * net_delta(S, dte / 365.0, sig_t) * MULTIPLIER
            hedge_pnl -= abs(target_units - hedge_units) * S * (hedge_cost_bps / 1e4)
            hedge_units = target_units
    if exit_dt is None:
        dt = fut[:max_hold_days][-1]; S = float(close.loc[dt]); cur = _as_date(dt)
        dte = max((expiry - cur).days, 0)
        sig_t = synthetic_iv(close.loc[:dt], vrp_factor=vrp_factor, window=window)
        exit_prem = (sum(bs_price(f, S, K, max(dte / 365.0, 1e-6), sig_t) for f, K in legs)
                     if dte > 0 else sum(max(0.0, (S - K) if f == 'c' else (K - S)) for f, K in legs))
        exit_dt, reason = dt, 'max_hold'

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
    }


def _price_single_cycle(spec, close: pd.Series, entry_dt, sign: int,
                        vrp_factor: float, window: int, max_hold_days: int) -> dict:
    """Price ONE single-leg cycle from entry_dt forward. Returns a trade dict
    or None if it can't be priced. sign +1 = long the option, -1 = short."""
    idx = close.index
    fut = idx[idx > entry_dt]
    if len(fut) == 0:
        return None
    S0 = float(close.loc[entry_dt])
    sigma0 = synthetic_iv(close.loc[:entry_dt], vrp_factor=vrp_factor, window=window)
    flag = _flag_for(spec.right)
    expiry = nearest_monthly_expiry(_as_date(entry_dt), spec.dte_target)
    t0 = max((expiry - _as_date(entry_dt)).days / 365.0, 1e-6)
    K = _select_strike(spec, S0, t0, sigma0, flag)
    entry_premium = bs_price(flag, S0, K, t0, sigma0)
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
            sig_t = synthetic_iv(close.loc[:dt], vrp_factor=vrp_factor, window=window)
            exit_premium = bs_price(flag, S, K, max(dte / 365.0, 1e-6), sig_t)
            exit_dt, reason = dt, 'roll'
            break
    if exit_dt is None:
        dt = fut[:max_hold_days][-1]
        S = float(close.loc[dt]); cur_date = _as_date(dt)
        dte = max((expiry - cur_date).days, 0)
        sig_t = synthetic_iv(close.loc[:dt], vrp_factor=vrp_factor, window=window)
        exit_premium = (bs_price(flag, S, K, max(dte / 365.0, 1e-6), sig_t)
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
    }


def simulate(instance, close_wide, bars_by_ticker, regimes, start_dt, end_dt, *,
             strategy_id=None, resolver=None, max_hold_days=21,
             vrp_factor=None, window=None):
    from backtest.synthetic_iv import DEFAULT_VRP_FACTOR, DEFAULT_WINDOW
    vrp_factor = DEFAULT_VRP_FACTOR if vrp_factor is None else vrp_factor
    window = DEFAULT_WINDOW if window is None else window

    min_lookback = getattr(instance, 'min_lookback', 20)
    static_universe = list(close_wide.columns)
    oos_dates = close_wide.loc[start_dt:end_dt].index

    trades, days_processed, days_with_signals = [], 0, 0
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
            if spec.structure == 'single':
                cyc = _price_single_cycle(spec, close_wide[ul].dropna(), current_date,
                                          sign, vrp_factor, window, max_hold_days)
            else:
                cyc = _price_multileg_cycle(spec, close_wide[ul].dropna(), current_date,
                                            sign, vrp_factor, window, max_hold_days)
            if cyc is None:
                continue
            cyc.update({'ticker': ul, 'direction': 'long' if sign > 0 else 'short',
                        'entry_regime': str(regime_state)})
            trades.append(cyc)

    return {'trades': trades, 'universe_sizes': [], 'days_processed': days_processed,
            'days_with_signals': days_with_signals, 'static_universe': static_universe,
            'min_lookback': min_lookback}
