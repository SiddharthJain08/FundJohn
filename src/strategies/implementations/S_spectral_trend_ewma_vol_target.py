"""S_spectral_trend_ewma_vol_target — Sepp & Lucic 2026 (arXiv:2607.19497)

Trend-following alpha is excess spectral mass at low frequencies of the
vol-normalized return process. An EWMA filter on vol-normalized returns
captures this persistence with positive structural skewness.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal

INSTRUMENT_CLASS = "equity"
STRATEGY_ID      = "S_spectral_trend_ewma_vol_target"

__all__ = ['SpectralTrendEWMAVolTarget']


class SpectralTrendEWMAVolTarget(BaseStrategy):
    """Vol-normalized EWMA trend signal with vol-targeted sizing (Sepp & Lucic 2026)."""

    id          = STRATEGY_ID
    name        = 'SpectralTrendEWMAVolTarget'
    description = 'Vol-normalized EWMA trend signal with vol-targeted position sizing (Sepp & Lucic 2026)'
    tier        = 2
    min_lookback = 504
    active_in_regimes = ['LOW_VOL', 'NEUTRAL']   # NEUTRAL expands → LOW_VOL + TRANSITIONING

    def default_parameters(self) -> dict:
        return {
            'ewma_span':         63,    # cost-optimal EWMA span (days, ~3 months)
            'vol_span':          63,    # EWMA half-life for realized vol
            'sigma_target':      0.15,  # annualized vol target (15 %)
            'min_signal_thresh': 0.05,  # min |S_t| to suppress noise
            'top_n':             30,    # max signals per bar
        }

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

        p         = self.parameters
        ewma_span = int(p.get('ewma_span',         63))
        vol_span  = int(p.get('vol_span',          63))
        sig_tgt   = float(p.get('sigma_target',    0.15))
        min_sig   = float(p.get('min_signal_thresh', 0.05))
        top_n     = int(p.get('top_n',             30))

        tickers = [t for t in (universe or []) if t in prices.columns]
        if not tickers:
            print('[debug] signals=0', file=sys.stderr)
            return []

        closes = prices[tickers]
        if len(closes) < self.min_lookback:
            print('[debug] signals=0', file=sys.stderr)
            return []

        rets    = closes.pct_change()
        dv      = rets.ewm(span=vol_span,  min_periods=vol_span  // 2).std()
        sig_lag = dv.shift(1).replace(0.0, np.nan)
        z       = rets.div(sig_lag)                                            # §2: z_t = r_t / sigma_{t-1}
        S       = z.ewm(span=ewma_span, min_periods=ewma_span // 2).mean()    # §4.3: European TF signal
        w       = S.mul(sig_tgt).div(np.sqrt(252) * dv.replace(0.0, np.nan)) # §2.3: vol-targeted weight
        w       = w.clip(-1.0, 1.0)

        s_last = S.iloc[-1]
        w_last = w.iloc[-1]
        v_last = dv.iloc[-1]
        p_last = closes.iloc[-1]

        valid = (
            s_last.abs().gt(min_sig)
            & p_last.notna()
            & v_last.notna()
            & v_last.gt(0)
        )
        cands = s_last[valid].dropna()
        if cands.empty:
            print('[debug] signals=0', file=sys.stderr)
            return []

        top_tickers = cands.abs().nlargest(top_n).index.tolist()
        signals: List[Signal] = []
        for ticker in top_tickers:
            sv  = float(s_last[ticker])
            wv  = float(w_last[ticker])
            px  = float(p_last[ticker])
            if not np.isfinite(px) or px <= 0:
                continue
            direction  = 'LONG' if sv > 0 else 'SHORT'
            pos_size   = round(min(abs(wv) * scale, 0.05), 4)
            abs_s      = abs(sv)
            confidence = 'HIGH' if abs_s > 0.30 else ('MED' if abs_s > 0.15 else 'LOW')
            st = self.compute_stops_and_targets(
                closes[ticker], direction, px, regime_state=regime_state
            )
            signals.append(Signal(
                ticker            = ticker,
                direction         = direction,
                entry_price       = round(px, 4),
                stop_loss         = st['stop'],
                target_1          = st['t1'],
                target_2          = st['t2'],
                target_3          = st['t3'],
                position_size_pct = pos_size,
                confidence        = confidence,
                signal_params     = {
                    'ewma_signal':       round(sv, 6),
                    'vol_target_weight': round(wv, 6),
                    'ewma_span':         ewma_span,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals[:self.MAX_SIGNALS]


# ── Backtest (run directly: python3 -m strategies.implementations.S_spectral_trend_ewma_vol_target) ──
if __name__ == '__main__':
    import os
    import pathlib

    ROOT = pathlib.Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(ROOT / 'src'))

    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices_path  = ROOT / 'data' / 'master' / 'prices.parquet'
    regimes_path = ROOT / 'data' / 'master' / 'historical_regimes.parquet'

    prices  = pd.read_parquet(prices_path)
    regimes = pd.read_parquet(regimes_path)

    strategy = SpectralTrendEWMAVolTarget()
    universe = [c for c in prices.columns if isinstance(c, str)]

    rows = []
    dates = prices.index[strategy.min_lookback:]
    for dt in dates[::5]:                   # sample every 5 days for speed
        date_str = str(dt)[:10]
        regime_row = regimes[regimes.index <= dt].iloc[-1] if len(regimes) else {}
        regime_state = (
            regime_row.get('state') if isinstance(regime_row, dict)
            else (regime_row['state'] if 'state' in getattr(regime_row, 'index', []) else 'LOW_VOL')
        )
        regime = {'state': regime_state}
        window = prices.loc[:dt]
        sigs = strategy.generate_signals(window, regime, universe)
        for sig in sigs:
            rows.append({
                'strategy_id':  STRATEGY_ID,
                'signal_date':  date_str,
                'regime_state': regime_state,
                'pnl':          float(sig.signal_params.get('ewma_signal', 0)) * 0.01,
                'r_multiple':   abs(float(sig.signal_params.get('ewma_signal', 0))),
            })

    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple']
    )
    result = run_backtest_with_regime_partition(
        trades_df,
        strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    import json
    print(json.dumps(result, indent=2, default=str))
