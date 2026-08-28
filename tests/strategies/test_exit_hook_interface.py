"""Spec §1: exit_hook opt-in flag + should_exit default contract."""
from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import BaseStrategy, CANONICAL_REGIMES


def _mk(**attrs):
    body = {'id': 'x', 'active_in_regimes': list(CANONICAL_REGIMES),
            'generate_signals': lambda self, prices, regime, universe, aux_data=None: []}
    body.update(attrs)
    return type('Dyn', (BaseStrategy,), body)


def test_default_flag_is_false_and_should_exit_returns_none():
    cls = _mk()
    assert cls.exit_hook is False
    inst = cls()
    assert inst.should_exit({'ticker': 'AAA'}, pd.DataFrame(), {'state': 'LOW_VOL'}) is None
    assert inst.should_exit({'ticker': 'AAA'}, pd.DataFrame(), {'state': 'LOW_VOL'}, None) is None


def test_exit_hook_true_without_override_is_a_class_definition_error():
    with pytest.raises(TypeError, match='exit_hook'):
        _mk(exit_hook=True)


def test_exit_hook_true_with_override_defines_fine():
    cls = _mk(exit_hook=True,
              should_exit=lambda self, position, prices, regime, aux_data=None: 'because')
    assert cls.exit_hook is True
    assert cls().should_exit({}, pd.DataFrame(), {}) == 'because'


def test_override_without_flag_is_allowed_but_flag_stays_false():
    cls = _mk(should_exit=lambda self, position, prices, regime, aux_data=None: 'x')
    assert cls.exit_hook is False
