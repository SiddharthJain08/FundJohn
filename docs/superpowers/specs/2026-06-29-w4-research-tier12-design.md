# W4 — Research Subsystem Remediation (Tier 1+2) Design Spec

- **Date:** 2026-06-29
- **Branch:** feat/intraday-regime-15min-prefetch
- **Status:** Approved (design) — pending spec review → implementation plan
- **Workstream:** W4 remediation. Source: W4 recon (workflow w7k99ji9h, 5 agents) in `.superpowers/sdd/progress.md`; verdict in memory (to be written). W1/W2/W3 done & live.

## §0 Context & Verdict
The research engine is fundamentally SOUND and the live promotion path works, but a single missing `process.exit(0)` has silently degraded the weekly pipeline for ~2 weeks (the candidate code-review has been OFFLINE since Jun 14), and the pipeline's progress metric (`research_candidates.status`) is a silently-wrong signal. Operator chose **Tier 1+2 (unblock + observability)**. Tier 3 (quality-gating, throughput) + vestigial cleanup are deferred (§7). The F2 sub-floor sign-off sheet (`docs/w4-sub-floor-signoff.md`) already shipped (commit 5afaeab) for the operator's owed KEEP/STOP decision.

## §1 Decisions Locked (operator, 2026-06-29)
- **W4-5** report-only mastermind code-review → **split into its own systemd unit + timer** (independent TimeoutStartSec) so it can never affect the coding run's budget/result.
- **W4-2** `research_candidates.status` observability → **stamp a terminal status in the finisher after phases 6-8 + one-time backfill** the 134 hunted-but-pending rows (makes the column honest; existing terminal values only).

## §2 Code fixes (SDD/TDD-built, committed, gated deploy)

### W4-1 — finisher missing process.exit(0) (the master fix)
`src/agent/curators/saturday_brain_finisher.js:339` ends `main()` with `log('Finisher complete.');` and NO exit; the pg pool (`_query._pool`, line 51) + the ResearchOrchestrator's pg/ioredis handles hold the Node event loop open → the process hangs → the `Type=oneshot` unit times out at 4h → false FAILED, code-review never runs, zombies leak. **The file's own early-exits at lines 170/185/213 already call bare `process.exit(0)`** — the happy path simply omits it. **Fix:** add `process.exit(0);` immediately after `log('Finisher complete.');` (line 339), matching the file's established early-exit pattern. Apply the identical fix to `src/agent/curators/saturday_brain_recovery.js` after its `log('Recovery complete.')` (~line 288). (Graceful `await _query._pool?.end()` before exit is optional polish; the early-exits prove bare `process.exit(0)` is the file's contract.)

### W4-2 — finisher stamps terminal research_candidates.status + backfill
The scheduled pipeline writes `data_tier`/`hunter_result_json` but never `research_candidates.status` (only the unscheduled `processQueue` does), so hunted rows stay `pending` forever (134 of 182 >30d are already hunted). Use the EXISTING terminal status values (the spine machine: `pending→processing→done/blocked_buildable/blocked_rejected/blocked_unclassified`). 
- **In `saturday_brain_finisher.js`** (the phase-6-8 outcome handling, ~lines 243-295): after each candidate's terminal outcome, `UPDATE research_candidates SET status=$1 WHERE candidate_id=$2`: Tier-A coded-success → `'done'`; Tier-B staged → `'blocked_buildable'`; hunter-rejected (`hunter_result_json->>'rejection_reason_if_any'` present) → `'blocked_rejected'`; Tier-C / no provider → `'blocked_unclassified'`. (Stamping is SAFE for `_hunt`: hunted rows are already excluded by `hunter_result_json IS NOT NULL`, so changing status off `'pending'` does not disturb re-hunt of genuinely-`pending` un-hunted rows.)
- **One-time backfill** (`scripts/backfill_research_candidate_status.py` OR a migration): for `status='pending' AND hunter_result_json IS NOT NULL`, set the terminal status from `rejection_reason_if_any` (→`blocked_rejected`) else `data_tier` (A→`done`, B→`blocked_buildable`, C/NULL→`blocked_unclassified`). Idempotent; rolled-back temp-table test (mirror `tests/test_migration_139.py`). research_candidates is research-pipeline state, NOT master data — UPDATE permitted; APPLIED at the gated deploy.

### W4-3 — funnel curator metric inversion
`src/agent/curators/mastermind.js:1006`: `const outcome = r.predicted_bucket === 'high' ? 'pass' : 'reject'` mislabels `implementable_candidate` (the dominant promoted bucket, ~50/wk) as `'reject'`, inverting `paper_hit_rate_funnel` curator metrics. **Fix:** reuse the existing `HIGH_BUCKETS` set (mastermind.js:106 = `new Set(['high','implementable_candidate'])`): `const outcome = HIGH_BUCKETS.has(r.predicted_bucket) ? 'pass' : 'reject'`. + one-time backfill of historical `paper_gate_decisions` (gate='curator') rows where the paper's bucket was `implementable_candidate` but outcome was recorded `'reject'` (rolled-back temp-table test). Applied at deploy.

### W4-4 — Research-queue dashboard "0 recent"
`src/channels/api/routes_research.js:123-125` (the `/queue` route) queries `pipeline_runs WHERE run_type LIKE '%research%'…` — `pipeline_runs` only holds ticker data-collection jobs, so it always returns 0. The sibling `/runs` route (lines 310-359) ALREADY correctly unions `saturday_runs` (332) + `curator_runs` (359). **Fix:** replace the `/queue` `recent_runs` sub-query with the same `saturday_runs`/`curator_runs` union projection used by `/runs` (or drop `recent_runs` from `/queue` and have the client read the count from `/runs`). Confirm the `server.js` Research-queue tile (`queue-count`, ~6259) renders a non-zero `recent` after.

## §3 Config / deploy-gated actions (root systemd; explicit operator approval at deploy)
- **W4-5 (split code-review unit):** edit the unit template (`docs/sunday-research-code.service` if tracked + the live `/etc/systemd/system/openclaw-sunday-research-code.service`): REMOVE the 2nd `ExecStart` (mastermind_code_review). Create a NEW `openclaw-sunday-code-review.service` + `.timer` (Sun ~18:00 ET, after the code lane) running `mastermind_code_review.js --state candidate --recent-days 14 --include-low-trade --concurrency 3 --limit 30 --out logs/code_review_candidates_sunday.md` with `TimeoutStartSec=7200`. Deploy-gated (root, `daemon-reload`).
- **W4-6 (recurrence guard + reap):** add `ExecStartPre=-/usr/bin/pkill -f saturday_brain_finisher` to the Sunday code unit so each run self-cleans stragglers; and **reap the 2 current zombies** (`systemctl stop smoke-git-code.service smoke-git-code2.service`) — EXPLICIT operator approval at deploy (the auto-classifier correctly blocked an unprompted reap).
- **Backfills (W4-2, W4-3):** applied at deploy via direct psql/script (not master data; research-pipeline state).

## §4 Testing (TDD)
- W4-1: hard to unit-test the process exit directly; verify by inspection + a focused check that the early-exit pattern is matched. Optionally extract nothing (1-line). Note in plan.
- W4-2: unit-test the status-mapping function (outcome→terminal status, pure) + a rolled-back temp-table test of the backfill SQL (mirror `tests/test_migration_139.py`).
- W4-3: unit-test the bucket→outcome mapping (`HIGH_BUCKETS.has` — pure) + a rolled-back temp-table test of the gate_decisions backfill.
- W4-4: `node --check` + a query-shape test if practical; verify by reading that the union matches `/runs`. (No live :3000 run.)
- Python tests via `python3 -m pytest`; JS via `node tests/*.js` / `node --check`. Respect VPS 2-core.

## §5 Sequencing & commit plan (path-scoped, W2/W3-style)
1. C1 — W4-1 finisher+recovery `process.exit(0)` [smallest, highest-leverage].
2. C2 — W4-3 funnel ternary (mastermind.js) + gate_decisions backfill + tests.
3. C3 — W4-4 routes_research `/queue` source fix.
4. C4 — W4-2 finisher status-stamp (mapping fn + wire) + the backfill script + tests.
Each commit PATH-SCOPED (the live tree carries UNRECOVERABLE WIP — manifest.json/registry.py/implementations/S_*; never stage it; verify the staged set + abort guard). Footer on every commit. The systemd-unit changes (W4-5/W4-6) are deploy-gated config, NOT git-committed code (edit the live units at deploy) — though if a tracked template exists under `docs/`, update it in a commit.

## §6 Gated deploy (operator-approved, AFTER final review — same posture as W2/W3)
Commits land on `feat/intraday-regime-15min-prefetch` (not pushed/restarted until approved). Deploy steps (each explicit-approved): (1) push; (2) the code fixes apply on the next research subprocess (Python/Node read fresh each run — like W3) — but a johnbot restart is harmless for clean state; (3) apply the W4-2 status backfill + W4-3 gate_decisions backfill via direct psql; (4) split the code-review unit + add ExecStartPre + `daemon-reload`; (5) reap the 2 zombies. Verify: next Sunday's code lane exits clean (Result=success) + the code-review runs.

## §7 Deferred (NOT this spec — logged backlog)
- Tier 3 (operator may pursue later): route the ungated `/approve-strategy` (relay.js:119) through the Sharpe/DD gate + manifest write (prevents future F2 leakage); a ranked/triaged pending_approval operator view; wire `fingerprint_dedup.py` into the finisher (strategy-level dedup); `processing`-status timeout recovery (cron reset stale `processing`→`pending`); raise/watchdog `--tier-a-cap`; per-instrument-class promotion thresholds in the JS /transition gate.
- F2 owed-decision: the operator marks KEEP/STOP on `docs/w4-sub-floor-signoff.md` (14 Tier-A/C rows) + a Tier-B metric backfill.
- Vestigial cleanup: `strategy_staging` + its Research-tab UI banner; 11 superseded research timers; `paper_truth_flags`/`canonical_signatures` scaffolding; the stale `After=` dep; mastermind-chat unit credential (verify); failure-notify webhook `.env` fallback.

## §8 Risk controls
- No fix touches the trading/sizing path. W4-1 is a process-lifecycle fix (matches the file's own pattern). W4-2 status-stamp is research-state only (safe for `_hunt`). Backfills are research-pipeline state (not master data), idempotent, deploy-gated.
- Path-scoped commits; no master-data touch; no live-book mutation; the systemd/reap actions are explicit-operator-approved at deploy.
