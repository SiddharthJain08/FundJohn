from __future__ import annotations
import sys
import pandas as pd
import numpy as np
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from src.strategies.universe_default import sp500 as universe_filter

__all__ = ['LSTMSectorEmbeddingSP500']

INSTRUMENT_CLASS = "equity"
STRATEGY_ID = "S_lstm_sector_embedding_sp500"


class LSTMSectorEmbeddingSP500(BaseStrategy):
    """Sector-aware LSTM proxy: 60d reversal + 12m industry momentum cross-sectional rank.

    Approximates Döbelt (2026) — learnable sector embeddings for S&P 500 directional
    forecasting — using rank-based combination of short-term reversal (-ret_5d) and
    industry momentum (ret_12m_skip_1m). Macro covariates (VIX) gate signal aggressiveness.
    LONG top quartile, SHORT bottom quartile of composite cross-sectional score.
    """

    id               = STRATEGY_ID
    name             = 'LSTMSectorEmbeddingSP500'
    description      = (
        'Sector-aware LSTM proxy: 60d reversal + 12m industry momentum '
        'cross-sectional long/short on S&P 500'
    )
    tier             = 2
    signal_frequency = 'weekly'
    min_lookback     = 756   # per spec — ~3 years for stable cross-section
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    # Cross-section sizing — LONG/SHORT top+bottom quartile
    LONG_QUARTILE  = 0.75   # top 25%
    SHORT_QUARTILE = 0.25   # bottom 25%
    BASE_SIZE_LONG  = 0.008
    BASE_SIZE_SHORT = 0.007

    # Signal weights: reversal + industry momentum (paper §3.2 feature importance)
    W_REVERSAL = 0.40
    W_MOMENTUM = 0.60

    # Reversal lookback (5d) and momentum formation (252d skip 21d)
    REV_DAYS   = 5
    MOM_DAYS   = 252
    SKIP_DAYS  = 21

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
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Weekly rebalance gate — fire on Monday (or cadence_reset)
        last_date = prices.index[-1] if hasattr(prices.index[-1], 'weekday') else None
        if last_date is not None and not self.cadence_reset(regime):
            if last_date.weekday() != 0:  # 0 = Monday
                print(f'[debug] signals=0 (non-rebalance day)', file=sys.stderr)
                return []

        scale = self.position_scale(regime_state)

        # Filter to available universe tickers with sufficient history
        min_rows = self.MOM_DAYS + self.SKIP_DAYS + 5
        tickers = [
            t for t in universe
            if t in prices.columns and prices[t].dropna().shape[0] >= min_rows
        ]
        if len(tickers) < 50:
            print(f'[debug] signals=0 (universe too small: {len(tickers)})', file=sys.stderr)
            return []

        prices_sub = prices[tickers].copy()

        # ── Feature 1: Short-term reversal (-ret_5d) ─────────────────────────
        # Paper §3.2: reversal anomaly — recent 5-day losers expected to revert up
        if len(prices_sub) < self.REV_DAYS + 2:
            print(f'[debug] signals=0 (insufficient rows)', file=sys.stderr)
            return []

        p_now    = prices_sub.iloc[-1].replace(0, np.nan)
        p_5d_ago = prices_sub.iloc[-1 - self.REV_DAYS].replace(0, np.nan)
        ret_5d   = ((p_now - p_5d_ago) / p_5d_ago).dropna()
        # Reversal: negate 5d return → recent losers score HIGH
        reversal = -ret_5d

        # ── Feature 2: Industry momentum (ret_12m skip 1m) ───────────────────
        # Paper §3.2: industry/sector momentum from 12m trailing return (skip 1m)
        end_idx   = len(prices_sub) - 1 - self.SKIP_DAYS
        start_idx = end_idx - self.MOM_DAYS
        if start_idx < 0:
            print(f'[debug] signals=0 (insufficient history for momentum)', file=sys.stderr)
            return []

        p_end   = prices_sub.iloc[end_idx].replace(0, np.nan)
        p_start = prices_sub.iloc[start_idx].replace(0, np.nan)
        mom_12m = ((p_end - p_start) / p_start).dropna()

        # ── Macro gate: VIX-based confidence scale ────────────────────────────
        macro_scale = 1.0
        if aux_data:
            macro = aux_data.get('macro')
            if macro is not None and not macro.empty:
                vix_col = next((c for c in macro.columns if 'VIX' in c.upper() or 'vix' in c.lower()), None)
                if vix_col:
                    vix_val = float(macro[vix_col].dropna().iloc[-1]) if macro[vix_col].dropna().shape[0] > 0 else 20.0
                    # Dampen signal aggressiveness when VIX > 25
                    macro_scale = max(0.4, 1.0 - max(0.0, (vix_val - 20.0) / 50.0))

        # ── Composite cross-sectional score ──────────────────────────────────
        common = reversal.index.intersection(mom_12m.index)
        if len(common) < 50:
            print(f'[debug] signals=0 (too few common tickers: {len(common)})', file=sys.stderr)
            return []

        rev_rank  = reversal[common].rank(pct=True)
        mom_rank  = mom_12m[common].rank(pct=True)
        composite = self.W_REVERSAL * rev_rank + self.W_MOMENTUM * mom_rank

        # LONG top quartile, SHORT bottom quartile (cross-sectional split)
        longs  = composite[composite >= self.LONG_QUARTILE].sort_values(ascending=False)
        shorts = composite[composite <= self.SHORT_QUARTILE].sort_values(ascending=True)

        current_prices = prices_sub.iloc[-1]
        size_long  = round(self.BASE_SIZE_LONG  * scale * macro_scale, 6)
        size_short = round(self.BASE_SIZE_SHORT * scale * macro_scale, 6)

        def conf(pct: float) -> str:
            if pct >= 0.95 or pct <= 0.05:
                return 'HIGH'
            elif pct >= 0.85 or pct <= 0.15:
                return 'MED'
            return 'LOW'

        signals: List[Signal] = []

        for ticker, score in longs.items():
            if len(signals) >= self.MAX_SIGNALS // 2:
                break
            raw_price = current_prices.get(ticker)
            if raw_price is None or raw_price != raw_price or raw_price <= 0:
                continue
            price = float(raw_price)
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'LONG', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='LONG',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size_long,
                confidence=conf(float(score)),
                signal_params={
                    'composite_score': round(float(score), 4),
                    'reversal_rank':   round(float(rev_rank.get(ticker, 0.5)), 4),
                    'mom_12m_rank':    round(float(mom_rank.get(ticker, 0.5)), 4),
                    'macro_scale':     round(macro_scale, 4),
                },
            ))

        for ticker, score in shorts.items():
            if len(signals) >= self.MAX_SIGNALS:
                break
            raw_price = current_prices.get(ticker)
            if raw_price is None or raw_price != raw_price or raw_price <= 0:
                continue
            price = float(raw_price)
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), 'SHORT', price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction='SHORT',
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=size_short,
                confidence=conf(float(score)),
                signal_params={
                    'composite_score': round(float(score), 4),
                    'reversal_rank':   round(float(rev_rank.get(ticker, 0.5)), 4),
                    'mom_12m_rank':    round(float(mom_rank.get(ticker, 0.5)), 4),
                    'macro_scale':     round(macro_scale, 4),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ── Backtest entry point ──────────────────────────────────────────────────────
def _run_backtest():
    """Walk-forward backtest using historical prices + regime data."""
    import os
    import json
    from backtest.quick_backtest import run_backtest_with_regime_partition

    PARQUET_ROOT = os.environ.get('OPENCLAW_PARQUET_ROOT', '/root/openclaw/data/master')
    prices_path  = os.path.join(PARQUET_ROOT, 'prices.parquet')
    regime_path  = os.path.join(PARQUET_ROOT, 'historical_regimes.parquet')

    if not os.path.exists(prices_path) or not os.path.exists(regime_path):
        print('[backtest] required parquets not found — skipping', file=sys.stderr)
        return

    prices  = pd.read_parquet(prices_path)
    regimes = pd.read_parquet(regime_path)

    prices.index  = pd.to_datetime(prices.index)
    regimes.index = pd.to_datetime(regimes.index)

    strategy = LSTMSectorEmbeddingSP500()
    REV_DAYS  = strategy.REV_DAYS
    MOM_DAYS  = strategy.MOM_DAYS
    SKIP_DAYS = strategy.SKIP_DAYS
    MIN_ROWS  = MOM_DAYS + SKIP_DAYS + 5

    # Rebalance on Mondays only
    trading_days = prices.index
    rebalance_days = [d for d in trading_days if d.weekday() == 0 and
                      trading_days.get_loc(d) >= MIN_ROWS]

    trades = []
    for rb_date in rebalance_days:
        loc = trading_days.get_loc(rb_date)
        window = prices.iloc[:loc + 1]

        # Get regime for this date
        regime_row = regimes[regimes.index <= rb_date]
        regime_state = regime_row['state'].iloc[-1] if (not regime_row.empty and 'state' in regime_row.columns) else 'LOW_VOL'

        regime = {'state': regime_state}
        universe = list(window.columns)

        sigs = strategy.generate_signals(window, regime, universe)
        if not sigs:
            continue

        # Estimate 5-day forward return as PnL proxy
        if loc + 5 >= len(trading_days):
            continue
        fwd_prices = prices.iloc[loc + 5]

        for sig in sigs:
            if sig.ticker not in fwd_prices.index:
                continue
            entry = sig.entry_price
            fwd   = float(fwd_prices[sig.ticker]) if fwd_prices[sig.ticker] == fwd_prices[sig.ticker] else entry
            if entry <= 0:
                continue
            ret = (fwd - entry) / entry
            pnl = ret if sig.direction == 'LONG' else -ret
            stop_dist = abs(entry - sig.stop_loss) / entry if entry > 0 else 0.02
            r_mult = pnl / stop_dist if stop_dist > 0 else 0.0
            trades.append({
                'strategy_id': STRATEGY_ID,
                'signal_date': rb_date.strftime('%Y-%m-%d'),
                'regime_state': regime_state,
                'pnl': float(pnl),
                'r_multiple': float(r_mult),
            })

    if not trades:
        print('[backtest] no trades generated', file=sys.stderr)
        return

    trades_df = pd.DataFrame(trades)
    result = run_backtest_with_regime_partition(
        trades_df,
        strategy_id=STRATEGY_ID,
        thresholds={'min_sharpe': 0.5, 'min_trade_count': 20, 'min_avg_r': 0.0},
    )
    print(json.dumps(result, indent=2, default=str))
    return result


if __name__ == '__main__':
    _run_backtest()
