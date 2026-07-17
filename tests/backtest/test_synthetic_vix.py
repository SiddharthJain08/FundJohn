"""tests/test_synthetic_vix.py

Unit tests for src/ingestion/synthetic_vix.py — Cboe variance-swap
synthetic VIX library.

Strategy: build SYNTHETIC chains where the ground-truth IV is known and
flat across strikes. Under a flat IV surface the variance-swap formula
reduces (in the limit of dense strikes) to σ² = IV², so VIX_synth → IV·100.
We pin per-test absolute tolerance (typically 1-2 vol points) reflecting
the discretisation error of finite strike grids.

Run:
    pytest tests/test_synthetic_vix.py -v
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]

# Load synthetic_vix.py directly without importing the `ingestion`
# package — `src/ingestion/__init__.py` has a pre-existing broken
# import (`fetch_polygon_universe`) we can't touch from this PR. The
# library has no internal package-level dependencies.
_spec = importlib.util.spec_from_file_location(
    'synthetic_vix', ROOT / 'src' / 'ingestion' / 'synthetic_vix.py',
)
synthetic_vix_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(synthetic_vix_mod)

bs_price            = synthetic_vix_mod.bs_price
compute_synthetic_vix = synthetic_vix_mod.compute_synthetic_vix
fit_svi             = synthetic_vix_mod.fit_svi
svi_iv              = synthetic_vix_mod.svi_iv


# ── Synthetic chain builder ──────────────────────────────────────────────────

def _build_flat_iv_chain(spot: float, t_days: float, iv: float,
                         strike_lo: float, strike_hi: float,
                         strike_step: float, t_now: pd.Timestamp,
                         r: float = 0.04) -> pd.DataFrame:
    """Build a synthetic option chain at one expiry with constant IV
    across all strikes. Bid/ask are set to the BS theoretical price ±
    a 0.01 spread (mid = exact BS price)."""
    expiry = t_now + pd.Timedelta(days=t_days)
    rows = []
    t_years = t_days / 365.0
    for k in np.arange(strike_lo, strike_hi + 1e-6, strike_step):
        for opt_type in ('C', 'P'):
            theo = bs_price(spot, float(k), t_years, r, 0.0, iv, opt_type)
            spread = 0.01
            rows.append({
                'expiration_date': expiry,
                'strike':          float(k),
                'option_type':     opt_type,
                'bid':             max(0.0, theo - spread / 2),
                'ask':             theo + spread / 2,
                'iv':              iv,
            })
    return pd.DataFrame(rows)


def _two_expiry_chain(spot, t_days_near, t_days_next, iv_near, iv_next,
                      strike_lo, strike_hi, strike_step, t_now):
    """Combine two flat-IV chains so the bracket-and-interpolate path
    is exercised."""
    near = _build_flat_iv_chain(spot, t_days_near, iv_near,
                                 strike_lo, strike_hi, strike_step, t_now)
    next_ = _build_flat_iv_chain(spot, t_days_next, iv_next,
                                  strike_lo, strike_hi, strike_step, t_now)
    return pd.concat([near, next_], ignore_index=True)


# ── Tests: variance-swap accuracy ────────────────────────────────────────────

class TestFlatIvAccuracy:
    """Under a flat IV surface, VIX_synth should converge to IV * 100
    as the strike grid gets denser. Pin tolerance per test."""

    def test_flat_iv_low_vol_recovers_input(self):
        """IV=15% across strikes, 30d expiry → VIX_synth ≈ 15."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        spot = 580.0
        chain = _two_expiry_chain(
            spot, t_days_near=23, t_days_next=37,
            iv_near=0.15, iv_next=0.15,
            strike_lo=400, strike_hi=760, strike_step=2.5,
            t_now=t_now,
        )
        result = compute_synthetic_vix(chain, spot=spot, t_now=t_now)
        # Tolerance 1 vol point: discretisation + truncation error on 360-strike grid.
        assert not result['fallback_flag']
        assert abs(result['vix_synth_30d'] - 15.0) < 1.0, \
            f"got {result['vix_synth_30d']}, expected ~15.0"

    def test_flat_iv_elevated_recovers_input(self):
        """IV=30% across strikes → VIX_synth ≈ 30."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        spot = 580.0
        chain = _two_expiry_chain(
            spot, t_days_near=25, t_days_next=35,
            iv_near=0.30, iv_next=0.30,
            strike_lo=200, strike_hi=950, strike_step=5,
            t_now=t_now,
        )
        result = compute_synthetic_vix(chain, spot=spot, t_now=t_now)
        assert not result['fallback_flag']
        # Higher IV widens the relevant strike range; tolerance ~1.5.
        assert abs(result['vix_synth_30d'] - 30.0) < 1.5, \
            f"got {result['vix_synth_30d']}, expected ~30.0"

    def test_flat_iv_crisis_recovers_input(self):
        """IV=60% across strikes → VIX_synth ≈ 60."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        spot = 400.0
        chain = _two_expiry_chain(
            spot, t_days_near=24, t_days_next=38,
            iv_near=0.60, iv_next=0.60,
            strike_lo=50, strike_hi=850, strike_step=10,
            t_now=t_now,
        )
        result = compute_synthetic_vix(chain, spot=spot, t_now=t_now)
        assert not result['fallback_flag']
        # Crisis IV needs much wider strike range; tolerance 3 vol points.
        assert abs(result['vix_synth_30d'] - 60.0) < 3.0, \
            f"got {result['vix_synth_30d']}, expected ~60.0"


class TestTermStructure:
    """Bracketing for both 30d and 90d horizons; term_slope should
    follow the input curve."""

    def test_term_slope_upward(self):
        """Near IV=15%, far IV=20% → term_slope > 1 (upward)."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        spot = 580.0
        # Need expiries bracketing BOTH 30 and 90 days.
        chain_parts = [
            _build_flat_iv_chain(spot, 23, 0.15, 400, 760, 2.5, t_now),
            _build_flat_iv_chain(spot, 37, 0.16, 400, 760, 2.5, t_now),
            _build_flat_iv_chain(spot, 80, 0.19, 400, 760, 2.5, t_now),
            _build_flat_iv_chain(spot, 100, 0.20, 400, 760, 2.5, t_now),
        ]
        chain = pd.concat(chain_parts, ignore_index=True)
        result = compute_synthetic_vix(chain, spot=spot, t_now=t_now)
        assert not result['fallback_flag']
        assert result['term_slope'] > 1.0
        assert 1.1 < result['term_slope'] < 1.5

    def test_term_slope_inverted_during_stress(self):
        """Near IV=40% (stress), far IV=25% → term_slope < 1 (inverted)."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        spot = 580.0
        chain_parts = [
            _build_flat_iv_chain(spot, 23, 0.40, 200, 950, 5, t_now),
            _build_flat_iv_chain(spot, 37, 0.38, 200, 950, 5, t_now),
            _build_flat_iv_chain(spot, 80, 0.27, 200, 950, 5, t_now),
            _build_flat_iv_chain(spot, 100, 0.25, 200, 950, 5, t_now),
        ]
        chain = pd.concat(chain_parts, ignore_index=True)
        result = compute_synthetic_vix(chain, spot=spot, t_now=t_now)
        assert not result['fallback_flag']
        assert result['term_slope'] < 1.0


class TestEmptyAndDegenerate:
    """Boundary conditions: empty / sparse chains return fallback_flag,
    don't raise."""

    def test_empty_chain_returns_nan_with_flag(self):
        result = compute_synthetic_vix(pd.DataFrame(), spot=580.0)
        assert math.isnan(result['vix_synth_30d'])
        assert result['fallback_flag'] is True
        assert result['strikes_used'] == 0

    def test_only_zero_dte_chain_excluded(self):
        """Chain with only ≤7 DTE contracts → excluded → fallback."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        chain = _build_flat_iv_chain(580.0, 3, 0.15, 540, 620, 2.5, t_now)
        result = compute_synthetic_vix(chain, spot=580.0, t_now=t_now,
                                        exclude_zero_dte=True)
        assert math.isnan(result['vix_synth_30d'])
        assert result['fallback_flag'] is True

    def test_zero_dte_included_when_flag_off(self):
        """Same chain, exclude_zero_dte=False → uses the 3DTE expiry."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        chain = pd.concat([
            _build_flat_iv_chain(580.0, 3,  0.15, 540, 620, 2.5, t_now),
            _build_flat_iv_chain(580.0, 5,  0.15, 540, 620, 2.5, t_now),
            _build_flat_iv_chain(580.0, 80, 0.15, 540, 620, 2.5, t_now),
            _build_flat_iv_chain(580.0, 100, 0.15, 540, 620, 2.5, t_now),
        ], ignore_index=True)
        result = compute_synthetic_vix(chain, spot=580.0, t_now=t_now,
                                        exclude_zero_dte=False)
        # With 0DTE included the 30-day extrapolation works off short-dated
        # chains. Tolerance is wider (±2 vol pts) since we're projecting
        # 3-5 day chains forward to 30d.
        assert not result['fallback_flag']
        assert abs(result['vix_synth_30d'] - 15.0) < 2.5

    def test_chain_with_missing_calls_returns_nan(self):
        """Puts-only chain — no put-call parity → fallback."""
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        chain = _build_flat_iv_chain(580.0, 30, 0.15, 540, 620, 2.5, t_now)
        chain = chain[chain['option_type'] == 'P']
        result = compute_synthetic_vix(chain, spot=580.0, t_now=t_now)
        assert result['fallback_flag'] is True


class TestSviRoundtrip:
    """Fit SVI to known-IV samples and assert IV at sample strikes
    is recoverable."""

    def test_svi_recovers_flat_surface(self):
        """Flat IV surface → SVI fit recovers near-flat IV across
        strike range. Residuals < 0.5 vol points."""
        spot = 580.0
        forward = 580.0
        t = 30 / 365.0
        strikes = np.linspace(450, 720, 40)
        ivs = np.full_like(strikes, 0.20)
        params = fit_svi(strikes, ivs, forward=forward, t=t)
        assert params is not None
        residuals = []
        for k in strikes:
            iv_pred = svi_iv(float(k), forward, t, params)
            residuals.append(abs(iv_pred - 0.20))
        max_resid = max(residuals)
        assert max_resid < 0.005, f"max residual {max_resid:.4f} > 0.005"


class TestStrikesUsed:
    """`strikes_used` reports the strike count the variance sum used,
    summed across both expiries."""

    def test_strikes_used_nonzero_on_valid_chain(self):
        t_now = pd.Timestamp('2026-05-08 14:00', tz='UTC')
        chain = _two_expiry_chain(
            580.0, 23, 37, 0.15, 0.15, 400, 760, 2.5, t_now,
        )
        result = compute_synthetic_vix(chain, spot=580.0, t_now=t_now)
        # Wide grid (~145 strikes per expiry × 2 expiries × 2 horizons).
        # Lower bound 200 to allow for the Cboe zero-bid run truncation.
        assert result['strikes_used'] > 200
