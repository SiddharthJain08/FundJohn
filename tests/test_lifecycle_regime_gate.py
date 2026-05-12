"""Tests for the regime-eligibility gate at candidate→staging.

Spec: docs/superpowers/specs/2026-05-11-regime-blended-position-sizing-design.md
§"Strategy creation pipeline changes – A. PaperHunter → StrategyCoder → Promotion"

Key: in this codebase STAGING precedes CANDIDATE in the forward pipeline.
CANDIDATE→STAGING is labeled "regress — needs additional data sources" in the
transition table, and the spec gates eligible_regimes validation there.
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import pytest
from strategies.lifecycle import LifecycleStateMachine, LifecycleError, StrategyState


def test_promotion_blocked_without_eligible_regimes():
    """candidate→staging blocked when metadata has no eligible_regimes."""
    sm = LifecycleStateMachine.new_empty()
    sm.register('S_test', initial_state=StrategyState.CANDIDATE, metadata={})
    with pytest.raises(LifecycleError, match='requires_regime_qualification'):
        sm.transition('S_test', StrategyState.STAGING)


def test_promotion_passes_with_explicit_eligible_regimes():
    """candidate→staging allowed when explicit eligible_regimes list is present."""
    sm = LifecycleStateMachine.new_empty()
    sm.register('S_test', initial_state=StrategyState.CANDIDATE,
                metadata={'eligible_regimes': ['LOW_VOL']})
    sm.transition('S_test', StrategyState.STAGING)
    assert sm.get_record('S_test').metadata['eligible_regimes'] == ['LOW_VOL']


def test_promotion_auto_derives_from_backtest_results():
    """Auto-derives eligible_regimes from backtest_results.eligible_regimes_proposed."""
    sm = LifecycleStateMachine.new_empty()
    sm.register('S_test', initial_state=StrategyState.CANDIDATE,
                metadata={'backtest_results': {'eligible_regimes_proposed': ['TRANSITIONING']}})
    sm.transition('S_test', StrategyState.STAGING)
    assert sm.get_record('S_test').metadata['eligible_regimes'] == ['TRANSITIONING']


def test_promotion_blocked_when_backtest_proposes_nothing():
    """Blocked when backtest_results.eligible_regimes_proposed is empty."""
    sm = LifecycleStateMachine.new_empty()
    sm.register('S_test', initial_state=StrategyState.CANDIDATE,
                metadata={'backtest_results': {'eligible_regimes_proposed': []}})
    with pytest.raises(LifecycleError, match='requires_regime_qualification'):
        sm.transition('S_test', StrategyState.STAGING)


def test_other_transitions_unaffected_by_gate():
    """CANDIDATE→ARCHIVED and STAGING→CANDIDATE must not invoke the gate."""
    # CANDIDATE→ARCHIVED: no eligible_regimes needed
    sm = LifecycleStateMachine.new_empty()
    sm.register('S_cand', initial_state=StrategyState.CANDIDATE, metadata={})
    sm.transition('S_cand', StrategyState.ARCHIVED)
    assert sm.get_state('S_cand') == StrategyState.ARCHIVED

    # STAGING→CANDIDATE (fused approval forward path): no eligible_regimes needed
    sm2 = LifecycleStateMachine.new_empty()
    sm2.register('S_stag', initial_state=StrategyState.STAGING, metadata={})
    sm2.transition('S_stag', StrategyState.CANDIDATE)
    assert sm2.get_state('S_stag') == StrategyState.CANDIDATE
