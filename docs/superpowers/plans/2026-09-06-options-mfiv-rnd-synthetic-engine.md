# Options surface v3 (MFIV + RN density) and synthetic engine upgrades — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add model-free implied variance and risk-neutral-density features (7 new keys, version 3) to the ONE options-surface implementation that feeds live and backtest, and upgrade the dormant synthetic options engine with a dividend yield, American exercise and a real-surface-first IV anchor.

**Architecture:** Part A extends `src/strategies/options_surface.py` (per-expiry strip integrals on the existing PCHIP smile → `SmileFit` fields → constant-maturity row keys) and lets the existing builder/panel/live plumbing carry the new `SCALAR_KEYS` unchanged; a v2 freeze test written FIRST pins every v2 value. Part B adds `src/backtest/dividends.py`, extends `src/backtest/options_pricing.py` (q, CRR American tree, dispatchers), `src/backtest/vol_index.py` (VIX9D/VIX term point), `src/backtest/synthetic_iv.py` (surface → vix_term → realized tiers) and wires `src/backtest/options_backtest.py` + `OptionSpec.exercise`. Part C is the rollout script + runbook.

**Tech Stack:** Python 3.13, numpy 2.4 (`np.trapezoid`, not `np.trapz`), pandas 3.0, scipy 1.15 (`PchipInterpolator`, `scipy.stats.norm`), pyarrow filtered reads, py_vollib 1.0.1 (`black_scholes`, `black_scholes_merton`), pytest.

**Spec:** `docs/specs/2026-09-06-options-mfiv-rnd-synthetic-engine-spec.md` (read it first; §G lists the rulings each task implements).

## Global Constraints

- 2-core / 8 GB no-swap VPS; a fleet re-backtest epoch is running (`openclaw-fleet-overnight-resume` nightly + `fleet-rf-epoch-20260906`). **Never run the full pytest suite; never run `tests/strategies/test_regime_stratified_backtest.py`.** Run only the test files named in each task.
- Tests never read production masters: every module that reads a parquet honours an env override (`OPENCLAW_OPTIONS_SURFACE_PATH`, `OPENCLAW_CORPORATE_ACTIONS_PARQUET`, `OPENCLAW_VOL_INDICES_PARQUET`) and tests point it at `tmp_path`.
- ONE implementation for live and backtest (`strategies.options_surface`); the live dict, the builder and the panel must agree on every shared key (parity test extended, freeze test added).
- Nothing is ever a fabricated 0: a quantity that cannot be computed is `None`.
- Masters are append-only (`append_dedup`); no master is written by any test.
- Formulas only from financepy (GPL-3); no financepy source, no new dependencies.
- Flags (`OPENCLAW_OPTIONS_SURFACE`, `OPENCLAW_RF_SOURCE`) are NOT touched by this plan.
- Work on a git worktree branch (`superpowers:using-git-worktrees`): symlink `data/master`, `data/derived`, `logs`, `output` and `.env` into the worktree as the 2026-09-04 plan did; the worktree shell guard rejects compound/git-mixed commands — issue single plain commands.
- Commit after every task with the message given; never `git add -A`.
- Do not modify `src/execution/pipeline_orchestrator.py`, `src/execution/resolve_script.js`, `src/strategies/manifest.json`, `src/strategies/registry.py` (another session's dirty files).

## File structure

| file | responsibility |
|---|---|
| `tests/fixtures/options_surface_v2_expected.json` (new) | frozen v2 `SCALAR_KEYS` values for SPY/AAPL/XOM on the checked-in chain fixture |
| `tests/strategies/test_options_surface_v2_freeze.py` (new) | pins them against the current module |
| `src/strategies/options_surface.py` | + `strip_features` (MFIV, BKM moments, tail digitals), `SmileFit` fields, v3 row keys, version 3 |
| `tests/strategies/test_options_surface_mfiv.py` (new) | oracles + fixture assertions for Part A |
| `src/strategies/aux_data_loader.py`, `src/execution/options_aux_v2.py`, `tests/strategies/test_options_surface_parity.py`, 3 existing tests | carry / pin the v3 keys and version |
| `src/backtest/dividends.py` (new) + `tests/backtest/test_dividends.py` (new) | trailing-year cash dividend yield with pre-coverage backfill |
| `src/backtest/options_pricing.py` + `tests/backtest/test_american_pricing.py` (new) | `q`, CRR American tree, `price`/`delta` dispatchers |
| `src/backtest/vol_index.py`, `src/backtest/synthetic_iv.py` + `tests/backtest/test_synthetic_iv_hierarchy.py` (new) | VIX9D/VIX term point; surface → vix_term → realized tiers |
| `src/strategies/base.py`, `src/backtest/options_backtest.py` + `tests/backtest/test_options_backtest.py` | `OptionSpec.exercise`, engine wiring, iv-sources log line |
| `scripts/rollout_surface_v3.sh`, `docs/runbooks/2026-09-06-options-surface-v3-rollout.md`, `docs/archive/changelog.md` | rollout + verification + record |

---

### Task 1: Freeze the v2 surface values on the checked-in fixture

**Files:**
- Create: `tests/fixtures/options_surface_v2_expected.json`
- Create: `tests/strategies/test_options_surface_v2_freeze.py`

**Interfaces:**
- Consumes: `scripts/build_options_surface.py::build_rows(chain, spots)` (existing), `strategies.options_surface.SCALAR_KEYS` (existing v2 list), fixtures `tests/fixtures/options_chain_2026-09-03.parquet` + `_spots.json`.
- Produces: the JSON snapshot every later task must keep reproducing (`options_features_version` deliberately excluded — it changes 2 → 3).

- [ ] **Step 1: Generate the snapshot with the CURRENT (v2) module**

Run from the worktree root (this must happen BEFORE any Part A code lands):

```bash
python3 - <<'PY'
import json, sys, importlib.util
sys.path[:0] = ['.', 'src']
import pandas as pd
spec = importlib.util.spec_from_file_location('bos', 'scripts/build_options_surface.py')
bos = importlib.util.module_from_spec(spec); spec.loader.exec_module(bos)
from strategies.options_surface import SCALAR_KEYS, OPTIONS_FEATURES_VERSION
assert OPTIONS_FEATURES_VERSION == 2, 'snapshot must be taken on the v2 module'
chain = pd.read_parquet('tests/fixtures/options_chain_2026-09-03.parquet')
meta = json.load(open('tests/fixtures/options_chain_2026-09-03_spots.json'))
day = pd.Timestamp('2026-09-03')
built = bos.build_rows(chain.assign(date=pd.to_datetime(chain['date'])),
                       {(t, day): s for t, s in meta['spots'].items()})
def py(v):
    try:
        if v is None or pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v.item() if hasattr(v, 'item') else v
rows = {t: {k: py(built[built.ticker == t].iloc[0][k]) for k in SCALAR_KEYS if k != 'options_features_version'}
        for t in ('SPY', 'AAPL', 'XOM')}
json.dump({'source': 'strategies.options_surface v2 on tests/fixtures/options_chain_2026-09-03.parquet '
                     '(frozen 2026-09-06 before the v3 work; options_features_version excluded on purpose)',
           'rows': rows}, open('tests/fixtures/options_surface_v2_expected.json', 'w'), indent=1, sort_keys=True)
print({t: sum(v is not None for v in r.values()) for t, r in rows.items()})
PY
```
Expected: three counts printed, each ≥ 15 non-null keys (SPY/AAPL/XOM all have fits on this fixture).

- [ ] **Step 2: Write the freeze test**

```python
# tests/strategies/test_options_surface_v2_freeze.py
"""Every v2 SCALAR_KEYS value on the checked-in chain fixture is reproduced
bit-for-bit by the current module. Spec 2026-09-06 §A.3: v3 adds keys, it
never moves a v2 value. Regenerate the JSON only from a v2 checkout, never
from the module under test."""
from __future__ import annotations
import importlib.util, json
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / 'tests' / 'fixtures'


def _build_rows():
    spec = importlib.util.spec_from_file_location('bos', ROOT / 'scripts' / 'build_options_surface.py')
    bos = importlib.util.module_from_spec(spec); spec.loader.exec_module(bos)
    chain = pd.read_parquet(FIX / 'options_chain_2026-09-03.parquet')
    meta = json.load(open(FIX / 'options_chain_2026-09-03_spots.json'))
    day = pd.Timestamp('2026-09-03')
    return bos.build_rows(chain.assign(date=pd.to_datetime(chain['date'])),
                          {(t, day): s for t, s in meta['spots'].items()})


def _is_null(v):
    try:
        return v is None or bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def test_current_module_reproduces_frozen_v2_values():
    expected = json.load(open(FIX / 'options_surface_v2_expected.json'))['rows']
    built = _build_rows()
    for ticker, keys in expected.items():
        row = built[built.ticker == ticker].iloc[0]
        for k, want in keys.items():
            got = row[k]
            if want is None:
                assert _is_null(got), (ticker, k, got)
            elif isinstance(want, str):
                assert got == want, (ticker, k, got, want)
            else:
                assert abs(float(got) - float(want)) <= 1e-12 * max(1.0, abs(float(want))), (ticker, k, got, want)


def test_snapshot_covers_every_v2_scalar_key():
    from strategies.options_surface import SCALAR_KEYS
    expected = json.load(open(FIX / 'options_surface_v2_expected.json'))['rows']
    v2_keys = {'spot', 'iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d',
               'ts_ratio', 'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm',
               'call_volume', 'put_volume', 'volume', 'pc_ratio', 'expiry_date',
               'n_expiries_fit', 'n_strikes_30d'}
    assert v2_keys <= set(SCALAR_KEYS)
    for ticker in ('SPY', 'AAPL', 'XOM'):
        assert set(expected[ticker]) == v2_keys, ticker
```

- [ ] **Step 3: Run it**

Run: `python3 -m pytest tests/strategies/test_options_surface_v2_freeze.py -q`
Expected: 2 passed.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/options_surface_v2_expected.json tests/strategies/test_options_surface_v2_freeze.py
git commit -m "test(options): freeze every v2 surface value on the checked-in chain fixture before the v3 work"
```

---

### Task 2: Strip features — model-free implied variance, BKM moments, tail digitals

**Files:**
- Modify: `src/strategies/options_surface.py` (constants after `_D1_25_PUT`; `SmileFit` fields; new helpers before `fit_smile`; `fit_smile` return)
- Create: `tests/strategies/test_options_surface_mfiv.py`

**Interfaces:**
- Consumes: `fit_smile(strikes, ivs, spot, dte) -> SmileFit | None`, `IV_MIN`, `norm` (existing).
- Produces: `strip_features(smile, k_min, k_max, atm, t) -> dict` with keys `mfiv, rn_skew, rn_kurt, rn_p_dn10, rn_p_up10` (floats or `None`); `SmileFit` gains the same five fields (default `None`); constants `K_TRUNC = 5.0`, `N_GRID = 401`, `RN_TAIL_MOVE = 0.10`, `RN_MOMENT_DTE_TOL = 15`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategies/test_options_surface_mfiv.py
"""Spec 2026-09-06 §A.1–A.2 oracles: a flat smile is lognormal (MFIV = σ, RN
skew 0, RN kurtosis 3, tails = Black digitals); a left-skewed SVI smile prices
its wings above ATM and its down-tail above its up-tail."""
from __future__ import annotations
import math
import numpy as np
import pytest
from scipy.stats import norm

from strategies import options_surface as osf


def _flat_fit(sigma=0.25, dte=30, spot=100.0):
    K = spot * np.exp(np.linspace(-0.4, 0.4, 17))
    return osf.fit_smile(K, np.full(len(K), sigma), spot, dte)


def _svi_fit(dte=30, spot=100.0, a=0.002, b=0.03, rho=-0.6, m=0.0, s=0.1):
    t = dte / 365
    k = np.linspace(-0.3, 0.3, 25)
    w = a + b * (rho * (k - m) + np.sqrt((k - m) ** 2 + s * s))
    return osf.fit_smile(spot * np.exp(k), np.sqrt(w / t), spot, dte)


def _black_p_below(k, sigma, t):
    d1 = (-k + 0.5 * sigma * sigma * t) / (sigma * math.sqrt(t))
    return norm.cdf(-(d1 - sigma * math.sqrt(t)))


def test_flat_smile_mfiv_equals_sigma():
    f = _flat_fit()
    assert f.mfiv == pytest.approx(0.25, rel=1e-4)


def test_flat_smile_rn_moments_are_lognormal():
    f = _flat_fit()
    assert abs(f.rn_skew) < 1e-2
    assert f.rn_kurt == pytest.approx(3.0, abs=5e-2)


def test_flat_smile_tails_equal_black_digitals():
    f = _flat_fit()
    t = 30 / 365
    assert f.rn_p_dn10 == pytest.approx(_black_p_below(math.log(0.9), 0.25, t), abs=1e-9)
    assert f.rn_p_up10 == pytest.approx(1.0 - _black_p_below(math.log(1.1), 0.25, t), abs=1e-9)
    assert 0.0 < f.rn_p_dn10 < 0.5 and 0.0 < f.rn_p_up10 < 0.5


def test_left_skewed_smile_prices_wings_and_down_tail():
    f = _svi_fit()
    assert f.mfiv > f.atm_iv
    assert f.rn_skew < 0.0
    assert f.rn_kurt > 3.0
    assert 0.0 <= f.rn_p_up10 < f.rn_p_dn10 <= 1.0


def test_wings_are_flat_beyond_observed_strikes():
    f = _svi_fit()
    K = 100.0 * np.exp(np.linspace(-0.3, 0.3, 25)); t = 30 / 365
    w = 0.002 + 0.03 * (-0.6 * np.log(K / 100.0) + np.sqrt(np.log(K / 100.0) ** 2 + 0.01))
    from scipy.interpolate import PchipInterpolator
    smile = PchipInterpolator(np.log(K / 100.0), np.sqrt(w / t), extrapolate=False)
    far = osf._sigma_on(smile, np.array([-0.9, 0.9]), f.k_min, f.k_max, f.atm_iv)
    assert far[0] == pytest.approx(float(smile(f.k_min)))
    assert far[1] == pytest.approx(float(smile(f.k_max)))


def test_strip_features_never_raises():
    def boom(k):
        raise RuntimeError('degenerate smile')
    out = osf.strip_features(boom, -0.1, 0.1, 0.2, 30 / 365)
    assert out == {'mfiv': None, 'rn_skew': None, 'rn_kurt': None, 'rn_p_dn10': None, 'rn_p_up10': None}
    assert osf.strip_features(lambda k: np.asarray(k) * 0 + 0.2, -0.1, 0.1, 0.0, 30 / 365)['mfiv'] is None


def test_fit_smile_none_paths_unchanged():
    assert osf.fit_smile([100.0, 101.0], [0.2, 0.2], 100.0, 30) is None          # < MIN_STRIKES
    assert osf.fit_smile(np.arange(101, 110), np.full(9, 0.2), 100.0, 30) is None  # no strike below spot
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/strategies/test_options_surface_mfiv.py -q`
Expected: failures — `SmileFit` has no attribute `mfiv`, `osf.strip_features` / `osf._sigma_on` missing.

- [ ] **Step 3: Implement**

In `src/strategies/options_surface.py`, after the `_D1_25_PUT = ...` line add:

```python
# Spec 2026-09-06 §A.1–A.2 — model-free strip on the fitted smile.
K_TRUNC = 5.0            # strip half-width in units of σ_atm·√T (ruling G2)
N_GRID = 401             # odd ⇒ k = 0 (the call/put switch) is a node
RN_TAIL_MOVE = 0.10      # ±10 % tail probabilities
RN_MOMENT_DTE_TOL = 15   # RN moments/tails from the expiry nearest 30 DTE within this (G4)
```

Extend the dataclass (append after `k_max: float`):

```python
    mfiv: float | None = None        # model-free implied vol √(V/T) — spec 2026-09-06 A.2
    rn_skew: float | None = None     # BKM risk-neutral skewness of ln(S_T/F)
    rn_kurt: float | None = None     # BKM risk-neutral kurtosis (raw; 3 = lognormal)
    rn_p_dn10: float | None = None   # RN P(S_T ≤ 0.9·F)
    rn_p_up10: float | None = None   # RN P(S_T ≥ 1.1·F)
```

Insert the helpers immediately BEFORE `def fit_smile(`:

```python
def _sigma_on(smile, k, k_min: float, k_max: float, atm: float) -> np.ndarray:
    """Smile vol at log-moneyness k with FLAT extrapolation outside the observed
    strike range (ruling G1); non-finite or sub-floor values fall back to ATM."""
    s = np.asarray(smile(np.clip(np.asarray(k, dtype=float), k_min, k_max)), dtype=float)
    return np.where(np.isfinite(s) & (s > IV_MIN), s, atm)


def _strip_prices(sig: np.ndarray, k: np.ndarray, t: float) -> np.ndarray:
    """Normalised, undiscounted OTM Black prices q(k) = Q/F with F = S, r = q = 0:
    call for k ≥ 0, put for k < 0 (spec A.1)."""
    st = sig * math.sqrt(t)
    d1 = (-k + 0.5 * sig * sig * t) / st
    d2 = d1 - st
    call = norm.cdf(d1) - np.exp(k) * norm.cdf(d2)
    put = np.exp(k) * norm.cdf(-d2) - norm.cdf(-d1)
    return np.maximum(np.where(k >= 0.0, call, put), 0.0)


def _tail_prob_below(smile, k: float, k_min: float, k_max: float, atm: float, t: float) -> float | None:
    """RN P(S_T ≤ F·e^k) from the smile-adjusted digital (spec A.2):
    Φ(−d2) + e^{−k} φ(d1) √T σ′(k), σ′ = 0 in the flat wings, clipped to [0, 1] (G3)."""
    sig = float(_sigma_on(smile, np.array([k]), k_min, k_max, atm)[0])
    st = sig * math.sqrt(t)
    d1 = (-k + 0.5 * sig * sig * t) / st
    d2 = d1 - st
    dsig = 0.0
    if k_min <= k <= k_max:
        try:
            dsig = float(smile.derivative()(k))
        except Exception:  # noqa: BLE001 — a callable without .derivative() ⇒ flat
            dsig = 0.0
        if not math.isfinite(dsig):
            dsig = 0.0
    p = float(norm.cdf(-d2)) + math.exp(-k) * float(norm.pdf(d1)) * math.sqrt(t) * dsig
    return float(min(max(p, 0.0), 1.0)) if math.isfinite(p) else None


_STRIP_NONE = {'mfiv': None, 'rn_skew': None, 'rn_kurt': None, 'rn_p_dn10': None, 'rn_p_up10': None}


def strip_features(smile, k_min: float, k_max: float, atm: float, t: float) -> dict:
    """Model-free implied variance (DDKZ/VIX integral), BKM (2003) risk-neutral
    skewness/kurtosis and ±10 % tail probabilities for ONE expiry, from its
    fitted smile (spec 2026-09-06 §A.1–A.2). In log-moneyness dK/K² = e^{−k}/F·dk,
    so every integral is ∫ weight(k)·q(k)·e^{−k} dk over a ±K_TRUNC·σ√T grid.
    Returns None values for anything not finite; never raises."""
    out = dict(_STRIP_NONE)
    try:
        if not (atm > 0.0 and t > 0.0):
            return out
        L = K_TRUNC * atm * math.sqrt(t)
        k = np.linspace(-L, L, N_GRID)
        q = _strip_prices(_sigma_on(smile, k, k_min, k_max, atm), k, t)
        w = q * np.exp(-k)
        var_total = 2.0 * float(np.trapezoid(w, k))            # variance-swap total variance
        if var_total > 0.0:
            out['mfiv'] = math.sqrt(var_total / t)
        v2 = 2.0 * float(np.trapezoid((1.0 - k) * w, k))        # BKM V  (E[x²])
        w3 = float(np.trapezoid((6.0 * k - 3.0 * k * k) * w, k))  # BKM W  (E[x³])
        x4 = float(np.trapezoid((12.0 * k * k - 4.0 * k ** 3) * w, k))  # BKM X (E[x⁴])
        mu = -v2 / 2.0 - w3 / 6.0 - x4 / 24.0                     # E[x], r = 0
        var = v2 - mu * mu
        if var > 0.0:
            out['rn_skew'] = (w3 - 3.0 * mu * v2 + 2.0 * mu ** 3) / var ** 1.5
            out['rn_kurt'] = (x4 - 4.0 * mu * w3 + 6.0 * mu * mu * v2 - 3.0 * mu ** 4) / var ** 2
        out['rn_p_dn10'] = _tail_prob_below(smile, math.log(1.0 - RN_TAIL_MOVE), k_min, k_max, atm, t)
        below_up = _tail_prob_below(smile, math.log(1.0 + RN_TAIL_MOVE), k_min, k_max, atm, t)
        out['rn_p_up10'] = None if below_up is None else float(1.0 - below_up)
        for key, val in list(out.items()):
            if val is not None and not math.isfinite(val):
                out[key] = None
        return out
    except Exception:  # noqa: BLE001 — a degenerate smile must never break the row
        return dict(_STRIP_NONE)
```

In `fit_smile`, replace the final `return SmileFit(...)` with:

```python
    strip = strip_features(smile, float(k[0]), float(k[-1]), atm, t)
    return SmileFit(dte=int(dte), t=t, atm_iv=atm, iv_25d_put=ivp, iv_25d_call=ivc,
                    n_strikes=int(len(K)), k_min=float(k[0]), k_max=float(k[-1]), **strip)
```

- [ ] **Step 4: Run the new tests, the freeze test and the existing surface tests**

Run: `python3 -m pytest tests/strategies/test_options_surface_mfiv.py tests/strategies/test_options_surface_v2_freeze.py tests/strategies/test_options_surface.py -q`
Expected: all pass. If `test_flat_smile_rn_moments_are_lognormal` misses by more than the tolerance, print `f.rn_skew, f.rn_kurt` and report the numbers in the task report — do NOT loosen the tolerance without reporting.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/options_surface.py tests/strategies/test_options_surface_mfiv.py
git commit -m "feat(options): model-free implied variance, BKM risk-neutral moments and tail digitals per fitted expiry (spec 2026-09-06 A.1-A.2)"
```

---

### Task 3: Row keys v3 — constant maturity, consumers, parity, shadow line

**Files:**
- Modify: `src/strategies/options_surface.py` (`OPTIONS_FEATURES_VERSION`, `SCALAR_KEYS`, end of `features_for_day`)
- Modify: `src/strategies/aux_data_loader.py` (`FIELDS`)
- Modify: `src/execution/options_aux_v2.py` (`shadow_summary`)
- Modify: `tests/strategies/test_options_surface_parity.py` (`SHARED`)
- Modify: `tests/strategies/test_options_surface.py:94`, `tests/scripts/test_build_options_surface.py:25`, `tests/execution/test_engine_options_surface_shadow.py:34,84` (version pins)
- Modify: `tests/strategies/test_options_surface_mfiv.py` (fixture-level assertions)

**Interfaces:**
- Consumes: `SmileFit.mfiv/rn_*` (Task 2), `constant_maturity(fits, target, attr)` (existing).
- Produces: row keys `mfiv_30d, mfiv_90d, mf_tail_premium_30d, rn_skew_30d, rn_kurt_30d, rn_p_dn10_30d, rn_p_up10_30d`; `OPTIONS_FEATURES_VERSION == 3`; shadow line fields `mfiv_nonnull=…% rn_nonnull=…%`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/strategies/test_options_surface_mfiv.py`:

```python
V3_KEYS = ['mfiv_30d', 'mfiv_90d', 'mf_tail_premium_30d',
           'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d']


def _fixture_row(ticker='SPY'):
    import json
    from pathlib import Path
    import pandas as pd
    fix = Path(__file__).resolve().parents[1] / 'fixtures'
    chain = pd.read_parquet(fix / 'options_chain_2026-09-03.parquet')
    meta = json.load(open(fix / 'options_chain_2026-09-03_spots.json'))
    ch = chain[chain['ticker'] == ticker]
    ch = ch.assign(date=pd.to_datetime(ch['date']))
    return osf.features_for_day(ch, meta['spots'][ticker], pd.Timestamp('2026-09-03'))


def test_v3_keys_are_scalar_keys_and_aux_fields():
    from strategies.aux_data_loader import FIELDS
    for k in V3_KEYS:
        assert k in osf.SCALAR_KEYS and k in FIELDS, k
    assert osf.OPTIONS_FEATURES_VERSION == 3


def test_spy_fixture_carries_v3_values():
    row = _fixture_row('SPY')
    assert row['options_features_version'] == 3
    assert row['mfiv_30d'] is not None and row['iv30'] is not None
    assert 0.0 <= row['mf_tail_premium_30d'] <= 0.05          # index smile: wings a few vol points rich
    assert row['mfiv_30d'] == pytest.approx(row['iv30'] + row['mf_tail_premium_30d'])
    assert row['rn_skew_30d'] < 0.0                             # left-skewed index smile
    assert row['rn_kurt_30d'] > 3.0
    assert 0.0 < row['rn_p_dn10_30d'] < 0.2 and 0.0 <= row['rn_p_up10_30d'] < 0.2
    assert row['mfiv_90d'] is None or row['mfiv_90d'] > 0.0


def test_v3_keys_none_without_a_30d_expiry():
    import pandas as pd
    K = 100.0 * np.exp(np.linspace(-0.3, 0.3, 25)); t = 60 / 365
    rows = [{'ticker': 'ZZZT', 'date': '2026-09-03', 'expiry': (pd.Timestamp('2026-09-03') + pd.Timedelta(days=60)).date(),
             'strike': float(k), 'option_type': f, 'implied_volatility': 0.25, 'delta': 0.5 if f == 'CALL' else -0.5,
             'gamma': 0.01, 'theta': -0.02, 'vega': 0.1, 'volume': 1.0}
            for k in K for f in ('CALL', 'PUT')]
    row = osf.features_for_day(pd.DataFrame(rows), 100.0, '2026-09-03')
    assert row['n_expiries_fit'] == 1
    for k in ('rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d', 'mfiv_30d', 'mf_tail_premium_30d'):
        assert row[k] is None, k                                # |60 − 30| > 15 and > CM_ONE_SIDED_TOL


def test_empty_chain_row_has_v3_keys_as_none():
    import pandas as pd
    row = osf.features_for_day(pd.DataFrame(), 100.0, '2026-09-03')
    assert all(row[k] is None for k in V3_KEYS) and row['options_features_version'] == 3
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/strategies/test_options_surface_mfiv.py -q`
Expected: the four new tests fail (`KeyError: 'mfiv_30d'`, version 2).

- [ ] **Step 3: Implement**

`src/strategies/options_surface.py`:
- `OPTIONS_FEATURES_VERSION = 3`
- `SCALAR_KEYS` becomes:

```python
SCALAR_KEYS = [
    'spot', 'iv30', 'iv90', 'iv_25d_put_30d', 'iv_25d_call_30d', 'skew_25d_30d', 'rr_25d_30d',
    'ts_ratio', 'term_slope', 'iv_spread', 'gamma_atm', 'theta_atm',
    'call_volume', 'put_volume', 'volume', 'pc_ratio', 'expiry_date',
    'n_expiries_fit', 'n_strikes_30d', 'options_features_version',
    # v3 (spec 2026-09-06 A.3): model-free variance + risk-neutral density
    'mfiv_30d', 'mfiv_90d', 'mf_tail_premium_30d',
    'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d',
]
```
- In `features_for_day`, after `row['skew_20d'] = row['skew_25d_30d']` and before `return row`:

```python
    # v3 (spec 2026-09-06 A.3): MFIV interpolates in total variance like the ATM
    # points; RN moments/tails come from the expiry nearest 30 DTE (G4).
    mf30 = constant_maturity(fits, 30, 'mfiv')
    mf90 = constant_maturity(fits, 90, 'mfiv')
    row.update({'mfiv_30d': mf30, 'mfiv_90d': mf90,
                'mf_tail_premium_30d': (mf30 - iv30) if (mf30 is not None and iv30 is not None) else None})
    if abs(near30 - 30) <= RN_MOMENT_DTE_TOL:
        f30 = fits[near30]
        row.update({'rn_skew_30d': f30.rn_skew, 'rn_kurt_30d': f30.rn_kurt,
                    'rn_p_dn10_30d': f30.rn_p_dn10, 'rn_p_up10_30d': f30.rn_p_up10})
```
(`_empty_row` already seeds every `SCALAR_KEYS` entry with `None`, so the new keys are present on every row.)

`src/strategies/aux_data_loader.py`, `FIELDS`: after the line `'max_pain', 'pcr_oi', 'oi_session',` add
```python
    # options_surface v3 (spec 2026-09-06 A.3)
    'mfiv_30d', 'mfiv_90d', 'mf_tail_premium_30d',
    'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d',
```

`src/execution/options_aux_v2.py`, `shadow_summary`: after the `vrp_pct = ...` line add
```python
    mf_pct = _pct_of_new(new, lambda r: r.get('mfiv_30d') is not None)
    rn_pct = _pct_of_new(new, lambda r: r.get('rn_skew_30d') is not None)
```
and change the returned f-string so the segment `f'iv_rank_nonnull={pct}% rv20_nonnull={rv_pct}% vrp_nonnull={vrp_pct}% '` becomes
```python
            f'iv_rank_nonnull={pct}% rv20_nonnull={rv_pct}% vrp_nonnull={vrp_pct}% '
            f'mfiv_nonnull={mf_pct}% rn_nonnull={rn_pct}% '
```
Update the docstring's list of fields accordingly (one sentence: "`mfiv_nonnull`/`rn_nonnull` are the v3 coverage — spec 2026-09-06 §C.3 expects ≥ 90 % of tickers with ≥ 2 fitted expiries").

`tests/strategies/test_options_surface_parity.py`: extend `SHARED` with `'mfiv_30d', 'mfiv_90d', 'mf_tail_premium_30d', 'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d'`.

Version pins: `tests/strategies/test_options_surface.py:94` → `== 3`; `tests/scripts/test_build_options_surface.py:25` → `== 3`; `tests/execution/test_engine_options_surface_shadow.py:34` → `== 3`; line 84 → `'version=3' in line`, and add to that test after the `assert 'spot_stale=0%' in line and 'dur=n/a' in line` line:
```python
    assert 'mfiv_nonnull=0%' in line and 'rn_nonnull=0%' in line   # v3 coverage fields (spec 2026-09-06 §C.3)
```

- [ ] **Step 4: Run the scoped set**

Run: `python3 -m pytest tests/strategies/test_options_surface_mfiv.py tests/strategies/test_options_surface_v2_freeze.py tests/strategies/test_options_surface.py tests/strategies/test_options_surface_series.py tests/strategies/test_options_surface_parity.py tests/scripts/test_build_options_surface.py tests/scripts/test_build_options_surface_oi.py tests/scripts/test_compute_rolling_from_surface.py tests/execution/test_engine_options_surface_shadow.py tests/strategies/test_options_oi.py -q`
Expected: all pass (the freeze test proves no v2 value moved; parity proves live ≡ builder on the 7 new keys).

- [ ] **Step 5: Commit**

```bash
git add src/strategies/options_surface.py src/strategies/aux_data_loader.py src/execution/options_aux_v2.py tests/strategies/test_options_surface_mfiv.py tests/strategies/test_options_surface_parity.py tests/strategies/test_options_surface.py tests/scripts/test_build_options_surface.py tests/execution/test_engine_options_surface_shadow.py
git commit -m "feat(options): surface v3 row keys — mfiv_30d/90d, tail premium, RN skew/kurt/tails; FIELDS + parity + shadow coverage (spec 2026-09-06 A.3-A.4)"
```

---

### Task 4: Dividend yield module

**Files:**
- Create: `src/backtest/dividends.py`
- Create: `tests/backtest/test_dividends.py`

**Interfaces:**
- Consumes: `data/master/corporate_actions.parquet` columns `symbol, action_type, ex_date, cash_amount` (env override `OPENCLAW_CORPORATE_ACTIONS_PARQUET`).
- Produces: `dividend_yield_asof(ticker, as_of, spot, ref_spot=None) -> float`, `coverage_start() -> pd.Timestamp | None`, `backfill_reference_date() -> pd.Timestamp | None`, `clear_cache()`, constants `PATH_ENV`, `TRAILING`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_dividends.py
from __future__ import annotations
import logging
import pandas as pd
import pytest


SPY_EX_DATES = ['2024-03-15', '2024-06-15', '2024-09-15', '2024-12-15', '2025-03-15', '2025-06-15',
                '2025-09-15', '2025-12-15', '2026-03-15', '2026-06-15']          # quarterly, 10 payments


def _fixture(tmp_path, monkeypatch):
    rows = [{'symbol': 'SPY', 'action_type': 'cash_dividend', 'ex_date': pd.Timestamp(d).date(), 'cash_amount': 1.5}
            for d in SPY_EX_DATES]
    rows.append({'symbol': 'ONE', 'action_type': 'cash_dividend', 'ex_date': pd.Timestamp('2025-05-15').date(), 'cash_amount': 2.0})
    rows.append({'symbol': 'SPY', 'action_type': 'forward_split', 'ex_date': pd.Timestamp('2025-01-02').date(), 'cash_amount': None})
    rows.append({'symbol': 'NODIV', 'action_type': 'reverse_split', 'ex_date': pd.Timestamp('2025-01-02').date(), 'cash_amount': None})
    p = tmp_path / 'corporate_actions.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_CORPORATE_ACTIONS_PARQUET', str(p))
    from backtest import dividends
    dividends.clear_cache()
    return dividends


def test_trailing_year_sum_over_spot(tmp_path, monkeypatch):
    dv = _fixture(tmp_path, monkeypatch)
    # (2025-06-30, 2026-06-30] holds 2025-09-15, 2025-12-15, 2026-03-15, 2026-06-15 → 4 × 1.5
    assert dv.dividend_yield_asof('SPY', '2026-06-30', 500.0) == pytest.approx(6.0 / 500.0)


def test_window_is_open_below_and_closed_above(tmp_path, monkeypatch):
    dv = _fixture(tmp_path, monkeypatch)                            # ONE pays 2.0 on 2025-05-15 only
    assert dv.dividend_yield_asof('ONE', '2025-05-15', 100.0) == pytest.approx(0.02)   # ex_date == as_of counts
    assert dv.dividend_yield_asof('ONE', '2025-05-14', 100.0) == 0.0                   # not yet
    assert dv.dividend_yield_asof('ONE', '2026-05-14', 100.0) == pytest.approx(0.02)   # 364 days later: still inside
    assert dv.dividend_yield_asof('ONE', '2026-05-15', 100.0) == 0.0                   # ex_date == as_of − 365 d: out


def test_no_dividend_ticker_and_unknown_ticker_are_zero(tmp_path, monkeypatch):
    dv = _fixture(tmp_path, monkeypatch)
    assert dv.dividend_yield_asof('NODIV', '2026-06-30', 50.0) == 0.0
    assert dv.dividend_yield_asof('ZZZT', '2026-06-30', 50.0) == 0.0
    assert dv.dividend_yield_asof('SPY', '2026-06-30', 0.0) == 0.0


def test_pre_coverage_backfills_first_full_year_and_warns_once(tmp_path, monkeypatch, caplog):
    dv = _fixture(tmp_path, monkeypatch)
    assert dv.coverage_start() == pd.Timestamp('2024-03-15')
    assert dv.backfill_reference_date() == pd.Timestamp('2025-03-15')
    with caplog.at_level(logging.WARNING):
        q1 = dv.dividend_yield_asof('SPY', '2024-06-01', 400.0)               # trailing window starts 2023-06 < coverage
        q2 = dv.dividend_yield_asof('SPY', '2024-09-01', 400.0, ref_spot=600.0)
    # first full year [2024-03-15, 2025-03-15): 2024-03-15, 06-15, 09-15, 12-15 → 4 × 1.5 = 6.0
    assert q1 == pytest.approx(6.0 / 400.0)
    assert q2 == pytest.approx(6.0 / 600.0)                                    # ref_spot wins when given
    assert sum('q backfilled' in r.message for r in caplog.records) == 1


def test_unreadable_file_is_zero_not_an_error(tmp_path, monkeypatch):
    bad = tmp_path / 'corporate_actions.parquet'; bad.write_text('not a parquet')
    monkeypatch.setenv('OPENCLAW_CORPORATE_ACTIONS_PARQUET', str(bad))
    from backtest import dividends
    dividends.clear_cache()
    assert dividends.dividend_yield_asof('SPY', '2026-06-30', 500.0) == 0.0
    assert dividends.coverage_start() is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/backtest/test_dividends.py -q`
Expected: `ModuleNotFoundError: backtest.dividends`.

- [ ] **Step 3: Implement**

```python
# src/backtest/dividends.py
"""Dividend yield `q` for the synthetic options engine (spec 2026-09-06 B.1).

q(ticker, as_of, spot) = Σ cash dividends with ex_date in (as_of − 365 d, as_of] / spot,
read from data/master/corporate_actions.parquet (action_type == 'cash_dividend').
Coverage starts 2024-02-09 in production: for as_of earlier than
coverage_start + 365 d the trailing window is incomplete, so q is BACKFILLED
with the ticker's first full trailing year (ruling G6) — divided by `ref_spot`
(the close at the backfill reference date) when the caller has it, else by
`spot` — and the module warns once per ticker. Never raises: a missing or
unreadable file, an unknown ticker or a non-positive spot ⇒ 0.0.
"""
from __future__ import annotations

import functools
import logging
import os
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
PATH_ENV = 'OPENCLAW_CORPORATE_ACTIONS_PARQUET'
TRAILING = pd.Timedelta(days=365)
_BACKFILL_WARNED: set[str] = set()
_MISSING_WARNED: set[str] = set()


def corporate_actions_path() -> Path:
    return Path(os.environ.get(PATH_ENV) or (ROOT / 'data' / 'master' / 'corporate_actions.parquet'))


def clear_cache() -> None:
    _load.cache_clear()
    _BACKFILL_WARNED.clear()
    _MISSING_WARNED.clear()


@functools.lru_cache(maxsize=2)
def _load(path_str: str, mtime_ns: int) -> pd.DataFrame:
    import pyarrow.parquet as pq
    tbl = pq.read_table(path_str, columns=['symbol', 'action_type', 'ex_date', 'cash_amount'],
                        filters=[('action_type', '==', 'cash_dividend')])
    df = tbl.to_pandas()
    df['symbol'] = df['symbol'].astype(str)
    df['ex_date'] = pd.to_datetime(df['ex_date'], errors='coerce').dt.normalize()
    df['cash_amount'] = pd.to_numeric(df['cash_amount'], errors='coerce')
    df = df.dropna(subset=['ex_date', 'cash_amount'])
    df = df[df['cash_amount'] > 0]
    return df[['symbol', 'ex_date', 'cash_amount']].sort_values(['symbol', 'ex_date']).reset_index(drop=True)


def _dividends() -> pd.DataFrame | None:
    p = corporate_actions_path()
    try:
        if p.exists():
            df = _load(str(p), p.stat().st_mtime_ns)
            return df if len(df) else None
    except Exception as exc:  # noqa: BLE001
        if str(p) not in _MISSING_WARNED:
            _MISSING_WARNED.add(str(p))
            log.warning('dividends: %s unreadable (%s) — q=0 everywhere', p, exc)
        return None
    if str(p) not in _MISSING_WARNED:
        _MISSING_WARNED.add(str(p))
        log.warning('dividends: %s missing — q=0 everywhere', p)
    return None


def coverage_start() -> pd.Timestamp | None:
    df = _dividends()
    return None if df is None else pd.Timestamp(df['ex_date'].min())


def backfill_reference_date() -> pd.Timestamp | None:
    cs = coverage_start()
    return None if cs is None else cs + TRAILING


def dividend_yield_asof(ticker: str, as_of, spot: float, ref_spot: float | None = None) -> float:
    df = _dividends()
    if df is None or spot is None or not (spot > 0):
        return 0.0
    d = df[df['symbol'] == str(ticker)]
    if d.empty:
        return 0.0
    as_of_ts = pd.Timestamp(as_of).normalize()
    lo = as_of_ts - TRAILING
    cs = pd.Timestamp(df['ex_date'].min())
    if lo < cs:
        # Incomplete trailing window (ruling G6): first full trailing year [cs, cs + 365 d).
        win = d[(d['ex_date'] >= cs) & (d['ex_date'] < cs + TRAILING)]
        if str(ticker) not in _BACKFILL_WARNED:
            _BACKFILL_WARNED.add(str(ticker))
            log.warning('dividends: q backfilled with the first full trailing year %s..%s for %s '
                        '(as_of %s precedes coverage + 365 d)', cs.date(), (cs + TRAILING).date(),
                        ticker, as_of_ts.date())
        denom = float(ref_spot) if (ref_spot is not None and ref_spot > 0) else float(spot)
        return float(win['cash_amount'].sum() / denom)
    win = d[(d['ex_date'] > lo) & (d['ex_date'] <= as_of_ts)]
    return float(win['cash_amount'].sum() / float(spot))
```

- [ ] **Step 4: Run**

Run: `python3 -m pytest tests/backtest/test_dividends.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/dividends.py tests/backtest/test_dividends.py
git commit -m "feat(backtest): trailing-year cash dividend yield with pre-coverage backfill for the synthetic options engine (spec 2026-09-06 B.1)"
```

---

### Task 5: Pricing — dividend yield `q`, CRR American tree, `price`/`delta` dispatchers

**Files:**
- Modify: `src/backtest/options_pricing.py`
- Create: `tests/backtest/test_american_pricing.py`

**Interfaces:**
- Consumes: `_rate(r, as_of)`, `_bs`, `_greeks` (existing).
- Produces: `bs_price(flag, S, K, t, sigma, r=None, as_of=None, q=0.0)`, `bs_greeks(..., q=0.0)`, `implied_vol(..., q=0.0)`, `strike_for_target_delta(flag, S, t, sigma, target_delta, r=None, as_of=None, q=0.0)`, `american_price(flag, S, K, t, sigma, r=None, as_of=None, q=0.0, steps=AMERICAN_STEPS)`, `american_delta(...)`, `price(flag, S, K, t, sigma, r=None, as_of=None, q=0.0, exercise='european')`, `delta(...)`, `AMERICAN_STEPS = 200`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_american_pricing.py
"""Spec 2026-09-06 B.2 oracles for q and the CRR American tree (ruling G7)."""
from __future__ import annotations
import itertools
import pytest

from backtest.options_pricing import (bs_price, bs_greeks, american_price, american_delta,
                                      price, delta, AMERICAN_STEPS)


def test_hull_american_put_example():
    # Hull, Options, Futures and Other Derivatives — binomial-tree chapter example:
    # S=50, K=50, r=10 %, σ=40 %, T=5 months. Fine tree ≈ 4.28–4.29; European ≈ 4.08.
    am = american_price('p', 50, 50, 5 / 12, 0.40, r=0.10, steps=500)
    eu = bs_price('p', 50, 50, 5 / 12, 0.40, r=0.10)
    assert 4.25 <= am <= 4.31
    assert eu == pytest.approx(4.08, abs=0.02)
    assert am > eu


def test_american_never_below_european_on_a_grid():
    for flag, K, t, q in itertools.product('cp', (80.0, 100.0, 120.0), (0.1, 0.5), (0.0, 0.03)):
        am = american_price(flag, 100.0, K, t, 0.3, r=0.05, q=q)
        eu = bs_price(flag, 100.0, K, t, 0.3, r=0.05, q=q)
        # an N=200 tree carries ~cent-level discretisation error against the closed form
        assert am >= eu * 0.99 - 0.01, (flag, K, t, q, am, eu)


def test_american_call_without_dividend_is_the_european_call():
    assert american_price('c', 100, 110, 0.5, 0.3, r=0.05) == bs_price('c', 100, 110, 0.5, 0.3, r=0.05)


def test_deep_itm_american_put_is_intrinsic():
    assert american_price('p', 20.0, 100.0, 0.5, 0.2, r=0.05) == pytest.approx(80.0, abs=1e-9)


def test_tree_converges():
    p200 = american_price('p', 100, 100, 0.5, 0.3, r=0.05, q=0.02, steps=200)
    p800 = american_price('p', 100, 100, 0.5, 0.3, r=0.05, q=0.02, steps=800)
    assert abs(p200 - p800) / p800 < 0.005
    assert AMERICAN_STEPS == 200


def test_q_lowers_calls_and_raises_puts():
    assert bs_price('c', 100, 100, 0.5, 0.2, r=0.04, q=0.03) < bs_price('c', 100, 100, 0.5, 0.2, r=0.04)
    assert bs_price('p', 100, 100, 0.5, 0.2, r=0.04, q=0.03) > bs_price('p', 100, 100, 0.5, 0.2, r=0.04)
    assert bs_greeks('c', 100, 100, 0.5, 0.2, r=0.04, q=0.03)['delta'] < bs_greeks('c', 100, 100, 0.5, 0.2, r=0.04)['delta']


def test_q_zero_path_is_the_legacy_path():
    # Same py_vollib call as before this task: the 6.627 reference from test_options_pricing.py.
    assert bs_price('c', 100, 100, 0.5, 0.2) == bs_price('c', 100, 100, 0.5, 0.2, q=0.0)
    assert abs(bs_price('c', 100, 100, 0.5, 0.2, q=0.0) - 6.627) < 0.01


def test_dispatchers_and_delta_bounds():
    assert price('p', 100, 100, 0.5, 0.3, r=0.05, exercise='american') == american_price('p', 100, 100, 0.5, 0.3, r=0.05)
    assert price('p', 100, 100, 0.5, 0.3, r=0.05) == bs_price('p', 100, 100, 0.5, 0.3, r=0.05)
    dc = delta('c', 100, 100, 0.5, 0.3, r=0.05, q=0.02, exercise='american')
    dp = delta('p', 100, 100, 0.5, 0.3, r=0.05, q=0.02, exercise='american')
    assert 0.0 < dc < 1.0 and -1.0 < dp < 0.0
    assert american_delta('p', 100, 100, 0.5, 0.3, r=0.05, q=0.02) == dp
    with pytest.raises(ValueError):
        price('p', 100, 100, 0.5, 0.3, exercise='bermudan')
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/backtest/test_american_pricing.py -q`
Expected: `ImportError` (no `american_price`).

- [ ] **Step 3: Implement**

Replace the module body of `src/backtest/options_pricing.py` from the imports through `strike_for_target_delta` with the following (keep `nearest_monthly_expiry` exactly as it is):

```python
"""Black-Scholes(-Merton) pricing/greeks wrapper over py_vollib plus a CRR
American tree for the synthetic options backtest engine (SP-4 Phase 0;
spec 2026-09-06 B.2). All functions are pure + deterministic.

Rate: `as_of=None` keeps the legacy flat RISK_FREE (4 %); with `as_of` the
rate comes from backtest.risk_free (DGS3MO behind OPENCLAW_RF_SOURCE).
Dividends: `q` (continuous yield) defaults to 0.0, and the q == 0 path calls
the SAME py_vollib black_scholes functions as before — bit-identical.
American exercise: CRR binomial tree (ruling G7), delta by central difference.
"""
from __future__ import annotations
import math
from datetime import date, timedelta
import numpy as np
from scipy.optimize import brentq
from py_vollib.black_scholes import black_scholes as _bs
from py_vollib.black_scholes.greeks import analytical as _greeks
from py_vollib.black_scholes_merton import black_scholes_merton as _bsm
from py_vollib.black_scholes_merton.greeks import analytical as _bsm_greeks

RISK_FREE = 0.04  # flat annual risk-free when as_of is None; see module docstring
AMERICAN_STEPS = 200
EXERCISES = ('european', 'american')


def _rate(r, as_of):
    if as_of is None:
        return RISK_FREE if r is None else r
    from backtest.risk_free import rf_annual_asof
    return rf_annual_asof(as_of) if r is None else r


def _clean(t, sigma):
    return max(float(t), 1e-6), max(float(sigma), 1e-4)


def bs_price(flag: str, S: float, K: float, t: float, sigma: float,
             r: float | None = None, as_of=None, q: float = 0.0) -> float:
    """flag 'c'|'p'; t in years; q = continuous dividend yield. Guards degenerate t/sigma."""
    r = _rate(r, as_of)
    t, sigma = _clean(t, sigma)
    if q:
        return float(_bsm(flag, float(S), float(K), t, r, sigma, float(q)))
    return float(_bs(flag, float(S), float(K), t, r, sigma))


def bs_greeks(flag: str, S: float, K: float, t: float, sigma: float,
              r: float | None = None, as_of=None, q: float = 0.0) -> dict:
    r = _rate(r, as_of)
    t, sigma = _clean(t, sigma)
    if q:
        g, args = _bsm_greeks, (flag, S, K, t, r, sigma, float(q))
    else:
        g, args = _greeks, (flag, S, K, t, r, sigma)
    return {
        'delta': float(g.delta(*args)),
        'gamma': float(g.gamma(*args)),
        'theta': float(g.theta(*args)),
        'vega':  float(g.vega(*args)),
    }


def implied_vol(price: float, flag: str, S: float, K: float, t: float,
                r: float | None = None, as_of=None, q: float = 0.0) -> float:
    r = _rate(r, as_of)
    if q:
        from py_vollib.black_scholes_merton.implied_volatility import implied_volatility
        return float(implied_volatility(float(price), float(S), float(K),
                                        max(float(t), 1e-6), r, float(q), flag))
    from py_vollib.black_scholes.implied_volatility import implied_volatility
    return float(implied_volatility(float(price), float(S), float(K),
                                    max(float(t), 1e-6), r, flag))


def strike_for_target_delta(flag: str, S: float, t: float, sigma: float,
                            target_delta: float, r: float | None = None, as_of=None,
                            q: float = 0.0) -> float:
    """Solve for the strike whose |delta| == target_delta at (S, t, sigma).
    Calls: strike increases as delta decreases (OTM). Puts: |delta|.
    """
    r = _rate(r, as_of)
    t, sigma = _clean(t, sigma)
    td = abs(float(target_delta))

    def f(K):
        return abs(bs_greeks(flag, S, K, t, sigma, r=r, q=q)['delta']) - td

    lo, hi = S * 0.30, S * 3.0
    try:
        return float(brentq(f, lo, hi, maxiter=100, xtol=1e-4))
    except ValueError:
        return float(S)


def american_price(flag: str, S: float, K: float, t: float, sigma: float,
                   r: float | None = None, as_of=None, q: float = 0.0,
                   steps: int = AMERICAN_STEPS) -> float:
    """Cox–Ross–Rubinstein binomial tree (ruling G7). A call on a non-dividend
    payer is never exercised early ⇒ its European price, exactly and cheaply."""
    r = _rate(r, as_of)
    t, sigma = _clean(t, sigma)
    S, K, q = float(S), float(K), float(q)
    is_call = flag == 'c'
    if is_call and q <= 0.0:
        return bs_price('c', S, K, t, sigma, r=r)
    n = max(int(steps), 1)
    dt = t / n
    u = math.exp(sigma * math.sqrt(dt))
    d = 1.0 / u
    p = (math.exp((r - q) * dt) - d) / (u - d)
    p = min(max(p, 0.0), 1.0)
    disc = math.exp(-r * dt)
    j = np.arange(n + 1)
    ST = S * u ** (2 * j - n)
    V = np.maximum(ST - K, 0.0) if is_call else np.maximum(K - ST, 0.0)
    for i in range(n - 1, -1, -1):
        ST = S * u ** (2 * np.arange(i + 1) - i)
        V = disc * (p * V[1:] + (1.0 - p) * V[:-1])
        ex = np.maximum(ST - K, 0.0) if is_call else np.maximum(K - ST, 0.0)
        V = np.maximum(V, ex)
    return float(V[0])


def american_delta(flag: str, S: float, K: float, t: float, sigma: float,
                   r: float | None = None, as_of=None, q: float = 0.0,
                   steps: int = AMERICAN_STEPS) -> float:
    h = max(1e-3 * float(S), 1e-6)
    up = american_price(flag, float(S) + h, K, t, sigma, r=r, as_of=as_of, q=q, steps=steps)
    dn = american_price(flag, float(S) - h, K, t, sigma, r=r, as_of=as_of, q=q, steps=steps)
    return float((up - dn) / (2.0 * h))


def _check_exercise(exercise: str) -> str:
    if exercise not in EXERCISES:
        raise ValueError(f'exercise must be one of {EXERCISES}, got {exercise!r}')
    return exercise


def price(flag: str, S: float, K: float, t: float, sigma: float, r: float | None = None,
          as_of=None, q: float = 0.0, exercise: str = 'european') -> float:
    if _check_exercise(exercise) == 'american':
        return american_price(flag, S, K, t, sigma, r=r, as_of=as_of, q=q)
    return bs_price(flag, S, K, t, sigma, r=r, as_of=as_of, q=q)


def delta(flag: str, S: float, K: float, t: float, sigma: float, r: float | None = None,
          as_of=None, q: float = 0.0, exercise: str = 'european') -> float:
    if _check_exercise(exercise) == 'american':
        return american_delta(flag, S, K, t, sigma, r=r, as_of=as_of, q=q)
    return bs_greeks(flag, S, K, t, sigma, r=r, as_of=as_of, q=q)['delta']
```

Check the py_vollib signatures before relying on them: `python3 -c "import inspect, py_vollib.black_scholes_merton as m, py_vollib.black_scholes_merton.implied_volatility as iv; print(inspect.signature(m.black_scholes_merton)); print(inspect.signature(iv.implied_volatility))"` — expected `(flag, S, K, t, r, sigma, q)` and `(price, S, K, t, r, q, flag)`; adapt the two call sites if the installed 1.0.1 differs, and say so in the report.

- [ ] **Step 4: Run new + existing pricing tests**

Run: `python3 -m pytest tests/backtest/test_american_pricing.py tests/backtest/test_options_pricing.py tests/backtest/test_options_backtest.py -q`
Expected: all pass (the legacy tests are untouched and still green: q=0 path identical). Report the Hull value the tree actually gives at 500 steps.

- [ ] **Step 5: Commit**

```bash
git add src/backtest/options_pricing.py tests/backtest/test_american_pricing.py
git commit -m "feat(backtest): dividend yield q on every BS call, CRR American tree with finite-difference delta, price/delta dispatchers (spec 2026-09-06 B.2)"
```

---

### Task 6: IV anchor hierarchy — real surface, VIX9D/VIX term, realized

**Files:**
- Modify: `src/backtest/vol_index.py`
- Modify: `src/backtest/synthetic_iv.py`
- Create: `tests/backtest/test_synthetic_iv_hierarchy.py`
- Modify: `tests/backtest/test_vol_index.py::test_synthetic_iv_uses_vix_when_supported` (isolate from a real surface master)

**Interfaces:**
- Consumes: `data/master/options_surface.parquet` (`ticker, date, iv30, iv90`; env `OPENCLAW_OPTIONS_SURFACE_PATH`), `data/master/vol_indices.parquet` (`date, vix9d_close`; env `OPENCLAW_VOL_INDICES_PARQUET`), existing `_vix_series()`.
- Produces: `vol_index.interp_total_variance(d1, v1, d2, v2, target) -> float`, `vol_index.vix_term_point(as_of, dte=30) -> float | None`, `vol_index.vix_anchored_iv(underlying, as_of, dte=30)`, `vol_index._vix9d_series()`; `synthetic_iv.surface_iv(underlying, as_of, dte=30) -> float | None`, `synthetic_iv.synthetic_iv_detail(prices, vrp_factor=…, window=…, underlying=None, as_of=None, dte=30) -> tuple[float, str]` with source ∈ `{'surface', 'vix_term', 'realized'}`, `synthetic_iv.synthetic_iv(..., dte=30)`, `synthetic_iv.clear_cache()`, `synthetic_iv.SURFACE_PATH_ENV`, `SURFACE_ASOF_TOLERANCE`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/backtest/test_synthetic_iv_hierarchy.py
"""Spec 2026-09-06 B.3: surface → vix_term → realized, with dte-aware points."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest


def _surface(tmp_path, monkeypatch, rows):
    p = tmp_path / 'options_surface.parquet'
    pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_PATH', str(p))
    from backtest import synthetic_iv as si
    si.clear_cache()
    return si


def _px(n=120, seed=0):
    rng = np.random.default_rng(seed)
    return pd.Series(100 * np.cumprod(1 + rng.normal(0, 0.01, n)), index=pd.date_range('2026-04-01', periods=n, freq='B'))


def test_surface_tier_and_total_variance_interpolation(tmp_path, monkeypatch):
    si = _surface(tmp_path, monkeypatch, [{'ticker': 'ZZZT', 'date': pd.Timestamp('2026-08-03').date(), 'iv30': 0.20, 'iv90': 0.30}])
    iv30, src = si.synthetic_iv_detail(_px(), underlying='ZZZT', as_of='2026-08-04', dte=30)
    assert (iv30, src) == (pytest.approx(0.20), 'surface')
    iv60, _ = si.synthetic_iv_detail(_px(), underlying='ZZZT', as_of='2026-08-04', dte=60)
    w = 0.04 * (30 / 365) + (0.09 * (90 / 365) - 0.04 * (30 / 365)) * ((60 - 30) / 365) / ((90 - 30) / 365)
    assert iv60 == pytest.approx(math.sqrt(w / (60 / 365)))
    assert si.surface_iv('ZZZT', '2026-08-04', 200) == pytest.approx(0.30)       # flat beyond 90
    assert si.surface_iv('ZZZT', '2026-08-04', 10) == pytest.approx(0.20)        # flat below 30


def test_surface_tier_respects_asof_tolerance_and_missing_iv90(tmp_path, monkeypatch):
    si = _surface(tmp_path, monkeypatch, [{'ticker': 'ZZZT', 'date': pd.Timestamp('2026-08-03').date(), 'iv30': 0.20, 'iv90': None}])
    assert si.surface_iv('ZZZT', '2026-08-10', 60) == pytest.approx(0.20)        # 7 days: still inside, iv90 None ⇒ flat iv30
    assert si.surface_iv('ZZZT', '2026-08-11', 60) is None                       # 8 days: stale
    assert si.surface_iv('ZZZT', '2026-08-01', 30) is None                       # before the first row
    assert si.surface_iv('NOPE', '2026-08-04', 30) is None


def test_realized_tier_when_nothing_else_applies(tmp_path, monkeypatch):
    si = _surface(tmp_path, monkeypatch, [{'ticker': 'ZZZT', 'date': pd.Timestamp('2026-08-03').date(), 'iv30': 0.20, 'iv90': 0.30}])
    px = _px()
    iv, src = si.synthetic_iv_detail(px, vrp_factor=1.2, underlying='NOPE', as_of=px.index[-1])
    from backtest.synthetic_iv import realized_vol
    assert src == 'realized' and iv == pytest.approx(max(0.05, realized_vol(px) * 1.2))
    assert si.synthetic_iv(px, vrp_factor=1.2, underlying='NOPE', as_of=px.index[-1]) == iv
    assert si.synthetic_iv_detail(px)[1] == 'realized'                            # no underlying/as_of


def test_vix_term_point_interpolates_9d_and_30d(tmp_path, monkeypatch):
    from backtest import vol_index as vi
    p = tmp_path / 'vol_indices.parquet'
    pd.DataFrame([{'date': pd.Timestamp('2026-08-03').date(), 'vix_close': 20.0, 'vvix_close': 90.0, 'vix9d_close': 16.0}]).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_VOL_INDICES_PARQUET', str(p))
    vi._vix9d_series.cache_clear()
    monkeypatch.setattr(vi, '_vix_series', lambda: pd.Series([0.20], index=pd.DatetimeIndex([pd.Timestamp('2026-08-03')])))
    try:
        assert vi.vix_term_point('2026-08-03', 30) == pytest.approx(0.20)
        assert vi.vix_term_point('2026-08-03', 45) == pytest.approx(0.20)         # flat above 30
        assert vi.vix_term_point('2026-08-03', 9) == pytest.approx(0.16)
        assert vi.vix_term_point('2026-08-03', 5) == pytest.approx(0.16)          # flat below 9
        mid = vi.vix_term_point('2026-08-03', 20)
        assert 0.16 < mid < 0.20
        assert mid == pytest.approx(vi.interp_total_variance(9, 0.16, 30, 0.20, 20))
        assert vi.vix_anchored_iv('SPY', '2026-08-03', 20) == pytest.approx(vi.OPTION_UNDERLYING_BETA['SPY'] * mid)
        assert vi.vix_anchored_iv('SPY', '2026-08-03') == pytest.approx(vi.OPTION_UNDERLYING_BETA['SPY'] * 0.20)
        assert vi.vix_term_point('2026-07-01', 30) is None                        # before any VIX
    finally:
        vi._vix9d_series.cache_clear()


def test_vix_term_tier_is_used_for_supported_names_without_surface(tmp_path, monkeypatch):
    si = _surface(tmp_path, monkeypatch, [{'ticker': 'ZZZT', 'date': pd.Timestamp('2026-08-03').date(), 'iv30': 0.20, 'iv90': 0.30}])
    from backtest import vol_index as vi
    monkeypatch.setattr(vi, 'vix_term_point', lambda as_of, dte=30: 0.25)
    iv, src = si.synthetic_iv_detail(_px(), underlying='SPY', as_of='2026-08-04', dte=30)
    assert src == 'vix_term' and iv == pytest.approx(vi.OPTION_UNDERLYING_BETA['SPY'] * 0.25)


def test_interp_total_variance_endpoints_and_monotone():
    from backtest.vol_index import interp_total_variance
    assert interp_total_variance(30, 0.2, 90, 0.3, 30) == 0.2
    assert interp_total_variance(30, 0.2, 90, 0.3, 90) == 0.3
    assert interp_total_variance(30, 0.2, 90, 0.3, 1) == 0.2 and interp_total_variance(30, 0.2, 90, 0.3, 400) == 0.3
    a, b = interp_total_variance(30, 0.2, 90, 0.3, 45), interp_total_variance(30, 0.2, 90, 0.3, 75)
    assert 0.2 < a < b < 0.3
```

Edit `tests/backtest/test_vol_index.py::test_synthetic_iv_uses_vix_when_supported` so it cannot see a real surface master: add the `tmp_path, monkeypatch` parameters and, as its first lines,
```python
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_PATH', str(tmp_path / 'no_surface.parquet'))
    from backtest import synthetic_iv as si; si.clear_cache()
```
(Everything else in that test stays.)

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/backtest/test_synthetic_iv_hierarchy.py -q`
Expected: `AttributeError`/`ImportError` on `synthetic_iv_detail`, `clear_cache`, `vix_term_point`.

- [ ] **Step 3: Implement — `src/backtest/vol_index.py`**

Add the imports `import math, os` and `from pathlib import Path` next to the existing ones, and add after `_PRICES = ...`:

```python
_VOL_INDICES_ENV = 'OPENCLAW_VOL_INDICES_PARQUET'
_VOL_INDICES = 'data/master/vol_indices.parquet'
VIX9D_DTE, VIX_DTE = 9, 30


@functools.lru_cache(maxsize=1)
def _vix9d_series() -> pd.Series:
    """VIX9D closes (decimal vol) from vol_indices.parquet; EMPTY when the file
    or column is unavailable (the term point then degrades to flat VIX)."""
    p = Path(os.environ.get(_VOL_INDICES_ENV) or _VOL_INDICES)
    try:
        df = pq.read_table(p, columns=['date', 'vix9d_close']).to_pandas()
    except Exception:  # noqa: BLE001
        return pd.Series(dtype=float)
    df['date'] = pd.to_datetime(df['date'])
    s = df.dropna(subset=['vix9d_close']).set_index('date')['vix9d_close'].sort_index() / 100.0
    return s[~s.index.duplicated(keep='last')].astype(float)


def interp_total_variance(d1: float, v1: float, d2: float, v2: float, target: float) -> float:
    """Linear in total variance σ²·T between (d1, v1) and (d2, v2), days; flat outside."""
    if target <= d1:
        return float(v1)
    if target >= d2:
        return float(v2)
    t1, t2, tt = d1 / 365.0, d2 / 365.0, target / 365.0
    w = v1 * v1 * t1 + (v2 * v2 * t2 - v1 * v1 * t1) * (tt - t1) / (t2 - t1)
    return math.sqrt(max(w, 0.0) / tt)


def vix_term_point(as_of, dte: int = 30) -> float | None:
    """VIX term point at `dte` (decimal vol): VIX9D (9 d) ↔ VIX (30 d) interpolated
    in total variance, flat VIX9D below 9 d, flat VIX above 30 d (spec 2026-09-06 B.3;
    no VIX3M in any master). At dte = 30 this is exactly the VIX close, so the
    legacy `beta × VIX` contract holds."""
    hist = _vix_series().loc[:pd.Timestamp(as_of)]
    if len(hist) == 0:
        return None
    v30 = float(hist.iloc[-1])
    if dte >= VIX_DTE:
        return v30
    s9 = _vix9d_series()
    h9 = s9.loc[:pd.Timestamp(as_of)] if len(s9) else s9
    if len(h9) == 0:
        return v30
    return interp_total_variance(VIX9D_DTE, float(h9.iloc[-1]), VIX_DTE, v30, dte)
```

Replace the body of `vix_anchored_iv` with:

```python
def vix_anchored_iv(underlying: str, as_of, dte: int = 30) -> float | None:
    """beta × the VIX term point at `dte` for a supported underlying; None otherwise
    or if no VIX history up to as_of."""
    beta = OPTION_UNDERLYING_BETA.get(underlying)
    if beta is None:
        return None
    pt = vix_term_point(as_of, dte)
    return None if pt is None else float(beta) * pt
```

- [ ] **Step 4: Implement — `src/backtest/synthetic_iv.py`**

Replace the module with:

```python
"""Synthetic implied-vol model for the options backtest engine (SP-4 Phase 0;
spec 2026-09-06 B.3 anchor hierarchy).

synthetic_iv_detail resolves, in order:
  1. 'surface'  — the REAL surface master (options_surface.parquet: iv30/iv90 as-of
                  the underlying, ≤ 7 days old), constant-maturity to `dte` in total
                  variance (flat iv30 below 30 d, flat iv90 above 90 d);
  2. 'vix_term' — beta × the VIX9D/VIX term point for OPTION_UNDERLYING_BETA names;
  3. 'realized' — trailing realized vol × VRP factor, floored.
The VRP factor (and window) are CALIBRATED by scripts/options_parity_check.py.
"""
from __future__ import annotations

import functools
import os
from pathlib import Path

import numpy as np
import pandas as pd

IV_FLOOR = 0.05
DEFAULT_WINDOW = 21          # trading days
DEFAULT_VRP_FACTOR = 1.15    # placeholder; calibrated by options_parity_check.py
TRADING_DAYS = 252
ROOT = Path(__file__).resolve().parents[2]
SURFACE_PATH_ENV = 'OPENCLAW_OPTIONS_SURFACE_PATH'
SURFACE_ASOF_TOLERANCE = pd.Timedelta(days=7)
SURFACE_DTES = (30, 90)


def realized_vol(prices: pd.Series, window: int = DEFAULT_WINDOW) -> float:
    """Annualized close-to-close realized vol over the trailing `window` days."""
    s = prices.dropna()
    if len(s) < 3:
        return IV_FLOOR
    rets = s.pct_change().dropna().iloc[-window:]
    if len(rets) < 2:
        return IV_FLOOR
    return float(rets.std(ddof=1) * np.sqrt(TRADING_DAYS))


def surface_path() -> Path:
    return Path(os.environ.get(SURFACE_PATH_ENV) or (ROOT / 'data' / 'master' / 'options_surface.parquet'))


def clear_cache() -> None:
    _surface_rows.cache_clear()


@functools.lru_cache(maxsize=512)
def _surface_rows(ticker: str, path_str: str, mtime_ns: int) -> pd.DataFrame:
    import pyarrow.parquet as pq
    df = pq.read_table(path_str, columns=['date', 'iv30', 'iv90'],
                       filters=[('ticker', '==', ticker)]).to_pandas()
    df['date'] = pd.to_datetime(df['date']).dt.normalize()
    df['iv30'] = pd.to_numeric(df['iv30'], errors='coerce')
    df['iv90'] = pd.to_numeric(df['iv90'], errors='coerce')
    return (df.dropna(subset=['iv30']).sort_values('date')
              .drop_duplicates('date', keep='last').reset_index(drop=True))


def surface_iv(underlying: str, as_of, dte: int = 30) -> float | None:
    """Tier 1: the real surface's constant-maturity ATM IV at `dte`, or None."""
    p = surface_path()
    try:
        if not p.exists():
            return None
        rows = _surface_rows(str(underlying), str(p), p.stat().st_mtime_ns)
    except Exception:  # noqa: BLE001 — an unreadable master degrades to the next tier
        return None
    if rows.empty:
        return None
    asof = pd.Timestamp(as_of).normalize()
    prior = rows[rows['date'] <= asof]
    if prior.empty or (asof - prior['date'].iloc[-1]) > SURFACE_ASOF_TOLERANCE:
        return None
    r = prior.iloc[-1]
    iv30 = float(r['iv30'])
    if not (iv30 > 0):
        return None
    iv90 = float(r['iv90']) if r['iv90'] == r['iv90'] else None
    if iv90 is None or not (iv90 > 0):
        return iv30
    from backtest.vol_index import interp_total_variance
    return interp_total_variance(SURFACE_DTES[0], iv30, SURFACE_DTES[1], iv90, int(dte))


def synthetic_iv_detail(prices: pd.Series, vrp_factor: float = DEFAULT_VRP_FACTOR,
                        window: int = DEFAULT_WINDOW, underlying: str | None = None,
                        as_of=None, dte: int = 30) -> tuple[float, str]:
    """(iv, source) for an underlying as of the last bar in `prices`."""
    if underlying is not None and as_of is not None:
        s = surface_iv(underlying, as_of, dte)
        if s is not None:
            return max(IV_FLOOR, float(s)), 'surface'
        from backtest.vol_index import vix_anchored_iv
        anchored = vix_anchored_iv(underlying, as_of, dte)
        if anchored is not None:
            return max(IV_FLOOR, float(anchored)), 'vix_term'
    rv = realized_vol(prices, window=window)
    return max(IV_FLOOR, float(rv) * float(vrp_factor)), 'realized'


def synthetic_iv(prices: pd.Series, vrp_factor: float = DEFAULT_VRP_FACTOR,
                 window: int = DEFAULT_WINDOW, underlying: str | None = None,
                 as_of=None, dte: int = 30) -> float:
    """Modeled IV — see synthetic_iv_detail for the tier order."""
    return synthetic_iv_detail(prices, vrp_factor=vrp_factor, window=window,
                               underlying=underlying, as_of=as_of, dte=dte)[0]
```

Also add one line to the docstring of `scripts/options_parity_check.py` (after the AUTHORITATIVE GATE paragraph): `2026-09-06: with the surface tier ON by default (spec 2026-09-06 B.3, ruling G8) the IV gate compares surface to chain on the overlap window and is expected to be near 0 there; run it with OPENCLAW_OPTIONS_SURFACE_PATH pointed at an empty path to measure the vix_term tier.`

- [ ] **Step 5: Run**

Run: `python3 -m pytest tests/backtest/test_synthetic_iv_hierarchy.py tests/backtest/test_vol_index.py tests/backtest/test_synthetic_iv.py tests/backtest/test_synthetic_vix.py tests/backtest/test_options_backtest.py -q`
Expected: all pass. (`test_vol_index.py` reads the production `prices.parquet` `^VIX` column through the existing `_vix_series` — pre-existing behaviour, unchanged here.)

- [ ] **Step 6: Commit**

```bash
git add src/backtest/vol_index.py src/backtest/synthetic_iv.py scripts/options_parity_check.py tests/backtest/test_synthetic_iv_hierarchy.py tests/backtest/test_vol_index.py
git commit -m "feat(backtest): synthetic IV anchor hierarchy — real surface, VIX9D/VIX term point, realized (spec 2026-09-06 B.3)"
```

---

### Task 7: Engine wiring — `OptionSpec.exercise`, q, dte-aware IV, iv-sources line

**Files:**
- Modify: `src/strategies/base.py` (`OptionSpec`)
- Modify: `src/backtest/options_backtest.py`
- Modify: `tests/backtest/test_options_backtest.py` (append tests)

**Interfaces:**
- Consumes: `price/delta/strike_for_target_delta` (Task 5), `synthetic_iv_detail` (Task 6), `dividend_yield_asof/backfill_reference_date` (Task 4).
- Produces: `OptionSpec.exercise: str = 'american'`; cycle pricers accept `stats: dict | None = None`; `simulate` logs `[options_backtest] iv sources: surface=… vix_term=… realized=…; exercise=…; q>0 on … prices`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/backtest/test_options_backtest.py`:

```python
# ── spec 2026-09-06 B.4: dividends, American exercise, dte-aware IV ──
def _isolated_masters(tmp_path, monkeypatch, spy_q=0.02):
    """No production masters: an empty surface path, a tiny corporate_actions file
    that gives SPY a `spy_q` trailing yield at a 500 spot, and no VIX9D file."""
    monkeypatch.setenv('OPENCLAW_OPTIONS_SURFACE_PATH', str(tmp_path / 'no_surface.parquet'))
    monkeypatch.setenv('OPENCLAW_VOL_INDICES_PARQUET', str(tmp_path / 'no_vol.parquet'))
    rows = [{'symbol': 'SPY', 'action_type': 'cash_dividend', 'ex_date': d.date(), 'cash_amount': spy_q * 500.0 / 4}
            for d in pd.date_range('2019-01-15', '2026-09-01', freq='3MS')]
    p = tmp_path / 'corporate_actions.parquet'; pd.DataFrame(rows).to_parquet(p, index=False)
    monkeypatch.setenv('OPENCLAW_CORPORATE_ACTIONS_PARQUET', str(p))
    from backtest import dividends, synthetic_iv, vol_index
    dividends.clear_cache(); synthetic_iv.clear_cache(); vol_index._vix9d_series.cache_clear()


def test_option_spec_exercise_defaults_to_american_and_round_trips():
    from strategies.base import OptionSpec
    assert OptionSpec(underlying='SPY').exercise == 'american'
    assert OptionSpec.from_dict({'underlying': 'SPY', 'exercise': 'european'}).exercise == 'european'


def test_american_put_cycle_costs_at_least_the_european_one(tmp_path, monkeypatch):
    from backtest import options_backtest as ob
    from strategies.base import OptionSpec
    _isolated_masters(tmp_path, monkeypatch, spy_q=0.03)
    monkeypatch.setattr(ob, 'synthetic_iv_detail', lambda *a, **k: (0.30, 'realized'))
    idx = pd.date_range('2025-06-02', periods=60, freq='B')
    flat = pd.Series(500.0, index=idx)
    am = OptionSpec(underlying='SPY', right='put', strike_rule='atm', dte_target=30, exercise='american')
    eu = OptionSpec(underlying='SPY', right='put', strike_rule='atm', dte_target=30, exercise='european')
    stats = ob._new_stats()
    ca = ob._price_single_cycle(am, flat, idx[0], +1, 1.2, 21, 10, stats=stats)
    ce = ob._price_single_cycle(eu, flat, idx[0], +1, 1.2, 21, 10)
    assert ca['entry_price'] >= ce['entry_price'] > 0
    assert stats['q_positive'] > 0 and stats['exercise'] == {'american'}


def test_simulate_logs_iv_sources_and_keeps_trade_keys(tmp_path, monkeypatch, caplog):
    import logging
    _isolated_masters(tmp_path, monkeypatch)
    close_wide, bars = _trending_panels()
    regimes = pd.Series('LOW_VOL', index=close_wide.index)
    inst = _LongCallStrat()
    with caplog.at_level(logging.INFO):
        out = options_backtest.simulate(inst, close_wide, bars, regimes, close_wide.index[0], close_wide.index[-1],
                                        strategy_id='T_long_call', vrp_factor=1.1)
    assert len(out['trades']) >= 1
    t = out['trades'][0]
    assert set(t) == {'entry_date', 'exit_date', 'entry_price', 'exit_price', 'exit_reason', 'holding_days',
                      'pnl_pct', 'strike', 'expiry', 'iv_entry', 'signal_stop', 'signal_target',
                      'ticker', 'direction', 'entry_regime'}
    line = [r.message for r in caplog.records if r.message.startswith('[options_backtest] iv sources:')]
    assert len(line) == 1 and 'realized=' in line[0] and 'exercise=american' in line[0]
    # the 2022 panel predates the dividend fixture's coverage + 365 d?  No — coverage starts 2019, so q > 0 applies
    assert 'q>0 on 0 prices' not in line[0]
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/backtest/test_options_backtest.py -q -k "exercise or american_put_cycle or iv_sources"`
Expected: failures (`exercise` unknown field; `synthetic_iv_detail` not an attribute of the engine module; no log line).

- [ ] **Step 3: Implement — `src/strategies/base.py`**

In `OptionSpec`, after `hold_to_expiry: bool = False` add:
```python
    exercise:      str = 'american'          # 'american' | 'european' — US-listed equity/ETF options are American (spec 2026-09-06 B.4, G9)
```

- [ ] **Step 4: Implement — `src/backtest/options_backtest.py`**

Replace the import block and the two cycle pricers, and extend `simulate`, as follows. Imports:

```python
from collections import Counter
from backtest.options_pricing import (price as _price, delta as _delta, strike_for_target_delta,
                                      nearest_monthly_expiry)
from backtest.synthetic_iv import synthetic_iv_detail
from backtest.dividends import dividend_yield_asof, backfill_reference_date
```
(remove the old `bs_price, bs_greeks, RISK_FREE` and `synthetic_iv` imports; grep the module for any other use of them — there is none.)

Add after `HEDGE_COST_PER_SHARE_BPS`:

```python
def _new_stats() -> dict:
    return {'iv_sources': Counter(), 'q_positive': 0, 'exercise': set()}


def _iv(close_to_dt: pd.Series, spec, dt, dte: int, vrp_factor: float, window: int, stats: dict) -> float:
    """IV for `spec.underlying` on `dt` at the contract's REMAINING life (spec B.4):
    surface → vix_term → realized; the tier is counted for the summary line."""
    iv, src = synthetic_iv_detail(close_to_dt, vrp_factor=vrp_factor, window=window,
                                  underlying=spec.underlying, as_of=_as_date(dt), dte=max(int(dte), 1))
    stats['iv_sources'][src] += 1
    return iv


def _q(spec, close: pd.Series, dt, S: float, stats: dict) -> float:
    """Trailing-year dividend yield; the backfill reference close is passed when
    the series reaches it (spec B.1 / ruling G6)."""
    ref = backfill_reference_date()
    ref_spot = None
    if ref is not None:
        upto = close.loc[:ref]
        if len(upto):
            ref_spot = float(upto.iloc[-1])
    q = dividend_yield_asof(spec.underlying, _as_date(dt), S, ref_spot=ref_spot)
    if q > 0:
        stats['q_positive'] += 1
    return q
```

`_select_strike` and `_legs_for` gain a `q` argument and pass it on:

```python
def _select_strike(spec, S: float, t_years: float, sigma: float, flag: str, q: float = 0.0) -> float:
    if spec.strike_rule == 'atm':
        return float(S)
    if spec.strike_rule == 'fixed_moneyness' and spec.moneyness:
        return float(S * spec.moneyness)
    return strike_for_target_delta(flag, S, t_years, sigma, spec.target_delta, q=q)


def _legs_for(spec, S, t_years, sigma, q: float = 0.0):
    """Return list of (flag, K) legs for a straddle/strangle."""
    if spec.structure == 'straddle':
        return [('c', S), ('p', S)]  # ATM call + put
    Kc = strike_for_target_delta('c', S, t_years, sigma, spec.target_delta, q=q)
    Kp = strike_for_target_delta('p', S, t_years, sigma, spec.target_delta, q=q)
    return [('c', Kc), ('p', Kp)]
```

`_price_multileg_cycle(spec, close, entry_dt, sign, vrp_factor, window, max_hold_days, stats=None)`:

```python
def _price_multileg_cycle(spec, close, entry_dt, sign, vrp_factor, window, max_hold_days, stats=None):
    """sign +1 long the structure, -1 short. Delta-hedged daily when spec.hedge=='delta'.
    Option PnL and hedge PnL are tracked separately; pnl_pct sums both over the cycle."""
    stats = _new_stats() if stats is None else stats
    ex = spec.exercise
    stats['exercise'].add(ex)
    idx = close.index
    fut = idx[idx > entry_dt]
    if len(fut) == 0:
        return None
    S0 = float(close.loc[entry_dt])
    expiry = nearest_monthly_expiry(_as_date(entry_dt), spec.dte_target)
    dte0 = (expiry - _as_date(entry_dt)).days
    t0 = max(dte0 / 365.0, 1e-6)
    sigma0 = _iv(close.loc[:entry_dt], spec, entry_dt, dte0, vrp_factor, window, stats)
    q0 = _q(spec, close, entry_dt, S0, stats)
    legs = _legs_for(spec, S0, t0, sigma0, q0)
    entry_prem = sum(_price(f, S0, K, t0, sigma0, as_of=_as_date(entry_dt), q=q0, exercise=ex) for f, K in legs)
    if entry_prem <= 0:
        return None

    def net_delta(S, t, sig, dt, q):
        return sum(_delta(f, S, K, max(t, 1e-6), sig, as_of=_as_date(dt), q=q, exercise=ex) for f, K in legs)

    hedge_units = 0.0
    hedge_pnl = 0.0
    prev_S = S0
    if spec.hedge == 'delta':
        target_units = -sign * net_delta(S0, t0, sigma0, entry_dt, q0) * MULTIPLIER
        hedge_pnl -= abs(target_units - hedge_units) * S0 * (HEDGE_COST_PER_SHARE_BPS / 1e4)
        hedge_units = target_units

    exit_dt = exit_prem = reason = None
    held = 0
    for dt in fut[:max_hold_days]:
        held += 1
        cur = _as_date(dt); S = float(close.loc[dt]); dte = (expiry - cur).days
        hedge_pnl += hedge_units * (S - prev_S)
        prev_S = S
        if dte <= 0:
            exit_prem = sum(max(0.0, (S - K) if f == 'c' else (K - S)) for f, K in legs)
            exit_dt, reason = dt, 'expiry'; break
        sig_t = _iv(close.loc[:dt], spec, dt, dte, vrp_factor, window, stats)
        q_t = _q(spec, close, dt, S, stats)
        if (not spec.hold_to_expiry) and dte <= spec.roll_dte:
            exit_prem = sum(_price(f, S, K, dte / 365.0, sig_t, as_of=cur, q=q_t, exercise=ex) for f, K in legs)
            exit_dt, reason = dt, 'roll'; break
        if spec.hedge == 'delta':
            target_units = -sign * net_delta(S, dte / 365.0, sig_t, dt, q_t) * MULTIPLIER
            hedge_pnl -= abs(target_units - hedge_units) * S * (HEDGE_COST_PER_SHARE_BPS / 1e4)
            hedge_units = target_units
    if exit_dt is None:
        dt = fut[:max_hold_days][-1]; S = float(close.loc[dt]); cur = _as_date(dt)
        dte = max((expiry - cur).days, 0)
        sig_t = _iv(close.loc[:dt], spec, dt, dte, vrp_factor, window, stats)
        q_t = _q(spec, close, dt, S, stats)
        exit_prem = (sum(_price(f, S, K, max(dte / 365.0, 1e-6), sig_t, as_of=cur, q=q_t, exercise=ex) for f, K in legs)
                     if dte > 0 else sum(max(0.0, (S - K) if f == 'c' else (K - S)) for f, K in legs))
        exit_dt, reason = dt, 'max_hold'

    # Liquidate the residual hedge at the exit bar (closeout friction).
    hedge_pnl -= abs(hedge_units) * S * (HEDGE_COST_PER_SHARE_BPS / 1e4)
    cost = (entry_prem + exit_prem) * (COST_PER_CONTRACT_BPS / 1e4)
    option_pnl = sign * (exit_prem - entry_prem) * MULTIPLIER - cost * MULTIPLIER
    cycle_pnl = option_pnl + hedge_pnl
    base = S0 * MULTIPLIER
    return {
        'entry_date': _as_date(entry_dt), 'exit_date': _as_date(exit_dt),
        'entry_price': round(entry_prem, 4), 'exit_price': round(exit_prem, 4),
        'exit_reason': reason, 'holding_days': held,
        'pnl_pct': float(cycle_pnl / base),
        'option_pnl_pct': float(option_pnl / base),
        'hedge_pnl_pct': float(hedge_pnl / base),
        'expiry': expiry.isoformat(), 'iv_entry': round(sigma0, 4),
        'signal_stop': None, 'signal_target': None,
    }
```

`_price_single_cycle(spec, close, entry_dt, sign, vrp_factor, window, max_hold_days, stats=None)`:

```python
def _price_single_cycle(spec, close: pd.Series, entry_dt, sign: int,
                        vrp_factor: float, window: int, max_hold_days: int, stats=None) -> dict:
    """Price ONE single-leg cycle from entry_dt forward. Returns a trade dict
    or None if it can't be priced. sign +1 = long the option, -1 = short."""
    stats = _new_stats() if stats is None else stats
    ex = spec.exercise
    stats['exercise'].add(ex)
    idx = close.index
    fut = idx[idx > entry_dt]
    if len(fut) == 0:
        return None
    S0 = float(close.loc[entry_dt])
    flag = _flag_for(spec.right)
    expiry = nearest_monthly_expiry(_as_date(entry_dt), spec.dte_target)
    dte0 = (expiry - _as_date(entry_dt)).days
    t0 = max(dte0 / 365.0, 1e-6)
    sigma0 = _iv(close.loc[:entry_dt], spec, entry_dt, dte0, vrp_factor, window, stats)
    q0 = _q(spec, close, entry_dt, S0, stats)
    K = _select_strike(spec, S0, t0, sigma0, flag, q0)
    entry_premium = _price(flag, S0, K, t0, sigma0, as_of=_as_date(entry_dt), q=q0, exercise=ex)
    if entry_premium <= 0:
        return None

    exit_dt, exit_premium, reason = None, None, None
    held = 0
    for dt in fut[:max_hold_days]:
        held += 1
        cur_date = _as_date(dt)
        dte = (expiry - cur_date).days
        S = float(close.loc[dt])
        if dte <= 0:
            exit_premium = max(0.0, (S - K) if flag == 'c' else (K - S))  # intrinsic
            exit_dt, reason = dt, 'expiry'
            break
        if (not spec.hold_to_expiry) and dte <= spec.roll_dte:
            sig_t = _iv(close.loc[:dt], spec, dt, dte, vrp_factor, window, stats)
            q_t = _q(spec, close, dt, S, stats)
            exit_premium = _price(flag, S, K, max(dte / 365.0, 1e-6), sig_t, as_of=cur_date, q=q_t, exercise=ex)
            exit_dt, reason = dt, 'roll'
            break
    if exit_dt is None:
        dt = fut[:max_hold_days][-1]
        S = float(close.loc[dt]); cur_date = _as_date(dt)
        dte = max((expiry - cur_date).days, 0)
        sig_t = _iv(close.loc[:dt], spec, dt, dte, vrp_factor, window, stats)
        q_t = _q(spec, close, dt, S, stats)
        exit_premium = (_price(flag, S, K, max(dte / 365.0, 1e-6), sig_t, as_of=cur_date, q=q_t, exercise=ex)
                        if dte > 0 else max(0.0, (S - K) if flag == 'c' else (K - S)))
        exit_dt, reason = dt, 'max_hold'

    cost = (entry_premium + exit_premium) * (COST_PER_CONTRACT_BPS / 1e4)
    cycle_pnl = sign * (exit_premium - entry_premium) * MULTIPLIER - cost * MULTIPLIER
    pnl_pct = cycle_pnl / (S0 * MULTIPLIER)
    return {
        'entry_date': _as_date(entry_dt), 'exit_date': _as_date(exit_dt),
        'entry_price': round(entry_premium, 4), 'exit_price': round(exit_premium, 4),
        'exit_reason': reason, 'holding_days': held, 'pnl_pct': float(pnl_pct),
        'strike': round(K, 2), 'expiry': expiry.isoformat(), 'iv_entry': round(sigma0, 4),
        'signal_stop': None, 'signal_target': None,
    }
```

In `simulate`: create `stats = _new_stats()` right after `trades, days_processed, days_with_signals = [], 0, 0`; pass `stats=stats` to both `_price_single_cycle(...)` and `_price_multileg_cycle(...)` calls; and immediately before the final `return {...}` add:

```python
    src = stats['iv_sources']
    logger.info('[options_backtest] iv sources: surface=%d vix_term=%d realized=%d; exercise=%s; q>0 on %d prices',
                src.get('surface', 0), src.get('vix_term', 0), src.get('realized', 0),
                ','.join(sorted(stats['exercise'])) or 'n/a', stats['q_positive'])
```

Update the module docstring's second paragraph to: "Contracts are synthesized from underlying closes via Black-Scholes-Merton with a trailing-year dividend yield (backtest.dividends), American exercise on a CRR tree by default (OptionSpec.exercise), and an IV anchored to the real surface master when it covers the date, else the VIX9D/VIX term point, else realized-vol × VRP (backtest.synthetic_iv) — spec 2026-09-06 Part B."

- [ ] **Step 5: Run**

Run: `python3 -m pytest tests/backtest/test_options_backtest.py tests/backtest/test_american_pricing.py tests/backtest/test_options_pricing.py tests/backtest/test_synthetic_iv_hierarchy.py -q`
Expected: all pass, including the four pre-existing engine tests (they now price American contracts with q from the PRODUCTION `corporate_actions.parquet` — SPY's real yield — and the real surface master is absent for 2022 dates, so they land on the vix_term/realized tiers; if a pre-existing assertion becomes marginal, report the numbers rather than editing the assertion).

- [ ] **Step 6: Commit**

```bash
git add src/strategies/base.py src/backtest/options_backtest.py tests/backtest/test_options_backtest.py
git commit -m "feat(backtest): synthetic options engine prices with dividends, American exercise and dte-aware surface-first IV; one iv-sources summary line (spec 2026-09-06 B.4)"
```

---

### Task 8: Rollout script, runbook, changelog

**Files:**
- Create: `scripts/rollout_surface_v3.sh`
- Create: `docs/runbooks/2026-09-06-options-surface-v3-rollout.md`
- Modify: `docs/archive/changelog.md` (one entry at the top of "Recent Changes")

**Interfaces:**
- Consumes: `scripts/build_options_surface.py --start --end`, `scripts/compute_rolling_options_fields.py`, the v3 keys (Task 3).
- Produces: a one-shot rollout script the controller runs as a transient unit after merge; `--verify-only` re-runs just the verification.

- [ ] **Step 1: Write the script**

```bash
#!/usr/bin/env bash
# scripts/rollout_surface_v3.sh — options surface v3 rebuild + panel rebuild + verification
# (spec docs/specs/2026-09-06-options-mfiv-rnd-synthetic-engine-spec.md §C).
# Run as a transient unit in an idle window; it waits for any fleet child first:
#   sudo systemd-run --unit=surface-v3-rollout-$(date -u +%Y%m%d) -p Nice=19 -p MemoryMax=3500M \
#     -p RuntimeMaxSec=5h -E PYTHONUNBUFFERED=1 -E PYTHONPATH=/root/openclaw/src \
#     --working-directory=/root/openclaw /bin/bash scripts/rollout_surface_v3.sh
#   scripts/rollout_surface_v3.sh --verify-only     # re-run the checks on the current masters
set -uo pipefail
cd /root/openclaw || exit 2
export PYTHONPATH=/root/openclaw/src
VERIFY_ONLY=0; START=2026-06-29; END=$(date -u +%F)
while [ $# -gt 0 ]; do
  case "$1" in
    --verify-only) VERIFY_ONLY=1;; --start) START="$2"; shift;; --end) END="$2"; shift;;
    *) echo "unknown arg $1" >&2; exit 2;;
  esac; shift
done
ts() { date -u +%FT%TZ; }
if [ "$VERIFY_ONLY" = 0 ]; then
  for u in openclaw-fleet-overnight-resume.service fleet-rf-epoch-20260906.service options-surface-rollout-20260906.service; do
    while systemctl is-active --quiet "$u"; do echo "[v3 $(ts)] waiting for $u"; sleep 300; done
  done
  echo "[v3 $(ts)] build $START..$END"
  python3 scripts/build_options_surface.py --start "$START" --end "$END" || { echo "[v3 $(ts)] build FAILED"; exit 1; }
  echo "[v3 $(ts)] panel rebuild"
  python3 scripts/compute_rolling_options_fields.py || { echo "[v3 $(ts)] panel FAILED"; exit 1; }
fi
echo "[v3 $(ts)] verify"
python3 - <<'PY'
import sys
import pyarrow.parquet as pq, pyarrow.compute as pc, pandas as pd
cols = ['ticker', 'date', 'iv30', 'n_expiries_fit', 'options_features_version', 'mfiv_30d', 'mfiv_90d',
        'mf_tail_premium_30d', 'rn_skew_30d', 'rn_kurt_30d', 'rn_p_dn10_30d', 'rn_p_up10_30d']
meta = pq.read_metadata('data/master/options_surface.parquet')
last = pq.read_table('data/master/options_surface.parquet', columns=['date']).to_pandas()['date'].max()
df = pq.read_table('data/master/options_surface.parquet', columns=cols,
                   filters=[('date', '==', last)]).to_pandas()
fit = df[df['n_expiries_fit'] >= 2]
mf = fit['mfiv_30d'].notna().mean() * 100; rn = fit['rn_skew_30d'].notna().mean() * 100
ver = df['options_features_version'].value_counts().to_dict()
spy = df[df['ticker'] == 'SPY'].iloc[0] if (df['ticker'] == 'SPY').any() else None
print(f'surface rows={meta.num_rows:,} latest={pd.Timestamp(last).date()} tickers={df.ticker.nunique():,} '
      f'fit>=2: {len(fit):,} mfiv_nonnull={mf:.1f}% rn_nonnull={rn:.1f}% version={ver}')
ok = mf >= 90 and rn >= 90 and ver.get(3, 0) == len(df)
if spy is not None:
    print('SPY', {k: (None if pd.isna(spy[k]) else round(float(spy[k]), 4)) for k in cols[2:]})
    ok &= 0.0 <= float(spy['mf_tail_premium_30d']) <= 0.03 and float(spy['rn_skew_30d']) < 0 \
          and 0.001 <= float(spy['rn_p_dn10_30d']) <= 0.10
panel = pq.read_metadata('data/derived/options_aggregates_enriched.parquet')
pcols = set(pq.read_schema('data/derived/options_aggregates_enriched.parquet').names)
missing = [c for c in cols[5:] if c not in pcols]
print(f'panel rows={panel.num_rows:,} v3 columns missing={missing}')
ok &= not missing
print('VERIFY', 'OK' if ok else 'FAIL')
sys.exit(0 if ok else 1)
PY
rc=$?
echo "[v3 $(ts)] end rc=$rc"
exit $rc
```

- [ ] **Step 2: Syntax-check and dry the verification path against the fixture**

Run: `bash -n scripts/rollout_surface_v3.sh && echo OK`
Expected: OK. (`--verify-only` needs the production masters and is run by the controller after the rebuild, not here.)

- [ ] **Step 3: Write the runbook**

```markdown
# Options surface v3 (MFIV + RN density) and synthetic engine upgrades — rollout runbook

Spec: docs/specs/2026-09-06-options-mfiv-rnd-synthetic-engine-spec.md · Plan: docs/superpowers/plans/2026-09-06-options-mfiv-rnd-synthetic-engine.md

## What changes when this merges
- `strategies.options_surface` is version **3**: seven new `SCALAR_KEYS` — `mfiv_30d`, `mfiv_90d`, `mf_tail_premium_30d`, `rn_skew_30d`, `rn_kurt_30d`, `rn_p_dn10_30d`, `rn_p_up10_30d`. Every v2 value is pinned unchanged by `tests/strategies/test_options_surface_v2_freeze.py`.
- Live: `engine.load_aux_data` computes the v3 keys on every cycle (flag `OPENCLAW_OPTIONS_SURFACE` unchanged — 0 = shadow, so the keys are summarised, not served, until the flag flips). Shadow line gains `mfiv_nonnull=…% rn_nonnull=…%`.
- Backtest: the enriched panel carries the v3 columns after the rebuild below; `aux_data_loader.FIELDS` exposes them. No manifest strategy reads them yet.
- Synthetic options engine (`backtest.options_backtest`, no manifest consumer): dividend yield `q` from `corporate_actions.parquet`, American exercise (CRR tree) by default (`OptionSpec.exercise`), IV from the real surface master when it covers the date, else the VIX9D/VIX term point, else realized × VRP. One `[options_backtest] iv sources:` line per run.

## Steps (controller, after merge, first idle window — never beside a fleet child)
1. `sudo systemd-run --unit=surface-v3-rollout-$(date -u +%Y%m%d) -p Nice=19 -p MemoryMax=3500M -p RuntimeMaxSec=5h -E PYTHONUNBUFFERED=1 -E PYTHONPATH=/root/openclaw/src --working-directory=/root/openclaw /bin/bash scripts/rollout_surface_v3.sh` — waits for `openclaw-fleet-overnight-resume` / `fleet-rf-epoch-20260906` / `options-surface-rollout-20260906` to be inactive, rebuilds `data/master/options_surface.parquet` from 2026-06-29 (append_dedup replace on (ticker, date) — UNION BY NAME fills the new columns for every rebuilt row), rebuilds `data/derived/options_aggregates_enriched.parquet`, verifies.
2. Verification thresholds (script exit 0 = all met): `mfiv_nonnull ≥ 90 %` and `rn_nonnull ≥ 90 %` of tickers with ≥ 2 fitted expiries on the latest session; every latest-session row at version 3; SPY `0 ≤ mf_tail_premium_30d ≤ 0.03`, `rn_skew_30d < 0`, `0.1 % ≤ rn_p_dn10_30d ≤ 10 %`; the panel carries all seven columns.
3. No re-backtest: no strategy reads a v3 key and no manifest strategy uses the synthetic engine. The options sleeve's v2 values are byte-identical (freeze test).
4. Results: **TO BE FILLED by the rollout run on main** (surface rows/dates, coverage percentages, SPY row, panel rows).

## Watch list
- First live shadow line after merge: `mfiv_nonnull` / `rn_nonnull` ≥ 80 % (live chains are thinner than the EOD master's — 90 % is the master's bar).
- `python3 -m system_checks --check options_aux_freshness` unchanged.
- `scripts/options_parity_check.py`'s IV gate is near-zero on the surface overlap by construction (ruling G8); measure the vix_term tier with `OPENCLAW_OPTIONS_SURFACE_PATH` pointed at an empty path.

## Rollback
- Surface: revert the Part A commits, then `scripts/compute_rolling_options_fields.py` (the panel is derived; the master's extra v3 columns are harmless to v2 readers — never delete a master).
- Engine: `git revert` of the Part B commits; nothing live depends on it.
```

- [ ] **Step 4: Changelog entry**

Add at the top of "Recent Changes" in `docs/archive/changelog.md`:

`- **2026-09-06: options surface v3 (F6 model-free implied variance + F7 risk-neutral density) and synthetic options engine upgrades (F5) — branch `worktree-options-v3`.** Spec `docs/specs/2026-09-06-options-mfiv-rnd-synthetic-engine-spec.md`, plan `docs/superpowers/plans/2026-09-06-options-mfiv-rnd-synthetic-engine.md`. Part A: `strategies.options_surface` computes, per fitted expiry, the DDKZ/VIX variance integral, the BKM (2003) risk-neutral skewness/kurtosis and ±10 % tail digitals on the existing PCHIP smile (flat wings, ±5σ√T, 401 points); constant-maturity `mfiv_30d/90d`, `mf_tail_premium_30d`, and `rn_skew_30d/rn_kurt_30d/rn_p_dn10_30d/rn_p_up10_30d` from the expiry nearest 30 DTE (±15) — seven new `SCALAR_KEYS`, `OPTIONS_FEATURES_VERSION = 3`, flowing unchanged through the builder, the panel, the live v2 dict and `aux_data_loader.FIELDS`; every v2 value frozen by `tests/strategies/test_options_surface_v2_freeze.py`; shadow line gains `mfiv_nonnull/rn_nonnull`. Part B: `backtest/dividends.py` (trailing-year cash yield, pre-coverage backfill), `options_pricing` gains `q` (py_vollib BSM; q = 0 path bit-identical), a CRR American tree (ruling G7) and `price/delta` dispatchers; `vol_index.vix_term_point` (VIX9D↔VIX in total variance); `synthetic_iv_detail` tiers surface → vix_term → realized with dte-aware points; `OptionSpec.exercise = 'american'`; the engine prices every contract with q, exercise and its remaining-life IV and logs one `[options_backtest] iv sources:` line. Rollout: `scripts/rollout_surface_v3.sh` (waits for fleet children; rebuild 2026-06-29..today; panel; verification thresholds) + runbook `docs/runbooks/2026-09-06-options-surface-v3-rollout.md`. Flags untouched; no re-backtest owed (no consumer reads a v3 key).`

- [ ] **Step 5: Commit**

```bash
git add scripts/rollout_surface_v3.sh docs/runbooks/2026-09-06-options-surface-v3-rollout.md docs/archive/changelog.md
git commit -m "ops(options): surface v3 rollout script + runbook + changelog (spec 2026-09-06 §C)"
```

---

### Task 9: ATM-band fallback for thin chains (coverage amendment, spec §H)

**Why this task exists (added 2026-09-06 13:00 UTC by the controller, ruling in the ledger):** the v2 rollout on main measured `iv30` non-null for only **30.5 %** of liquid-tier tickers on 2026-09-04 (1,178 of 3,861) against **100 %** in the v1 panel. v2's smile gate (≥ 5 IV-bearing strikes on both sides of spot per expiry) plus the 30-day bracket (or a one-sided expiry within ±10 d) drops every thin chain that v1 covered with a single ATM strike. Four live strategies read `iv30`/`iv_rank`/`vrp`; their candidate sets would shrink ~3× the day the flag flips. This task restores v1-style coverage WITHOUT touching any value v2 already computes: an expiry that cannot carry a smile gets a v1-style ATM-band point (|Δ| .40–.60 mean IV, exactly v1's `iv_front` definition per expiry) that participates in the constant-maturity interpolation, flagged by a new `iv30_source` key; the one-sided tolerance widens from 10 to 20 days so a lone 14-day or 42-day monthly can anchor the 30-day point.

**Files:**
- Modify: `src/strategies/options_surface.py` (`CM_ONE_SIDED_TOL`, `SmileFit.source`, new `atm_band_fit`, the fit loop in `features_for_day`, two new `SCALAR_KEYS`)
- Modify: `src/strategies/aux_data_loader.py` (`FIELDS` + 2)
- Modify: `tests/strategies/test_options_surface_parity.py` (`SHARED` + 2)
- Modify: `scripts/rollout_surface_v3.sh` (verification prints the `iv30` coverage and source split)
- Modify: `docs/runbooks/2026-09-06-options-surface-v3-rollout.md` (one paragraph)
- Create: `tests/strategies/test_options_surface_atm_band.py`

**Interfaces:**
- Consumes: `fit_smile`, `SmileFit`, `constant_maturity`, `_empty_row`, `features_for_day` (Tasks 2–3), `ATM_DELTA`, `IV_MIN` (existing).
- Produces: `atm_band_fit(exp_rows, dte) -> SmileFit | None`; `SmileFit.source: str = 'smile'` (`'smile' | 'atm_band'`); row keys `iv30_source` (`'smile' | 'atm_band' | None`) and `n_expiries_atm` (int); `CM_ONE_SIDED_TOL = 20`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/strategies/test_options_surface_atm_band.py
"""Spec 2026-09-06 §H: a chain too thin for a smile still yields a 30-day ATM
point from the |Δ| .40–.60 band (v1's definition), flagged by iv30_source; a
chain with smiles is untouched (the v2 freeze test guards SPY/AAPL/XOM)."""
from __future__ import annotations
import math
import numpy as np
import pandas as pd
import pytest

from strategies import options_surface as osf


def _rows(dte, strikes, spot=100.0, as_of='2026-09-03', iv=0.30):
    """One expiry; every strike carries a CALL and a PUT with a BS-ish delta."""
    t = dte / 365
    exp = (pd.Timestamp(as_of) + pd.Timedelta(days=dte)).date()
    out = []
    for K in strikes:
        d1 = (math.log(spot / K) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
        from scipy.stats import norm
        dc = float(norm.cdf(d1))
        for flag, d in (('CALL', dc), ('PUT', dc - 1.0)):
            out.append({'ticker': 'THIN', 'date': as_of, 'expiry': exp, 'strike': float(K), 'option_type': flag,
                        'implied_volatility': iv, 'delta': d, 'gamma': 0.01, 'theta': -0.02, 'vega': 0.1, 'volume': 1.0})
    return out


def test_thin_chain_gets_atm_band_iv30_flagged():
    # 3 strikes per expiry: below MIN_STRIKES (5) ⇒ no smile; ATM strike 100 sits in the .40–.60 band.
    rows = _rows(14, [95.0, 100.0, 105.0], iv=0.30) + _rows(42, [95.0, 100.0, 105.0], iv=0.34)
    row = osf.features_for_day(pd.DataFrame(rows), 100.0, '2026-09-03')
    assert row['n_expiries_fit'] == 0 and row['n_expiries_atm'] == 2
    assert row['iv30_source'] == 'atm_band'
    assert row['iv30'] is not None and 0.30 < row['iv30'] < 0.34          # bracketed 14 d ↔ 42 d in total variance
    assert row['iv_25d_put_30d'] is None and row['skew_25d_30d'] is None    # smile-only keys stay None
    assert row['mfiv_30d'] is None and row['rn_skew_30d'] is None            # v3 keys need a smile
    assert row['n_strikes_30d'] >= 1


def test_one_sided_tolerance_is_twenty_days():
    assert osf.CM_ONE_SIDED_TOL == 20
    row = osf.features_for_day(pd.DataFrame(_rows(14, [95.0, 100.0, 105.0])), 100.0, '2026-09-03')
    assert row['iv30'] == pytest.approx(0.30) and row['iv30_source'] == 'atm_band'   # lone 14-day monthly anchors 30 d
    row2 = osf.features_for_day(pd.DataFrame(_rows(60, [95.0, 100.0, 105.0])), 100.0, '2026-09-03')
    assert row2['iv30'] is None and row2['iv30_source'] is None                   # 30 d away: still None


def test_band_never_fabricates_when_no_atm_rows():
    rows = _rows(14, [80.0, 120.0])                                             # deltas far outside .40–.60
    row = osf.features_for_day(pd.DataFrame(rows), 100.0, '2026-09-03')
    assert row['iv30'] is None and row['iv30_source'] is None and row['n_expiries_atm'] == 0


def test_smile_expiry_keeps_smile_source_and_band_fills_gaps():
    K = 100.0 * np.exp(np.linspace(-0.3, 0.3, 25))
    rich = _rows(42, list(K), iv=0.25)                                           # smile-capable expiry
    thin = _rows(14, [95.0, 100.0, 105.0], iv=0.30)                              # band-only expiry
    row = osf.features_for_day(pd.DataFrame(rich + thin), 100.0, '2026-09-03')
    assert row['n_expiries_fit'] == 1 and row['n_expiries_atm'] == 1
    assert row['iv30_source'] == 'atm_band'                                      # nearest-30 expiry is the 14-day band point
    assert row['iv30'] is not None and row['iv90'] is None or row['iv90'] is None or row['iv90'] > 0


def test_new_keys_registered():
    from strategies.aux_data_loader import FIELDS
    for k in ('iv30_source', 'n_expiries_atm'):
        assert k in osf.SCALAR_KEYS and k in FIELDS
    empty = osf.features_for_day(pd.DataFrame(), 100.0, '2026-09-03')
    assert empty['iv30_source'] is None and empty['n_expiries_atm'] == 0
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m pytest tests/strategies/test_options_surface_atm_band.py -q`
Expected: failures (`KeyError: 'n_expiries_atm'`, `CM_ONE_SIDED_TOL == 10`).

- [ ] **Step 3: Implement**

`src/strategies/options_surface.py`:
- `CM_ONE_SIDED_TOL = 20   # was 10; amendment 2026-09-06 §H — a lone 14 d / 42 d monthly anchors the 30 d point`
- Append to `SmileFit` (after `rn_p_up10`): `source: str = 'smile'   # 'smile' | 'atm_band' (amendment §H)`
- Append to `SCALAR_KEYS`: `'iv30_source', 'n_expiries_atm',` under a comment `# amendment 2026-09-06 §H: thin-chain fallback`.
- In `_empty_row`, add `'n_expiries_atm': 0` to the `row.update({...})` dict (`iv30_source` is already `None` from the SCALAR_KEYS seed).
- New function, placed right after `fit_smile`:

```python
ATM_BAND_MIN_ROWS = 1


def atm_band_fit(exp_rows: pd.DataFrame, dte: int) -> SmileFit | None:
    """Fallback per-expiry point when no smile can be fitted (amendment 2026-09-06 §H):
    the |Δ| .40–.60 band mean IV — v1's `iv_front` definition — carrying only
    `atm_iv`; every smile-only field is None and `source == 'atm_band'`.
    Never a fabricated 0: no band row ⇒ None."""
    if 'delta' not in exp_rows.columns:
        return None
    iv = pd.to_numeric(exp_rows['implied_volatility'], errors='coerce')
    d = pd.to_numeric(exp_rows['delta'], errors='coerce').abs()
    band = iv[(d >= ATM_DELTA[0]) & (d <= ATM_DELTA[1]) & (iv > IV_MIN)]
    if len(band) < ATM_BAND_MIN_ROWS:
        return None
    atm = float(band.mean())
    if not (atm > 0):
        return None
    return SmileFit(dte=int(dte), t=dte / 365.0, atm_iv=atm, iv_25d_put=None, iv_25d_call=None,
                    n_strikes=int(len(band)), k_min=0.0, k_max=0.0, source='atm_band')
```
- In `features_for_day`, replace the fit loop and the two counters:

```python
    fits: dict[int, SmileFit] = {}
    n_smile = n_atm = 0
    for dte, exp_rows in ch[(ch['dte'] >= FIT_DTE[0]) & (ch['dte'] <= FIT_DTE[1])].groupby('dte'):
        side = _otm_side(exp_rows, spot_f)
        fit = fit_smile(side['strike'].to_numpy(), side['iv'].to_numpy(), spot_f, int(dte))
        if fit is not None:
            n_smile += 1
        else:
            fit = atm_band_fit(exp_rows, int(dte))       # amendment §H: smile first, band fills the gap
            if fit is not None:
                n_atm += 1
        if fit is not None:
            fits[int(dte)] = fit
    row['n_expiries_fit'] = n_smile
    row['n_expiries_atm'] = n_atm
    if not fits:
        return row
```
and, right after `near30 = min(fits, key=lambda d: abs(d - 30))`, add `row['iv30_source'] = fits[near30].source if row.get('iv30') is None else None` — NO: set it after the `row.update({...})` that assigns `iv30`: `row['iv30_source'] = fits[near30].source if row['iv30'] is not None else None`. `n_expiries_fit` keeps its v2 meaning (smile fits only) so the freeze test still holds; the v3 block (Task 3) is unchanged — `constant_maturity(fits, 30, 'mfiv')` skips band fits automatically because their `mfiv` is None, and the RN keys read `fits[near30].rn_*`, which are None on a band fit.

`src/strategies/aux_data_loader.py`: add `'iv30_source', 'n_expiries_atm',` to `FIELDS` after the v3 block.
`tests/strategies/test_options_surface_parity.py`: add `'iv30_source', 'n_expiries_atm'` to `SHARED`.
`scripts/rollout_surface_v3.sh`: in the verification python, extend `cols` with `'iv30_source'` and add, after the `print(f'surface rows=...')` line:
```python
src = df['iv30_source'].value_counts(dropna=False).to_dict()
print(f'iv30 nonnull={df.iv30.notna().mean()*100:.1f}% of {len(df):,} tickers; iv30_source={src}')
```
`docs/runbooks/2026-09-06-options-surface-v3-rollout.md`: add under "What changes when this merges": `- Amendment §H (coverage): expiries too thin for a smile now carry a v1-style |Δ| .40–.60 band ATM point into the 30/90-day interpolation (`iv30_source = 'atm_band'`, `n_expiries_atm`); one-sided tolerance 10 → 20 days. Expected `iv30` coverage on the liquid tier rises from 30 % toward v1's 100 %; smile-derived keys (25Δ, MFIV, RN) stay None on those names.`

- [ ] **Step 4: Run**

Run: `python3 -m pytest tests/strategies/test_options_surface_atm_band.py tests/strategies/test_options_surface_v2_freeze.py tests/strategies/test_options_surface_mfiv.py tests/strategies/test_options_surface.py tests/strategies/test_options_surface_series.py tests/strategies/test_options_surface_parity.py tests/scripts/test_build_options_surface.py tests/scripts/test_compute_rolling_from_surface.py tests/execution/test_engine_options_surface_shadow.py -q`
Expected: all pass. **If the freeze test fails** (a band fit changed a SPY/AAPL/XOM value), do not edit the snapshot: switch to the documented alternative — use band fits only when a ticker-day has NO smile fit at all (`if not fits: fits = band_fits`) — re-run, and say so in the report.

- [ ] **Step 5: Commit**

```bash
git add src/strategies/options_surface.py src/strategies/aux_data_loader.py tests/strategies/test_options_surface_parity.py tests/strategies/test_options_surface_atm_band.py scripts/rollout_surface_v3.sh docs/runbooks/2026-09-06-options-surface-v3-rollout.md
git commit -m "feat(options): ATM-band fallback for thin chains + 20-day one-sided tolerance — restores v1-style iv30 coverage, flagged by iv30_source (amendment 2026-09-06 §H)"
```

---

## Self-review (done while writing)

- **Spec coverage:** §H (coverage amendment, added 2026-09-06) → Task 9; A.1/A.2 → Task 2; A.3/A.4 → Task 3; A.5 (catalogue = runbook "What changes") → Task 8; B.1 → Task 4; B.2 → Task 5; B.3 → Task 6; B.4 → Task 7; C → Task 8; D → Tasks 1–7 tests; G rulings named in the code comments where they bite (G1 `_sigma_on`, G3 `_tail_prob_below`, G4 `features_for_day`, G6 `dividends`, G7 `american_price`, G8 parity-check docstring, G9 `OptionSpec`).
- **Type consistency:** `strip_features(smile, k_min, k_max, atm, t)` (Task 2) ← `fit_smile`; `SmileFit.mfiv/rn_*` (Task 2) → `constant_maturity(fits, 30, 'mfiv')` and `fits[near30].rn_*` (Task 3); `price/delta(flag, S, K, t, sigma, r=None, as_of=None, q=0.0, exercise=...)` (Task 5) ← Task 7; `synthetic_iv_detail(prices, vrp_factor, window, underlying, as_of, dte) -> (float, str)` (Task 6) ← Task 7 `_iv`; `dividend_yield_asof(ticker, as_of, spot, ref_spot=None)` + `backfill_reference_date()` (Task 4) ← Task 7 `_q`; `interp_total_variance(d1, v1, d2, v2, target)` (Task 6) ← `surface_iv`.
- **Placeholders:** the only "TO BE FILLED" is the runbook's results line, filled by the rollout run on main by design (same convention as the 2026-09-04 runbook).
