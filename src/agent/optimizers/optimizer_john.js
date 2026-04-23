'use strict';

/**
 * optimizer_john.js — FundJohn weekly self-optimization loop.
 *
 * Fires Sunday 09:00 ET via cron-schedule.js (after the 08:00
 * maintenance block so universe_sync, data_ledger refresh, and
 * signatures have completed).
 *
 * Flow:
 *   1. Pull 7d of telemetry from Postgres + Redis.
 *   2. Load current subagent prompts from src/agent/prompts/subagents/.
 *   3. Invoke the `optimizer-john` subagent (Opus 4.7, 1M ctx, $4 cap).
 *   4. Parse the envelope: one markdown memo + a fenced `patches` JSON array.
 *   5. Validate each patch against protected-path rules; drop anything
 *      that fails. Write survivors to workspaces/default/optimizer/queue/.
 *   6. Post the memo (+ patch IDs + apply instructions) to Discord `#ops`.
 *
 * Patches never auto-apply. Operator runs `!john /optimizer apply <id>`
 * (or `patch -p1 < <file>`) to land them.
 */

const fs   = require('fs');
const path = require('path');
const { spawn } = require('child_process');
const { Pool } = require('pg');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const NODE_CLI     = path.join(OPENCLAW_DIR, 'src/agent/run-subagent-cli.js');
const WORKSPACE    = path.join(OPENCLAW_DIR, 'workspaces/default');
const QUEUE_DIR    = path.join(WORKSPACE, 'optimizer', 'queue');
const PROMPT_DIR   = path.join(OPENCLAW_DIR, 'src/agent/prompts/subagents');

const MAX_PATCHES_PER_RUN = 5;

// Protected paths — any patch touching any of these is rejected before
// it hits the queue. This is the only safety rail between the LLM and
// real code; keep the list tight and obvious.
const PROTECTED_PREFIXES = [
  'src/strategies/',
  'src/agent/config/subagent-types.json',
  'src/agent/config/models.js',
  'config/budget.json',
  'src/database/migrations/',
];

let _pool = null;
function pool() {
  if (!_pool) _pool = new Pool({ connectionString: process.env.POSTGRES_URI });
  return _pool;
}
async function query(sql, params = []) { return pool().query(sql, params); }

// ── Telemetry loaders ────────────────────────────────────────────────────────

// token_usage rows contain one row per (workspace, agent_type, date).
// Cost is denormalized on each row.
async function loadSubagentCosts7d() {
  try {
    const { rows } = await query(
      `SELECT agent_type,
              SUM(cost_usd)::float   AS cost_7d,
              SUM(call_count)::int   AS calls_7d,
              AVG(avg_tokens_in)::int AS avg_in,
              AVG(avg_tokens_out)::int AS avg_out
         FROM token_usage
        WHERE day >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY agent_type
        ORDER BY cost_7d DESC NULLS LAST`,
    );
    return rows;
  } catch (e) {
    console.error(`[optimizer] subagent_costs_7d unavailable: ${e.message}`);
    return [];
  }
}

async function loadCacheHit7d() {
  try {
    const { rows } = await query(
      `SELECT agent_type,
              SUM(cache_read_tokens)::bigint    AS cache_read,
              SUM(non_cache_input_tokens)::bigint AS non_cache_in,
              CASE WHEN SUM(cache_read_tokens)+SUM(non_cache_input_tokens) = 0 THEN NULL
                   ELSE ROUND(SUM(cache_read_tokens)::numeric
                              / (SUM(cache_read_tokens)+SUM(non_cache_input_tokens)), 3)
              END AS cache_hit_ratio
         FROM cache_tokens
        WHERE day >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY agent_type`,
    );
    return rows;
  } catch (e) {
    console.error(`[optimizer] cache_hit_7d unavailable: ${e.message}`);
    return [];
  }
}

async function loadVetoDigest30d() {
  try {
    const { rows } = await query(
      `SELECT strategy_id, veto_reason, COUNT(*)::int AS n
         FROM veto_log
        WHERE run_date >= CURRENT_DATE - INTERVAL '30 days'
          AND strategy_id IS NOT NULL
        GROUP BY strategy_id, veto_reason
        ORDER BY strategy_id, n DESC`,
    );
    return rows;
  } catch (e) {
    console.error(`[optimizer] veto_digest unavailable: ${e.message}`);
    return [];
  }
}

async function loadCuratorCalibration() {
  try {
    const { rows } = await query(
      `SELECT bucket, n_evaluated, pass_rate, target_pass_rate,
              over_confidence_bias
         FROM curator_bucket_calibration
        ORDER BY bucket`,
    );
    return rows;
  } catch (e) {
    console.error(`[optimizer] curator_calibration unavailable: ${e.message}`);
    return [];
  }
}

async function loadEvCalibrationSummary() {
  try {
    const { rows } = await query(
      `WITH closed AS (
         SELECT strategy_id,
                realized_pnl_pct::float AS pnl,
                closed_at
           FROM signal_pnl
          WHERE status = 'closed'
            AND realized_pnl_pct IS NOT NULL
            AND closed_at >= CURRENT_DATE - INTERVAL '30 days'
       )
       SELECT strategy_id,
              COUNT(*)::int                               AS n_closed,
              AVG(CASE WHEN pnl > 0 THEN 1 ELSE 0 END)::float AS hit_rate,
              AVG(pnl)::float                              AS realized_pnl_avg,
              AVG(pnl) FILTER (WHERE closed_at >= CURRENT_DATE - INTERVAL '15 days')::float AS recent_avg,
              AVG(pnl) FILTER (WHERE closed_at <  CURRENT_DATE - INTERVAL '15 days')::float AS prior_avg
         FROM closed
         GROUP BY strategy_id
         ORDER BY strategy_id`,
    );
    return rows.map((r) => ({
      ...r,
      drift_score: (r.recent_avg != null && r.prior_avg != null)
        ? Number((r.recent_avg - r.prior_avg).toFixed(4))
        : null,
    }));
  } catch (e) {
    console.error(`[optimizer] ev_calibration_summary unavailable: ${e.message}`);
    return [];
  }
}

async function loadPipelineHealth7d() {
  try {
    const { rows } = await query(
      `SELECT step,
              AVG(duration_seconds)::numeric(10,2) AS avg_duration_s,
              COUNT(*) FILTER (WHERE status='failed')::int AS failed,
              COUNT(*)::int AS runs
         FROM pipeline_runs
        WHERE started_at >= CURRENT_DATE - INTERVAL '7 days'
        GROUP BY step
        ORDER BY avg_duration_s DESC NULLS LAST`,
    );
    return rows;
  } catch (e) {
    console.error(`[optimizer] pipeline_health unavailable: ${e.message}`);
    return [];
  }
}

function loadRecentPrompts() {
  const out = {};
  try {
    for (const f of fs.readdirSync(PROMPT_DIR)) {
      if (!f.endsWith('.md')) continue;
      const key = f.replace(/\.md$/, '');
      out[key] = fs.readFileSync(path.join(PROMPT_DIR, f), 'utf8');
    }
  } catch (e) {
    console.error(`[optimizer] prompt snapshot failed: ${e.message}`);
  }
  return out;
}

function loadPreviousOptimizerMemos(n = 4) {
  const memoDir = path.join(WORKSPACE, 'optimizer', 'memos');
  try {
    if (!fs.existsSync(memoDir)) return [];
    const files = fs.readdirSync(memoDir)
      .filter((f) => f.endsWith('.md'))
      .sort()
      .slice(-n);
    return files.map((f) => ({
      date: f.replace(/\.md$/, ''),
      body: fs.readFileSync(path.join(memoDir, f), 'utf8').slice(0, 4000),
    }));
  } catch (e) {
    console.error(`[optimizer] prior memos unavailable: ${e.message}`);
    return [];
  }
}

// ── Subagent invocation ──────────────────────────────────────────────────────

function spawnSubagent(contextPayload, runDate) {
  return new Promise((resolve, reject) => {
    const tmp = path.join('/tmp', `optimizer-ctx-${Date.now()}.json`);
    fs.writeFileSync(tmp, JSON.stringify(contextPayload));
    const args = [
      NODE_CLI,
      '--type', 'optimizer-john',
      '--ticker', `optimizer-${runDate}`,
      '--workspace', WORKSPACE,
      '--context-file', tmp,
    ];
    const child = spawn('node', args, {
      cwd: OPENCLAW_DIR,
      env: { ...process.env, OPENCLAW_DIR },
    });
    let out = '';
    let err = '';
    child.stdout.on('data', (d) => { out += d; });
    child.stderr.on('data', (d) => { err += d; process.stderr.write(d); });
    child.on('exit', (code) => {
      try { fs.unlinkSync(tmp); } catch { /* ignore */ }
      if (code === 0) resolve(out);
      else reject(new Error(`optimizer-john exited ${code}: ${err.slice(0, 400)}`));
    });
  });
}

function parseEnvelope(raw) {
  let memo = null;
  let cost = 0;
  for (const line of raw.split('\n').reverse()) {
    const l = line.trim();
    if (!l.startsWith('{')) continue;
    try {
      const env = JSON.parse(l);
      if (env.subtype === 'success' && typeof env.result === 'string') {
        memo = env.result;
        cost = env.total_cost_usd || 0;
        break;
      }
    } catch { /* ignore */ }
  }
  return { memo, cost };
}

function extractPatchBlock(memo) {
  if (!memo) return [];
  const m = /```patches\s*([\s\S]*?)```/i.exec(memo);
  if (!m) return [];
  try {
    const parsed = JSON.parse(m[1].trim());
    if (!Array.isArray(parsed)) return [];
    return parsed.slice(0, MAX_PATCHES_PER_RUN);
  } catch {
    return [];
  }
}

// ── Patch validation ─────────────────────────────────────────────────────────

function isProtected(targetPath) {
  if (!targetPath || typeof targetPath !== 'string') return true;
  const norm = targetPath.replace(/^\/+/, '').replace(/^\.\//, '');
  return PROTECTED_PREFIXES.some((p) => norm.startsWith(p));
}

function validatePatch(patch) {
  const problems = [];
  if (!patch || typeof patch !== 'object') { problems.push('not an object'); return problems; }
  if (!patch.id || !/^[a-z0-9][a-z0-9-_]{2,60}$/i.test(patch.id)) problems.push('invalid id');
  if (!patch.target || typeof patch.target !== 'string') problems.push('missing target');
  else if (isProtected(patch.target))                    problems.push(`target is protected: ${patch.target}`);
  if (!patch.evidence || !patch.evidence.length)         problems.push('missing evidence');
  if (!patch.rationale || !patch.rationale.length)       problems.push('missing rationale');
  if (!patch.diff || !patch.diff.includes('@@'))         problems.push('missing/invalid unified diff');
  // Also scan diff for any protected path reference (the diff header may
  // include files beyond `target`).
  if (patch.diff) {
    const headerPaths = [];
    const re = /^\+\+\+ b\/(.+)$|^--- a\/(.+)$/gm;
    let mm;
    while ((mm = re.exec(patch.diff)) !== null) {
      const p = mm[1] || mm[2];
      if (p) headerPaths.push(p);
    }
    for (const p of headerPaths) {
      if (isProtected(p)) { problems.push(`diff touches protected path: ${p}`); break; }
    }
  }
  return problems;
}

function writePatchToQueue(patch, runDate) {
  fs.mkdirSync(QUEUE_DIR, { recursive: true });
  const fname = `${runDate}_${patch.id}.patch`;
  const header =
    `# Optimizer-John patch\n` +
    `# Generated: ${runDate}\n` +
    `# Target:    ${patch.target}\n` +
    `# Rationale: ${patch.rationale}\n` +
    `# Evidence:  ${patch.evidence}\n` +
    `# Apply:     cd /root/openclaw && patch -p1 < workspaces/default/optimizer/queue/${fname}\n` +
    `# Discard:   rm workspaces/default/optimizer/queue/${fname}\n` +
    `#\n`;
  fs.writeFileSync(path.join(QUEUE_DIR, fname), header + patch.diff + '\n');
  return fname;
}

function saveMemo(memo, runDate) {
  const memoDir = path.join(WORKSPACE, 'optimizer', 'memos');
  fs.mkdirSync(memoDir, { recursive: true });
  fs.writeFileSync(path.join(memoDir, `${runDate}.md`), memo);
}

// ── Discord posting ──────────────────────────────────────────────────────────

function httpsRequest(urlStr, opts, body) {
  return new Promise((resolve) => {
    const https = require('https');
    const u = new URL(urlStr);
    const req = https.request({
      hostname: u.hostname, port: 443, path: u.pathname + (u.search || ''),
      method: opts.method || 'POST',
      headers: opts.headers || {},
    }, (res) => {
      let chunks = '';
      res.on('data', (d) => chunks += d);
      res.on('end', () => resolve({ ok: res.statusCode < 300, status: res.statusCode, body: chunks }));
    });
    req.on('error', (e) => resolve({ ok: false, status: 0, body: e.message }));
    if (body) req.write(body);
    req.end();
  });
}

async function findChannelId(botToken, name) {
  const guilds = await httpsRequest(
    'https://discord.com/api/v10/users/@me/guilds',
    { method: 'GET', headers: { Authorization: `Bot ${botToken}` } },
  );
  if (!guilds.ok) return null;
  for (const g of JSON.parse(guilds.body)) {
    const channels = await httpsRequest(
      `https://discord.com/api/v10/guilds/${g.id}/channels`,
      { method: 'GET', headers: { Authorization: `Bot ${botToken}` } },
    );
    if (!channels.ok) continue;
    for (const ch of JSON.parse(channels.body)) {
      if (ch.name === name && ch.type === 0) return ch.id;
    }
  }
  return null;
}

async function postToOps(memo, accepted, rejected) {
  const token = process.env.DISCORD_BOT_TOKEN;
  if (!token) return false;
  const chId = await findChannelId(token, 'ops');
  if (!chId) return false;
  let summary = memo;
  if (accepted.length || rejected.length) {
    summary += '\n\n---\n';
    if (accepted.length) {
      summary += `**Queued patches (${accepted.length}):**\n`;
      for (const a of accepted) summary += `- \`${a.fname}\` — ${a.rationale}\n`;
      summary += `\nApply: \`!john /optimizer apply <id>\`. Discard: \`!john /optimizer discard <id>\`.\n`;
    }
    if (rejected.length) {
      summary += `\n**Rejected ${rejected.length} patch(es) on safety rails:**\n`;
      for (const r of rejected) summary += `- \`${r.id || 'unnamed'}\` — ${r.reasons.join(', ')}\n`;
    }
  }
  let remaining = summary;
  while (remaining) {
    const chunk = remaining.slice(0, 1900);
    remaining = remaining.slice(1900);
    await httpsRequest(
      `https://discord.com/api/v10/channels/${chId}/messages`,
      { method: 'POST', headers: { Authorization: `Bot ${token}`, 'Content-Type': 'application/json' } },
      JSON.stringify({ content: chunk }),
    );
  }
  return true;
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function run({ dryRun = false, notify = null } = {}) {
  const log = (m) => { notify?.(m); console.error(`[optimizer] ${m}`); };
  const runDate = new Date().toISOString().slice(0, 10);
  log(`starting weekly tune-up for ${runDate}`);

  const [
    subagentCosts, cacheHit, vetoDigest, curatorCalib,
    evCalib, pipelineHealth,
  ] = await Promise.all([
    loadSubagentCosts7d(),
    loadCacheHit7d(),
    loadVetoDigest30d(),
    loadCuratorCalibration(),
    loadEvCalibrationSummary(),
    loadPipelineHealth7d(),
  ]);
  const recentPrompts = loadRecentPrompts();
  const priorMemos    = loadPreviousOptimizerMemos();

  const ctx = {
    run_date:                  runDate,
    subagent_costs_7d:         subagentCosts,
    cache_hit_7d:              cacheHit,
    veto_digest_30d:           vetoDigest,
    curator_calibration:       curatorCalib,
    ev_calibration_summary:    evCalib,
    pipeline_health_7d:        pipelineHealth,
    recent_prompts:            recentPrompts,
    previous_optimizer_memos:  priorMemos,
    protected_prefixes:        PROTECTED_PREFIXES,
    max_patches:               MAX_PATCHES_PER_RUN,
  };

  log(`telemetry assembled: ${subagentCosts.length} agents, ${evCalib.length} strategies, ${Object.keys(recentPrompts).length} prompts`);

  if (dryRun) {
    log('dry-run: skipping Opus call + Discord post');
    return { runDate, memo: null, patchesQueued: [], patchesRejected: [], ctx };
  }

  log('calling optimizer-john (Opus 4.7)...');
  let memo = null;
  let cost = 0;
  try {
    const raw = await spawnSubagent(ctx, runDate);
    const parsed = parseEnvelope(raw);
    memo = parsed.memo;
    cost = parsed.cost;
  } catch (e) {
    log(`optimizer-john call failed: ${e.message}`);
    return { runDate, memo: null, error: e.message, patchesQueued: [], patchesRejected: [] };
  }
  log(`optimizer-john returned — $${cost.toFixed(3)} spent`);

  if (!memo) {
    log('no memo returned — aborting');
    return { runDate, memo: null, patchesQueued: [], patchesRejected: [] };
  }

  saveMemo(memo, runDate);

  const patches = extractPatchBlock(memo);
  log(`${patches.length} patch(es) proposed`);

  const accepted = [];
  const rejected = [];
  for (const p of patches) {
    const problems = validatePatch(p);
    if (problems.length) {
      rejected.push({ id: p?.id, reasons: problems });
      continue;
    }
    try {
      const fname = writePatchToQueue(p, runDate);
      accepted.push({ id: p.id, fname, rationale: p.rationale });
    } catch (e) {
      rejected.push({ id: p.id, reasons: [`write failed: ${e.message}`] });
    }
  }

  await postToOps(memo, accepted, rejected).catch((e) => log(`discord post failed: ${e.message}`));

  log(`done — ${accepted.length} accepted, ${rejected.length} rejected, cost $${cost.toFixed(2)}`);
  return { runDate, memo, cost, patchesQueued: accepted, patchesRejected: rejected };
}

// ── Patch queue helpers (used by Discord !john /optimizer commands) ─────────

function listQueue() {
  if (!fs.existsSync(QUEUE_DIR)) return [];
  return fs.readdirSync(QUEUE_DIR)
    .filter((f) => f.endsWith('.patch'))
    .sort()
    .map((f) => {
      const full = path.join(QUEUE_DIR, f);
      const head = fs.readFileSync(full, 'utf8').split('\n').slice(0, 8).join('\n');
      return { file: f, fullPath: full, head };
    });
}

function applyPatch(idOrFile) {
  const all = listQueue();
  const match = all.find((q) => q.file === idOrFile || q.file.includes(idOrFile));
  if (!match) return { ok: false, error: `no patch matching ${idOrFile}` };
  try {
    const { execSync } = require('child_process');
    const out = execSync(`patch -p1 < ${match.fullPath}`, { cwd: OPENCLAW_DIR, encoding: 'utf8' });
    // Move out of queue to applied/
    const appliedDir = path.join(WORKSPACE, 'optimizer', 'applied');
    fs.mkdirSync(appliedDir, { recursive: true });
    fs.renameSync(match.fullPath, path.join(appliedDir, match.file));
    return { ok: true, output: out, file: match.file };
  } catch (e) {
    return { ok: false, error: e.message };
  }
}

function discardPatch(idOrFile) {
  const all = listQueue();
  const match = all.find((q) => q.file === idOrFile || q.file.includes(idOrFile));
  if (!match) return { ok: false, error: `no patch matching ${idOrFile}` };
  const discardedDir = path.join(WORKSPACE, 'optimizer', 'discarded');
  fs.mkdirSync(discardedDir, { recursive: true });
  fs.renameSync(match.fullPath, path.join(discardedDir, match.file));
  return { ok: true, file: match.file };
}

module.exports = { run, listQueue, applyPatch, discardPatch, PROTECTED_PREFIXES };
