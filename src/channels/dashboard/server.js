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
