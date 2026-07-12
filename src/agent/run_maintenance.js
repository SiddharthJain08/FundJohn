'use strict';

/**
 * run_maintenance.js — BotJohn system-maintenance driver.
 *
 * Three modes, selected via --mode {daily|saturday|saturday-verify}.
 * Default mode=daily so the original weekday timer's flag-less ExecStart
 * keeps working unchanged.
 *
 *   daily            Mon-Fri 12:00 ET — audits the 10:00 ET trade pipeline.
 *                    Driven by openclaw-botjohn-maintenance.timer.
 *   saturday         Sat 16:00 ET — audits the 10:00 ET saturday-brain run.
 *                    Recovery levers: saturday_brain_finisher.js,
 *                    saturday_brain_retry_failed.js, full re-trigger.
 *                    Driven by openclaw-botjohn-saturday-maintenance.timer.
 *   saturday-verify  Sun 12:00 ET, READ-ONLY — verifies any Saturday
 *                    recovery actually completed.
 *                    Driven by openclaw-botjohn-saturday-verify.timer.
 *
 * Each mode uses its own prompt constant; everything else (claude-bin
 * spawn, webhook lookup, Discord post, preamble clip, cost footer,
 * fallback alert) is shared. On any wrapper failure (claude-bin crash,
 * malformed JSON, empty result, webhook 5xx) we still post a fallback
 * `🚨 BotJohn maintenance run failed` line to #botjohn-log so silence is
 * never the failure mode.
 *
 * Run as: claudebot user (uid 1001) with .env loaded.
 *   /usr/bin/node src/agent/run_maintenance.js [--mode <mode>]
 *
 * Reuses:
 *   - getWebhook/postWebhook pattern from src/pipeline/daily_health_digest.js
 *   - claude-bin spawn pattern from src/agent/botjohn-direct.js
 *   - dotenv loader + getArg helper shape from src/agent/curators/run_mastermind.js
 */

const fs    = require('fs');
const path  = require('path');
const https = require('https');
const { spawn } = require('child_process');
const { Client } = require('pg');

// ── env loader (mirrors run_mastermind.js) ─────────────────────────────
const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../..');
try {
  const envPath = path.join(OPENCLAW_DIR, '.env');
  if (fs.existsSync(envPath)) {
    for (const line of fs.readFileSync(envPath, 'utf8').split('\n')) {
      const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
      if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
    }
  }
} catch { /* ignore */ }

const CLAUDE_BIN  = process.env.CLAUDE_BIN  || '/usr/local/bin/claude-bin';
const CLAUDE_UID  = parseInt(process.env.CLAUDE_UID  || '1001', 10);
const CLAUDE_GID  = parseInt(process.env.CLAUDE_GID  || '1001', 10);
const CLAUDE_HOME = process.env.CLAUDE_HOME || '/home/claudebot';

// Per-mode caps. Daily runs are quick green-lights or short fixes; Saturday
// runs are the heaviest investigation+recovery path (today's 14-strategy
// debug session needed multi-script edits and iterative re-runs); verify is
// strictly read-only.
//
// Empirical baseline (from 4 production runs Apr 30 → May 3): median cost
// $0.27, median duration 152s. Headroom is generous on every mode; raising
// these is virtually free unless a cycle goes catastrophically off-rails.
const COST_CAP_BY_MODE = {
  'daily':           parseFloat(process.env.MAINT_COST_CAP_USD_DAILY        || '5.00'),
  'saturday':        parseFloat(process.env.MAINT_COST_CAP_USD_SATURDAY     || '15.00'),
  'saturday-verify': parseFloat(process.env.MAINT_COST_CAP_USD_VERIFY       || '3.00'),
  'weekend-sat':     parseFloat(process.env.MAINT_COST_CAP_USD_WEEKEND_SAT  || '8.00'),
  'weekend-sun':     parseFloat(process.env.MAINT_COST_CAP_USD_WEEKEND_SUN  || '15.00'),
};
const TIMEOUT_MS_BY_MODE = {
  'daily':           parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS_DAILY          || '1800000', 10),  // 30 min
  'saturday':        parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS_SATURDAY       || '3000000', 10),  // 50 min
  'saturday-verify': parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS_VERIFY         || '900000',  10),  //  15 min
  'weekend-sat':     parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS_WEEKEND_SAT    || '1800000', 10),  // 30 min
  'weekend-sun':     parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS_WEEKEND_SUN    || '3000000', 10),  // 50 min
};

// Legacy global env knobs retained for back-compat. If MAINT_COST_CAP_USD or
// MAINT_CLAUDE_TIMEOUT_MS are set, they override every mode (operator
// short-circuit). Tests + unit tests still reach in via these globals.
const _GLOBAL_COST_CAP_OVERRIDE = process.env.MAINT_COST_CAP_USD ? parseFloat(process.env.MAINT_COST_CAP_USD) : null;
const _GLOBAL_TIMEOUT_OVERRIDE  = process.env.MAINT_CLAUDE_TIMEOUT_MS ? parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS, 10) : null;

function costCapFor(mode) {
  if (_GLOBAL_COST_CAP_OVERRIDE !== null && Number.isFinite(_GLOBAL_COST_CAP_OVERRIDE)) {
    return _GLOBAL_COST_CAP_OVERRIDE;
  }
  return COST_CAP_BY_MODE[mode] ?? COST_CAP_BY_MODE.daily;
}
function timeoutMsFor(mode) {
  if (_GLOBAL_TIMEOUT_OVERRIDE !== null && Number.isFinite(_GLOBAL_TIMEOUT_OVERRIDE)) {
    return _GLOBAL_TIMEOUT_OVERRIDE;
  }
  return TIMEOUT_MS_BY_MODE[mode] ?? TIMEOUT_MS_BY_MODE.daily;
}

// Back-compat alias — formatReport uses this for the cost-cap warning line.
// Resolved per-call in main(), not at module load, so the prompt's
// {{COST_CAP_USD}} interpolation always matches the cap actually in effect.
let COST_CAP_USD = costCapFor('daily');

// ── argv helper (mirrors run_mastermind.js:66-72) ─────────────────────
function getArg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  if (i < 0) return fallback;
  const next = process.argv[i + 1];
  if (!next || next.startsWith('--')) return true;
  return next;
}

// ── prompts ───────────────────────────────────────────────────────────
const DAILY_PROMPT = `# BotJohn — Daily System Maintenance

You are BotJohn, portfolio manager and orchestrator of the OpenClaw quant
hedge fund. Today is {{TODAY_ISO}} (America/New_York). It is 12:00 PM ET on
a weekday. The 10:00 AM ET pipeline cycle should already have completed.

## Your job, in order

1. Preflight check (fast, sub-second per check):
       python3 src/maintenance/doctor.py --json
   Parse the JSON. Note exit code (0 pass / 1 warn / 2 fail) and any
   check with status "warn" or "fail".

2. System probes (deeper post-cycle health — verifies pipeline ran,
   signals are clean, broker session works, regime gate admits strategies,
   strategies are importable, agents are reachable):
       python3 -m system_checks --json
   Parse the JSON. Same exit-code convention as doctor. Each check has
   a \`name\`, \`status\`, \`tags\`, and \`detail\`. Failing checks should be
   investigated before greenlight.

3. Digest:
       node src/pipeline/daily_health_digest.js --dry-run
   Cross-reference with doctor + system_checks findings.

4. If anything is off, investigate:
   - tail -200 logs/pipeline_orchestrator_{{TODAY_ISO}}.log
   - Postgres queries via $POSTGRES_URI:
       SELECT count(*), max(generated_at) FROM execution_signals
         WHERE generated_at::date = '{{TODAY_ISO}}';
       SELECT count(*) FROM alpaca_submissions
         WHERE submitted_at::date = '{{TODAY_ISO}}';
       SELECT state, vix_level, updated_at FROM market_regime
         ORDER BY updated_at DESC LIMIT 1;
       SELECT * FROM daily_signal_summary
         WHERE run_date = '{{TODAY_ISO}}';
   - Run a specific system_check for finer-grained diagnostics:
       python3 -m system_checks --check <name> --json

5. Fix and re-run (only if a real failure is found):
   - Apply code fixes directly. You have full filesystem write access.
   - Commit with \`[botjohn-maint] <reason>\` and \`git push\`.
   - Re-trigger the cycle SYNCHRONOUSLY:
         python3 scripts/run_pipeline.py --force-resume --date {{TODAY_ISO}}
     Wait for it to finish (5–10 min). Capture the result. Do not loop.
   - When you fix a class of bug that should not recur, add a new
     check under src/system_checks/checks/ so future maintenance
     catches the regression. See src/system_checks/README.md for the
     contract.

6. Idempotency: if doctor green + system_checks green + digest healthy
   + DB row counts plausible, green-light it. Don't manufacture work.

## Output format — REQUIRED

Return your final response as Discord-ready Markdown ≤1700 chars total.
Do NOT post to Discord — the wrapper handles posting. Do NOT include
cost — the wrapper appends it.

### Green-light (≤300 chars):
    ✅ **Daily maintenance — {{TODAY_ISO}}**
    Pipeline ran cleanly. Doctor: pass (11/11). Cycle: 8/8 steps. Signals: <N>. Submissions: <N>. Regime: <STATE>.
    No action taken.

### Fix-and-recovered:
    🔧 **Daily maintenance — {{TODAY_ISO}}**
    **Detected:** <one-line summary>
    **Root cause:** <2-3 lines>
    **Fix applied:**
    - <bullet> (commit \`<sha>\` if code change)
    - <bullet>
    **Re-run:** \`--force-resume\` → <result>
    **Residual concerns:** <one line, or "none">

### Cannot-auto-fix (escalate):
    🚨 **Daily maintenance — {{TODAY_ISO}}**
    **Detected:** <summary>
    **Investigation:** <what you tried>
    **Could not auto-fix because:** <reason>
    **Suggested next steps:** <bullets>

## Boundaries
- NEVER delete from master parquets / canonical Postgres tables
  (CLAUDE.md core invariant). Schema additions only.
- Do NOT skip a real fix and just send a green light.
- Do NOT spawn subagents. Run tools directly.
- Do NOT post to any Discord webhook yourself.
- Keep total session budget under $\{\{COST_CAP_USD\}\}. If your work expands beyond
  a single root cause, write the cannot-auto-fix template and stop.
`;

const SATURDAY_PROMPT = `# BotJohn — Sunday Research Maintenance

You are BotJohn, portfolio manager and orchestrator of the OpenClaw quant
hedge fund. Today is {{TODAY_ISO}} (America/New_York). It is 21:00 ET on
Sunday. Weekend research now runs in TWO Sunday slots (consolidated 2026-06-09):
the 08:00 ET INGEST run (phases 0-4: expand→sweep→rate→recurate→hunt) leaves
its saturday_runs row at status='ingest_complete'; the 14:00 ET CODE run
(saturday_brain_finisher --mark-run-complete = phases 5-8 tier→code→stage→vault)
flips that SAME row to status='completed' + stamps coded_synchronous. By 21:00 ET
both should be done. A row stuck at 'ingest_complete' means the 14:00 code run
didn't finish; a row stuck at 'running' is an ingest zombie.

Your job: audit the run, report counts to #botjohn-log, fix anything broken
SURGICALLY, and re-trigger the appropriate recovery lever DETACHED. You
have \{\{TIMEOUT_MIN\}\} minutes wrapper budget. Monday 08:00 ET verify run will
close the loop on whatever recovery you kick off.

## Step 1 — Pull canonical run state

Connect via $POSTGRES_URI:

    SELECT run_id, started_at, finished_at, status, current_phase,
           sources_discovered, papers_ingested, papers_rated,
           implementable_n, paperhunters_run,
           tier_a_count, tier_b_count, tier_c_count,
           coded_synchronous, coded_failed,
           cost_usd, error_detail
      FROM saturday_runs
     ORDER BY started_at DESC
     LIMIT 3;

Capture latest run_id. Status values seen in production:
'completed', 'partial', 'abandoned', 'failed', 'running'.

## Step 2 — Pull complementary metrics

    -- bucket distribution from corpus rating
    SELECT predicted_bucket, COUNT(*) AS n
      FROM curated_candidates
     WHERE run_id = '<run_id>'
     GROUP BY predicted_bucket
     ORDER BY 1;

    -- candidate staging counts (today)
    SELECT status, data_tier, COUNT(*) AS n
      FROM research_candidates
     WHERE created_at::date = '{{TODAY_ISO}}'::date
     GROUP BY 1,2
     ORDER BY 1,2;

    -- paperhunter rejection breakdown (today)
    SELECT gate_name, outcome, COUNT(*) AS n
      FROM paper_gate_decisions
     WHERE occurred_at::date = '{{TODAY_ISO}}'::date
     GROUP BY 1,2
     ORDER BY 1, 3 DESC;

    -- Tier-A backtest results landed today
    -- backtest metrics come from canonical strategy_backtest_runs (latest
    -- primary_window=true run per strategy_id == registry id slug), NOT the
    -- strategy_registry mirror (retired as a read-consumer 2026-07-05,
    -- Option B). "Landed today" is driven by canonical bt.run_at — post-
    -- Option B the registry's updated_at is NOT touched by backtest
    -- completion (it only moves on lifecycle/status edits), so filtering on
    -- it silently missed every fresh backtest landing. sr.created_at still
    -- catches strategies registered today that have no run yet.
    SELECT sr.name, sr.status,
           bt.total_sharpe     AS backtest_sharpe,
           bt.total_return_pct AS backtest_return_pct,
           bt.total_max_dd_pct AS backtest_max_dd_pct
      FROM strategy_registry sr
      LEFT JOIN LATERAL (
        SELECT run_at, total_sharpe, total_return_pct, total_max_dd_pct
          FROM strategy_backtest_runs
         WHERE strategy_id = sr.id AND primary_window = TRUE
         ORDER BY run_at DESC LIMIT 1
      ) bt ON TRUE
     WHERE sr.created_at::date = '{{TODAY_ISO}}'::date
        OR bt.run_at::date = '{{TODAY_ISO}}'::date
     ORDER BY bt.total_sharpe DESC NULLS LAST
     LIMIT 20;

## Step 3 — Classify

  GREEN-LIGHT  status='completed' AND finished_at IS NOT NULL
               AND papers_ingested >= 1
               AND papers_rated >= 1
               AND (coded_synchronous >= 1 OR implementable_n == 0)
               AND cost_usd <= 100.

  ZOMBIE       status='running' AND started_at < NOW() - INTERVAL '7 hours'.

  PARTIAL      status='partial' OR (status='completed' AND any green-light
               criterion above is violated except cost).

  FAILED       status IN ('failed','abandoned')
               OR papers_ingested == 0 (with status not 'running').

  COST_OVERRUN cost_usd > 100. Compose alongside the above.

## Step 4 — Recovery (PARTIAL / FAILED / ZOMBIE only)

Choose surgical-first. Re-trigger always DETACHED.

  coded_synchronous == 0 AND implementable_n > 0  (Phase 6 silently broken)
     1. tail -200 logs/saturday_brain_<run_id>.log to find the cause
     2. Apply code fix in src/agent/curators/* if obvious; commit
        with \`[botjohn-saturday] <reason>\` and git push
     3. Re-run finisher detached:
          nohup /usr/bin/node /root/openclaw/src/agent/curators/saturday_brain_finisher.js --mark-run-complete \\
              > /root/openclaw/logs/saturday_brain_finisher_{{TODAY_ISO}}.log 2>&1 &
        Idempotent on strategy_id. ~30 min.

  Many gate_decisions.outcome='fetch_failed'
     nohup /usr/bin/node /root/openclaw/src/agent/curators/saturday_brain_retry_failed.js \\
         --max-age-hours 36 \\
         > /root/openclaw/logs/saturday_brain_retry_{{TODAY_ISO}}.log 2>&1 &
     Chains finisher automatically. ~45 min.

  status='partial' with persisted hunter rows
     finisher (same as Phase 6 case).

  status IN ('failed','abandoned') with no salvage
     1. Diagnose root cause from error_detail JSONB and logs
     2. Apply code fix if applicable; commit + push
     3. systemctl start openclaw-sunday-research-ingest.service   # then 14:00 code via finisher --mark-run-complete
        (full re-run, ~1h, ~$40)

  status='running' >7h (ZOMBIE)
     1. UPDATE saturday_runs
           SET status='failed', finished_at=NOW(),
               error_detail=jsonb_build_object('reason','zombie_killed_by_botjohn',
                                                'killed_at',NOW())
         WHERE run_id='<run_id>' AND status='running';
     2. systemctl start openclaw-sunday-research-ingest.service   # then 14:00 code via finisher --mark-run-complete

  papers_ingested == 0 (arxiv/openalex API issue)
     If \`curl https://export.arxiv.org/api/query?search_query=cat:q-fin.PM&max_results=1\`
     fails → ESCALATE (transient, retry next week).
     If logic bug → fix code, commit, full re-trigger.

  cost_usd > 100  → ESCALATE. Do not re-trigger.

After any re-trigger, confirm it actually started:
  systemctl status openclaw-sunday-research-ingest.service --no-pager | head -10
or for nohup:
  ps -ef | grep saturday_brain_finisher | head -3

## Step 5 — Output (Discord-ready Markdown ≤1700 chars)

Wrapper handles posting + cost footer. Lead with the ✅/🔧/🚨 emoji.

### GREEN-LIGHT:
    ✅ **Saturday research — {{TODAY_ISO}}**
    Run \`<run_id_short>\` completed in <H>h<M>m. Cost: $<N>.
    **Papers looked at:** <sources_discovered>
    **Papers ingested:** <papers_ingested> new (research_corpus)
    **Sent for staging:** <buildable+pending> candidates
    Buckets: high=<n> · med=<n> · low=<n> · reject=<n> · implementable=<implementable_n>
    **Directly implemented:** <coded_synchronous>/<tier_a_count> backtested
    Top: <name> (Sharpe <S>, ret <R>%, DD <DD>%)
    Tier-B staged: <tier_b_count> · Tier-C deferred: <tier_c_count>
    No action taken.

### FIX-AND-RECOVERED:
    🔧 **Saturday research — {{TODAY_ISO}}**
    Run \`<run_id_short>\` status=<status> at phase=<current_phase>.
    **Detected:** <one-line summary>
    **Root cause:** <2-3 lines from error_detail / logs>
    **Fix applied:** commit \`<sha>\` (<file>: <one-line change>)
    **Recovery:** \`<finisher|retry_failed|full re-trigger>\` started detached at <HH:MM ET>
    PID <pid> · log: logs/<file>
    **Salvaged so far:** ingested=<n> rated=<n> implementable=<n> coded=<n>
    **Verification:** Sunday 12:00 ET verify run will confirm completion.

### ESCALATION:
    🚨 **Saturday research — {{TODAY_ISO}}**
    Run \`<run_id_short>\` status=<status>, cannot auto-fix.
    **Detected:** <summary>
    **Investigation:** <what you tried>
    **Could not auto-fix because:** <reason>
    **Suggested next steps:**
    - <bullet>
    - <bullet>
    **Salvaged so far:** ingested=<n> rated=<n> implementable=<n> coded=<n>

## Boundaries
- NEVER delete from master parquets / canonical Postgres tables. Schema additions only.
- NEVER full re-trigger when surgical lever applies. Cost: ~$40 vs $\{\{COST_CAP_USD\}\}.
- ALWAYS detach re-triggers (\`nohup ... &\` or \`systemctl start\`). Wrapper times out at \{\{TIMEOUT_MIN\}\} min.
- ALWAYS update zombie saturday_runs row to status='failed' BEFORE re-triggering — keeps the dashboard coherent.
- Do NOT spawn subagents.
- Do NOT post to Discord yourself.
- Keep your own audit-session budget under $\{\{COST_CAP_USD\}\}. If multiple bugs interact, use ESCALATION and stop.
`;

const SATURDAY_VERIFY_PROMPT = `# BotJohn — Saturday Research Verify

You are BotJohn. Today is {{TODAY_ISO}} (Sunday). At 12:00 ET we close
the loop on yesterday's saturday-brain run + any recovery that fired
Saturday evening.

This run is READ-ONLY. Do not apply fixes. Report status; if anything
looks wrong, escalate so a human can intervene Monday morning.

## Step 1 — Pull yesterday's runs

    SELECT run_id, started_at, finished_at, status, current_phase,
           sources_discovered, papers_ingested, papers_rated,
           implementable_n, paperhunters_run,
           tier_a_count, tier_b_count, tier_c_count,
           coded_synchronous, coded_failed,
           cost_usd, error_detail
      FROM saturday_runs
     WHERE started_at::date = ('{{TODAY_ISO}}'::date - INTERVAL '1 day')::date
     ORDER BY started_at;

May have 1 row (clean Saturday) or multiple (original + recovery
re-trigger).

## Step 2 — Check finisher / retry artifacts

    -- strategy_registry rows landed yesterday or overnight
    SELECT COUNT(*) FROM strategy_registry
     WHERE created_at >= ('{{TODAY_ISO}}'::date - INTERVAL '1 day')
       AND created_at <  '{{TODAY_ISO}}'::date;

    -- finisher log if exists
    ls -la /root/openclaw/logs/saturday_brain_*_$(date -d yesterday +%Y-%m-%d).log

## Step 3 — Decide outcome

  CONFIRMED      Latest row status='completed', all green-light criteria met.

  RECOVERY-OK    First row failed/partial/zombie BUT a later row (or a
                 finisher log) shows recovery completed cleanly.

  RECOVERY-FAIL  First row failed AND no later completed row, OR latest
                 row still status='running' (>26h orphan), OR finisher
                 log shows non-zero exit / no new strategy_registry rows.

  NO-RUN         Zero rows for yesterday — timer failed to fire.

## Step 4 — Output (≤1200 chars)

### CONFIRMED:
    ✅ **Saturday verify — {{TODAY_ISO}}**
    Yesterday's run \`<run_id_short>\` closed cleanly: <coded_synchronous> backtested,
    <tier_b_count> staged, $<cost> spent. No action.

### RECOVERY-OK:
    ✅ **Saturday verify — {{TODAY_ISO}}**
    Run \`<run_id_1_short>\` failed at <phase>; recovery (<finisher|retry|run_2>) closed it.
    Final: <coded_synchronous> backtested, <tier_b_count> staged.

### RECOVERY-FAIL:
    🚨 **Saturday verify — {{TODAY_ISO}}**
    Recovery did NOT complete. Run \`<run_id_short>\` still <status>;
    <coded_synchronous> Tier-A entries vs <implementable_n> implementable.
    **Suggested next steps:** <bullets>

### NO-RUN:
    🚨 **Saturday verify — {{TODAY_ISO}}**
    No saturday_runs row exists for yesterday. Timer may have failed to fire.
    Check: \`systemctl status openclaw-sunday-research-ingest.timer\`

## Boundaries
- READ-ONLY. No fixes, commits, or re-triggers.
- Keep audit budget under $\{\{COST_CAP_USD\}\}.
- Do NOT post yourself; wrapper handles posting.
`;

const WEEKEND_SAT_PROMPT = `# BotJohn — Saturday Strategy-Adjustment Audit

You are BotJohn, portfolio manager and orchestrator of the OpenClaw quant
hedge fund. Today is {{TODAY_ISO}} (America/New_York). It is Saturday and
the 08:00 ET strategy-adjustment + backtest-refresh pipeline should have
completed (typical runtime ~2-3h; look for the weekend_saturday.sh log
under /var/log/openclaw/).

Your job: audit every stage of that pipeline, surface gaps, and recover
where safe — all within your \{\{TIMEOUT_MIN\}\} minute budget.

## Step 1 — Check pipeline log

    ls -lt /var/log/openclaw/weekend_saturday_*.log | head -3
    tail -200 /var/log/openclaw/weekend_saturday_$(date +%Y%m%d)*.log 2>/dev/null \
      || echo 'no log for today'

Note whether all 8 steps completed and their exit/WARN/ABORT status.

## Step 2 — Verify strategy-memos (step 1: comprehensive-review)

Connect via $POSTGRES_URI:

    SELECT COUNT(*) AS memos_today
      FROM strategy_memos
     WHERE written_at::date = '{{TODAY_ISO}}';

Expect ≥ 1 memo per live strategy. Zero memos → comprehensive-review failed.

## Step 3 — Verify sizing recommendations (step 3: position-recs)

    SELECT COUNT(*) AS recs_today
      FROM strategy_sizing_recommendations
     WHERE created_at::date = '{{TODAY_ISO}}';

Expect ≥ 1 row. Zero → position-recs failed.

## Step 4 — Verify backtest-coupling (step 4)

    SELECT COUNT(*) AS coupling_rows_today
      FROM strategy_regime_param_changes
     WHERE source = 'saturday_coupling'
       AND changed_at::date = '{{TODAY_ISO}}';

Zero rows is acceptable when no changes were warranted; non-zero confirms
the coupler ran and wrote at least one adjustment.

## Step 5 — Verify backtests refreshed (step 5: full backtest refresh)

    SELECT COUNT(*) AS fresh_runs_today
      FROM strategy_backtest_runs
     WHERE run_at::date = '{{TODAY_ISO}}';

    -- Check every live strategy has a run today. NOTE: the authoritative
    -- "live" source-of-truth is the manifest (strategies/manifest.json,
    -- state='live') — NOT strategy_registry. The query below uses the
    -- registry's state only as a convenient proxy; if it disagrees with the
    -- manifest, trust the manifest and cross-check
    -- (python3 -c "from strategies import lifecycle; ...") before concluding
    -- a strategy was missed.
    SELECT s.name, MAX(r.run_at) AS last_run
      FROM strategy_registry s
      LEFT JOIN strategy_backtest_runs r ON r.strategy_id = s.id
     WHERE s.state = 'live'
     GROUP BY s.name
    HAVING MAX(r.run_at)::date < '{{TODAY_ISO}}'
        OR MAX(r.run_at) IS NULL
     ORDER BY last_run ASC NULLS FIRST;

Any manifest-live strategy with no run today → backtest refresh incomplete.

## Step 6 — Verify strategy weights refreshed (step 6)

    SELECT updated_at::date AS refresh_date, COUNT(*) AS rows
      FROM strategy_weights_by_regime
     GROUP BY 1
     ORDER BY 1 DESC
     LIMIT 3;

Weights should be updated today.

## Step 7 — Verify panel freshness (step 7: panel rebuild)

    SELECT COUNT(*) AS stale_panels
      FROM strategy_backtest_panel p
      JOIN (SELECT DISTINCT ON (strategy_id) strategy_id, run_at
              FROM strategy_backtest_runs
             WHERE primary_window = TRUE
             ORDER BY strategy_id, run_at DESC) r
        ON r.strategy_id = p.strategy_id
     WHERE p.computed_at < r.run_at;

Zero stale panels = success.

## Step 8 — Classify and recover

  GREEN-LIGHT   All steps completed; memos ≥ 1, recs ≥ 1, backtests fresh,
                weights updated today, stale_panels = 0.

  PARTIAL       One or more steps WARN'd but others completed. Recover the
                failed step surgically (re-run the specific script). Budget:
                $\{\{COST_CAP_USD\}\}.

  FAILED        Step 5 (backtest refresh) ABORT'd — steps 6+7 were skipped.
                Re-trigger manually:
                  bash /root/openclaw/src/maintenance/refresh_backtests.sh
                  node /root/openclaw/src/agent/curators/weekly_live_sharpe.js
                  python3 -m backtest.backtest_panel --rebuild

  NO-RUN        Log file missing or step 1 never ran. Check:
                  systemctl status openclaw-weekend-saturday.service --no-pager

## Output format — REQUIRED

Return Discord-ready Markdown ≤1700 chars. Lead with emoji.

### GREEN-LIGHT:
    ✅ **Saturday strategy-adjustment — {{TODAY_ISO}}**
    All 8 steps completed. Memos: <N>. Recs: <N>. Coupling rows: <N>.
    Backtests: <N> runs, <live_count> live strategies fresh.
    Weights: updated. Stale panels: 0.
    No action taken.

### PARTIAL / FIX-AND-RECOVERED:
    🔧 **Saturday strategy-adjustment — {{TODAY_ISO}}**
    **Gaps detected:** <summary per step>
    **Root cause:** <diagnosis>
    **Fix applied:** <what ran / committed>
    **Residual concerns:** <one line or "none">

### ESCALATION:
    🚨 **Saturday strategy-adjustment — {{TODAY_ISO}}**
    **Detected:** <summary>
    **Could not auto-fix because:** <reason>
    **Suggested next steps:** <bullets>

## Boundaries
- NEVER delete from master parquets / canonical Postgres tables.
- Do NOT spawn subagents.
- Do NOT post to Discord yourself.
- Keep audit budget under $\{\{COST_CAP_USD\}\}.
`;

const WEEKEND_SUN_PROMPT = `# BotJohn — Sunday Research-Pipeline Audit

You are BotJohn, portfolio manager and orchestrator of the OpenClaw quant
hedge fund. Today is {{TODAY_ISO}} (America/New_York). It is Sunday 20:00 ET.
Weekend research now runs in TWO Sunday slots (consolidated 2026-06-09): the
08:00 ET INGEST run (phases 0-4: expand→sweep→rate→recurate→hunt) leaves its
saturday_runs row at status='ingest_complete'; the 14:00 ET CODE run
(saturday_brain_finisher --mark-run-complete = phases 5-8 tier→code→stage→vault)
flips that SAME row to status='completed' and stamps coded_synchronous. By
20:00 ET both should be done. A row stuck at 'ingest_complete' means the 14:00
code run never closed it (most common partial → run the finisher). A row stuck
at 'running' is an ingest zombie (the hunt-hang kill-timeout should prevent it).

Your job: audit the run, report counts to #botjohn-log, fix anything broken
SURGICALLY, and re-trigger the appropriate recovery lever DETACHED. You
have \{\{TIMEOUT_MIN\}\} minutes wrapper budget. Monday 12:00 ET maintenance run
will close the loop on whatever recovery you kick off.

## Step 1 — Pull canonical run state

Connect via $POSTGRES_URI:

    SELECT run_id, started_at, finished_at, status, current_phase,
           sources_discovered, papers_ingested, papers_rated,
           implementable_n, paperhunters_run,
           tier_a_count, tier_b_count, tier_c_count,
           coded_synchronous, coded_failed,
           cost_usd, error_detail
      FROM saturday_runs
     ORDER BY started_at DESC
     LIMIT 3;

Capture latest run_id. Status values: 'completed', 'partial', 'abandoned',
'failed', 'running'.

## Step 2 — Pull complementary metrics

    -- bucket distribution from corpus rating
    SELECT predicted_bucket, COUNT(*) AS n
      FROM curated_candidates
     WHERE run_id = '<run_id>'
     GROUP BY predicted_bucket
     ORDER BY 1;

    -- candidate staging counts (today)
    SELECT status, data_tier, COUNT(*) AS n
      FROM research_candidates
     WHERE created_at::date = '{{TODAY_ISO}}'::date
     GROUP BY 1,2
     ORDER BY 1,2;

    -- paperhunter rejection breakdown (today)
    SELECT gate_name, outcome, COUNT(*) AS n
      FROM paper_gate_decisions
     WHERE occurred_at::date = '{{TODAY_ISO}}'::date
     GROUP BY 1,2
     ORDER BY 1, 3 DESC;

    -- Tier-A backtest results landed today
    -- backtest metrics come from canonical strategy_backtest_runs (latest
    -- primary_window=true run per strategy_id == registry id slug), NOT the
    -- strategy_registry mirror (retired as a read-consumer 2026-07-05,
    -- Option B). "Landed today" is driven by canonical bt.run_at — post-
    -- Option B the registry's updated_at is NOT touched by backtest
    -- completion (it only moves on lifecycle/status edits), so filtering on
    -- it silently missed every fresh backtest landing. sr.created_at still
    -- catches strategies registered today that have no run yet.
    SELECT sr.name, sr.status,
           bt.total_sharpe     AS backtest_sharpe,
           bt.total_return_pct AS backtest_return_pct,
           bt.total_max_dd_pct AS backtest_max_dd_pct
      FROM strategy_registry sr
      LEFT JOIN LATERAL (
        SELECT run_at, total_sharpe, total_return_pct, total_max_dd_pct
          FROM strategy_backtest_runs
         WHERE strategy_id = sr.id AND primary_window = TRUE
         ORDER BY run_at DESC LIMIT 1
      ) bt ON TRUE
     WHERE sr.created_at::date = '{{TODAY_ISO}}'::date
        OR bt.run_at::date = '{{TODAY_ISO}}'::date
     ORDER BY bt.total_sharpe DESC NULLS LAST
     LIMIT 20;

## Step 3 — Classify

  GREEN-LIGHT  status='completed' AND finished_at IS NOT NULL
               AND papers_ingested >= 1
               AND papers_rated >= 1
               AND (coded_synchronous >= 1 OR implementable_n == 0)
               AND cost_usd <= 100.

  ZOMBIE       status='running' AND started_at < NOW() - INTERVAL '7 hours'.

  PARTIAL      status='partial' OR (status='completed' AND any green-light
               criterion above is violated except cost).

  FAILED       status IN ('failed','abandoned')
               OR papers_ingested == 0 (with status not 'running').

  COST_OVERRUN cost_usd > 100. Compose alongside the above.

## Step 4 — Recovery (PARTIAL / FAILED / ZOMBIE only)

Choose surgical-first. Re-trigger always DETACHED.

  coded_synchronous == 0 AND implementable_n > 0  (Phase 6 silently broken)
     1. tail -200 logs/saturday_brain_<run_id>.log to find the cause
     2. Apply code fix in src/agent/curators/* if obvious; commit
        with \`[botjohn-sunday] <reason>\` and git push
     3. Re-run finisher detached:
          nohup /usr/bin/node /root/openclaw/src/agent/curators/saturday_brain_finisher.js --mark-run-complete \\
              > /root/openclaw/logs/saturday_brain_finisher_{{TODAY_ISO}}.log 2>&1 &
        Idempotent on strategy_id. ~30 min.

  Many gate_decisions.outcome='fetch_failed'
     nohup /usr/bin/node /root/openclaw/src/agent/curators/saturday_brain_retry_failed.js \\
         --max-age-hours 36 \\
         > /root/openclaw/logs/saturday_brain_retry_{{TODAY_ISO}}.log 2>&1 &
     Chains finisher automatically. ~45 min.

  status='partial' with persisted hunter rows
     finisher (same as Phase 6 case).

  status IN ('failed','abandoned') with no salvage
     1. Diagnose root cause from error_detail JSONB and logs
     2. Apply code fix if applicable; commit + push
     3. systemctl start openclaw-sunday-research-ingest.service   # then 14:00 code via finisher --mark-run-complete
        (full re-run, ~1h, ~$40)

  status='running' >7h (ZOMBIE)
     1. UPDATE saturday_runs
           SET status='failed', finished_at=NOW(),
               error_detail=jsonb_build_object('reason','zombie_killed_by_botjohn',
                                                'killed_at',NOW())
         WHERE run_id='<run_id>' AND status='running';
     2. systemctl start openclaw-sunday-research-ingest.service   # then 14:00 code via finisher --mark-run-complete

  papers_ingested == 0 (arxiv/openalex API issue)
     If \`curl https://export.arxiv.org/api/query?search_query=cat:q-fin.PM&max_results=1\`
     fails → ESCALATE (transient, retry next week).
     If logic bug → fix code, commit, full re-trigger.

  cost_usd > 100  → ESCALATE. Do not re-trigger.

After any re-trigger, confirm it actually started:
  systemctl status openclaw-sunday-research-ingest.service --no-pager | head -10
or for nohup:
  ps -ef | grep saturday_brain_finisher | head -3

## Step 5 — Output (Discord-ready Markdown ≤1700 chars)

Wrapper handles posting + cost footer. Lead with the ✅/🔧/🚨 emoji.

### GREEN-LIGHT:
    ✅ **Sunday research — {{TODAY_ISO}}**
    Run \`<run_id_short>\` completed in <H>h<M>m. Cost: $<N>.
    **Papers looked at:** <sources_discovered>
    **Papers ingested:** <papers_ingested> new (research_corpus)
    **Sent for staging:** <buildable+pending> candidates
    Buckets: high=<n> · med=<n> · low=<n> · reject=<n> · implementable=<implementable_n>
    **Directly implemented:** <coded_synchronous>/<tier_a_count> backtested
    Top: <name> (Sharpe <S>, ret <R>%, DD <DD>%)
    Tier-B staged: <tier_b_count> · Tier-C deferred: <tier_c_count>
    No action taken.

### FIX-AND-RECOVERED:
    🔧 **Sunday research — {{TODAY_ISO}}**
    Run \`<run_id_short>\` status=<status> at phase=<current_phase>.
    **Detected:** <one-line summary>
    **Root cause:** <2-3 lines from error_detail / logs>
    **Fix applied:** commit \`<sha>\` (<file>: <one-line change>)
    **Recovery:** \`<finisher|retry_failed|full re-trigger>\` started detached at <HH:MM ET>
    PID <pid> · log: logs/<file>
    **Salvaged so far:** ingested=<n> rated=<n> implementable=<n> coded=<n>
    **Verification:** Monday 12:00 ET maintenance run will confirm completion.

### ESCALATION:
    🚨 **Sunday research — {{TODAY_ISO}}**
    Run \`<run_id_short>\` status=<status>, cannot auto-fix.
    **Detected:** <summary>
    **Investigation:** <what you tried>
    **Could not auto-fix because:** <reason>
    **Suggested next steps:**
    - <bullet>
    - <bullet>
    **Salvaged so far:** ingested=<n> rated=<n> implementable=<n> coded=<n>

## Boundaries
- NEVER delete from master parquets / canonical Postgres tables. Schema additions only.
- NEVER full re-trigger when surgical lever applies. Cost: ~$40 vs $\{\{COST_CAP_USD\}\}.
- ALWAYS detach re-triggers (\`nohup ... &\` or \`systemctl start\`). Wrapper times out at \{\{TIMEOUT_MIN\}\} min.
- ALWAYS update zombie saturday_runs row to status='failed' BEFORE re-triggering — keeps the dashboard coherent.
- Do NOT spawn subagents.
- Do NOT post to Discord yourself.
- Keep your own audit-session budget under $\{\{COST_CAP_USD\}\}. If multiple bugs interact, use ESCALATION and stop.
`;

const PROMPT_BY_MODE = {
  'daily':            DAILY_PROMPT,
  'saturday':         SATURDAY_PROMPT,
  'saturday-verify':  SATURDAY_VERIFY_PROMPT,
  'weekend-sat':      WEEKEND_SAT_PROMPT,
  'weekend-sun':      WEEKEND_SUN_PROMPT,
};

function buildPrompt({ today, mode = 'daily' }) {
  const tmpl = PROMPT_BY_MODE[mode];
  if (!tmpl) {
    throw new Error(`unknown mode: ${mode} (expected one of: ${Object.keys(PROMPT_BY_MODE).join(', ')})`);
  }
  const cap        = costCapFor(mode);
  const timeoutMin = Math.round(timeoutMsFor(mode) / 60000);
  return tmpl
    .replace(/\{\{TODAY_ISO\}\}/g,    today)
    .replace(/\{\{COST_CAP_USD\}\}/g, cap.toFixed(2))
    .replace(/\{\{TIMEOUT_MIN\}\}/g,  String(timeoutMin));
}

function todayET() {
  // YYYY-MM-DD in America/New_York. Avoids dragging in moment/zoned-time
  // libs — Intl is enough for one date string.
  const fmt = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'America/New_York',
    year: 'numeric', month: '2-digit', day: '2-digit',
  });
  return fmt.format(new Date()); // en-CA → "YYYY-MM-DD"
}

// ── claude-bin spawn (mirrors botjohn-direct.js, drops chat path) ─────
function runClaudeBin(prompt, { timeoutMs } = {}) {
  // Caller passes the mode-resolved timeout. Falls back to the daily
  // default when called without one (legacy test paths + back-compat).
  const effectiveTimeoutMs = Number.isFinite(timeoutMs) ? timeoutMs : timeoutMsFor('daily');
  return new Promise((resolve, reject) => {
    const t0 = Date.now();
    const args = [
      '--dangerously-skip-permissions',
      '-p', prompt,
      '--output-format', 'json',
      '--model', 'claude-sonnet-4-6',
      '--effort', 'high',
    ];
    const child = spawn(CLAUDE_BIN, args, {
      uid: CLAUDE_UID,
      gid: CLAUDE_GID,
      stdio: ['ignore', 'pipe', 'pipe'],
      env: {
        ...process.env,
        HOME: CLAUDE_HOME,
        CLAUDE_HOME,
      },
      cwd: OPENCLAW_DIR,
    });

    let stdout = '', stderr = '';
    child.stdout.on('data', d => { stdout += d.toString(); });
    child.stderr.on('data', d => { stderr += d.toString(); });

    let timedOut = false;
    const t = setTimeout(() => {
      timedOut = true;
      try { child.kill('SIGTERM'); } catch {}
    }, effectiveTimeoutMs);

    child.on('exit', (code) => {
      clearTimeout(t);
      const durationMs = Date.now() - t0;
      if (timedOut) {
        return reject(new Error(`claude-bin timed out at ${Math.round(effectiveTimeoutMs/1000)}s`));
      }
      if (code !== 0 && !stdout) {
        return reject(new Error(`claude-bin exit ${code}: ${stderr.slice(0, 200)}`));
      }
      let parsed;
      try { parsed = JSON.parse(stdout); }
      catch (e) { return reject(new Error(`malformed claude-bin JSON: ${stdout.slice(0, 300)}`)); }
      const result = parsed.result ?? parsed.message ?? '';
      if (!result || !result.trim()) {
        return reject(new Error('empty result from BotJohn'));
      }
      const costUsd = Number(parsed.total_cost_usd ?? parsed.cost_usd ?? 0) || 0;
      const isError = parsed.is_error === true || parsed.subtype === 'error_during_execution';
      // Guard: claude-bin can return success-shaped JSON when something
      // upstream failed — `result` becomes the raw API error string and
      // cost_usd is 0 (no tokens were billed because the assistant message
      // is synthetic). Without this guard the wrapper posts the error
      // string verbatim to #botjohn-log. Seen in production:
      //   - 2026-05-02 OAuth expiry: "Failed to authenticate. API Error: 401"
      //   - 2026-05-06 529 overloaded: "API Error: 529 ... overloaded_error"
      //     where claude-bin internally retried for 225s before giving up,
      //     so a duration gate would false-negative.
      // Real successes always have non-zero cost (input tokens are billed
      // at minimum), so cost===0 plus an error-prefixed result is enough —
      // no duration gate needed. parsed.is_error is the strongest signal
      // when claude-bin sets it; fall back to text match for older shapes.
      const errorPrefixRe = /^(Failed to authenticate|API Error:|401\b|authentication_error|Invalid authentication credentials|Overloaded|overloaded_error)/i;
      const looksLikeUpstreamError =
          isError
          || (costUsd === 0 && errorPrefixRe.test(result.trim()));
      if (looksLikeUpstreamError) {
        return reject(new Error(`claude-bin upstream failure: ${result.trim().slice(0, 200)}`));
      }
      resolve({ result, costUsd, durationMs, raw: stdout });
    });
    child.on('error', (err) => { clearTimeout(t); reject(err); });
  });
}

// ── webhook lookup + post (verbatim shape from daily_health_digest.js) ─
async function getWebhook(agentId, channelKey) {
  const client = new Client({ connectionString: process.env.POSTGRES_URI });
  await client.connect();
  try {
    const r = await client.query(
      'SELECT webhook_urls FROM agent_registry WHERE id=$1',
      [agentId]
    );
    return (r.rows[0]?.webhook_urls || {})[channelKey] || null;
  } finally {
    await client.end();
  }
}

function postWebhook(url, content) {
  return new Promise((resolve) => {
    const u = new URL(url);
    const body = JSON.stringify({ content: content.slice(0, 1900) });
    const req = https.request({
      hostname: u.hostname,
      path:     u.pathname + u.search,
      method:   'POST',
      headers:  { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) },
    }, (res) => {
      let buf = '';
      res.on('data', (c) => buf += c);
      res.on('end', () => resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode, body: buf }));
    });
    req.on('error', (err) => resolve({ ok: false, status: 0, body: err.message }));
    req.write(body);
    req.end();
  });
}

// ── report formatting ─────────────────────────────────────────────────
// Clip preamble narration. Sonnet 4.6 sometimes ignores the prompt's
// "no narration" instruction and prepends "All checks complete..." or
// "Let me compose the maintenance report" before the actual ✅/🔧/🚨
// template. The first scheduled runs (Apr 30 + May 1) buried the green
// line at row 11 of the message; the user couldn't see them at a glance.
// Defensive extraction here is more robust than re-prompting.
// Match any of the three template headers. The Saturday and verify modes
// share the same wrapper so a single regex covers all three.
const TEMPLATE_MARKER_RE = /[✅🔧🚨]\s*\*\*(Daily maintenance|Saturday research|Saturday verify|Saturday strategy-adjustment|Sunday research)/u;

function clipToTemplate(text) {
  if (!text) return '';
  const m = TEMPLATE_MARKER_RE.exec(text);
  // No marker → BotJohn produced a malformed report; pass through so
  // the user still sees something rather than dropping the message.
  if (!m) return text;
  return text.slice(m.index);
}

function formatReport(text, costUsd, durationMs, costCap = COST_CAP_USD) {
  // 1. Strip preamble narration before slicing — otherwise we'd lose
  //    the actual report when claude-bin's narration is long.
  const clipped = clipToTemplate(text || '');
  // 2. Slice the assistant body so the cost footer (and optional
  //    cost-cap line) always fit inside Discord's 1900-char limit.
  const body = clipped.slice(0, 1750).trimEnd();
  const seconds = Math.round((durationMs || 0) / 1000);
  const dollars = (Number.isFinite(costUsd) ? costUsd : 0).toFixed(2);
  let footer = `\n_session cost: $${dollars} | duration: ${seconds}s_`;
  if (Number.isFinite(costUsd) && costUsd > costCap) {
    footer += `\n⚠️ cost exceeded $${costCap.toFixed(2)} budget — investigate.`;
  }
  return body + footer;
}

// ── main ──────────────────────────────────────────────────────────────
async function main(deps = {}) {
  const _runClaudeBin = deps.runClaudeBin || runClaudeBin;
  const _getWebhook   = deps.getWebhook   || getWebhook;
  const _postWebhook  = deps.postWebhook  || postWebhook;

  const mode      = deps.mode || getArg('--mode', 'daily');
  const today     = todayET();
  // Resolve per-mode cap + timeout once. The prompt was already templated
  // with these in buildPrompt(); reusing the same values here keeps the
  // cost-cap warning line, the wrapper kill-deadline, and the boundary
  // text BotJohn read all aligned.
  const costCap   = costCapFor(mode);
  const timeoutMs = timeoutMsFor(mode);
  COST_CAP_USD    = costCap;   // keep formatReport's default sane

  let prompt;
  try {
    prompt = buildPrompt({ today, mode });
  } catch (err) {
    console.error(`[run_maintenance] ${err.message}`);
    process.exitCode = 1;
    return { ok: false, reason: 'unknown_mode', mode };
  }
  console.log(`[run_maintenance] mode=${mode} today=${today} cap=$${costCap.toFixed(2)} timeout=${Math.round(timeoutMs/60000)}min`);

  let session;
  try {
    session = await _runClaudeBin(prompt, { timeoutMs });
  } catch (err) {
    return postFallback(_getWebhook, _postWebhook, `🚨 BotJohn maintenance run failed (mode=${mode}): ${err.message}`);
  }

  const content = formatReport(session.result, session.costUsd, session.durationMs, costCap);

  const url = await _getWebhook('botjohn', 'botjohn-log').catch(() => null);
  if (!url) {
    console.error('[run_maintenance] no #botjohn-log webhook for botjohn in agent_registry — cannot post report');
    process.exitCode = 1;
    return { ok: false, reason: 'no_webhook' };
  }
  const r = await _postWebhook(url, content);
  if (!r.ok) {
    console.error(`[run_maintenance] webhook POST failed: ${r.status} ${r.body}`);
    process.exitCode = 1;
    return { ok: false, reason: 'post_failed', status: r.status };
  }
  console.log(`[run_maintenance] posted ${content.length} chars to #botjohn-log (mode=${mode}, cost $${session.costUsd.toFixed(4)}, ${Math.round(session.durationMs/1000)}s)`);
  return { ok: true, mode, costUsd: session.costUsd, durationMs: session.durationMs };
}

async function postFallback(getWebhookFn, postWebhookFn, content) {
  // Visible entry log — without this, a fallback delivery is indistinguishable
  // from a hard crash in journalctl. 2026-05-05: claudebot OAuth expired at
  // 16:00 UTC and the wrapper's auth-detector triggered postFallback, but
  // the silent path made it look like a hard exit; only the session jsonl
  // revealed the synthetic 401 actually fired.
  console.error(`[run_maintenance] fallback path: ${content.slice(0, 160)}`);
  const url = await getWebhookFn('botjohn', 'botjohn-log').catch((e) => { console.error(`[run_maintenance] webhook lookup failed in fallback: ${e.message}`); return null; });
  if (!url) {
    console.error(`[run_maintenance] FATAL: ${content} (and no webhook to alert on)`);
    process.exitCode = 1;
    return { ok: false, reason: 'no_webhook' };
  }
  const r = await postWebhookFn(url, content);
  if (!r || !r.ok) {
    console.error(`[run_maintenance] fallback POST failed: status=${r?.status} body=${(r?.body || '').slice(0, 200)}`);
    process.exitCode = 1;
    return { ok: false, reason: 'fallback_post_failed', status: r?.status };
  }
  console.error(`[run_maintenance] posted fallback (${content.length} chars) to #botjohn-log (status ${r.status})`);
  process.exitCode = 1;
  return { ok: false, reason: 'wrapper_failure' };
}

module.exports = {
  buildPrompt,
  todayET,
  getArg,
  runClaudeBin,
  getWebhook,
  postWebhook,
  formatReport,
  clipToTemplate,
  postFallback,
  main,
  DAILY_PROMPT,
  SATURDAY_PROMPT,
  SATURDAY_VERIFY_PROMPT,
  WEEKEND_SAT_PROMPT,
  WEEKEND_SUN_PROMPT,
  PROMPT_BY_MODE,
  COST_CAP_BY_MODE,
  TIMEOUT_MS_BY_MODE,
  costCapFor,
  timeoutMsFor,
  // Back-compat aliases — pre-2026-05-02 the daily prompt was the only one,
  // and external callers occasionally read COST_CAP_USD as a constant.
  MAINTENANCE_PROMPT: DAILY_PROMPT,
  get COST_CAP_USD() { return COST_CAP_USD; },
};

if (require.main === module) {
  main()
    .then(() => process.exit(process.exitCode || 0))
    .catch((err) => {
      console.error('[run_maintenance] unexpected:', err);
      process.exit(1);
    });
}
