from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE
from src.strategies.universe_default import no_otc as universe_filter

INSTRUMENT_CLASS = "equity"

__all__ = ['FridayEarningsInattentionDrift']

STRATEGY_ID = 'S_friday_earnings_inattention_drift'


class FridayEarningsInattentionDrift(BaseStrategy):
    """Long positive-SUE and short negative-SUE Friday announcers for 60-day PEAD harvest."""

    id          = STRATEGY_ID
    name        = 'FridayEarningsInattentionDrift'
    description = (
        'Friday earnings announcements receive 15% less immediate attention, generating '
        '70% more post-announcement drift; LONG positive-SUE and SHORT negative-SUE '
        'Friday announcers for ~60 trading days (DellaVigna & Pollet 2009).'
    )
    tier        = 2
    min_lookback = 504

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    _HOLD_DAYS   = 60      # trading-day window for active drift positions
    _MIN_PRICE   = 5.0     # §3 price filter
    _SUE_WINDOW  = 8       # quarters of history for SUE σ (§2)
    _BASE_SIZE   = 0.012   # 1.2% base notional per name
    _MAX_PER_SIDE = 20

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

        scale    = self.position_scale(regime_state)
        aux_data = aux_data or {}

        earnings = aux_data.get('earnings')
        if earnings is None or earnings.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        sue_series = self._compute_sue_friday(earnings, universe, prices)
        if sue_series.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # ── Price filter & availability ────────────────────────────────
        avail = [t for t in sue_series.index if t in prices.columns]
        if not avail:
            print('[debug] signals=0', file=sys.stderr)
            return []

        close          = prices[avail].ffill()
        latest_prices  = close.iloc[-1]
        avail          = [t for t in avail
                          if not np.isnan(float(latest_prices.get(t, np.nan)))
                          and float(latest_prices.get(t, 0)) >= self._MIN_PRICE]
        if not avail:
            print('[debug] signals=0', file=sys.stderr)
            return []

        sue_series     = sue_series[avail]
        abs_rank       = sue_series.abs().rank(pct=True)

        longs  = sue_series[sue_series > 0].nlargest(self._MAX_PER_SIDE)
        shorts = sue_series[sue_series < 0].nsmallest(self._MAX_PER_SIDE)

        signals: List[Signal] = []
        for direction, cohort in (('LONG', longs), ('SHORT', shorts)):
            for ticker, sue_val in cohort.items():
                cp = float(latest_prices.get(ticker, np.nan))
                if np.isnan(cp) or cp <= 0:
                    continue
                rank_pct = float(abs_rank.get(ticker, 0.5))
                size     = float(round(
                    min(0.03, max(0.005, self._BASE_SIZE * scale * rank_pct * 2)), 4
                ))
                st = self.compute_stops_and_targets(
                    close[ticker].dropna(), direction, cp, regime_state=regime_state
                )
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = direction,
                    entry_price       = float(cp),
                    stop_loss         = float(st['stop']),
                    target_1          = float(st['t1']),
                    target_2          = float(st['t2']),
                    target_3          = float(st['t3']),
                    position_size_pct = size,
                    confidence        = 'HIGH' if abs(sue_val) > 1.5 else 'MED',
                    signal_params     = {
                        'sue':              float(sue_val),
                        'abs_sue_rank_pct': float(rank_pct),
                        'report_day':       'friday',
                        'hold_days':        self._HOLD_DAYS,
                    },
                ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]

    # ── helpers ────────────────────────────────────────────────────────
    def _compute_sue_friday(
        self, earnings: pd.DataFrame, universe: list, prices: pd.DataFrame
    ) -> pd.Series:
        """Return SUE series for tickers with a Friday announcement in the last _HOLD_DAYS."""
        df = earnings.copy()
        df.columns = [c.lower().strip() for c in df.columns]

        t_col  = next((c for c in df.columns if c in ('ticker', 'symbol')), None)
        a_col  = next((c for c in df.columns if 'actual' in c), None)
        e_col  = next((c for c in df.columns if 'estimate' in c or 'consensus' in c), None)
        d_col  = next((c for c in df.columns if 'report_date' in c or c == 'date'), None)

        if any(c is None for c in [t_col, a_col, d_col]):
            return pd.Series(dtype=float)

        df = df[[t_col, a_col, *([] if e_col is None else [e_col]), d_col]].copy()
        df.columns = ['ticker', 'actual'] + (['estimate'] if e_col else []) + ['report_date']
        if 'estimate' not in df.columns:
            df['estimate'] = df['actual']

        df['report_date'] = pd.to_datetime(df['report_date'], errors='coerce')
        df = df.dropna(subset=['report_date', 'actual'])
        df['surprise'] = df['actual'] - df['estimate'].fillna(df['actual'])

        # Determine lookback window
        price_dates = (pd.DatetimeIndex(prices.index)
                       if not isinstance(prices.index, pd.DatetimeIndex)
                       else prices.index)
        if len(price_dates) < self._HOLD_DAYS:
            return pd.Series(dtype=float)
        today          = price_dates[-1]
        lookback_start = price_dates[-self._HOLD_DAYS]

        friday_in_window = set(
            df.loc[
                (df['report_date'] >= lookback_start) &
                (df['report_date'] <= today) &
                (df['report_date'].dt.dayofweek == 4),
                'ticker'
            ].values
        ) & set(universe)

        if not friday_in_window:
            return pd.Series(dtype=float)

        sue_vals = {}
        for tkr, grp in df[df['ticker'].isin(friday_in_window)].groupby('ticker'):
            window_surprises = grp.sort_values('report_date')['surprise'].dropna().values
            if len(window_surprises) < 2:
                continue
            w   = window_surprises[-self._SUE_WINDOW:]
            std = float(np.std(w, ddof=1))
            if std < 1e-8:
                continue
            sue_vals[tkr] = float(w[-1]) / std

        return pd.Series(sue_vals)


# ── Standalone backtest (regime-partition required for lifecycle promotion) ───
if __name__ == '__main__':
    import os
    import json as _json
    _sys_path_root = '/root/openclaw/src'
    if _sys_path_root not in sys.path:
        sys.path.insert(0, _sys_path_root)

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')

    prices_long = pd.read_parquet(f'{PARQUET_ROOT}/prices.parquet',
                                  columns=['ticker', 'date', 'close'])
    prices_long['date'] = pd.to_datetime(prices_long['date'])
    prices_wide = prices_long.pivot_table(
        index='date', columns='ticker', values='close', aggfunc='last'
    )

    earn_df  = pd.read_parquet(f'{PARQUET_ROOT}/earnings.parquet')
    regimes  = pd.read_parquet(f'{PARQUET_ROOT}/historical_regimes.parquet')
    regimes['date'] = pd.to_datetime(regimes['date'])
    regime_map = regimes.set_index('date')['regime']

    strat = FridayEarningsInattentionDrift()
    dates = prices_wide.loc['2017-01-01':'2025-12-31'].index
    records = []

    for i in range(strat.min_lookback, len(dates) - strat._HOLD_DAYS, 21):
        sd    = dates[i]
        rs    = str(regime_map.asof(sd)) if sd >= regime_map.index[0] else 'LOW_VOL'
        if pd.isna(rs) or rs == 'nan':
            rs = 'LOW_VOL'
        window = prices_wide.iloc[:i + 1]
        sigs   = strat.generate_signals(
            prices=window, regime={'state': rs},
            universe=list(prices_wide.columns),
            aux_data={'earnings': earn_df},
        )
        if not sigs:
            continue
        exit_date = dates[min(i + strat._HOLD_DAYS, len(dates) - 1)]
        for sig in sigs:
            t = sig.ticker
            if t not in prices_wide.columns:
                continue
            ep = sig.entry_price
            xp = prices_wide.loc[exit_date, t] if exit_date in prices_wide.index else np.nan
            if pd.isna(xp) or ep <= 0:
                continue
            ret  = (xp - ep) / ep
            pnl  = ret if sig.direction == 'LONG' else -ret
            sdist = abs(ep - sig.stop_loss)
            rmult = pnl / (sdist / ep) if sdist > 0 else 0.0
            records.append({
                'strategy_id': STRATEGY_ID,
                'signal_date': str(sd.date()),
                'regime_state': rs,
                'pnl': float(pnl),
                'r_multiple': float(rmult),
            })

    trades_df = pd.DataFrame(records)
    if trades_df.empty:
        print('No trades in backtest window — check earnings data coverage', file=sys.stderr)
        sys.exit(1)

    from backtest.quick_backtest import run_backtest_with_regime_partition
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(_json.dumps({
        'trade_count':               len(trades_df),
        'mean_pnl':                  round(float(trades_df['pnl'].mean()), 4),
        'eligible_regimes_proposed': result['eligible_regimes_proposed'],
        'regime_partition': {
            r: {k: round(v, 4) if isinstance(v, float) else v
                for k, v in s.items()}
            for r, s in result['regime_partition'].items()
        },
    }, indent=2))
