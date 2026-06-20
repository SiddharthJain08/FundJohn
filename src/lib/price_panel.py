"""Shared strategy price-panel calendar — used by BOTH the live engine
(execution.engine.run_strategies) and the backtest (backtest.unified_backtest).
One definition, one gate, so live and backtest never diverge."""
from __future__ import annotations
import os
import pandas as pd

GATE = 'OPENCLAW_EQUITY_TRADING_CALENDAR'


def is_equity_ticker(ticker: str) -> bool:
    """True for cash-equity / ETF tickers; False for indices (^…), crypto
    (…-USD), futures (…=F) and forex (…=X). Defines the equity trading calendar."""
    t = str(ticker)
    return (not t.startswith('^')) and ('-USD' not in t) and ('=F' not in t) and ('=X' not in t)


def apply_equity_calendar(close_wide: pd.DataFrame) -> pd.DataFrame:
    """Restrict a (date × ticker) close panel to the equity trading calendar:
    keep only rows with ≥1 non-NaN equity observation. Drops weekend/holiday rows
    contributed solely by 7-day crypto/forex tickers. Rows only; columns untouched."""
    eq_cols = [c for c in close_wide.columns if is_equity_ticker(c)]
    if not eq_cols:
        return close_wide
    return close_wide.loc[close_wide[eq_cols].notna().any(axis=1).values]


def equity_calendar_enabled() -> bool:
    """One system-wide gate (default OFF) for the equity trading-day panel."""
    return os.environ.get(GATE, '0') == '1'


def calendar_for(instrument_class: str) -> str:
    """Pick the panel calendar for a strategy's instrument class. Equity-like
    classes get the equity calendar when the gate is on; crypto ALWAYS gets the
    full union calendar (it trades 7 days a week)."""
    if equity_calendar_enabled() and instrument_class in ('equity', 'etp', 'option'):
        return 'equity'
    return 'union'
