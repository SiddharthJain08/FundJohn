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

**Via CLI:**
```bash
cd /root/openclaw && PYTHONPATH=/root/openclaw/src \
    python3 -m strategies.eligibility_manager \
    --set <strategy_id> LOW_VOL TRANSITIONING \
    --actor "operator:<name>" \
    --reason "trim HIGH_VOL after -0.6 Sh over 90d" \
    --source "live_90d"
```

## After acting

1. **Commit the manifest change** so it survives redeploys and new clones:
   ```bash
   cd /root/openclaw && git add src/strategies/manifest.json
   git commit -m "config: trim <strategy> eligible_regimes per live metrics"
   ```
   The `manifest_eligibility_drift` doctor check WARNs in DRY-RUN and **FAILs
   in LIVE** until you commit.

2. **Watch the next cycle.** `regime_gate` re-reads `manifest.json` on every
   `is_eligible()` call, so changes take effect immediately. Verify no
   signals fire from the trimmed regime by:
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

## Operational setup notes (one-time)

If the `manifest_eligibility_drift` doctor check fails with `Could not
access 'HEAD'` or `dubious ownership` errors:
```bash
git config --system --add safe.directory /root/openclaw
```
This makes `safe.directory` available to all users (root, claudebot,
systemd-spawned ExecStartPre) regardless of HOME.
