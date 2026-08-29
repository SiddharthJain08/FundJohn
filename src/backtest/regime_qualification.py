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
and backtest.eligibility_assigner (manifest eligible_regimes hint). NOTE
(verified 2026-08-24, R1): both of those actually reimplement the LEGACY
gate INLINE from class_thresholds()/dd_leg_passes() rather than calling
qualifies_regime() itself — qualifies_regime is the reference rule and unit
tests exercise it directly, but its only *production* import today is
lifecycle.py's docstring cross-reference.

2026-08-29: benchmark leg removed (spec docs/specs/2026-08-29-benchmark-relative-sizing-spec.md D1).

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
    min_trades, min_calmar, dd_hard_cap_pct}. Unknown/None class falls back to
    equity (same as the JS getPromotionThreshold)."""
    thr = _promotion_threshold(instrument_class or 'equity')
    return {
        'min_sharpe': float(thr['min_sharpe']),
        'max_dd_pct': float(thr['max_drawdown']) * 100.0,
        'min_trades': int(thr['min_trades']),
        'min_calmar': float(thr.get('min_calmar', 0.5)),
        'dd_hard_cap_pct': float(thr.get('dd_hard_cap', 0.50)) * 100.0,
    }


def dd_leg_passes(max_dd_pct, calmar, thr: dict) -> bool:
    """Drawdown leg of the sleeve gate (2026-07-27 Calmar escape hatch).

    Passes when dd <= class ceiling, OR when the sleeve's Calmar clears the
    class floor AND dd stays under the catastrophic hard cap. Max drawdown is
    a running-max extreme — it deepens mechanically with backtest duration and
    breadth, so the flat ceiling alone systematically deactivated long-history
    sleeves (momentum_12_1 LOW_VOL: Sharpe 2.62 / 4,759 trades / DD 26%).
    Calmar (annualized return / max DD) is self-normalizing across horizons.
    Missing calmar → legacy ceiling-only behavior (never a silent pass)."""
    if max_dd_pct is None:
        return False
    dd = float(max_dd_pct)
    if dd <= thr['max_dd_pct']:
        return True
    return (calmar is not None
            and float(calmar) >= thr['min_calmar']
            and dd <= thr['dd_hard_cap_pct'])


def qualifies_regime(sharpe, trade_count, max_dd_pct,
                     instrument_class: Optional[str] = 'equity',
                     calmar=None) -> bool:
    """True iff one regime sleeve clears all gates. Any missing metric fails
    closed (mirrors judgeRegimeSleeve's no_backtest behavior); a missing
    calmar only forfeits the DD escape hatch, not the whole sleeve.

    2026-08-29 (benchmark-relative sizing spec, D1): the R1 benchmark leg was
    REMOVED. SPY's regime Sharpe is a sizing input (execution.benchmark_sizing),
    never a gate."""
    thr = class_thresholds(instrument_class)
    if sharpe is None or trade_count is None or max_dd_pct is None:
        return False
    return (float(sharpe) > thr['min_sharpe']
            and dd_leg_passes(max_dd_pct, calmar, thr)
            and int(trade_count) >= thr['min_trades'])
