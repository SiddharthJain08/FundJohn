# Operator Runbook — Regime Eligibility Trim/Expand

**Purpose:** safely tune `manifest.json` `eligible_regimes` based on live
performance, without restarting any service.

## When to act

- Strategy shows consistently negative `sharpe_proxy` in a regime over the 90d
  window → **trim** that regime.
- Strategy shows positive sharpe + ≥10 trades in a regime it's not currently
  eligible for → consider **expand**.
- Strategy has zero trades over 90d in a regime → no signal; leave alone.

Live metrics in the dashboard come from `strategy_regime_live_pnl_rollup`,
refreshed nightly at ~02:30 ET. The `regime_live_rollup_freshness` doctor
check surfaces a stale rollup.

## How to act

**Via Dashboard (preferred):**
1. Open the johnbot dashboard, click the **Regime** tab in the header nav.
2. Find the (strategy, regime) cell you want to toggle.
3. Click — the prompt asks for a reason (lands in audit table) and operator
   name (saved to localStorage for the session).
4. Verify the new audit row appears in the table below the grid.

**Via CLI** (Phase 2A — per-(strategy, regime), not list-set):
```bash
cd /root/openclaw && PYTHONPATH=/root/openclaw/src \
    python3 -m strategies.eligibility_manager \
    --set <strategy_id> <regime> [--eligible | --ineligible] \
    [--size <float>] [--stop <float>] [--target <float>] [--max-hold <int>] \
    --actor 'operator:<name>' --reason '<reason>'
```

`<regime>` is one of `LOW_VOL`, `TRANSITIONING`, `HIGH_VOL`, `CRISIS`. You
can set just eligibility, just one numeric param, or any combination. NULL
values are inherited from the prior row. Each invocation writes ONE
strategy × regime row.

## After acting

1. **No manifest commit needed.** Phase 2A moved eligibility into the
   `strategy_regime_params` DB table; every CLI/dashboard write commits
   atomically inside a single Postgres transaction with an audit row in
   `strategy_regime_param_changes`. The doctor check
   `strategy_regime_params_consistency` PASSes when all 98×4 = 392 rows are
   present; `manifest_eligibility_drift` WARNs whenever any strategy
   *still* carries the deprecated `eligible_regimes` field on disk
   (cleanup spec will remove the field entirely).

2. **Watch the next cycle.** `regime_gate.is_eligible` queries the resolver,
   which has a 30s in-process cache + explicit invalidate on every write.
   So within the same johnbot process the change is visible immediately;
   cross-process consumers (cron, separate Python invocations) see the
   change within 30s. Verify by:
   ```bash
   docker exec openclaw-postgres psql -U openclaw -d openclaw -c "
     SELECT strategy_id, regime_state, signal_date
       FROM execution_signals
      WHERE strategy_id = '<id>'
        AND signal_date >= CURRENT_DATE - INTERVAL '1 day'
      ORDER BY signal_date DESC LIMIT 5;"
   ```

## Audit & rollback

- Full history (Dashboard): "Recent eligibility changes" table on the
  Regime tab.
- Full history (CLI):
  ```bash
  PYTHONPATH=/root/openclaw/src python3 -m strategies.eligibility_manager \
      --audit --limit 50
  ```
- Rollback: set `eligible_regimes` back to the prior list via the same
  Dashboard/CLI flow — forward and rollback edits both audit, no asymmetry.

## Cautions

- **Empty eligibility is rejected.** A strategy must remain eligible in at
  least one regime; the writer enforces this before mutating manifest.
- **DRY-RUN vs LIVE.** In DRY-RUN, the eligibility filter runs in
  `engine.run_strategies()` but sizing is still LLM-driven. In LIVE
  (`OPENCLAW_REGIME_BLENDED_LIVE=1`), `regime_blended_sizer` also reads the
  same manifest. Trim/expand affects both paths.
- **No automatic flipping.** Phase 1 is operator-gated by design. The
  eventual learned sizer (Phase 2 — see roadmap below) will propose changes
  but still require operator approval.

## Position-determination roadmap

| Phase | Method | Validation |
|---|---|---|
| 0 — pre-regime | `trade_agent_llm` discretion | LLM judgment + review |
| **1 — current** | regime_blended_sizer scalars + manifest eligibility | **this runbook (operator trim/expand from live metrics)** |
| 2 — future | Scalars + eligibility derived from rolling live-perf distribution | Monte Carlo + drift vs literature priors |

## Operational setup notes

**Phase 2A** (2026-05-12) moved eligibility from `manifest.json` into the
`strategy_regime_params` DB table. The dashboard regime cells now POST to
`/api/regime-params/:strategy/:regime`; the CLI is per-(strategy, regime)
rather than list-set. `manifest.eligible_regimes` is deprecated — the
doctor check `manifest_eligibility_drift` WARNs as long as any strategy
still has the field on disk. A follow-up spec removes the field entirely.

**Phase 2B** (2026-05-12) adds MastermindJohn-emitted proposals. Every
Saturday at 18:00 ET, the comprehensive-review run includes a
`regime_recommendations` JSON block per strategy; valid entries land in
`strategy_regime_param_proposals` with `status='pending'`. The dashboard
shows a "Pending Regime Proposals" panel above the Active Stack with
Approve / Reject buttons. CLI: `python3 -m strategies.proposal_manager
--list` / `--approve <id>` / `--reject <id> --reason '...'` / `--modify
<id> [--size N] [--stop N] ...`. Approving routes through the same
`eligibility_manager.set_params` transaction that operator-direct edits
use. The doctor check `regime_proposals_backlog` WARNs at 14-day-old
pending, FAILs at 30-day-old or ≥10 aged-warn pending — review hygiene.

**Phase 1 git config note (still relevant for any legacy paths)**: if you
ever see `Could not access 'HEAD'` or `dubious ownership` errors from
git-based doctor checks running under systemd:
```bash
git config --system --add safe.directory /root/openclaw
```
