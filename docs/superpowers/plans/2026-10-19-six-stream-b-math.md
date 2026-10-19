# Six-repo Stream B — Math Fixes and Shadow QP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land spec §2 Stream B: exact NNLS tangency (B1), exact OU estimators (B2), a reproducible and unbiased intraday Monte Carlo (B3), an advisory breaker first-passage KPI (B4), convex helpers (B5) and a shadow QP sizer that never routes (B6).

**Architecture:** These are pure-math swaps inside existing modules, plus one new pure module per new capability (`src/lib/convex.py`, `src/execution/qp_shadow_sizer.py`). The live sizer gets three small edits: one B1 diagnostic token on the existing `corr_cumsharpe.live[...]` line, a `size_positions` wrapper and a hook just before `_emit_orders_from_targets` (B6). The QP shadow runs as a **hook in `_sharpe_cadence_path`**, not as a new orchestrator step. A step would need edits in three places (orchestrator `STEPS`, `daily-cycle.js`, `cron-schedule.js`, per repo CLAUDE.md). It would also need `h`, the caps and λ persisted, and nothing persists them today. The hook needs about 15 lines in `regime_blended_sizer.py`, and it reuses the existing `broker=` parameter of `_emit_orders_from_targets` so the broker is fetched only once.

**Tech Stack:** Python 3.13, numpy 2.4.4, scipy 1.15.3 (`optimize.nnls`, `linalg`), cvxpy 1.8.2 + clarabel 0.11.1 (already installed transitively; pinned in B5), pyarrow 23, pytest 9.

**Spec:** `docs/specs/2026-10-19-six-repo-adoptions-spec.md` §0, §2, §6, §7. Notes: `/root/.learnings/reviews/2026-10-19-six-repo-fit-review/NOTES-cvxpy.md` (F1, F2, F3, §4, §5) and `NOTES-diffrax.md` (F1–F4, §4).

## Global Constraints

Spec §0, verbatim:

- Master parquets and canonical Postgres tables are append-only (repo CLAUDE.md). New tables/columns only; never DELETE, never rewrite history. A one-off re-layout that preserves every row byte-for-byte (A1) is a re-write of the container, not of the data, and is allowed under R1.
- Backtest side is AUTHORITATIVE (2026-08-07 ruling). Anything that changes a backtest input (D1) lands flag-gated, default off, and flips only with a fleet epoch.
- Every new behaviour that can change sizing, signals or gate outcomes ships behind an `OPENCLAW_*` env flag, default = pre-change behaviour, byte-identical when unset. Pure bug fixes with no gated consumer (B2 OU estimator, B3 MC seed/bridge, A2 windowed reads, A4 doc/dormant) need no flag; the plan says which.
- Never read the whole `prices.parquet` or `options_eod.parquet` in a test or an implementer session. Tests use synthetic frames; parquet tests write a tiny file to `tmp_path`.
- Tests reach the REAL Postgres when `.env` is importable — stub every DB gate in fixtures (`monkeypatch` the connect function); fixture tickers must not be real symbols (`ZZTEST1`, not `AAA`).
- The box is 2-core / 8 GB / no swap and shared with the live pipeline. Implementers run single-file pytest only, `nice -n 19`, never the full suite, never while a fleet unit is active.
- `strategy_lint.ALLOWED_ROOTS` stays as is; new shared modules live under `src/strategies/` (already an allowed root) or `src/lib/`.
- Observability: every new gate/shadow emits one log line per cycle with a fixed prefix; a missing line means the process died, never "nothing to report". Never silent.
- Commit per task; edits, `git commit` and `git push` as separate tool calls (ERR-20261018-001).

Stream B flags and defaults:

| Item | Flag | Default (unset) | Notes |
|---|---|---|---|
| B1 | `OPENCLAW_TANGENCY_NNLS` | enumeration (today's code, byte-identical S_adj) | `=1` uses NNLS. B1 changes the live S_adj (exactly when n>10 or R is not PD, and at float level everywhere), so it is gated under §0 (D3, accepted). While the flag is OFF, every cycle also computes the NNLS value for each side, as a diagnostic only, and logs `tangency_nnls_maxdiff=` so the flip decision rests on evidence. When ON, the token reads `tangency_mode=nnls`. |
| B2 | none | — | Pure bug fix. Consumers: `pairs_scanner` ledger (X1 is PARKED) and the deprecated `S_pca_etf_stat_arb_reversion`. |
| B3 | `OPENCLAW_MC_BRIDGE` | bridge ON | Bug fix; `=0` is a kill switch. Seed derivation and the parquet read need no flag. |
| B4 | none | — | Advisory log token only; never gates. |
| B5 | none | — | No consumer; strategies are NOT edited. |
| B6 | `OPENCLAW_QP_SHADOW`, `OPENCLAW_QP_SHADOW_TAU` | off; τ = 0.0 | Shadow only (ruling R5); never routes. |

- **B6 ruling (D4, controller 2026-10-19):** the gross target is `G = min(λ_blended·NAV, Σcap_i)`, where `λ_blended` is the regime-blended `lam` the cascade actually scales gross by (`scale = lam·NAV/Σ|w|`, `regime_blended_sizer.py:1857`). Per-ticker caps use `lam_global`, exactly as the cascade computes them (`:1931`). The net constraint applies only when `_load_net_cap_cfg()` says the cascade's net cap is enabled, and then uses that cap's fraction. The extra tokens `gross_target= net_cap= tau=` follow the spec's fixed tokens.
- **B4 ruling (D5, controller 2026-10-19):** the token is labelled `p_trip_21d~=` (the tilde marks a total-equity σ proxy).
- Migration numbering: **165** (`165_mc_runs_seed.sql`). Stream A owns 164. `src/database/postgres.js:43` applies migrations idempotently at johnbot start, so the column exists before the :3000 MC route serves.
- Test commands run from the worktree root: `nice -n 19 python3 -m pytest <file>[::<test>] -v`. A task's final pass also adds `-W error`. Never run `tests/metrics/test_intraday_path_montecarlo.py` before Task 3 Step 1 has landed: today it reads the real masters.
- Never `git push`. The controller merges the stream.

## Spec deviations decided here (also in the hand-off report)

- **D1 (B3):** `random.Random((seed, trade_idx))` raises `TypeError` on Python 3.13 (verified: "The only supported seed types are: None, int, float, str, bytes, and bytearray"). The per-trade RNG is `random.Random(f"{seed}:{trade_idx}")` instead. String seeds are hashed with sha512, so they do not depend on PYTHONHASHSEED.
- **D2 (B3):** `run_path_mc` has no `proposal_id` parameter, but the spec's seed formula uses it. The plan adds `proposal_id: Optional[int] = None` and threads `args.proposal_id` from `main()`.
- **D3 (B1):** B1 is not on §0's no-flag list, yet the spec says `TANGENCY_MAX_ENUM` is "retired from the live path". The plan gates NNLS behind `OPENCLAW_TANGENCY_NNLS=1`, default enumeration. The live path is retired when the operator flips the flag.
- **D4 (B6):** Gross EQUALITY `sgnᵀx = λ·NAV` plus `|1ᵀx| ≤ 0.6·λ·NAV` plus the sign lock is infeasible for every one-sided book. Today's book is about 96 % SPY plus all-long alpha. Equality is also infeasible whenever Σcap_i < λ·NAV, which is the few-survivor day the caps exist for. The plan uses `G = min(λ·NAV, Σ cap_i)`. The net constraint is applied only when the cascade's own net cap is enabled (`_load_net_cap_cfg()`; it is OFF when absent), with the cascade's `max_net_frac`. Gross uses the blended `lam` (the cascade's gross scaler, `scale = lam·NAV/Σ|w|`, `regime_blended_sizer.py:1857`). Caps use `lam_global` (`:1931`). The line appends `gross_target= net_cap= tau=` after the spec's fixed tokens.
- **D5 (B4):** `nav_ohlc_path()` holds **total-account equity** candles (`logs/pnl_daily_ohlc.json`; 45 days on file on 2026-10-19), not alpha NAV. σ therefore includes the SPY sleeve, which overstates alpha-P&L volatility. It is implemented as written and documented as a proxy. The spec signature `p_trip_within(days=21)` has no drawdown input, so the plan uses `p_trip_within(dd, days=21, *, path=None)` with `dd` = the alpha `dd_pnl` the drawdown rule reads.
- **D6 (B2):** In `S_pca_etf_stat_arb_reversion`, `b` is the Δ-regression slope (≈ −κ), not the AR coefficient. The spec's `kappa = -ln(b)` read literally gives the log of a negative number. The plan uses `phi = 1 + b`, `kappa = -ln(phi)` (mask `0 < phi < 1`), `sigma_eq = sigma/sqrt(1-phi²)` and `m = a/(1-phi)`, which is numerically today's `a/(-b)`.
- **D7 (B5):** The notes' EVaR form `t·LSE(L/t) − t·log(1−z)` uses log-SUM. The strategy (`S_evar_tempered_stable_sector_etf.py:46`) uses log-MEAN. The helper uses log-mean, so it carries the extra `T` factor in the exponential-cone constraint.

## Review Focus

1. **QP flag on must not change a single emitted order.** The broker pre-fetch path must produce exactly the order list of the flag-off path. Pinned: Task 9 `test_qp_flag_on_orders_identical_to_flag_off` (EOD lane) and `test_qp_shadow_line_intraday_lane`.
2. **A one-sided book with the net cap enabled must log `status=infeasible` and never raise.** This is the realistic production book. Pinned: Task 8 `test_net_cap_one_sided_book_reports_infeasible`.
3. **Every sizer exit path needs a line when the flag is on.** That includes early returns: empty signals, the acting gate, gross ≤ 0, the thin-set abort and the flatten path. Pinned: Task 9 `test_qp_skipped_line_on_early_return`.
4. **Generators without `bar_sigma` (test stubs, NaN generators) and σ = 0 must skip the bridge, not crash.** Pinned: Task 5 `test_bridge_skipped_without_bar_sigma`.
5. **Breaker tick with fewer than 20 NAV points, an unreadable OHLC file, σ = 0 or `dd=None`.** The line must render `p_trip_21d~=n/a` and the tick must never raise. Pinned: Task 6 `test_p_trip_degrades_to_none`.

---

### Task 1: B1 — NNLS tangency behind `OPENCLAW_TANGENCY_NNLS`

**Files:**
- Modify: `src/execution/orthogonalization.py:66-69` (constants), `:155-216` (`_tangency_side_sharpe`)
- Modify: `src/execution/regime_blended_sizer.py:401-430` (`_corr_adjusted_maps` gains a `diag` kwarg), `:1673-1674` (pass a cycle dict), `:1696-1703` (append the token to the existing `corr_cumsharpe.live[%s]` line, which is built in the same function, so no new line is needed)
- Test: `tests/execution/test_sizer_per_ticker_cap.py` (one sizer-line test via `_run_eod`)
- Create: `tests/execution/test_tangency_nnls.py`
- Unchanged and must stay green: `tests/execution/test_tangency_sadj.py`

**Interfaces:**
- Produces:
  - `tangency_nnls_enabled() -> bool` returns `os.environ.get('OPENCLAW_TANGENCY_NNLS') == '1'`, read at call time.
  - `_tangency_R(sids: list[str], sim: dict, eff_shrink: float) -> np.ndarray` builds the shrunk R. The body is today's `:190-198` lifted verbatim (SPARSE_DEFAULT fallback, `(1-γ)R + γI`).
  - `_tangency_side_sharpe_enum(sids, s, sim, shrink, gamma=None) -> tuple[float, bool]` is today's function body unchanged, including the `TANGENCY_MAX_ENUM` truncation. It now calls `_tangency_R`.
  - `_tangency_side_sharpe_nnls(sids, s, sim, shrink, gamma=None) -> tuple[float, bool]`.
  - `_tangency_side_sharpe(...)` keeps its signature and dispatches on `tangency_nnls_enabled()`.
  - `tangency_net_sharpe(contribs_by_ticker, sim, weight_by_strat, shrink=..., gamma=None, diag: dict | None = None)`. The return value is unchanged and byte-identical whether or not `diag` is passed.
    - When `diag` is a dict and the flag is OFF: after each side's enumeration result, it also computes `_tangency_side_sharpe_nnls` for the same side and sets `diag['maxdiff'] = max(diag.get('maxdiff', 0.0), abs(enum - nnls))` and `diag['n_sides'] = diag.get('n_sides', 0) + 1`. This applies only to sides with at least 2 contributors; singletons are trivially equal and are not counted. The NNLS call sits in its own `try`; on exception it sets `diag['error'] = type(e).__name__` and S_adj is unaffected.
    - When the flag is ON it sets `diag['mode'] = 'nnls'`.
  - `tangency_diag_token(diag: dict) -> str` renders:
    - `tangency_mode=nnls` when `diag.get('mode') == 'nnls'`;
    - else `tangency_nnls_maxdiff=error:<cls>` when an error was recorded;
    - else `tangency_nnls_maxdiff=n/a` when `n_sides == 0`;
    - else `tangency_nnls_maxdiff=%.3g`.
  - In the sizer: `_corr_adjusted_maps(..., gamma=None, diag=None)` forwards `diag` to both `tangency_net_sharpe` calls (the legacy branch ignores it). Its 4-tuple return is unchanged; `test_tangency_sadj.py` unpacks it.

- [ ] **Step 1: Write the failing tests** in `tests/execution/test_tangency_nnls.py`

```python
import importlib, importlib.util, math
from pathlib import Path
import numpy as np, pytest
og = importlib.import_module("execution.orthogonalization")

def _pd_sim(rng, n):
    A = rng.standard_normal((n, n)); C = A @ A.T
    d = np.sqrt(np.diag(C)); C = C / np.outer(d, d)
    ids = [f"s{i}" for i in range(n)]
    return ids, {ids[i]: {ids[j]: float(C[i, j]) for j in range(n) if j != i} for i in range(n)}

def test_nnls_matches_enum_oracle():
    rng = np.random.default_rng(3)
    for _ in range(300):
        n = int(rng.integers(2, 9)); ids, sim = _pd_sim(rng, n)
        s = [float(v) for v in rng.uniform(0.05, 2.0, n)]
        e, de = og._tangency_side_sharpe_enum(ids, s, sim, 0.10)
        q, dq = og._tangency_side_sharpe_nnls(ids, s, sim, 0.10)
        assert abs(e - q) < 1e-9 and not de and not dq

def test_nnls_removes_top10_truncation():
    ids = [f"s{i}" for i in range(25)]
    sim = {a: {b: 0.0 for b in ids if b != a} for a in ids}
    q, _ = og._tangency_side_sharpe_nnls(ids, [1.0] * 25, sim, 0.0)
    e, _ = og._tangency_side_sharpe_enum(ids, [1.0] * 25, sim, 0.0)
    assert q == pytest.approx(5.0, rel=1e-9) and e == pytest.approx(math.sqrt(10), rel=1e-9)

def test_nnls_non_psd_fallback_floor_and_degraded():
    sim = {"a": {"b": 0.9, "c": -0.9}, "b": {"a": 0.9, "c": 0.9}, "c": {"a": -0.9, "b": 0.9}}
    q, deg = og._tangency_side_sharpe_nnls(["a", "b", "c"], [1.2, 1.1, 1.0], sim, 0.10)
    assert math.isfinite(q) and q >= 1.2 - 1e-9 and deg is True

def test_all_nonpositive_side_returns_max(): 
    q, _ = og._tangency_side_sharpe_nnls(["a", "b"], [-0.5, -0.2], {}, 0.10)
    assert q == pytest.approx(-0.2)

def test_dispatch_default_is_enum_byte_identical(monkeypatch):
    monkeypatch.delenv("OPENCLAW_TANGENCY_NNLS", raising=False)
    ids, sim = _pd_sim(np.random.default_rng(1), 6); s = [0.3, 1.1, 0.7, 0.9, 1.4, 0.2]
    assert og._tangency_side_sharpe(ids, s, sim, 0.10) == og._tangency_side_sharpe_enum(ids, s, sim, 0.10)
    monkeypatch.setenv("OPENCLAW_TANGENCY_NNLS", "1")
    assert og._tangency_side_sharpe(ids, s, sim, 0.10) == og._tangency_side_sharpe_nnls(ids, s, sim, 0.10)

_spec = importlib.util.spec_from_file_location(
    "_tangency_invariants", Path(__file__).with_name("test_tangency_sadj.py"))
_INV = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_INV)

@pytest.mark.parametrize("name", sorted(n for n in dir(_INV) if n.startswith("test_")))
def test_invariants_hold_under_nnls(name, monkeypatch):
    monkeypatch.setenv("OPENCLAW_TANGENCY_NNLS", "1")
    fn = getattr(_INV, name)
    fn(monkeypatch) if fn.__code__.co_argcount else fn()
```

Also add to `tests/execution/test_tangency_nnls.py`:

```python
def test_diag_maxdiff_tiny_and_sadj_unchanged(monkeypatch):
    monkeypatch.delenv("OPENCLAW_TANGENCY_NNLS", raising=False)
    w = {"a": 1.43, "b": 1.27, "c": 0.78, "d": 0.9}
    sim = {"a": {"b": 0.29, "c": 0.33, "d": 0.1}, "b": {"c": 0.58, "d": 0.2}, "c": {"d": 0.4}}
    contribs = {"ZZTEST1": [("a", 1), ("b", 1), ("c", 1)], "ZZTEST2": [("a", 1), ("d", -1), ("c", -1)]}
    diag = {}
    out_d, nb_d = og.tangency_net_sharpe(contribs, sim, w, diag=diag)
    assert (out_d, nb_d) == og.tangency_net_sharpe(contribs, sim, w)
    assert diag["n_sides"] == 2 and diag["maxdiff"] < 1e-9      # ZZTEST1 long (3), ZZTEST2 short (2)
    assert og.tangency_diag_token(diag).startswith("tangency_nnls_maxdiff=")
    assert float(og.tangency_diag_token(diag).split("=")[1]) < 1e-9

def test_diag_token_flag_on_and_error(monkeypatch):
    monkeypatch.setenv("OPENCLAW_TANGENCY_NNLS", "1")
    diag = {}
    og.tangency_net_sharpe({"ZZTEST1": [("a", 1), ("b", 1)]}, {}, {"a": 1.0, "b": 0.8}, diag=diag)
    assert og.tangency_diag_token(diag) == "tangency_mode=nnls"
    monkeypatch.delenv("OPENCLAW_TANGENCY_NNLS")
    monkeypatch.setattr(og, "_tangency_side_sharpe_nnls", lambda *a, **k: 1 / 0)
    diag = {}
    out, _ = og.tangency_net_sharpe({"ZZTEST1": [("a", 1), ("b", 1)]}, {}, {"a": 1.0, "b": 0.8}, diag=diag)
    assert og.tangency_diag_token(diag) == "tangency_nnls_maxdiff=error:ZeroDivisionError" and out["ZZTEST1"] > 1.0
    assert og.tangency_diag_token({}) == "tangency_nnls_maxdiff=n/a"
```

Add to `tests/execution/test_sizer_per_ticker_cap.py`: `test_corr_cumsharpe_line_carries_tangency_token(monkeypatch, caplog)`. It sets the caplog level to INFO for the sizer logger, then runs `_run_eod` with three `_carried('S_zz_i', 'ZZTEST1')` rows on ONE ticker, so there is a multi-member side. It asserts that exactly one record starting `corr_cumsharpe.live[` ends with a `tangency_nnls_maxdiff=<v>` token where `float(v) < 1e-9`. It then reruns with `OPENCLAW_TANGENCY_NNLS=1` and asserts the line ends with `tangency_mode=nnls`. Before running `_run_eod`, stub `_sizer._load_net_cap_cfg` to return `(False, 0.6)`, and stub `_sizer._post_corr_cumsharpe_log` to a no-op so nothing posts to Discord.

- [ ] **Step 2: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/execution/test_tangency_nnls.py -v`. Expected: FAIL with `AttributeError: ... '_tangency_side_sharpe_enum'`.

- [ ] **Step 3: Implement the four functions above in `orthogonalization.py`.** Keep `TANGENCY_MAX_ENUM = 10`, and update its comment to "enumeration path / test oracle only; the NNLS path has no cap". `_tangency_side_sharpe_nnls`:
  - `n == 0` returns `(0.0, False)`; `n == 1` returns `(float(s[0]), False)`.
  - Build `R = _tangency_R(sids, sim, eff_shrink)` (no truncation).
  - Try `L = np.linalg.cholesky(R)`. On `LinAlgError`, eigen-clip: take `w, V = np.linalg.eigh(R)`, clip with `w = np.maximum(w, 1e-8)`, rebuild `R = V @ np.diag(w) @ V.T`, rescale to a unit diagonal with `d = sqrt(diag(R))` and `R /= outer(d, d)`, then `L = cholesky(R)` and set `degraded = True`.
  - Solve `b = scipy.linalg.solve_triangular(L, s, lower=True)`, then `x, _ = scipy.optimize.nnls(L.T, b)`, then `q = float(s @ x)`.
  - Return `(max(max(s), math.sqrt(q)) if q > 1e-12 else max(s), degraded)`.
  - Update the docstrings of `_tangency_side_sharpe` and `tangency_net_sharpe` (`:229`) to name both paths and the flag.

- [ ] **Step 4: Wire the diagnostic into the sizer.**
  - At `:1673` create `_tan_diag: dict = {}` and pass `diag=_tan_diag` to `_corr_adjusted_maps`.
  - At `:1696-1703`, append ` %s` to the end of the `corr_cumsharpe.live[%s]: …` format string, with the argument `_og.tangency_diag_token(_tan_diag)` (import `execution.orthogonalization as _og` locally, as `_corr_adjusted_maps` does).
  - Both the line and the Discord post (`_post_corr_cumsharpe_log`) therefore carry the token once per cycle.
  - Under `OPENCLAW_TANGENCY_SADJ=0` the dict stays empty and the token reads `n/a`.
  - No test, script or parser matches the line's tail (grep `corr_cumsharpe.live` in `tests/` and `scripts/` before editing; if a parser exists, append after its last parsed token).

- [ ] **Step 5: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/execution/test_tangency_nnls.py tests/execution/test_tangency_sadj.py -v -W error`, then `nice -n 19 python3 -m pytest tests/execution/test_sizer_per_ticker_cap.py -v -W error`. Expected: all PASS, with `test_tangency_sadj.py` unchanged.

- [ ] **Step 6: Stage.** `git add src/execution/orthogonalization.py src/execution/regime_blended_sizer.py tests/execution/test_tangency_nnls.py tests/execution/test_sizer_per_ticker_cap.py`
- [ ] **Step 7: Commit.** `git commit -m "feat(sizer): NNLS tangency S_adj behind OPENCLAW_TANGENCY_NNLS (B1)"`

---

### Task 2: B2 — exact OU discretisation (no flag)

**Files:**
- Modify: `src/pipeline/pairs_scanner.py:204-219` (`ar1_half_life`)
- Modify: `src/strategies/implementations/S_pca_etf_stat_arb_reversion.py:85-104` (OU block), `:118` (skip test)
- Test: `tests/pipeline/test_pairs_scanner.py` (add two tests; the existing `test_half_life_ar1_within_15pct` and the explosive-series test stay unchanged)
- Create: `tests/strategies/test_pca_ou_exact.py`

**Interfaces:**
- Produces: `ar1_half_life(spread) -> float | None` (same signature). New module-level function in the PCA file: `_ou_fit_exact(X_w: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]`, which takes a (W, N) cumulative-residual window and returns per-column `(kappa_per_bar, m, sigma_eq)`, with NaN where `not 0 < phi < 1`.

- [ ] **Step 1: Write the failing tests.** Add to `tests/pipeline/test_pairs_scanner.py`:

```python
def test_half_life_exact_ou_bias_under_3pct_at_hl2():
    rng = np.random.default_rng(11); n = 50_000; phi = 2 ** -0.5     # true HL = 2.0 bars
    s = np.zeros(n)
    for t in range(1, n): s[t] = phi * s[t - 1] + rng.normal()
    hl = ps.ar1_half_life(s)
    assert abs(hl - 2.0) / 2.0 < 0.03
    coef = np.linalg.lstsq(np.column_stack([np.ones(n - 1), s[:-1]]), np.diff(s), rcond=None)[0][1]
    assert (math.log(2) / -coef - 2.0) / 2.0 > 0.10                  # old Euler form is biased ≈ +18 %

def test_half_life_none_when_phi_nonpositive():
    rng = np.random.default_rng(5); s = np.zeros(2000)
    for t in range(1, 2000): s[t] = -0.5 * s[t - 1] + rng.normal()
    assert ps.ar1_half_life(s) is None
```

Create `tests/strategies/test_pca_ou_exact.py`. It builds four OU columns of length 40 000 with `phi ∈ (0.5, 0.8, 0.9, 0.95)`, unit innovations and mean 0.3, Assertions, for each of the four columns:
  - `kappa == approx(-ln(phi), rel=0.05)`
  - `sigma_eq == approx(1/sqrt(1-phi²), rel=0.03)`
  - `abs(m - 0.3) < 0.1·sigma_eq`

In a separate call, an explosive series (`phi = 1.02`, shape `(300, 1)`, not padded: padding with a constant would make φ land in (0, 1)) must give NaN for both `kappa` and `sigma_eq`. Import with `importlib.import_module('strategies.implementations.S_pca_etf_stat_arb_reversion')._ou_fit_exact`.

- [ ] **Step 2: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/pipeline/test_pairs_scanner.py -k "exact_ou or phi_nonpositive" tests/strategies/test_pca_ou_exact.py -v`. Expected: the bias test fails at its first assertion (≈ 18 %), the phi test fails (returns ≈ 0.46), and the PCA test fails with `AttributeError: _ou_fit_exact`.

- [ ] **Step 3: Implement.**
  - `ar1_half_life`: compute `phi = 1.0 + float(coef[1])`, return `None` unless `0.0 < phi < 1.0`, otherwise return `-math.log(2.0) / math.log(phi)`. Rewrite the docstring to describe the exact discretisation and cite spec B2.
  - PCA: move today's vectorised OLS (`:85-101`) into `_ou_fit_exact`, then apply:
    - `phi = 1 + b`
    - `ok = (phi > 0) & (phi < 1)`
    - `kappa = where(ok, -log(phi), nan)`
    - `m = where(ok, a / (1 - phi), 0.0)`
    - `sigma = std of the OU residuals` (unchanged)
    - `sigma_eq = where(ok, sigma / sqrt(1 - phi²), nan)`
  
  Mask before calling `log`/`sqrt` so `-W error` sees no RuntimeWarning. `generate_signals` then calls it. Change `:118` from `kappa[idx] <= 0` to `not (kappa[idx] > 0)` so NaN is skipped explicitly.

- [ ] **Step 4: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/pipeline/test_pairs_scanner.py tests/strategies/test_pca_ou_exact.py -v -W error`. Expected: PASS.
- [ ] **Step 5: Stage.** `git add src/pipeline/pairs_scanner.py src/strategies/implementations/S_pca_etf_stat_arb_reversion.py tests/pipeline/test_pairs_scanner.py tests/strategies/test_pca_ou_exact.py`
- [ ] **Step 6: Commit.** `git commit -m "fix(ou): exact AR(1)->OU half-life and kappa/sigma_eq (B2)"`

---

### Task 3: B3a — filtered parquet read, hold-aware Sharpe, test-file hygiene (no flag)

**Files:**
- Modify: `src/metrics/intraday_path_montecarlo.py:121-132` (`GBMPathGen.from_parquet`), `:322-332` (bootstrap Sharpe)
- Modify: `tests/metrics/test_intraday_path_montecarlo.py` (autouse fixture; `SPY`/`AAPL` pools become `ZZTEST1`)

**Interfaces:**
- Produces: `GBMPathGen.from_parquet(ticker, window_days=90)` (same signature).

- [ ] **Step 1: Harden the existing test file first.** The file must never touch real masters. Today `test_path_mc_no_gen_fallthrough` reads the whole `prices.parquet` through `GBMPathGen.from_parquet`. Add an autouse fixture `_synthetic_masters(tmp_path, monkeypatch)` that:
  - writes `tmp_path/prices.parquet` with columns `ticker, date (str 'YYYY-MM-DD'), close`, containing 120 business days for `ZZTEST1` and `ZZTEST2` (seeded GBM closes);
  - sets `ipm._PRICES_DAILY_PATH` to that file and `ipm._PRICES_30M_PATH` to `tmp_path/missing_30m.parquet`;
  - clears `ipm._GEN_CACHE` before and after the test.
  
  Replace the `'SPY'` and `'AAPL'` pool tickers with `'ZZTEST1'`, so `test_run_path_mc_emits_hit_rates` and the linear-scaling test run on GBM instead of skipping.

- [ ] **Step 2: Write the failing tests** in the same file:

```python
def test_gbm_from_parquet_filters_to_one_ticker(monkeypatch):
    import pyarrow.parquet as pq
    calls, orig = [], pq.read_table
    monkeypatch.setattr(pq, "read_table", lambda *a, **k: calls.append(k) or orig(*a, **k))
    gen = ipm.GBMPathGen.from_parquet("ZZTEST1")
    assert calls and calls[0]["filters"] == [("ticker", "==", "ZZTEST1")]
    assert calls[0]["columns"] == ["ticker", "date", "close"]
    assert gen.sigma == pytest.approx(_expected_bar_sigma("ZZTEST1"), rel=1e-12)

def test_sharpe_annualised_by_hold_days(monkeypatch):
    class OneDrawGen:                      # consumes exactly one draw regardless of n_bars
        sigma = 0.01
        def sample_path(self, n_bars, rng): return [rng.gauss(0.001, 0.01)] + [0.0] * (n_bars - 1)
    monkeypatch.setattr(ipm, "_gen_for_ticker", lambda t, window_days=90: (OneDrawGen(), "gbm"))
    kw = dict(strategy_id="S_zz", regime_state="LOW_VOL", current_size=1.0, proposed_size=1.0,
              proposed_stop_pct=1.0, proposed_target_pct=1.0, n_iter=300,
              trade_pool=[{"ticker": "ZZTEST1", "direction": "LONG"}] * 20, seed=7)
    r1 = ipm.run_path_mc(proposed_max_hold_days=1, **kw)
    r4 = ipm.run_path_mc(proposed_max_hold_days=4, **kw)
    assert r4["sharpe_p50"] == pytest.approx(r1["sharpe_p50"] / 2.0, rel=1e-12)
```

`_expected_bar_sigma(t)` is a test helper. It recomputes the sample std of that ticker's daily log returns from the fixture's own frame and divides by `sqrt(13)`.

- [ ] **Step 3: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/metrics/test_intraday_path_montecarlo.py -v`. Expected: the two new tests FAIL. The filter test fails because `read_table` is never called; the Sharpe test fails because the ratio is 1.0, not 0.5.

- [ ] **Step 4: Implement.**
  - `from_parquet`: `df = pq.read_table(_PRICES_DAILY_PATH, columns=['ticker', 'date', 'close'], filters=[('ticker', '==', ticker)]).to_pandas()` (import `pyarrow.parquet as pq` locally). The rest is unchanged.
  - Sharpe: before the bootstrap loop, compute `ann = math.sqrt(TRADING_DAYS_PER_YEAR / max(int(proposed_max_hold_days), 1))`; then `:332` becomes `sharpes.append((m / sd) * ann if sd > 0 else 0.0)`.
  - Add a comment there that the linear MC's `_bootstrap_sharpe` (`regime_param_montecarlo.py:72`) annualises with `sqrt(252/n)`, so the dashboard's `sharpe_p50_delta` is not like-for-like. That module is out of scope.

- [ ] **Step 5: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/metrics/test_intraday_path_montecarlo.py -v -W error`. Expected: PASS with zero skips.
- [ ] **Step 6: Stage.** `git add src/metrics/intraday_path_montecarlo.py tests/metrics/test_intraday_path_montecarlo.py`
- [ ] **Step 7: Commit.** `git commit -m "fix(mc): ticker-filtered GBM parquet read, hold-aware MC Sharpe, synthetic-only tests (B3)"`

---

### Task 4: B3b — derived seed, common random numbers, migration 165 (no flag)

**Files:**
- Create: `src/database/migrations/165_mc_runs_seed.sql`
- Modify: `src/metrics/intraday_path_montecarlo.py` (`run_path_mc` :257-324, `persist_run` :371-405, `main` :423-430)
- Modify: `src/metrics/regime_param_montecarlo.py` (`bootstrap_size_scalar` :153-193)
- Create: `tests/metrics/test_mc_seed_crn.py`, `tests/database/test_migration_165_mc_seed.py`

**Interfaces:**
- Consumes: Task 3's synthetic-masters fixture pattern (re-declare it locally; do not import test modules).
- Produces:
  - `derive_seed(strategy_id, regime_state, proposal_id, stop, target, max_hold) -> int` returns `zlib.crc32(f"{strategy_id}|{regime_state}|{proposal_id}|{stop}|{target}|{max_hold}".encode())`. This is the spec's exact format string.
  - `run_path_mc(..., seed=None, trade_pool=None, proposal_id: Optional[int] = None) -> dict`. The result gains a `'seed': int` key.
  - The per-iteration RNG is `random.Random(f"{seed}:{i}")`, used for the trade choice and then the path. The bootstrap RNG is `random.Random(f"{seed}:boot")`.
  - In `regime_param_montecarlo.bootstrap_size_scalar`, when `seed is None` it becomes `zlib.crc32(f"{strategy_id}|{regime_state}|{proposal_id}|{current_size}|{proposed_size}|{n_iter}|{window_days}".encode())`. The linear MC has no stop or target, so its key uses its own request fields. The result gains `'seed'`.

- [ ] **Step 1: Write the failing tests.** Create `tests/database/test_migration_165_mc_seed.py`, following `tests/database/test_stream_b_migration_shape.py`. It asserts that the text contains `ALTER TABLE strategy_regime_intraday_mc_runs` and `ALTER TABLE strategy_regime_mc_runs`, each with `ADD COLUMN IF NOT EXISTS seed BIGINT`, and that `re.search(r'\b(DELETE|DROP|TRUNCATE|UPDATE|RENAME)\b', code, re.I)` is None, where `code` is the SQL with `--` comments stripped (`re.sub(r'--[^\n]*', '', sql)`), so header prose cannot fail it.

Create `tests/metrics/test_mc_seed_crn.py`. All of its tests stub `_gen_for_ticker` with a recording GBM-like generator (`sigma=0.005`, `sample_path` draws `rng.gauss`) and use pools of 20 `ZZTEST1` rows.
  - `test_default_seed_reproducible`: two `seed=None` runs with identical args return identical dicts, and `r["seed"] == ipm.derive_seed("S_zz", "LOW_VOL", 9, 0.02, 0.05, 5)` (with `proposal_id=9`).
  - `test_seed_depends_on_proposal`: `derive_seed(..., 1, ...) != derive_seed(..., 2, ...)`.
  - `test_crn_prefix_stable_across_n_iter`: the generator records each path it emits. A run with `n_iter=50` and a run with `n_iter=100` at `seed=123` produce identical first 50 recorded paths.
  - `test_persist_run_writes_seed`: monkeypatch `ipm._connect` with a fake connection/cursor that captures `(sql, params)`. Assert `"seed"` appears in the SQL column list and `params[-2] == result["seed"]`; the column order is `..., seed, proposal_id`.
  - `test_linear_mc_persists_derived_seed`: monkeypatch `rpm._load_realized_pnls` (returns 30 floats) and `rpm._connect` (fake). Call `bootstrap_size_scalar(..., seed=None, proposal_id=4)`. Assert `isinstance(result["seed"], int)`, that the seed is in the captured params, and that a second call returns the same `sharpe_p50`.

- [ ] **Step 2: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/metrics/test_mc_seed_crn.py tests/database/test_migration_165_mc_seed.py -v`. Expected: FAIL. `derive_seed` is missing and the migration file is not found.

- [ ] **Step 3: Implement.**
  - Write the migration. The header comment follows `163_*.sql`: additive only, nullable, no backfill. Add `COMMENT ON COLUMN` for both tables saying "RNG seed used for the run; derived from the request when not supplied (spec 2026-10-19 B3)".
  - Add `derive_seed` and change `run_path_mc` as in Interfaces. Persist `result.get('seed')`; it goes before `proposal_id` in both the column list and the params.
  - `main()` passes `proposal_id=args.proposal_id` into `run_path_mc`.
  - `regime_param_montecarlo`: add `seed` to its INSERT (before `proposal_id`) and to the returned dict.
  - `bootstrap_pnls` keeps its signature. The route `src/channels/api/routes_regime_2d.js:133-134` already forwards `--seed`/`--proposal-id`, so no JS change is needed.

- [ ] **Step 4: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/metrics/test_mc_seed_crn.py tests/database/test_migration_165_mc_seed.py tests/metrics/test_intraday_path_montecarlo.py tests/metrics/test_regime_param_montecarlo.py -v -W error`. Expected: PASS.
- [ ] **Step 5: Stage.** `git add src/database/migrations/165_mc_runs_seed.sql src/metrics/intraday_path_montecarlo.py src/metrics/regime_param_montecarlo.py tests/metrics/test_mc_seed_crn.py tests/database/test_migration_165_mc_seed.py`
- [ ] **Step 6: Commit.** `git commit -m "feat(mc): request-derived seeds, per-trade CRN streams, seed column (mig 165) (B3)"`

---

### Task 5: B3c — Brownian-bridge barrier crossing (`OPENCLAW_MC_BRIDGE`, default on)

**Files:**
- Modify: `src/metrics/intraday_path_montecarlo.py` (`EmpiricalPathGen.__init__` :70-71, `GBMPathGen` :117-119, `apply_policy` :184-222, call in `run_path_mc`)
- Create: `tests/metrics/test_mc_bridge.py`

**Interfaces:**
- Consumes: Task 4's per-iteration `rng_i`.
- Produces:
  - `GBMPathGen.bar_sigma` (property returning `self.sigma`). `EmpiricalPathGen.bar_sigma` is the sample stdev of its log returns (`statistics.stdev`, or 0.0 when there are fewer than 2), set in `__init__`.
  - `apply_policy(path, stop_pct, target_pct, max_hold_bars, direction='LONG', *, bridge: bool = True, sigma_bar: float | None = None, rng: random.Random | None = None) -> tuple[float, str, float]`. The bridge is active only when `bridge and rng is not None and sigma_bar and sigma_bar > 0`, so every existing positional caller is unchanged.

- [ ] **Step 1: Write the failing tests** in `tests/metrics/test_mc_bridge.py`:

```python
import math, random
from statistics import NormalDist
import pytest
from metrics import intraday_path_montecarlo as ipm
SIG, BARS, STOP = 0.0055, 65, 0.02
P_TRUE = 2 * NormalDist().cdf(math.log(1 - STOP) / (SIG * math.sqrt(BARS)))   # reflection, mu = 0

def _rate(bridge):
    rng, gen, hits = random.Random(12345), ipm.GBMPathGen(0.0, SIG), 0
    for _ in range(20_000):
        _, why, _ = ipm.apply_policy(gen.sample_path(BARS, rng), STOP, None, BARS,
                                     bridge=bridge, sigma_bar=SIG, rng=rng)
        hits += why == "stop"
    return hits / 20_000

def test_bridge_converges_to_reflection_first_passage():
    assert abs(_rate(True) - P_TRUE) < 0.015

def test_without_bridge_is_measurably_below():
    assert _rate(False) < P_TRUE - 0.03

class _U:                                   # fixed uniform
    def __init__(self, u): self.u = u
    def random(self): return self.u

def test_bridge_deterministic_long_stop():
    assert ipm.apply_policy([-0.019], 0.02, None, 1, sigma_bar=0.005, rng=_U(0.0))[:2] == (-0.02, "stop")
    assert ipm.apply_policy([-0.019], 0.02, None, 1, sigma_bar=0.005, rng=_U(0.99))[1] == "max_hold"

def test_bridge_short_stop_and_stop_wins_double_touch():
    assert ipm.apply_policy([0.019], 0.02, None, 1, "SHORT", sigma_bar=0.005, rng=_U(0.0))[1] == "stop"
    assert ipm.apply_policy([0.0], 0.001, 0.001, 1, sigma_bar=0.05, rng=_U(0.0))[1] == "stop"

def test_bridge_skipped_without_bar_sigma(monkeypatch):
    class NoSigma:
        def sample_path(self, n, rng): return [0.0] * n
    monkeypatch.setattr(ipm, "_gen_for_ticker", lambda t, window_days=90: (NoSigma(), "gbm"))
    r = ipm.run_path_mc("S_zz", "LOW_VOL", 1.0, 1.0, 0.02, 0.05, 1, n_iter=20,
                        trade_pool=[{"ticker": "ZZTEST1", "direction": "LONG"}] * 20, seed=1)
    assert r["status"] == "OK" and r["max_hold_hit_rate"] == 1.0
    assert ipm.apply_policy([0.0], 0.02, None, 1, sigma_bar=0.0, rng=_U(0.0))[1] == "max_hold"

def test_kill_switch_and_paths_identical(monkeypatch):
    seen = []
    orig = ipm.apply_policy
    monkeypatch.setattr(ipm, "apply_policy",
                        lambda p, *a, **k: seen.append((tuple(p), k["bridge"])) or orig(p, *a, **k))
    monkeypatch.setattr(ipm, "_gen_for_ticker", lambda t, window_days=90: (ipm.GBMPathGen(0.0, SIG), "gbm"))
    kw = dict(n_iter=30, trade_pool=[{"ticker": "ZZTEST1", "direction": "LONG"}] * 20, seed=5)
    ipm.run_path_mc("S_zz", "LOW_VOL", 1.0, 1.0, 0.02, 0.05, 2, **kw)
    monkeypatch.setenv("OPENCLAW_MC_BRIDGE", "0")
    ipm.run_path_mc("S_zz", "LOW_VOL", 1.0, 1.0, 0.02, 0.05, 2, **kw)
    on, off = seen[:30], seen[30:]
    assert all(b for _, b in on) and not any(b for _, b in off)
    assert [p for p, _ in on] == [p for p, _ in off]
```

- [ ] **Step 2: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/metrics/test_mc_bridge.py -v`. Expected: FAIL with `TypeError: apply_policy() got an unexpected keyword argument 'bridge'`.

- [ ] **Step 3: Implement `apply_policy`.** Keep the close checks exactly as they are. Track `prev = cum_log` before adding `r`. When the bridge is active and neither close check fired, test barriers in this order: **stop, then target** (the stop wins, matching `OPENCLAW_BT_DOUBLE_TOUCH` default `'stop'`, `unified_backtest.py:582`). Each barrier, in log space, with the distance `d` that must be positive on the safe side:

| | LONG barrier `b` | LONG `d` | SHORT barrier `b` | SHORT `d` |
|---|---|---|---|---|
| stop (requires `stop < 1`) | `ln(1 − stop)` | `x − b` | `ln(1 + stop)` | `b − x` |
| target | `ln(1 + target)` | `b − x` | `ln(1 − target)` (requires `target < 1`) | `x − b` |

If `d(prev) > 0 and d(cum_log) > 0`, draw `u = rng.random()`. If `u < exp(−2·d(prev)·d(cum_log)/sigma_bar²)`, return the barrier exit, exactly as today's close-hit return: `(−|stop|, 'stop', max_dd)` or `(|target|, 'target', max_dd)`.

In `run_path_mc`:
  - read `bridge = os.environ.get('OPENCLAW_MC_BRIDGE', '1') != '0'` once;
  - draw the full path first, then call `apply_policy(path, ..., direction=..., bridge=bridge, sigma_bar=getattr(gen, 'bar_sigma', None), rng=rng_i)`;
  - add `'bridge': bridge` to the OK result dict (JSON only; not persisted).

Update the module docstring with one paragraph on the discrete-monitoring bias and the bridge (NOTES-diffrax F1).

- [ ] **Step 4: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/metrics/test_mc_bridge.py tests/metrics/test_intraday_path_montecarlo.py tests/metrics/test_mc_seed_crn.py -v -W error`. Expected: PASS.
- [ ] **Step 5: Stage.** `git add src/metrics/intraday_path_montecarlo.py tests/metrics/test_mc_bridge.py`
- [ ] **Step 6: Commit.** `git commit -m "fix(mc): Brownian-bridge stop/target crossing in path MC, OPENCLAW_MC_BRIDGE kill switch (B3)"`

---

### Task 6: B4 — breaker first-passage KPI (advisory, no flag)

**Files:**
- Modify: `src/execution/account_breaker.py`: add the functions after `nav_ohlc_path` (:100-101); change `format_line` (:829-887); update the two call sites at `:2349-2352` and `:2481-2484`
- Create: `tests/execution/test_account_breaker_p_trip.py`

**Interfaces:**
- Produces:
  - `P_TRIP_HORIZON_DAYS = 21`, `P_TRIP_LOOKBACK = 60`, `P_TRIP_MIN_POINTS = 20`.
  - `nav_daily_sigma(path: Path | None = None, lookback: int = P_TRIP_LOOKBACK) -> float | None`. It reads `days[*].close` from the OHLC JSON (sorted by date key) and keeps the last `lookback + 1` closes. It returns `None` when there are fewer than `P_TRIP_MIN_POINTS` closes, when any close is ≤ 0 or non-finite, when σ is 0 or non-finite, or on any read or parse error. Otherwise it returns `statistics.stdev` of the simple returns. The docstring says this is a TOTAL-equity proxy (D5).
  - `p_trip_within(dd: float | None, days: int = P_TRIP_HORIZON_DAYS, *, path: Path | None = None) -> float | None`. With μ = 0, headroom `a = dd − DD_LIMIT` and `σ = nav_daily_sigma(path)`, it returns `2·Φ(−a/(σ·√days))` (with Φ via `math.erfc`). It returns `1.0` when `a ≤ 0` and `None` when `dd` or σ is None. It never raises: wrap the body and on exception log at debug and return `None`.
  - `format_line(..., pnl=None, p_trip=_NO_P_TRIP)`, where `_NO_P_TRIP = object()`. When `p_trip` is passed, append ` p_trip_21d~=<n/a | f'{p:.2f}'>` at the very END of the line, after the `| alpha_pnl=…` tail. When it is omitted the line is unchanged, so the existing `endswith(...)` assertions in `test_account_breaker_rebase.py:739-740`, `test_account_breaker_state.py:208` and `test_account_breaker_alpha_pnl.py:335` stay green.

- [ ] **Step 1: Write the failing tests:**

```python
import json, math, statistics
import pytest
from execution import account_breaker as ab

def test_reflection_values(monkeypatch):
    monkeypatch.setattr(ab, "nav_daily_sigma", lambda path=None, lookback=60: 0.01)
    assert ab.p_trip_within(-0.06) == pytest.approx(0.383, abs=0.005)    # 4 % headroom, 1 %/d
    assert ab.p_trip_within(-0.04) == pytest.approx(0.19, abs=0.005)     # 6 % headroom
    assert ab.p_trip_within(-0.10) == 1.0 and ab.p_trip_within(-0.12) == 1.0

def test_p_trip_degrades_to_none(monkeypatch, tmp_path):
    assert ab.p_trip_within(None) is None
    days = {f"2026-09-{d:02d}": {"close": 100.0} for d in range(1, 26)}       # zero variance
    (tmp_path / "flat.json").write_text(json.dumps({"days": days}))
    assert ab.nav_daily_sigma(tmp_path / "flat.json") is None
    short = {f"2026-09-{d:02d}": {"close": 100.0 + d} for d in range(1, 20)}  # 19 points
    (tmp_path / "short.json").write_text(json.dumps({"days": short}))
    assert ab.nav_daily_sigma(tmp_path / "short.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert ab.nav_daily_sigma(tmp_path / "bad.json") is None
    assert ab.p_trip_within(-0.05, path=tmp_path / "bad.json") is None

def test_sigma_uses_last_60_returns(tmp_path):
    closes = [100.0 * (1.0 + 0.003 * ((i * 7) % 5 - 2)) ** 1 + i for i in range(80)]
    days = {f"2026-{7 + i // 28:02d}-{i % 28 + 1:02d}": {"close": c} for i, c in enumerate(closes)}
    (tmp_path / "n.json").write_text(json.dumps({"days": days}))
    tail = closes[-61:]
    expect = statistics.stdev([b / a - 1 for a, b in zip(tail, tail[1:])])
    assert ab.nav_daily_sigma(tmp_path / "n.json") == pytest.approx(expect, rel=1e-12)

def test_format_line_token_only_when_passed():
    st = {"dd": -0.05, "daily": None, "rule": "none", "breach": False, "peak": 1.0}
    kw = dict(equity=1.0, bench_mv=0.0, alpha=1.0, st=st, open_equity=1.0, open_src="stored", halted=False)
    assert "p_trip" not in ab.format_line("shadow", **kw)
    assert ab.format_line("shadow", p_trip=None, **kw).endswith(" p_trip_21d~=n/a")
    assert ab.format_line("shadow", p_trip=0.3812, **kw).endswith(" p_trip_21d~=0.38")

def test_both_run_once_lines_pass_p_trip():
    import inspect
    assert inspect.getsource(ab.run_once).count("p_trip=p_trip_within(") == 2
```

- [ ] **Step 2: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/execution/test_account_breaker_p_trip.py -v`. Expected: FAIL with `AttributeError: p_trip_within`.
- [ ] **Step 3: Implement as in Interfaces.** The halted-path call site (`:2349`) passes `p_trip=p_trip_within((pnl or {}).get('dd_pnl'))`. The normal call site (`:2481`) passes `p_trip=p_trip_within((line_pnl or {}).get('dd_pnl'))`. Add a docstring line to `format_line` naming the new tail token and that it is advisory, a GBM approximation and a total-equity σ proxy.
- [ ] **Step 4: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/execution/test_account_breaker_p_trip.py tests/execution/test_account_breaker_state.py tests/execution/test_account_breaker_rebase.py tests/execution/test_account_breaker_alpha_pnl.py -v -W error`. Expected: PASS.
- [ ] **Step 5: Stage.** `git add src/execution/account_breaker.py tests/execution/test_account_breaker_p_trip.py`
- [ ] **Step 6: Commit.** `git commit -m "feat(breaker): advisory p_trip_21d~ first-passage token (B4)"`

---

### Task 7: B5 — convex helpers in `src/lib/convex.py` and dependency pins (no flag)

**Files:**
- Create: `src/lib/convex.py`, `tests/lib/test_convex.py`
- Modify: `requirements.txt` (insert `clarabel==0.11.1` and `cvxpy==1.8.2` in alphabetical position, after `APScheduler==3.11.2`)
- Modify: `src/agent/prompts/subagents/strategycoder.md:114` (add the note immediately after the covariance rule, keeping that rule intact)
- NOT modified: `S_markov_frontier_regimes.py`, `S_evar_tempered_stable_sector_etf.py` (approved strategies; re-gate)

**Interfaces:**
- Produces (cvxpy is imported lazily inside each function; the import costs 0.77 s; `solver=cp.CLARABEL` is always passed explicitly):
  - `tangency_weights(mu: np.ndarray, Sigma: np.ndarray, long_only: bool = True) -> np.ndarray | None`.
    - With `long_only=True`, it solves `min yᵀΣy s.t. μᵀy = 1, y ≥ 0` (using `cp.quad_form(y, cp.psd_wrap(Sigma))`) and returns `p = y/1ᵀy`, after clipping values below 0 to 0 and renormalising. It returns `None` if every `mu ≤ 0` (infeasible; no solve) or if the status is not `optimal`/`optimal_inaccurate`.
    - With `long_only=False`, it returns the closed form `Σ⁻¹μ / 1ᵀΣ⁻¹μ`, or `None` when the denominator is below 1e-12 in absolute value.
  - `min_evar_weights(L: np.ndarray, z: float = 0.95) -> np.ndarray | None`. `L` is the T×n matrix of per-asset scenario LOSSES; the portfolio loss is `ℓ = L @ w`. Variables: `w ≥ 0, 1ᵀw = 1`, `eta`, `s`, `u ∈ ℝᵀ`. Constraints: `ExpCone(ℓ − eta, s·1, u)` and `Σu ≤ T·s`. Objective: minimise `eta − s·log(1 − z)`. This equals `inf_t (1/t)(log mean e^{tℓ} − log(1−z))` with `s = 1/t`, the log-mean of the strategy (D7). It returns `w` (clipped and renormalised), or `None` on a non-optimal status or a solver exception.

- [ ] **Step 1: Write the failing tests** in `tests/lib/test_convex.py`:
  - `test_tangency_uncorrelated_analytic`: `Sigma = diag([0.04, 0.09, 0.01])`, `mu = [0.05, 0.06, 0.02]`. Assert `p ≈ (mu/diag)/Σ(mu/diag)` with `atol=1e-6`.
  - `test_tangency_beats_or_ties_slsqp`: for 5 seeds (`default_rng(seed)`, n ∈ 4..8, `Sigma = A Aᵀ/n + 0.01·I`, `mu ~ N(0.02, 0.03)` with `mu[0] = abs(mu[0]) + 0.01`), assert that the Sharpe of the cvx result is at least the SLSQP Sharpe minus 1e-6. The SLSQP reference is the neg-Sharpe minimiser copied into the test from `S_markov_frontier_regimes.py:199-212` (`bounds (0, 1)`, sum = 1, `ftol 1e-9`).
  - `test_tangency_nonpositive_mu_returns_none`: `mu = [-0.01, 0.0]` returns `None`.
  - `test_tangency_unconstrained_closed_form`: `long_only=False` gives `allclose(p, solve(Sigma, mu)/sum(solve(Sigma, mu)))`.
  - `test_min_evar_beats_or_ties_slsqp`: `R = default_rng(9).normal(0.0005, 0.01, (250, 5))` and `L = -R`. Let `w_cvx = min_evar_weights(L)` and let `w_ref` be the SLSQP weights from an in-test copy of `_min_evar_weights` (`S_evar_tempered_stable_sector_etf.py:56-73`). Using an in-test oracle `_evar(losses, z)` (bounded `minimize_scalar` over `log t ∈ [−9, 9]`, log-mean form), assert `_evar(L@w_cvx) ≤ _evar(L@w_ref) + 1e-5`, `w_cvx ≥ −1e-9` and `w_cvx.sum() == approx(1)`.
  - `test_requirements_pin_cvxpy_and_clarabel`: `requirements.txt` lines include `cvxpy==1.8.2` and `clarabel==0.11.1`.
  - `test_helpers_lint_clean_for_strategies`: a strategy-shaped source string `from lib.convex import tangency_weights` passes the lint: `strategies.strategy_lint.lint_source(src) == []` (`strategy_lint.py:164`).

- [ ] **Step 2: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/lib/test_convex.py -v`. Expected: FAIL with `ModuleNotFoundError: lib.convex`.
- [ ] **Step 3: Implement `src/lib/convex.py`, the two pins and the StrategyCoder note.** The note text: "If a strategy needs a long-only tangency or minimum-EVaR weight vector, call `from lib.convex import tangency_weights, min_evar_weights`. Never import cvxpy in a strategy file. A `None` return means fall back (e.g. to equal weight). The 3×-observations rule above still applies; a convex solver does not fix an underdetermined Σ."
- [ ] **Step 4: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/lib/test_convex.py -v -W error`. Expected: PASS.
- [ ] **Step 5: Stage.** `git add src/lib/convex.py tests/lib/test_convex.py requirements.txt src/agent/prompts/subagents/strategycoder.md`
- [ ] **Step 6: Commit.** `git commit -m "feat(lib): convex tangency + min-EVaR helpers, pin cvxpy/clarabel (B5)"`

---

### Task 8: B6a — pure QP shadow module `src/execution/qp_shadow_sizer.py`

**Files:**
- Create: `src/execution/qp_shadow_sizer.py`, `tests/execution/test_qp_shadow_sizer.py`

**Interfaces:**
- Produces:
  - `FLAG = 'OPENCLAW_QP_SHADOW'`, `TAU_ENV = 'OPENCLAW_QP_SHADOW_TAU'`, `PREFIX = '[qp_shadow]'`.
  - `qp_shadow_enabled() -> bool` returns `os.environ.get(FLAG) == '1'`, read at call time.
  - `resolve_tau() -> float`: a float env value ≥ 0; unset, garbage or negative gives `0.0`.
  - `build_caps(target_usd: dict[str, float], *, nav: float, lam_global: float, gate_net_sharpe: dict[str, float], bench_tkrs: set[str], bench_exempt: set[str], bench_frac: float, cap_active: bool) -> dict[str, float]`. It mirrors the cascade (`regime_blended_sizer.py:1918-1937`, `:1972-1979`) for each ticker:
    - the per-ticker cap is `0.10·(|S|+1)·lam_global·nav` when `cap_active`, the ticker is not in `bench_exempt` and the gate value is present; otherwise it is `math.inf`;
    - a ticker in `bench_tkrs` additionally gets `min(cap, bench_frac·nav)`.
    
    Use `PER_TICKER_CAP_SHARPE_FRAC`, imported from the sizer module lazily inside the function to avoid a circular import.
  - `_build_problem(n: int, with_net: bool, tau: float) -> tuple[cp.Problem, dict[str, cp.Parameter], cp.Variable]`. It works in NAV units (`x̂ = x/NAV`), with Parameters `h, cur, cap (nonneg), sgn, G (nonneg)` and `netb (nonneg)` when `with_net`. τ is a Python constant (the third argument), not a Parameter, because a parameter multiplying a parameter-bearing term breaks DPP (NOTES-cvxpy §5). The formulation lifted from `qp2.py`:
    - objective `cp.sum_squares(x − h) + tau·cp.norm1(x − cur)`;
    - constraints `sgn @ x == G`, `cp.abs(x) <= cap`, `cp.multiply(sgn, x) >= 0`, plus `cp.sum(x) <= netb` and `cp.sum(x) >= −netb` when `with_net`.
  - `solve_qp(h, cur, cap, sgn, *, nav, gross_usd, net_frac: float | None, tau) -> tuple[np.ndarray | None, str, float]` returns USD `x`, a status and `solve_ms`. It calls `prob.solve(solver=cp.CLARABEL)`. Status mapping: `optimal` gives `ok`, `optimal_inaccurate` gives `ok_inaccurate`, `infeasible`/`infeasible_inaccurate` give `infeasible`, any other status gives the status string, and an exception gives `error:<ExcClassName>`. `netb = net_frac·gross_usd/nav`. Infinite caps are replaced by `gross_usd/nav`.
  - `run_qp_shadow(target_usd, broker, *, nav, lam_nav, caps, bench_tkrs, net_frac, tau) -> str`. It never raises. It keeps names with finite non-zero `h` and sets `sgn = sign(h)`, `cur = broker.get(t, 0.0)` and `G = min(lam_nav, Σ caps[t])` (D4). It solves, emits the line via `logger.info` and `lib.shadow_log.record('qp_shadow', line)`, marks the cycle emitted, and returns the line. Empty names give `status=skipped:no_targets`.
  - `format_line(*, n, l1_dist, gross_qp, gross_cascade, spy_qp, spy_cascade, solve_ms, status, gross_target, net_frac, tau) -> str` renders exactly `[qp_shadow] n=%d l1_dist=%s gross_qp=%s gross_cascade=%.0f spy_qp=%s spy_cascade=%.0f solve_ms=%.1f status=%s gross_target=%.0f net_cap=%s tau=%g`. The QP-derived fields render `%.0f`, or `n/a` when `x is None`; `net_cap` renders `off` or `%.2f`. `l1_dist = Σ|x−h|` over the solved names, `spy_* = Σ` over `bench_tkrs`.
  - `emit_status(status: str) -> str` emits a line with zeroed counts, `n/a` QP fields and the given status (used for `skipped:*` and `error:*`). Like `run_qp_shadow`, it **marks the cycle as emitted**, so `end_cycle` never adds a second line after an error line.
  - `begin_cycle() -> None` and `end_cycle() -> None`. `end_cycle` emits `emit_status('skipped:no_targets')` when the flag is on and nothing was emitted since `begin_cycle`.

- [ ] **Step 1: Write the failing tests** in `tests/execution/test_qp_shadow_sizer.py` (tickers `ZZTEST1..3`, bench `ZZBENCH`, `nav = 100_000`; `_isolate_shadow_log` from the conftest keeps logs in tmp):
  - `test_problem_is_dpp`: `_build_problem(5, True, 0.0)[0].is_dpp()`, and likewise with `tau=0.02`.
  - `test_redistributes_shaved_mass`: `h = {ZZTEST1: 30k, ZZTEST2: 20k}`, caps 60k each, `lam_nav = 100k`, net off, τ 0. The solution is `x ≈ (55k, 45k)` within $1, with `status=ok` and `gross_qp=100000`.
  - `test_caps_bind`: caps `(40k, 60k)` give `x ≈ (40k, 60k)`.
  - `test_gross_target_clipped_to_sum_caps`: caps `(30k, 30k)` give `gross_target=60000`, `status=ok` and `gross_qp=60000`.
  - `test_sign_lock`: `h = {ZZTEST1: 50k, ZZTEST2: −10k}` gives `x1 ≥ −1e-6` and `x2 ≤ 1e-6`.
  - `test_net_cap_one_sided_book_reports_infeasible`: an all-long `h` with `net_frac=0.6`. The returned line has `status=infeasible` and `gross_qp=n/a`, and nothing raises.
  - `test_net_cap_mixed_book_respected`: `h = {ZZTEST1: 60k, ZZTEST2: −40k}`, `net_frac=0.6`. Assert `|Σx| ≤ 0.6·G + 1`.
  - `test_bench_frac_cap_and_spy_tokens`: `build_caps` with `bench_tkrs = bench_exempt = {ZZBENCH}` and `bench_frac=0.5` gives a ZZBENCH cap of 50k. `run_qp_shadow` shows `spy_qp=` ≤ 50000 and `spy_cascade=` equal to h's bench value.
  - `test_build_caps_mirrors_cascade`: with `cap_active=False` the alpha cap is `inf`. With it True, the cap equals `0.10·(|S|+1)·lam_global·nav`. A missing gate value gives `inf`. A bench ticker that is not exempt gets `min(per-ticker, frac·nav)`.
  - `test_tau_cuts_turnover_keeps_gross`: with τ = 0.02, `Σ|x−cur|` is lower than with τ = 0, and `Σ|x| == approx(G, abs=1)` (NOTES-cvxpy F2(c)).
  - `test_solver_exception_reported`: monkeypatch `cvxpy.Problem.solve` to raise `cvxpy.error.SolverError`. The line ends with `status=error:SolverError` and `run_qp_shadow` returns normally.
  - `test_line_format_exact`: the regex `^\[qp_shadow\] n=\d+ l1_dist=\S+ gross_qp=\S+ gross_cascade=\S+ spy_qp=\S+ spy_cascade=\S+ solve_ms=\S+ status=\S+ gross_target=\S+ net_cap=\S+ tau=\S+$` matches.
  - `test_tau_env_parsing`: `''` and `'abc'` give 0.0, `'-1'` gives 0.0, `'0.02'` gives 0.02.
  - `test_end_cycle_emits_skipped_once`: with the flag on, `begin_cycle(); end_cycle()` logs exactly one `[qp_shadow]` record containing `status=skipped:no_targets`. `begin_cycle(); run_qp_shadow(...); end_cycle()` and `begin_cycle(); emit_status('error:X'); end_cycle()` each log exactly one. With the flag off, `end_cycle` logs nothing.

- [ ] **Step 2: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/execution/test_qp_shadow_sizer.py -v`. Expected: FAIL with `ModuleNotFoundError: execution.qp_shadow_sizer`.
- [ ] **Step 3: Implement the module as in Interfaces.** Write a module docstring citing ruling R5, the D4 resolution, and NOTES-cvxpy §5: always pass `solver=cp.CLARABEL`, never `ignore_dpp`, never let the QP choose direction, never route. Do not import cvxpy at module level.
- [ ] **Step 4: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/execution/test_qp_shadow_sizer.py -v -W error`. Expected: PASS.
- [ ] **Step 5: Stage.** `git add src/execution/qp_shadow_sizer.py tests/execution/test_qp_shadow_sizer.py`
- [ ] **Step 6: Commit.** `git commit -m "feat(sizer): shadow tracking-QP module (DPP, Clarabel) for the cap cascade (B6)"`

---

### Task 9: B6b — wire the QP shadow into the trade step (`OPENCLAW_QP_SHADOW`)

**Files:**
- Modify: `src/execution/regime_blended_sizer.py:126` (`size_positions`). Rename it to `_size_positions_impl` and add the wrapper.
- Modify: `src/execution/regime_blended_sizer.py:2022-2026` (hook plus `broker=` pass-through)
- Test: `tests/execution/test_sizer_per_ticker_cap.py`. Add `class TestQpShadowWiringEOD`, reusing `_run_eod`, `_carried`, `_weights_row`, `NAV` and `LAM` from that file. This is the production EOD/same-day lane (`OPENCLAW_EOD_RECONCILE=1`), which falls through to the `_sharpe_cadence_path` call at `:182`, and it has no thin-set gate.
- Test: `tests/execution/test_regime_blended_sizer.py`. Add one intraday-lane test, reusing `_run_intraday_path`, `_carried_cap` and `_weights_row_cap`. It needs **≥ 12 signals**: `_run_intraday_path` stubs the baseline to `len(signals)`, and `is_signal_set_thin` aborts below `SIGNAL_SET_MIN_FLOOR = 10` (`regime_blended_sizer.py:57`), which would return `[]` before the hook.

**Interfaces:**
- Consumes (Task 8): `qp_shadow_enabled`, `build_caps`, `run_qp_shadow`, `resolve_tau`, `emit_status`, `begin_cycle`, `end_cycle`.
- Produces:
  - `size_positions(*args, **kwargs)`, decorated with `functools.wraps(_size_positions_impl)`. It calls `_qps.begin_cycle()`, then `try: return _size_positions_impl(*args, **kwargs)`, then `finally: _qps.end_cycle()`. Every early return in the sizer (`:175`, the acting gate, gross ≤ 0, the thin-set abort, the flatten path) now still yields one line when the flag is on.
  - The hook, placed immediately before `_orders = _emit_orders_from_targets(` at `:2022`:

```python
_qp_broker = None
if _qps.qp_shadow_enabled():
    try:
        _qp_broker = _load_broker_positions_usd()
        _net_on, _net_frac = _load_net_cap_cfg()
        _qps.run_qp_shadow(
            target_usd, _qp_broker, nav=nav, lam_nav=lam * nav,
            caps=_qps.build_caps(
                target_usd, nav=nav, lam_global=lam_global, gate_net_sharpe=gate_net_sharpe,
                bench_tkrs=_bench_tkrs, bench_exempt=_bench_exempt,
                bench_frac=_bsz.benchmark_max_nav_frac(),
                cap_active=(os.environ.get('OPENCLAW_EOD_RECONCILE') == '1'
                            or os.environ.get('OPENCLAW_INTRADAY_REDEPLOY') == '1')),
            bench_tkrs=_bench_tkrs, net_frac=_net_frac if _net_on else None,
            tau=_qps.resolve_tau())
    except Exception as _e:          # shadow must never break sizing
        _qps.emit_status(f'error:{type(_e).__name__}')
```

  Then add `broker=_qp_broker` to the existing `_emit_orders_from_targets(...)` call. When the flag is off, `_qp_broker is None` and `_emit_orders_from_targets` fetches the broker itself, exactly as today. The `import execution.qp_shadow_sizer as _qps` goes at module top; it imports no cvxpy at import time.

- [ ] **Step 1: Write the failing tests.** Every test calls `caplog.set_level(logging.INFO, logger='execution.qp_shadow_sizer')`. It monkeypatches `_sizer._load_net_cap_cfg` to return `(False, 0.6)` and `execution.benchmark_sizing.benchmark_max_nav_frac` to return `1.0`, so no test reaches Postgres.

  In `TestQpShadowWiringEOD`, the carried rows are three `_carried('S_zz_i', 'ZZTEST<i>')` rows, with weights `_weights_row('S_zz_i', eff_sharpe=3.5)`:
  - `test_qp_flag_on_orders_identical_to_flag_off`: run `_run_eod` with the flag unset, then with `OPENCLAW_QP_SHADOW=1`.
    - Orders are non-empty and the two lists are equal.
    - With the flag on there is exactly one `[qp_shadow]` record, containing `n=3` and `status=ok`.
  - `test_qp_flag_off_emits_nothing_and_broker_untouched`: spy on `_sizer._emit_orders_from_targets` with a wrapper that records the `broker` kwarg and calls the original. With the flag off, `broker` is `None` and there is no `[qp_shadow]` record. With the flag on, `broker` is the stubbed dict.
  - `test_qp_skipped_line_on_early_return`: with the flag on, `_run_eod(monkeypatch, [], [])` returns `[]` and logs exactly one `[qp_shadow]` record containing `status=skipped:`.
  - `test_qp_error_never_breaks_sizing`: monkeypatch `_qps.build_caps` to raise `RuntimeError`. Orders equal the flag-off orders, and there is **exactly one** `[qp_shadow]` record, ending `status=error:RuntimeError`.

  In `test_regime_blended_sizer.py`:
  - `test_qp_shadow_line_intraday_lane`: 12 `_carried_cap('S_zz_i', 'ZZTEST<i>')` signals with `_weights_row_cap(..., eff_sharpe=3.5)`. With the flag on, orders are non-empty and equal the flag-off orders, and there is exactly one `[qp_shadow]` record, containing `n=12` and `status=ok`.

- [ ] **Step 2: Run to verify failure.** Run `nice -n 19 python3 -m pytest tests/execution/test_sizer_per_ticker_cap.py -k QpShadow tests/execution/test_regime_blended_sizer.py -k qp_shadow -v`. Expected: FAIL, because there is no `[qp_shadow]` record.
- [ ] **Step 3: Implement the wrapper and the hook exactly as in Interfaces.** Add a one-paragraph comment above the hook stating R5 (shadow, never routes), why it is a hook and not a step, and that `h` is the cascade's final `target_usd` as handed to `_emit_orders_from_targets`.
- [ ] **Step 4: Run to verify pass.** Run `nice -n 19 python3 -m pytest tests/execution/test_sizer_per_ticker_cap.py -v -W error`, then `nice -n 19 python3 -m pytest tests/execution/test_regime_blended_sizer.py -v -W error`, then `nice -n 19 python3 -m pytest tests/execution/test_tangency_sadj.py -v`. Expected: PASS.
- [ ] **Step 5: Add a changelog entry** at the top of `docs/archive/changelog.md` (newest first), in the file's existing format. It records:
  - the B1–B6 flags and defaults;
  - migration 165;
  - spec deviations D1–D7, one line each;
  - the live-watch item "first trade step after merge with `OPENCLAW_QP_SHADOW=1` shows one `[qp_shadow]` line; ≥ 2 weeks of lines before any promotion decision (R5)".
- [ ] **Step 6: Stage.** `git add src/execution/regime_blended_sizer.py tests/execution/test_sizer_per_ticker_cap.py tests/execution/test_regime_blended_sizer.py docs/archive/changelog.md`
- [ ] **Step 7: Commit.** `git commit -m "feat(sizer): wire [qp_shadow] line into the trade step behind OPENCLAW_QP_SHADOW (B6)"`
