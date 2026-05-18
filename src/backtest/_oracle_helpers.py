"""Helpers used only by tests/test_backtest_oracles.py — not for production.

Synthesizes minimal OHLCV bars and bracket-order specs that exercise the
edge cases the kernc/backtesting.py test corpus uses to pin down broker
behavior.  We reproduce only the *cases* (inputs + expected outcomes).
The reference implementation is AGPL and is NOT imported."""
from __future__ import annotations

from dataclasses import dataclass
import pandas as pd


def ohlcv(rows: list[tuple[float, float, float, float, int]]) -> pd.DataFrame:
    """rows of (open, high, low, close, volume) → DataFrame indexed by minute."""
    idx = pd.date_range("2026-01-02 09:30", periods=len(rows), freq="1min", tz="America/New_York")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"], index=idx)


@dataclass(frozen=True)
class Bracket:
    """Minimal bracket-order spec — long-only, single asset, single fill."""
    entry: float        # limit entry price
    stop: float         # stop-loss
    target: float       # take-profit
    qty: int = 100
