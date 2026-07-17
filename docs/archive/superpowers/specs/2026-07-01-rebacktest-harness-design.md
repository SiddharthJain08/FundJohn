# Re-backtest Harness (Phase 1b) — Design

**Date:** 2026-07-01
**Status:** Design approved (operator: "Exclude deprecated by default" + "Approve run-safety as designed")
**Type:** Ops tooling — a resumable, memory-safe, per-strategy re-backtest driver

## Problem

After the true-MTM engine fix (Phase 1a, flag `OPENCLAW_TRUE_MTM_MARKS`), every live-relevant strategy
must be re-backtested with the flag ON to replace its distorted `strategy_backtest_runs` metrics. The
existing driver (`refresh_backtests.sh` → `unified_backtest --all-live`) is a single monolithic process
with no subprocess isolation, no per-strategy timeout, and no resume — and it currently FAILS on the
2-core/8GB no-swap VPS (Jun-27 ran 8h → killed at `TimeoutStartSec`; prices grew 13× to 6246 tickers so
each strategy load is ~1.5-3GB). We need a harness that runs one strategy at a time, contains memory to a
single unit, survives crashes, and resumes.

## Goal

`scripts/rebacktest_runner.py`: enumerate the live-relevant strategies with an existing canonical
backtest, run each as its own memory-capped, watchdog-bounded subprocess with `OPENCLAW_TRUE_MTM_MARKS=1`,
strictly sequential, resumable via `run_at`, with an auditable OK/FAIL/SKIP log and a `--dry-run`.

## Non-goals

- The engine fix itself (Phase 1a, done). The probe (Phase 1c) and the actual gated run (Phase 1d).
- The live cascade — eligibility decoupling, Option-B mirror retirement, propagation (Phase 1e).
- A universe-scoped price-load optimization (operator declined for now — the probe decides if it's needed).
- Options-class strategies (their engine doesn't get the Phase-1a fix).

## Design

Single script `scripts/rebacktest_runner.py`, structured as pure helpers (unit-tested) + a thin driver.

### Work-list (pure `build_worklist`)
Base = `SELECT DISTINCT strategy_id FROM strategy_backtest_runs WHERE primary_window=true` (189). Classify
each via `src/strategies/manifest.json`:
- **Default** = base MINUS manifest `state=='deprecated'` MINUS orphans (no manifest entry) → ~157 live-relevant.
- `--include-deprecated` → add the 24 deprecated + 8 orphans (orphans have intact `.py`, run via
  `--strategy-file src/strategies/implementations/<sid>.py`; deprecated resolve via `--strategy-id`).
- `--only s1,s2` / `--exclude s1,s2` override.
- Out-of-default-scope note (logged, not silently dropped): the 18 manifest-live strategies with NO
  primary_window row are first-backtests, not re-backtests — add explicitly via `--only` if wanted.

### Resume (pure `is_done`)
Stamp `start_ts` (a wall-clock ISO string) once at first launch, persisted to `<log-dir>/state.json`
(`--log-dir` default `/var/log/openclaw/rebacktest/`, created if absent; a stable path so a resumed run
reuses the same `start_ts`, NOT the session scratchpad). A strategy is DONE iff it has a
`primary_window=true` row with `run_at > start_ts`. Before each launch, re-query and skip done strategies
→ idempotent, survives crash/session-restart (agent-2's recommended mechanism over log-grep). Per-strategy
logs go under the same `<log-dir>`.

### Per-strategy run (pure `build_systemd_cmd`)
Each strategy runs as its OWN transient unit so a runaway is cgroup-OOM-killed as a single unit, not the box:
```
systemd-run --quiet --collect --unit=rebacktest-<sid> --wait \
  -p EnvironmentFile=/root/openclaw/.env \
  -p Environment=OPENCLAW_TRUE_MTM_MARKS=1 \
  -p Environment=PYTHONPATH=/root/openclaw/src \
  -p WorkingDirectory=/root/openclaw \
  -p MemoryMax=<M>G          # default 4; tuned by the 1c probe \
  -p RuntimeMaxSec=<watchdog_sec>   # default 90 min; outliers separate \
  -p Nice=19 \
  -p StandardOutput=append:<per_sid_log> -p StandardError=append:<per_sid_log> \
  python3 -m backtest.unified_backtest --strategy-id <sid>
```
`--wait` blocks (sequential). Exit code is advisory; the AUTHORITATIVE outcome is the `run_at` re-check
after the unit finishes: new `primary_window=true` row with `run_at > start_ts` ⇒ **OK**; else **FAIL**
(covers OOM-kill, RuntimeMaxSec timeout, or a backtest error — all leave no fresh row).

### Pre-launch RAM gate
Before each launch, read `MemAvailable` from `/proc/meminfo`; if `< (MemoryMax + 0.5)GB`, sleep and
re-check (bounded retries, then log and proceed — MemoryMax still contains it). Belt-and-suspenders vs
the cgroup cap.

### Guardrails
- Refuse to start (exit non-zero) if a competing heavy backtest unit is active-running
  (`openclaw-weekend-saturday.service` / `openclaw-strategy-backtest-refresh.service`) — never two heavy
  backtests on 2 cores/8GB. The runbook documents `systemctl mask` of their timers for the run window.
- `--dry-run`: print the planned work-list + the exact `systemd-run` command per strategy, execute nothing.
- Per-strategy log line `OK <sid> <sec>s` / `FAIL <sid> <reason>` / `SKIP <sid> done`; final tally
  (`n_ok / n_fail / n_skip / remaining`) + the FAIL list for a targeted rerun.

### Watchdog + outliers
`--watchdog-min` (default 90 → `RuntimeMaxSec`). The 2 known-slow strategies (`S_tr_03_bocpd_change_point`
~3.5h, `S_pairs_trading_jump_diffusion_intraday` >40min) are run in a SEPARATE invocation:
`--only S_tr_03_bocpd_change_point,S_pairs_trading_jump_diffusion_intraday --watchdog-min 300`.

## Error handling
- A FAIL never aborts the run — log it, continue; the final FAIL list drives a targeted `--only` rerun.
- DB/enumerate failure at startup → exit non-zero with a clear message (nothing launched).
- The state file is advisory; deleting it just restarts `start_ts` (re-runs everything — safe, idempotent).

## Testing (`tests/test_rebacktest_runner.py`, unittest, mock DB/manifest)
1. `build_worklist`: default excludes deprecated + orphans; `--include-deprecated` adds them;
   `--only`/`--exclude` override; orphan → `--strategy-file` form.
2. `is_done`: row with `run_at > start_ts` ⇒ True; older/absent ⇒ False; `primary_window=false` ⇒ False.
3. `build_systemd_cmd`: contains `OPENCLAW_TRUE_MTM_MARKS=1`, `MemoryMax`, `RuntimeMaxSec`, the sid, and
   the `--strategy-file` form for orphans.
4. tally parse: OK/FAIL/SKIP counts from a synthetic result list.
5. `--dry-run` end-to-end prints commands and touches no unit (integration, mock DB).

## Rollout
Build + review + commit + push (the script is inert until invoked). The actual run is Phase 1d (gated):
probe (1c) sets `MemoryMax`, mask competing timers, run default scope in the background over hours, monitor
via the log + a `run_at` progress count, rerun FAILs, then run the 2 outliers, then Phase 1e cascade.
