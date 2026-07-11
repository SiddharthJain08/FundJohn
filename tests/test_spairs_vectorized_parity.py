"""Parity tests: vectorized S_pairs_trading_jump_diffusion_intraday vs the
frozen O(n^2) per-pair-lstsq reference implementation.

The reference below (`_ReferenceSpairs`) is a VERBATIM copy of the strategy's
`_fit_ou` + `generate_signals` as of commit 5c52078 (pre-vectorization). The
vectorized reimplementation must emit an IDENTICAL signal list on every panel.

Degenerate-design coverage (rank-deficient lstsq min-norm behavior):
  - exactly-constant regressor column in the first OLS (delisted/ffilled)
  - exactly-constant spread head in the AR(1) step
  - all-constant pair (must be filtered by BOTH implementations)
  - near-constant-but-not-exact column (must stay on the FULL-RANK path)
"""
from __future__ import annotations

import sys
from collections import Counter
from typing import List

import numpy as np
import pandas as pd
import pytest

from strategies.base import BaseStrategy, Signal
from strategies.implementations.S_pairs_trading_jump_diffusion_intraday import (
    PairsTradingJumpDiffusionIntraday,
    _scan_pairs,
)


# ─────────────────────────────────────────────────────────────────────────────
# Frozen reference — verbatim copy of the pre-vectorization implementation.
# Do NOT edit this class when changing the strategy: it is the parity oracle.
# ─────────────────────────────────────────────────────────────────────────────
class _ReferenceSpairs(BaseStrategy):
    id          = 'S_pairs_trading_jump_diffusion_intraday'
    name        = 'PairsTradingJumpDiffusionIntraday'
    description = 'frozen reference copy'
    tier        = 2
    active_in_regimes = ['LOW_VOL', 'TRANSITIONING', 'HIGH_VOL']

    LOOKBACK  = 252
    TOP_K     = 5
    ENTRY_Z   = 1.5
    BASE_SIZE = 0.04   # fraction per leg

    def _fit_ou(self, spread: np.ndarray):
        """Fit AR(1) to spread; return (kappa_annual, mu_eq, sigma_eq) or None."""
        y, x = spread[1:], spread[:-1]
        X = np.column_stack([x, np.ones(len(x))])
        coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
        b, a = float(coeffs[0]), float(coeffs[1])
        if not (0.0 < b < 1.0):
            return None
        kappa = -np.log(b) * 252.0
        mu_eq = a / (1.0 - b)
        resid = y - (a + b * x)
        sigma_eq = float(np.std(resid)) / max(np.sqrt(1.0 - b ** 2), 1e-8)
        return float(kappa), float(mu_eq), sigma_eq

    def generate_signals(
        self, prices: pd.DataFrame, regime: dict, universe: List[str], aux_data: dict = None
    ) -> List[Signal]:
        if prices is None or prices.empty:
            return []
        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []
        scale = self.position_scale(regime_state)

        available = [t for t in universe if t in prices.columns]
        if len(available) < 4 or len(prices) < self.LOOKBACK:
            print(f'[debug] signals=0 (n={len(available)}, rows={len(prices)})', file=sys.stderr)
            return []

        log_px = np.log(prices[available].ffill().dropna(how='all'))
        if len(log_px) < self.LOOKBACK:
            print(f'[debug] signals=0 (log dropna reduced rows below lookback)', file=sys.stderr)
            return []

        recent = log_px.iloc[-self.LOOKBACK:].values
        cur_log = log_px.iloc[-1].values
        cur_px  = prices[available].iloc[-1]
        tickers = list(log_px.columns)
        n = len(tickers)

        pair_scores = []
        for i in range(n):
            for j in range(i + 1, n):
                si, sj = recent[:, i], recent[:, j]
                if np.isnan(si).any() or np.isnan(sj).any():
                    continue
                X = np.column_stack([sj, np.ones(len(sj))])
                coeffs, _, _, _ = np.linalg.lstsq(X, si, rcond=None)
                beta, alpha = float(coeffs[0]), float(coeffs[1])
                spread = si - beta * sj - alpha
                result = self._fit_ou(spread)
                if result is None:
                    continue
                kappa, mu_eq, sigma_eq = result
                if sigma_eq < 1e-8 or kappa < 5.0:
                    continue
                cur_spread = float(cur_log[i] - beta * cur_log[j] - alpha)
                zscore = (cur_spread - mu_eq) / sigma_eq
                pair_scores.append((kappa, tickers[i], tickers[j], beta, alpha, mu_eq, sigma_eq, zscore))

        pair_scores.sort(key=lambda x: x[0], reverse=True)
        signals: List[Signal] = []

        for kappa, ti, tj, beta, alpha, mu_eq, sigma_eq, zscore in pair_scores[:self.TOP_K]:
            # Individualized threshold — higher kappa → tighter entry
            entry_z = max(1.0, self.ENTRY_Z - kappa / 500.0)
            if abs(zscore) < entry_z:
                continue
            pi = float(cur_px[ti])
            pj = float(cur_px[tj])
            if not (np.isfinite(pi) and pi > 0.0 and np.isfinite(pj) and pj > 0.0):
                continue
            dir_i = 'LONG' if zscore < -entry_z else 'SHORT'
            dir_j  = 'SHORT' if dir_i == 'LONG' else 'LONG'
            conf   = 'HIGH' if abs(zscore) > 2.0 else 'MED'
            size   = round(float(self.BASE_SIZE * scale), 4)
            params = {
                'pair':   f'{ti}/{tj}',
                'zscore': round(float(zscore), 4),
                'kappa':  round(float(kappa), 4),
                'beta':   round(float(beta), 4),
                'entry_z': round(float(entry_z), 4),
            }
            st_i = self.compute_stops_and_targets(
                prices[ti].dropna(), dir_i, pi, regime_state=regime_state
            )
            st_j = self.compute_stops_and_targets(
                prices[tj].dropna(), dir_j, pj, regime_state=regime_state
            )
            signals.append(Signal(
                ticker=ti, direction=dir_i, entry_price=pi,
                stop_loss=st_i['stop'], target_1=st_i['t1'], target_2=st_i['t2'], target_3=st_i['t3'],
                position_size_pct=size, confidence=conf, signal_params=params,
            ))
            signals.append(Signal(
                ticker=tj, direction=dir_j, entry_price=pj,
                stop_loss=st_j['stop'], target_1=st_j['t1'], target_2=st_j['t2'], target_3=st_j['t3'],
                position_size_pct=size, confidence=conf, signal_params={**params, 'zscore': round(-float(zscore), 4)},
            ))
            if len(signals) >= self.MAX_SIGNALS:
                break

        signals = signals[:self.MAX_SIGNALS]
        print(f'[debug] signals={len(signals)}', file=sys.stderr)
        return signals


# ─────────────────────────────────────────────────────────────────────────────
# Per-pair reference stats (mirrors the frozen loop body; for intermediate
# comparison against the vectorized _scan_pairs candidates).
# ─────────────────────────────────────────────────────────────────────────────
def _reference_pair_stats(recent: np.ndarray, cur_log: np.ndarray, i: int, j: int):
    """Return (kappa, beta, alpha, mu_eq, sigma_eq, zscore) or None (filtered)."""
    si, sj = recent[:, i], recent[:, j]
    if np.isnan(si).any() or np.isnan(sj).any():
        return None
    X = np.column_stack([sj, np.ones(len(sj))])
    coeffs, _, _, _ = np.linalg.lstsq(X, si, rcond=None)
    beta, alpha = float(coeffs[0]), float(coeffs[1])
    spread = si - beta * sj - alpha
    y, x = spread[1:], spread[:-1]
    Xa = np.column_stack([x, np.ones(len(x))])
    co, _, _, _ = np.linalg.lstsq(Xa, y, rcond=None)
    b, a = float(co[0]), float(co[1])
    if not (0.0 < b < 1.0):
        return None
    kappa = float(-np.log(b) * 252.0)
    mu_eq = float(a / (1.0 - b))
    resid = y - (a + b * x)
    sigma_eq = float(np.std(resid)) / max(np.sqrt(1.0 - b ** 2), 1e-8)
    if sigma_eq < 1e-8 or kappa < 5.0:
        return None
    cur_spread = float(cur_log[i] - beta * cur_log[j] - alpha)
    zscore = (cur_spread - mu_eq) / sigma_eq
    return kappa, beta, alpha, mu_eq, sigma_eq, zscore


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic panel builders
# ─────────────────────────────────────────────────────────────────────────────
def _random_walk(rng, n_bars, p0=50.0, vol=0.02):
    return p0 * np.exp(rng.normal(0.0, vol, n_bars).cumsum())


def _build_panel(seed: int, n_rw: int = 70, n_bars: int = 400) -> pd.DataFrame:
    """Random walks + planted cointegrated pairs + constant columns +
    leading-NaN columns + a near-constant column + a head-constant column
    that jumps near the end (AR-degenerate-with-signal-potential case)."""
    rng = np.random.default_rng(seed)
    cols: dict[str, np.ndarray] = {}
    for k in range(n_rw):
        cols[f'RW{k:03d}'] = _random_walk(rng, n_bars, p0=rng.uniform(10, 200))
    # Planted cointegrated pairs: log partner = log base + const + strong-OU spread
    for k in range(3):
        base = _random_walk(rng, n_bars, p0=rng.uniform(30, 120), vol=0.015)
        b_true = rng.uniform(0.2, 0.6)          # kappa_true = -ln(b)*252 in [129, 405]
        eps = rng.normal(0.0, 0.01, n_bars)
        sp = np.zeros(n_bars)
        for t in range(1, n_bars):
            sp[t] = b_true * sp[t - 1] + eps[t]
        sp[-1] += rng.choice([-1.0, 1.0]) * 0.06  # push final z-score out
        cols[f'CPA{k}'] = base
        cols[f'CPB{k}'] = np.exp(np.log(base) + 0.1 + sp)
    # Exactly-constant columns (delisted tickers ffilled forever)
    cols['CONST1'] = np.full(n_bars, 42.17)
    cols['CONST2'] = np.full(n_bars, 7.03)
    # Delisted mid-panel: real walk that freezes at bar 150
    dl = _random_walk(rng, n_bars, p0=60.0)
    dl[150:] = dl[150]
    cols['DELIST1'] = dl
    # Leading NaNs (listed late) — complete-column set grows over time
    ln = _random_walk(rng, n_bars, p0=30.0)
    ln[:120] = np.nan
    cols['LATE1'] = ln
    ln2 = _random_walk(rng, n_bars, p0=90.0)
    ln2[:390] = np.nan   # never complete within any 252-window of this panel
    cols['LATE2'] = ln2
    # Near-constant column (NOT exactly constant → must stay full-rank path).
    # 1e-5 relative noise ≈ a penny-quantized stale ticker: design condition
    # ~1e6, where lstsq's SVD forward error stays far below 4-dp rounding.
    # (At 1e-9 noise the condition hits ~1e10 and lstsq itself is off the TRUE
    # OLS beta by ~2e-7 relative — see
    # TestDegenerateClosedForms::test_extreme_near_constant_lstsq_forward_error,
    # which pins that adversarial regime with longdouble ground truth.)
    cols['NEARC1'] = 55.5 * (1.0 + rng.normal(0.0, 1e-5, n_bars))
    # Head-constant column that first moves at bar 380: at bar index 380 the
    # 252-window is constant on rows [0..250] and changes only at the last row
    # → AR(1) design exactly rank-deficient (min-norm path) with signal potential.
    hc = np.full(n_bars, 77.7)
    hc[380:] = 77.7 * np.exp(rng.normal(0.0, 0.03, n_bars - 380).cumsum())
    cols['HCJ1'] = hc
    idx = pd.date_range('2020-01-02', periods=n_bars, freq='B')
    return pd.DataFrame(cols, index=idx)


def _universe_for(panel: pd.DataFrame, seed: int) -> list[str]:
    """Shuffled universe incl. tickers absent from the panel (exercises the
    universe∩columns intersection in universe order)."""
    rng = np.random.default_rng(seed + 1000)
    u = list(panel.columns)
    rng.shuffle(u)
    u.insert(3, 'NOT_IN_PANEL_1')
    u.append('NOT_IN_PANEL_2')
    return u


REGIME = {'state': 'LOW_VOL'}


def _signals_equal(a: list[Signal], b: list[Signal]) -> bool:
    return a == b


def _assert_bar_parity(panel, universe, bars, regime=REGIME):
    ref = _ReferenceSpairs()
    new = PairsTradingJumpDiffusionIntraday()
    total = 0
    for k in bars:
        sub = panel.iloc[:k]
        s_ref = ref.generate_signals(sub, regime, universe)
        s_new = new.generate_signals(sub, regime, universe)
        assert _signals_equal(s_new, s_ref), (
            f'signal mismatch at bar {k} (panel len {len(sub)}):\n'
            f'ref={s_ref}\nnew={s_new}'
        )
        total += len(s_ref)
    return total


# ─────────────────────────────────────────────────────────────────────────────
# 1. Frozen-reference full-pipeline parity on synthetic panels
# ─────────────────────────────────────────────────────────────────────────────
class TestFullPipelineParity:
    def test_panel_a_20_bars_identical_signals(self):
        panel = _build_panel(seed=7)
        universe = _universe_for(panel, seed=7)
        bars = list(range(381, 401))          # 20 distinct bars
        total = _assert_bar_parity(panel, universe, bars)
        # the test must actually exercise the emit path
        assert total > 0, 'parity test never emitted a signal — panel too tame'

    def test_panel_b_different_seed_identical_signals(self):
        panel = _build_panel(seed=21, n_rw=50)
        universe = _universe_for(panel, seed=21)
        bars = [355, 362, 370, 377, 384, 391, 396, 400]
        _assert_bar_parity(panel, universe, bars)

    def test_bar_covering_ar_degenerate_window(self):
        """Bar 381: HCJ1's window is head-constant with a last-row tick — the
        AR(1) min-norm path is live for (CONST*, HCJ1) pairs."""
        panel = _build_panel(seed=7)
        universe = _universe_for(panel, seed=7)
        _assert_bar_parity(panel, universe, [381])

    def test_regime_gate_and_guards_identical(self):
        panel = _build_panel(seed=7)
        universe = _universe_for(panel, seed=7)
        ref = _ReferenceSpairs()
        new = PairsTradingJumpDiffusionIntraday()
        # CRISIS regime → should_run False → both empty
        assert new.generate_signals(panel, {'state': 'CRISIS'}, universe) == []
        assert ref.generate_signals(panel, {'state': 'CRISIS'}, universe) == []
        # too few rows
        short = panel.iloc[:200]
        assert new.generate_signals(short, REGIME, universe) == \
               ref.generate_signals(short, REGIME, universe) == []
        # too few tickers
        u4 = [c for c in panel.columns[:3]]
        assert new.generate_signals(panel, REGIME, u4) == \
               ref.generate_signals(panel, REGIME, u4) == []
        # empty / None prices
        assert new.generate_signals(pd.DataFrame(), REGIME, universe) == []
        assert new.generate_signals(None, REGIME, universe) == []

    def test_position_scale_parity_other_regime(self):
        panel = _build_panel(seed=7)
        universe = _universe_for(panel, seed=7)
        _assert_bar_parity(panel, universe, [388, 400], regime={'state': 'HIGH_VOL'})


# ─────────────────────────────────────────────────────────────────────────────
# 2. Intermediate closed-form vs per-pair lstsq (candidate set + values)
# ─────────────────────────────────────────────────────────────────────────────
class TestIntermediateParity:
    def _window(self, seed=7, bar=400):
        panel = _build_panel(seed=seed)
        universe = _universe_for(panel, seed=seed)
        available = [t for t in universe if t in panel.columns]
        log_px = np.log(panel.iloc[:bar][available].ffill().dropna(how='all'))
        recent = log_px.iloc[-252:].values
        cur_log = log_px.iloc[-1].values
        return recent, cur_log, available

    def test_candidate_set_and_values_match_lstsq(self):
        recent, cur_log, available = self._window()
        n = recent.shape[1]
        cands = _scan_pairs(recent, cur_log, top_k=None)
        got = {(i, j): (k, b, a, m, s, z) for (k, i, j, b, a, m, s, z) in cands}

        # identify degenerate-adjacent columns (exact + near-constant windows)
        win = recent
        exact_const = [c for c in range(n) if not np.isnan(win[:, c]).any()
                       and (win[:, c] == win[0, c]).all()]
        head_const = [c for c in range(n) if not np.isnan(win[:, c]).any()
                      and (win[:-1, c] == win[0, c]).all()]
        near_const_cols = [c for c in range(n) if not np.isnan(win[:, c]).any()
                           and c not in exact_const
                           and np.ptp(win[:, c]) < 1e-3]

        expected = {}
        for i in range(n):
            for j in range(i + 1, n):
                r = _reference_pair_stats(recent, cur_log, i, j)
                if r is not None:
                    expected[(i, j)] = r

        assert set(got.keys()) == set(expected.keys()), (
            f'candidate membership diverges: only-new='
            f'{sorted(set(got) - set(expected))[:10]} '
            f'only-ref={sorted(set(expected) - set(got))[:10]}'
        )
        degenerate_adjacent = set(exact_const) | set(head_const) | set(near_const_cols)
        checked_clean = 0
        for (i, j), (k_r, b_r, a_r, m_r, s_r, z_r) in expected.items():
            k_n, b_n, a_n, m_n, s_n, z_n = got[(i, j)]
            if i in degenerate_adjacent or j in degenerate_adjacent:
                rtol = 1e-6   # min-norm / ill-conditioned designs: SVD vs closed form
            else:
                rtol = 1e-8
                checked_clean += 1
            for name, v_r, v_n in (('kappa', k_r, k_n), ('beta', b_r, b_n),
                                   ('alpha', a_r, a_n), ('mu_eq', m_r, m_n),
                                   ('sigma_eq', s_r, s_n), ('zscore', z_r, z_n)):
                assert np.isclose(v_n, v_r, rtol=rtol, atol=1e-12), (
                    f'pair ({i},{j}) [{available[i]}/{available[j]}] {name}: '
                    f'ref={v_r!r} new={v_n!r} rtol={rtol}'
                )
        assert checked_clean >= 100, 'too few non-degenerate pairs exercised'

    def test_topk_selection_matches_full_sort(self):
        recent, cur_log, _ = self._window()
        all_c = _scan_pairs(recent, cur_log, top_k=None)
        top5 = _scan_pairs(recent, cur_log, top_k=5)
        assert top5 == all_c[:5]
        # stable order: kappa desc, ties by (i, j) enumeration order
        kaps = [c[0] for c in all_c]
        assert kaps == sorted(kaps, reverse=True)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Degenerate-design unit tests: closed forms vs np.linalg.lstsq
# ─────────────────────────────────────────────────────────────────────────────
class TestDegenerateClosedForms:
    def test_first_ols_constant_regressor_min_norm(self):
        rng = np.random.default_rng(0)
        T = 252
        for c in (42.17, 7.03, 0.5, -3.2):
            y = rng.normal(4.0, 0.3, T)
            X = np.column_stack([np.full(T, c), np.ones(T)])
            coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
            ybar = float(np.mean(y))
            beta_mn = c * ybar / (c * c + 1.0)
            alpha_mn = ybar / (c * c + 1.0)
            assert np.isclose(coeffs[0], beta_mn, rtol=1e-10, atol=1e-14), c
            assert np.isclose(coeffs[1], alpha_mn, rtol=1e-10, atol=1e-14), c
            # min-norm spread reduces to demeaning
            spread = y - coeffs[0] * c - coeffs[1]
            assert np.allclose(spread, y - ybar, atol=1e-10)

    def test_ar_constant_x_min_norm(self):
        rng = np.random.default_rng(1)
        N = 251
        for c in (0.013, -0.4, 2.0):
            y = np.full(N, c, dtype=float)
            y[-1] = c + 0.37          # head-constant spread with last-row tick
            x = np.full(N, c, dtype=float)
            X = np.column_stack([x, np.ones(N)])
            coeffs, _, _, _ = np.linalg.lstsq(X, y, rcond=None)
            ybar = float(np.mean(y))
            b_mn = c * ybar / (c * c + 1.0)
            a_mn = ybar / (c * c + 1.0)
            assert np.isclose(coeffs[0], b_mn, rtol=1e-10, atol=1e-14), c
            assert np.isclose(coeffs[1], a_mn, rtol=1e-10, atol=1e-14), c
            # residual std equals std of y (fitted value is constant ybar)
            resid = y - (coeffs[1] + coeffs[0] * x)
            assert np.isclose(float(np.std(resid)), float(np.std(y)), rtol=1e-9)

    def test_all_constant_pair_filtered_by_both(self):
        """spread ≡ 0 → AR min-norm gives b=0 → 0<b<1 fails → no candidate."""
        T = 252
        recent = np.column_stack([
            np.full(T, np.log(42.17)),
            np.full(T, np.log(7.03)),
            np.log(_random_walk(np.random.default_rng(3), T)),
            np.log(_random_walk(np.random.default_rng(4), T)),
        ])
        cur_log = recent[-1]
        assert _reference_pair_stats(recent, cur_log, 0, 1) is None
        cands = _scan_pairs(recent, cur_log, top_k=None)
        assert (0, 1) not in {(i, j) for (_, i, j, *_r) in cands}

    def test_near_constant_column_stays_full_rank(self):
        """1e-9-relative noise: lstsq must NOT trip its rcond cutoff — its
        solution matches the regular cov/var closed form, not min-norm."""
        rng = np.random.default_rng(5)
        T = 252
        c = np.log(55.5)
        sj = c + rng.normal(0.0, 1e-9, T)
        si = np.log(_random_walk(rng, T))
        X = np.column_stack([sj, np.ones(T)])
        coeffs, _, _, _ = np.linalg.lstsq(X, si, rcond=None)
        mu_i, mu_j = float(np.mean(si)), float(np.mean(sj))
        zc_i, zc_j = si - mu_i, sj - mu_j
        beta_reg = float(zc_i @ zc_j) / float(zc_j @ zc_j)
        alpha_reg = mu_i - beta_reg * mu_j
        beta_mn = c * mu_i / (c * c + 1.0)
        # full-rank OLS beta is huge (var ~1e-18 in denominator); min-norm is O(1)
        assert not np.isclose(coeffs[0], beta_mn, rtol=1e-3)
        assert np.isclose(coeffs[0], beta_reg, rtol=1e-6), \
            f'lstsq={coeffs[0]!r} closed={beta_reg!r}'
        assert np.isclose(coeffs[1], alpha_reg, rtol=1e-6)

    def test_extreme_near_constant_lstsq_forward_error(self):
        """Adversarial regime (documented boundary of FP parity): a column with
        1e-9 relative noise gives a design condition ~1e10. lstsq stays on the
        FULL-RANK path (sv ratio ~6e-11 >> rcond cutoff eps*252 ~ 5.6e-14) but
        its SVD forward error on beta is ~kappa_2*eps ~ 1e-7 relative, while
        the centered-Gram closed form matches longdouble ground truth to
        ~1e-13. So a 4-decimal-rounded beta of magnitude ~1e5 CAN differ
        between the two implementations here — this is lstsq's own error, not
        a defect of the closed form. Real panels don't hit this: ffilled
        delisted columns are EXACTLY constant (min-norm path, exact agreement)
        and live near-constant windows are tick-quantized (>=1e-5 relative)."""
        rng = np.random.default_rng(9)
        T = 252
        sj = np.log(55.5) + rng.normal(0.0, 1e-9, T)
        si = np.log(_random_walk(rng, T))
        X = np.column_stack([sj, np.ones(T)])
        coeffs, _res, rank, sv = np.linalg.lstsq(X, si, rcond=None)
        assert rank == 2, 'lstsq must treat 1e-9 noise as full rank'
        # longdouble ground truth for the OLS beta
        sil, sjl = si.astype(np.longdouble), sj.astype(np.longdouble)
        zil, zjl = sil - sil.mean(), sjl - sjl.mean()
        beta_true = float((zil @ zjl) / (zjl @ zjl))
        # float64 centered-Gram closed form (what _scan_pairs computes)
        zi, zj = si - si.mean(), sj - sj.mean()
        beta_closed = float(zi @ zj) / float(zj @ zj)
        err_closed = abs(beta_closed - beta_true) / abs(beta_true)
        err_lstsq = abs(float(coeffs[0]) - beta_true) / abs(beta_true)
        assert err_closed < 1e-10, f'closed form drifted from truth: {err_closed}'
        assert err_lstsq < 1e-5, f'lstsq forward error blew past its bound: {err_lstsq}'
        assert err_closed <= err_lstsq, 'closed form must not be worse than lstsq'

    def test_head_constant_pair_full_pipeline_agreement(self):
        """Both columns head-constant, one ticks at the last row: the AR design
        is exactly rank-deficient and the pair can legitimately produce a
        candidate. Vectorized values must match per-pair lstsq."""
        rng = np.random.default_rng(6)
        T = 252
        col_const = np.full(T, np.log(42.17))
        col_tick = np.full(T, np.log(77.7))
        col_tick[-1] = np.log(77.7 * 1.35)
        recent = np.column_stack([
            col_const, col_tick,
            np.log(_random_walk(rng, T)),
            np.log(_random_walk(rng, T)),
        ])
        cur_log = recent[-1]
        ref = _reference_pair_stats(recent, cur_log, 0, 1)
        cands = {(i, j): (k, b, a, m, s, z) for (k, i, j, b, a, m, s, z)
                 in _scan_pairs(recent, cur_log, top_k=None)}
        if ref is None:
            assert (0, 1) not in cands
        else:
            assert (0, 1) in cands, 'vectorized impl dropped an AR-degenerate candidate'
            k_r, b_r, a_r, m_r, s_r, z_r = ref
            k_n, b_n, a_n, m_n, s_n, z_n = cands[(0, 1)]
            assert np.isclose(k_n, k_r, rtol=1e-8)
            assert np.isclose(b_n, b_r, rtol=1e-8, atol=1e-12)
            assert np.isclose(s_n, s_r, rtol=1e-8, atol=1e-12)
            assert np.isclose(z_n, z_r, rtol=1e-6, atol=1e-10)


# ─────────────────────────────────────────────────────────────────────────────
# 4. Block-boundary robustness: _scan_pairs must be block-size invariant
# ─────────────────────────────────────────────────────────────────────────────
class TestBlocking:
    def test_block_size_invariance(self):
        panel = _build_panel(seed=11, n_rw=40)
        universe = _universe_for(panel, seed=11)
        available = [t for t in universe if t in panel.columns]
        log_px = np.log(panel[available].ffill().dropna(how='all'))
        recent = log_px.iloc[-252:].values
        cur_log = log_px.iloc[-1].values
        base = _scan_pairs(recent, cur_log, top_k=None, block=4096)
        for blk in (1, 2, 3, 7, 16, 33):
            alt = _scan_pairs(recent, cur_log, top_k=None, block=blk)
            assert alt == base, f'block={blk} changed the candidate list'

    def test_multiblock_topk_pruning_matches_unpruned_and_single_block(self):
        """Every committed test elsewhere runs single-block (m < the 512
        default) or with top_k=None, so the MULTI-BLOCK + per-block top-k
        pruning path that fires in production at n≈6300 is otherwise
        untested. block=8 on a panel with well over 8 complete columns forces
        several blocks, and a density check below proves pruning actually
        drops candidates in at least one of them (not just multi-block
        concatenation with nothing pruned). Per the docstring's
        pruning-exactness argument (any global top-k element is inside its
        own block's stable top-k under the same total order), the pruned
        multi-block result must exactly equal both the fully unpruned scan
        truncated to top_k, and a single-huge-block top_k scan — same
        candidates, same fields, same stable kappa-DESC/(i,j) order."""
        block = 8
        top_k = 5
        panel = _build_panel(seed=11, n_rw=40)
        universe = _universe_for(panel, seed=11)
        available = [t for t in universe if t in panel.columns]
        log_px = np.log(panel[available].ffill().dropna(how='all'))
        recent = log_px.iloc[-252:].values
        cur_log = log_px.iloc[-1].values
        complete_idx = np.flatnonzero(~np.isnan(recent).any(axis=0))
        n_complete = int(complete_idx.size)
        n_blocks = len(range(0, n_complete - 1, block))
        assert n_blocks >= 2, (
            'panel must yield >=2 blocks at block=8 (otherwise this test '
            'degenerates to single-block)'
        )

        unpruned = _scan_pairs(recent, cur_log, top_k=None, block=block)

        # Prove pruning is non-vacuous: some block's candidate count must
        # exceed top_k, else the multi-block/single-block/unpruned outputs
        # could agree trivially (nothing ever gets pruned) and this test
        # would pass against a broken per-block top-k selector.
        pos_of = {int(orig): k for k, orig in enumerate(complete_idx)}
        per_block_counts = Counter(pos_of[i] // block for (_k, i, _j, *_rest) in unpruned)
        assert per_block_counts and max(per_block_counts.values()) > top_k, (
            'no block produced more candidates than top_k — pruning would '
            'never fire, making this test vacuous'
        )

        multiblock_pruned = _scan_pairs(recent, cur_log, top_k=top_k, block=block)
        single_block_pruned = _scan_pairs(recent, cur_log, top_k=top_k, block=10_000)

        assert len(multiblock_pruned) == top_k
        assert multiblock_pruned == unpruned[:top_k]
        assert multiblock_pruned == single_block_pruned
