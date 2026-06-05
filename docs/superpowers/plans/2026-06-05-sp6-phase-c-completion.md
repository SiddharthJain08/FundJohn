# SP-6 Phase C — Continuation Fixes + Activation + Completion

**Date:** 2026-06-05 · **Branch:** `feat/sp6-phase-a-eod-open-execution` (pushed ..201a9ae)
**Predecessors:** Phase A live (first fills 06-04), B0 built (`feat/sp6-phase-b0-fill-persistence`, mig 127 applied), B1 built (`feat/sp6-phase-b1-execution-scheduler`, mig 128 applied), B2 designed (07441ad, build-gated on B1 live).
**Handoff:** `/root/sp6_completion_handoff_2026-06-05.md` · Diagnosis: `/root/sp6_phaseA_conviction_gate_diagnosis_2026-06-04.md` §13.

**Architecture scoping (operator, 2026-06-05):** in the FINAL sequencing, close-at-open
followed by optimal order submission happens BY CONSTRUCTION (9:28 reconcile + B1/B2
intraday scheduler). The 06-04 errors live only in the INTERMEDIATE step (3:55 dump).
Task 2 is therefore a minimal stopgap; Task 1 is the durable change.

---

## Part 1 — Pre-market fixes (subagent-TDD, sequential, ~2-3h)

### Task 1: `write_signals` continuation mint (durable fix)

**Problem (proven):** filled rows keep `status='open'` until pnl closes them; the upsert in
`engine.py:write_signals` matches `(strategy_id, ticker, status='open')` and bracket-refreshes
the SPENT row (target_date already consumed) — re-emissions never mint a row for the next
target_date ⇒ held tickers locked out of subsequent target sets ⇒ structural 1-day max-hold
⇒ 06-05: 9:28 closes 14/16 positions (10 = pure churn).

**Change (engine.py, upsert branch, ~line 970):** when ALL of:
- `_eod_signal_register_gate_on()` (legacy gate-off path stays byte-identical),
- existing row matched on `(strategy_id, ticker, status='open')`,
- `existing.target_date IS NOT NULL AND existing.target_date < _next_td` (row is spent),

then **do NOT bracket-refresh**; instead INSERT a fresh row (gate-ON INSERT shape:
`lifecycle_state='COMPUTED', computed_at=now, target_date=_next_td`, signal_date=run_date).
No unique-key collision: new row has a new signal_date. Keep the geometry guards on the
INSERT path (NaN + degenerate brackets) as they are.

**Decision D1 — double-pnl mitigation (recommended, implement with the mint):** the spent
row keeps tracking the live position (pnl continuity). When the CONTINUATION row is marked
FILLED by `parity_mark.finalize_parity_marks` while the same ticker's older spent row is
still `status='open'`, close the older row: `status='closed', close_reason='rolled_continuation',
closed_at=now` (in signal_pnl semantics, via the existing close path — never DELETE).
Otherwise one broker position carries two live pnl series and by-ticker reports double-count.

**Consumers audited (verify in tests, don't assume):**
- `_load_approved_carried_signals` — `DISTINCT ON (strategy_id, ticker)` already collapses multiples ✓
- `open_reconcile` — continued ticker now IN the target set ⇒ not closed ✓ (the point)
- `update_pnl` / OUE classifier — D1 prevents double-series
- dashboard positions view — spot-check rendering with rolled rows

**Tests (red→green, `tests/test_write_signals_continuation.py`):**
1. spent open row + gate ON + re-emission ⇒ NEW row minted (COMPUTED, target=_next_td), old row untouched (entry/signal_date frozen)
2. UNSPENT open row (target_date == _next_td, same-evening re-run / intraday redeploy idempotency) ⇒ bracket-refresh only, NO dup mint
3. gate OFF ⇒ byte-identical legacy upsert (no mint, refresh as before)
4. no unique-key violation across consecutive-day mints (3 days, same strategy/ticker)
5. D1: parity-mark fill of continuation row closes the spent sibling (`rolled_continuation`)
6. regression: `tests/test_sp6_eod_compute_health.py`, `test_engine_run_date_arg.py`, parity suite

**Deploy:** commit local → operator approval → push. No restart needed (engine is per-dispatch subprocess). Lands before tonight's 20:15 UTC compute ⇒ tonight mints Monday-target (06-08) rows for held tickers.

### Task 2: closes-first sized-payload ordering (intermediate-step stopgap)

**Change (one site):** in `regime_blended_sizer._sharpe_cadence_path` (or `_build_sized_payload`),
sort the final orders list `close_*` before opens before hedges. Nothing else — the final
architecture supersedes this by construction (see scoping note).

**Tests (`tests/test_sized_payload_close_ordering.py`):**
1. mixed payload ⇒ all `close_*` precede all opens
2. legacy gate-off path byte-identical (ordering applied only under `OPENCLAW_EOD_RECONCILE=1`, mirroring the cap's gating)
3. regression: `tests/test_sizer_per_ticker_cap.py` (41+6)

### Task 3 (15 min, env hygiene, with operator):
- `.env`: `OPENCLAW_CLOSE_PROXY_SNAPSHOT=0` (superseded close-exec leftover; proven misbehaving-but-inert; fix B owns close capture now). Takes effect at next johnbot restart — bundle with the Phase-C restart below.

## Part 2 — Phase C activation (operator executes; runbook `/root/sp6_activation_runbook_2026-06-02.md`)

### Task 4: B0 — fill persistence / per-ORDER exec ledger
- Merge `feat/sp6-phase-b0-fill-persistence` into the live branch (mig 127 already applied).
- Verify `finalize_execution_ledger` wiring in parity_mark's gated 4PM block; tests (25) green post-merge.
- Acceptance: tonight's 20:15 run populates `official_close` + `exec_ledger_usd` on today's submissions.

### Task 5: B1 — intraday execution scheduler (observation-only shadow)
- Merge `feat/sp6-phase-b1-execution-scheduler`; flip `OPENCLAW_B1_SHADOW=1`; johnbot restart (bundles Task 3's env flip).
- Restart health: Discord online, crons re-registered, no mutual-exclusion throw, gates verified in process env.
- Acceptance over 3-5 sessions: shadow ledger coverage (first smoke was 26/50 — target steadily >40/50), work-an-open vs close[T+1] deltas recorded, ZERO live-order interference (observation-only invariant).

### Task 6: completion gates + B2 authorization
- **SP-6 "complete" =** Phase A trading daily with: panel fresh (`panel_max_date == run_date` streak ≥3 sessions), diversified fills, churn collapsed post-Task-1 (held∩next-set overlap markedly above 2/16 — track daily), B0 ledger populating, B1 shadow accumulating.
- Then authorize the B2 build (13-task plan, 07441ad; worktree off the MERGED SP-6 base; `w_hawkes=0` until §28 — non-negotiable).
- Update memory topic files + MEMORY.md; close the §13 finding.

## Risks / cautions
- Task 1 touches the live signal-writing path: subagent-TDD, whole-delta review before merge; the gate-off byte-identical test is load-bearing.
- D1 touches pnl-close semantics — never DELETE; close via the existing close path only.
- 2-core box: run test suites sequentially, `nice -n 19`.
- Do not touch uncommitted live-critical files (manifest.json, strategy_signatures.json, run_sentiment_step.py).
