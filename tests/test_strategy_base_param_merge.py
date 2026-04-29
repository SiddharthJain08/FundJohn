"""tests/test_strategy_base_param_merge.py

Guards the BaseStrategy.__init__ contract: DB-supplied parameters must
*merge over* code defaults, not replace them. The prior behavior dropped
defaults whenever the DB row was non-empty, which silently broke any
strategy whose code referenced a key the DB row didn't carry.

Real failures from the 2026-04-29 cycle this protects against:

  - S15_iv_rv_arb FAILED: 'min_option_vol'
    DB row had {min_iv_rv_ratio, min_iv_abs, max_iv_abs, base_size_pct}
    but the strategy's `if opt_vol < p['min_option_vol']` blew up with
    KeyError because min_option_vol lived only in default_parameters().

  - S_sparse_basis_pursuit_sdf signals=0 (error: 'n_rff')
    Same shape — DB row had stop/target/holding but n_rff was a
    default-only parameter that the random Fourier features step needs.

Run:
    pytest tests/test_strategy_base_param_merge.py -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from strategies.base import BaseStrategy  # noqa: E402


class _DemoStrategy(BaseStrategy):
    id   = 'S_demo_test'
    name = 'demo'

    def default_parameters(self) -> dict:
        return {
            'alpha':           0.1,
            'beta':            0.2,
            'min_option_vol':  100,    # the key 2026-04-29 lost
        }

    def generate_signals(self, prices, regime, universe, aux_data=None):
        return []


class TestBaseStrategyParamMerge(unittest.TestCase):
    def test_no_overrides_uses_pure_defaults(self):
        s = _DemoStrategy()
        self.assertEqual(s.parameters['alpha'], 0.1)
        self.assertEqual(s.parameters['beta'], 0.2)
        self.assertEqual(s.parameters['min_option_vol'], 100)

    def test_db_overrides_layer_on_top_of_defaults(self):
        """The exact 2026-04-29 S15 shape: DB row carries a *subset* and
        one *override* of the strategy's keys; defaults must survive."""
        db_row = {'alpha': 0.5}     # override one default; omit the rest
        s = _DemoStrategy(parameters=db_row)
        self.assertEqual(s.parameters['alpha'], 0.5)            # overridden
        self.assertEqual(s.parameters['beta'], 0.2)             # from defaults
        self.assertEqual(s.parameters['min_option_vol'], 100)   # from defaults

    def test_db_only_keys_are_preserved(self):
        """Mastermind sometimes adds curator-only keys (e.g. 'hypothesis')
        the strategy code doesn't reference. They must round-trip
        untouched so other consumers (memos/UI) still see them."""
        db_row = {'alpha': 0.5, 'hypothesis': 'mean reversion holds'}
        s = _DemoStrategy(parameters=db_row)
        self.assertEqual(s.parameters['hypothesis'], 'mean reversion holds')

    def test_default_dict_is_not_aliased_across_instances(self):
        """If two instances ever share the same `parameters` dict by
        reference, an override on one would silently mutate the other.
        Defensive guard so the merge always produces a fresh dict."""
        a = _DemoStrategy(parameters={'alpha': 9.9})
        b = _DemoStrategy()
        self.assertEqual(b.parameters['alpha'], 0.1)
        self.assertEqual(a.parameters['alpha'], 9.9)


if __name__ == '__main__':
    unittest.main()
