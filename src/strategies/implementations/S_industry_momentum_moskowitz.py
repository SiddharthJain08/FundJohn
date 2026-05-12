from __future__ import annotations
import sys
import pandas as pd
from typing import List, Dict
from strategies.base import BaseStrategy, Signal

__all__ = ['IndustryMomentumMoskowitz']

# GICS sector groupings for liquid S&P 500 constituents.
# Proxy for SIC2 industry classification (not in data pipeline).
_SECTOR_MAP: Dict[str, str] = {
    # Technology
    'AAPL': 'Tech', 'MSFT': 'Tech', 'NVDA': 'Tech', 'AVGO': 'Tech', 'ORCL': 'Tech',
    'CRM': 'Tech', 'CSCO': 'Tech', 'AMD': 'Tech', 'INTC': 'Tech', 'TXN': 'Tech',
    'QCOM': 'Tech', 'AMAT': 'Tech', 'MU': 'Tech', 'LRCX': 'Tech', 'ADI': 'Tech',
    'KLAC': 'Tech', 'MCHP': 'Tech', 'FTNT': 'Tech', 'NOW': 'Tech', 'INTU': 'Tech',
    'ACN': 'Tech', 'IBM': 'Tech', 'SNPS': 'Tech', 'CDNS': 'Tech', 'ANSS': 'Tech',
    # Communication Services
    'GOOGL': 'CommSvc', 'GOOG': 'CommSvc', 'META': 'CommSvc', 'NFLX': 'CommSvc',
    'DIS': 'CommSvc', 'CMCSA': 'CommSvc', 'T': 'CommSvc', 'VZ': 'CommSvc',
    'TMUS': 'CommSvc', 'CHTR': 'CommSvc', 'WBD': 'CommSvc', 'PARA': 'CommSvc',
    # Consumer Discretionary
    'AMZN': 'ConsDis', 'TSLA': 'ConsDis', 'HD': 'ConsDis', 'MCD': 'ConsDis',
    'NKE': 'ConsDis', 'LOW': 'ConsDis', 'SBUX': 'ConsDis', 'TJX': 'ConsDis',
    'BKNG': 'ConsDis', 'CMG': 'ConsDis', 'GM': 'ConsDis', 'F': 'ConsDis',
    'MAR': 'ConsDis', 'HLT': 'ConsDis', 'DHI': 'ConsDis', 'LEN': 'ConsDis',
    # Consumer Staples
    'WMT': 'ConsStap', 'PG': 'ConsStap', 'KO': 'ConsStap', 'PEP': 'ConsStap',
    'COST': 'ConsStap', 'PM': 'ConsStap', 'MO': 'ConsStap', 'MDLZ': 'ConsStap',
    'CL': 'ConsStap', 'GIS': 'ConsStap', 'KMB': 'ConsStap', 'STZ': 'ConsStap',
    # Financials
    'JPM': 'Fin', 'BAC': 'Fin', 'WFC': 'Fin', 'GS': 'Fin', 'MS': 'Fin',
    'BLK': 'Fin', 'SCHW': 'Fin', 'C': 'Fin', 'AXP': 'Fin', 'USB': 'Fin',
    'SPGI': 'Fin', 'CB': 'Fin', 'MMC': 'Fin', 'ICE': 'Fin', 'MCO': 'Fin',
    'CME': 'Fin', 'V': 'Fin', 'MA': 'Fin', 'PNC': 'Fin', 'TFC': 'Fin',
    # Healthcare
    'LLY': 'Health', 'UNH': 'Health', 'JNJ': 'Health', 'ABBV': 'Health',
    'MRK': 'Health', 'TMO': 'Health', 'ABT': 'Health', 'DHR': 'Health',
    'PFE': 'Health', 'AMGN': 'Health', 'BMY': 'Health', 'GILD': 'Health',
    'ISRG': 'Health', 'REGN': 'Health', 'VRTX': 'Health', 'CVS': 'Health',
    # Industrials
    'GE': 'Indus', 'CAT': 'Indus', 'RTX': 'Indus', 'HON': 'Indus',
    'UNP': 'Indus', 'BA': 'Indus', 'LMT': 'Indus', 'ETN': 'Indus',
    'DE': 'Indus', 'WM': 'Indus', 'GD': 'Indus', 'NOC': 'Indus',
    'FDX': 'Indus', 'UPS': 'Indus', 'MMM': 'Indus', 'EMR': 'Indus',
    # Energy
    'XOM': 'Energy', 'CVX': 'Energy', 'COP': 'Energy', 'SLB': 'Energy',
    'EOG': 'Energy', 'OXY': 'Energy', 'PSX': 'Energy', 'MPC': 'Energy',
    'VLO': 'Energy', 'HES': 'Energy', 'HAL': 'Energy', 'DVN': 'Energy',
    # Materials
    'LIN': 'Matls', 'APD': 'Matls', 'ECL': 'Matls', 'SHW': 'Matls',
    'FCX': 'Matls', 'NEM': 'Matls', 'NUE': 'Matls', 'DOW': 'Matls',
    # Utilities
    'NEE': 'Utils', 'SO': 'Utils', 'DUK': 'Utils', 'SRE': 'Utils',
    'AEP': 'Utils', 'EXC': 'Utils', 'D': 'Utils', 'XEL': 'Utils',
    # Real Estate
    'PLD': 'REIT', 'AMT': 'REIT', 'EQIX': 'REIT', 'CCI': 'REIT',
    'SPG': 'REIT', 'DLR': 'REIT', 'O': 'REIT', 'PSA': 'REIT',
}


class IndustryMomentumMoskowitz(BaseStrategy):
    """Industry-level momentum — LONG top-3-sector stocks, SHORT bottom-3-sector stocks (Moskowitz & Grinblatt 1999)."""

    id               = 'S_industry_momentum_moskowitz'
    name             = 'IndustryMomentumMoskowitz'
    description      = 'Industry-level momentum — LONG top-3-sector, SHORT bottom-3-sector (Moskowitz 1999)'
    tier             = 2
    signal_frequency = 'monthly'
    min_lookback     = 280
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING']

    FORMATION_DAYS = 126   # ~6 months (6 × 21 trading days)
    SKIP_DAYS      = 21    # skip most-recent month (microstructure bias, per §2)
    TOP_N          = 3     # number of top/bottom sectors to trade
    BASE_SIZE      = 0.008 # per-stock fraction of portfolio

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

        scale = self.position_scale(regime_state)

        # prices is wide format: date-indexed DataFrame, ticker columns, close values
        tickers = [t for t in universe if t in prices.columns and t in _SECTOR_MAP]
        if len(tickers) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        prices_sub = prices[tickers]
        min_rows = self.FORMATION_DAYS + self.SKIP_DAYS + 5
        if len(prices_sub) < min_rows:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Formation window: [t - SKIP - FORMATION, t - SKIP], skip last month
        end_idx   = len(prices_sub) - 1 - self.SKIP_DAYS
        start_idx = end_idx - self.FORMATION_DAYS
        if start_idx < 0:
            print('[debug] signals=0', file=sys.stderr)
            return []

        p_end   = prices_sub.iloc[end_idx]
        p_start = prices_sub.iloc[start_idx].replace(0, float('nan'))
        stock_ret = ((p_end - p_start) / p_start).dropna()

        if len(stock_ret) < 20:
            print('[debug] signals=0', file=sys.stderr)
            return []

        # Equal-weight sector returns (proxy for value-weight; no market-cap in pipeline)
        sector_rets: Dict[str, float] = {}
        for sector in set(_SECTOR_MAP[t] for t in stock_ret.index):
            members = [t for t in stock_ret.index if _SECTOR_MAP[t] == sector]
            if members:
                sector_rets[sector] = float(stock_ret[members].mean())

        if len(sector_rets) < self.TOP_N * 2:
            print('[debug] signals=0', file=sys.stderr)
            return []

        sr = pd.Series(sector_rets).sort_values(ascending=False)
        n  = len(sr)
        top_sectors    = set(sr.index[:self.TOP_N])
        bottom_sectors = set(sr.index[n - self.TOP_N:])

        current_prices = prices_sub.iloc[-1]
        size = round(self.BASE_SIZE * scale, 6)
        signals: List[Signal] = []

        for ticker in stock_ret.index:
            if len(signals) >= self.MAX_SIGNALS:
                break
            sector = _SECTOR_MAP.get(ticker)
            if sector not in top_sectors and sector not in bottom_sectors:
                continue
            raw = current_prices.get(ticker)
            if raw is None or raw != raw or raw <= 0:
                continue
            price = float(raw)
            direction = 'LONG' if sector in top_sectors else 'SHORT'
            stops = self.compute_stops_and_targets(
                prices_sub[ticker].dropna(), direction, price,
                regime_state=regime_state,
            )
            s_rank = int(sr.index.get_loc(sector))
            conf = (
                ('HIGH' if s_rank == 0 else 'MED' if s_rank == 1 else 'LOW')
                if direction == 'LONG' else
                ('HIGH' if s_rank == n - 1 else 'MED' if s_rank == n - 2 else 'LOW')
            )
            signals.append(Signal(
                ticker=ticker, direction=direction,
                entry_price=price, stop_loss=stops['stop'],
                target_1=stops['t1'], target_2=stops['t2'], target_3=stops['t3'],
                position_size_pct=size, confidence=conf,
                signal_params={
                    'sector': sector,
                    'sector_6m_ret': round(sector_rets[sector], 4),
                    'sector_rank': s_rank + 1,
                },
            ))

        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals
