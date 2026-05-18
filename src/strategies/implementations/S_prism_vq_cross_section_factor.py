from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE

__all__ = ['PrismVQCrossSectionFactor']

STRATEGY_ID = 'S_prism_vq_cross_section_factor'

# MoE routing: regime-specific factor weights
# Cols: mom_1m, mom_3m, mom_12_1m, low_vol, quality, value
_MOE_WEIGHTS = {
    'LOW_VOL':       [0.10, 0.15, 0.30, 0.15, 0.20, 0.10],
    'TRANSITIONING': [0.20, 0.25, 0.25, 0.10, 0.15, 0.05],
    'HIGH_VOL':      [0.05, 0.05, 0.10, 0.30, 0.35, 0.15],
    'CRISIS':        [0.00, 0.00, 0.00, 0.40, 0.45, 0.15],
}


class PrismVQCrossSectionFactor(BaseStrategy):
    """VQ discrete latent factor + MoE cross-sectional ranking — LONG/SHORT decile every 5 days."""

    id               = STRATEGY_ID
    name             = 'PrismVQCrossSectionFactor'
    description      = ('VQ-inspired discrete factor encoding + regime-conditioned MoE weights; '
                        'LONG top decile, SHORT bottom decile — rebalance every 5 trading days')
    tier             = 2
    signal_frequency = 'weekly'
    min_lookback     = 252
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL', 'CRISIS']

    REBAL_DAYS  = 5
    DECILE_FRAC = 0.10
    BASE_SIZE   = 0.015

    def generate_signals(
        self,
        prices: pd.DataFrame,
        regime: dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print('[debug] signals=0', file=sys.stderr)
            return []

        # 5-day rebalance gate
        if len(prices) % self.REBAL_DAYS != 0:
            print('[debug] signals=0 (off-cycle)', file=sys.stderr)
            return []

        scale   = self.position_scale(regime_state)
        weights = np.array(_MOE_WEIGHTS.get(regime_state, _MOE_WEIGHTS['LOW_VOL']))

        tickers    = [t for t in universe if t in prices.columns]
        if len(tickers) < 30:
            print('[debug] signals=0 (universe too small)', file=sys.stderr)
            return []

        psub = prices[tickers]
        if len(psub) < 265:
            print('[debug] signals=0', file=sys.stderr)
            return []

        cur  = psub.iloc[-1]
        rets = psub.pct_change()

        # Price-based factors
        safe = lambda df: df.replace(0, np.nan)
        mom_1m    = cur / safe(psub.iloc[-22])  - 1
        mom_3m    = cur / safe(psub.iloc[-63])  - 1
        mom_12_1m = psub.iloc[-22] / safe(psub.iloc[-253]) - 1
        low_vol   = -(rets.iloc[-21:].std() * np.sqrt(252))   # inverse vol
        def quintile_code(series: pd.Series) -> pd.Series:
            ranked = series.rank(pct=True)
            return ranked.apply(lambda x: float(min(int(x * 5), 4)) if pd.notna(x) else 2.0)

        codes = pd.DataFrame({
            'mom_1m':    quintile_code(mom_1m),
            'mom_3m':    quintile_code(mom_3m),
            'mom_12_1m': quintile_code(mom_12_1m),
            'low_vol':   quintile_code(low_vol),
            'quality':   pd.Series(2.0, index=psub.columns),
            'value':     pd.Series(2.0, index=psub.columns),
        })

        # Augment from financials when available
        financials = (aux_data or {}).get('financials', {})
        if financials:
            q_raw, v_raw = {}, {}
            for t in tickers:
                fin = financials.get(t)
                if not fin:
                    continue
                total_a = float(fin.get('totalAssets') or 0)
                net_inc = float(fin.get('netIncome') or 0)
                book_eq = float(fin.get('totalStockholdersEquity') or fin.get('totalEquity') or 0)
                cp      = float(cur.get(t) or 0)
                if total_a > 0:
                    q_raw[t] = net_inc / total_a
                if book_eq > 0 and cp > 0:
                    v_raw[t] = book_eq / (cp * 1e6)
            for raw, col in [(q_raw, 'quality'), (v_raw, 'value')]:
                if raw:
                    s = pd.Series(raw)
                    codes.loc[s.index, col] = quintile_code(s).values

        factor_matrix = codes[['mom_1m', 'mom_3m', 'mom_12_1m', 'low_vol', 'quality', 'value']].values
        composite     = pd.Series(factor_matrix @ weights, index=codes.index)

        n          = len(composite)
        decile_n   = min(max(1, int(n * self.DECILE_FRAC)), self.MAX_SIGNALS // 2)
        sorted_idx = np.argsort(composite.values)
        long_idx   = sorted_idx[-decile_n:]
        short_idx  = sorted_idx[:decile_n]

        per_pos = min(round(self.BASE_SIZE * scale, 6), 0.04)
        signals: List[Signal] = []

        def _conf(score: float, long: bool) -> str:
            if long:
                return 'HIGH' if score >= 3.5 else ('MED' if score >= 2.8 else 'LOW')
            return 'HIGH' if score <= 0.5 else ('MED' if score <= 1.2 else 'LOW')

        for idx in long_idx:
            t = tickers[idx]; ts = psub[t].dropna()
            if len(ts) < 20: continue
            price = float(ts.iloc[-1])
            if price <= 0: continue
            stops = self.compute_stops_and_targets(ts, 'LONG', price, regime_state=regime_state)
            signals.append(Signal(
                ticker=t, direction='LONG', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=per_pos, confidence=_conf(float(composite.iloc[idx]), True),
                signal_params={'composite': round(float(composite.iloc[idx]), 4),
                               'mom_12_1m': round(float(mom_12_1m.get(t, 0)), 4)},
            ))

        for idx in short_idx:
            t = tickers[idx]; ts = psub[t].dropna()
            if len(ts) < 20: continue
            price = float(ts.iloc[-1])
            if price <= 0: continue
            stops = self.compute_stops_and_targets(ts, 'SHORT', price, regime_state=regime_state)
            signals.append(Signal(
                ticker=t, direction='SHORT', entry_price=price,
                stop_loss=stops['stop'], target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=per_pos, confidence=_conf(float(composite.iloc[idx]), False),
                signal_params={'composite': round(float(composite.iloc[idx]), 4),
                               'mom_12_1m': round(float(mom_12_1m.get(t, 0)), 4)},
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

if __name__ == '__main__':
    from backtest.quick_backtest import run_backtest_with_regime_partition
    import os, json
    prices_path = 'data/master/prices.parquet'
    regimes_path = 'data/master/historical_regimes.parquet'
    if not os.path.exists(prices_path) or not os.path.exists(regimes_path):
        print('Missing data files — skipping backtest', file=sys.stderr); sys.exit(0)
    px = pd.read_parquet(prices_path).sort_index()
    reg = pd.read_parquet(regimes_path).sort_index()
    reg.index = pd.to_datetime(reg.index)
    px.index = pd.to_datetime(px.index)

    universe = [c for c in px.columns if c.isalpha() and len(c) <= 5][:500]
    strategy = PrismVQCrossSectionFactor()
    rows = []
    for i in range(265, len(px), 5):
        snap    = px.iloc[:i]
        dt      = snap.index[-1]
        r_row   = reg[reg.index <= dt]
        regime_s = r_row['state'].iloc[-1] if not r_row.empty and 'state' in r_row else 'LOW_VOL'
        signals = strategy.generate_signals(snap, {'state': regime_s}, universe)
        for s in signals:
            fwd = px[s.ticker].reindex(px.index[i:i+5]).dropna()
            if len(fwd) < 1: continue
            ret = (fwd.iloc[-1] - s.entry_price) / s.entry_price
            if s.direction == 'SHORT': ret = -ret
            risk = abs(s.entry_price - s.stop_loss)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': str(dt.date()),
                         'regime_state': regime_s, 'pnl': float(ret),
                         'r_multiple': float(ret * s.entry_price / risk) if risk > 0 else 0.0})

    if not rows:
        print('No trades generated', file=sys.stderr); sys.exit(1)

    result = run_backtest_with_regime_partition(
        pd.DataFrame(rows), strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
