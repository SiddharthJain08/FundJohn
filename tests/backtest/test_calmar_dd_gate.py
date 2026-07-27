"""Calmar escape hatch on the sleeve DD leg (operator directive 2026-07-27).

Max drawdown is a running-max extreme — it deepens mechanically with backtest
duration and breadth, so the flat class ceiling systematically deactivated
long-history high-breadth sleeves (momentum_12_1 LOW_VOL: Sharpe 2.62 on
4,759 trades, DD 26%). The DD leg now passes when dd <= ceiling OR
(calmar >= class floor AND dd <= catastrophic hard cap)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.regime_qualification import (class_thresholds, dd_leg_passes,
                                           qualifies_regime)  # noqa: E402


EQ = class_thresholds('equity')


def test_class_thresholds_carry_hatch_values():
    assert EQ['min_calmar'] == 0.5
    assert EQ['dd_hard_cap_pct'] == 50.0
    assert class_thresholds('option')['dd_hard_cap_pct'] == 60.0
    assert class_thresholds('crypto')['dd_hard_cap_pct'] == 85.0


def test_under_ceiling_passes_without_calmar():
    assert dd_leg_passes(16.1, None, EQ)


def test_momentum_12_1_low_vol_sleeve_recovers():
    # the motivating case: DD 26.0 > 20 ceiling, Calmar 2.07
    assert dd_leg_passes(26.0, 2.07, EQ)
    assert qualifies_regime(2.62, 4759, 26.0, 'equity', calmar=2.07)


def test_deep_dd_good_calmar_blocked_by_hard_cap():
    # S_idiosyncratic_vol_puzzle class: Calmar 0.70 but DD 55 > 50 cap
    assert not dd_leg_passes(55.0, 0.70, EQ)


def test_weak_calmar_over_ceiling_fails():
    # S9_dual_momentum class: DD 64.4, Calmar 0.38
    assert not dd_leg_passes(64.4, 0.38, EQ)
    assert not dd_leg_passes(24.6, 0.37, EQ)


def test_missing_calmar_forfeits_hatch_only():
    assert not dd_leg_passes(26.0, None, EQ)     # over ceiling, no hatch
    assert dd_leg_passes(19.9, None, EQ)         # under ceiling still fine


def test_missing_dd_fails_closed():
    assert not dd_leg_passes(None, 3.0, EQ)


def test_qualifies_regime_other_legs_unchanged():
    assert not qualifies_regime(0.0, 4759, 26.0, 'equity', calmar=2.07)   # sharpe strict
    assert not qualifies_regime(2.62, 99, 26.0, 'equity', calmar=2.07)    # trade floor
    assert not qualifies_regime(None, 4759, 26.0, 'equity', calmar=2.07)  # fail closed
