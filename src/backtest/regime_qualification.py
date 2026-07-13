"""regime_qualification.py — the per-regime promotion/activation rule.

Operator policy 2026-07-13 v2: a strategy qualifies IN A REGIME when that
regime's sleeve of its latest primary backtest has
    sharpe STRICTLY > min_sharpe (0.0 — "positive Sharpe"),
    max_dd_pct of the SLEEVE <= the class ceiling (equity/etp 20, option 30,
        crypto 70 — same values as before, now judged per regime),
    trade_count of the SLEEVE >= min_trades (100).

This is the single python source of the rule; the JS twin is
judgeRegimeSleeve in src/lib/promotion_service.js — keep them in sync.
Consumers: backtest.activation_assigner (live sizer eligibility, where the
dashboard's activation min-Sharpe slider adds a further sharpe floor on top)
and backtest.eligibility_assigner (manifest eligible_regimes hint).
Threshold VALUES come from strategies.lifecycle.PROMOTION_THRESHOLDS
(fractional max_drawdown there → percent here, matching the
strategy_backtest_regimes.max_dd_pct unit).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from strategies.lifecycle import _promotion_threshold  # noqa: E402


def class_thresholds(instrument_class: Optional[str]) -> dict:
    """Per-class gate thresholds in sleeve units: {min_sharpe, max_dd_pct,
    min_trades}. Unknown/None class falls back to equity (same as the JS
    getPromotionThreshold)."""
    thr = _promotion_threshold(instrument_class or 'equity')
    return {
        'min_sharpe': float(thr['min_sharpe']),
        'max_dd_pct': float(thr['max_drawdown']) * 100.0,
        'min_trades': int(thr['min_trades']),
    }


def qualifies_regime(sharpe, trade_count, max_dd_pct,
                     instrument_class: Optional[str] = 'equity') -> bool:
    """True iff one regime sleeve clears all three gates. Any missing metric
    fails closed (mirrors judgeRegimeSleeve's no_backtest behavior)."""
    thr = class_thresholds(instrument_class)
    if sharpe is None or trade_count is None or max_dd_pct is None:
        return False
    return (float(sharpe) > thr['min_sharpe']
            and float(max_dd_pct) <= thr['max_dd_pct']
            and int(trade_count) >= thr['min_trades'])
