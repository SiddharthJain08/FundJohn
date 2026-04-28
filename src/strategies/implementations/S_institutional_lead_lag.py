from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE

__all__ = ['InstitutionalLeadLag']


class InstitutionalLeadLag(BaseStrategy):
    """Lagged high-inst-ownership returns predict low-inst-ownership returns (Badrinath, Kale, Noe 1995)."""

    id                = 'S_institutional_lead_lag'
    name              = 'InstitutionalLeadLag'
    description       = 'Lagged returns of high-inst-ownership stocks predict low-inst-ownership stocks via informed-trader lead-lag effect.'
    tier              = 2
    signal_frequency  = 'weekly'
    min_lookback      = 504
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    QUARTILE_FRAC   = 0.25
    WEEKLY_DAYS     = 5       # lag window: 1 calendar week ≈ 5 trading days
    NOISE_BAND      = 0.001   # skip if |lead_signal| below this
    BASE_SIZE_LONG  = 0.012
    BASE_SIZE_SHORT = 0.010

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

        financials = (aux_data or {}).get('financials') or {}
        if not financials:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Filter universe to tickers present in both prices and financials
        tickers = [t for t in universe if t in prices.columns]
        if len(tickers) < 50:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        if len(prices) < self.WEEKLY_DAYS + 2:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Build institutional ownership series {ticker: inst_own_pct}
        inst_own = {}
        for t in tickers:
            fin = financials.get(t) or {}
            val = fin.get('institutionalOwnershipPercent') or fin.get('institutional_ownership_pct')
            if val is not None:
                try:
                    inst_own[t] = float(val)
                except (TypeError, ValueError):
                    pass

        if len(inst_own) < 20:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        inst_series = pd.Series(inst_own)
        rank_pct    = inst_series.rank(pct=True)

        high_inst = rank_pct[rank_pct >= (1.0 - self.QUARTILE_FRAC)].index.tolist()
        low_inst  = rank_pct[rank_pct <= self.QUARTILE_FRAC].index.tolist()

        if not high_inst or not low_inst:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        # Lagged 1-week equal-weight return of high-inst portfolio
        # Window: prices[-WEEKLY_DAYS-1] → prices[-1]  (then signal at prices[-1])
        prices_sub     = prices[tickers]
        hi_cols        = [t for t in high_inst if t in prices_sub.columns]
        if not hi_cols:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        hi_window = prices_sub[hi_cols].iloc[-(self.WEEKLY_DAYS + 2):].ffill()
        hi_window = hi_window.dropna(axis=1, how='all')
        if hi_window.empty or len(hi_window) < self.WEEKLY_DAYS + 1:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        p_start = hi_window.iloc[0].replace(0, float('nan'))
        p_end   = hi_window.iloc[self.WEEKLY_DAYS]
        weekly_rets = ((p_end - p_start) / p_start).dropna()
        if weekly_rets.empty:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        lead_signal = float(weekly_rets.mean())
        if abs(lead_signal) < self.NOISE_BAND:
            print(f'[debug] signals=0', file=sys.stderr)
            return []

        direction      = 'LONG' if lead_signal > 0 else 'SHORT'
        current_prices = prices_sub.iloc[-1]
        scale          = self.position_scale(regime_state)
        base_size      = self.BASE_SIZE_LONG if direction == 'LONG' else self.BASE_SIZE_SHORT

        abs_sig = abs(lead_signal)
        conf    = 'HIGH' if abs_sig >= 0.02 else ('MED' if abs_sig >= 0.01 else 'LOW')

        signals: List[Signal] = []
        for ticker in low_inst:
            if len(signals) >= self.MAX_SIGNALS:
                break
            if ticker not in prices_sub.columns:
                continue
            raw_price = current_prices.get(ticker)
            if raw_price is None or raw_price != raw_price or raw_price <= 0:
                continue
            price = float(raw_price)
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), direction, price, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ticker,
                direction=direction,
                entry_price=price,
                stop_loss=stops['stop'],
                target_1=stops['t1'],
                target_2=stops['t2'],
                target_3=stops['t3'],
                position_size_pct=round(base_size * scale, 6),
                confidence=conf,
                signal_params={
                    'lead_signal': round(lead_signal, 6),
                    'high_inst_n': len(hi_cols),
                    'low_inst_n': len(low_inst),
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
