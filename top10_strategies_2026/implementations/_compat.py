"""
_compat.py
==========
Lightweight shim so the strategy files in this folder can be imported and
unit-tested *outside* the FundJohn repository.

When deployed to /root/openclaw/src/strategies/implementations/, the strategy
files use absolute imports against the live FundJohn package
(`from ..base_strategy import BaseStrategy`).  Each strategy file falls back to
this shim when those imports fail (i.e., when running offline backtests in this
research folder).

This file is NOT shipped to FundJohn; the deploy script copies only the
production strategy files into /root/openclaw/src/strategies/implementations/.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class Signal:
    """Mirror of FundJohn's models.signal.Signal dataclass."""
    ticker: str
    direction: str                     # LONG / SHORT / SELL_VOL / BUY_VOL / FLAT
    entry_price: float
    stop_loss: float
    target_1: float
    target_2: float
    target_3: float
    position_size_pct: float
    confidence: str = 'MED'            # HIGH / MED / LOW
    signal_params: Dict[str, Any] = field(default_factory=dict)


class BaseStrategy:
    """Mirror of FundJohn's strategies.base_strategy.BaseStrategy."""
    id: str = ''
    version: str = '1.0.0'
    regime_filter: List[str] = []

    def generate_signals(self, market_data: dict, opts_map: dict) -> List[Signal]:
        raise NotImplementedError
