"""Parity tests for the vectorized S_tr_03_bocpd_change_point reimplementation.

The strategy's §7 re-backtest blew a 25-hour watchdog because the original
implementation ran a 126-step BOCPD recursion PER TICKER, calling
scipy.stats.t.logpdf (~100µs of rv_continuous dispatch) once per step —
~800k scipy calls per full-width bar. The reimplementation must be
mathematically equivalent: one cross-ticker vectorized recursion + a pure
numpy Student-t logpdf.

Oracle: the pre-vectorization implementation is copied VERBATIM below as
``_reference_nig_params`` / ``_reference_bocpd`` / ``_ReferenceBOCPD``.
Do NOT "fix" or modernize the reference — it is the parity contract.

Tolerances (per task brief):
  - _t_logpdf vs scipy.stats.t.logpdf: rtol 1e-12
  - cp_probs / run_dist vs reference:  rtol 1e-10 (expected drift ~1e-13)
  - generate_signals output:           identical signal lists (exact equality
                                       after the existing rounding)

The real-data spot-parity test is gated on OPENCLAW_TR03_PANEL (path to a
cached close_wide parquet produced via backtest.unified_backtest.
load_prices_panels) so the default suite stays parquet-free and fast-ish.
"""
from __future__ import annotations

import dataclasses
import os
import sys

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from strategies.base import Signal
from strategies.implementations.S_tr_03_bocpd_change_point import (
    BOCPDChangePoint,
    _bocpd,
    _bocpd_panel,
    _t_logpdf,
)


# ─── Frozen reference (pre-vectorization implementation, VERBATIM) ──────────

def _reference_nig_params(sums: np.ndarray, sq_sums: np.ndarray, n_obs: np.ndarray,
                          mu0: float, kappa0: float, alpha0: float, beta0: float):
    """Return NIG posterior parameters for each run length."""
    nn     = n_obs.astype(np.float64)
    kappa  = kappa0 + nn
    alpha  = alpha0 + nn * 0.5
    mu_r   = (kappa0 * mu0 + sums) / kappa
    xbar   = sums / np.maximum(nn, 1)
    var_r  = np.maximum(sq_sums / np.maximum(nn, 1) - xbar ** 2, 0.0)
    beta_r = (beta0
              + 0.5 * nn * var_r
              + 0.5 * kappa0 * nn * (xbar - mu0) ** 2 / kappa)
    dof    = 2.0 * alpha
    scale2 = np.maximum(beta_r * (kappa + 1.0) / (alpha * kappa), 1e-12)
    return mu_r, np.sqrt(scale2), dof


def _reference_bocpd(returns: np.ndarray, hazard_rate: float = 0.005) -> tuple[np.ndarray, np.ndarray]:
    """
    Bayesian Online Change Point Detection (Adams & MacKay 2007).
    Normal-Inverse-Gamma conjugate model → Student-t predictive.

    Correct update (Adams & MacKay eq. 3):
        R_t[0]   ∝ H × pred_prior(x_t)          ← prior predictive, NOT joint_sum
        R_t[r+1] ∝ (1-H) × pred_r(x_t) × R_{t-1}[r]

    Returns:
        cp_probs  — P(CP at t | x_{1:t}) = R_t[0], shape (T,)
        run_dist  — final posterior run-length distribution, shape (T+1,)
    """
    T = len(returns)
    if T < 10:
        return np.zeros(T), np.zeros(T + 1)

    mu0, kappa0, alpha0, beta0 = 0.0, 1.0, 1.0, 0.01
    log_h   = np.log(hazard_rate)
    log_1mh = np.log(1.0 - hazard_rate)

    # Work in log space for numerical stability
    log_R    = np.full(T + 1, -np.inf)
    log_R[0] = 0.0  # log(1)
    sums     = np.zeros(T + 1)
    sq_sums  = np.zeros(T + 1)
    n_obs    = np.zeros(T + 1, dtype=np.int32)
    cp_probs = np.zeros(T)

    for t in range(T):
        x  = returns[t]
        sl = slice(0, t + 1)

        mu_r, scale, dof = _reference_nig_params(
            sums[sl], sq_sums[sl], n_obs[sl], mu0, kappa0, alpha0, beta0
        )

        # Log predictive for each run length (absolute PDF values — NOT normalized)
        log_pred = stats.t.logpdf(x, df=dof, loc=mu_r, scale=scale)

        # Adams & MacKay eq. 3:
        #   log P_new[0]   = log(H)   + log_pred[0]               ← prior predictive
        #   log P_new[r+1] = log(1-H) + log_pred[r] + log_R[r]    ← continue
        log_R_new        = np.full(t + 2, -np.inf)
        log_R_new[0]     = log_h + log_pred[0]
        finite_R         = log_R[sl] > -np.inf
        if finite_R.any():
            idx = np.where(finite_R)[0]
            log_R_new[idx + 1] = log_1mh + log_pred[idx] + log_R[sl][idx]

        # Normalise in log space (log-sum-exp)
        finite           = log_R_new[:t + 2] > -np.inf
        max_val          = log_R_new[:t + 2][finite].max()
        log_norm         = max_val + np.log(np.exp(log_R_new[:t + 2][finite] - max_val).sum())
        log_R_new[:t+2] -= log_norm

        log_R                 = np.full(T + 1, -np.inf)
        log_R[:t + 2]         = log_R_new[:t + 2]
        cp_probs[t]           = float(np.exp(log_R_new[0]))

        # Shift sufficient statistics right
        sums[1 : t + 2]    = sums[sl] + x
        sq_sums[1 : t + 2] = sq_sums[sl] + x ** 2
        n_obs[1 : t + 2]   = n_obs[sl] + 1
        sums[0] = sq_sums[0] = n_obs[0] = 0

    return cp_probs, np.exp(log_R)


class _ReferenceBOCPD(BOCPDChangePoint):
    """Frozen copy of the pre-vectorization generate_signals (verbatim body,
    with _bocpd → _reference_bocpd). Inherits every constant + helper from the
    production class, so any decision difference comes from the BOCPD path."""

    def generate_signals(self, prices, regime, universe, aux_data=None):
        if prices is None or prices.empty:
            return []

        regime_state = regime.get('state', 'LOW_VOL')
        if not self.should_run(regime_state):
            return []

        tickers = [t for t in universe if t in prices.columns]
        if not tickers:
            return []

        price_data = prices[tickers].ffill()
        if len(price_data) < self.min_lookback:
            print(f'[debug] {self.id}: signals=0 (need {self.min_lookback} rows)', file=sys.stderr)
            return []

        returns_df = price_data.pct_change().dropna(how='all')
        latest     = price_data.iloc[-1]
        vol        = returns_df.iloc[-self.VOL_WINDOW:].std() * np.sqrt(252)

        scale        = self.position_scale(regime_state)
        signals = []
        max_per_side = self.MAX_SIGNALS // 2
        long_cands:  list = []
        short_cands: list = []

        for ticker in tickers:
            series = returns_df[ticker].dropna().values
            if len(series) < self.min_lookback:
                continue

            arr            = series[-self.BOCPD_WINDOW:]
            cp_probs, R    = _reference_bocpd(arr, hazard_rate=self.HAZARD_RATE)

            # Recent CP: max probability in last CP_LOOKBACK bars
            recent_cp = float(cp_probs[-self.CP_LOOKBACK:].max())
            if recent_cp < self.CP_THRESHOLD:
                continue

            # Direction: most likely current run length → mean of post-break returns
            most_likely_rl = int(R.argmax())
            if most_likely_rl == 0:
                most_likely_rl = 1
            post_break = arr[-most_likely_rl:]
            post_mean  = float(post_break.mean()) if len(post_break) > 0 else 0.0

            if post_mean > 0:
                long_cands.append((ticker, recent_cp, post_mean))
            elif post_mean < 0:
                short_cands.append((ticker, recent_cp, post_mean))

        # Sort by CP probability (strongest signal first)
        long_cands.sort(key=lambda x: x[1], reverse=True)
        short_cands.sort(key=lambda x: x[1], reverse=True)

        for direction, candidates, max_n in [
            ('LONG',  long_cands[:max_per_side],  max_per_side),
            ('SHORT', short_cands[:max_per_side], max_per_side),
        ]:
            for ticker, cp_prob, post_mean in candidates:
                price = float(latest.get(ticker, 0))
                if price <= 0:
                    continue
                ticker_vol = max(float(vol.get(ticker, 0.20)), 1e-4)
                size = float(self.BASE_SIZE_PCT * (0.15 / ticker_vol) * scale)
                size = max(0.001, min(size, 0.05))

                confidence = 'HIGH' if cp_prob > 0.60 else ('MED' if cp_prob > 0.40 else 'LOW')

                st = self.compute_stops_and_targets(
                    price_data[ticker].dropna(), direction, price,
                    regime_state=regime_state,
                )
                signals.append(Signal(
                    ticker            = ticker,
                    direction         = direction,
                    entry_price       = round(price, 4),
                    stop_loss         = st['stop'],
                    target_1          = st['t1'],
                    target_2          = st['t2'],
                    target_3          = st['t3'],
                    position_size_pct = size,
                    confidence        = confidence,
                    signal_params     = {
                        'cp_prob':    round(cp_prob, 4),
                        'post_mean':  round(post_mean, 6),
                        'hazard':     self.HAZARD_RATE,
                        'vol_annual': round(ticker_vol, 4),
                    },
                ))

        print(f'[debug] {self.id}: signals={len(signals)} '
              f'(long_cands={len(long_cands)}, short_cands={len(short_cands)})', file=sys.stderr)
        return signals


# ─── Synthetic panel fixture ────────────────────────────────────────────────
#
# ≈80 tickers × 300 bars covering every structural case the strategy sees on
# the real panel: planted change points (mean + vol breaks, some inside the
# CP_LOOKBACK window of the tested bars so long AND short candidates fire),
# leading-NaN columns (late listings), short-history columns (ineligible),
# flat/ffill'd columns (zero returns → scale²=1e-12 floor path), and
# died-to-zero columns (interior NaN returns → per-ticker extraction
# fallback; ffill keeps the 0 prices, pct_change yields -1 then 0/0 = NaN).

N_BARS = 300


def _build_synthetic_prices(n_bars: int = N_BARS, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range('2024-01-02', periods=n_bars)
    cols: dict[str, np.ndarray] = {}

    def prices_from(rets: np.ndarray, p0: float = 100.0) -> np.ndarray:
        return p0 * np.cumprod(1.0 + rets)

    # 60 normal tickers with planted change points. For i < 30, plant a strong
    # break in the last ~35 bars (alternating sign) so candidates on both
    # sides show up inside the 16 tested end-slices.
    for i in range(60):
        rets = rng.normal(0.0004, rng.uniform(0.008, 0.02), size=n_bars)
        n_breaks = int(rng.integers(1, 4))
        for b in np.sort(rng.choice(np.arange(30, n_bars - 40), size=n_breaks, replace=False)):
            rets[b:] = rng.normal(rng.normal(0, 0.003), rng.uniform(0.006, 0.03),
                                  size=n_bars - b)
        if i < 30:
            cp_at = n_bars - int(rng.integers(5, 36))
            sign = 1.0 if i % 2 == 0 else -1.0
            rets[cp_at:] = rng.normal(sign * 0.012, 0.035, size=n_bars - cp_at)
        cols[f'SYN{i:02d}'] = prices_from(rets)

    # 5 leading-NaN (late listing, still ≥126 returns at every tested slice)
    for i in range(5):
        p = prices_from(rng.normal(0.0003, 0.015, size=n_bars))
        if i >= 3:  # some of these also get a late break
            p[-20:] = p[-21] * np.cumprod(1 + rng.normal(-0.015, 0.04, size=20))
        p = p.copy()
        p[:140] = np.nan
        cols[f'LNAN{i}'] = p

    # 5 short-history (<126 non-NaN returns → never eligible)
    for i in range(5):
        p = prices_from(rng.normal(0.0, 0.02, size=n_bars))
        p[: n_bars - 100] = np.nan
        cols[f'SHORT{i}'] = p

    # 3 fully flat + 1 flat-tail (zero returns → var_r=0 → scale² floor)
    for i in range(3):
        cols[f'FLAT{i}'] = np.full(n_bars, 50.0 + i)
    p = prices_from(rng.normal(0.0005, 0.012, size=n_bars))
    p[-60:] = p[-61]
    cols['FLATTAIL'] = p

    # 2 died-to-zero (interior NaN returns after ffill → extraction fallback)
    for i in range(2):
        p = prices_from(rng.normal(0.0, 0.02, size=n_bars))
        p[200:] = 0.0
        cols[f'DEAD{i}'] = p

    # 4 high-vol
    for i in range(4):
        cols[f'HV{i}'] = prices_from(rng.normal(0.0, 0.06, size=n_bars))

    return pd.DataFrame(cols, index=idx)


REGIME_T = {'state': 'TRANSITIONING'}


def _assert_signals_identical(ref_sigs, new_sigs, ctx: str):
    assert len(ref_sigs) == len(new_sigs), (
        f'{ctx}: signal count mismatch ref={len(ref_sigs)} new={len(new_sigs)} '
        f'ref_tickers={[s.ticker for s in ref_sigs]} new_tickers={[s.ticker for s in new_sigs]}'
    )
    for i, (r, n) in enumerate(zip(ref_sigs, new_sigs)):
        rd, nd = dataclasses.asdict(r), dataclasses.asdict(n)
        assert rd == nd, f'{ctx}: signal #{i} differs:\n ref={rd}\n new={nd}'


# ─── 1. Pure-numpy Student-t logpdf vs scipy ────────────────────────────────

def test_t_logpdf_matches_scipy():
    dofs   = np.array([2.0, 3.0, 4.0, 5.0, 10.0, 27.0, 63.0, 126.0, 127.0, 2.5, 7.7])
    xs     = np.concatenate([np.linspace(-8.0, 8.0, 41), [-1e3, -3.7, 1e-9, 3.7, 1e3]])
    locs   = np.array([0.0, -0.05, 0.02, 3.0])
    scales = np.array([1e-6, 1e-3, 0.02, 1.0, 10.0, 1e3])
    X, D, L, S = np.meshgrid(xs, dofs, locs, scales, indexing='ij')
    got  = _t_logpdf(X, D, L, S)
    want = stats.t.logpdf(X, df=D, loc=L, scale=S)
    np.testing.assert_allclose(got, want, rtol=1e-12, atol=1e-13)


def test_t_logpdf_recursion_dof_grid():
    # The exact (x, dof, scale) shapes the recursion produces: dof = 2..127,
    # scale down to the 1e-6 floor (sqrt of the 1e-12 scale² clamp).
    rng = np.random.default_rng(3)
    dof = np.arange(2, 128, dtype=np.float64)
    x   = rng.normal(0, 0.02, size=dof.shape)
    loc = rng.normal(0, 0.001, size=dof.shape)
    for s in (1e-6, 0.014, 2.0):
        scale = np.full(dof.shape, s)
        np.testing.assert_allclose(
            _t_logpdf(x, dof, loc, scale),
            stats.t.logpdf(x, df=dof, loc=loc, scale=scale),
            rtol=1e-12, atol=1e-13,
        )


# ─── 2. Scalar _bocpd wrapper vs frozen reference ───────────────────────────

def test_bocpd_scalar_parity_random():
    rng = np.random.default_rng(42)
    for L in (10, 11, 50, 126, 200):
        for k in range(4):
            x = rng.normal(0.0005, 0.015, size=L)
            if k == 1:
                x[L // 2:] += 0.03           # planted mean break
            if k == 2:
                x[L // 2:] *= 4.0            # planted vol break
            if k == 3:
                x[::7] = 0.25                # fat outliers
            hz = 0.02 if k == 3 else 0.005
            cp_ref, R_ref = _reference_bocpd(x, hazard_rate=hz)
            cp_new, R_new = _bocpd(x, hazard_rate=hz)
            np.testing.assert_allclose(cp_new, cp_ref, rtol=1e-10, atol=1e-300,
                                       err_msg=f'cp_probs L={L} case={k}')
            np.testing.assert_allclose(R_new, R_ref, rtol=1e-10, atol=1e-300,
                                       err_msg=f'run_dist L={L} case={k}')


def test_bocpd_short_series_zero_path():
    x = np.random.default_rng(0).normal(size=9)
    cp, R = _bocpd(x)
    cp_ref, R_ref = _reference_bocpd(x)
    assert cp.shape == (9,) and R.shape == (10,)
    np.testing.assert_array_equal(cp, cp_ref)
    np.testing.assert_array_equal(R, R_ref)


def test_bocpd_zero_returns_scale_floor_path():
    x = np.zeros(126)
    cp_ref, R_ref = _reference_bocpd(x)
    cp_new, R_new = _bocpd(x)
    np.testing.assert_allclose(cp_new, cp_ref, rtol=1e-10, atol=1e-300)
    np.testing.assert_allclose(R_new, R_ref, rtol=1e-10, atol=1e-300)


# ─── 3. Panel recursion vs per-column reference on strategy-shaped data ─────

def test_bocpd_panel_parity_synthetic_columns():
    prices = _build_synthetic_prices()
    strat  = BOCPDChangePoint()
    price_data = prices.ffill()
    returns_df = price_data.pct_change().dropna(how='all')

    arrs, names = [], []
    for t in returns_df.columns:
        s = returns_df[t].dropna().values
        if len(s) >= strat.min_lookback:
            arrs.append(s[-strat.BOCPD_WINDOW:])
            names.append(t)
    assert len(names) > 60, 'fixture broke: expected >60 eligible tickers'

    panel = np.column_stack(arrs)
    cp_p, R_p = _bocpd_panel(panel, hazard_rate=strat.HAZARD_RATE)
    assert cp_p.shape == (strat.BOCPD_WINDOW, len(names))
    assert R_p.shape == (strat.BOCPD_WINDOW + 1, len(names))

    for j, name in enumerate(names):
        cp_ref, R_ref = _reference_bocpd(arrs[j], hazard_rate=strat.HAZARD_RATE)
        np.testing.assert_allclose(cp_p[:, j], cp_ref, rtol=1e-10, atol=1e-300,
                                   err_msg=f'cp_probs ticker={name} (col {j})')
        np.testing.assert_allclose(R_p[:, j], R_ref, rtol=1e-10, atol=1e-300,
                                   err_msg=f'run_dist ticker={name} (col {j})')


def test_fixture_exercises_interior_nan_fallback():
    # DEAD* columns must be eligible AND carry NaN inside their last
    # BOCPD_WINDOW returns rows — that is the per-ticker extraction fallback
    # branch. If this ever stops holding, the parity suite silently stops
    # covering the fallback, so pin it.
    prices = _build_synthetic_prices()
    strat  = BOCPDChangePoint()
    returns_df = prices.ffill().pct_change().dropna(how='all')
    for name in ('DEAD0', 'DEAD1'):
        col = returns_df[name]
        assert col.notna().sum() >= strat.min_lookback, f'{name} not eligible'
        assert col.iloc[-strat.BOCPD_WINDOW:].isna().any(), f'{name} tail has no NaN'


# ─── 4. generate_signals parity across ≥15 TRANSITIONING bars ───────────────

def test_generate_signals_parity_15_bars():
    prices   = _build_synthetic_prices()
    universe = list(prices.columns) + ['NOT_IN_PANEL', 'ALSO_MISSING']
    ref, new = _ReferenceBOCPD(), BOCPDChangePoint()

    n = len(prices)
    total = 0
    long_seen = short_seen = False
    for end in range(n - 15, n + 1):          # 16 TRANSITIONING bars
        sl = prices.iloc[:end]
        rs = ref.generate_signals(sl, dict(REGIME_T), universe)
        ns = new.generate_signals(sl, dict(REGIME_T), universe)
        _assert_signals_identical(rs, ns, ctx=f'bar_end={end}')
        total += len(ns)
        long_seen  |= any(s.direction == 'LONG' for s in ns)
        short_seen |= any(s.direction == 'SHORT' for s in ns)

    assert total > 0, 'fixture broke: no signals on any tested bar'
    assert long_seen and short_seen, 'fixture broke: need both LONG and SHORT coverage'


def test_generate_signals_guard_paths_match():
    prices = _build_synthetic_prices()
    ref, new = _ReferenceBOCPD(), BOCPDChangePoint()
    uni = list(prices.columns)

    # wrong regime → both empty
    assert new.generate_signals(prices, {'state': 'LOW_VOL'}, uni) == []
    assert ref.generate_signals(prices, {'state': 'LOW_VOL'}, uni) == []
    # too little history → both empty
    assert new.generate_signals(prices.iloc[:100], dict(REGIME_T), uni) == []
    assert ref.generate_signals(prices.iloc[:100], dict(REGIME_T), uni) == []
    # universe misses the panel entirely / empty frame / None
    assert new.generate_signals(prices, dict(REGIME_T), ['ZZZ']) == []
    assert new.generate_signals(pd.DataFrame(), dict(REGIME_T), uni) == []
    assert new.generate_signals(None, dict(REGIME_T), uni) == []


# ─── 5. Real-data spot parity (gated: needs cached close_wide parquet) ──────

@pytest.mark.skipif(
    not os.environ.get('OPENCLAW_TR03_PANEL'),
    reason='set OPENCLAW_TR03_PANEL=/path/to/close_wide.parquet (cached from '
           'backtest.unified_backtest.load_prices_panels) to run real-data spot parity',
)
def test_real_data_spot_parity():
    close_wide = pd.read_parquet(os.environ['OPENCLAW_TR03_PANEL'])
    # Deterministic ~400-column subset (every 16th column → mixes dense and
    # sparse listings), last 300 rows. NEVER run the scipy reference at full
    # panel width — that is the pathology this work removes.
    cols = list(close_wide.columns)[::16][:400]
    sub  = close_wide[cols].iloc[-300:]
    del close_wide

    strat = BOCPDChangePoint()
    n_elig = sum(
        int(sub[c].ffill().pct_change().notna().sum()) >= strat.min_lookback
        for c in cols
    )
    assert n_elig > 50, f'real-data subset too sparse to be meaningful ({n_elig} eligible)'

    ref, new = _ReferenceBOCPD(), BOCPDChangePoint()
    n = len(sub)
    for end in range(n - 4, n + 1):           # 5 bars
        sl = sub.iloc[:end]
        rs = ref.generate_signals(sl, dict(REGIME_T), cols)
        ns = new.generate_signals(sl, dict(REGIME_T), cols)
        _assert_signals_identical(rs, ns, ctx=f'real bar_end={end} (n_elig={n_elig})')

    # cp/R spot parity on the last bar for a sample of eligible tickers
    returns_df = sub.ffill().pct_change().dropna(how='all')
    checked = 0
    for c in cols:
        s = returns_df[c].dropna().values
        if len(s) < strat.min_lookback:
            continue
        arr = s[-strat.BOCPD_WINDOW:]
        cp_ref, R_ref = _reference_bocpd(arr, hazard_rate=strat.HAZARD_RATE)
        cp_new, R_new = _bocpd(arr, hazard_rate=strat.HAZARD_RATE)
        np.testing.assert_allclose(cp_new, cp_ref, rtol=1e-10, atol=1e-300,
                                   err_msg=f'cp_probs real ticker={c}')
        np.testing.assert_allclose(R_new, R_ref, rtol=1e-10, atol=1e-300,
                                   err_msg=f'run_dist real ticker={c}')
        checked += 1
        if checked >= 25:
            break
    assert checked >= 25
