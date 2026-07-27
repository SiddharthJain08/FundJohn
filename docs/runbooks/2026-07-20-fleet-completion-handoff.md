# Fleet re-backtest completion — session handoff

**Written:** 2026-07-20 ~09:10 UTC. **For:** the session driving the overnight fleet.
**One-line:** drive the fleet to CANONICAL UNIFORM, then run the operator-gated
restore sequence (un-mask 3 actuator timers + 2 flags), then Phase 3 splits.

---

## Current state (measured 2026-07-20 09:07 UTC)

- **fleet = 145** (manifest strategies in state live/candidate/staging)
- **fresh-complete (SQL truth) = 87** · **checkpoint (`data/.refresh_backtests.done`) = 87**
- **TRUE outstanding = 58** — still MIXED / NOT uniform → all actuation stays masked.
- Overnight driver `openclaw-fleet-overnight-resume.timer` → next fire **Mon 2026-07-20 21:30 UTC**.

## The overnight driver (already armed — runs itself)

`openclaw-fleet-overnight-resume.{timer,service}` + `/root/fleet_overnight_resume.sh`:
- Nightly **Mon–Fri 21:30 UTC**; `Persistent=false` (never a trading-day catch-up).
- `node scripts/refresh_backtests_resumable.js --resume --deadline <tomorrow 06:15> --per-timeout 14400 --mem-floor 4500`; RuntimeMaxSec → hard-kill **06:30 UTC** (clears before ~07:30 premarket). COMPUTE-ONLY (writes canonical; no weights/activation).
- On reaching **0 outstanding** it logs `CANONICAL UNIFORM` and **self-disables its own timer**.
- Log: `logs/fleet_overnight_resume.log`. Concurrency-guarded (skips if a driver is already running).

## ⚠️ Completion metric = SQL, NOT the checkpoint

The driver checkpoints strategy_ids only on a clean exit; an OOM that kills the driver
AFTER the canonical commit but BEFORE the checkpoint append leaves a strategy
complete-in-DB but un-checkpointed → `--resume` would re-run it (wasted window).
Earlier this session that gap was 76→86 (reconciled: 11 ids appended, incl `low_volatility_us`).
**Before declaring UNIFORM or acting, reconcile via SQL, not the log's "outstanding" count:**

```
node -e 'require("dotenv").config({path:"/root/openclaw/.env"});const fs=require("fs");const{Pool}=require("pg");const p=new Pool({connectionString:process.env.POSTGRES_URI});(async()=>{const m=JSON.parse(fs.readFileSync("src/strategies/manifest.json","utf8"));const R={live:0,candidate:1,staging:2};const fleet=Object.entries(m.strategies||{}).filter(([,e])=>e.state in R).length;const fc=await p.query("SELECT count(DISTINCT strategy_id) n FROM strategy_backtest_runs r WHERE primary_window AND run_at>=\x27 2026-07-16\x27 AND EXISTS(SELECT 1 FROM strategy_backtest_regimes g WHERE g.run_id=r.run_id)");console.log("fleet",fleet,"complete",fc.rows[0].n,"outstanding",fleet-fc.rows[0].n);await p.end();})()'
```
If checkpoint < SQL-complete, append the verified-complete-but-uncheckpointed ids to
`data/.refresh_backtests.done` (must have BOTH `strategy_backtest_runs` primary_window ≥07-16
AND matching `strategy_backtest_regimes`) so the next resume skips them.

## 🔒 Masked state to RESTORE once uniform (measured, all currently OFF)

| Item | Current | Restore to |
|---|---|---|
| `openclaw-weekly-strategy-weights.timer` (Mon 04:00 UTC actuator) | disabled/inactive | enabled |
| `openclaw-weekend-maintenance-sat.timer` (Mon 00:00 UTC actuator) | disabled/inactive | enabled |
| `openclaw-weekend-saturday.timer` (Sat actuator) | disabled/inactive | enabled |
| `.env OPENCLAW_ACTIVATION_ASSIGNER` | `0` | `1` |
| `.env OPENCLAW_AUTO_DEMOTE` | `0` | `1` |

All 3 timers are `Persistent=yes` → **stamp-touch BEFORE re-enable** or they fire a
missed-window catch-up immediately (actuating on freshly-uniform canonical mid-day).

## ⚠️ ORDERING CORRECTION (2026-07-25) — ACTIVATION MUST PRECEDE WEIGHTS

The step order below (weights=3, activation=5) is **WRONG whenever activation changes
eligibility**. `strategy_weights` skips any live strategy with no eligible regime in
`strategy_regime_params` ("is live but has no eligible regimes; skipping"), and the
activation assigner is what SETS that column. Running weights first builds them against
*pre-activation* eligibility.

Measured on 2026-07-25: weights ran 06:56:37, activation ran 07:06:37 → **5 LOW_VOL cells
had no weight row, 3 of which produced 32 of Monday's tickers** and therefore contributed
ZERO to the conviction gate. Rebuilding weights after activation: 67→71 active strategies,
89→100 rows, **LOW_VOL breadth 10→15**, skipped 22→18.

**Correct order: … → activation → weights → daily_returns → similarity.**
(`strategy_similarity` imports `strategy_weights`, so its strategy set also derives from
weights — rebuild it AFTER weights or new sleeves get the SPARSE_DEFAULT 0.05 rho instead
of a real one.) If you must keep the legacy order, **run weights TWICE** (before and after
activation). Same defect class as passing `--reassign` to `run_universe_shrink.py`, which
runs activation inline during the shrink and ahead of the conviction-floor read.

## OWED post-UNIFORM sequence (operator-gated — do in order)

1. **Verify uniform** via the SQL query above → outstanding == 0. (Do NOT trust the log.)
   Gate on **`live` strategies specifically**: a missing `live` strategy is silently dropped
   from `strategy_weights`; a missing `candidate` is tolerable.
2. **Reconcile** canonical (spot-check no partial/lying rows).
3. **Weights rebuild** — ⚠️ see ORDERING CORRECTION above, run this AFTER step 5 activation:
   `PYTHONPATH=src python3 -m execution.strategy_weights --rebuild --trigger=manual_post_uniform --verbose`
4. **Conviction-floor recheck** — verify live `regime_sizer_params.min_corr_cum_sharpe` floors are sane for the rebuilt metrics (they've been retuned twice; restoring a floor can dump backlog in one execute — see [[project_conviction_floor_trading_halt]]).
5. **Activation:** restore `OPENCLAW_ACTIVATION_ASSIGNER=1` first, then `PYTHONPATH=src python3 -m backtest.activation_assigner --all --notify`; eyeball the eligibility diff.
6. **Restore flags:** `sed -i 's/^OPENCLAW_ACTIVATION_ASSIGNER=0$/OPENCLAW_ACTIVATION_ASSIGNER=1/; s/^OPENCLAW_AUTO_DEMOTE=0$/OPENCLAW_AUTO_DEMOTE=1/' .env`
7. **Re-enable the 3 timers (stamp-touch first):** for each `<unit>` above —
   `touch /var/lib/systemd/timers/stamp-<unit>` ; `systemctl enable <unit>` ; `systemctl start <unit>` ; then confirm `systemctl list-timers <unit>` shows the NEXT natural fire (not an immediate catch-up).
8. **THEN Phase 3 splits** — parked runbook: `docs/runbooks/2026-07-17-phase3-splits-plan.md`.

## Context — already DONE + pushed this session (no action; don't redo)

- TradeJohn LLM confirmer **retired**; intraday redeploy now news-gated (`02e7755`). News-veto = pre-market gate `premarket_gate.py` (`OPENCLAW_EOD_PREMARKET_GATE=1`).
- Research backlog committed (`8fd531a`); **auto-commit-research** feature LIVE (`9b72f7b`) — see [[project_auto_commit_research_output]].
- Registry↔manifest drift resolved (4 stale rows de-approved).
- Git cleanup closed: repo is PUBLIC (intended), no live secret in history, history rewrite skipped. Only residual: revoke old Polygon key provider-side (operator).
- Working tree is CLEAN + `main` in sync with origin.

## Deep-context memory files
[[project_fleet_rebacktest_and_trade_factor]] · [[project_tradejohn_confirmer_retired]] ·
[[project_registry_manifest_drift_4_stale_approved]] · [[project_conviction_floor_trading_halt]] ·
[[feedback_manifest_vs_registry_execution_gate]] · [[reference_vps_two_core_cpu]]
