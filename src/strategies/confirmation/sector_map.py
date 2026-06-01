"""Static GICS ticker->sector and sector->SPDR-ETF maps for the sector-flow strategy.

TICKER_SECTOR is lifted verbatim from S_industry_momentum_moskowitz._SECTOR_MAP (the
established in-repo precedent; no DB/FMP dependency, point-in-time stable). SECTOR_ETF maps
each GICS group to its State Street SPDR sector ETF (all present in prices.parquet, 10y daily).
"""
from __future__ import annotations
from typing import Dict, List, Optional

TICKER_SECTOR: Dict[str, str] = {
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

SECTOR_ETF: Dict[str, str] = {
    'Tech': 'XLK', 'CommSvc': 'XLC', 'ConsDis': 'XLY', 'ConsStap': 'XLP',
    'Fin': 'XLF', 'Health': 'XLV', 'Indus': 'XLI', 'Energy': 'XLE',
    'Matls': 'XLB', 'Utils': 'XLU', 'REIT': 'XLRE',
}

BROAD_MARKET_ETFS = ('SPY', 'QQQ')


def etf_for_ticker(ticker: str) -> Optional[str]:
    """SPDR sector ETF for a ticker, or None if unmapped."""
    sector = TICKER_SECTOR.get(ticker)
    return SECTOR_ETF.get(sector) if sector else None


def constituents(sector: str) -> List[str]:
    """All mapped tickers in a sector."""
    return sorted(t for t, s in TICKER_SECTOR.items() if s == sector)
