"""
Intraday First Half-Hour Momentum (ETP) — QuantSeeker 2026.

Hypothesis: The first 30-minute return of SPY/QQQ predicts the last 30-minute
return of the same session. When the opening half-hour closes positive, go LONG
at 15:30 ET and exit at the 16:00 ET close.

Source: https://www.quantseeker.com/p/revisiting-intraday-momentum
Extends Lou et al. / Gao et al. intraday momentum study on SPY and QQQ.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['IntradayFirstHalfHourMomentumEtf']

INSTRUMENT_CLASS = 'etp'

# Fixed ETP basket — intraday momentum is documented on SPY and QQQ only.
BASKET = ('SPY', 'QQQ')

# 30-minute bar index offsets within a regular session (9:30–16:00 ET).
# Bar 0 = 9:30–10:00 (first half-hour), bar 12 = 15:30–16:00 (last half-hour).
# The prices_30m DataFrame is expected to carry a MultiIndex or a column set
# keyed by ticker, with DatetimeIndex timestamps in ET.
FIRST_BAR_HOUR  = 9
FIRST_BAR_MIN   = 30
LAST_BAR_HOUR   = 15
LAST_BAR_MIN    = 30


class IntradayFirstHalfHourMomentumEtf(BaseStrategy):
    """First 30-minute return of SPY/QQQ predicts last 30-minute return → LONG at 15:30 ET."""

    id                = 'S_intraday_first_half_hour_momentum_etf'
    name              = 'Intraday First Half-Hour Momentum (ETP)'
    description       = (
        'If SPY or QQQ first 30-minute return > 0, go LONG at 15:30 ET exit at 16:00 ET.'
    )
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 1
    # Strategy works in all regimes — intraday momentum is regime-agnostic per source.
    # (NEUTRAL is not canonical; omitted. CRISIS included but position_scale heavily discounts.)
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    MAX_SIGNALS = 2

    def default_parameters(self) -> dict:
        return {
            'pos_size_frac': 0.08,    # base fraction per ticker (pre-regime scale)
            'min_first_bar_ret': 0.0, # threshold for first-bar return to be positive
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        """Emit LONG signals for basket members whose first 30m return > 0 today."""
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        scale    = self.position_scale(regime_state)
        pos_size = round(self.parameters.get('pos_size_frac', 0.08) * scale, 4)
        min_ret  = self.parameters.get('min_first_bar_ret', 0.0)

        # Retrieve intraday 30-minute bars from aux_data.
        prices_30m = None
        if aux_data:
            prices_30m = aux_data.get('prices_30m')

        signals: List[Signal] = []

        for ticker in BASKET:
            # Fall back to daily close prices if no intraday data is available.
            if prices_30m is not None and not prices_30m.empty:
                first_bar_ret = self._compute_first_bar_return(prices_30m, ticker)
                entry_price   = self._last_close(prices, ticker)
            else:
                # Graceful degradation: use daily prices to approximate.
                # first_bar_ret proxy = today's open-to-close return.
                first_bar_ret = self._daily_return_proxy(prices, ticker)
                entry_price   = self._last_close(prices, ticker)

            if first_bar_ret is None or entry_price is None:
                continue

            if first_bar_ret <= min_ret:
                # Signal condition not met — skip this ticker.
                continue

            # Compute stops/targets using the daily price series for ATR.
            ticker_series = prices[ticker].dropna() if ticker in prices.columns else pd.Series(dtype=float)
            if len(ticker_series) < 14:
                continue

            stops = self.compute_stops_and_targets(
                ticker_series,
                direction='LONG',
                current_price=entry_price,
                regime_state=regime_state,
            )

            confidence = 'HIGH' if first_bar_ret > 0.005 else 'MED'

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = entry_price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'first_bar_ret': round(float(first_bar_ret), 5),
                    'regime':        regime_state,
                    'scale':         scale,
                    'entry_time':    '15:30 ET',
                    'exit_time':     '16:00 ET',
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    # ── helpers ────────────────────────────────────────────────────────────────

    def _compute_first_bar_return(self, prices_30m: pd.DataFrame, ticker: str):
        """Extract today's first 30-minute return (9:30→10:00) from intraday bars.

        prices_30m may be a wide DataFrame (columns = tickers, index = Timestamp)
        or a long DataFrame with a 'ticker' column. We handle the wide case.
        Returns float or None.
        """
        try:
            if ticker not in prices_30m.columns:
                return None
            series = prices_30m[ticker].dropna()
            if series.empty:
                return None
            idx = series.index
            # Filter to the most recent trading session.
            today = idx[-1].normalize()
            today_bars = series[idx.normalize() == today]
            if len(today_bars) < 2:
                return None
            # First bar: first timestamp of the session (9:30 ET).
            open_price  = float(today_bars.iloc[0])
            close_first = float(today_bars.iloc[1])  # price at start of bar 2 ≈ end of bar 1
            if open_price == 0:
                return None
            return (close_first - open_price) / open_price
        except Exception:
            return None

    def _daily_return_proxy(self, prices: pd.DataFrame, ticker: str):
        """Return today's daily close-to-close return as a proxy for first-bar return."""
        try:
            if ticker not in prices.columns:
                return None
            series = prices[ticker].dropna()
            if len(series) < 2:
                return None
            return float((series.iloc[-1] - series.iloc[-2]) / series.iloc[-2])
        except Exception:
            return None

    def _last_close(self, prices: pd.DataFrame, ticker: str):
        """Return the most recent close price for ticker."""
        try:
            if ticker not in prices.columns:
                return None
            series = prices[ticker].dropna()
            if series.empty:
                return None
            return float(series.iloc[-1])
        except Exception:
            return None
