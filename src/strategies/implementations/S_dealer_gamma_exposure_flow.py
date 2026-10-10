"""Gamma Exposure: How Dealer Hedging Moves the Market — Melchor Alaiz, H. (2026).
https://hmaquant.substack.com/p/gamma-exposure-how-dealer-hedging

Hypothesis: dealer options books generate forced, view-agnostic hedging flow
in the underlying that AMPLIFIES moves when aggregate dealer gamma exposure
(GEX) is negative (dealers short gamma -> they buy into up-moves / sell into
down-moves to stay hedged) and DAMPENS moves when GEX is positive (dealers
long gamma -> they sell into up-moves / buy into down-moves).

Implementation: per-ticker GEX is read directly from aux_data['options'][t]
['gex'] (canonical type 'options_eod' -> CBOE chain OI x gamma aggregation,
src/strategies/options_oi.py::oi_features_for_day — gc - gp over the front
expiry, so negative = dealers net short gamma). Today's close-to-close return
z-scored against a rolling window stands in for the paper's "realized
intraday direction" (no intraday panel is wired into generate_signals):
  - GEX < 0 (SHORT_GAMMA): trade WITH today's move once it clears a modest
    z-score floor (dealer flow should keep chasing it).
  - GEX >= 0 (LONG_GAMMA): fade today's move once it is an outsized
    (extreme-z) print (dealer flow should be dampening it back).
Cross-sectional across the universe, daily rebalance.
"""
from __future__ import annotations
import sys
from typing import List
import pandas as pd
from strategies.base import BaseStrategy, Signal

__all__ = ['DealerGammaExposureFlow']

INSTRUMENT_CLASS = 'equity'
STRATEGY_ID = 'S_dealer_gamma_exposure_flow'


class DealerGammaExposureFlow(BaseStrategy):
    """Trade with dealer short-gamma hedging flow (momentum) / against dealer
    long-gamma hedging flow (mean-reversion on extreme prints), per ticker,
    using options_eod-derived GEX as the regime switch."""

    id                = STRATEGY_ID
    name              = 'Dealer Gamma Exposure Flow'
    description       = ('Per-ticker dealer GEX sign (from options_eod OI x gamma) gates '
                          'momentum (GEX<0, chase the daily move) vs mean-reversion '
                          '(GEX>=0, fade an extreme daily move).')
    tier              = 2
    signal_frequency  = 'daily'
    min_lookback      = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    def default_parameters(self) -> dict:
        return {
            'zscore_window':   20,
            'momentum_min_z':  0.5,    # SHORT_GAMMA: minimum |z| to trust the chase
            'extreme_z':       1.5,    # LONG_GAMMA: minimum |z| to call a print "extreme"
            'base_size_pct':   0.03,
            'max_signals':     30,
        }

    def generate_signals(self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (empty prices)', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0 (regime inactive)', file=sys.stderr)
            return []
        if len(prices) < max(self.parameters.get('zscore_window', 20) if self.parameters else 20, 25):
            print(f'[debug] signals=0 (insufficient history: {len(prices)})', file=sys.stderr)
            return []

        opts_map = (aux_data or {}).get('options') or {}
        if not opts_map:
            print('[debug] signals=0 (no options data)', file=sys.stderr)
            return []

        p         = self.parameters
        window    = int(p['zscore_window'])
        mom_min_z = float(p['momentum_min_z'])
        extreme_z = float(p['extreme_z'])
        scale     = self.position_scale(regime_state)
        pos_size  = round(p['base_size_pct'] * scale, 4)

        signals: List[Signal] = []
        for ticker in universe:
            if ticker not in prices.columns or ticker not in opts_map:
                continue
            opts = opts_map.get(ticker) or {}
            gex = opts.get('gex')
            if gex is None:
                continue
            try:
                gex = float(gex)
            except (TypeError, ValueError):
                continue

            ts = prices[ticker].dropna()
            if len(ts) < window + 2:
                continue
            daily_ret = ts.pct_change().dropna()
            if len(daily_ret) < window + 1:
                continue
            today_ret = float(daily_ret.iloc[-1])
            hist_ret  = daily_ret.iloc[-(window + 1):-1]
            std = float(hist_ret.std())
            if std <= 0 or pd.isna(std):
                continue
            z = today_ret / std

            direction = None
            confidence = 'MED'
            gamma_regime = 'SHORT_GAMMA' if gex < 0 else 'LONG_GAMMA'

            if gex < 0:
                # Dealers short gamma -> hedging flow chases the move.
                if abs(z) < mom_min_z or today_ret == 0:
                    continue
                direction = 'LONG' if today_ret > 0 else 'SHORT'
                confidence = 'HIGH' if abs(z) >= extreme_z else 'MED'
            else:
                # Dealers long gamma -> hedging flow dampens extreme moves; fade them.
                if abs(z) < extreme_z:
                    continue
                direction = 'SHORT' if today_ret > 0 else 'LONG'
                confidence = 'MED'

            cur_price = float(ts.iloc[-1])
            if cur_price <= 0:
                continue
            stops = self.compute_stops_and_targets(ts, direction, cur_price, regime_state=regime_state)

            signals.append(Signal(
                ticker=ticker, direction=direction, entry_price=round(cur_price, 4),
                stop_loss=stops['stop'], target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=pos_size, confidence=confidence,
                signal_params={
                    'gex': round(gex, 4), 'gamma_regime': gamma_regime,
                    'daily_return_z': round(z, 4), 'daily_return': round(today_ret, 6),
                },
            ))
            if len(signals) >= int(p['max_signals']):
                break

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]


# ---------------------------------------------------------------------------
# Regime-partitioned backtest
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    import json as _json
    from backtest.unified_backtest import load_prices_panels, load_regimes
    from strategies.aux_data_loader import load_aux_data

    WINDOW = 20
    MOM_MIN_Z = 0.5
    EXTREME_Z = 1.5

    prices_df, _bars = load_prices_panels()
    reg_series = load_regimes()

    opt_df = pd.read_parquet('data/master/options_aggregates_enriched.parquet', columns=['date', 'ticker', 'gex'])
    opt_df = opt_df.dropna(subset=['gex'])
    gex_dates = sorted(opt_df['date'].unique())
    print(f'[backtest] {len(gex_dates)} sessions with gex data', file=sys.stderr)

    strat = DealerGammaExposureFlow()
    rows = []
    for d in gex_dates:
        d = pd.Timestamp(d)
        if d not in prices_df.index:
            continue
        loc = prices_df.index.get_loc(d)
        if loc + 1 >= len(prices_df.index):
            continue  # need a next-bar exit
        prices_upto = prices_df.iloc[: loc + 1]
        if len(prices_upto) < WINDOW + 2:
            continue
        next_date = prices_df.index[loc + 1]

        day_gex = opt_df[opt_df['date'] == d].set_index('ticker')['gex'].to_dict()
        aux = load_aux_data(d.strftime('%Y-%m-%d'))
        opts_map = aux.get('options') or {}
        for t, g in day_gex.items():
            if t in opts_map:
                opts_map[t]['gex'] = g
            else:
                opts_map[t] = {'gex': g}
        aux['options'] = opts_map

        prior_regimes = reg_series[reg_series.index <= d]
        rstate = str(prior_regimes.iloc[-1]) if not prior_regimes.empty else 'LOW_VOL'
        regime_dict = {'state': rstate}

        universe = list(day_gex.keys())
        sigs = strat.generate_signals(prices_upto, regime_dict, universe, aux_data=aux)

        close_t0 = prices_df.loc[d]
        close_xt = prices_df.loc[next_date]
        for sig in sigs:
            ep = close_t0.get(sig.ticker)
            xp = close_xt.get(sig.ticker)
            if ep is None or xp is None or pd.isna(ep) or pd.isna(xp) or ep <= 0:
                continue
            raw_ret = (float(xp) - float(ep)) / float(ep)
            pnl = raw_ret if sig.direction == 'LONG' else -raw_ret
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
