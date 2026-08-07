# Spec — Backtest ≡ Live signal-generation parity

**Status:** AUDITED 2026-08-07 (operator directive: "the main thing to verify is
that backtest signal generation matches live signal generation exactly … same
paths, same information, and re-running the backtest with the same newer
information generates exactly the same signals"). Phase 1 (determinism) SHIPPED
same day (`1acb7eb`). Phases 2–4 need operator scheduling — every structural
change below invalidates the current promotion/gating baselines and requires a
fleet re-backtest + re-gate before its numbers are comparable again.

**Grounding:** every claim verified against the working tree on 2026-08-07 by a
line-level audit of `src/execution/engine.py` (live) vs
`src/backtest/unified_backtest.py` + `src/strategies/aux_data_loader.py`
(backtest). Line numbers drift; symbol names are the stable reference.

---

## 0. Verdict

**Not the same program.** The two sides share the strategy `generate_signals`
function objects — everything AROUND that call diverges:

| Stage | Live (`engine.py`) | Backtest (`unified_backtest.py`) | Shared? |
|---|---|---|---|
| Strategy admission | DB `strategy_registry status='approved'` | manifest/file discovery | NO |
| Universe | resolver + non-equity passthrough (`build_strategy_universes`) | `static_universe` = ALL equity cols of prices.parquet for 132/140 strategies | NO |
| Prices panel | universe-pushdown read, float64, close-proxy row injected | full read, **float32**, `filter_quarantined` | NO |
| Panel handed to strategy | column-sliced to the strategy's universe | all ~12.5k columns, date-sliced only | NO |
| Aux data | parquets + Postgres sentiment, per-field construction in engine | `aux_data_loader.py` from `options_aggregates_enriched` etc. | **NO — fully duplicated implementations** |
| Regime source / gate | `regime_latest.json` + `is_eligible` per-strategy | `historical_regimes.parquet`, NO eligibility gate (discovery mode — intentional) | NO |
| generate() | same function | same function | **YES** |
| Truncation | none | `signals[:MAX_SIGNALS]` (50) | NO |

Neither side was reproducible before Phase 1: unseeded hash order (two
strategies emitted different signal SETS run-to-run), wall-clock leakage into
live aux, a wall-clock volume-frame cutoff that made one strategy backtest-blind,
and a silent prior-date fallback serving a 3-month-frozen options surface to
every recent backtest bar.

## 1. Phase 1 — SHIPPED 2026-08-07 (`1acb7eb`)

- `engine.load_aux_data(as_of=run_date)` — aux anchored to the run date, not
  wall clock (options DTE/iv_rank window, upcoming-earnings split, sentiment).
- `S23_regime_momentum` — `dict.fromkeys` replaces hash-ordered `set()` cut.
- `S_industry_momentum_moskowitz` — `sorted()` sector iteration.
- `S_ast_earnings_announcement_premium` — volume cutoff anchored to the bar
  date (was: stood down in every historical backtest, fired live).
- `aux_data_loader._day_slice` — staleness ≥5d now WARNS; opt-in
  `OPENCLAW_BT_OPTIONS_MAX_STALE_DAYS=N` refuses older slices.
- `.env PYTHONHASHSEED=0` — hash order pinned for every spawned python.
- Reviewed and CLEARED: fixed-seed RNG uses (4 sites), `_greeks_filter.py`
  `date.today()` (intraday live-snapshot triage, not the signal path).

## 2. Phase 2 — data repair (owed, independent of code)

1. **Rebuild `options_aggregates_enriched.parquet`** forward from 2026-07-28
   (frozen; every later backtest bar currently reads the 07-28 slice — loud
   since Phase 1, still stale). After the rebuild, set
   `OPENCLAW_BT_OPTIONS_MAX_STALE_DAYS=5` permanently so a future freeze makes
   strategies stand down instead of trading a dead surface.
2. This unblocks honest re-gating of the 17 options strategies (prior memory:
   repair owed before they run).

## 3. Phase 3 — structural unification (operator-scheduled; baselines reset)

Ordered by blast radius; each lands as its own epoch with a fleet re-backtest.

1. **Universe + panel parity.** Backtest adopts resolver universes and
   column-sliced, float64, calendar-on-sliced-frame panels — live semantics.
   This changes every cross-sectional rank/z-score/quantile for 132/140
   strategies; it is THE dominant divergence. Includes: quarantine filter on
   BOTH sides or neither (decide; live currently has none), shared
   `is_equity_ticker` predicate (backtest's hand-rolled copy leaks `=X` forex).
2. **One aux loader.** A single point-in-time aux construction serving both
   sides: same options source + fields (backtest lacks
   `open_interest_by_strike`/`expiry_date` → `s5_max_pain` structurally blind;
   live lacks `vol_indices`/`insider_history_long`/`recent_stop_outs` →
   `S_insider_seller_strike`, `s12_insider` cooldown degraded live), same
   iv_rank definition (live's is a 14-day percentile labelled 30-day), same
   financials as-of (live is NOT point-in-time), same insider window (live:
   full history; backtest: 45d), same sentiment store.
3. **MAX_SIGNALS.** Remove the backtest's `signals[:50]` truncation or apply
   it identically in live. Deterministic ordering (Phase 1) is prerequisite —
   done.
4. **`auto_backtest.py` third path.** The research promotion gate fabricates
   regime features from a hardcoded table and calls aux without strategy_id —
   matches NEITHER side. Converge onto the unified path or retire it.
5. **Accepted divergences — document, don't fix:** regime eligibility gate
   (backtest = discovery mode by design); close-proxy vs settled close (live
   acts at the 15:00 proxy by design; `same_close` mirrors the timing model,
   the ~86% proxy coverage and price delta are measured under §10 intensity
   re-measure); slippage/spread/entry-gate (P&L-side, not signal-side).

## 4. Phase 4 — parity harness (the regression stopper)

A runnable check: `engine.py --dry-run --date D` signal set vs a single-day
backtest signal emission for D, diffed per strategy. Green = identical sets.
Wire as a `system_checks` probe run after any change to either path. Without
this, parity rots again silently — the audit found 25 divergences precisely
because nothing ever compared the two outputs.

## 5. Answer to the operator's reproducibility question

After Phase 1: re-running the SAME code on the SAME data now yields identical
signals per side (hash order pinned, wall-clock leaks anchored, RNGs seeded) —
with one caveat: a live `--date` re-run across the close-proxy settlement
boundary still differs on the last bar (proxy vs settled close; by design in
same-day mode). Backtest-vs-live IDENTITY on the same day requires Phases 2–3.
