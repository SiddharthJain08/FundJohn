from __future__ import annotations
import sys
import pandas as pd
from typing import List
from strategies.base import BaseStrategy, Signal, REGIME_POSITION_SCALE, REGIME_ATR_SCALE
from strategies.confirmation.sector_map import TICKER_SECTOR
from src.strategies.universe_default import r1000 as universe_filter

__all__ = ['SectorNeutralDebtEquity']

INSTRUMENT_CLASS = 'equity'


class SectorNeutralDebtEquity(BaseStrategy):
    """Sector-neutral debt-to-equity rank: within each GICS sector, LONG the
    lowest-leverage decile and SHORT the highest-leverage decile (QuantRocket
    2023, 'Sector Neutralization: Why It Matters and How to Use It')."""

    id          = 'S_sector_neutral_debt_equity'
    name        = 'SectorNeutralDebtEquity'
    description = ('Sector-neutral rank of debt_equity_ratio: LONG low-leverage, '
                    'SHORT high-leverage names within each sector')
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']
    min_lookback = 20

    def default_parameters(self):
        return {
            'decile_frac':  0.3,   # fraction of each sector taken long/short
            'min_sector_n': 4,     # minimum names in a sector to rank it
            'base_size':    0.04,  # base position fraction per leg
        }

    def generate_signals(
        self,
        prices:   pd.DataFrame,
        regime:   dict,
        universe: List[str],
        aux_data: dict = None,
    ) -> List[Signal]:
        if prices is None or prices.empty:
            print('[debug] signals=0 (empty prices)', file=sys.stderr)
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            print(f'[debug] signals=0 (regime={regime_state} not active)', file=sys.stderr)
            return []
        scale = self.position_scale(regime_state)
        p = self.parameters

        de_by_ticker = self._latest_debt_equity(aux_data)
        if not de_by_ticker:
            print('[debug] signals=0 (no debt_equity_ratio data)', file=sys.stderr)
            return []

        signals: List[Signal] = []
        try:
            by_sector: dict = {}
            for ticker in universe:
                sector = TICKER_SECTOR.get(ticker)
                if sector is None or ticker not in prices.columns:
                    continue
                de = de_by_ticker.get(ticker)
                if de is None:
                    continue
                series = prices[ticker].dropna()
                if len(series) < 20 or float(series.iloc[-1]) <= 0:
                    continue
                by_sector.setdefault(sector, []).append((ticker, de))

            for sector, members in by_sector.items():
                n = len(members)
                if n < p['min_sector_n']:
                    continue
                ranked = sorted(members, key=lambda x: x[1])  # ascending debt/equity
                k = max(1, int(round(n * p['decile_frac'])))
                long_bucket  = ranked[:k]           # lowest relative leverage
                short_bucket = ranked[-k:]          # highest relative leverage

                for ticker, de in long_bucket:
                    sig = self._make_signal(ticker, 'LONG', de, prices, regime_state, scale, p)
                    if sig:
                        signals.append(sig)
                for ticker, de in short_bucket:
                    sig = self._make_signal(ticker, 'SHORT', de, prices, regime_state, scale, p)
                    if sig:
                        signals.append(sig)
        except Exception as e:
            print(f'[debug] signals=0 (error: {e})', file=sys.stderr)
            return []

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals

    def _make_signal(self, ticker, direction, de_ratio, prices, regime_state, scale, p):
        series = prices[ticker].dropna()
        current_price = float(series.iloc[-1])
        stops = self.compute_stops_and_targets(
            series, direction, current_price, regime_state=regime_state
        )
        return Signal(
            ticker=ticker,
            direction=direction,
            entry_price=current_price,
            stop_loss=stops['stop'],
            target_1=stops['t1'],
            target_2=stops['t2'],
            target_3=stops['t3'],
            position_size_pct=round(p['base_size'] * scale, 4),
            confidence='MED',
            signal_params={
                'sector': TICKER_SECTOR.get(ticker),
                'debt_equity_ratio': round(float(de_ratio), 4),
                'regime': regime_state,
            },
        )

    def _latest_debt_equity(self, aux_data) -> dict:
        """Most recent debt_equity_ratio per ticker from financials."""
        if not aux_data or not isinstance(aux_data.get('financials'), pd.DataFrame):
            return {}
        fin = aux_data['financials']
        if 'ticker' not in fin.columns or 'debt_equity_ratio' not in fin.columns:
            return {}
        fin = fin.dropna(subset=['debt_equity_ratio'])
        if fin.empty:
            return {}
        if 'date' in fin.columns:
            fin = fin.sort_values('date')
        latest = fin.groupby('ticker')['debt_equity_ratio'].last()
        return latest.to_dict()
