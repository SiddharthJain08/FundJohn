"""Pure-helper tests for size_scalar wiring + signed contributions."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution import regime_blended_sizer as rbs  # noqa: E402


def test_apply_size_scalars_off_is_identity():
    w = {'A': 2.0, 'B': 1.0}
    out = rbs._apply_size_scalars(w, {'A': 1.5}, gate_on=False)
    assert out == {'A': 2.0, 'B': 1.0}          # OFF → untouched


def test_apply_size_scalars_on_scales_present_defaults_missing_to_one():
    w = {'A': 2.0, 'B': 1.0, 'C': 4.0}
    out = rbs._apply_size_scalars(w, {'A': 1.5, 'B': 0.0}, gate_on=True)
    assert out['A'] == 3.0                       # 2.0 × 1.5
    assert out['B'] == 0.0                       # explicit mute honored
    assert out['C'] == 4.0                       # missing → ×1.0


def test_build_contributions_signed_sums_to_ticker_w():
    # AAPL: A long (eff 3.0), B short (eff 1.0) → contributions +3, -1; net +2
    strategies = ['A', 'B']
    directions = [1, -1]
    eff_weight = {'A': 3.0, 'B': 1.0}
    contribs = rbs._build_contributions(strategies, directions, eff_weight)
    assert contribs == [
        {'strategy_id': 'A', 'contribution': 3.0, 'direction': 1},
        {'strategy_id': 'B', 'contribution': -1.0, 'direction': -1},
    ]
    assert round(sum(c['contribution'] for c in contribs), 9) == 2.0


def test_build_contributions_skips_synthetic_and_unknown():
    strategies = ['__close_orphan__', 'A']
    directions = [0, 1]
    eff_weight = {'A': 2.0}                       # synthetic not in eff_weight
    contribs = rbs._build_contributions(strategies, directions, eff_weight)
    assert contribs == [{'strategy_id': 'A', 'contribution': 2.0, 'direction': 1}]


def test_scaled_target_diff_reports_per_ticker_dollar_delta():
    # survivors: AAPL = [(A,+1)], MSFT = [(B,+1)]; raw weights A=1,B=1 → 50/50
    # scalar A=3 → AAPL share 3/4, MSFT 1/4 of lam_nav=100.
    survivors = {'AAPL': [('A', 1)], 'MSFT': [('B', 1)]}
    weight_by_strat = {'A': 1.0, 'B': 1.0}
    scalars = {'A': 3.0}
    raw_targets = {'AAPL': 50.0, 'MSFT': 50.0}
    diff = rbs._scaled_target_diff(survivors, weight_by_strat, scalars,
                                   raw_targets, lam_nav=100.0)
    assert round(diff['AAPL']['scaled_usd'], 6) == 75.0
    assert round(diff['MSFT']['scaled_usd'], 6) == 25.0
    assert round(diff['AAPL']['delta_usd'], 6) == 25.0
    assert round(diff['MSFT']['delta_usd'], 6) == -25.0
