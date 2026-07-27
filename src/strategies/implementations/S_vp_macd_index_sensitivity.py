"""
S_vp_macd_index_sensitivity — VP-MACD with λ Sensitivity Calibration for U.S. Index ETFs

Replacing the standard MACD price input with a volume-volatility-intraday-structure-weighted
price (P*) and a calibrated λ sensitivity parameter reduces signal lag and false entries,
producing superior risk-adjusted returns on SPY/QQQ/DIA (Lin et al. 2026).
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from functools import lru_cache
from pathlib import Path
from typing import List, Tuple
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['VPMACDIndexSensitivity']

# Fixed universe: U.S. equity index ETFs the paper studies
_UNIVERSE = ['SPY', 'QQQ', 'DIA']

# Per-ticker λ sensitivity midpoints from paper §3.3 grid search
_LAMBDA = {'SPY': 0.89, 'QQQ': 0.91, 'DIA': 0.89}
_LAMBDA_DEFAULT = 0.89

# EMA spans
_FAST, _SLOW, _SIG = 12, 26, 9
_MIN_BARS = _SLOW + _SIG + 10    # minimum rows needed to compute signal

# Base position size per ticker (3 tickers, ~30% each with regime scale)
_BASE_SIZE = 0.30

# Path to master prices parquet
_PRICES_PATH = Path(__file__).parents[3] / 'data' / 'master' / 'prices.parquet'


def _ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def _compute_vp_macd(ohlcv: pd.DataFrame) -> pd.DataFrame:
    """
    Compute P*, VP-MACD, and signal line for a single-ticker OHLCV DataFrame.

    P*_t = close_t * σ_t * r_t  (per-bar adjusted price; volume cancels single-bar sum)
      σ_t = (high_t - low_t) / close_t          # normalized daily range
      r_t = |close_t - open_t| / (high_t - low_t)  # intraday directional strength
    """
    hi = ohlcv['high']
    lo = ohlcv['low']
    cl = ohlcv['close']
    op = ohlcv['open']

    hl_range = (hi - lo).clip(lower=1e-8)
    sigma = hl_range / cl.clip(lower=1e-8)
    r = (cl - op).abs() / hl_range

    p_star = cl * sigma * r

    vp_macd = _ema(p_star, _FAST) - _ema(p_star, _SLOW)
    signal_line = _ema(vp_macd, _SIG)

    return pd.DataFrame({'vp_macd': vp_macd, 'signal': signal_line}, index=ohlcv.index)


@lru_cache(maxsize=4)
def _load_ohlcv_cached(tickers: Tuple[str, ...]) -> dict:
    """Load OHLCV from master parquet; returns {ticker: DataFrame(date, open, high, low, close)}.

    Memoised + predicate-pushed-down (2026-07-25). generate_signals calls this once
    per BAR, and the old bare full-parquet read re-materialised all 18.7M rows of the
    master panel every bar — anon-rss 4.16GB, global OOM-kill (rc=137) on the 8GB box
    even with the box otherwise idle. `filters=` restricts the read to this strategy's
    3 index ETFs at the row-group level, and lru_cache makes it ONE read per process
    instead of ~2,600. Pure performance change: same rows, same dtypes, same order.
    See reference_vps_two_core_cpu — never load the whole parquet.
    """
    if not _PRICES_PATH.exists():
        return {}
    try:
        df = pd.read_parquet(
            _PRICES_PATH,
            columns=['ticker', 'date', 'open', 'high', 'low', 'close'],
            filters=[('ticker', 'in', list(tickers))],
        )
        df = df[df['ticker'].isin(tickers)].copy()
        df['date'] = pd.to_datetime(df['date'])
        df.sort_values('date', inplace=True)
        result = {}
        for tkr, grp in df.groupby('ticker'):
            result[tkr] = grp.set_index('date')[['open', 'high', 'low', 'close']]
        return result
    except Exception as exc:
        print(f'[debug] S_vp_macd_index_sensitivity: OHLCV load failed: {exc}', file=sys.stderr)
        return {}


def _load_ohlcv(tickers: List[str]) -> dict:
    return _load_ohlcv_cached(tuple(sorted(tickers)))


class VPMACDIndexSensitivity(BaseStrategy):
    """VP-MACD with λ sensitivity calibration for U.S. index ETFs (Lin et al. 2026)."""

    id          = 'S_vp_macd_index_sensitivity'
    name        = 'VPMACDIndexSensitivity'
    description = (
        'VP-MACD: volume-volatility-intraday-weighted price input with λ-adjusted '
        'crossover threshold for SPY/QQQ/DIA (Lin et al. 2026)'
    )
    tier        = 2
    min_lookback = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

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

        scale = self.position_scale(regime_state)

        # Point-in-time anchor (2026-07-25). `prices` is truncated to the bar the
        # harness is simulating, but the master parquet below carries FULL history
        # through today. Without this cutoff, _compute_vp_macd's .iloc[-1]/.iloc[-2]
        # read the LATEST real-world bars regardless of which bar is being simulated
        # — a look-ahead that made every backtest bar evaluate the same crossover
        # (observable as an identical `signals=` line on every bar) while filling at
        # historical prices. NO-OP in live: there prices.index[-1] IS the last bar.
        as_of = prices.index[-1]

        # Load OHLCV from master parquet (needed for P* computation)
        ohlcv_map = _load_ohlcv(_UNIVERSE)
        if not ohlcv_map:
            print(f'[debug] signals=0 (OHLCV unavailable)', file=sys.stderr)
            return []

        signals: List[Signal] = []

        for ticker in _UNIVERSE:
            if ticker not in ohlcv_map:
                continue

            ohlcv = ohlcv_map[ticker]
            ohlcv = ohlcv[ohlcv.index <= as_of].dropna()
            if len(ohlcv) < _MIN_BARS:
                continue

            # Compute VP-MACD indicators
            ind = _compute_vp_macd(ohlcv)

            if len(ind) < 2:
                continue

            vp_macd_prev  = float(ind['vp_macd'].iloc[-2])
            signal_prev   = float(ind['signal'].iloc[-2])
            vp_macd_curr  = float(ind['vp_macd'].iloc[-1])
            signal_curr   = float(ind['signal'].iloc[-1])

            lam = _LAMBDA.get(ticker, _LAMBDA_DEFAULT)

            # λ-adjusted BUY crossover (paper §3.3)
            buy_signal = (vp_macd_prev <= lam * signal_prev) and (vp_macd_curr > lam * signal_curr)
            # SELL crossover (symmetric, no λ on exit per paper)
            sell_signal = (vp_macd_prev >= signal_prev) and (vp_macd_curr < signal_curr)

            if not buy_signal:
                # Emit FLAT on sell or no signal (position management)
                if sell_signal and ticker in prices.columns:
                    close_price = float(prices[ticker].dropna().iloc[-1])
                    signals.append(Signal(
                        ticker=ticker,
                        direction='FLAT',
                        entry_price=close_price,
                        stop_loss=close_price,
                        target_1=close_price,
                        target_2=close_price,
                        target_3=close_price,
                        position_size_pct=0.0,
                        confidence='MED',
                        signal_params={'lambda': lam, 'vp_macd': round(vp_macd_curr, 6)},
                    ))
                continue

            if ticker not in prices.columns:
                continue

            close_series = prices[ticker].dropna()
            if close_series.empty:
                continue

            current_price = float(close_series.iloc[-1])
            if current_price <= 0:
                continue

            # Confidence from MACD histogram momentum
            hist = vp_macd_curr - lam * signal_curr
            hist_prev = vp_macd_prev - lam * signal_prev
            if abs(hist) > abs(hist_prev) * 1.5:
                confidence = 'HIGH'
            elif abs(hist) > abs(hist_prev):
                confidence = 'MED'
            else:
                confidence = 'LOW'

            size = float(min(_BASE_SIZE * scale, 0.35))
            if confidence == 'LOW':
                size *= 0.5

            stops = self.compute_stops_and_targets(
                close_series, 'LONG', current_price, regime_state=regime_state
            )

            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=float(current_price),
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=size,
                confidence=confidence,
                signal_params={
                    'lambda': lam,
                    'vp_macd': round(vp_macd_curr, 6),
                    'signal_line': round(signal_curr, 6),
                    'histogram': round(hist, 6),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
