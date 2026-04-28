"""tests/test_deterministic_sizer.py

Unit tests for the deterministic position sizer that replaced TradeJohn
the LLM in the daily cycle (2026-04-27). Each test exercises one rule
or boundary in isolation; together they pin the contract that downstream
alpaca_executor reads.

Run:
    pytest tests/test_deterministic_sizer.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))

from execution.deterministic_sizer import (  # noqa: E402
    size_orders,
    MAX_POSITION_PCT,
    MIN_EFFECTIVE_PCT,
    MAX_DAILY_NEW_NOTIONAL_PCT,
    HALF_KELLY,
    OVERPERF_BONUS,
    UNDERPERF_PENALTY,
    STRATEGY_REJECTED_MULTIPLIER,
    STRATEGY_UNDER_MULTIPLIER,
)


# ── Fixture builders ─────────────────────────────────────────────────────

def _make_signal(**overrides) -> dict:
    """Build a baseline LONG signal: entry=100, stop=95, t1=110, p=0.7.
       R = (110-100)/(100-95) = 2.0
       f* = (0.7*2 - 0.3) / 2 = 0.55
       half_kelly = 0.275 → clipped to MAX_POSITION_PCT=0.05.
    """
    base = {
        'ticker':      'AAPL',
        'strategy_id': 'S_test',
        'direction':   'LONG',
        'entry':       100.0,
        'stop':        95.0,
        't1':          110.0,
        'p_t1':        0.7,
        'ev_gbm':      0.04,
    }
    base.update(overrides)
    return base


def _make_handoff(signals, *, regime_scale=1.0, d1_strategy_stats=None,
                  nav=1_000_000) -> dict:
    return {
        'cycle_date': '2026-04-27',
        'regime':     {'state': 'LOW_VOL', 'stress': 0.0, 'scale': regime_scale},
        'portfolio':  {'portfolio_value': nav},
        'signals':    signals,
        'd1_strategy_stats': d1_strategy_stats or {},
    }


def _make_manifest(**states) -> dict:
    """`_make_manifest(S_test='live', S_other='candidate')` → manifest dict."""
    return {'strategies': {sid: {'state': st} for sid, st in states.items()}}


# ── Tests ────────────────────────────────────────────────────────────────

def test_kelly_full_path_live_strategy():
    """A live strategy signal sizes via half-Kelly clipped at MAX_POSITION_PCT,
    then scaled by regime, with no d-1 adjustments."""
    sig = _make_signal()
    handoff = _make_handoff([sig], regime_scale=1.0)
    manifest = _make_manifest(S_test='live')
    result = size_orders(handoff, manifest)

    assert result['total_green'] == 1
    assert result['total_vetoed'] == 0
    o = result['orders'][0]
    # half_kelly=0.275 clipped to 0.05; ×1.0 regime; ×1.0 lifecycle
    assert o['pct_nav'] == pytest.approx(MAX_POSITION_PCT)
    # shares = floor(0.05 * 1_000_000 / 100) = 500
    assert o['shares'] == 500
    assert o['notional_usd'] == pytest.approx(50_000)
    assert o['priority_rank'] == 1


def test_rule_A_veto_repeat_offender():
    """Signal with d1.kind='rejected' and an allowlisted reason → veto."""
    sig = _make_signal(d1={'kind': 'rejected', 'reason': 'prefilter_negative_ev'})
    handoff = _make_handoff([sig])
    result = size_orders(handoff, _make_manifest(S_test='live'))
    assert result['total_green'] == 0
    assert result['total_vetoed'] == 1
    assert result['vetoed'][0]['reason'] == 'repeat_offender_d-1'

    # A non-allowlisted reason should NOT trigger Rule A.
    sig2 = _make_signal(d1={'kind': 'rejected', 'reason': 'data_gap'})
    result2 = size_orders(_make_handoff([sig2]), _make_manifest(S_test='live'))
    assert result2['total_green'] == 1


def test_rule_C_overperf_bonus():
    """d1.kind='over' multiplies pct_nav by OVERPERF_BONUS=1.2.

    Use p=0.38 so half-Kelly = 0.035 stays below MAX_POSITION_PCT=0.05
    even after the ×1.2 bonus → bonus visible in the output.
    """
    sig_base = _make_signal(ticker='AAA', p_t1=0.38)
    sig_over = _make_signal(ticker='BBB', p_t1=0.38,
                             d1={'kind': 'over', 'sigma_delta': 2.1})
    handoff = _make_handoff([sig_base, sig_over], regime_scale=1.0)
    result = size_orders(handoff, _make_manifest(S_test='live'))

    by_ticker = {o['ticker']: o for o in result['orders']}
    assert result['total_green'] == 2
    assert by_ticker['BBB']['pct_nav'] == pytest.approx(
        by_ticker['AAA']['pct_nav'] * OVERPERF_BONUS, abs=1e-6)


def test_rule_E_underperf_penalty():
    """d1.kind='under' multiplies by 0.7. Below MIN_EFFECTIVE_PCT → veto."""
    sig_under = _make_signal(p_t1=0.55,
                              d1={'kind': 'under', 'sigma_delta': -2.5})
    handoff = _make_handoff([sig_under], regime_scale=1.0)
    result = size_orders(handoff, _make_manifest(S_test='live'))

    # half_kelly(0.55, 2)=0.5*0.325=0.1625 → clip to 0.05 → ×0.7 → 0.035 → kept
    assert result['total_green'] == 1
    assert result['orders'][0]['pct_nav'] == pytest.approx(0.05 * UNDERPERF_PENALTY,
                                                           abs=1e-5)

    # Now a tiny-EV signal where the underperf penalty drops below floor.
    # p=0.32 R=2 → f*=(0.64-0.68)/2=-0.02 → negative_kelly veto. Use p=0.34
    # → f*=(0.68-0.66)/2=0.01 → half_kelly=0.005 → ×0.7=0.0035 → above floor.
    # Try regime scale to drag below floor.
    sig_below = _make_signal(p_t1=0.34, d1={'kind': 'under', 'sigma_delta': -3})
    handoff2 = _make_handoff([sig_below], regime_scale=0.15)  # CRISIS
    result2 = size_orders(handoff2, _make_manifest(S_test='live'))
    assert result2['total_green'] == 0
    assert result2['vetoed'][0]['reason'] == 'below_min_effective_pct'


def test_rule_BF_strategy_wide_compose():
    """Signal with d1.kind='under' AND strategy with rejected≥5 AND
    underperf≥3 → 0.7×0.7×0.8 = 0.392 multiplier composes correctly."""
    sig = _make_signal(p_t1=0.55, d1={'kind': 'under', 'sigma_delta': -2})
    handoff = _make_handoff(
        [sig], regime_scale=1.0,
        d1_strategy_stats={'S_test': {'rejected': 5, 'underperf': 3, 'overperf': 0}},
    )
    result = size_orders(handoff, _make_manifest(S_test='live'))

    assert result['total_green'] == 1
    expected = (
        MAX_POSITION_PCT                # half-Kelly clipped to cap
        * UNDERPERF_PENALTY             # rule E (per-signal)
        * STRATEGY_REJECTED_MULTIPLIER  # rule B (rejected ≥ 5)
        * STRATEGY_UNDER_MULTIPLIER     # rule F (underperf ≥ 3)
    )  # = 0.05 * 0.7 * 0.7 * 0.8 = 0.0196
    assert result['orders'][0]['pct_nav'] == pytest.approx(expected, abs=1e-5)


def test_lifecycle_candidate_vetoed():
    """Manifest state='candidate' → vetoed (multiplier 0.0).
    state='paper' → 0.5×.  state='live' → 1.0×."""
    sig = _make_signal()
    base_result = size_orders(_make_handoff([sig], regime_scale=1.0),
                              _make_manifest(S_test='live'))
    paper_result = size_orders(_make_handoff([sig], regime_scale=1.0),
                               _make_manifest(S_test='paper'))
    cand_result  = size_orders(_make_handoff([sig], regime_scale=1.0),
                               _make_manifest(S_test='candidate'))

    assert base_result['total_green'] == 1
    assert paper_result['total_green'] == 1
    assert paper_result['orders'][0]['pct_nav'] == pytest.approx(
        base_result['orders'][0]['pct_nav'] * 0.5, abs=1e-6)

    assert cand_result['total_green'] == 0
    assert cand_result['vetoed'][0]['reason'] == 'lifecycle_candidate'


def test_daily_notional_cap_pro_rata_scales_all():
    """Many high-Kelly signals exceeding 25% NAV → sizer pro-rata
    scales every surviving order so the aggregate equals 25% NAV.
    Every signal still gets a slice — none dropped purely for cap."""
    # Each signal independently sizes to MAX_POSITION_PCT (0.05). 30 signals
    # → 1.5 NAV worth of orders, way over the 0.25 cap. Pro-rata scale
    # factor = 0.25 / 1.5 = 1/6. Every order ends at ~0.0083 (above
    # MIN_EFFECTIVE_PCT=0.001). Total = 0.25.
    signals = []
    for i in range(30):
        s = _make_signal()
        s['ticker']      = f'TKR{i:02d}'
        s['strategy_id'] = 'S_test'
        s['ev_gbm']      = 0.001 + 0.001 * i  # ascending EV; idx 29 is highest
        signals.append(s)
    handoff = _make_handoff(signals, regime_scale=1.0)
    result = size_orders(handoff, _make_manifest(S_test='live'))

    total_pct = sum(o['pct_nav'] for o in result['orders'])
    assert total_pct == pytest.approx(MAX_DAILY_NEW_NOTIONAL_PCT, abs=1e-4)
    # Every signal kept (each above the post-scale floor).
    assert result['total_green'] == 30
    # Each order ≈ 0.0083 NAV.
    for o in result['orders']:
        assert o['pct_nav'] == pytest.approx(0.25 / 30, abs=1e-4)
        assert 'pct_nav_pre_scale' in o   # bookkeeping field added on scale


def test_daily_cap_pro_rata_drops_only_below_floor():
    """When the pro-rata scale-down pushes a signal below
    MIN_EFFECTIVE_PCT, that signal IS vetoed (with the post-scale reason)
    while the rest stay. This is the only legitimate drop path."""
    # Build a population where one signal has a far-smaller raw Kelly than
    # the others — after scale-down it falls under the noise floor.
    signals = []
    # 95 high-Kelly signals (each clip-saturated at MAX_POSITION_PCT=0.05)
    for i in range(95):
        s = _make_signal()
        s['ticker']      = f'BIG{i:02d}'
        s['strategy_id'] = 'S_test'
        s['ev_gbm']      = 0.05 + 0.0001 * i
        signals.append(s)
    handoff = _make_handoff(signals, regime_scale=1.0)
    result = size_orders(handoff, _make_manifest(S_test='live'))
    # 95 × 0.05 = 4.75 raw → scale 0.25/4.75 = 0.0526 → each ≈ 0.00263.
    # 0.00263 > 0.001 → all kept.
    assert result['total_green'] == 95
    total_pct = sum(o['pct_nav'] for o in result['orders'])
    assert total_pct == pytest.approx(MAX_DAILY_NEW_NOTIONAL_PCT, abs=1e-3)


def test_below_floor_post_scale_vetoed():
    """If many tiny-Kelly signals drag down the pro-rata factor enough to
    push some below MIN_EFFECTIVE_PCT, those specific orders are vetoed
    with reason='below_min_effective_post_scale'; the rest survive."""
    # Mix one big-Kelly signal with 999 tiny-Kelly ones. Big stays;
    # tinies fall below the floor after scale-down.
    signals = [_make_signal()]   # half-Kelly clips to 0.05
    signals[0]['ticker'] = 'BIG'
    # 999 marginal-positive-Kelly signals: p=0.34 R=2 → f*=0.01 →
    # half_kelly=0.005 each. Total raw = 0.05 + 999*0.005 = 5.045.
    # Scale = 0.25 / 5.045 = 0.0496. Each tiny → 0.005 * 0.0496 = 0.000248
    # < MIN_EFFECTIVE_PCT (0.001) → all 999 vetoed by floor.
    for i in range(999):
        s = _make_signal()
        s['ticker'] = f'TNY{i:03d}'
        s['p_t1']   = 0.34
        signals.append(s)
    handoff = _make_handoff(signals, regime_scale=1.0)
    result = size_orders(handoff, _make_manifest(S_test='live'))
    # Big survives.
    big = [o for o in result['orders'] if o['ticker'] == 'BIG']
    assert len(big) == 1
    # Tinies are all in vetoed with the post-scale reason.
    tnies_vetoed = [v for v in result['vetoed']
                    if v.get('reason') == 'below_min_effective_post_scale']
    assert len(tnies_vetoed) == 999


def test_schema_matches_alpaca_executor_contract():
    """Sized payload includes every field alpaca_executor.execute_single
    requires: ticker, direction, entry, stop, t1 (or target), pct_nav."""
    sig = _make_signal()
    result = size_orders(_make_handoff([sig], regime_scale=1.0),
                         _make_manifest(S_test='live'))
    o = result['orders'][0]
    for required in ('ticker', 'direction', 'entry', 'stop', 't1', 'pct_nav'):
        assert required in o, f'missing required field: {required}'
    # Sized payload top-level shape
    for top in ('cycle_date', 'regime', 'orders', 'vetoed'):
        assert top in result


def test_regime_scale_from_handoff():
    """The sizer reads `handoff.regime.scale` directly. Halving the scale
    halves the resulting pct_nav (assuming Kelly was already at the cap)."""
    sig = _make_signal()
    full  = size_orders(_make_handoff([sig], regime_scale=1.0),
                        _make_manifest(S_test='live'))
    half  = size_orders(_make_handoff([sig], regime_scale=0.5),
                        _make_manifest(S_test='live'))
    assert half['orders'][0]['pct_nav'] == pytest.approx(
        full['orders'][0]['pct_nav'] * 0.5, abs=1e-6)


def test_malformed_signal_vetoed():
    """Signals with stop ≥ entry (LONG) are inverted brackets → R≤0 veto."""
    bad = _make_signal(stop=105.0)  # stop ABOVE entry on a LONG
    result = size_orders(_make_handoff([bad]),
                         _make_manifest(S_test='live'))
    assert result['total_green'] == 0
    assert result['vetoed'][0]['reason'] == 'malformed_signal_R<=0'


def test_negative_kelly_vetoed():
    """Low-probability signals where (p*R - (1-p)) < 0 → veto."""
    # p=0.3 R=2: f* = (0.6 - 0.7)/2 = -0.05 < 0 → veto.
    sig = _make_signal(p_t1=0.3)
    result = size_orders(_make_handoff([sig]),
                         _make_manifest(S_test='live'))
    assert result['total_green'] == 0
    assert result['vetoed'][0]['reason'] == 'negative_kelly'
