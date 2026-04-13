'use strict';

const AV_BASE = 'https://www.alphavantage.co/query';

function generatePython(server) {
  return `# Auto-generated — Alpha Vantage tool module
# ${server.description}
import os, requests
from _rate_limiter import _acquire_token

_API_KEY = os.environ.get("ALPHA_VANTAGE_API_KEY", "")
_BASE = "${AV_BASE}"
_PROVIDER = "alpha_vantage"

def _get(params: dict) -> dict:
    _acquire_token(_PROVIDER)
    p = {"apikey": _API_KEY, **params}
    r = requests.get(_BASE, params=p, timeout=30)
    r.raise_for_status()
    data = r.json()
    if "Note" in data:
        raise RuntimeError(f"Alpha Vantage rate limit: {data['Note']}")
    if "Information" in data:
        raise RuntimeError(f"Alpha Vantage quota: {data['Information']}")
    return data

def get_rsi(ticker: str, interval: str = "daily", time_period: int = 14, series_type: str = "close") -> dict:
    """RSI indicator."""
    return _get({"function": "RSI", "symbol": ticker, "interval": interval,
                 "time_period": time_period, "series_type": series_type})

def get_sma(ticker: str, interval: str = "daily", time_period: int = 50, series_type: str = "close") -> dict:
    """Simple Moving Average."""
    return _get({"function": "SMA", "symbol": ticker, "interval": interval,
                 "time_period": time_period, "series_type": series_type})

def get_bbands(ticker: str, interval: str = "daily", time_period: int = 20) -> dict:
    """Bollinger Bands."""
    return _get({"function": "BBANDS", "symbol": ticker, "interval": interval,
                 "time_period": time_period, "series_type": "close"})

def get_sector_performance() -> dict:
    """S&P 500 sector performance."""
    return _get({"function": "SECTOR"})

def get_treasury_yield(maturity: str = "10year") -> dict:
    """US Treasury yield curve. maturity: 3month, 2year, 5year, 10year, 30year."""
    return _get({"function": "TREASURY_YIELD", "maturity": maturity, "interval": "monthly"})

def get_federal_funds_rate() -> dict:
    """Federal Funds Rate monthly."""
    return _get({"function": "FEDERAL_FUNDS_RATE", "interval": "monthly"})

def get_cpi() -> dict:
    """Consumer Price Index monthly."""
    return _get({"function": "CPI", "interval": "monthly"})

def get_gdp() -> dict:
    """Real GDP quarterly."""
    return _get({"function": "REAL_GDP", "interval": "quarterly"})

def get_intraday(ticker: str, interval: str = "15min") -> dict:
    """Intraday OHLCV. interval: 1min, 5min, 15min, 30min, 60min."""
    return _get({"function": "TIME_SERIES_INTRADAY", "symbol": ticker,
                 "interval": interval, "outputsize": "compact"})
`;
}

module.exports = { generatePython };
