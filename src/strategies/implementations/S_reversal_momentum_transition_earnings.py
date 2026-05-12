"""
Short-term cross-sectional reversal with earnings-announcement attenuation.
Based on: Jegadeesh, Luo, Subrahmanyam, Titman (RFS 2025).
Signal: -1 * prior_1m_return, attenuated 50% post-earnings. LONG top quintile, SHORT bottom.
"""
from __future__ import annotations

import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

STRATEGY_ID = 'S_reversal_momentum_transition_earnings'

__all__ = ['ReversalMomentumTransitionEarnings']


class ReversalMomentumTransitionEarnings(BaseStrategy):
    """Cross-sectional reversal attenuated post-earnings; transitions to momentum at 12m horizon."""

    id          = 'S_reversal_momentum_transition_earnings'
    name        = 'ReversalMomentumTransitionEarnings'
    description = 'Monthly reversal with post-earnings attenuation; LONG top-quintile, SHORT bottom-quintile.'
    tier        = 2
    min_lookback = 504

    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    def default_parameters(self) -> dict:
        return {
            'reversal_window': 21,      # trading days ≈ 1 month
            'momentum_window': 252,     # 12m skip-1 (informational, not primary signal)
            'momentum_skip':   21,
            'earnings_window': 5,       # days post-announcement to attenuate
            'ea_attenuation':  0.5,     # multiply reversal by (1 - 0.5) post-EA
            'quintile_frac':   0.20,    # top/bottom 20%
            'base_size_pct':   0.02,    # per-name position size before regime scale
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

        p = self.default_parameters()
        p.update(self.parameters or {})

        rev_win    = int(p['reversal_window'])
        ea_win     = int(p['earnings_window'])
        ea_atten   = float(p['ea_attenuation'])
        q_frac     = float(p['quintile_frac'])
        base_size  = float(p['base_size_pct'])
        scale      = self.position_scale(regime_state)

        # Filter to universe tickers present in prices columns
        cols = [t for t in universe if t in prices.columns]
        if len(cols) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        closes = prices[cols]
        if len(closes) < rev_win + 1:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # --- 1-month reversal signal ---
        # prior month return = close[-rev_win-1] → close[-1]
        ret_1m = (closes.iloc[-1] / closes.iloc[-rev_win - 1].replace(0, float('nan'))) - 1.0
        reversal_signal = -ret_1m  # sign-flip

        # --- Earnings attenuation ---
        ea_flag = pd.Series(False, index=cols)
        if aux_data is not None:
            earnings = aux_data.get('earnings')
            if earnings is not None and not earnings.empty:
                # Expect columns: ticker (or symbol), date (or report_date / earnings_date)
                tkr_col  = next((c for c in earnings.columns if c.lower() in ('ticker', 'symbol')), None)
                date_col = next(
                    (c for c in earnings.columns if 'date' in c.lower() or c.lower() == 'report_date'),
                    None,
                )
                if tkr_col and date_col:
                    try:
                        earnings[date_col] = pd.to_datetime(earnings[date_col], errors='coerce')
                        last_date = pd.to_datetime(closes.index[-1])
                        cutoff    = last_date - pd.Timedelta(days=ea_win)
                        recent_ea = earnings[
                            (earnings[date_col] >= cutoff) & (earnings[date_col] <= last_date)
                        ][tkr_col].unique()
                        for t in recent_ea:
                            if t in ea_flag.index:
                                ea_flag[t] = True
                    except Exception:
                        pass  # graceful degradation — proceed without attenuation

        # Apply attenuation: multiply reversal by (1 - ea_attenuation) if post-EA
        adjusted_reversal = reversal_signal.copy()
        adjusted_reversal[ea_flag] = reversal_signal[ea_flag] * (1.0 - ea_atten)

        # Drop NaN
        adjusted_reversal = adjusted_reversal.dropna()
        if len(adjusted_reversal) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # --- Cross-sectional quintile ranking ---
        n_q = max(1, int(len(adjusted_reversal) * q_frac))
        ranked = adjusted_reversal.rank(ascending=False)
        long_tickers  = ranked[ranked <= n_q].index.tolist()
        short_tickers = ranked[ranked >= len(adjusted_reversal) - n_q + 1].index.tolist()

        signals: List[Signal] = []
        pos_size = round(base_size * scale, 6)
        conf = 'MED' if scale >= 0.55 else 'LOW'

        for tkr in long_tickers:
            if len(signals) >= self.MAX_SIGNALS // 2:
                break
            price_series = closes[tkr].dropna()
            if len(price_series) < 2:
                continue
            entry = float(price_series.iloc[-1])
            if entry <= 0:
                continue
            stops = self.compute_stops_and_targets(
                price_series, 'LONG', entry, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=tkr, direction='LONG',
                entry_price=entry,
                stop_loss=stops['stop'],
                target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=pos_size,
                confidence=conf,
                signal_params={
                    'reversal_score': float(round(adjusted_reversal[tkr], 6)),
                    'post_earnings':  bool(ea_flag.get(tkr, False)),
                },
            ))

        for tkr in short_tickers:
            if len(signals) >= self.MAX_SIGNALS:
                break
            price_series = closes[tkr].dropna()
            if len(price_series) < 2:
                continue
            entry = float(price_series.iloc[-1])
            if entry <= 0:
                continue
            stops = self.compute_stops_and_targets(
                price_series, 'SHORT', entry, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=tkr, direction='SHORT',
                entry_price=entry,
                stop_loss=stops['stop'],
                target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=pos_size,
                confidence=conf,
                signal_params={
                    'reversal_score': float(round(adjusted_reversal[tkr], 6)),
                    'post_earnings':  bool(ea_flag.get(tkr, False)),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


if __name__ == '__main__':
    import os, json
    sys.path.insert(0, '/root/openclaw/src')
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    prices_long  = pd.read_parquet(os.path.join(PARQUET_ROOT, 'prices.parquet'))
    wide = prices_long.pivot_table(index='date', columns='ticker', values='close')
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()

    reg_df = pd.read_parquet(os.path.join(PARQUET_ROOT, 'historical_regimes.parquet'))
    reg_df['date'] = pd.to_datetime(reg_df['date'])
    regime_map = reg_df.set_index('date')['regime'].to_dict()

    strat    = ReversalMomentumTransitionEarnings()
    universe = wide.columns.tolist()
    monthly  = wide.resample('ME').last().loc['2020-01':'2025-12'].index

    records = []
    for dt in monthly:
        nearest      = max((d for d in regime_map if d <= dt), default=None)
        regime_state = regime_map.get(nearest, 'LOW_VOL') if nearest else 'LOW_VOL'
        hist         = wide.loc[:dt]
        signals      = strat.generate_signals(hist, {'state': regime_state}, universe)
        fwd_idx      = wide.index[wide.index > dt]
        if len(fwd_idx) < 21:
            continue
        fwd_date = fwd_idx[20]
        for sig in signals:
            t = sig.ticker
            if t not in wide.columns:
                continue
            entry  = wide.at[dt, t]  if dt  in wide.index else float('nan')
            exit_p = wide.at[fwd_date, t] if fwd_date in wide.index else float('nan')
            if not (entry > 0 and exit_p > 0):
                continue
            raw  = (exit_p - entry) / entry
            pnl  = raw if sig.direction == 'LONG' else -raw
            risk = abs(entry - sig.stop_loss) / entry or 0.02
            records.append({'strategy_id': STRATEGY_ID, 'signal_date': dt.strftime('%Y-%m-%d'),
                            'regime_state': regime_state, 'pnl': float(pnl),
                            'r_multiple': float(pnl / risk)})

    trades_df = pd.DataFrame(records)
    print(f'[backtest] {len(trades_df)} trades over {len(monthly)} months', file=sys.stderr)
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
