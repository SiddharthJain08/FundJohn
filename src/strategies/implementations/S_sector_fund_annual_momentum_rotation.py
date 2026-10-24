"""
S_sector_fund_annual_momentum_rotation

Source: https://bestfolio.app/blog/last-years-best-funds-momentum-rule (2026),
"Last year's best funds, bought every January: a member's momentum rule, tested"
— a non-peer-reviewed blog replication/test, not a primary academic source.

Hypothesis: each January, buy whichever sector/country fund had the best prior
calendar-year total return and hold it for the next calendar year, with half
of capital parked in Treasuries as a buffer and a 35% hard stop on the fund
leg. The author's own test shows this beats the group average only 10/26
years and the apparent edge is concentrated in the 1990s tech run — flagged
`data_snooping_bias` and does not persist out of that window. Implemented
faithfully (tier 3) so it can be backtested regime-by-regime rather than
taken on faith; do not expect it to clear the promotion gate.

Rule (annual, first trading days of January):
  candidates = 11 US sector SPDRs + 21 country/regional ETFs (sidesteps
               single-stock survivorship bias, same as the source post using
               Fidelity Select / sector / country funds instead of stocks)
  winner     = candidate with the highest prior-calendar-year total return
  LONG winner at ~50% of capital (scaled by regime), 35% hard stop
  LONG TLT (Treasuries) at ~50% of capital (scaled by regime) as the buffer
  Rebalance only in the first trading days of January; hold the rest of the
  year (no cadence_reset — this is a seasonality window, not a generic
  rebalance cadence).
"""
from __future__ import annotations
import sys
from typing import List

import pandas as pd

from strategies.base import BaseStrategy, Signal

__all__ = ['SectorFundAnnualMomentumRotation']

INSTRUMENT_CLASS = 'etp'
STRATEGY_ID = 'S_sector_fund_annual_momentum_rotation'

# 11 US sector SPDRs + 21 country/regional ETFs — fixed candidate universe,
# all with full history in data/master/prices.parquet.
SECTOR_FUNDS = ('XLB', 'XLC', 'XLE', 'XLF', 'XLI', 'XLK', 'XLP', 'XLRE', 'XLU', 'XLV', 'XLY')
COUNTRY_FUNDS = (
    'EWJ', 'EWG', 'EWU', 'EWC', 'EWA', 'EWY', 'EWT', 'EWZ', 'FXI', 'EWH',
    'EWS', 'EWI', 'EWP', 'EWQ', 'EWL', 'EWN', 'EWD', 'EWM', 'INDA', 'EZA', 'EWW',
)
BASKET = SECTOR_FUNDS + COUNTRY_FUNDS

TREASURY_TICKER = 'TLT'           # Treasury buffer leg
JAN_WINDOW_DAYS = 3               # tolerate a few days' pipeline slip into January
FUND_STOP_PCT = 0.35              # hard stop on the fund leg, per spec
FUND_BASE_SIZE = 0.50             # ~50% of capital to the winning fund
TREASURY_BASE_SIZE = 0.50         # ~50% of capital to the Treasury buffer
MIN_LOOKBACK = 504                # ~2 years, per spec min_lookback_required


def _in_january_rebalance_window(idx: pd.DatetimeIndex, dt: pd.Timestamp, window: int = JAN_WINDOW_DAYS) -> bool:
    """True if `dt` is among the first `window` trading days of its calendar year's January."""
    jan_days = idx[(idx.year == dt.year) & (idx.month == 1)]
    if jan_days.empty:
        return False
    return dt in jan_days[:window]


def _prior_calendar_year_return(yearly_closes: pd.Series) -> 'float | None':
    """Total return of the most recently COMPLETED calendar year.

    `yearly_closes` is a year-end-resampled close series whose last entry is
    the (incomplete) current year; the prior complete year is iloc[-2] vs
    the year before that, iloc[-3]."""
    s = yearly_closes.dropna()
    if len(s) < 3:
        return None
    start_val = float(s.iloc[-3])
    end_val = float(s.iloc[-2])
    if start_val <= 0:
        return None
    return end_val / start_val - 1.0


class SectorFundAnnualMomentumRotation(BaseStrategy):
    """Annual January rotation into the prior year's best sector/country fund,
    half capital in Treasuries, 35% hard stop on the fund leg (bestfolio.app 2026)."""

    id                = STRATEGY_ID
    name              = 'Sector Fund Annual Momentum Rotation'
    description       = (
        "Buy January's prior-calendar-year winner among 11 sector SPDRs + 21 "
        "country ETFs, half capital in Treasuries (TLT), 35% hard stop on the "
        "fund leg; author's own backtest beats the group average only 10/26 "
        "years and the edge is concentrated in the 1990s tech run."
    )
    tier              = 3
    signal_frequency  = 'annual'
    min_lookback      = MIN_LOOKBACK
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']
    instrument_class  = INSTRUMENT_CLASS
    MAX_SIGNALS       = 2

    def default_parameters(self) -> dict:
        return {
            'fund_base_size':     FUND_BASE_SIZE,
            'treasury_base_size': TREASURY_BASE_SIZE,
            'fund_stop_pct':      FUND_STOP_PCT,
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
        if not _in_january_rebalance_window(prices.index, last_date):
            print('[debug] signals=0 (not the January rebalance window)', file=sys.stderr)
            return []

        if TREASURY_TICKER not in prices.columns:
            print('[debug] signals=0 (no TLT column for Treasury buffer)', file=sys.stderr)
            return []

        available = [t for t in BASKET if t in prices.columns]
        if not available:
            print('[debug] signals=0 (no basket members in prices)', file=sys.stderr)
            return []

        yearly = prices[available].resample('YE').last()
        momentum: dict[str, float] = {}
        for ticker in available:
            ret = _prior_calendar_year_return(yearly[ticker])
            if ret is not None:
                momentum[ticker] = ret

        if not momentum:
            print('[debug] signals=0 (no usable prior-year momentum scores)', file=sys.stderr)
            return []

        winner = max(momentum, key=momentum.__getitem__)
        winner_mom = momentum[winner]

        fund_series = prices[winner].dropna()
        treasury_series = prices[TREASURY_TICKER].dropna()
        if fund_series.empty or treasury_series.empty:
            print('[debug] signals=0 (fund or treasury leg has no price history)', file=sys.stderr)
            return []

        fund_price = float(fund_series.iloc[-1])
        treasury_price = float(treasury_series.iloc[-1])
        if not (fund_price > 0 and treasury_price > 0):
            print('[debug] signals=0 (bad last price on a leg)', file=sys.stderr)
            return []

        scale = self.position_scale(regime_state)
        p = self.parameters
        fund_size = round(float(p.get('fund_base_size', FUND_BASE_SIZE)) * scale, 4)
        treasury_size = round(float(p.get('treasury_base_size', TREASURY_BASE_SIZE)) * scale, 4)
        fund_stop_pct = float(p.get('fund_stop_pct', FUND_STOP_PCT))

        fund_stops = self.compute_stops_and_targets(
            fund_series, direction='LONG', current_price=fund_price, regime_state=regime_state,
        )
        # Override the ATR stop with the spec's explicit 35% hard stop on the fund leg.
        fund_stop = round(fund_price * (1.0 - fund_stop_pct), 4)

        treasury_stops = self.compute_stops_and_targets(
            treasury_series, direction='LONG', current_price=treasury_price, regime_state=regime_state,
        )

        fund_confidence = 'HIGH' if winner_mom > 0.30 else ('MED' if winner_mom > 0.10 else 'LOW')

        signals = [
            Signal(
                ticker            = winner,
                direction         = 'LONG',
                entry_price       = fund_price,
                stop_loss         = fund_stop,
                target_1          = float(fund_stops['t1']),
                target_2          = float(fund_stops['t2']),
                target_3          = float(fund_stops['t3']),
                position_size_pct = fund_size,
                confidence        = fund_confidence,
                signal_params     = {
                    'prior_year_return': round(winner_mom, 4),
                    'basket_size':       len(available),
                    'fund_stop_pct':     fund_stop_pct,
                    'regime':            regime_state,
                    'scale':             scale,
                    'leg':               'fund',
                },
            ),
            Signal(
                ticker            = TREASURY_TICKER,
                direction         = 'LONG',
                entry_price       = treasury_price,
                stop_loss         = float(treasury_stops['stop']),
                target_1          = float(treasury_stops['t1']),
                target_2          = float(treasury_stops['t2']),
                target_3          = float(treasury_stops['t3']),
                position_size_pct = treasury_size,
                confidence        = 'MED',
                signal_params     = {
                    'regime': regime_state,
                    'scale':  scale,
                    'leg':    'treasury_buffer',
                },
            ),
        ]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes
    from strategies.aux_data_loader import load_aux_data

    legs = list(BASKET) + [TREASURY_TICKER]
    prices_df, _bars = load_prices_panels(tickers=legs)
    reg_series = load_regimes()

    strat = SectorFundAnnualMomentumRotation()
    idx = prices_df.index
    start = max(strat.min_lookback, 400)
    HOLD_DAYS = 252  # ~1 calendar year forward

    rows = []
    last_year = None
    for i in range(start, len(idx) - HOLD_DAYS):
        d = idx[i]
        if d.year == last_year:
            continue  # one decision per calendar year, like the live cadence
        if not _in_january_rebalance_window(idx, d):
            continue
        last_year = d.year

        xd = idx[i + HOLD_DAYS]
        prices_upto = prices_df.iloc[: i + 1]
        aux = load_aux_data(d.strftime('%Y-%m-%d'))

        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'
        regime_dict = {'state': rstate}

        sigs = strat.generate_signals(prices_upto, regime_dict, legs, aux_data=aux)
        if not sigs:
            continue
        sig = sigs[0]  # fund leg — the momentum rule under test

        leg = prices_df[sig.ticker].dropna()
        if d not in leg.index or xd not in leg.index:
            continue
        c0 = float(leg.loc[d])
        cx = float(leg.loc[xd])
        if c0 <= 0:
            continue
        pnl = (cx - c0) / c0
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
