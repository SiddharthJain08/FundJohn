from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

__all__ = ['IntradaySPYMomentum']

_SPY_PROXIES = ['SPY', 'IVV', 'VOO']

# Zarattini noise boundary (Zarattini & Staley 2023)
NOISE_N        = 1.0     # multiplier on σ_14d for noise boundary
SIGMA_WINDOW   = 14      # days to compute avg absolute intraday return
MIN_DAYS       = 20      # minimum history needed

# VWAP exit signal (S-TR-05 merged): positions that crossed VWAP back
# are considered "stopped out" — reduce confidence
RTH_START_UTC  = '13:30'  # 09:30 ET in UTC
RTH_END_UTC    = '20:00'  # 16:00 ET in UTC


def _filter_rth(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only regular trading hours (09:30–16:00 ET, which is 13:30–20:00 UTC)."""
    t = pd.to_datetime(df['datetime'], utc=True).dt.strftime('%H:%M')
    return df[(t >= RTH_START_UTC) & (t <= RTH_END_UTC)].copy()


def _intraday_stats(bars_30m: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """
    Per-day intraday stats: open, close, intraday_return, abs_return.
    Uses only RTH bars; open = first RTH bar open, close = last RTH bar close.
    """
    sub = bars_30m[bars_30m['ticker'] == ticker].copy()
    if sub.empty:
        return pd.DataFrame()
    sub = _filter_rth(sub)
    if sub.empty:
        return pd.DataFrame()
    sub = sub.sort_values('datetime')
    grouped = sub.groupby('date')
    stats = []
    for d, g in grouped:
        day_open  = float(g['open'].iloc[0])
        day_close = float(g['close'].iloc[-1])
        day_vwap  = float(g['vwap'].mean())
        intraday_ret = (day_close - day_open) / day_open if day_open > 0 else 0.0
        stats.append({
            'date':          d,
            'open':          day_open,
            'close':         day_close,
            'vwap':          day_vwap,
            'intraday_ret':  intraday_ret,
            'abs_ret':       abs(intraday_ret),
        })
    return pd.DataFrame(stats).sort_values('date').reset_index(drop=True)


def _zarattini_signal(day_stats: pd.DataFrame, bars_today: pd.DataFrame) -> dict:
    """
    Compute today's Zarattini signal:
    1. σ_14d = mean(abs_ret[-14:]) — avg absolute intraday return
    2. noise_upper = today_open × (1 + NOISE_N × σ_14d)
       noise_lower = today_open × (1 - NOISE_N × σ_14d)
    3. today_close vs noise bounds
    4. VWAP: did price stay above/below intraday VWAP after break?

    Returns dict with keys: direction, confidence, params, fired
    """
    if len(day_stats) < MIN_DAYS:
        return {'fired': False}

    # σ_14d from history (exclude today)
    sigma_14d = float(day_stats['abs_ret'].iloc[-SIGMA_WINDOW:].mean())
    if sigma_14d <= 0:
        return {'fired': False}

    latest       = day_stats.iloc[-1]
    today_open   = float(latest['open'])
    today_close  = float(latest['close'])
    today_vwap   = float(latest['vwap'])

    noise_upper = today_open * (1.0 + NOISE_N * sigma_14d)
    noise_lower = today_open * (1.0 - NOISE_N * sigma_14d)

    # Check boundary break
    broke_upper = today_close > noise_upper
    broke_lower = today_close < noise_lower

    if not broke_upper and not broke_lower:
        return {'fired': False, 'reason': 'within_noise_band',
                'close': today_close, 'upper': noise_upper, 'lower': noise_lower}

    direction = 'LONG' if broke_upper else 'SHORT'

    # VWAP filter (S-TR-05 merged):
    # Count VWAP crossbacks in today's RTH bars
    rth_today = _filter_rth(bars_today) if bars_today is not None and not bars_today.empty else pd.DataFrame()
    vwap_crossbacks = 0
    if not rth_today.empty:
        rth_today = rth_today.sort_values('datetime')
        cum_vwap_list = rth_today['vwap'].values
        closes = rth_today['close'].values
        # Count sign changes in (close - cum_vwap)
        diffs = closes - cum_vwap_list
        sign_changes = int(np.sum(np.diff(np.sign(diffs)) != 0))
        vwap_crossbacks = sign_changes

    # Confirm: close is on the right side of VWAP
    if direction == 'LONG' and today_close < today_vwap:
        return {'fired': False, 'reason': 'vwap_crossback_long'}
    if direction == 'SHORT' and today_close > today_vwap:
        return {'fired': False, 'reason': 'vwap_crossback_short'}

    # Confidence: fewer VWAP crossbacks + stronger boundary break = higher confidence
    break_strength = abs(today_close - (noise_upper if broke_upper else noise_lower)) / today_open
    confidence = 'HIGH' if (vwap_crossbacks <= 1 and break_strength > sigma_14d * 0.3) else (
                 'MED'  if vwap_crossbacks <= 2 else 'LOW')

    return {
        'fired':           True,
        'direction':       direction,
        'confidence':      confidence,
        'today_open':      round(today_open, 4),
        'today_close':     round(today_close, 4),
        'today_vwap':      round(today_vwap, 4),
        'noise_upper':     round(noise_upper, 4),
        'noise_lower':     round(noise_lower, 4),
        'sigma_14d':       round(sigma_14d, 6),
        'vwap_crossbacks': vwap_crossbacks,
        'break_strength':  round(break_strength, 6),
    }


class IntradaySPYMomentum(BaseStrategy):
    """
    Zarattini intraday SPY momentum signal (Zarattini & Staley 2023).
    S-TR-05 VWAP exit logic merged in as a confidence filter.

    EOD signal: if today's SPY broke the noise boundary (open ± N×σ_14d)
    AND did not cross VWAP back → generate LONG/SHORT for next-day open
    momentum continuation.

    Noise boundary: open × (1 ± 1.0 × σ_14d) where σ_14d = 14-day avg
    absolute intraday return. VWAP crossback reduces confidence.

    Requires aux_data['prices_30m'] loaded from prices_30m.parquet.
    """

    id                = 'S_tr_04_intraday_spy_momentum'
    name              = 'IntradaySPYMomentum'
    description       = 'Zarattini noise-boundary intraday momentum — next-open continuation (S-TR-05 VWAP exit merged)'
    tier              = 1
    active_in_regimes = ['TRANSITIONING']
    min_lookback      = MIN_DAYS + 5
    BASE_SIZE_PCT     = 0.030  # SPY-only, higher concentration OK

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        bars_30m: pd.DataFrame | None = (aux_data or {}).get('prices_30m')
        if bars_30m is None or bars_30m.empty:
            print(f'[debug] {self.id}: signals=0 (prices_30m not loaded)', file=sys.stderr)
            return []

        # Find SPY proxy
        spy_ticker = next((t for t in _SPY_PROXIES if t in universe and t in prices.columns), None)
        if spy_ticker is None:
            print(f'[debug] {self.id}: signals=0 (no SPY proxy)', file=sys.stderr)
            return []

        # Build daily intraday stats from 30m bars
        spy_bars = bars_30m[bars_30m['ticker'] == spy_ticker].copy()
        if spy_bars.empty:
            print(f'[debug] {self.id}: signals=0 (no 30m bars for {spy_ticker})', file=sys.stderr)
            return []

        day_stats = _intraday_stats(spy_bars, spy_ticker)
        if len(day_stats) < self.min_lookback:
            print(f'[debug] {self.id}: signals=0 (need {self.min_lookback} days, got {len(day_stats)})', file=sys.stderr)
            return []

        # Today's 30-min bars for VWAP analysis
        latest_date  = day_stats['date'].iloc[-1]
        bars_today   = spy_bars[spy_bars['date'] == latest_date]

        result = _zarattini_signal(day_stats, bars_today)

        if not result.get('fired'):
            reason = result.get('reason', 'within_noise_band')
            print(f'[debug] {self.id}: signals=0 ({reason})', file=sys.stderr)
            return []

        direction  = result['direction']
        confidence = result['confidence']
        spy_price  = float(prices[spy_ticker].iloc[-1])
        if spy_price <= 0:
            return []

        scale = self.position_scale(regime_state)
        size  = float(self.BASE_SIZE_PCT * scale)
        if confidence == 'LOW':
            size *= 0.5
        size = max(0.005, min(size, 0.08))

        st = self.compute_stops_and_targets(
            prices[spy_ticker].dropna(), direction, spy_price, regime_state=regime_state,
        )
        signal = Signal(
            ticker            = spy_ticker,
            direction         = direction,
            entry_price       = round(spy_price, 4),
            stop_loss         = st['stop'],
            target_1          = st['t1'],
            target_2          = st['t2'],
            target_3          = st['t3'],
            position_size_pct = size,
            confidence        = confidence,
            signal_params     = {
                'sigma_14d':       result['sigma_14d'],
                'noise_upper':     result['noise_upper'],
                'noise_lower':     result['noise_lower'],
                'vwap_crossbacks': result['vwap_crossbacks'],
                'break_strength':  result['break_strength'],
                'today_vwap':      result['today_vwap'],
                'signal_type':     'zarattini_intraday',
            },
        )

        print(f'[debug] {self.id}: signals=1 dir={direction} conf={confidence} '
              f'break={result["break_strength"]:.4f}', file=sys.stderr)
        return [signal]
