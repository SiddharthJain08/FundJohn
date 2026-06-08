"""tests/test_sizer_dust_floor.py

The per-regime min-notional parameter (regime_sizer_params.min_signal_notional_*,
$100-500, regime-varying) was removed 2026-06-08: it dropped sub-threshold
tickers and RENORMALIZED the survivors, concentrating into fewer/larger names
and capping diversification. It is replaced by a single hard dust floor at
Alpaca's ~$1 fractional minimum that drops ONLY true dust and does NOT
renormalize — so meaningful small positions are kept (diversification), and the
sizer never emits orders the broker would reject.

Run:
    pytest tests/test_sizer_dust_floor.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.regime_blended_sizer import _dust_tickers, DUST_FLOOR_USD  # noqa: E402


class TestDustTickers:
    def test_dust_floor_is_alpaca_minimum(self):
        assert DUST_FLOOR_USD == 1.0

    def test_keeps_all_names_above_floor(self):
        # $50 and $200 would have been DROPPED by the old $100-500 regime
        # threshold — the whole point of the change is they're kept now.
        target = {'A': 50.0, 'B': 200.0, 'C': 900.0}
        assert _dust_tickers(target, DUST_FLOOR_USD) == []

    def test_drops_subdollar_dust_only(self):
        target = {'A': 5000.0, 'B': 0.40}
        assert _dust_tickers(target, DUST_FLOOR_USD) == ['B']

    def test_one_dollar_kept_below_dropped(self):
        assert _dust_tickers({'A': 1.0}, DUST_FLOOR_USD) == []
        assert _dust_tickers({'A': 0.99}, DUST_FLOOR_USD) == ['A']

    def test_uses_absolute_value_for_shorts(self):
        target = {'A': -0.50, 'B': -5000.0}
        assert _dust_tickers(target, DUST_FLOOR_USD) == ['A']

    def test_does_not_mutate_input(self):
        target = {'A': 0.10, 'B': 3000.0}
        _dust_tickers(target, DUST_FLOOR_USD)
        assert target == {'A': 0.10, 'B': 3000.0}
