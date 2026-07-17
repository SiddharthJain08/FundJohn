"""The sizer weight must NOT apply an OUE multiplier.

Operator decision (2026-05-29): strategy quality is now governed by
cross-sector corroboration + the weekly position-recommendations (which
carry their own sizing multiplier and stop changes), so the OUE-derived
multiplier was removed from the sizer entirely. The per-(strategy,regime)
weight is now the effective Sharpe directly:

    weight       = effective_sharpe
    daily_weight = effective_sharpe / sqrt(cadence_days)

OUE classification still runs (it feeds the trade report, dashboard O/U/E
column, and weekly review) — it just no longer scales position size.
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'src'))

from execution import strategy_weights


def test_regime_weight_equals_effective_sharpe():
    w, w_daily = strategy_weights._regime_weight(1.8, 4.0)
    assert w == 1.8                                  # no multiplier applied
    assert math.isclose(w_daily, 1.8 / math.sqrt(4.0))


def test_regime_weight_cadence_floored_to_one():
    w, w_daily = strategy_weights._regime_weight(2.0, 0)
    assert w == 2.0
    assert math.isclose(w_daily, 2.0)                # sqrt(max(1, 0)) == 1


def test_oue_multiplier_machinery_removed():
    """Guardrail: the multiplier function and its gating constants are gone
    so they can't silently creep back into sizing."""
    assert not hasattr(strategy_weights, '_oue_multiplier')
    for const in ('OUE_FLOOR', 'OUE_CEIL', 'OUE_MIN_TOTAL', 'OUE_MIN_OUTLIERS',
                  'OUE_LIFETIME_MIN_TOTAL', 'OUE_LIFETIME_MIN_OUTLIERS'):
        assert not hasattr(strategy_weights, const), f'{const} should be removed'


def test_oue_classification_loader_retained():
    """The OUE *count* loader stays — it populates the audit columns and
    the dashboard O/U/E view; only the multiplier was removed."""
    assert hasattr(strategy_weights, '_load_oue_by_strategy_regime')
    assert hasattr(strategy_weights, 'OUE_TAU_DAYS')   # count time-decay constant
