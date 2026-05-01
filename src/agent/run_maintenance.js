'use strict';

/**
 * run_maintenance.js — daily 12:00 PM ET BotJohn system-maintenance driver.
 *
 * Mon-Fri at 12:00 America/New_York the systemd timer
 *   openclaw-botjohn-maintenance.timer
 * fires this script. We spawn claude-bin with a fixed maintenance prompt
 * (no chat history, no system-context injection — fresh-shot job), capture
 * the assistant's report + cost, append a footer, and POST to Discord
 * #general via the webhook persisted in agent_registry.webhook_urls.
 *
 * On any wrapper failure (claude-bin crash, malformed JSON, empty result,
 * webhook 5xx) we still post a fallback `🚨 BotJohn maintenance run failed`
 * line to #general so silence is never the failure mode.
 *
 * Run as: claudebot user (uid 1001) with .env loaded.
 *   /usr/bin/node src/agent/run_maintenance.js
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

// ── maintenance prompt ────────────────────────────────────────────────
const MAINTENANCE_PROMPT = `# BotJohn — Daily System Maintenance

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

function buildPrompt({ today }) {
  return MAINTENANCE_PROMPT.replace(/\{\{TODAY_ISO\}\}/g, today);
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
const TEMPLATE_MARKER_RE = /[✅🔧🚨]\s*\*\*Daily maintenance/u;

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

  const today = todayET();
  const prompt = buildPrompt({ today });

  let session;
  try {
    session = await _runClaudeBin(prompt);
  } catch (err) {
    return postFallback(_getWebhook, _postWebhook, `🚨 BotJohn maintenance run failed: ${err.message}`);
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
  console.log(`[run_maintenance] posted ${content.length} chars to #general (cost $${session.costUsd.toFixed(4)}, ${Math.round(session.durationMs/1000)}s)`);
  return { ok: true, costUsd: session.costUsd, durationMs: session.durationMs };
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
  runClaudeBin,
  getWebhook,
  postWebhook,
  formatReport,
  clipToTemplate,
  postFallback,
  main,
  MAINTENANCE_PROMPT,
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
