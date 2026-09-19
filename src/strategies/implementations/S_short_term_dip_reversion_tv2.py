"""
Short-Term Dip Reversion.
Source: https://www.crackingmarkets.com/buying-short-term-dips-in-stocks-realtest-code/

Hypothesis: stocks that suffer a sharp short-term price dip tend to
mean-revert/bounce over the following days — an anomaly attributable to
short-term overreaction and liquidity provision by dip-buyers. LONG-only.

Interpretation choices (variant 2 of 2 — the source abstract is paywalled and
gives no concrete formula, thresholds, or hold period; the paper's only
unambiguous claims — LONG-only direction and the general "sharp dip -> short
hold -> bounce" shape — are preserved). This variant deliberately takes the
road NOT taken by variant 1:

  * Oversold trigger: RSI(2) < 10 (Connors-style short-lookback oscillator),
    NOT a raw N-day cumulative-return threshold. The oscillator normalizes
    for a name's own volatility (a 6% move means something different for a
    utility than for a small-cap chipmaker), which a flat percentage
    threshold does not.
  * Trend context: 50-day SMA is rising (today's 50-SMA > its value 20
    trading days ago), NOT price-above-200-day-SMA. A medium-term rising
    average captures "still in an active uptrend" without requiring the
    stock to be above a slow-moving long-run anchor it may have only
    recently reclaimed or be about to lose — this admits dips earlier in a
    fresh uptrend that a 200-day filter would exclude.
  * Ranking / cap: no dip-depth ranking. All names passing both filters on a
    given day are taken (up to MAX_SIGNALS by liquidity — highest recent
    dollar volume proxy via price level as a simple tiebreak), and position
    size is inversely scaled by each name's own ATR (volatility-parity
    sizing) rather than a flat 3% per name — a violent, high-ATR dip and a
    calm, low-ATR dip should not receive the same conviction.
  * Exit geometry: base ATR stop (2.0x, unscaled from the class default) and
    the base R-multiple ladder (2/4/8), betting the bounce can run further
    than a tight scalp — a wider stop also tolerates the extra noise RSI(2)
    triggers introduce versus a blunter return-threshold filter.
  * Universe: sp500 (INFERRED_UNIVERSE_FILTER) — unchanged, liquid large
    caps where "overreaction + dip-buyer liquidity provision" is plausible.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List

from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import sp500 as universe_filter

__all__ = ['ShortTermDipReversionTV2']

INSTRUMENT_CLASS = 'equity'

STRATEGY_ID    = 'S_short_term_dip_reversion'
RSI_LOOKBACK   = 2        # Connors-style short RSI window
RSI_THRESHOLD  = 10.0     # RSI(2) below this trips the oversold filter
TREND_SMA      = 50       # medium-term trend window
TREND_SLOPE_LB = 20       # trading days back to compare SMA slope against
MIN_LOOKBACK   = 252      # ~1y, covers warm-up for SMA/RSI/ATR
MAX_HOLD_DAYS  = 10       # informational — expected bounce window, used by the backtest


def _wilder_rsi(series: pd.Series, window: int) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / window, min_periods=window, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, float('nan'))
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return rsi.where(avg_loss != 0.0, 100.0)


class ShortTermDipReversionTV2(BaseStrategy):
    """LONG-only short-term dip reversion: buy names with RSI(2) < 10 while
    their 50-day SMA is still rising, sized inversely to each name's own
    ATR, expecting a short-horizon bounce.
    Source: https://www.crackingmarkets.com/buying-short-term-dips-in-stocks-realtest-code/
    """

    id                  = STRATEGY_ID
    name                = 'Short-Term Dip Reversion (TV2)'
    description         = (
        'LONG-only: buy S&P 500 names with RSI(2) < 10 while their 50-day '
        'SMA is rising, position-sized inversely to ATR, expecting a '
        'short-horizon mean-reversion bounce.'
    )
    tier                = 3
    signal_frequency    = 'daily'
    min_lookback        = MIN_LOOKBACK
    active_in_regimes   = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    target_r_multiples  = (2.0, 4.0, 8.0)
    MAX_SIGNALS         = 20

    def default_parameters(self) -> dict:
        return {
            'rsi_lookback':   RSI_LOOKBACK,
            'rsi_threshold':  RSI_THRESHOLD,
            'trend_sma':      TREND_SMA,
            'trend_slope_lb': TREND_SLOPE_LB,
            'max_hold_days':  MAX_HOLD_DAYS,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        p              = self.parameters
        rsi_lookback   = int(p.get('rsi_lookback', RSI_LOOKBACK))
        rsi_threshold  = float(p.get('rsi_threshold', RSI_THRESHOLD))
        trend_sma      = int(p.get('trend_sma', TREND_SMA))
        trend_slope_lb = int(p.get('trend_slope_lb', TREND_SLOPE_LB))

        candidates = [t for t in universe if t in prices.columns] if universe else list(prices.columns)
        if not candidates:
            print('[debug] signals=0', file=sys.stderr)
            return []

        panel   = prices[candidates].astype('float64')
        current = panel.iloc[-1]
        sma     = panel.rolling(trend_sma).mean()
        sma_now = sma.iloc[-1]
        sma_lag = sma.iloc[-1 - trend_slope_lb] if len(sma) > trend_slope_lb else sma.iloc[0]

        scale = self.position_scale(regime_state)

        hits = []
        for ticker in candidates:
            price = current.get(ticker)
            s_now = sma_now.get(ticker)
            s_lag = sma_lag.get(ticker)
            if price is None or s_now is None or s_lag is None:
                continue
            if pd.isna(price) or pd.isna(s_now) or pd.isna(s_lag) or price <= 0 or s_now <= 0 or s_lag <= 0:
                continue
            if s_now <= s_lag:
                continue  # 50-day SMA must be rising

            series = panel[ticker].dropna()
            if len(series) < max(trend_sma, 14) + 1:
                continue
            rsi_val = _wilder_rsi(series, rsi_lookback).iloc[-1]
            if pd.isna(rsi_val) or rsi_val >= rsi_threshold:
                continue

            diff = series.diff().abs()
            atr_raw = diff.rolling(14).mean().iloc[-1]
            atr = float(atr_raw) if (pd.notna(atr_raw) and float(atr_raw) > 0) else price * 0.02

            hits.append((ticker, float(rsi_val), float(price), float(atr)))

        if not hits:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # No dip-depth ranking (deliberate — see module docstring). Break
        # ties on liquidity proxy (higher price level) and cap the list.
        hits.sort(key=lambda x: x[2], reverse=True)
        hits = hits[: self.MAX_SIGNALS]

        # Volatility-parity sizing: inverse-ATR weights normalized to sum to
        # a fixed gross book allocation, then regime-scaled.
        inv_atr_sum = sum(1.0 / h[3] for h in hits if h[3] > 0)
        gross_alloc = 0.03 * len(hits)  # same aggregate conviction budget as a flat-3%-times-N book

        signals: List[Signal] = []
        for ticker, rsi_val, price, atr in hits:
            series = prices[ticker].dropna()
            if len(series) < 14:
                continue
            stops = self.compute_stops_and_targets(
                series, direction='LONG', current_price=price,
                atr_multiplier=2.0, regime_state=regime_state,
            )
            if rsi_val <= 2.0:
                conf = 'HIGH'
            elif rsi_val <= 6.0:
                conf = 'MED'
            else:
                conf = 'LOW'

            weight = (1.0 / atr) / inv_atr_sum if inv_atr_sum > 0 else 1.0 / len(hits)
            pos_pct = max(0.01, min(0.05, gross_alloc * weight)) * scale

            signals.append(Signal(
                ticker            = ticker,
                direction         = 'LONG',
                entry_price       = price,
                stop_loss         = stops['stop'],
                target_1          = stops['t1'],
                target_2          = stops['t2'],
                target_3          = stops['t3'],
                position_size_pct = round(pos_pct, 6),
                confidence        = conf,
                signal_params     = {
                    'rsi2':           round(rsi_val, 2),
                    'rsi_lookback':   rsi_lookback,
                    'trend_sma':      trend_sma,
                    'trend_slope_lb': trend_slope_lb,
                    'max_hold_days':  int(p.get('max_hold_days', MAX_HOLD_DAYS)),
                    'regime':         regime_state,
                },
                features          = {'rsi_2': round(rsi_val, 2), 'atr_14': round(atr, 4)},
            ))

        signals = signals[: self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes

    prices_df, _bars = load_prices_panels()
    reg_series = load_regimes()

    sma      = prices_df.rolling(TREND_SMA).mean()
    rsi_full = prices_df.apply(lambda col: _wilder_rsi(col, RSI_LOOKBACK))

    rows = []
    idx = prices_df.index
    start = max(TREND_SMA + TREND_SLOPE_LB, MIN_LOOKBACK)
    for i in range(start, len(idx) - MAX_HOLD_DAYS):
        d  = idx[i]
        xd = idx[i + MAX_HOLD_DAYS]

        sma_now = sma.loc[d]
        sma_lag = sma.iloc[i - TREND_SLOPE_LB]
        rsi_now = rsi_full.loc[d]
        day_price = prices_df.loc[d]
        exit_price = prices_df.loc[xd]

        rising = sma_now > sma_lag
        oversold = rsi_now < RSI_THRESHOLD
        hits = day_price[rising & oversold & day_price.notna() & (day_price > 0)]
        if hits.empty:
            continue
        hits = hits.sort_values(ascending=False).iloc[:20]

        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'

        for ticker in hits.index:
            ep = day_price.get(ticker)
            xp = exit_price.get(ticker)
            if ep is None or xp is None or pd.isna(ep) or pd.isna(xp) or float(ep) <= 0:
                continue
            pnl = (float(xp) - float(ep)) / float(ep)
            rows.append({
                'strategy_id': STRATEGY_ID, 'signal_date': d, 'regime_state': rstate,
                'pnl': pnl, 'r_multiple': round(pnl / 0.02, 4),
            })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
