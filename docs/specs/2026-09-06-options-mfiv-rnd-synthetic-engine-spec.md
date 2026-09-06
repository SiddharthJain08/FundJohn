# Options surface v3 (F6 model-free implied variance, F7 risk-neutral density) + synthetic options engine upgrades (F5) — design spec

**Status:** APPROVED in principle by the operator 2026-09-04 ("You may do the F5–7 upgrades afterwards") and re-confirmed 2026-09-06 ("continue with the fleet re-backtest as necessary as well as f5-f7 afterwards"). Design choices below are the controller's rulings (the operator is not in the loop in real time); each is listed in §G with its cost if wrong.
**Builds on:** `docs/specs/2026-09-04-options-surface-cboe-oi-rf-calendar-spec.md` (landed on main `92fc9d11`). Same invariants: ONE implementation feeds live and backtest; nothing is ever a fabricated 0; masters append-only; formulas only from financepy (GPL-3) — never source.
**Compute constraint:** 2-core / 8 GB VPS; the fleet re-backtest epoch (started 2026-09-06) owns the box until ≈ Wed 2026-09-09 — this spec's rollout (surface v3 rebuild) runs in the first idle window after merge, never beside a fleet child.

## 0. Why

The v2 surface (PCHIP smile per expiry on log-moneyness, constant-maturity 30/90-day ATM and 25Δ points) gives strategies the *level* and the *25Δ slope* of the smile. It says nothing about the smile's **curvature and tails** — the part of the surface that prices crash risk. Two standard, model-free quantities close that gap, and both fall out of the smile we already fit:

- **F6 — model-free implied variance (MFIV).** The VIX construction (Demeterfi–Derman–Kamal–Zou 1999; CBOE 2003) applied per ticker: the variance swap rate implied by the whole OTM strip, not just ATM. `mfiv_30d − iv30` is the tail/convexity premium in vol points.
- **F7 — risk-neutral density features.** Breeden–Litzenberger (1978): the RN distribution of `S_T` is the second strike-derivative of the call price. From it, RN skewness and kurtosis of the 30-day log-return (via the Bakshi–Kapadia–Madan 2003 spanning integrals — the same density's moments without a numerically-differentiated-twice curve) and two RN tail probabilities, `P(S_T ≤ 0.9·S)` and `P(S_T ≥ 1.1·S)`, from the smile-adjusted digital (first derivative).

**F5 — synthetic options engine** (`src/backtest/options_backtest.py`, SP-4 Phase 0, dormant — the manifest has zero option-instrument strategies) gets the three correctness upgrades the fit review ranked: a dividend yield `q` from `corporate_actions.parquet`, American exercise, and an IV anchor hierarchy that prefers the REAL surface master over the VIX-beta and realized-vol proxies, with a VIX9D/VIX term shape for the proxy. The hedging-error simulation from the fit review is NOT in scope — the engine already delta-hedges daily with a 1 bp cost; that *is* a discrete-hedging P&L simulation.

## A. Surface v3 — MFIV and RN-density features (`src/strategies/options_surface.py`)

### A.1 Conventions (unchanged from v2 A.4)
`r = q = 0`, forward `F = spot`, log-moneyness `k = ln(K/F)`, `T = dte/365`. All new quantities are computed from the fitted PCHIP smile `σ(k)` of ONE expiry, with **flat extrapolation** outside the observed strike range: `σ(k) = smile(clip(k, k_min, k_max))`. (Ruling G1: flat wings, not linear-in-variance — conservative, matches CBOE's practice of truncating at the last quoted strike, and cannot produce runaway wing variance from a steep last segment.)

Normalised, undiscounted OTM prices as a function of `k` (Black 1976 with `F = S`, `e^{rT} = 1`):
- `d1(k) = (−k + σ²T/2)/(σ√T)`, `d2 = d1 − σ√T`
- call for `k ≥ 0`: `c(k) = Φ(d1) − e^{k}Φ(d2)`
- put for `k < 0`: `p(k) = e^{k}Φ(−d2) − Φ(−d1)`
- `q(k) = c(k)` if `k ≥ 0` else `p(k)` (the OTM strip)

Grid: `k ∈ [−L, +L]`, `L = K_TRUNC · σ_atm · √T` with `K_TRUNC = 5`, `N_GRID = 401` points, trapezoid rule (ruling G2). Jiang–Tian (2005) show truncation error below 0.1 % of variance at ±3.5σ√T for realistic smiles; 5σ√T plus 401 points leaves discretisation as the only error, and the flat-smile oracle below pins it to `1e-4` relative.

### A.2 Per-expiry quantities (new `SmileFit` fields; `None` when the fit exists but a quantity is not finite)
| field | definition |
|---|---|
| `mfiv` | `√(V/T)` with `V = 2 ∫ q(k) e^{−k} dk` — the model-free total variance (the VIX integral in log-moneyness: `dK/K² = e^{−k}/F · dk`). Oracle: flat smile ⇒ `mfiv = σ` to 1e-4. |
| `rn_skew`, `rn_kurt` | BKM (2003) with `r = 0`: `V₂ = 2∫(1−k) q e^{−k} dk`, `W = ∫(6k − 3k²) q e^{−k} dk`, `X = ∫(12k² − 4k³) q e^{−k} dk` (one integrand weight for both wings — the put-wing signs collapse because `ln(S/K) = −k`), `μ = −V₂/2 − W/6 − X/24`, `skew = (W − 3μV₂ + 2μ³)/(V₂ − μ²)^{3/2}`, `kurt = (X − 4μW + 6μ²V₂ − 3μ⁴)/(V₂ − μ²)²`. `rn_kurt` is RAW kurtosis (3 = lognormal). Oracle: flat smile ⇒ `skew ≈ 0` (|·| < 1e-3), `kurt ≈ 3` (±1e-2). |
| `rn_p_dn10`, `rn_p_up10` | smile-adjusted digital: `P(S_T ≤ K) = Φ(−d2) + e^{−k} φ(d1) √T σ′(k)` evaluated at `k = ln 0.9` (dn10) and `1 − P(S_T ≤ K)` at `k = ln 1.1` (up10), `σ′` = PCHIP derivative (0 in the flat wings). Clipped to `[0, 1]` — a PCHIP smile is not arbitrage-free by construction (ruling G3). Oracle: flat smile ⇒ `Φ(−d2)` exactly. |

### A.3 Constant maturity and the row keys (version 3)
| key | definition |
|---|---|
| `mfiv_30d`, `mfiv_90d` | `constant_maturity(fits, 30/90, 'mfiv')` — the existing total-variance interpolation, unchanged (it interpolates `v²·t` linearly, which is exactly right for a variance-swap rate). |
| `mf_tail_premium_30d` | `mfiv_30d − iv30` (vol points; ≥ 0 for a convex smile — the price of the wings relative to ATM). |
| `rn_skew_30d`, `rn_kurt_30d`, `rn_p_dn10_30d`, `rn_p_up10_30d` | taken from the expiry nearest 30 DTE when `|dte − 30| ≤ 15`, else `None` (moments and probabilities do not interpolate linearly; ruling G4). |
| `options_features_version` | **3** |

All seven are `SCALAR_KEYS` members ⇒ they flow through the builder (`OUT_COLS`), the panel (`compute_rolling_options_fields.build_panel`, which defaults missing SCALAR_KEYS to `None` with one warning), the live dict (`options_aux_v2.build` calls `features_for_day`) and `aux_data_loader.FIELDS` (added there explicitly). **Every v2 key is byte-identical under v3** — pinned by a new "v2 freeze" test that snapshots the v2 outputs for the checked-in `tests/fixtures/options_chain_2026-09-03.parquet` (SPY/AAPL/XOM, every `SCALAR_KEYS` value) into `tests/fixtures/options_surface_v2_expected.json` BEFORE any v3 code lands, and asserts the v3 module reproduces them.

Series features (A.5 of the v2 spec) are unchanged: no rank/z-score of the new keys in this iteration (ruling G5 — consumers first).

### A.4 Cost
Per (ticker, session): ≤ ~8 fitted expiries × (401-point strip with vectorised `scipy.stats.norm.cdf`) — under 2 ms. Live: ~3,500 tickers ⇒ < 10 s inside the existing 240 s `OPENCLAW_OPTIONS_SURFACE_BUDGET_S`; the shadow line gains `mfiv_nonnull=…% rn_nonnull=…%`. Builder: the 2026-06-29..today rebuild adds minutes, not hours.

### A.5 Consumers
None change in this spec. The seven keys are available to the research lane (strategycoder / mastermind) through `aux_data['options'][ticker]` and the enriched panel; the research-lane key catalogue that documents `aux_data['options']` (wherever `iv_25d_put_30d` is documented today — the plan locates it) gets the v3 rows. `S21_iv_hv_spread` and `S_HV19/20` keep reading `iv30`/`skew` — a follow-up research item, not this spec.

## B. Synthetic options engine (F5)

### B.1 Dividend yield — `src/backtest/dividends.py` (new)
`dividend_yield_asof(ticker, as_of, spot) -> float`: sum of `cash_amount` over `cash_dividend` rows of `data/master/corporate_actions.parquet` with `as_of − 365 d < ex_date ≤ as_of`, divided by `spot`; `0.0` when the ticker has no dividends. Coverage today is 2024-02-09..present (9,612 cash-dividend rows, 2,851 symbols); for `as_of` earlier than `coverage_start + 365 d` the trailing window is incomplete, so `q` is **backfilled with the ticker's first full trailing-year yield** and the module warns ONCE per process (`dividends: q backfilled before <date> for <n> tickers`). Ruling G6: a constant per-ticker pre-coverage yield beats `q = 0` (which mis-prices every pre-2025 SPY/QQQ put by the full 1.2 %/0.6 % carry) and beats a look-ahead trailing yield (which would leak the future). Per-ticker series `lru_cache`d; `OPENCLAW_CORPORATE_ACTIONS_PARQUET` env override for tests; the module never raises — an unreadable file ⇒ `q = 0` with one warning.

### B.2 Pricing — `src/backtest/options_pricing.py`
- `bs_price / bs_greeks / strike_for_target_delta / implied_vol` gain `q: float = 0.0`. With `q == 0` they call the SAME py_vollib `black_scholes` path as today (bit-identical; existing tests untouched); with `q ≠ 0` they use `py_vollib.black_scholes_merton` (+ its analytical greeks). `_rate(r, as_of)` unchanged (constant 4 % when `as_of is None`, `risk_free.rf_annual_asof` otherwise).
- `american_price(flag, S, K, t, sigma, r, q) -> float`: **Cox–Ross–Rubinstein binomial tree, `N = 200` steps, vectorised numpy backward induction** (ruling G7: a tree, not Bjerksund–Stensland 2002 — the closed form is a transcription risk with no independent oracle on this box, while the tree IS the oracle; ~1 ms per price is irrelevant at the engine's few-thousand contracts per backtest). Shortcut: a call with `q ≤ 0` returns the European price (never optimal to exercise early). `american_delta(...)` by central difference (`ΔS = 1e-3·S`).
- `price(flag, S, K, t, sigma, r=None, q=0.0, as_of=None, exercise='european')` and `delta(...)` dispatchers; `exercise ∈ {'european', 'american'}`.
- Oracles (tests): (1) Hull's American put `S=50, K=50, r=0.10, σ=0.40, T=5/12` ⇒ `4.29 ± 0.02` at `N=500`, while its European value is `4.08 ± 0.01`; (2) American ≥ European for every point of a (flag, moneyness, T, q) grid; (3) American call with `q = 0` equals the European call; (4) deep-ITM American put ≥ intrinsic `K − S` and within 1e-6 of it when `r > 0`, `q = 0`, `S ≪ K`; (5) convergence `|P_200 − P_800| < 0.5 %`.

### B.3 IV anchor hierarchy — `src/backtest/synthetic_iv.py`
`synthetic_iv(prices, vrp_factor, window, underlying, as_of, dte=30)` resolves, in order, and `synthetic_iv_detail(...)` returns `(iv, source)`:
1. **`surface`** — `data/master/options_surface.parquet` row for `(underlying, date ≤ as_of, within 7 calendar days)`: constant-maturity to `dte` in total variance between `iv30` (30 d) and `iv90` (90 d), flat `iv30` below 30 d, flat `iv90` above 90 d; `None` when the row or `iv30` is missing. Per-ticker series cached; `OPENCLAW_OPTIONS_SURFACE_PATH` honoured (already the v2 override).
2. **`vix_term`** — for `OPTION_UNDERLYING_BETA` names: `β ×` the VIX term point at `dte`, interpolated in total variance between `VIX9D` (9 d) and `VIX` (30 d) from `data/master/vol_indices.parquet`, flat `VIX9D` below 9 d, flat `VIX` above 30 d (no VIX3M in any master — §F). At `dte = 30` this is `β × VIX(as_of)` exactly, so `vol_index.vix_anchored_iv` keeps its contract and its tests. When `vol_indices.parquet` lacks the date, fall back to the prices.parquet `^VIX` series as today.
3. **`realized`** — `realized_vol × vrp_factor`, floored at `IV_FLOOR` (unchanged).
The default `dte=30` and the tier order keep every existing `synthetic_iv` test green EXCEPT that tests must point `OPENCLAW_OPTIONS_SURFACE_PATH` at an empty temp path to exercise tiers 2–3 on SPY — the plan updates `tests/backtest/test_vol_index.py::test_synthetic_iv_uses_vix_when_supported` accordingly (ruling G8: the surface tier is ON by default — the engine, the calibration script and any future caller should all see the real surface when it exists).

### B.4 Engine wiring — `src/backtest/options_backtest.py`, `src/strategies/base.py`
- `OptionSpec.exercise: str = 'american'` (US-listed equity/ETF options are American; ruling G9). `from_dict` picks it up automatically.
- Every `bs_price`/`bs_greeks` call in the cycle pricers goes through `price`/`delta` with `q = dividend_yield_asof(ul, as_of, S)`, `exercise = spec.exercise`, `as_of` for the rate, and `synthetic_iv(..., dte = remaining calendar days)` — the IV used to mark a contract on day `t` is the surface/term point at ITS remaining life, not the 30-day point.
- Expiry intrinsic settlement, roll, hedge cost and the `pnl_pct` convention are unchanged.
- One summary log line per `simulate` call: `[options_backtest] iv sources: surface=n vix_term=n realized=n; exercise=american|european; q>0 on n contracts` (counts, not per-trade noise). The result dict shape is unchanged (downstream metrics/DB reuse).
- `scripts/options_parity_check.py`'s IV gate compares the engine's IV to real chain IV; with tier 1 live it will compare surface to chain (near-zero MAE) on the overlap — documented in its header by the plan, not changed.

## C. Rollout
1. Merge to main ⇒ live dict serves v3 keys on the next signals compute (the flag `OPENCLAW_OPTIONS_SURFACE` still gates the SWAP of the dict; in shadow mode the v3 keys are computed and summarised, not served). No flag change in this spec.
2. Surface master v3 rebuild: `scripts/build_options_surface.py --start 2026-06-29 --end <latest>` (append_dedup replace on `(ticker, date)`; `UNION ALL BY NAME` fills the new columns) → `scripts/compute_rolling_options_fields.py` → the parity/freeze tests already guarantee no v2 value moved, but **a re-backtest IS owed** (final review I1): amendment §H changes `iv30` — and therefore `vrp`, `iv_rank`, `ts_ratio`, `term_slope` — for the thin-chain names, and `S21_iv_hv_spread`, `S_HV8_gamma_theta_carry`, `S_HV19_iv_surface_tilt`, `S_HV20_iv_dispersion_reversion` read those keys, so after the panel rebuild re-run those four one strategy at a time (`scripts/rebacktest_options_sleeve.sh` pattern; the 2026-09-06 sleeve unit OOM-killed at 3.4 GB inside `S_HV19` under `MemoryMax=3500M` — use `MemoryMax=4500M`). The v3-only keys themselves have no consumer and the synthetic engine has no manifest consumer. Transient unit, Nice 19, MemoryMax 3500M, in the first window with no fleet child running (`scripts/fleet_weekend_window.sh`-style wait on `openclaw-fleet-overnight-resume.service` / `fleet-rf-epoch-20260906.service`).
3. Verification: `mfiv_nonnull`/`rn_nonnull` ≥ 90 % of tickers with `n_expiries_fit ≥ 2`; SPY `mfiv_30d` within 0.00–0.03 above `iv30`; `rn_skew_30d < 0` for SPY (the index smile is left-skewed); `rn_p_dn10_30d` for SPY in 0.5–5 %.
4. Runbook `docs/runbooks/2026-09-06-options-surface-v3-rollout.md` + changelog entry; memory + handoff.

## D. Tests (all new, pytest, no network, no production masters)
- `tests/strategies/test_options_surface_v2_freeze.py` — v2 outputs reproduced byte-for-byte on the fixture (written FIRST, against the v2 module).
- `tests/strategies/test_options_surface_mfiv.py` — flat-smile oracles (mfiv = σ, skew ≈ 0, kurt ≈ 3, tails = Φ(−d2)); skewed SVI fixture ⇒ `mfiv > atm`, `rn_skew < 0`, `p_dn10 > p_up10`; extrapolation flatness; `None` when `|dte−30| > 15`; every new key present in `SCALAR_KEYS`, `FIELDS`, the builder's `OUT_COLS`, the panel and the live dict (parity test `SHARED` list extended).
- `tests/backtest/test_dividends.py` — synthetic corporate_actions fixture: trailing-year sum, zero-dividend ticker, pre-coverage backfill + one warning, unreadable file ⇒ 0.
- `tests/backtest/test_american_pricing.py` — the B.2 oracles.
- `tests/backtest/test_synthetic_iv_hierarchy.py` — tier selection with temp surface + vol-indices fixtures; `dte` interpolation; `dte = 30` ≡ `β × VIX`.
- `tests/backtest/test_options_backtest.py` — extended: engine runs end-to-end with `exercise='american'` and `q > 0`; the iv-sources log line; result dict keys unchanged.

## E. Sequencing (plan tasks)
Freeze test → strip helpers (A.1/A.2) → CM + row keys + version 3 + consumers + shadow (A.3, A.4) → dividends (B.1) → q + American pricer (B.2) → IV hierarchy (B.3) → engine wiring (B.4) → docs/runbook/rollout script (C) → final whole-branch review → merge → rollout in the first idle box window.

## F. Out of scope
VIX3M ingest (the term proxy stops at 30 d); F1b (solving IV for the 39 % null-IV wing rows — would sharpen every tail feature and is the natural next step); strategy consumers of the v3 keys (research lane); an arbitrage-free smile (SVI/SSVI fit) — the PCHIP smile stays, with the G3 clip; the hedging-error simulation; any change to `OPENCLAW_OPTIONS_SURFACE` / `OPENCLAW_RF_SOURCE` flag state.

## G. Rulings (controller, on the operator's behalf) — cost if wrong
- **G1** flat-wing extrapolation — cost: MFIV understated on names whose last quoted strike sits inside the tail (deep wings are exactly the rows Alpaca leaves null); F1b fixes the input, not this rule.
- **G2** `K_TRUNC = 5`, 401 points, trapezoid — cost: none measurable (oracle-pinned).
- **G3** tail probabilities clipped to [0, 1] instead of rejecting non-arbitrage-free smiles — cost: a locally negative density passes silently; the freeze/oracle tests bound it on the fixture only.
- **G4** RN moments/tails from the expiry nearest 30 DTE (`±15`), MFIV interpolated — cost: a ±15-day maturity mismatch on the moments for names with sparse expiries.
- **G5** no series (rank/z-score) features on v3 keys yet — cost: a strategy wanting `mfiv_rank` computes it itself.
- **G6** pre-coverage `q` backfilled with the first full trailing year — cost: pre-2025 `q` is a constant per ticker (documented, flagged in a warning).
- **G7** CRR tree (N=200) instead of Bjerksund–Stensland 2002 — cost: ~1 ms per price; no closed-form greeks (delta by finite difference).
- **G8** surface IV tier ON by default — cost: `options_parity_check.py`'s IV gate becomes trivial on the overlap window (documented).
- **G9** `OptionSpec.exercise` defaults to `'american'` — cost: any existing synthetic backtest row (none in the manifest) would re-price slightly higher on puts.
- **G10** no interactive brainstorming with the operator — the F5–F7 scope was ranked and approved in the 2026-09-04 fit review; ambiguities resolved here as rulings rather than blocking on questions the operator cannot answer in real time.

## H. Amendment 2026-09-06 13:00 UTC — thin-chain coverage (controller ruling G11)

**Measured on main after the v2 rollout (surface master 150,777 rows, 46 sessions 2026-06-29..09-04):** on 2026-09-04 `iv30` is non-null for 28.8 % of the 4,169 panel tickers and **30.5 % of the 3,861 liquid-tier tickers** (1,178 names); the v1 panel carried `iv_front` for 100 % of 3,855 liquid names on 09-03. Cause: v2's smile gate (≥ 5 IV-bearing strikes on both sides of spot, per expiry) plus the 30-day bracket / ±10-day one-sided rule. 57 % of liquid names fit at least one expiry, 43.8 % fit two. Four live strategies (`S21`, `S_HV8`, `S_HV19`, `S_HV20`) read `iv30`/`iv_rank`/`vrp`; their candidate sets would shrink ~3× when `OPENCLAW_OPTIONS_SURFACE` flips. SPY/AAPL/XOM values themselves are correct (0.12 / 0.24 / 0.27).

**Amendment (plan Task 9):**
1. An expiry that cannot carry a smile gets a v1-style ATM-band point — the |Δ| .40–.60 mean IV (exactly v1's `iv_front` per expiry), `n_strikes` = band row count, every smile-only field `None`, `SmileFit.source = 'atm_band'`. Smile fits take precedence per expiry; band points only fill expiries with no smile. Band points participate in the 30/90-day total-variance interpolation for `iv30`/`iv90` (and therefore `vrp`, `iv_rank`, `ts_ratio`, `term_slope`). Every smile-only key stays smile-only: the 25Δ points, MFIV and the RN moments/tails interpolate over smile fits alone, and the two difference keys `skew_25d_30d` and `mf_tail_premium_30d` are taken against the smile-only 30-day ATM (`iv30_s`), never against a band-blended `iv30`. `iv30_source` is `'atm_band'` whenever a band point participated in `iv30` (final review C1, 2026-09-06).
2. `CM_ONE_SIDED_TOL` 10 → 20 days: a lone 14-day or 42-day monthly (the typical Friday-capture pair) anchors the 30-day point alone.
3. New keys: `iv30_source` (`'smile' | 'atm_band' | None`) and `n_expiries_atm`; `n_expiries_fit` keeps its v2 meaning (smile fits only) so every frozen v2 value stands.
4. Guard: the v2 freeze test must still pass; if a band point ever changes a SPY/AAPL/XOM value, the fallback degrades to ticker-level (band points used only when a ticker-day has no smile fit at all) — documented in the plan.
5. The runbook flip thresholds for `OPENCLAW_OPTIONS_SURFACE` (`iv_rank_nonnull ≥ 80 %`) were written assuming v1-like coverage; after this amendment they are read against the live line as-is, and the `iv30_source` split is reported beside them.

**Cost if wrong:** two extra columns and a ~40-line function to revert; names that only ever had a band point carry a noisier `iv30` than a smile would give — exactly what v1 served, now labelled.
