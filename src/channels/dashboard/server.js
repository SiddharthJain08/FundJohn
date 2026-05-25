/**
 * FundJohn self-hosted dashboard — :7870
 *
 * Single Express process that surfaces:
 *   - Bot registry   (systemctl is-active for known units)
 *   - Subagent swarm (Redis subagent:* keys)
 *   - Analyses       (Postgres analyses + verdict_cache)
 *   - Trades         (Postgres trades)
 *   - Workspaces     (filesystem /root/openclaw/workspaces/*)
 *   - LangGraph runs (in-memory traceBus)
 *
 * Streams live updates via SSE on /api/stream.
 */
'use strict';

const express = require('express');
const path = require('path');
const fs = require('fs');
const { exec } = require('child_process');
const { promisify } = require('util');

require('dotenv').config({ path: path.join(__dirname, '../../../.env') });

const { query } = require('../../database/postgres');
const redis = require('../../database/redis');
const traceBus = require('../../agent/traceBus');
const graph = require('../../agent/graph');
const graphRegistry = require('../../agent/graphs');

const execP = promisify(exec);

const PORT = parseInt(process.env.FUNDJOHN_DASHBOARD_PORT || '7870', 10);
const BIND = process.env.FUNDJOHN_DASHBOARD_BIND || '127.0.0.1';
const WORKSPACES_ROOT = path.join(process.env.OPENCLAW_DIR || '/root/openclaw', 'workspaces');

const SYSTEMD_UNITS = [
  'johnbot',
  'fundjohn-dashboard',
  'openclaw-curator',
  'openclaw-curator.timer',
  'postgresql',
  'redis-server',
];

const app = express();
app.use(express.json());

// ─────────────────────────── Bots ────────────────────────────────────────────
async function unitStatus(unit) {
  const out = { unit, active: 'unknown', sub: '', since: '', memory: '' };
  try {
    const { stdout } = await execP(
      `systemctl show ${unit} -p ActiveState,SubState,ActiveEnterTimestamp,MemoryCurrent --no-pager`,
      { timeout: 3000 }
    );
    for (const line of stdout.split('\n')) {
      const [k, v] = line.split('=');
      if (k === 'ActiveState') out.active = v;
      else if (k === 'SubState') out.sub = v;
      else if (k === 'ActiveEnterTimestamp') out.since = v;
      else if (k === 'MemoryCurrent' && v && v !== '[not set]') {
        const n = parseInt(v, 10);
        if (Number.isFinite(n)) out.memory = `${(n / 1024 / 1024).toFixed(1)} MB`;
      }
    }
  } catch (err) {
    out.error = err.message;
  }
  return out;
}

app.get('/api/bots', async (_req, res) => {
  const rows = await Promise.all(SYSTEMD_UNITS.map(unitStatus));
  res.json({ units: rows });
});

// ─────────────────────────── Subagents (Redis) ───────────────────────────────
app.get('/api/subagents', async (_req, res) => {
  try {
    const r = redis.getClient();
    const keys = await r.keys('subagent:*');
    const pipeline = r.pipeline();
    for (const k of keys) pipeline.get(k);
    const results = await pipeline.exec();
    const subagents = [];
    for (let i = 0; i < keys.length; i++) {
      const [, val] = results[i];
      if (!val) continue;
      try {
        const parsed = JSON.parse(val);
        subagents.push({ key: keys[i], ...parsed });
      } catch {
        subagents.push({ key: keys[i], raw: val });
      }
    }
    subagents.sort((a, b) => (b.startedAt || 0) - (a.startedAt || 0));
    res.json({ subagents });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/redis-keys', async (_req, res) => {
  try {
    const r = redis.getClient();
    const groups = {};
    for (const pattern of ['subagent:*', 'steering:*', 'rate_limit:*', 'ratelimit:*', 'engine:last_run:*', 'cache:*']) {
      const keys = await r.keys(pattern);
      groups[pattern] = keys.length;
    }
    res.json({ groups });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ─────────────────────────── Analyses + Verdicts (Postgres) ──────────────────
app.get('/api/analyses', async (_req, res) => {
  try {
    const { rows } = await query(
      `SELECT id, workspace_id, ticker, analysis_type, verdict, signals, stale_after, created_at
         FROM analyses
        ORDER BY created_at DESC NULLS LAST
        LIMIT 100`
    );
    res.json({ analyses: rows });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/verdicts', async (_req, res) => {
  try {
    const { rows } = await query(
      `SELECT ticker, analysis_date, analysis_type, verdict, score,
              bull_target, bear_target, ev_pct, position_size_pct,
              risk_verdict, stale_after
         FROM verdict_cache
        ORDER BY analysis_date DESC NULLS LAST
        LIMIT 100`
    );
    res.json({ verdicts: rows });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/trades', async (_req, res) => {
  try {
    const { rows } = await query(
      `SELECT id, ticker, direction, entry_low, entry_high, stop_loss, targets,
              position_size_pct, ev_pct, risk_verdict, timing_signal,
              status, created_at, executed_at
         FROM trades
        ORDER BY created_at DESC
        LIMIT 100`
    );
    res.json({ trades: rows });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/checkpoints', async (_req, res) => {
  try {
    const { rows } = await query(
      `SELECT id, thread_id, subagent_type, ticker, status, created_at, completed_at
         FROM checkpoints
        ORDER BY created_at DESC
        LIMIT 50`
    );
    res.json({ checkpoints: rows });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ─────────────────────────── Workspaces (filesystem) ─────────────────────────
app.get('/api/workspaces', async (_req, res) => {
  try {
    const entries = fs.existsSync(WORKSPACES_ROOT)
      ? fs.readdirSync(WORKSPACES_ROOT, { withFileTypes: true }).filter(e => e.isDirectory())
      : [];
    const workspaces = entries.map(e => {
      const p = path.join(WORKSPACES_ROOT, e.name);
      const out = { name: e.name, path: p, subdirs: [] };
      try {
        const subs = fs.readdirSync(p, { withFileTypes: true })
          .filter(s => s.isDirectory())
          .map(s => s.name);
        out.subdirs = subs;
      } catch { /* ignore */ }
      // Key memory files
      const memDir = path.join(p, 'memory');
      if (fs.existsSync(memDir)) {
        out.memoryFiles = fs.readdirSync(memDir).slice(0, 20);
      }
      return out;
    });
    res.json({ workspaces });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/workspaces/:name/memory/:file', (req, res) => {
  const { name, file } = req.params;
  // Strict allowlist — reject anything that isn't a plain identifier / filename.
  if (!/^[A-Za-z0-9_.-]+$/.test(name) || !/^[A-Za-z0-9_.-]+$/.test(file)) {
    return res.status(400).json({ error: 'bad path' });
  }
  const fp = path.resolve(WORKSPACES_ROOT, name, 'memory', file);
  const expectedPrefix = path.resolve(WORKSPACES_ROOT) + path.sep;
  if (!fp.startsWith(expectedPrefix)) return res.status(400).json({ error: 'bad path' });
  if (!fs.existsSync(fp) || !fs.statSync(fp).isFile()) return res.status(404).json({ error: 'not found' });
  res.type('text/plain').send(fs.readFileSync(fp, 'utf8').slice(0, 200_000));
});

// ─────────────────────────── LangGraph runs / traces ─────────────────────────
app.get('/api/runs', (_req, res) => {
  res.json({ runs: traceBus.listRuns() });
});

app.get('/api/runs/:id', (req, res) => {
  const run = traceBus.getRun(req.params.id);
  if (!run) return res.status(404).json({ error: 'not found' });
  res.json({ run });
});

// ─────────────────────────── LangGraph HITL resume ───────────────────────────
// POST /api/runs/:threadId/resume  { approval: 'approved' | 'vetoed' }
// threadId is the LangGraph thread_id (we default it to runId if none supplied).
app.post('/api/runs/:threadId/resume', async (req, res) => {
  const { threadId } = req.params;
  const { approval } = req.body || {};
  if (!['approved', 'vetoed'].includes(approval)) {
    return res.status(400).json({ error: "approval must be 'approved' or 'vetoed'" });
  }
  try {
    const out = await graph.resumeCycle({ threadId, approval });
    res.json({ ok: true, status: out.status, runId: out.runId });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/runs/:threadId/state', async (req, res) => {
  try {
    const snap = await graph.listThreadState(req.params.threadId);
    res.json({ snap });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.get('/api/traces', (_req, res) => {
  res.json({ events: traceBus.recentEvents(500) });
});

app.get('/api/graphs', (_req, res) => {
  res.json({
    graphs: graphRegistry.list(),
    langsmith: !!process.env.LANGCHAIN_TRACING_V2,
  });
});

// ─────────────────────────── SSE live stream ─────────────────────────────────
app.get('/api/stream', (req, res) => {
  res.set({
    'Content-Type': 'text/event-stream',
    'Cache-Control': 'no-cache',
    Connection: 'keep-alive',
    'X-Accel-Buffering': 'no',
  });
  res.flushHeaders();
  res.write(`event: hello\ndata: ${JSON.stringify({ ts: Date.now() })}\n\n`);

  const onEvent = (ev) => res.write(`event: trace\ndata: ${JSON.stringify(ev)}\n\n`);
  const onRun   = (r)  => res.write(`event: run\ndata: ${JSON.stringify(r)}\n\n`);
  traceBus.bus.on('event', onEvent);
  traceBus.bus.on('run', onRun);

  const ping = setInterval(() => res.write(`event: ping\ndata: ${Date.now()}\n\n`), 15_000);

  req.on('close', () => {
    clearInterval(ping);
    traceBus.bus.off('event', onEvent);
    traceBus.bus.off('run', onRun);
  });
});

// ─────────────────────────── Data Provider Health ────────────────────────────
app.get('/api/data-health', async (_req, res) => {
  try {
    const { rows } = await query(`
      SELECT provider, endpoint,
             SUM(success_count) AS success_24h,
             SUM(error_count)   AS error_24h,
             MAX(last_error_at) AS last_error_at,
             MAX(last_error)    AS last_error
      FROM data_provider_health
      WHERE window_start >= NOW() - INTERVAL '24 hours'
      GROUP BY provider, endpoint
      ORDER BY provider, endpoint
    `);
    const enriched = rows.map(r => {
      const total = (+r.success_24h) + (+r.error_24h);
      const success_pct = total > 0 ? (+r.success_24h) / total * 100 : null;
      let status = 'green';
      if (success_pct !== null && success_pct < 95) status = 'red';
      else if (success_pct !== null && success_pct < 99) status = 'yellow';
      return { ...r, success_pct, status };
    });
    res.json({ providers: enriched, generated_at: new Date().toISOString() });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ─────────────────────────── Backfill Progress (SP-2 Phase B) ───────────────
// Aggregates backfill_audit rows by (target, status) over the last 7 days.
// Returns:
//   {
//     prices:   {in_progress:N, validated:N, promoted:N, quarantined:N, failed:N},
//     metadata: {...},
//     options:  {...},
//     generated_at: ISO
//   }
app.get('/api/backfill-progress', async (_req, res) => {
  try {
    const { rows } = await query(`
      SELECT target, status, COUNT(*)::int AS n
        FROM backfill_audit
       WHERE started_at >= NOW() - INTERVAL '7 days'
       GROUP BY target, status
    `);
    const TARGETS = ['prices', 'metadata', 'options'];
    const STATUSES = ['in_progress', 'validated', 'promoted', 'quarantined', 'failed'];
    const out = {};
    for (const t of TARGETS) {
      out[t] = {};
      for (const s of STATUSES) out[t][s] = 0;
    }
    for (const r of rows) {
      if (!out[r.target]) out[r.target] = {};
      out[r.target][r.status] = Number(r.n);
    }
    res.json({ ...out, generated_at: new Date().toISOString() });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// ─────────────────────────── Universe Recommendations (SP-2 Phase C) ─────────
// GET  /api/universe-recs        — recent recs (last 14 days)
// POST /api/universe-recs/:id/:action — operator decision (approve / reject / defer)

app.get('/api/universe-recs', async (_req, res) => {
  try {
    const { rows } = await query(`
      SELECT id, strategy_id, current_predicate, candidate_predicate,
             backtest_summary->>'grid_sha256' AS grid_sha256,
             rationale, approved, approved_by, adopted, recommended_at, mastermind_cost_usd
        FROM strategy_universe_recommendations
       WHERE recommended_at > NOW() - INTERVAL '14 days'
       ORDER BY recommended_at DESC
    `);
    res.json({ recs: rows });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

app.post('/api/universe-recs/:id/:action', async (req, res) => {
  const { id, action } = req.params;
  if (!['approve', 'reject', 'defer'].includes(action)) {
    return res.status(400).json({ error: "action must be one of: approve, reject, defer" });
  }
  if (!/^\d+$/.test(String(id))) {
    return res.status(400).json({ error: 'id must be numeric' });
  }
  try {
    if (action === 'approve') {
      const repoRoot = path.resolve(__dirname, '../../..');
      const { execFileSync } = require('node:child_process');
      execFileSync(
        'python3',
        ['-m', 'src.strategies.lifecycle_universe_adoption', 'adopt', '--rec-id', String(id)],
        { cwd: repoRoot, stdio: 'pipe', timeout: 30000 }
      );
    } else if (action === 'reject') {
      // Only act on a not-yet-adopted rec (guard against rejecting live strategy).
      const rejectResult = await query(
        `UPDATE strategy_universe_recommendations
            SET approved=false, adopted=false, approved_at=NOW(), approved_by='operator:dashboard'
          WHERE id=$1 AND adopted=false`,
        [id]
      );
      if (rejectResult.rowCount === 0) {
        return res.status(409).json({ error: 'rec already adopted or not found' });
      }
      // Audit trail — failure does not 500 the main action.
      try {
        const { rows: recRows } = await query(
          `SELECT strategy_id FROM strategy_universe_recommendations WHERE id=$1`, [id]
        );
        if (recRows.length > 0) {
          await query(
            `INSERT INTO lifecycle_audit_log (event, strategy_id, before_state, after_state, actor)
             VALUES ('universe_filter_rejected', $1, NULL, NULL, 'operator:dashboard')`,
            [recRows[0].strategy_id]
          );
        }
      } catch (auditErr) {
        console.error(`[dashboard] universe-recs reject audit insert failed (non-fatal):`, auditErr.message);
      }
    } else {
      // defer — only defer a still-pending rec (approved IS NULL).
      const deferResult = await query(
        `UPDATE strategy_universe_recommendations
            SET approved=NULL, approved_by='operator:dashboard:defer'
          WHERE id=$1 AND approved IS NULL`,
        [id]
      );
      if (deferResult.rowCount === 0) {
        return res.status(409).json({ error: 'rec already adopted or not found' });
      }
      // Audit trail — failure does not 500 the main action.
      try {
        const { rows: recRows } = await query(
          `SELECT strategy_id FROM strategy_universe_recommendations WHERE id=$1`, [id]
        );
        if (recRows.length > 0) {
          await query(
            `INSERT INTO lifecycle_audit_log (event, strategy_id, before_state, after_state, actor)
             VALUES ('universe_filter_deferred', $1, NULL, NULL, 'operator:dashboard')`,
            [recRows[0].strategy_id]
          );
        }
      } catch (auditErr) {
        console.error(`[dashboard] universe-recs defer audit insert failed (non-fatal):`, auditErr.message);
      }
    }
    res.json({ ok: true });
  } catch (err) {
    console.error(`[dashboard] universe-recs ${action} ${id} failed:`, err.message);
    res.status(500).json({ error: err.message });
  }
});

// ──────────────────── Papermint Predicate Coverage (SP-2 Phase D) ────────────
// GET /api/papermint-recent — recent research_candidates (last 30 days) with
//   inferred predicate + adoption lag where a strategy was staged.

app.get('/api/papermint-recent', async (_req, res) => {
  try {
    const { rows } = await query(`
      SELECT rc.candidate_id, rc.source_url, rc.submitted_at,
             rc.hunter_result_json->>'inferred_universe_filter' AS inferred,
             sr.id AS staged_strategy_id,
             sr.created_at - rc.submitted_at AS adoption_lag
        FROM research_candidates rc
        LEFT JOIN strategy_registry sr ON sr.id = (rc.hunter_result_json->>'strategy_id')::integer
       WHERE rc.submitted_at > NOW() - INTERVAL '30 days'
       ORDER BY rc.submitted_at DESC LIMIT 50
    `);
    res.json({ rows });
  } catch (e) { res.status(500).json({ error: String(e) }); }
});

// ─────────────────────────── Universe Resolution ─────────────────────────────
app.get('/api/universe-slice', async (_req, res) => {
  try {
    const { rows } = await query(`
      SELECT resolved_for_date, union_size, per_strategy_sizes, resolver_ms
      FROM universe_resolution_audit
      ORDER BY resolved_at DESC
      LIMIT 30
    `);
    res.json(rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─────────────────────────── Health ──────────────────────────────────────────
app.get('/api/health', async (_req, res) => {
  const health = { ok: true, ts: Date.now(), postgres: 'unknown', redis: 'unknown' };
  try { await query('SELECT 1'); health.postgres = 'ok'; }
  catch (e) { health.postgres = 'error: ' + e.message; health.ok = false; }
  try { await redis.getClient().ping(); health.redis = 'ok'; }
  catch (e) { health.redis = 'error: ' + e.message; health.ok = false; }
  res.json(health);
});

// ─────────────────────────── Static UI ───────────────────────────────────────
app.use('/', express.static(path.join(__dirname, 'public')));

app.listen(PORT, BIND, () => {
  console.log(`[fundjohn-dashboard] listening on ${BIND}:${PORT}`);
});
