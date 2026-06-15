"""Shared Crabel/Oxford daily-bar helpers + OHLC-aware base class.

oxfordstrat.com strategies are single-instrument daily-OHLC rules. generate_signals
receives a CLOSE-ONLY wide panel, so OxfordBaseStrategy self-loads basket OHLC from
the master parquet (filtered to the basket) and caches it on the instance (the
backtest builds one instance per run, then loops bars). All indicator helpers are
pure functions over an OHLC DataFrame indexed by date with open/high/low/close.
"""
from __future__ import annotations
from pathlib import Path
from typing import Dict, List, Optional
import pandas as pd
import numpy as np
from strategies.base import BaseStrategy, Signal

# Liquid ETF basket proxying Oxford's 4 futures sectors. Verified present in
# data/master/prices.parquet 2026-06-15. CORE = full ~10y history; EXT = ~5y.
OXFORD_ETF_BASKET_CORE = [
    'SPY','QQQ','IWM','DIA','EFA','EEM','VTI','TLT','IEF','SHY','LQD','HYG','AGG',
    'GLD','SLV','USO','UNG','XLE','XLF','XLK','XLV','XLI','XLP','XLU','XLY','XLB','GDX']
OXFORD_ETF_BASKET_EXT = [
    'DBC','DBA','DBB','CPER','PALL','PPLT','CORN','WEAT','SOYB','MDY','UUP','UDN','FXF']
OXFORD_ETF_BASKET = OXFORD_ETF_BASKET_CORE + OXFORD_ETF_BASKET_EXT


def _prices_parquet_path() -> Path:
    return Path(__file__).resolve().parents[2] / 'data' / 'master' / 'prices.parquet'


def true_range_series(bars: pd.DataFrame) -> pd.Series:
    h, l, c = bars['high'], bars['low'], bars['close']
    pc = c.shift(1)
    return pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)


def atr(bars: pd.DataFrame, n: int = 20) -> float:
    if len(bars) < n + 1:
        return float('nan')
    return float(true_range_series(bars).rolling(n).mean().iloc[-1])


def donchian_prev(bars: pd.DataFrame, n: int) -> tuple[float, float]:
    """Upper/lower Donchian over the n bars BEFORE the current bar (no same-bar leak)."""
    if len(bars) < n + 1:
        return float('nan'), float('nan')
    up = float(bars['high'].iloc[-(n+1):-1].max())
    lo = float(bars['low'].iloc[-(n+1):-1].min())
    return up, lo


def sma(s: pd.Series, n: int) -> float:
    if len(s) < n:
        return float('nan')
    return float(s.rolling(n).mean().iloc[-1])


def rsi_wilder(s: pd.Series, n: int) -> float:
    if len(s) < n + 1:
        return float('nan')
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = gain.iloc[-1] / loss.iloc[-1] if loss.iloc[-1] > 1e-12 else float('inf')
    return float(100 - 100/(1+rs))


def avg_noise(bars: pd.DataFrame, n: int = 10) -> float:
    """Crabel stretch base: mean over n bars of min(open-low, high-open)."""
    if len(bars) < n:
        return float('nan')
    sub = bars.iloc[-n:]
    noise = np.minimum((sub['open']-sub['low']).abs(), (sub['high']-sub['open']).abs())
    return float(noise.mean())


def is_nrn(bars: pd.DataFrame, n: int) -> bool:
    """True if the current bar's range is the narrowest of the last n bars (NR-n / NR7)."""
    if len(bars) < n:
        return False
    rng = (bars['high'] - bars['low']).iloc[-n:]
    return bool(rng.iloc[-1] == rng.min() and rng.iloc[-1] < rng.iloc[:-1].min())


def gap_dir(bars: pd.DataFrame) -> int:
    """+1 if today gapped fully up (low > prior high), -1 if down (high < prior low), else 0."""
    if len(bars) < 2:
        return 0
    today, prev = bars.iloc[-1], bars.iloc[-2]
    if today['low'] > prev['high']:
        return 1
    if today['high'] < prev['low']:
        return -1
    return 0


class OxfordBaseStrategy(BaseStrategy):
    """BaseStrategy + lazy, cached, point-in-time basket OHLC self-load."""
    instrument_class = 'etp'

    def __init__(self, parameters: dict = None):
        super().__init__(parameters)
        self._ohlc_cache: Optional[Dict[str, pd.DataFrame]] = None

    def _load_basket_ohlc(self) -> Dict[str, pd.DataFrame]:
        if self._ohlc_cache is not None:
            return self._ohlc_cache
        cache: Dict[str, pd.DataFrame] = {}
        try:
            df = pd.read_parquet(
                _prices_parquet_path(),
                columns=['ticker','date','open','high','low','close'],
                filters=[('ticker','in',OXFORD_ETF_BASKET)])
            df['date'] = pd.to_datetime(df['date'])
            for t, g in df.groupby('ticker'):
                cache[t] = g.set_index('date')[['open','high','low','close']].sort_index()
        except Exception:
            cache = {}
        self._ohlc_cache = cache
        return cache

    def basket_ohlc(self, prices: pd.DataFrame) -> Dict[str, pd.DataFrame]:
        """Point-in-time OHLC per basket ticker, sliced to the signal date (no look-ahead)."""
        if prices is None or prices.empty:
            return {}
        asof = prices.index[-1]
        out: Dict[str, pd.DataFrame] = {}
        for t, bars in self._load_basket_ohlc().items():
            b = bars.loc[:asof]
            if len(b) >= self.min_lookback:
                out[t] = b
        return out

    # NOTE: brackets are NOT computed here. All oxf_* strategies use the house
    # BaseStrategy.compute_stops_and_targets (regime-scaled ATR(14)×2 stop +
    # 5/10/20% targets) — the SAME risk management every other candidate on the
    # page uses, so metrics are comparable. The Oxford contribution is the entry
    # SIGNAL; sizing/brackets are house-standard. (A custom ATR×6 Oxford bracket
    # was considered and rejected: it deviates from the book and, with the 21-day
    # house max_hold, would just make these strategies incomparable scalps.)
