"""S_retail_scale_multimarket_trend_tv2 — multi-market time-series trend on
6 asset classes a normal retail brokerage account can actually reach.

Source: https://yuratrading.com/blogs/trend-portfolio (2026), "We Tested the
Best-Evidenced Idea in Finance at the Scale a Retail Trader Can Reach."

Hypothesis under test: the published managed-futures result (weak per-market
trend signals diversify into a strong portfolio across dozens of markets) is
claimed by the authors to collapse once the universe is shrunk to the ~6
markets reachable via ordinary ETFs — the author reports 58 of 60 swept
parameter settings lost money. Implemented faithfully as a falsification
candidate (tier 3); expected weak/negative, which is the point of running it
through the regime-partitioned backtest rather than taking the blog's own
numbers on faith.

Rule (unambiguous from the pseudocode):
  for each of 6 retail-accessible markets:
      trend_signal = sign(trailing_return(lookback))
      weight       = kelly_sized(trend_signal, realized_vol)
  combine equal-risk-weighted across markets
  LONG when trend_signal > 0, SHORT when trend_signal < 0, FLAT otherwise
  (swept over lookback x sizing — 60 settings in the source; we fix one
  defensible setting per variant rather than re-running the sweep).

Interpretation variant 2 of 2 — deliberately different resolutions of the
same ambiguous points that variant 1 resolved one way:
  - The 6 markets: SPY (US equities), IEF (7-10yr Treasuries, NOT the 20yr+
    TLT point on the curve), GLD (gold), DBC (broad/diversified commodities,
    NOT the single-commodity USO), FXE (Euro currency, the mirror-image
    currency trade to variant 1's dollar-index UUP), VNQ (US REITs, a
    distinct asset class, NOT EM equities which already overlaps with the
    SPY equity exposure). Equally "6 retail-accessible, commission-free
    ETFs" as variant 1's basket, but a different set of 6 distinct points
    in asset-class space.
  - Trend lookback: 252 trading days (~12 months) — the canonical
    Moskowitz/Ooi/Pedersen time-series-momentum window, i.e. the textbook
    choice variant 1 explicitly avoided in favor of a faster 6-month read.
    The source sweeps many lookbacks and reports the edge as fragile across
    the whole grid, so 12 months is an equally defensible single pick.
  - "Kelly-sized by realized vol": read literally as the classic single-
    asset Kelly fraction f* = mu / sigma^2 (expected-return-over-variance),
    using the trailing return itself (clipped) as the mu estimate — NOT
    variant 1's magnitude-blind inverse-vol risk parity, which sizes every
    active leg by vol alone regardless of how strong its trend is. This
    variant lets a stronger trend get a larger weight than a weak one at
    the same vol, which is what "Kelly" ordinarily means.
  - "FLAT otherwise" is read literally: a leg whose trailing return is
    exactly zero (or whose signal has just reversed) emits an explicit
    FLAT signal to flatten that leg, rather than variant 1's approach of
    silently omitting the leg from the signal list.
  - Regime scope: LOW_VOL + TRANSITIONING + HIGH_VOL — trend-following
    needs persistent, low-noise directional moves to earn its risk premium;
    variant 1 bet on trend as a crisis-alpha convexity play and excluded
    LOW_VOL. This variant instead excludes CRISIS (tail events are famous
    for momentum-crash whipsaws that break trailing-return trend rules)
    and includes LOW_VOL, where calm multi-month drifts are most likely to
    persist long enough for a 12-month lookback to pay off.
  - Rebalance cadence: weekly (every Monday, or the first trading day of
    the week if Monday is a holiday) — variant 1 used monthly; a 12-month
    lookback signal changes slowly, so a weekly rebalance only changes
    which legs are active, not the underlying trend read, while giving the
    backtest meaningfully more trade observations per regime bucket.
"""
from __future__ import annotations

import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['RetailScaleMultimarketTrendTv2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_retail_scale_multimarket_trend_tv2'

# Six retail-accessible, asset-class-distinct markets (variant 2 basket).
MARKETS = ('SPY', 'IEF', 'GLD', 'DBC', 'FXE', 'VNQ')

TREND_LOOKBACK   = 252   # ~12 trading months (canonical TSMOM window)
VOL_WINDOW       = 63    # ~3 trading months realized-vol/variance estimate
RETURN_CAP       = 0.40  # clip trailing_return magnitude before the Kelly ratio
GROSS_BASE_SIZE  = 0.60  # total gross exposure across all 6 legs at scale=1.0
MIN_LOOKBACK     = TREND_LOOKBACK + VOL_WINDOW + 5
MIN_LOOKBACK_REQUIRED = 504  # per spec (~2yr) — generous floor beyond the strategy's own need


def _first_trading_day_of_week(idx: pd.DatetimeIndex, dt: pd.Timestamp) -> bool:
    iso_year, iso_week, _ = dt.isocalendar()
    week_days = idx[idx.map(lambda d: d.isocalendar()[:2]) == (iso_year, iso_week)]
    if week_days.empty:
        return False
    return dt == week_days[0]


def _trailing_return(close: pd.Series, lookback: int) -> float:
    """trailing_return(lookback), NaN-safe; returns 0.0 if insufficient history."""
    if len(close) < lookback + 1:
        return 0.0
    start, end = float(close.iloc[-lookback - 1]), float(close.iloc[-1])
    if start <= 0:
        return 0.0
    return end / start - 1.0


def _realized_variance(close: pd.Series, window: int) -> float:
    """Annualized realized variance of daily returns over `window` bars."""
    rets = close.pct_change().dropna()
    if len(rets) < window:
        return float('nan')
    daily_var = float(rets.iloc[-window:].std() ** 2)
    return daily_var * 252.0


class RetailScaleMultimarketTrendTv2(BaseStrategy):
    """Literal-Kelly (mu/sigma^2) sign-of-trend across 6 retail-accessible
    macro ETFs (SPY/IEF/GLD/DBC/FXE/VNQ), 12-month lookback, weekly
    rebalance, explicit FLAT on a zero trailing return. Falsification
    candidate per yuratrading.com (2026) — expected weak/negative at this
    universe size (variant 2 of 2)."""

    id                = STRATEGY_ID
    name              = 'RetailScaleMultimarketTrendTv2'
    description       = (
        'Literal Kelly fraction (trailing-return / realized-variance) sign-'
        'of-trend (252d) across 6 retail-accessible macro ETFs '
        '(SPY/IEF/GLD/DBC/FXE/VNQ), weekly rebalance, explicit FLAT on a '
        'zero trailing return. Falsification test (yuratrading.com 2026): '
        '58/60 swept settings lost money at this universe size '
        '(variant 2 of 2).'
    )
    tier              = 3
    min_lookback      = MIN_LOOKBACK_REQUIRED
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    instrument_class  = INSTRUMENT_CLASS
    MAX_SIGNALS       = len(MARKETS)

    def default_parameters(self) -> dict:
        return {
            'trend_lookback':  TREND_LOOKBACK,
            'vol_window':      VOL_WINDOW,
            'return_cap':      RETURN_CAP,
            'gross_base_size': GROSS_BASE_SIZE,
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (no price data)', file=sys.stderr)
            return []

        regime_state = regime.get('state', 'LOW_VOL') if isinstance(regime, dict) else 'LOW_VOL'
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime gate)', file=sys.stderr)
            return []

        if len(prices) < self.min_lookback:
            print(f'[debug] signals=0 (insufficient history: {len(prices)})', file=sys.stderr)
            return []

        last_date = prices.index[-1]
        if not _first_trading_day_of_week(prices.index, last_date):
            print('[debug] signals=0 (not the weekly rebalance day)', file=sys.stderr)
            return []

        p = self.parameters
        lookback   = int(p.get('trend_lookback', TREND_LOOKBACK))
        vol_window = int(p.get('vol_window', VOL_WINDOW))
        return_cap = float(p.get('return_cap', RETURN_CAP))

        available = [t for t in MARKETS if t in prices.columns]
        if len(available) < 2:
            print('[debug] signals=0 (fewer than 2 basket members present)', file=sys.stderr)
            return []

        legs: dict[str, dict] = {}
        flats: dict[str, float] = {}
        for ticker in available:
            series = prices[ticker].dropna()
            if len(series) < lookback + vol_window + 1:
                continue
            ret = _trailing_return(series, lookback)
            price = float(series.iloc[-1])
            if price <= 0:
                continue
            if ret == 0.0:
                flats[ticker] = price
                continue
            var = _realized_variance(series, vol_window)
            if not (var == var) or var <= 0:  # NaN or non-positive
                continue
            clipped_ret = max(min(ret, return_cap), -return_cap)
            kelly = clipped_ret / var
            legs[ticker] = {
                'series': series, 'ret': ret, 'kelly': kelly, 'price': price,
            }

        if not legs and not flats:
            print('[debug] signals=0 (no active trend or flat legs this week)', file=sys.stderr)
            return []

        kelly_gross = sum(abs(leg['kelly']) for leg in legs.values())
        scale = self.position_scale(regime_state)
        gross_budget = float(p.get('gross_base_size', GROSS_BASE_SIZE)) * scale

        signals: List[Signal] = []
        for ticker, leg in legs.items():
            raw_weight = abs(leg['kelly']) / kelly_gross if kelly_gross > 0 else 0.0
            pos_size = round(min(raw_weight * gross_budget, 1.0), 4)
            if pos_size <= 0:
                continue
            direction = 'LONG' if leg['kelly'] > 0 else 'SHORT'
            stops = self.compute_stops_and_targets(
                leg['series'], direction=direction, current_price=leg['price'],
                regime_state=regime_state,
            )
            confidence = 'MED' if abs(leg['ret']) > return_cap * 0.5 else 'LOW'
            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = leg['price'],
                stop_loss         = float(stops['stop']),
                target_1          = float(stops['t1']),
                target_2          = float(stops['t2']),
                target_3          = float(stops['t3']),
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'trend_lookback': lookback,
                    'vol_window':     vol_window,
                    'trailing_return': round(leg['ret'], 4),
                    'kelly_fraction': round(leg['kelly'], 4),
                    'basket_size':    len(legs),
                    'regime':         regime_state,
                },
            ))

        for ticker, price in flats.items():
            signals.append(Signal(
                ticker            = ticker,
                direction         = 'FLAT',
                entry_price       = price,
                stop_loss         = price,
                target_1          = price,
                target_2          = price,
                target_3          = price,
                position_size_pct = 0.0,
                confidence        = 'LOW',
                signal_params     = {
                    'trend_lookback': lookback,
                    'reason':         'zero_trailing_return',
                    'regime':         regime_state,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json

    from backtest.unified_backtest import load_prices_panels, load_regimes
    from strategies.aux_data_loader import load_aux_data

    prices_df, _bars = load_prices_panels(tickers=list(MARKETS))
    reg_series = load_regimes()

    strat = RetailScaleMultimarketTrendTv2()
    idx = prices_df.index
    start = max(strat.min_lookback, MIN_LOOKBACK + 10)
    HOLD_DAYS = 5  # ~1 week forward, matches the weekly rebalance cadence

    rows = []
    last_week = None
    for i in range(start, len(idx) - HOLD_DAYS):
        d = idx[i]
        key = d.isocalendar()[:2]
        if key == last_week:
            continue
        if not _first_trading_day_of_week(idx, d):
            continue
        last_week = key

        xd = idx[i + HOLD_DAYS]
        prices_upto = prices_df.iloc[: i + 1]
        aux = load_aux_data(d.strftime('%Y-%m-%d'))

        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'
        regime_dict = {'state': rstate}

        sigs = strat.generate_signals(prices_upto, regime_dict, list(MARKETS), aux_data=aux)
        for sig in sigs:
            if sig.direction == 'FLAT':
                continue
            leg = prices_df[sig.ticker].dropna()
            if d not in leg.index or xd not in leg.index:
                continue
            c0 = float(leg.loc[d])
            cx = float(leg.loc[xd])
            if c0 <= 0:
                continue
            raw_ret = (cx - c0) / c0
            pnl = raw_ret if sig.direction == 'LONG' else -raw_ret
            stop_dist = max(abs(c0 - sig.stop_loss), 1e-6)
            rows.append({
                'strategy_id': STRATEGY_ID, 'signal_date': d, 'regime_state': rstate,
                'pnl': pnl, 'r_multiple': round(pnl * c0 / stop_dist, 4),
            })

    trades_df = pd.DataFrame(rows)
    print(f'[backtest] {len(trades_df)} trades', file=sys.stderr)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
