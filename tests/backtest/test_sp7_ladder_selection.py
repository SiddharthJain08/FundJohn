"""Ladder selection — LARGEST-first + ΔSharpe≥0.10 displacement + the
per-regime maintain-constraint (universe ladder campaign, 2026-07-21).
Flipped from the original SP-7 Phase B parsimony direction."""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from backtest.universe_ladder_selection import select_tier, LADDER_TIERS


def _m(sharpe, trades=100):
    return {'sharpe': sharpe, 'trades_n': trades}


def test_ladder_tiers_order():
    assert LADDER_TIERS == ('sp500', 'tier_r1000', 'tier_r3000', 'tier_liquid')


def test_largest_wins_on_tie_band():
    v = select_tier({'sp500': _m(1.09), 'tier_r1000': _m(1.05),
                     'tier_r3000': _m(1.00), 'tier_liquid': _m(1.00)})
    # sp500 is only +0.09 over the broadest — stays tier_liquid
    assert v['verdict'] == 'winner' and v['choice'] == 'tier_liquid'


def test_narrower_displaces_at_threshold():
    v = select_tier({'sp500': _m(1.12), 'tier_r1000': _m(1.15),
                     'tier_r3000': _m(1.10), 'tier_liquid': _m(1.00)})
    # r3000 displaces liquid (Δ=0.10); r1000 does NOT displace r3000 (Δ=0.05)
    assert v['choice'] == 'tier_r3000'


def test_chained_displacement_to_narrowest():
    v = select_tier({'sp500': _m(1.31), 'tier_r1000': _m(1.20),
                     'tier_r3000': _m(1.10), 'tier_liquid': _m(1.0)})
    assert v['choice'] == 'sp500'


def test_none_and_low_trades_ineligible():
    v = select_tier({'sp500': _m(None), 'tier_r1000': _m(2.0, trades=10),
                     'tier_r3000': _m(1.0), 'tier_liquid': None})
    assert v['choice'] == 'tier_r3000'  # only eligible tier


def test_all_ineligible_is_no_signal():
    v = select_tier({'sp500': _m(None), 'tier_r1000': None,
                     'tier_r3000': _m(1.0, trades=5), 'tier_liquid': _m(None)})
    assert v['verdict'] == 'no_signal' and v['choice'] is None


def test_missing_tier_keys_treated_ineligible():
    v = select_tier({'sp500': _m(1.4)})
    assert v['choice'] == 'sp500'


def test_displacement_at_float_boundary():
    """A challenger at exactly winner+0.10 must displace despite IEEE754
    (1.2 − 1.1 = 0.09999999999999987 without the epsilon guard)."""
    v = select_tier({'sp500': _m(0.5), 'tier_r1000': _m(0.6),
                     'tier_r3000': _m(1.20), 'tier_liquid': _m(1.10)})
    assert v['choice'] == 'tier_r3000'


# ── maintain-constraint (per-regime DD/trades bar) ─────────────────────────

def _rm(trades, dd):
    return {'trade_count': trades, 'max_dd_pct': dd}


FULL_REGIMES = {'LOW_VOL': _rm(400, 10.0), 'HIGH_VOL': _rm(150, 15.0),
                'CRISIS': _rm(40, 25.0)}  # CRISIS unmet in full → not protected


def test_shrink_blocked_when_regime_drops_below_trade_floor():
    metrics = {'tier_liquid': _m(1.00), 'sp500': _m(1.50)}
    regime = {'tier_liquid': FULL_REGIMES,
              # sp500 beats on sharpe but HIGH_VOL falls to 60 trades
              'sp500': {'LOW_VOL': _rm(300, 9.0), 'HIGH_VOL': _rm(60, 12.0)}}
    v = select_tier(metrics, regime_metrics_by_tier=regime,
                    baseline_regime_metrics=FULL_REGIMES)
    assert v['choice'] == 'tier_liquid'
    assert v['maintained_regimes'] == ['HIGH_VOL', 'LOW_VOL']  # CRISIS excluded
    (c,) = [c for c in v['comparisons'] if c['challenger'] == 'sp500']
    assert c['displaced'] is False and c['blocked_regimes'] == ['HIGH_VOL']


def test_shrink_blocked_when_regime_breaches_dd_ceiling():
    metrics = {'tier_liquid': _m(1.00), 'sp500': _m(1.50)}
    regime = {'tier_liquid': FULL_REGIMES,
              # equity ceiling is 20% — LOW_VOL blows out to 25% on sp500
              'sp500': {'LOW_VOL': _rm(300, 25.0), 'HIGH_VOL': _rm(140, 12.0)}}
    v = select_tier(metrics, regime_metrics_by_tier=regime,
                    baseline_regime_metrics=FULL_REGIMES)
    assert v['choice'] == 'tier_liquid'
    (c,) = [c for c in v['comparisons'] if c['challenger'] == 'sp500']
    assert c['blocked_regimes'] == ['LOW_VOL']


def test_shrink_allowed_when_all_maintained_regimes_hold():
    metrics = {'tier_liquid': _m(1.00), 'sp500': _m(1.50)}
    regime = {'tier_liquid': FULL_REGIMES,
              # CRISIS was NOT met by the full universe, so its absence on
              # sp500 does not block; the two protected regimes hold
              'sp500': {'LOW_VOL': _rm(300, 9.0), 'HIGH_VOL': _rm(120, 14.0)}}
    v = select_tier(metrics, regime_metrics_by_tier=regime,
                    baseline_regime_metrics=FULL_REGIMES)
    assert v['choice'] == 'sp500'


def test_missing_challenger_regime_map_fails_closed():
    metrics = {'tier_liquid': _m(1.00), 'sp500': _m(1.50)}
    regime = {'tier_liquid': FULL_REGIMES}  # sp500 has no regime split at all
    v = select_tier(metrics, regime_metrics_by_tier=regime,
                    baseline_regime_metrics=FULL_REGIMES)
    assert v['choice'] == 'tier_liquid'


def test_baseline_defaults_to_broadest_eligible_tier():
    metrics = {'tier_liquid': _m(1.00), 'sp500': _m(1.50)}
    regime = {'tier_liquid': {'LOW_VOL': _rm(400, 10.0)},
              'sp500': {'LOW_VOL': _rm(50, 10.0)}}
    v = select_tier(metrics, regime_metrics_by_tier=regime)
    assert v['maintained_regimes'] == ['LOW_VOL']
    assert v['choice'] == 'tier_liquid'  # sp500 drops LOW_VOL to 50 trades


def test_crypto_class_uses_wider_dd_ceiling():
    metrics = {'tier_liquid': _m(1.00), 'sp500': _m(1.50)}
    base = {'LOW_VOL': _rm(400, 50.0)}
    regime = {'tier_liquid': base, 'sp500': {'LOW_VOL': _rm(300, 60.0)}}
    # 60% DD blocks under equity (ceiling 20) but passes under crypto (70)
    v = select_tier(metrics, regime_metrics_by_tier=regime,
                    baseline_regime_metrics=base, instrument_class='crypto')
    assert v['choice'] == 'sp500'


def test_no_regime_data_skips_constraint():
    v = select_tier({'tier_liquid': _m(1.00), 'sp500': _m(1.50)})
    assert v['choice'] == 'sp500' and v['maintained_regimes'] == []
