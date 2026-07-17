# SP-6 Phase A — Zero-Orders Diagnosis #2: Conviction Gate vs Single-Day Carried Set

**Date:** 2026-06-04 03:30 UTC (night of 06-03 ET)
**Status of last night's verdict:** 🟡 PARTIAL (2026-06-03 20:40 UTC) — 0 submissions, health green, 574 COMPUTED for 06-04
**Predecessor doc:** `/root/sp6_phaseA_trade_gap_diagnosis_2026-06-02.md` (driver `read_handoff→None→rc=1` bail — FIXED, merged `c49011e` 08:13 UTC 06-03)

---

## 1. What happened on 2026-06-03

The 06-02 driver fix **worked**. The 3:55pm ET `eod-into-close-fill` cron ran clean for the
first time: `status=ok aborted=none` (vs 06-02's `aborted=trade`). The EOD branch in
`regime_blended_sizer_live.py:main()` synthesized the minimal handoff and called
`size_positions`, which self-loaded the APPROVED carried set.

**And then sized it to zero.** Evidence chain:

| Evidence | Value | Implication |
|---|---|---|
| langgraph checkpoint `daily-cycle:2026-06-03` | trade step rc=0, **durationMs=995** | ran + exited clean in under 1s |
| `output/handoffs/` | **no `2026-06-03_sized.json`** (last: 05-28) | driver returned at the `if not orders:` exit (line ~446) before `finalize_sized_payload` |
| 995ms duration | real confirmer = per-proposal `claude-bin` LLM calls (seconds each), runs in LOW_VOL | **zero proposals ever reached the confirmer** — the book emptied before line 1057 |
| `alpaca_submissions` run_date 06-03 | 0 rows | alpaca step (365ms) found no payload → no-op |
| `execution_signals` 06-03 | 84 APPROVED (never executed), 8 FILLED (reconcile-marks) | carried set passed the 9:15 gate but died in the sizer |

The only `return []` exits in `_sharpe_cadence_path` that precede payload-writing are:
empty weights, empty `active`, **the min_cum_sharpe gate emptying `ticker_w` (line ~933)**,
or zero gross. A read-only repro (below) shows the cum-sharpe gate is the operative one.

## 2-PRE-2. ✅ RESOLVED (05:10 UTC) — trigger CONFIRMED = operator's 4.0 gate × correlation deflation

Operator hypothesis ("correlation sizer clumps highly correlated strategies + I had myself
increased the gate to 4.0 on LOW_VOL") **confirmed quantitatively**:

- `regime_sizer_params` rows were edited via the dashboard API (`server.js:765`) at
  **03:01 UTC 06-04** (23:01 ET — all 4 regimes, sequentially). LOW_VOL now reads 3.0;
  at 19:55 06-03 it was the operator's **4.0**.
- Deflated gate values on the faithful 06-03 set: **MU 9.58→3.57** (7-strategy momentum
  clump deflates 2.7×), **STX 5.49→3.51**, INTC 5.99→2.18. Both survivors sit in
  **[3.0, 4.0)** ⇒ at gate=4.0 **kept=0** → `return []` (line ~933) → 0 orders, rc=0.
  At gate=3.0 (post-edit) kept=2 — exactly why the replay emitted 7 while live emitted 0.
- Explains both zero-runs (17:05 + 19:55) AND the repro divergence. §2-PRE's archaeology
  stands as the ruled-out list; the un-replayed mutable input was `min_cumulative_sharpe`
  itself (the repro read the row 4 minutes after it changed).

The stdout-persistence patch (§7) remains recommended — this took hours to pin down
because the step's own log line (`dropped N tickers below min_cum_sharpe=4.00 (kept=0)`)
was discarded.

## 2-PRE. ADDENDUM (03:55 UTC, superseded by 2-PRE-2) — exact 06-03 trigger was UNCONFIRMED

A **faithful replay** (APPROVED ∪ the-8-later-FILLED rows = the 19:55 set, same weights
[stable since 05-31 11:40 UTC — verified], same regime [LOW_VOL all afternoon — verified],
same tradable universe, same `.env` content [mtime 06-01 16:25 — unchanged]) produced
**7 orders** (kept=2: MU $128k + STX $96k opens + 5 closes). Live produced 0 — **twice**
(17:05 UTC redeploy trade 524ms AND 19:55 EOD trade 995ms, both rc=0, no payload). So the
conviction gate is NOT proven to be yesterday's trigger; one of the four pre-confirmer
`return []` exits fired (`active` empty @802 / weight-filter @901 / cum-sharpe @933 /
gross≤0 @966), but which one cannot be recovered — **the graph discards step stdout on
rc=0** (`daily_cycle_node.js` persists stderr only on FAILURE), so the breadcrumbs are gone.

Ruled out: `PIPELINE_DRY_RUN` (absent from .env; 06-02's rc=1 abort under the same PID
proves no `--dry-run` in argv), weights drift, regime flap, λ=0 (DB-backed, clamped ≥0.10),
stale driver code (merged 08:13, prefire confirmed armed on disk at 13:50).

Materially: **johnbot restarted 21:54 UTC 06-03** (SP-5 deploy; old PID 2095273 → 2690671).
Both zero-runs happened under the OLD process. Today's 19:55 run executes under a fresh
process whose context the replay matches — so today should behave like the replay (emit).
If it zeroes again on a fat day, the divergence is in the johnbot-subprocess context
specifically, and the stdout-persistence patch (§7) becomes the diagnostic.

§2 below stands as the **structural** finding (thin sets vs accumulation-calibrated gate
— robust regardless of yesterday's exact trigger; the 06-02 probe kept 14 on 550 rows,
the 06-03 set keeps ≤2 on 92). It is no longer claimed as the proven 06-03 trigger.

## 2. Structural finding — conviction gate vs single-day carried set

*(Structural — robust; no longer claimed as the proven 06-03 trigger, see §2-PRE.)*

`regime_sizer_params.min_cumulative_sharpe` (LOW_VOL = **3.0**; the operator's primary
conviction filter, line 919-931 of `regime_blended_sizer.py`) was calibrated for the
**legacy cadence-window loader**, which accumulated signals per ticker across each
strategy's multi-day cadence window (multi-strategy stacking prevalent; STX had 8
contributions on 05-28). Per-ticker net sharpe routinely cleared 3.0.

The SP-6 EOD carried set is **one day's gated emission**: read-only repro on the 06-03
APPROVED set (today's weights — see caveat §3):

- 37 tickers, **avg 1.30 contributions/ticker**
- raw |net_sharpe| ≥ 3.0: **2/37** (INTC 5.99, STX 5.49 — the only 4-contrib names)
- after orthogonalization deflation: **1/37** (STX)
- next-best tickers: 1.34, 1.16, 0.97 … nowhere near 3.0

Same gate, ~4× thinner substrate ⇒ the gate now blocks ~95–100% of the book **every day**.
On a 0-survivor day the sizer exits `return []` *before* the netting step, so not even
closes emit (benign — carried book just sits). Yesterday was a 0-survivor day.

**Why undetected:** the 06-02 fix unblocked the driver; yesterday was the first time the
full library EOD path executed live. Rollout Steps 2–4 (shadow/dry-run) were skipped, so
each blocker in the chain only surfaces when the previous one is removed.

## 3. Honesty caveat on the repro

The repro is **not a faithful replay** of 19:55 UTC 06-03: it used (a) today's
`load_current('LOW_VOL')` weights, (b) the now-84-row APPROVED set (8 rows flipped FILLED
at 20:26 are excluded), (c) today's 5 broker positions. "kept=1 (STX)" is today's number.
The *structural* finding (1.3 contribs/ticker; 2/37 clear 3.0 raw) is robust to those
deltas; the specific yesterday-exit at line 933 is best-supported inference, independently
corroborated by the 995ms no-confirmer timing.

## 4. ⚠️ THE BIGGER FINDING — concentration on few-survivor days

The same gate that zeroes most days produces this on a 1-survivor day (repro output, real
account data):

```
targets=1, broker=5, emissions=6 (flips=0)
STX  open_long  target_usd=224,809.54     ← λ×NAV = 2.0 × $112,404 equity
+ close orders for all 5 held positions   ← held-but-unsignalled → closed
```

Because the normalization is `Σ|target_usd| = λ × NAV` **regardless of survivor count**
(no per-ticker cap — explicitly accepted for the *many-ticker* legacy book), a
one-survivor day allocates **2× equity to a single name** *and* flattens the rest of the
book into it. `alpaca_executor.py` line 11: *"no per-order or aggregate notional caps"* —
the only practical brake is broker buying power (RegT BP $198k < $225k ⇒ likely broker
reject or partial). This path is live-armed and untested.

A 0-order day is paper-benign. A 1-survivor day is the dangerous one.

## 5. Weights coverage — INVESTIGATED (operator request, 06-04 05:10 UTC)

The weights funnel (`strategy_weights.rebuild`): manifest state ∈ {live,monitoring} →
`strategy_regime_params.eligible=TRUE` for the regime (**fail-closed**: no row ⇒ skip) →
effective sharpe (bt/live blend) finite **and > 0**. Rebuilt ONLY by the weekly timer
`openclaw-weekly-strategy-weights.timer` (**Sun 10:00 UTC**); current batch = 05-31.

**Finding 1 — staleness (main cause).** 6 strategies pass every filter TODAY but are
absent from the 05-31 batch (their backtest rows / promotions landed mid-week):
`S24_52wk_high_proximity` +0.52, `S_HV8_gamma_theta_carry` +0.89, `S_btc_momentum` +1.65,
`S_idiosyncratic_vol_puzzle` +0.66, `S_macro_risk_momentum_ip_beta` +0.50,
`S_news_sentiment_long_short` +0.88. Read-only rebuild simulation: **fresh batch = 30
strategies** (matches the operator's ~29) — would also **DROP `S12_insider`** (live
sharpe −0.90 drags its blend negative; it was in 05-31 via the regime-agnostic override).

**Finding 2 — eligible=FALSE on positive-sharpe strategies.** 4 actives have positive
LOW_VOL bt sharpe but `eligible=FALSE` (excluded even by a fresh rebuild). Audit trail
(`strategy_regime_param_changes`):
- `S_vp_macd_index_sensitivity` (+0.05) and `S_reversal_momentum_transition_earnings`
  (+0.01) — set by **operator:Sid via strategies-page 05-30** (deliberate; sharpe ≈ 0).
- `S_growth_inflation_sector_timing` (+0.24) and `S_price_earnings_momentum_drift`
  (**+0.69, n=13,919**) — set by **`dashboard_picker` automated default** ("picker set
  eligible=false (transition candidate→live)") — a promotion side-effect, not a judgment.
  Operator review recommended, esp. the +0.69.

**Finding 3 — engine/sizer population divergence (structural).** The engine emits signals
by `strategy_registry.status='approved'` (fail-open on regime), while the sizer weights
by manifest ∩ eligible=TRUE ∩ sharpe>0 (fail-closed) ⇒ APPROVED-but-unsizeable rows every
day. Extreme case: `S_regime_specialist_vol` is **manifest=candidate** yet
**registry=approved** — it emits signals daily with LOW_VOL bt sharpe **−3.43** (correctly
unsized, but it shouldn't be emitting; the [[feedback_manifest_vs_registry_execution_gate]]
divergence, inverse direction). Worth a registry↔manifest sync pass.

**Remediation — EXECUTED 06-04 ~05:25 UTC (operator-approved):**
- (a) ✅ `strategy_weights.rebuild(trigger='manual')` — LOW_VOL **25 → 31** strategies
  (TRANSITIONING 32 / HIGH_VOL 39 / CRISIS 46; 148 rows total). The 6 stale-missing all
  in; `S12_insider` dropped (live blend −0.90), as flagged.
- (c) ✅ `S_price_earnings_momentum_drift` LOW_VOL `eligible=TRUE` via
  `eligibility_manager.set_params` (audited; actor `operator:Sid (via BotJohn)`).
  Operator chose to LEAVE `S_growth_inflation_sector_timing` (+0.24) off.
- (d) ✅ `S_regime_specialist_vol` registry `approved → pending_approval` (status flag
  only; stops emissions from TONIGHT's 16:15 compute — today's already-COMPUTED rows from
  it remain, gate-pass but unsizeable as before, harmless).
- (b) moot. Operator also chose **let-it-run for today**.

**Post-remediation projection for today's 3:55 ET fill** (06-04 COMPUTED set, new weights,
gate 3.0, after deflation): 173 tickers → **35 survivors** (top: STX/WDC 8.2, VRT 8.2,
GLW 7.8, AMD 7.7, MU 7.4 …) ⇒ well-diversified first-fill day, no concentration risk
profile.

## 8. ✅ Per-ticker conviction cap — BUILT + LIVE (06-04 ~06:00 UTC, commit `91a3272`)

Operator formula: **`cap_usd(t) = 0.05 × |gate_net_sharpe(t)| × λ × NAV`**, implemented
in `_sharpe_cadence_path` (after the min-notional renorm, before the cap-exempt hedge
injection + broker netting), module constant `PER_TICKER_CAP_SHARPE_FRAC = 0.05`.
Design points: (1) uses the SAME conviction value the gate uses (deflated when
`OPENCLAW_STRATEGY_CORR_WEIGHT` on, raw otherwise — self-consistent); (2) **no
renorm-up** — shaved capital is not redistributed, capped-day gross lands below λ×NAV;
(3) rides `OPENCLAW_EOD_RECONCILE` ⇒ legacy path byte-identical; (4) missing gate value
→ fail-open. TDD: 6 tests `tests/test_sizer_per_ticker_cap.py` (single-survivor clamp,
fat-day no-op, no-renorm-up, short symmetry, deflated-value source, legacy-uncapped) +
41 sizer-suite regression green. **Faithful 06-03 thin-day replay with cap: MU $125k →
$38.7k, STX $99.6k → $37.9k** (vs the uncapped $224.8k single-name case). Fat-day (06-04)
projection unaffected (cap ≈ $92k vs ~$15-25k proportional targets — not binding ⇒ no
confound for today's first-fill test). Live on disk immediately (cron re-reads the file);
NOT pushed. §4 hazard CLOSED.

## 9. ✅ CORR_WEIGHT verified live + similarity matrix re-synced (06-04 ~06:20 UTC)

Operator: "make sure OPENCLAW_STRATEGY_CORR_WEIGHT is flipped live and active and
Cum_Sharpe is automatically adjusted by it."

- **Gate live:** `OPENCLAW_STRATEGY_CORR_WEIGHT=1` in `.env` (line 144) AND in the
  RUNNING johnbot process env (PID 2690671, restarted 21:54 UTC 06-03) — today's cron
  subprocesses inherit it. (`FOLD`/`ORTHO_SHADOW`/`BRACKET_STACK` also =1.)
- **Automatic adjustment:** when the gate is on, `gate_net_sharpe` is REPLACED by
  `orthogonalization.deflated_net_sharpe(...)` (sizer ~line 914) and BOTH the
  min_cum_sharpe gate AND the per-ticker cap read that same dict — wiring proven by
  `test_cap_uses_deflated_value_when_corr_weight_on` + live projection (STX raw 12.54 →
  deflated 7.27).
- **De-sync found + fixed:** the similarity matrix rebuilds on the SAME Sunday job as the
  weights (`weekly_live_sharpe.js` → `strategy_similarity --rebuild`), so the manual
  weights rebuild left the matrix stale (05-31; only 15/31 weighted strategies covered).
  **Ran `strategy_similarity.rebuild(trigger='manual')`** — LOW_VOL matrix 34→41
  strategies, coverage now **22/31**, blocks 6→8. The 9 still-uncovered (btc/insider/
  low-co-firing strategies) are singleton blocks = no deflation = raw credit — the
  DOCUMENTED fail-open for names lacking 90d co-firing/returns overlap; they enter the
  matrix as data accrues. CRISIS matrix is empty (no CRISIS days in window) ⇒ deflation
  skips there (pre-existing).
- **Final 06-04 projection (new weights + new matrix, gate 3.0):** 173 tickers →
  **28 survivors** (top deflated: STX/WDC 7.27, VRT 7.17, GLW 6.81, AMD 6.72, MU 6.38);
  caps $67-82k vs ~$8-25k proportional targets ⇒ cap not binding; diversified.
- Lesson encoded: a manual weights rebuild should ALWAYS be paired with a similarity
  rebuild (the Sunday job runs both back-to-back).

## 6. Downstream impact

- **Phase A**: live but functionally not trading — 0 opening submissions since 05-28.
- **B0/B1**: still moot (`alpaca_submissions` empty; B0 finalize only UPDATEs existing
  rows, B1 live_shadow reads it). Activation runbook remains DO-NOT-EXECUTE.
- **B2**: build premise (B1 live + Phase A executing) unchanged — still on hold.
- **No destructive path armed**: 574 COMPUTED for 06-04 ⇒ the 9:28 reconcile flatten
  branch is not armed; the sizer's empty exit precedes close-emission.

## 7. Decision menu (operator-owned; two independent levers + one clock)

**⏰ Decision with a clock — but tomorrow's profile is the OPPOSITE of yesterday's.**
Pre-gate projection on the 06-04 COMPUTED set (574 rows): **≥20 tickers clear 3.0 raw**
(MU/STX/WDC 13.4 @ 10 contribs, INTC/TER 11.9 @ 9, AMD 10.5 …) — a fat-set day like
06-02 (whose probe kept 14). Set size swings wildly day to day (566 → 92 → 574); 06-03
was a thin day, 06-04 is fat. So tomorrow at 3:55 ET (19:55 UTC) is most likely the
**first real multi-order into-close fill** with diversified targets (λ×NAV spread over
~15–25 names ≈ $9–15k each), NOT a concentration event. Options:
- **(a) Let it run + watch** (recommended profile-wise): first real fills finally populate
  `alpaca_submissions` → unblocks B0/B1 verification; `sp6-fill-verify` @20:40 UTC posts
  the verdict. The §4 concentration hazard stays LATENT for the next thin day — fix it
  deliberately before one arrives.
- **(b) DISARM before 13:15 UTC** — rollback recipe from the verify message: set
  `OPENCLAW_EOD_SIGNAL_REGISTER/PREMARKET_GATE/RECONCILE=0` + `OPENCLAW_CLOSE_EXEC_LIVE=1`
  in `.env`, restart johnbot (operator approval required for restart). Costs another day
  of Phase-B accrual.
Caveats on (a): the 9:15 gate can still veto; a regime flip raises the floor (HIGH_VOL=5.0);
deflation lowers some names — but 13.4 doesn't deflate below 3.0.

**Lever 1 — threshold/statistic (should it trade more?):**
- A. Recalibrate `min_cumulative_sharpe` for EOD mode (per-mode value or scale by
  contribution count).
- B. Gate EOD mode on a different statistic (e.g., mean sharpe per contribution).
- C. Accept sparse trading (only 3.0+ conviction days) — but this starves Phase B's
  fill-ledger accrual indefinitely.
- D. Make the EOD register accumulate multi-day signals (mirrors the legacy cadence
  window) — bigger redesign.

**Lever 2 — concentration cap (should one name ever get 2× equity?):**
- Needed *independent* of Lever 1: thin sets ⇒ few survivors ⇒ renorm concentration.
  A per-ticker cap (or "scale down, don't renorm up" rule) in EOD mode only, gate-guarded,
  legacy path byte-identical.

**Also:**
- Weights-coverage fix for the 5 unsizeable strategies (§5).
- **Observability patch — ✅ DONE 06-04 04:31 UTC (operator-authorized, commit `123c4e5`):**
  `daily_cycle_node.js` now appends a header (step/rc/duration/runId) + 4k stdout tail to
  `logs/daily_cycle_steps_<runDate>.log` on EVERY step completion (rc=0 included); the
  abort-stderr path is byte-unchanged. TDD: `test/daily-cycle-stdout-log-smoke.js`
  (rc=0 persisted, rc=2 stdout+stderr both persisted, 50k stdout tail-bounded <10k).
  johnbot restarted 04:30:48 UTC (PID 2740474, cron re-registered, no mutual-exclusion
  throw, gates verified in process env, NRestarts=0). Tonight's 19:55 trade step will be
  the first run with its stdout on disk.

All fixes are deliberate/TDD per standing practice; none should be made silently — the
conviction filter and the concentration trade-off are both explicitly operator-owned.

## 10. ⚠️ NEW FINDING (06-04 ~15:20 UTC) — EOD compute runs on close[T−1], not close[T]

Operator asked to confirm survivors were recomputed off previous-EOD prices after the
process redo. Verification (read-only replay `/root/sp6_survivor_projection.py` using the
sizer's own loader/fold/deflation/gate functions against the live APPROVED set):

- **Survivor membership: CONFIRMED.** 574 APPROVED rows (target_date=06-04, promoted
  574/574 at the 9:15 gate) → 173 tickers → **28 survivors @ gate 3.0** with current
  weights + re-synced matrix. Top deflated: STX/WDC +7.30, VRT +7.16, GLW +6.81,
  AMD +6.71, MU +6.42 (±0.04 vs the §9 COMPUTED-set projection — same set).
  `regime_sizer_params.LOW_VOL.min_cumulative_sharpe=3.0` (operator edit 03:01:40 UTC).
  Weights note: current LOW_VOL batch = **32** strategies — a `lifecycle_change` rebuild
  at 04:22 UTC superseded this session's 31-row manual batch, adding
  S_commodity_etp_momentum (promoted by the parallel SP-5 session). The replay used
  `load_current`, so the 28 already reflect it.

- **Price basis: close[T−1], not close[T] — STRUCTURAL (SQL-proven), if Phase A's
  intent was close[T] parity.**
  Every APPROVED row — decisively including the 40 FRESH INSERTS at 20:26:33,
  post-close — carries entry_price = the **06-02 close** (MU 1064.10 vs 06-03 close
  1079.57; ADBE 262.11 vs 256.24; GIS 33.07 vs 32.17). The 06-01 EOD compute produced
  same-day-close entries (MU 1034.74), ruling out any strategy-side shift(1).

  **Mechanism (proven in code, not inferred from timing):**
  1. The 16:15 ET cycle collect (`runDailyCollection`) pre-scans gaps via
     `store.getGapSummary`, which marks a ticker `covered` when
     `data_coverage.date_to >= yesterday` — so a ticker current through T−1 is
     EXCLUDED from the price phase **by definition**. The in-cycle collect can never
     fetch today's close (06-03: only 5 stale stragglers updated @20:15).
  2. Same-day close capture belongs EXCLUSIVELY to `openclaw-eod-refresh.timer` at
     **16:30 ET** (`run_collector_once --eod-only` → `runEodRefresh`, which bypasses
     the pre-scan; 06-03: 421 tickers @20:30:01–20:30:52). The timer predates Phase A
     ("EOD bars the 10am cycle structurally cannot capture").
  3. The engine finished 20:26:33 — **4 min before** the closes landed. Phase A placed
     the compute BEFORE the only close-capture process; the ordering was never re-checked.
  06-01's freshness came from an out-of-band panel refresh that evening (not
  attributable from upsert-only data_coverage); the SQL rule makes T−1 the default.

  **Per-run nuance (don't over-read it):** of the 574 rows, 534 originated from the
  two intraday redeploys (10:10/13:05 ET) — using the 06-02 panel intraday is CORRECT
  (the 06-03 close didn't exist yet). Only the 16:26 post-close compute genuinely
  missed close[T], by 4 minutes. Net informational effect on today's fills is the same
  (no 06-03 information anywhere in the set), but the bug is one run's ordering, not
  three. Corollary: fix A alone re-runs strategies on close[T] (fresh emissions +
  bracket refresh on re-emits) yet intraday-originated rows NOT re-emitted post-close
  stay in the target set on their stale basis — full closure needs the gate or the
  upsert to require/prefer post-close re-confirmation (design decision, operator's).

  **Impact (conditional on intent):** live decision basis = close[T−1], fill =
  close[T+1] ⇒ one day staler than the t+1 backtest model (signal close[t] → fill
  close[t+1]). Momentum: modest drag. Short-horizon reversal strategies
  (S_tr_06_eod_reversal, S_extreme_intraday_reversal_nasdaq): can flip sign. Brackets
  likewise anchored off T−1 (parity_mark re-anchors at fill, mitigating).

  **Fix options (operator decision; nothing changed today — first-fill test must stay
  unconfounded):**
  - A. Move the EOD compute cron 16:15 → ~16:45 ET (after the refresh timer); keep
    everything else. Smallest diff; verify 16:45 still clears the option_hedge step
    before any downstream consumer.
  - B. Freshness gate inside the 16:15 cycle: collect blocks/retries until
    max(panel date) == run_date for ≥N% of the equity envelope, then signals run.
    More robust (also covers refresh-timer failures), more code.
  - C. Detection-only: eod_compute_health gains panel_max_date; healthy ⇒
    panel_max_date == run_date. Cheap, catches it forever; pairs with A or B.

  Note: today's 3:55pm ET fill executes the 06-02-basis signals regardless — the gate
  verdict tonight should be read with that in mind (it tests the EXECUTION path, not
  signal freshness).

## 11. ✅ OPERATOR-DIRECTED FIXES EXECUTED (06-04 ~16:20 UTC) — B + C + today's recompute

Operator: "Implement fix B with C detection immediately so that 3:55 fill can execute
based on 06-03 signals as desired. We do not need eod-refresh or even sod refresh…
make sure collection is actually successful at 16:15."

**1. Today's 3:55 fill → 06-03-close basis (DONE, time-critical path):**
- engine.py had a latent interface bug: resolve_script has always passed `--date`,
  main() ignored argv (`run_date = date.today()`). Added `_parse_run_date` (TDD, 4 tests).
- Ran `engine.py --date 2026-06-03` 15:54 UTC (proxy OFF override): 62 strategies,
  **289 fresh COMPUTED rows carrying exact 06-03 closes** (MU 1079.57 ✓ STX 940.69 ✓
  WDC 594.11 ✓), target_date=06-04. Re-ran premarket gate: **289/289 APPROVED**.
- Refreshed set: 863 APPROVED rows → 284 pre-gate tickers → **44 survivors @3.0**
  (CIEN +8.16 NEW-on-06-03-info, STX/TER +7.48, VRT/INTC/WDC/COHR/MU…). Union
  semantics: the 574 earlier 06-02-basis rows remain (no retraction mechanism);
  membership now includes all fresh 06-03 emissions. parity_mark re-marked LEN at the
  TRUE 06-03 close (last night's 8 marks used 06-02 closes — known wart, B0 territory).

**2. Fix B — 16:15 collect captures close[T] (commit `6a31a60`, live for tonight, no restart needed):**
- `getGapSummary` gains `pricesRequiredThrough`; post-close on a trading day the
  equity requirement = TODAY (was: yesterday ⇒ structurally never fetched today).
- `_eodFreshnessContext`: ≥16:05 ET + weekday + `alpaca calendar` probe (holiday →
  legacy; probe failure → fail-LOUD require). Live-verified: 06-04→require,
  07-03 (July-4 observed)→legacy.
- `_verifyEquityFreshness` after phase 2a: bounded retries (3 attempts/45s) on
  stragglers; <`OPENCLAW_EOD_FRESHNESS_MIN_FRAC` (0.95) ⇒ THROW ⇒ collect rc≠0 ⇒
  strict-mode aborts the cycle BEFORE signals. data_coverage is an honest oracle
  (updateCoverage refuses 0-row advances).
- TDD: `test/collector-eod-freshness-smoke.js` ALL PASS; collector/store load clean.

**3. Fix C — stale-panel detection (same commit; migration 130 APPLIED):**
- `eod_compute_health.panel_max_date` (PRE-proxy parquet max — a close-proxy row
  can't mask a failed capture); `healthy=false` when a post-close compute
  (`_panel_fresh_required`: run_date==today-ET ∧ ≥16:05 ET ∧ trading session) sees
  panel_max_date < run_date; unknown-when-required fails CLOSED. Intraday redeploys +
  historical re-runs exempt. 5 TDD tests + legacy-shape regression (31 green total).
- Prefire watchdog now prints panel_max_date + PANEL STALE warning.

**4. Decommission (operator-authorized) — STAGED, not yet executed:**
- No SOD timer exists ("SOD" is a legacy label in run_collector_once). The 20:52 UTC
  checkpoint verifies tonight's compute shows panel_max_date=2026-06-04 + healthy=true,
  THEN runs `systemctl disable --now openclaw-eod-refresh.timer`. If tonight is RED the
  timer stays (sole close-capture fallback) and the failure is reported.

**5. OPEN — coverage seed for tonight's first expanded-universe collect:**
- 163/536 active equities have NO data_coverage row (today's SP-7/SP500 backfills wrote
  parquet + backfill_audit, never data_coverage — split-source pattern again). Tonight's
  phase 2a will re-fetch their FULL history (~10-20 min, wasted bandwidth, dedup on
  write) before the freshness gate; self-healing after one night. The truthful
  parquet-derived coverage UPSERT was BLOCKED by the permission classifier (canonical
  table) — operator decision pending. Either answer is safe; seeding makes tonight
  uniform 1-day fetches.

**6. Residual hazards flagged:**
- `OPENCLAW_CLOSE_PROXY_SNAPSHOT=1` still in live johnbot env (superseded close-exec
  leftover). Demonstrably did NOT inject usable prices on 06-03 (all three batches
  carry exact 06-02 parquet closes). Harmless once fix B works (today-row present ⇒
  injection skips); recommend flipping to 0 in .env at next restart.
- Commits local-only: 91a3272, 123c4e5, 6a31a60 — push needs operator approval.

## 12. 🔴→✅ DB BTREE CORRUPTION found during coverage seed — REPAIRED (06-04 16:52 UTC, operator-approved)

The operator-authorized coverage seed exposed it: post-seed verification returned
impossible results (W–Z tickers "behind" despite correct rows; 531+22>536), then
`mergejoin input data is out of order`. amcheck over 31 btrees on trading-critical
tables → **4 corrupt**: `data_coverage_pkey`, `universe_config_pkey`,
`execution_signals_…_strategy_id_signal_date_ticker_direction_key` (the engine's
upsert UNIQUE key), `idx_signal_pnl_unreported`.

Mechanics: rows fell OUT of the btrees (~2026-04-25 01:56 UTC, alphabetic W–Z/XL*
tail — interrupted index write; box has OOM history) ⇒ index-plan reads missed them
(plan-dependent truth) AND ON CONFLICT upserts inserted byte-identical PK twins
(91 in data_coverage incl. my seed's, 21 in universe_config, 97 in execution_signals
on high-churn days 05-13/20/22).

**Repaired now (audit: /root/db_corruption_repair_2026-06-04.log):** data_coverage
91 twin groups merged (kept newest, span widened) + 91 dead twins deleted;
universe_config 21 identical twins deleted (533/533 distinct); REINDEX ×3 + amcheck
green. Coverage ledger now truthful: only BF-B (no data) + BK (gap since 05-20)
behind — tonight's collect handles both.

**Deferred to tonight ≥21:00 UTC (operator-approved, in the 20:52 checkpoint):**
execution_signals dedupe (97 historical tuples, ZERO in today's APPROVED set; keep
signal_pnl-referenced twin, skip-and-report if both referenced) + REINDEX of the
unique key. Today's 19:55 fill was never exposed: all sizer read-path indexes clean
+ `DISTINCT ON` collapse + zero dups in today's set.

**Standing defense recommended:** nightly amcheck system_check (storage tag).

## 13. 🔴 CARRIED-SET RE-EMISSION GAP — quantified 06-05 ~04:00 UTC (operator-prompted overlap check)

Operator suspicion CONFIRMED on both counts:

**1. Non-repeating positions were NOT closed before/with submission.** The 06-04 sized
payload sequenced its 4 close orders (AAPL, LEN, PTC, SPGI=10.8% NAV) LAST of 48; the
executor crashed at order ~18 (dtbp-coid bug, since fixed 201a9ae) ⇒ closes never
attempted ⇒ all 4 stale positions held overnight. Self-heals at today's 9:28 reconcile
(all 4 absent from the 06-05 set). **Sequencing flaw independent of the crash: closes
should execute FIRST** (free BP + cut gross before opens — yesterday's DTBP exhaustion
might not have happened with closes up front).

**2. Structural one-day max-hold.** Held book (16) ∩ 06-05 set (101 tickers) = **2**
(CSGP, TTD). The 9:28 reconcile will close **14/16**, including 10 of yesterday's 12
brand-new shorts — ~83% daily churn. Root mechanism (proven on ACN/ARE/NOW): filled
rows stay status='open' (until pnl closes), so `write_signals`' upsert matches them and
bracket-refreshes — **re-emissions can never mint a row for the NEXT target_date**.
A ticker is locked out of subsequent target sets while its position is open ⇒
continuation impossible by construction; legacy cadence_days multi-day-hold semantics
were silently lost in the Phase-A redesign.

**Recommended fix (operator decision — fits the SP-6 completion session, BEFORE B0/B1
metrics become load-bearing):** in `write_signals`, when the matched open row's
target_date < the new _next_td (row is spent), INSERT a fresh COMPUTED row for the new
target instead of bracket-refreshing. Preserves audit; restores continuation; the gate
then re-approves carried conviction daily. Secondary: emit closes FIRST in the sized
payload ordering.

Today's practical effect if left as-is: 14 positions close at the open (4 stale =
correct; 10 = one-day round-trips, ~$42k notional churn); today's 3:55 sizes the 06-05
set fresh (CIEN/AMAT semis longs re-appear — yesterday's never-attempted long book gets
its second chance with the coid fix live).
