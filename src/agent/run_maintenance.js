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
 * `🚨 BotJohn maintenance run failed` line to #general so silence is
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
const CLAUDE_TIMEOUT_MS = parseInt(process.env.MAINT_CLAUDE_TIMEOUT_MS || '1800000', 10); // 30 min
const COST_CAP_USD = parseFloat(process.env.MAINT_COST_CAP_USD || '5.00');

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

1. Health check:
       python3 src/maintenance/doctor.py --json
   Parse the JSON. Note exit code (0 pass / 1 warn / 2 fail) and any
   check with status "warn" or "fail".

2. Digest:
       node src/pipeline/daily_health_digest.js --dry-run
   Cross-reference with doctor's findings.

3. If anything is off, investigate:
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

4. Fix and re-run (only if a real failure is found):
   - Apply code fixes directly. You have full filesystem write access.
   - Commit with \`[botjohn-maint] <reason>\` and \`git push\`.
   - Re-trigger the cycle SYNCHRONOUSLY:
         python3 scripts/run_pipeline.py --force-resume --date {{TODAY_ISO}}
     Wait for it to finish (5–10 min). Capture the result. Do not loop.

5. Idempotency: if doctor green + digest healthy + DB row counts plausible,
   green-light it. Don't manufacture work.

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
- Keep total session budget under $5. If your work expands beyond a
  single root cause, write the cannot-auto-fix template and stop.
`;

const SATURDAY_PROMPT = `# BotJohn — Saturday Research Maintenance

You are BotJohn, portfolio manager and orchestrator of the OpenClaw quant
hedge fund. Today is {{TODAY_ISO}} (America/New_York). It is 16:00 ET on
Saturday. The 10:00 AM saturday-brain pipeline should have completed
(typical runtime ~1h; recent runs landed by ~11:00 ET). At 16:00 ET the
run has been done for hours — anything still 'running' is a zombie.

Your job: audit the run, report counts to #general, fix anything broken
SURGICALLY, and re-trigger the appropriate recovery lever DETACHED. You
have 30 minutes wrapper budget; you cannot wait for a saturday-brain
re-run (~1h). Sunday 12:00 ET verify run will close the loop on
whatever recovery you kick off.

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
    SELECT name, status, backtest_sharpe, backtest_return_pct, backtest_max_dd_pct
      FROM strategy_registry
     WHERE created_at::date = '{{TODAY_ISO}}'::date
        OR updated_at::date = '{{TODAY_ISO}}'::date
     ORDER BY backtest_sharpe DESC NULLS LAST
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
          nohup /usr/bin/node /root/openclaw/src/agent/curators/saturday_brain_finisher.js \\
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
     3. systemctl start openclaw-saturday-brain.service
        (full re-run, ~1h, ~$40)

  status='running' >7h (ZOMBIE)
     1. UPDATE saturday_runs
           SET status='failed', finished_at=NOW(),
               error_detail=jsonb_build_object('reason','zombie_killed_by_botjohn',
                                                'killed_at',NOW())
         WHERE run_id='<run_id>' AND status='running';
     2. systemctl start openclaw-saturday-brain.service

  papers_ingested == 0 (arxiv/openalex API issue)
     If \`curl https://export.arxiv.org/api/query?search_query=cat:q-fin.PM&max_results=1\`
     fails → ESCALATE (transient, retry next week).
     If logic bug → fix code, commit, full re-trigger.

  cost_usd > 100  → ESCALATE. Do not re-trigger.

After any re-trigger, confirm it actually started:
  systemctl status openclaw-saturday-brain.service --no-pager | head -10
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
- NEVER full re-trigger when surgical lever applies. Cost: ~$40 vs ~$5.
- ALWAYS detach re-triggers (\`nohup ... &\` or \`systemctl start\`). Wrapper times out at 30 min.
- ALWAYS update zombie saturday_runs row to status='failed' BEFORE re-triggering — keeps the dashboard coherent.
- Do NOT spawn subagents.
- Do NOT post to Discord yourself.
- Keep your own audit-session budget under $5. If multiple bugs interact, use ESCALATION and stop.
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
    Check: \`systemctl status openclaw-saturday-brain.timer\`

## Boundaries
- READ-ONLY. No fixes, commits, or re-triggers.
- Keep audit budget under $2.
- Do NOT post yourself; wrapper handles posting.
`;

const PROMPT_BY_MODE = {
  'daily':            DAILY_PROMPT,
  'saturday':         SATURDAY_PROMPT,
  'saturday-verify':  SATURDAY_VERIFY_PROMPT,
};

function buildPrompt({ today, mode = 'daily' }) {
  const tmpl = PROMPT_BY_MODE[mode];
  if (!tmpl) {
    throw new Error(`unknown mode: ${mode} (expected one of: ${Object.keys(PROMPT_BY_MODE).join(', ')})`);
  }
  return tmpl.replace(/\{\{TODAY_ISO\}\}/g, today);
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
function runClaudeBin(prompt) {
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
    }, CLAUDE_TIMEOUT_MS);

    child.on('exit', (code) => {
      clearTimeout(t);
      const durationMs = Date.now() - t0;
      if (timedOut) {
        return reject(new Error(`claude-bin timed out at ${Math.round(CLAUDE_TIMEOUT_MS/1000)}s`));
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
      // Guard: claude-bin can return success-shaped JSON when the OAuth
      // token is expired — `result` becomes the literal API error
      // ("Failed to authenticate. API Error: 401 ...") and cost_usd is 0.
      // Without this guard the wrapper happily posts the error string to
      // #general (saw this 2026-05-02 — Saturday's first scheduled run
      // posted "Failed to authenticate" verbatim instead of triggering
      // the 🚨 fallback). Detect by signature: 0 cost + sub-30s + the
      // result starts with an error-like prefix.
      const looksLikeAuthError =
          costUsd === 0
          && durationMs < 30_000
          && /^(Failed to authenticate|API Error:|401\b|authentication_error|Invalid authentication credentials)/i.test(result.trim());
      if (looksLikeAuthError) {
        return reject(new Error(`claude-bin auth failure: ${result.trim().slice(0, 200)}`));
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
const TEMPLATE_MARKER_RE = /[✅🔧🚨]\s*\*\*(Daily maintenance|Saturday research|Saturday verify)/u;

function clipToTemplate(text) {
  if (!text) return '';
  const m = TEMPLATE_MARKER_RE.exec(text);
  // No marker → BotJohn produced a malformed report; pass through so
  // the user still sees something rather than dropping the message.
  if (!m) return text;
  return text.slice(m.index);
}

function formatReport(text, costUsd, durationMs) {
  // 1. Strip preamble narration before slicing — otherwise we'd lose
  //    the actual report when claude-bin's narration is long.
  const clipped = clipToTemplate(text || '');
  // 2. Slice the assistant body so the cost footer (and optional
  //    cost-cap line) always fit inside Discord's 1900-char limit.
  const body = clipped.slice(0, 1750).trimEnd();
  const seconds = Math.round((durationMs || 0) / 1000);
  const dollars = (Number.isFinite(costUsd) ? costUsd : 0).toFixed(2);
  let footer = `\n_session cost: $${dollars} | duration: ${seconds}s_`;
  if (Number.isFinite(costUsd) && costUsd > COST_CAP_USD) {
    footer += `\n⚠️ cost exceeded $${COST_CAP_USD.toFixed(2)} budget — investigate.`;
  }
  return body + footer;
}

// ── main ──────────────────────────────────────────────────────────────
async function main(deps = {}) {
  const _runClaudeBin = deps.runClaudeBin || runClaudeBin;
  const _getWebhook   = deps.getWebhook   || getWebhook;
  const _postWebhook  = deps.postWebhook  || postWebhook;

  const mode  = deps.mode || getArg('--mode', 'daily');
  const today = todayET();
  let prompt;
  try {
    prompt = buildPrompt({ today, mode });
  } catch (err) {
    console.error(`[run_maintenance] ${err.message}`);
    process.exitCode = 1;
    return { ok: false, reason: 'unknown_mode', mode };
  }
  console.log(`[run_maintenance] mode=${mode} today=${today}`);

  let session;
  try {
    session = await _runClaudeBin(prompt);
  } catch (err) {
    return postFallback(_getWebhook, _postWebhook, `🚨 BotJohn maintenance run failed (mode=${mode}): ${err.message}`);
  }

  const content = formatReport(session.result, session.costUsd, session.durationMs);

  const url = await _getWebhook('botjohn', 'general').catch(() => null);
  if (!url) {
    console.error('[run_maintenance] no #general webhook for botjohn in agent_registry — cannot post report');
    process.exitCode = 1;
    return { ok: false, reason: 'no_webhook' };
  }
  const r = await _postWebhook(url, content);
  if (!r.ok) {
    console.error(`[run_maintenance] webhook POST failed: ${r.status} ${r.body}`);
    process.exitCode = 1;
    return { ok: false, reason: 'post_failed', status: r.status };
  }
  console.log(`[run_maintenance] posted ${content.length} chars to #general (mode=${mode}, cost $${session.costUsd.toFixed(4)}, ${Math.round(session.durationMs/1000)}s)`);
  return { ok: true, mode, costUsd: session.costUsd, durationMs: session.durationMs };
}

async function postFallback(getWebhookFn, postWebhookFn, content) {
  const url = await getWebhookFn('botjohn', 'general').catch(() => null);
  if (!url) {
    console.error(`[run_maintenance] FATAL: ${content} (and no webhook to alert on)`);
    process.exitCode = 1;
    return { ok: false, reason: 'no_webhook' };
  }
  await postWebhookFn(url, content);
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
  PROMPT_BY_MODE,
  // Back-compat alias — pre-2026-05-02 the daily prompt was the only one.
  MAINTENANCE_PROMPT: DAILY_PROMPT,
  COST_CAP_USD,
};

if (require.main === module) {
  main()
    .then(() => process.exit(process.exitCode || 0))
    .catch((err) => {
      console.error('[run_maintenance] unexpected:', err);
      process.exit(1);
    });
}
