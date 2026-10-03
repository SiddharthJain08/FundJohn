"""
September Seasonal Weakness — Jeff Hirsch / Stock Trader's Almanac.
Source: https://jeffhirsch.tumblr.com/post/826225834183491584

Hypothesis: September is the weakest calendar month for major US equity
indices since 1950, with losses widening further in midterm election years
(post-summer repositioning + Q3-end institutional flows). direction_vocab is
['FLAT','SHORT'] — no long leg.

Interpretation variant 2 of 2 — the abstract pins the calendar window
(September) and the midterm amplifier (current_year % 4 == 2) unambiguously,
but is silent on HOW the "broad equity index" short is actually implemented
against an sp500-filtered equity universe, and on how much conviction the
midterm amplifier should add. This variant deliberately reads those two gaps
the OPPOSITE way from variant 1:
  - breadth: short only the WEAKEST-MOMENTUM DECILE within the eligible
    universe (bottom ~10% by trailing 63-trading-day return), a concentrated
    stock-selection reading, rather than variant 1's equal-weight whole-basket
    replication. "Post-summer repositioning" is itself a rotation story —
    the names already losing momentum into Labor Day are the ones
    institutional flows abandon fastest in September, so concentrating the
    short in the weakest decile is at least as defensible a reading of
    "SHORT broad_equity_index" as a literal diversified-basket replication,
    and it is the more surgical way to express the thesis without taking on
    unnecessary idiosyncratic risk in the other 90% of names.
  - midterm amplification: a flat 2x position-size doubling in midterm
    election years, rather than variant 1's moderate 1.3x size + one-notch
    confidence upgrade — the abstract's own framing ("losses widening
    further") in a 75-year sample with only 19 midterm Septembers supports
    a simple, interpretable doubling as the more literal reading of
    "amplify conviction" than a muted multiplier.
Calendar window (September) and FLAT elsewhere are unambiguous and kept
as-is, identical to variant 1.
"""
from __future__ import annotations
import sys
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal
from src.strategies.universe_default import sp500 as universe_filter

__all__ = ['SeptemberSeasonalWeaknessTV2']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID      = 'S_september_seasonal_weakness'

SEASONAL_MONTH    = 9
MIDTERM_MOD       = 2        # current_year % 4 == 2 -> midterm election year
MOMENTUM_WINDOW   = 63       # trailing trading days (~1 quarter) for ranking
DECILE_FRACTION   = 0.10     # short only the weakest-momentum decile
BASE_SIZE         = 0.03     # larger per-ticker size — concentrated, not a basket
MIDTERM_SIZE_MULT = 2.0      # aggressive flat doubling (variant 2 — not 1.3x)
MIN_UNIVERSE      = 50
ATR_MULTIPLIER    = 2.0


def _equity_trading_days(prices: pd.DataFrame) -> pd.DatetimeIndex:
    """Restrict the union calendar to days with meaningful equity breadth."""
    counts = prices.notna().sum(axis=1)
    threshold = max(MIN_UNIVERSE, int(0.05 * prices.shape[1]))
    return prices.index[counts >= threshold]


class SeptemberSeasonalWeaknessTV2(BaseStrategy):
    """SHORT the weakest-momentum decile of the eligible equity universe
    through September, doubled in midterm election years; FLAT every other
    month. Jeff Hirsch / Stock Trader's Almanac seasonal observation."""

    id                = STRATEGY_ID
    name              = 'SeptemberSeasonalWeaknessTV2'
    description       = (
        'SHORT the weakest-momentum decile of the eligible equity universe '
        'during September (worst calendar month since 1950 per Stock '
        'Trader\'s Almanac), doubled in midterm election years; FLAT '
        'otherwise (variant 2 of 2).'
    )
    tier              = 2
    signal_frequency  = 'daily'
    calendar_edge     = True   # window IS the signal; ports across regime flips
    min_lookback      = 252
    instrument_class  = INSTRUMENT_CLASS
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    MAX_SIGNALS       = 50

    def default_parameters(self) -> dict:
        return {'base_size': BASE_SIZE, 'midterm_size_mult': MIDTERM_SIZE_MULT}

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

        today = prices.index[-1]
        if today.month != SEASONAL_MONTH:
            print('[debug] signals=0', file=sys.stderr)
            return []  # FLAT outside September — unambiguous in the abstract

        is_midterm = (today.year % 4 == MIDTERM_MOD)

        idx = _equity_trading_days(prices)
        if len(idx) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Rank by trailing momentum; short the weakest decile only (variant 2 —
        # concentrated stock selection, not a whole-basket replication).
        momentum: dict = {}
        series_cache: dict = {}
        for ticker in tickers:
            series = prices[ticker].dropna()
            if len(series) < MOMENTUM_WINDOW + 5 or today not in series.index:
                continue
            pos = series.index.get_loc(today)
            if pos < MOMENTUM_WINDOW:
                continue
            past_px = float(series.iloc[pos - MOMENTUM_WINDOW])
            cur_px  = float(series.iloc[pos])
            if past_px <= 0 or cur_px <= 0:
                continue
            momentum[ticker] = (cur_px - past_px) / past_px
            series_cache[ticker] = series

        if len(momentum) < MIN_UNIVERSE:
            print('[debug] signals=0', file=sys.stderr)
            return []

        ranked = sorted(momentum, key=momentum.get)  # ascending -> weakest first
        decile_n = max(5, int(len(ranked) * DECILE_FRACTION))
        decile_n = min(decile_n, self.MAX_SIGNALS)
        weakest = ranked[:decile_n]

        scale      = self.position_scale(regime_state)
        base_size  = float(self.parameters.get('base_size', BASE_SIZE))
        size_mult  = float(self.parameters.get('midterm_size_mult', MIDTERM_SIZE_MULT)) if is_midterm else 1.0
        confidence = 'HIGH' if is_midterm else 'MED'

        signals: List[Signal] = []
        for ticker in weakest:
            series = series_cache[ticker]
            price  = float(series.iloc[-1])
            if price <= 0:
                continue

            stops = self.compute_stops_and_targets(
                series, 'SHORT', price,
                atr_multiplier=ATR_MULTIPLIER, regime_state=regime_state,
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=price,
                stop_loss=float(stops['stop']),
                target_1=float(stops['t1']),
                target_2=float(stops['t2']),
                target_3=float(stops['t3']),
                position_size_pct=round(base_size * size_mult * scale, 6),
                confidence=confidence,
                signal_params={
                    'month':            today.month,
                    'is_midterm_yr':    bool(is_midterm),
                    'regime':           regime_state,
                    'momentum_window':  MOMENTUM_WINDOW,
                    'momentum_rank':    round(momentum[ticker], 4),
                    'decile_size':      decile_n,
                },
            ))

        print(
            f'[{STRATEGY_ID}] today={today.date()} is_midterm={is_midterm} '
            f'universe={len(tickers)} decile={decile_n} signals={len(signals)} '
            f'regime={regime_state}',
            file=sys.stderr,
        )
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import os, json as _json
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    long_df = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet', columns=['ticker', 'date', 'close'])
    long_df['date'] = pd.to_datetime(long_df['date'])
    prices_df = long_df.pivot(index='date', columns='ticker', values='close').sort_index()

    reg_df = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    reg_df = reg_df.sort_values('date')

    def _regime_on(dt):
        mask = reg_df['date'] <= dt
        return str(reg_df.loc[mask, 'regime'].iloc[-1]) if mask.any() else 'LOW_VOL'

    idx = _equity_trading_days(prices_df)
    sept_days = idx[idx.month == SEASONAL_MONTH]
    years = sorted(set(sept_days.year))
    rows = []
    for y in years:
        year_days = sept_days[sept_days.year == y]
        if len(year_days) < 2:
            continue
        entry_date, exit_date = year_days[0], year_days[-1]
        is_midterm = (y % 4 == MIDTERM_MOD)
        size_mult  = MIDTERM_SIZE_MULT if is_midterm else 1.0
        reg = _regime_on(entry_date)
        entry_row = prices_df.loc[entry_date]
        exit_row  = prices_df.loc[exit_date]

        # Rank by momentum as of entry_date, trailing MOMENTUM_WINDOW days.
        pos = prices_df.index.get_loc(entry_date)
        if pos < MOMENTUM_WINDOW:
            continue
        past_row = prices_df.iloc[pos - MOMENTUM_WINDOW]
        mom = {}
        for c in prices_df.columns:
            p0, p1 = past_row.get(c), entry_row.get(c)
            if pd.notna(p0) and pd.notna(p1) and float(p0) > 0:
                mom[c] = (float(p1) - float(p0)) / float(p0)
        if len(mom) < MIN_UNIVERSE:
            continue
        ranked = sorted(mom, key=mom.get)
        decile_n = max(5, int(len(ranked) * DECILE_FRACTION))
        weakest = ranked[:decile_n]

        for ticker in weakest:
            if pd.isna(exit_row.get(ticker)) or pd.isna(entry_row.get(ticker)):
                continue
            entry_px = float(entry_row[ticker])
            exit_px  = float(exit_row[ticker])
            if entry_px <= 0:
                continue
            pnl = -(exit_px - entry_px) / entry_px  # SHORT
            r_mult = pnl / (BASE_SIZE * size_mult) if (BASE_SIZE * size_mult) else 0.0
            rows.append({
                'strategy_id': STRATEGY_ID, 'signal_date': str(entry_date.date()),
                'regime_state': reg, 'pnl': pnl, 'r_multiple': round(r_mult, 4),
            })

    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    print(f'[backtest] completed_trades={len(trades_df)}', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps(result, indent=2, default=str))
