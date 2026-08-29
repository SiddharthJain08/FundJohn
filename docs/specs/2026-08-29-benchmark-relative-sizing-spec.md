# Benchmark-relative sizing (beta sleeve + `S_adj − S_m`) — design spec

**Date:** 2026-08-29 · **Status:** IMPLEMENTING — code landed 2026-08-29 (tasks 1–13, `67afceb..ecaad15`; plan `docs/superpowers/plans/2026-08-29-benchmark-relative-sizing.md`); rollout §3 steps 3–4 pending (sleeve backtest/promotion, shadow soak, flag flip) · **Owner:** BotJohn
**Supersedes (partially):** R1 benchmark-relative promotion criterion (five-repo adoptions, 2026-08-24) and R1-assigners (2026-08-25).

## 0. Decisions this spec encodes (operator, 2026-08-29)

| # | Decision | Consequence |
|---|----------|-------------|
| D1 | SPY regime Sharpe (`S_m`) is a **sizing** input only. It gates nothing. | R1 leg removed from promotion (candidate→live) AND from both activation assigners. Activation = the sliders (`strategy_activation_min_sharpe`, `min_trades`) alone. |
| D2 | Cadence normalization (`/√avg_holding_days`) removed from strategy weights and therefore from tangency S_adj. | `daily_weight = effective_sharpe`. Weights and S_adj are in annualized-Sharpe units, comparable to `S_m`. |
| D3 | Trade-count factor `√(ln n / ln anchor)` disabled for all strategy weights. | Contributions are raw sleeve Sharpe. |
| D4 | Beta sleeve = a plain fleet strategy `S_beta_spy` (approach A), SPY as the vehicle. | Backtested, promoted, sized, executed on the existing rails; flagged `benchmark_sleeve=True`. |
| D5 | Sizing rule C: per ticker, `w = sign(S_adj)·max(|S_adj| − S_m, 0)`; benchmark ticker exempt (`w = S_adj`). | Alphas are sized on conviction **in excess of the market**; beta is the base. |
| D6 | No beta cap. | Benchmark ticker exempt from the per-ticker conviction cap and excluded from the asset-correlation cluster cap. Upper bound = the gross rule λ·NAV. |
| D7 | Shorts face the same hurdle `S_m` as longs. | Symmetric: any position competes for the risk budget SPY would otherwise take. |
| D8 | Beta sleeve weight is data-driven (its own sleeve Sharpe), not pinned to `S_m`. | Same rails as every strategy; expected ≈1.9–2.0 vs `S_m` 2.03 in LOW_VOL. |
| D9 | Strategy-similarity pairs containing a benchmark sleeve use return-correlation only (when ≥ `ALPHA_FULL_OBS` overlapping obs). | Prevents the Jaccard leg from over-crediting diversification between beta and index-timers on the SPY ticker. |

## 1. Motivation (measured 2026-08-29, read-only)

- The R1 SPY leg was wired but unapplied: 101 of 103 live strategies sit on pre-migration-149 runs with NULL `benchmark_sharpe`.
- Against the actual sizing book (`strategy_weights_by_regime`, 109 current cells, all ≥ 1.0 slider), R1 at `sharpe > S_m` would remove **19 of the 25 LOW_VOL cells** (6 strategies lose their only sleeve) and nothing elsewhere. SPY regime Sharpe over 2016-04-11..2026-08: LOW_VOL 2.03, TRANSITIONING 0.55–0.60, HIGH_VOL 0.60, CRISIS 0.73.
- The intraday regime-of-record has been LOW_VOL for 121 days. A binary strategy-level gate against a 2.0 bar guts the live book; a continuous ticker-level rule lets multiple sub-market strategies combine (tangency is monotone) and lets beta absorb the exposure they cannot justify.

## 2. Components

### 2.1 R1 removal (D1)

Delete the benchmark leg from:
- `src/backtest/regime_qualification.py` — inline block in `qualifies_regime` (`benchmark_sharpe` kwarg, `[bench_gate]` logs) and `benchmark_leg_passes` + its two log helpers.
- `src/strategies/lifecycle.py` — `MIN_EXCESS_SHARPE_VS_BENCHMARK`, `_BY_CLASS`, `min_excess_sharpe_vs_benchmark`, and the `min_excess_sharpe_vs_benchmark` key in `_promotion_threshold`.
- `src/backtest/activation_assigner.py`, `src/backtest/eligibility_assigner.py` — the R1-assigners leg (`benchmark_sharpe` column in the SELECTs at ~:298/:309, the AND onto legacy pass, the `[bench_gate]` lines).
- `src/lib/promotion_service.js` — `MIN_EXCESS_SHARPE_VS_BENCHMARK*`, `getMinExcessSharpeVsBenchmark`, the `benchmark_sharpe` fail label in `judgeRegimeSleeve` (~:98–:114) and its export.
- `src/agent/curators/auto_approval.js` — any benchmark label.

Keep: `src/backtest/benchmark_baseline.py` (now the sizing input), `strategy_backtest_regimes.benchmark_sharpe` (migration 149; `unified_backtest.py` ~:1439 keeps writing it — informational, shown on the dashboard), `tests/backtest/test_benchmark_baseline.py`.
Tests to delete/rewrite: `test_r1_assigners_benchmark_leg.py`, the R1 cases in `test_activation_assigner.py`, `test_eligibility_assigner.py`, `test_promotion_thresholds.py`, `test_tail_stats_backtest_wiring.py`, `promotion_exit_hook_guard.test.js`.
Live effect: none today except the two strategies re-run on 08-26 (`S_btc_halving_clock_cycle_timing`, `S_evar_tempered_stable_sector_etf`) whose sleeves are currently judged with the leg — the plan's dry-run diff must list any cell that re-activates.

### 2.2 Cadence normalization out of weights (D2)

- `src/execution/strategy_weights.py::_regime_weight` (~:113): `w_daily = w` unless `OPENCLAW_STRATEGY_CADENCE_WEIGHT_NORM == '1'` (revert path; mirrors the stops precedent `OPENCLAW_STRATEGY_CADENCE_STOP_NORM=0` in `bracket_stacking.daily_normalized_levels`). Column `daily_weight` is kept so no consumer's schema moves; `cadence_days` is untouched (signal-window quantity: carried-set lookback, cadence gate).
- Consumers that read `daily_weight` and therefore flip consistently with the writer: sizer (`weight_by_strat` / `eff_weight_by_strat` → gate/size contributions → tangency), `crypto_redeploy_sizer.py:58`, `strategy_similarity.py:694–709` (gate-leg replica), `pyportfolioopt_shadow_sizer.py`.
- Dashboard (`src/channels/api/server.js`): `_effSharpeOf` (~:9361) and the row-level Eff.Sharpe column become the raw sleeve Sharpe; labels at ~:539/:558/:7778 drop "÷√hold". `X-Dashboard-Build` bump so stale tabs reload.
- Expected effect: long-hold strategies (9–21 d) gain ×3–4.6 relative weight vs short-hold; S_adj values rise ~3×. Per-ticker cap `0.10·(|S_adj|+1)·λ·NAV` loosens accordingly — constant left alone; retune only after reading the shadow diff.

### 2.3 Trade factor off (D3)

- New flag `OPENCLAW_TRADE_WEIGHT_FACTOR` (default unset → factor 1.0 for every strategy). When `'1'`, `orthogonalization.trade_weight_factor` applies as today. Sites: `regime_blended_sizer.py` ~:1627 (`_twf`), `strategy_similarity.py` ~:694–709 replica, dashboard contribution label. `strategy_trade_factor_anchor` stays as the knob for the ON path.

### 2.4 Beta sleeve strategy (D4, D8)

`src/strategies/implementations/S_beta_spy.py`:
- `STRATEGY_ID = 'S_beta_spy'`, `INSTRUMENT_CLASS = 'etp'`, basket `('SPY',)` (strategy-declared, like `S_evar_tempered_stable_sector_etf`); `benchmark_sleeve = True`.
- `BaseStrategy.benchmark_sleeve: bool = False` (new class attribute, `src/strategies/base.py`).
- Signal: LONG SPY **every bar**, `hold_days = 21` (= live default hold cap → hook/parity clean), stop/targets set far enough never to bind (stop −40 %, targets +400 %; the sleeve carries no bracket edge). Confidence HIGH, `position_size_pct` per fleet convention (ignored by the sizer).
- Backtest: overlapping 21-day lots average to SPY's daily return; one round-trip per 21 days → cost drag negligible; `trade_count` ≈ 2,600 clears `min_trades=100` per regime. Expected sleeve Sharpe LOW_VOL ≈1.9–2.0; TRANSITIONING/HIGH_VOL/CRISIS < 1.0 → **dormant under the 1.0 slider** (regime self-selection; no size scalar).
- Live: daily emission, `cadence_days` from `signal_frequency='daily'` → the sizer carries the signal; the rebalance step nets target vs held position (no churn). Hold cap 21 via `configured_max_hold_days` default.
- Registration: `strategy_registry.parameters.benchmark_sleeve = true` mirrored at registration so JS consumers (dashboard label) can read it; Python reads the class attribute (source of truth).
- Exemptions keyed on the flag: (i) `min_acting_strategies` gate — a ticker with a benchmark-sleeve contributor in the net direction passes; (ii) the `S_m` subtraction (§2.5); (iii) caps (§2.6); (iv) similarity rule (§2.7). The R1 exemption is moot (D1).

### 2.5 Sizing rule C (D5, D7)

Insertion point: `src/execution/regime_blended_sizer.py::_sharpe_cadence_path`, immediately after `ticker_w = defaultdict(float, _size_adj)` (~:1680) and before the acting gate.

```
S_m = _benchmark_regime_sharpe(regime_state)      # §2.5.1
bench_tickers = {tkr for tkr, meta in ticker_meta.items()
                 if any(is_benchmark_sleeve(sid) for sid in meta['strategies'])}
for tkr, s in list(ticker_w.items()):
    if tkr in bench_tickers or S_m is None:
        continue                                   # beta base / fail-open
    ex = abs(s) - S_m
    if ex > 0:  ticker_w[tkr] = math.copysign(ex, s)
    else:       ticker_w.pop(tkr); ticker_meta.pop(tkr); dropped.append(tkr)
```
- Then: acting gate → dust → normalization Σ|target| = λ·NAV → caps (with §2.6 exemptions) → rebalance/orders, all unchanged.
- `gate_net_sharpe` (raw S_adj) still drives the per-ticker cap formula and the bracket leader; only the sizing basis changes.
- Benchmark-ticker conviction = tangency S_adj of **all** strategies signalling SPY (sleeve + long index-timers add on the long side; short-SPY strategies subtract via S*_short). No separate base constant. Asymmetry is intentional: SPY at S_adj 2.6 is sized at 2.6; an alpha ticker at 2.6 is sized at 0.6.
- Flatten: unchanged. If no alpha ticker clears and the sleeve is dormant, `_maybe_flatten_zero_conviction` runs as today.
- Options overlay (`opt_active`) scales off the post-C equity gross, unchanged.

#### 2.5.1 `S_m` source
`backtest.benchmark_baseline.regime_benchmark_sharpe(DEFAULT_START_DATE, run_date)[regime_state]` — the same function and canonical window (`unified_backtest.DEFAULT_START_DATE = '2016-04-11'`) that produced the sleeve Sharpes S_adj is built from, so the subtraction is like-for-like. Computed once per sizer cycle (one predicate-pushdown SPY read + the 2.6k-row regimes parquet), persisted to `pipeline_config['benchmark_regime_sharpe']` as `{regime: value, as_of}` for the dashboard and for reuse by the intraday lane. `None`/`{}` → no subtraction that cycle, WARN `[bench_sizing] S_m unavailable; sizing on raw S_adj`. `regime_state` = the sizer's regime-of-record (intraday HMM); tags for `S_m` are the daily HMM's — the same mismatch every sleeve already carries.

#### 2.5.2 Flag and shadow
- `OPENCLAW_BENCH_RELATIVE_SIZING` (default unset = OFF). OFF: sizing byte-identical to today, but a **shadow line every cycle**: `bench_sizing.shadow[REGIME]: S_m=… dropped=N/M beta_share=… gross_moved=… top_moves=[…]`, posted to #botjohn-log via the existing `_post_corr_cumsharpe_log` expiry pattern. ON: the rule applies.
- Replay script `scripts/bench_relative_sizing_replay.py` (read-only, mirrors `exit_hook_live_replay.py`): rebuilds today's book with the flag OFF and ON from the persisted signals/weights and prints the per-ticker diff. This is the parity artefact — there is no per-strategy backtest representation of a portfolio-level rule.

### 2.6 Caps (D6)

- Per-ticker conviction cap (`regime_blended_sizer.py` block under `OPENCLAW_EOD_RECONCILE`/`INTRADAY_REDEPLOY`, ~:1706): skip tickers in `bench_tickers`.
- Asset-correlation cluster cap (`asset_correlation_filter`, invoked from the sizer ~:1813; live `asset_corr_cap_enabled=1`, thr 0.6, cap_pct 0.20): benchmark tickers are removed from the ticker set **before** clustering and re-inserted untouched after — they never consume cluster budget and never cause alpha releases.
- Gross rule λ·NAV (λ=1.85) is the only bound left on beta. Margin: RegT 2× on the paper account.

### 2.7 Strategy-similarity rule (D9)

`strategy_similarity.blend_similarity`: for a pair where either member is a benchmark sleeve, `sim = return_corr` when `n_obs ≥ ALPHA_FULL_OBS` (60), else the existing adaptive blend. Rationale and where it bites:
- It only affects sizing on the **SPY long side** (tangency combines same-direction contributors of one ticker). Worked pair sleeve 2.0 + timer 1.5, true ρ 0.7, shrink 0.10: return-only → S_adj 2.02; current blend → 2.09–2.14; overlap-only → 2.23. The over-credit lands directly in the beta share because SPY is exempt from the subtraction.
- Never affects: the SPY short side (sides solve separately), alpha tickers (sleeve not a contributor), the `S_m` subtraction (no correlation), the ticker-level LW cluster cap (price returns; SPY excluded).
- Grouping: factor blocks (0.40) feed γ resolution + dashboard; fold (0.85) is OFF. If fold is re-armed a ≥0.85 timer folds into the sleeve (higher Sharpe is representative) — acceptable.
- Known properties: the return leg buckets P&L on exit date, so the sleeve's series is a rolling-21-day SPY return, not daily; the backtest-overlap fallback restricts to the universe intersection, which is `{SPY}` for index-timers and empty for everything else.
- Also flows into (shadow only): per-regime LW γ estimate (P1), strategy-level HRP shadow (P2).

## 3. Rollout order and safety

1. **§2.1 R1 removal** — pure simplification; dry-run diff of assigner output before/after (expect ≤2 strategies touched).
2. **§2.2 + §2.3** — land behind flags; run `strategy_weights.rebuild(trigger='cadence_norm_removed')` shadow-first: print per-(strategy, regime) old/new `daily_weight` and the sizer replay diff of today's book; then let the daily cycle pick the new weights up.
3. **§2.4 beta sleeve** — register, backtest as a transient systemd unit outside the compute lane (13:00–20:15 UTC forbidden), promote through the normal candidate→live path (no SPY leg any more), confirm it appears in `strategy_weights_by_regime` for LOW_VOL only.
4. **§2.5–2.7 C + caps + similarity** — flag OFF with shadow log ≥ 2 daily cycles, replay diff reviewed, then flip `OPENCLAW_BENCH_RELATIVE_SIZING=1` + user-scope johnbot restart (engine inherits johnbot's dotenv).

Each step: one commit, targeted tests only (never the full suite while the fleet runs; tests reach the real DB — stub gates in fixtures), revert = flag flip. Do not stage `manifest.json` / `strategy_signatures*.json` / the foreign `daily-health-digest.js` edit.

## 4. Tests (per component)

- 2.1: assigner/promotion tests assert no `benchmark_sharpe` influence; `qualifies_regime` signature without the kwarg.
- 2.2: `_regime_weight` returns `(w, w)` by default and `(w, w/√h)` under the revert flag; consumers' fixtures updated; dashboard `_effSharpeOf` unit test.
- 2.3: factor 1.0 by default; ON path unchanged (existing tests re-scoped under the flag).
- 2.4: signal shape (one LONG SPY per bar, hold 21, non-binding levels); `benchmark_sleeve` attr default False on `BaseStrategy`; acting-gate exemption unit test.
- 2.5: pure function `apply_benchmark_hurdle(ticker_w, ticker_meta, S_m, is_bench)` — exemption, drop, sign preservation, `S_m=None` passthrough; sizer wiring test asserting byte-identical output with flag OFF; shadow-line format test; replay script test with a fixture book.
- 2.6: per-ticker cap skips bench tickers; cluster filter excludes/re-inserts them with gross unchanged for the rest.
- 2.7: blend returns `return_corr` for sleeve pairs at n_obs ≥ 60 and the blend below.

## 5. Owed / out of scope

- SSO/UPRO as a leverage knob if the λ·NAV bound ever binds beta hard (not needed under C).
- Retune `PER_TICKER_CAP_SHARPE_FRAC` after the §2.2 shadow diff if the loosened cap changes the alpha book materially.
- Dashboard: Conviction Gates card shows `S_m` per regime + `as_of`; contributions view gets an `excess` column and a `benchmark` badge.
- Memory/CLAUDE.md note: activation slider is 1.0 (not 0.5) since the 08-22 weekly cron.
