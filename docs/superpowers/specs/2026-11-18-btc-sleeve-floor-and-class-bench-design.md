# BTC sleeve floor + per-class activation benchmark — design

**Date:** 2026-11-18 · **Status:** APPROVED by the operator 2026-11-18 ("continue" on the short design; floor default 10 % NAV accepted as proposed) · **Parent:** `docs/specs/2026-11-06-crypto-engine-whale-flow-design.md` §5, §7c; Phase 3 ledger decisions 14 and 15 (`.superpowers/sdd/2026-11-10-crypto-pooled-sizing-phase3/progress.md`, 2026-11-17 20:35Z).

## 1. Intent and why

Two findings from the 2026-11-17 re-gate of record and shadow cycle #2:

- **Decision 14.** `activation_assigner --all` judges every strategy against ONE benchmark vector — the equity sleeve's per-regime run Sharpe (`S_beta_spy` run `79ce96de`: LOW 0.9378 / TRANS 0.4299 / HIGH 0.5326 / CRISIS 1.575). `instrument_class` only sets the drawdown ceiling and `min_trades`. The four crypto TRANSITIONING cells switched on at 20:00Z cleared SPY's 0.43, not a BTC comparator. The operator ruled **(b)**: crypto-class strategies are judged against the **BTC sleeve's own run vector** (`S_beta_btc` run `12632bad`: LOW 0.86 / TRANS 0.23 / HIGH −0.49 / CRISIS 0.60), i.e. the equity convention applied per class.
- **Decision 15.** The first non-trivial shadow line sized BTC-USD to $172 = 0.2 % NAV (`crypto_sizing.shadow[HIGH_VOL]: S_m=1.79 n=1 gross=172 … eq_scale_ratio=0.9981`). The sleeve is one order among ~490 under own-conviction λ-sharing in the one normaliser, so BTC exposure after the flip would be negligible. The operator ruled: **build a BTC sleeve floor "in the same way as SPY"; BTC is not discounted by its regime Sharpe, while the other crypto tickers keep facing the rule-C conviction discount against BTC S_m.**

Why it matters now: two clean shadow cycles are on the books; the pooled flip is the next operator step. Flipping with a 0.2 % BTC sleeve would make the crypto engine inert in practice, and the activation comparator would keep letting crypto cells through on an equity hurdle.

## 2. Goals, non-goals, anti-goals

**Goals**
- G1 Crypto-class strategies are activated/deactivated against the crypto sleeve's per-regime run vector; equity/etp/option classes are unchanged (SPY vector).
- G2 When the BTC sleeve is acting, BTC-USD's sized target is at least `benchmark_min_nav_frac_crypto · NAV` (default **0.10**) before the physical/safety bounds (25 % ticker cap, 50 % crypto gross cap, non-marginable funding, hold-flat).
- G3 The floor is **not** a function of BTC's regime Sharpe: it applies in every crypto regime in which the sleeve has a live long BTC-USD signal, including regimes where `S_m_crypto ≤ 0`.
- G4 Other crypto tickers keep the existing rule-C hurdle `|S_adj| − max(S_m_crypto, 0)` (spec §5.3). Unchanged.
- G5 The crypto beta budget (rule C's removed crypto-alt conviction redirected to BTC, Σ conserved) runs under its **own** flag so SPY's `OPENCLAW_BENCH_BETA_BUDGET=0` no longer kills it; its pool is visible on the line.
- G6 Everything is visible in the `crypto_sizing.{shadow|apply}` line and the assigner log before the flip; the dashboard's pinned parsers keep working.

**Non-goals**
- No change to the equity sizing path. The pinned test `test_pooled_shadow_leaves_equity_targets_identical` must still pass with the pooled flag unset. With the flag on, the only equity-visible effect remains the shared normaliser (already the §5.3b design).
- No pooled flip, no breaker arming, no ETH sleeve, no change to `S_beta_btc`'s bracket or weight rule (`weight := max(S_m, 0)` stays; it governs only the sleeve's own-conviction share *above* the floor).
- No new comparator for crypto activation (no BTC buy-and-hold S_m as the activation gate — option (c) was rejected).
- No dashboard redesign; the bench card may gain a crypto payload, UI work is optional.

**Anti-goals (failure even if it "works")**
- Equity byte-identity broken under the unset flag.
- The floor surviving a hold-flat / breaker zero, or exceeding the 25 % ticker cap or the non-marginable buying power.
- A crypto strategy silently judged against the SPY vector because the crypto sleeve had no run for some regime (the tier-2 fallback must be class-scoped; never SPY).
- A log line that the dashboard's `HEADER_RE` / `DETAIL_RE` / `BENCH_VECTOR_RE` / `SUMMARY_RE` parsers mis-count.

## 3. Constraints

- Box: 2-core / 8 GB. No full test suite while the fleet runs; no heavy tests inside 20:00–20:15Z (compute) or 20:55–21:02Z (execute). Build in a worktree.
- Python steps read the main tree at spawn ⇒ the assigner change is live at the next 15:00 ET daily cycle after merge (intended: the cycle re-derives crypto cells). `promotion_service.js` runs inside johnbot ⇒ needs a johnbot restart in a window (21:30–22:00Z or after 23:30Z, `XDG_RUNTIME_DIR=/run/user/0 systemctl --user restart johnbot`).
- The sizer change is shadow until `OPENCLAW_CRYPTO_POOLED_SIZING=1` (operator). The shadow line must show the floor on the first cycle after merge.
- Pinned formats: `HEADER_RE`, `DETAIL_RE` (`<tok>: LOW_VOL: …`), `BENCH_VECTOR_RE` (`bench: sleeve=…`), `THRESHOLDS_RE` (`thresholds: …`), `SUMMARY_RE` in `src/channels/api/activation_preview.js:76-111`; `tests/test_activation_api.test.js`. `test_shadow_line_format` in `tests/execution/test_pooled_sizing_helpers.py` pins the crypto line.
- Tests on this box reach the real DB (`.env` loads at import): stub DB in fixtures, as the existing assigner/sizer tests do.
- Keep file edits, `git commit`, `git push` as separate tool calls (ERRORS.md rule). Commit messages: no backticks in shell; use a message file.

## 4. Approaches

### Decision 14 — comparator for crypto-class activation
- (a) Keep one SPY vector for all classes. Rejected: contradicts the "BTC-only benchmark" ruling (spec §9).
- **(b) Per-class sleeve vector** — the crypto sleeve's latest primary run `strategy_backtest_regimes.sharpe` per regime, same tiering as equity. One code path (`resolve_bench_sleeve_id` already takes a class). **Chosen by the operator.**
- (c) BTC buy-and-hold `S_m_crypto` (1.50 / 1.10 / 1.79 / 0.95). Rejected: a second comparator semantics; would switch all four new TRANS cells off.

### Decision 15 — BTC sleeve size
1. Crypto beta budget only, own flag. Faithful to SPY's mechanism but yields ≈ $0 today (no crypto alts to harvest; the half-wired budget at `regime_blended_sizer.py:1814-1815` discards its pool).
2. Flat NAV floor only, injected as weight before the normaliser (λ·NAV conserved; equities give up exactly the top-up).
3. **Both** — the floor is the guarantee, the budget adds on when alts appear. **Chosen** (recommended; the budget is a few lines on existing wiring).

## 5. What is decided (build exactly this)

### 5.1 Per-class activation bench (decision 14b) — `src/backtest/activation_assigner.py`
- `load_bench_sharpe(conn, sleeve_id=None, *, instrument_class='equity')`: resolves the sleeve via `resolve_bench_sleeve_id(conn, sleeve_id, instrument_class)`; tier 2 fallback key is **per class**: `strategy_activation_bench_sharpe` (equity, unchanged) / `strategy_activation_bench_sharpe_crypto` (new); tier 3 `DEFAULT_MIN_SHARPE` with the existing WARN naming the regime **and the class**. Crypto never reads the equity key.
- `main()`: build `bench_vectors = {'equity': (vec, meta)}` as today, plus `'crypto'` when the registry holds a crypto sleeve (`parameters.benchmark_sleeve='true'` and `instrument_class='crypto'`). `apply_one(..., bench=bench_vectors.get(cls, equity_vec))` where `cls = classes.get(sid, 'equity')` from the manifest (`_load_instrument_classes`). Non-crypto classes (equity/etp/option) keep the equity vector.
- Log lines (parser-safe; must NOT start with `bench: ` or `thresholds: ` and must not contain `: LOW_VOL: `): `bench[crypto]: sleeve=S_beta_btc source=registry run=<id> LOW_VOL=… TRANSITIONING=… HIGH_VOL=… CRISIS=…` and `thresholds[crypto]: LOW_VOL=… … (excess=…)`. The equity lines, `HEADER` (`threshold=` stays the equity LOW_VOL display value) and `SUMMARY` are byte-unchanged.
- Stamps (additive, never reshaping existing keys): `strategy_activation_last_applied` gains `bench_by_class: {"crypto": {"sleeve_id","bench_sharpe","bench_run_id","bench_regime_source"}}`; `stamp_bench_sharpe_config` also writes `strategy_activation_bench_sharpe_crypto` (same "sleeve-sourced regimes only" rule).
- Consumers: `src/lib/promotion_service.js` picks the key by the strategy's manifest `instrument_class` (crypto → `_crypto` key; if absent, use the Python `DEFAULT_MIN_SHARPE` 0.5 and log — never the SPY vector). `src/execution/activation_apply._bench_sleeve_run_id` / `pending_state`: also compare the crypto sleeve's latest run against `bench_by_class.crypto.bench_run_id` so a new `S_beta_btc` run marks activation pending. `activation_preview.js benchCardPayload`: include `bench_by_class` in the payload (UI optional).
- Tests: update `tests/backtest/test_activation_assigner_multi_sleeve.py` (the "never picks the crypto sleeve for the vector" pin flips to "picks it for crypto-class strategies only"), add per-class fallback tests (crypto missing regime → crypto key → 0.5, never SPY), log-shape tests against the real regexes (`tests/test_activation_api.test.js` must still pass), promotion_service class test.
- Expected effect at the next daily cycle (from the 11-17 runs): halving CRISIS (0.64 vs 0.60) turns ON; LOW/TRANS cells unchanged; HIGH cells stay off on `min_trades`.

### 5.2 BTC sleeve floor + crypto beta budget (decision 15) — `_crypto_pooled_block` in `src/execution/regime_blended_sizer.py` + pure helpers in `src/execution/pooled_sizing.py`
- **Floor level**: `pipeline_config.benchmark_min_nav_frac_crypto`, default **0.10**, read via a new `benchmark_min_nav_frac(instrument_class='crypto')` in `benchmark_sizing.py` (same shape as `benchmark_max_nav_frac`; `0` disables; must be ≤ `benchmark_max_nav_frac_crypto`, else clamp to it with a WARN).
- **When it applies**: the sleeve is *acting* — `S_beta_btc` has an open LONG BTC-USD signal for the run date in the crypto signal set (eligible in the current crypto regime per `strategy_regime_params`; `always_on` makes that every regime). Detection must come from the signal set, **not** from the sleeve's weight row: with `S_m ≤ 0` the sleeve weight is 0 and today's aggregation drops it — the floor still applies (G3). `S_m` sign and value play no part in the floor.
- **Where**: inject before the normaliser as a weight, so the one λ·NAV normaliser sees it and equities give up exactly the top-up under pooling (Σ = λ·NAV conserved — "shave, never redistribute" is honoured because the floor is a weight, not a post-hoc dollar add). Pure helper `pooled_sizing.floor_weight(w_btc, f, gross_eq, gross_cr_other, lam, liq_cr) -> float` returns `w'_btc = max(w_btc, w_floor)` where `w_floor` solves `w·scale_cr(G_eq, G_other + w) = f·NAV` for the actual `pooled_scales` formula; if the equation has no positive solution (`k = λ·clip(liq_cr, 0, 1) ≤ f`), the floor takes the half-book weight `w'_btc = max(w_btc, G)` (G = Σ the other equity + crypto weights; BTC then gets about half of `k·NAV`): still a weight inside the one normaliser, so Σ = λ·NAV is conserved, logged with a WARN, and bounded by the 25 % BTC clamp, the gross cap, funding and hold-flat downstream (as-built ruling 2026-11-18; no post-normaliser dollar floor). In **shadow** mode the equity book is untouched by construction (crypto excluded from the equity normaliser when the flag is unset) and the line shows what the floor would do.
- **Order** (steps after the hurdle in the block): hurdle → beta budget → **floor weight** → normaliser → dust → per-ticker cap (BTC exempt as today) → cluster cap → corr removal → 25 % BTC clamp → long-only → 50 % crypto gross cap (**shave non-BTC tickers first; BTC only if alts are exhausted**) → funding pass (binds; physical) → hold-flat (zeroes; safety). The floor never survives hold-flat, never exceeds the 25 % clamp or non-marginable BP.
- **Crypto beta budget**: `OPENCLAW_CRYPTO_BETA_BUDGET` (new env; **default ON** when unset; `'0'` kills) replaces `beta_budget_enabled()` at `regime_blended_sizer.py:1814`. Keep `apply_beta_budget(w, hurdled, max(S_m,0), bench_cr)`; **keep the pool** and carry it to the line. Pool is harvested from crypto alts only (never from equities; that is SPY's).
- **Line**: `pooled_sizing.shadow_line(...)` gains two trailing fields appended after `top=…`: ` floor_usd=%.0f pool=%.2f` (floor_usd = the dollars the floor added before caps, 0 when inactive; pool = beta-budget pool in Sharpe units). Update `test_shadow_line_format`; verify `crypto_sizing_last_line` consumers (`routes_crypto.js` / Crypto page) display the raw line and need no parser change (builder confirms by grep).
- Tests (`tests/execution/test_sizer_pooled_shadow.py`, `test_pooled_sizing_helpers.py`, `test_benchmark_sizing_multi_bench.py`): floor applies with sleeve acting and `S_m` negative; floor inactive without the sleeve signal; Σ conservation under pooling (equity gross shrinks by the top-up); floor bounded by the 25 % clamp; hold-flat zeroes the floored BTC; funding pass scales it; 50 % gross cap shaves alts before BTC; budget on by default, off with `'0'`, pool carried; `benchmark_min_nav_frac` default/override/clamp-to-max; equity byte-identity pin still passes.

### 5.3 Rollout
1. Worktree `/root/openclaw-wt/crypto-sleeve-floor`, branch `crypto-sleeve-floor`, SDD ledger `.superpowers/sdd/2026-11-18-btc-sleeve-floor/progress.md` (gitignored; copy back to `/root/openclaw/.superpowers/sdd/` before the worktree is removed).
2. Merge to main (ff) outside compute/execute windows; push.
3. The assigner change is live at the next 15:00 ET daily cycle (`step=activation`) — expect the `bench[crypto]:` line and halving CRISIS ON; weights rebuild follows in-step.
4. The sizer change is SHADOW (flag unset): the next `crypto_sizing.shadow[…]` line must show `floor_usd≈0.10·NAV` (≈ $9.1k at NAV 91k) and `pool=0.00`.
5. johnbot restart in a window for `promotion_service.js`.
6. Record: spec §7d (as-built), Phase 3 ledger (decisions 14/15 RESOLVED), CLAUDE.md crypto line, memory.

**Post-build amendment 2026-11-23**: the floor weight is solved with the EQUITY liquidity param (k = λ·liq_eq) and the
floor/own decision is made in own-conviction dollars; the floored BTC leg is priced at exactly frac·NAV. As built on 11-18
it used k = λ·liq_cr and scale_cr, which under crypto CRISIS (liq 0.5) cost equity 20 % for a 10 % floor. See crypto spec §7d.

## 6. Left to the builder
- Exact algebra of `floor_weight` against `pooled_sizing.pooled_scales` (liquidity params per class; dust floor interaction).
- How "sleeve acting" is detected from the crypto signal set when the sleeve weight row is absent (likely: keep the sleeve signal through aggregation with weight 0 and flag it, or query the signal set directly before `drop_stables`).
- Whether the 50 % gross cap's "alts first" shave is a new helper or a parameter on `crypto_gross_cap`.
- Dashboard bench-card crypto rendering (optional; payload is required).
- Test fixtures for a crypto sleeve with a run missing one regime.
