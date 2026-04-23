"""
aux_metrics.py
==============
Pre-compute the auxiliary options-chain metrics required by the FundJohn
Top-10 strategies, from the raw `options_eod.parquet` and `prices.parquet`
files that already exist on the VPS at /root/openclaw/data/master/.

This module provides one public function:

    build_opts_map(opts_eod_today, prices_recent, vol_indices_today=None,
                   earnings_today=None) -> Dict[str, dict]

which returns the per-ticker `opts_map[ticker]` dict consumed by every
strategy's `generate_signals(market_data, opts_map)` contract.

It also provides a thin helper

    build_market_data(prices_recent, intraday_30m_recent, vol_indices_recent)
        -> dict

that bundles the cross-sectional / index-level fields the strategies look up
under `market_data[...]`.

Wiring into engine.py
---------------------
Insert at the top of `_load_options_aux()` (or wherever opts_map is built):

    from openclaw.engine_patches.aux_metrics import (
        build_opts_map, build_market_data,
    )

    opts_map = build_opts_map(
        opts_eod_today=opts_eod_today_df,
        prices_recent=prices_lookback_df,
        vol_indices_today=vol_indices_df.iloc[-1] if vol_indices_df is not None else None,
        earnings_today=earnings_df,
    )
    market_data.update(build_market_data(
        prices_recent=prices_lookback_df,
        intraday_30m_recent=intraday_30m_lookback_df,
        vol_indices_recent=vol_indices_df,
    ))

All fields written here match what each strategy reads via opts_map.get(...)
and market_data.get(...) — see top10_strategies_2026/TOP10_README.md for the
field-by-field cross-reference.

Author: Claude / FundJohn research, 2026-04-23.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, Optional

import numpy as np
import pandas as pd


# ─────────────────────────────────────────────────────────────────────────────
#  PER-TICKER OPTIONS METRICS
# ─────────────────────────────────────────────────────────────────────────────

def _atm_pairs(chain: pd.DataFrame, dte_min: int = 10, dte_max: int = 45,
               delta_low: float = 0.40, delta_high: float = 0.60
               ) -> pd.DataFrame:
    """Filter chain to near-ATM call/put pairs at one nearest expiry within DTE band."""
    if chain.empty or 'expiry' not in chain.columns:
        return chain.iloc[0:0]
    today = pd.to_datetime(chain['date'].iloc[0])
    expiry = pd.to_datetime(chain['expiry'])
    dte = (expiry - today).dt.days
    mask = (dte >= dte_min) & (dte <= dte_max)
    sel = chain[mask].copy()
    if sel.empty:
        return sel
    # Pick single nearest-DTE expiry
    sel['dte'] = (pd.to_datetime(sel['expiry']) - today).dt.days
    nearest_dte = sel['dte'].min()
    sel = sel[sel['dte'] == nearest_dte]
    abs_d = sel['delta'].abs()
    sel = sel[(abs_d >= delta_low) & (abs_d <= delta_high)]
    return sel


def call_put_iv_spread_atm_oi_weighted(chain: pd.DataFrame) -> Optional[float]:
    """OI-weighted average of (call_iv − put_iv) across matched ATM strikes.

    Returns None if no matched call/put pairs are available.
    """
    sel = _atm_pairs(chain)
    if sel.empty:
        return None
    calls = sel[sel['option_type'] == 'call'][
        ['strike', 'expiry', 'implied_volatility', 'open_interest']
    ].rename(columns={'implied_volatility': 'iv_call', 'open_interest': 'oi_call'})
    puts = sel[sel['option_type'] == 'put'][
        ['strike', 'expiry', 'implied_volatility', 'open_interest']
    ].rename(columns={'implied_volatility': 'iv_put', 'open_interest': 'oi_put'})
    pairs = calls.merge(puts, on=['strike', 'expiry'], how='inner')
    if pairs.empty:
        return None
    pairs = pairs[(pairs['oi_call'] > 100) & (pairs['oi_put'] > 100)]
    if pairs.empty:
        return None
    w = pairs['oi_call'] + pairs['oi_put']
    return float(((pairs['iv_call'] - pairs['iv_put']) * w).sum() / w.sum())


def smirk_otmput_atmcall(chain: pd.DataFrame,
                         dte_min: int = 20, dte_max: int = 45) -> Optional[float]:
    """Xing-Zhang-Zhao (2010) smirk: IV(OTM put |Δ|≈0.20) − IV(ATM call |Δ|≈0.50)."""
    if chain.empty or 'expiry' not in chain.columns:
        return None
    today = pd.to_datetime(chain['date'].iloc[0])
    expiry = pd.to_datetime(chain['expiry'])
    dte = (expiry - today).dt.days
    mask = (dte >= dte_min) & (dte <= dte_max)
    sel = chain[mask].copy()
    if sel.empty:
        return None
    sel['dte'] = (pd.to_datetime(sel['expiry']) - today).dt.days
    nearest_dte = sel['dte'].min()
    sel = sel[sel['dte'] == nearest_dte]
    # ATM call (|Δ| ∈ [0.45, 0.55])
    atm_call = sel[(sel['option_type'] == 'call') &
                   (sel['delta'].abs().between(0.45, 0.55))]
    if atm_call.empty:
        return None
    iv_atm_call = float(atm_call['implied_volatility'].mean())
    # OTM put (Δ ∈ [-0.30, -0.10])
    otm_put = sel[(sel['option_type'] == 'put') &
                  (sel['delta'].between(-0.30, -0.10))]
    if otm_put.empty:
        return None
    iv_otm_put = float(otm_put['implied_volatility'].mean())
    return iv_otm_put - iv_atm_call


def atm_iv_for_dte(chain: pd.DataFrame, target_dte: int,
                   dte_tolerance: int = 7) -> Optional[float]:
    """Return ATM-call IV for the listed expiry closest to target_dte."""
    if chain.empty or 'expiry' not in chain.columns:
        return None
    today = pd.to_datetime(chain['date'].iloc[0])
    expiry = pd.to_datetime(chain['expiry'])
    dte = (expiry - today).dt.days
    sel = chain[dte.between(target_dte - dte_tolerance, target_dte + dte_tolerance)].copy()
    if sel.empty:
        return None
    sel['dte'] = (pd.to_datetime(sel['expiry']) - today).dt.days
    sel['dte_dist'] = (sel['dte'] - target_dte).abs()
    nearest = sel[sel['dte_dist'] == sel['dte_dist'].min()]
    atm = nearest[(nearest['option_type'] == 'call') &
                  (nearest['delta'].between(0.45, 0.55))]
    if atm.empty:
        # Fall back to near-the-money by absolute moneyness
        atm = nearest[nearest['option_type'] == 'call']
        if atm.empty:
            return None
    return float(atm['implied_volatility'].mean())


def ts_ratio(chain: pd.DataFrame) -> Optional[float]:
    """Term-structure ratio iv_30d / iv_90d (>1 means inverted)."""
    iv30 = atm_iv_for_dte(chain, 30)
    iv90 = atm_iv_for_dte(chain, 90)
    if iv30 is None or iv90 is None or iv90 <= 0:
        return None
    return iv30 / iv90


def atm_bid_ask_spread_pct(chain: pd.DataFrame) -> Optional[float]:
    """Average (ask - bid) / mid for the near-ATM weekly chain.  Used by
    S-HV17 to gate untradable names."""
    sel = _atm_pairs(chain)
    if sel.empty or 'bid' not in sel.columns or 'ask' not in sel.columns:
        return None
    mid = (sel['bid'] + sel['ask']) / 2.0
    spread = (sel['ask'] - sel['bid']) / mid.replace(0, np.nan)
    return float(spread.mean())


# ─────────────────────────────────────────────────────────────────────────────
#  PRICE-DERIVED METRICS (from prices.parquet rolling window)
# ─────────────────────────────────────────────────────────────────────────────

def realised_vol_window(close: pd.Series, window: int = 21,
                        ann: int = 252) -> Optional[float]:
    if close is None or len(close) < window + 1:
        return None
    log_ret = np.log(close).diff().dropna()
    if log_ret.empty:
        return None
    return float(log_ret.tail(window).std(ddof=1) * math.sqrt(ann))


def avg_dollar_volume_30d(close: pd.Series, volume: pd.Series) -> Optional[float]:
    """30-day rolling average dollar volume."""
    if close is None or volume is None or len(close) < 5 or len(volume) < 5:
        return None
    n = min(30, len(close), len(volume))
    return float((close.tail(n) * volume.tail(n)).mean())


def iv_rank_from_history(iv_series: pd.Series, lookback: int = 252) -> Optional[float]:
    """IV rank = where current IV sits in [min, max] of the trailing window
    (0 = bottom, 100 = top)."""
    if iv_series is None or len(iv_series) < 20:
        return None
    win = iv_series.dropna().tail(lookback)
    if win.empty:
        return None
    cur = win.iloc[-1]
    lo, hi = win.min(), win.max()
    if hi - lo <= 0:
        return 50.0
    return float((cur - lo) / (hi - lo) * 100.0)


# ─────────────────────────────────────────────────────────────────────────────
#  PUBLIC ENTRY POINTS
# ─────────────────────────────────────────────────────────────────────────────

def build_opts_map(opts_eod_today: pd.DataFrame,
                   prices_recent: pd.DataFrame,
                   vol_indices_today: Optional[pd.Series] = None,
                   earnings_today: Optional[pd.DataFrame] = None,
                   iv_history: Optional[pd.DataFrame] = None,
                   ) -> Dict[str, dict]:
    """Build the per-ticker `opts_map` dict consumed by all 10 strategies.

    Parameters
    ----------
    opts_eod_today  : full options chain for the current EOD snapshot
    prices_recent   : long-format prices for the trailing 60 sessions or so
    vol_indices_today : optional row containing VIX_close, VVIX_close, VIX9D
    earnings_today  : optional earnings calendar slice for next 7 days
    iv_history      : optional long-format ATM-IV history (ticker, date, iv_30d)
                      used to compute iv_rank
    """
    out: Dict[str, dict] = {}
    if opts_eod_today is None or opts_eod_today.empty:
        return out

    px_groups = (prices_recent.groupby('ticker') if prices_recent is not None
                 else None)
    iv_hist_groups = (iv_history.groupby('ticker') if iv_history is not None
                      else None)
    earn_lookup = {}
    if earnings_today is not None and not earnings_today.empty:
        for _, r in earnings_today.iterrows():
            earn_lookup[r['ticker']] = pd.to_datetime(r['next_earnings_date'])

    for ticker, chain in opts_eod_today.groupby('ticker'):
        # Basic price snapshot
        price_now = None
        rv20 = None
        adv30 = None
        if px_groups is not None and ticker in px_groups.groups:
            tk_px = px_groups.get_group(ticker).sort_values('date')
            if not tk_px.empty:
                price_now = float(tk_px['close'].iloc[-1])
                rv20 = realised_vol_window(tk_px['close'], 21)
                adv30 = avg_dollar_volume_30d(tk_px['close'], tk_px['volume'])

        # Options metrics
        iv_spread = call_put_iv_spread_atm_oi_weighted(chain)
        smirk = smirk_otmput_atmcall(chain)
        iv30 = atm_iv_for_dte(chain, 30)
        iv90 = atm_iv_for_dte(chain, 90)
        ts = (iv30 / iv90) if (iv30 and iv90 and iv90 > 0) else None
        ba_spread = atm_bid_ask_spread_pct(chain)

        # IV rank from history (if available)
        iv_rank = None
        if iv_hist_groups is not None and ticker in iv_hist_groups.groups:
            tk_iv = iv_hist_groups.get_group(ticker).sort_values('date')
            iv_rank = iv_rank_from_history(tk_iv['iv_30d'])

        # Earnings fields (S-HV17)
        next_earn = earn_lookup.get(ticker)
        earnings_implied_move = None
        hist_post_earnings_move = None
        if next_earn is not None and iv30 is not None and price_now is not None:
            today = pd.to_datetime(chain['date'].iloc[0])
            days_to_event = max(1, (next_earn - today).days)
            tau = days_to_event / 365.0
            # Implied 1-day move from ATM straddle approximation:
            #   straddle ≈ 0.8 × S × IV × √τ_year   (Black-Scholes ATM convention)
            #   implied move on event day ≈ straddle / S  (scaled by event vol weight)
            earnings_implied_move = 0.8 * iv30 * math.sqrt(tau)
            # Use trailing realised vol scaled to a single day as a hist proxy
            if rv20 is not None and rv20 > 0:
                hist_post_earnings_move = rv20 / math.sqrt(252) * 1.5  # 1.5σ heuristic

        out[ticker] = {
            'last_price': price_now,
            'iv_spread_atm_oi_weighted': iv_spread,
            'iv_spread': iv_spread,
            'smirk_otmput_atmcall': smirk,
            'iv_30d': iv30,
            'iv_90d': iv90,
            'ts_ratio': ts,
            'atm_iv_30d': iv30,
            'atm_bid_ask_spread_pct': ba_spread,
            'iv_rank': iv_rank,
            'rv20': rv20,
            'avg_dollar_volume_30d': adv30,
            'next_earnings_date': next_earn.date() if next_earn is not None else None,
            'earnings_implied_move': earnings_implied_move,
            'hist_post_earnings_move': hist_post_earnings_move,
        }
    return out


def build_market_data(prices_recent: Optional[pd.DataFrame] = None,
                      intraday_30m_recent: Optional[pd.DataFrame] = None,
                      vol_indices_recent: Optional[pd.DataFrame] = None,
                      spx_options_today: Optional[pd.DataFrame] = None,
                      ) -> dict:
    """Bundle index-level / cross-sectional fields strategies expect under
    market_data[...].

    Strategies and the fields they look up:

        S-TR-01  vol_indices['vix_close'], ['vvix_close'], ['vix9d_close']
        S-TR-02  spy_close_history (>=120 sessions)
        S-TR-03  spy_close_history (>=200 sessions)
        S-TR-04  spy_30m_bars (today), spy_prev_close, spy_10d_volume_avg, vix_close
        S-TR-06  intraday_30m_bars (today, all liquid universe)
        S-HV20   spx_iv_30d, spx_close, prices (for component IV stats)
    """
    md: dict = {}

    if vol_indices_recent is not None and not vol_indices_recent.empty:
        last = vol_indices_recent.iloc[-1]
        md['vix_close'] = float(last.get('vix_close', 20.0))
        md['vvix_close'] = float(last.get('vvix_close', 90.0))
        md['vix9d_close'] = float(last.get('vix9d_close', md['vix_close']))
        md['vix_history'] = list(vol_indices_recent['vix_close'].astype(float))
        md['vvix_history'] = list(vol_indices_recent['vvix_close'].astype(float))

    if prices_recent is not None and not prices_recent.empty:
        spy = prices_recent[prices_recent['ticker'] == 'SPY'].sort_values('date')
        if not spy.empty:
            md['spy_close_history'] = list(spy['close'].astype(float))
            md['spy_prev_close'] = float(spy['close'].iloc[-1])
            md['spx_close'] = float(spy['close'].iloc[-1])

    if intraday_30m_recent is not None and not intraday_30m_recent.empty:
        # Today's bars only
        intraday_30m_recent = intraday_30m_recent.copy()
        intraday_30m_recent['datetime'] = pd.to_datetime(intraday_30m_recent['datetime'])
        intraday_30m_recent['date'] = intraday_30m_recent['datetime'].dt.date
        last_date = intraday_30m_recent['date'].max()
        today_bars = intraday_30m_recent[intraday_30m_recent['date'] == last_date]
        if not today_bars.empty:
            md['intraday_30m_bars'] = {}
            for tk, grp in today_bars.groupby('ticker'):
                md['intraday_30m_bars'][tk] = grp.sort_values('datetime').to_dict('records')
            spy_bars = today_bars[today_bars['ticker'] == 'SPY'].sort_values('datetime')
            if not spy_bars.empty:
                md['spy_30m_bars'] = spy_bars.to_dict('records')

        # 10-day average first-bar volume (S-TR-04)
        first_bars = intraday_30m_recent[
            intraday_30m_recent['datetime'].dt.time == pd.Timestamp('09:30').time()
        ]
        if not first_bars.empty:
            spy_first = first_bars[first_bars['ticker'] == 'SPY']
            if not spy_first.empty and len(spy_first) >= 5:
                md['spy_10d_volume_avg'] = float(
                    spy_first['volume'].tail(10).mean()
                )

    if spx_options_today is not None and not spx_options_today.empty:
        spx_chain = spx_options_today[spx_options_today['ticker'] == 'SPX']
        spx_iv = atm_iv_for_dte(spx_chain, 30) if not spx_chain.empty else None
        if spx_iv is not None:
            md['spx_iv_30d'] = spx_iv

    return md


# ─────────────────────────────────────────────────────────────────────────────
#  ENGINE PATCH SNIPPET — paste into /root/openclaw/src/engine.py
# ─────────────────────────────────────────────────────────────────────────────

ENGINE_PATCH_SNIPPET = '''
# ── Top-10 strategy aux fields ───────────────────────────────────────────────
# Insert this block inside engine.py wherever opts_map / market_data are
# built (typically inside _load_options_aux() and _load_market_data()).

from openclaw.engine_patches.aux_metrics import (
    build_opts_map, build_market_data,
)

# Lookback windows for each ancillary metric
_PRICES_LOOKBACK_DAYS = 60          # rv20, adv30
_IV_HISTORY_LOOKBACK_DAYS = 252     # iv_rank
_INTRADAY_30M_LOOKBACK_DAYS = 14    # spy_10d_volume_avg + intraday_30m_bars

opts_map = build_opts_map(
    opts_eod_today=opts_eod_df_for_today,           # filtered to today's date
    prices_recent=prices_lookback_df,               # 60-day window
    vol_indices_today=vol_indices_df.iloc[-1] if vol_indices_df is not None else None,
    earnings_today=earnings_calendar_df,            # next 7 days
    iv_history=iv_history_df,                       # optional, for iv_rank
)

market_data.update(build_market_data(
    prices_recent=prices_lookback_df,
    intraday_30m_recent=intraday_30m_lookback_df,
    vol_indices_recent=vol_indices_df,
    spx_options_today=spx_options_today_df,
))
# ─────────────────────────────────────────────────────────────────────────────
'''
