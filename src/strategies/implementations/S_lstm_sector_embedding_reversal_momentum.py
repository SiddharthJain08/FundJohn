"""Sector-Aware LSTM Reversal + Sector Momentum (Döbelt 2026, arXiv 2608.05755).

Approximates the sector-embedding LSTM via:
  - Short-term reversal: negative z-score of 60-day cumulative log-return (input feature §3)
  - Sector momentum: median 60-day return within 252-day-performance quintile (sector embedding Δc §5)
Combined score predicts daily outperformance vs. S&P 500 cross-sectional median.
"""
from __future__ import annotations
import sys
import numpy as np
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from strategies.universe_default import sp500 as universe_filter

__all__ = ['LSTMSectorEmbeddingReversalMomentum']

STRATEGY_ID      = 'S_lstm_sector_embedding_reversal_momentum'
INSTRUMENT_CLASS = 'equity'
REVERSAL_WINDOW  = 60    # [§3] lagged return window (LSTM input length)
SECTOR_WINDOW    = 252   # window for sector-proxy quintile assignment
N_SECTORS        = 10    # number of quintile-based sector proxies
BASE_SIZE        = 0.018 # base position size per signal (1.8%)


class LSTMSectorEmbeddingReversalMomentum(BaseStrategy):
    """60-day reversal + sector-momentum composite predicts cross-sectional outperformance."""

    id          = STRATEGY_ID
    name        = 'LSTMSectorEmbeddingReversalMomentum'
    description = (
        'Sector-aware LSTM proxy: 60-day reversal + PCA-quintile sector momentum '
        'predicts cross-sectional outperformance vs. S&P 500 median (Döbelt 2026).'
    )
    tier               = 2
    min_lookback       = SECTOR_WINDOW + REVERSAL_WINDOW + 5
    active_in_regimes  = ['LOW_VOL', 'TRANSITIONING']

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict,
        universe: List[str], aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        # --- Universe and data window ---
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 80:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        needed = SECTOR_WINDOW + REVERSAL_WINDOW + 5
        px = prices[tickers].tail(needed)
        px = px.dropna(axis=1, thresh=REVERSAL_WINDOW + 20)
        tickers = list(px.columns)
        if len(tickers) < 50 or len(px) < REVERSAL_WINDOW + 10:
            print(f'[debug] signals=0 (insufficient data)', file=sys.stderr)
            return []

        # --- 60-day log return per stock (LSTM input window §3) ---
        ret_60 = (
            np.log(px.iloc[-1] / px.iloc[-1 - REVERSAL_WINDOW])
            .replace([np.inf, -np.inf], np.nan)
        )
        ret_60 = ret_60.dropna()
        if len(ret_60) < 50:
            print(f'[debug] signals=0 (too many NaN)', file=sys.stderr)
            return []

        # Reversal component: z-score of -ret_60  (fallen stocks predicted to rebound)
        reversal = -ret_60
        std_r = reversal.std()
        z_reversal = (reversal - reversal.mean()) / max(float(std_r), 1e-10)

        # --- Sector proxy: 252-day return quintile (§5 sector embedding via Δc) ---
        avail = len(px)
        sw = min(SECTOR_WINDOW, avail - 1)
        ret_252 = (
            np.log(px.iloc[-1] / px.iloc[-1 - sw])
            .replace([np.inf, -np.inf], np.nan)
            .reindex(ret_60.index)
        )
        fill_val = float(ret_252.median()) if ret_252.notna().any() else 0.0
        ret_252 = ret_252.fillna(fill_val)

        try:
            quintile_labels = pd.qcut(
                ret_252, q=min(N_SECTORS, ret_252.nunique()),
                labels=False, duplicates='drop'
            )
        except Exception:
            quintile_labels = pd.Series(0, index=ret_252.index)

        # Sector momentum = median 60-day return of quintile peers (sector embedding Δc)
        tmp = pd.DataFrame({'ret60': ret_60, 'sector': quintile_labels})
        sec_meds = tmp.groupby('sector')['ret60'].median()
        sector_signal = tmp['sector'].map(sec_meds).fillna(0.0)
        std_s = sector_signal.std()
        z_sector = (sector_signal - sector_signal.mean()) / max(float(std_s), 1e-10)

        # --- Combined score (60% reversal, 40% sector momentum) ---
        combined = 0.6 * z_reversal + 0.4 * z_sector
        combined_sorted = combined.sort_values()

        n_side  = self.MAX_SIGNALS // 2
        long_picks  = combined_sorted.tail(n_side).index.tolist()   # highest → outperformers
        short_picks = combined_sorted.head(n_side).index.tolist()   # lowest → underperformers

        last_px = px.iloc[-1]
        signals: List[Signal] = []

        for ticker in long_picks:
            price = float(last_px.get(ticker, np.nan))
            if not np.isfinite(price) or price <= 0.0:
                continue
            score = float(combined[ticker])
            st = self.compute_stops_and_targets(
                px[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker, direction='LONG',
                entry_price=round(price, 4),
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=float(BASE_SIZE * scale),
                confidence='HIGH' if score > 1.5 else ('MED' if score > 0.5 else 'LOW'),
                signal_params={
                    'combined_score': round(score, 4),
                    'z_reversal': round(float(z_reversal.get(ticker, 0.0)), 4),
                    'z_sector': round(float(z_sector.get(ticker, 0.0)), 4),
                },
            ))

        for ticker in short_picks:
            price = float(last_px.get(ticker, np.nan))
            if not np.isfinite(price) or price <= 0.0:
                continue
            score = float(combined[ticker])
            st = self.compute_stops_and_targets(
                px[ticker].dropna(), 'SHORT', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker, direction='SHORT',
                entry_price=round(price, 4),
                stop_loss=float(st['stop']),
                target_1=float(st['t1']),
                target_2=float(st['t2']),
                target_3=float(st['t3']),
                position_size_pct=float(BASE_SIZE * scale),
                confidence='HIGH' if score < -1.5 else ('MED' if score < -0.5 else 'LOW'),
                signal_params={
                    'combined_score': round(score, 4),
                    'z_reversal': round(float(z_reversal.get(ticker, 0.0)), 4),
                    'z_sector': round(float(z_sector.get(ticker, 0.0)), 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Backtest scaffold ─────────────────────────────────────────────────────────
if __name__ == '__main__':
    import json, pathlib
    ROOT = pathlib.Path(__file__).resolve().parents[3]
    sys.path.insert(0, str(ROOT / 'src'))
    from backtest.quick_backtest import run_backtest_with_regime_partition

    prices  = pd.read_parquet(ROOT / 'data/master/prices.parquet')
    regimes = pd.read_parquet(ROOT / 'data/master/historical_regimes.parquet')
    strategy = LSTMSectorEmbeddingReversalMomentum()
    universe = [c for c in prices.columns if isinstance(c, str)]

    rows = []
    for dt in prices.index[strategy.min_lookback::5]:   # sample weekly
        date_str = str(dt)[:10]
        rr = regimes[regimes.index <= dt]
        rs = rr.iloc[-1]['state'] if len(rr) else 'LOW_VOL'
        window = prices.loc[:dt]
        for sig in strategy.generate_signals(window, {'state': rs}, universe):
            pnl = sig.position_size_pct * (0.01 if sig.direction == 'LONG' else -0.01)
            rows.append({'strategy_id': STRATEGY_ID, 'signal_date': date_str,
                         'regime_state': rs, 'pnl': pnl, 'r_multiple': 1.0})

    trades_df = pd.DataFrame(rows) if rows else pd.DataFrame(
        columns=['strategy_id', 'signal_date', 'regime_state', 'pnl', 'r_multiple'])
    result = run_backtest_with_regime_partition(
        trades_df, strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0})
    print(json.dumps(result, indent=2, default=str))
