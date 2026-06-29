/**
 * Pipeline Diagnostics — dashboard backend for LangGraph runs.
 *
 * Surfaces:
 *  - GET /api/pipelines/summary       tile data (active, today, 24h failures, graphs)
 *  - GET /api/pipelines/runs          recent runs across all graphs
 *  - GET /api/pipelines/runs/:thread  checkpoint timeline + final state for one run
 *  - GET /api/pipelines/stream        SSE — live trace events from traceBus
 *
 * Data sources:
 *  - traceBus (in-memory, ≤2000 events / 100 runs) for live + recent
 *  - langgraph.checkpoints (Postgres) for durable history
 */
const express = require('express');
const router = express.Router();

const { query: dbQuery } = require('../../database/postgres');
const traceBus = require('../../agent/traceBus');
const { summarizePipelines } = require('./pipelines_summary');

// ── Helpers ───────────────────────────────────────────────────────────────

function graphFromRun(run) {
  return run?.meta?.graph || run?.meta?.graphName || 'unknown';
}

function checkpointMetaGraph(meta) {
  return meta?.graph || meta?.graph_name || meta?.config?.configurable?.graph || null;
}

async function fetchPersistedRuns(limit = 50) {
  // Group checkpoints by thread_id and pick the latest per thread.
  // langgraph.checkpoint_id is lexicographically sortable (UUIDv7-like).
  const rows = await dbQuery(`
    WITH ranked AS (
      SELECT thread_id, checkpoint_id, metadata,
             ROW_NUMBER() OVER (PARTITION BY thread_id
                                ORDER BY checkpoint_id DESC) AS rn,
             COUNT(*)    OVER (PARTITION BY thread_id) AS checkpoint_count,
             MIN(checkpoint_id) OVER (PARTITION BY thread_id) AS first_cp,
             MAX(checkpoint_id) OVER (PARTITION BY thread_id) AS last_cp
        FROM langgraph.checkpoints
       WHERE checkpoint_ns = ''
    )
    SELECT thread_id, metadata, checkpoint_count, first_cp, last_cp
      FROM ranked
     WHERE rn = 1
     ORDER BY last_cp DESC
     LIMIT $1
  `, [limit]);

  return (rows.rows || rows).map((r) => ({
    threadId: r.thread_id,
    graph: checkpointMetaGraph(r.metadata) || 'unknown',
    checkpointCount: Number(r.checkpoint_count),
    firstCheckpointId: r.first_cp,
    lastCheckpointId: r.last_cp,
    metadata: r.metadata,
    source: 'persisted',
  }));
}

function mergeRuns(liveRuns, persistedRuns) {
  // Live (traceBus) wins over persisted for the same threadId — it has
  // status, durations, and node names. Persisted fills in graphs that
  // ran before johnbot's current process started.
  const merged = new Map();
  for (const p of persistedRuns) merged.set(p.threadId, p);
  for (const l of liveRuns) {
    const existing = merged.get(l.runId) || {};
    merged.set(l.runId, {
      threadId: l.runId,
      graph: graphFromRun(l),
      status: l.status,
      lastNode: l.lastNode,
      startedAt: l.startedAt,
      updatedAt: l.updatedAt,
      finishedAt: l.finishedAt,
      durationMs: (l.finishedAt || l.updatedAt) - l.startedAt,
      eventCount: l.events?.length || 0,
      error: l.error,
      checkpointCount: existing.checkpointCount,
      source: 'live',
    });
  }
  return [...merged.values()].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
}

// ── Routes ────────────────────────────────────────────────────────────────

// GET /api/pipelines/summary
router.get('/summary', async (_req, res) => {
  try {
    const liveRuns = traceBus.listRuns();
    let persisted = [];
    try { persisted = await fetchPersistedRuns(200); } catch (_) { /* schema absent; non-fatal */ }
    const base = summarizePipelines(liveRuns, persisted, Date.now());
    let durableTotal = null;
    try {
      const r = await dbQuery(`SELECT COUNT(DISTINCT thread_id) AS n FROM langgraph.checkpoints WHERE checkpoint_ns = ''`);
      durableTotal = Number((r.rows || r)[0].n);
    } catch (_) { /* non-fatal */ }
    res.json({ ...base, durable_total: durableTotal, generated_at: new Date().toISOString() });
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// GET /api/pipelines/runs?limit=50&graph=daily-cycle
router.get('/runs', async (req, res) => {
  try {
    const limit = Math.min(Number(req.query.limit) || 50, 200);
    const graphFilter = req.query.graph;

    const liveRuns = traceBus.listRuns();
    let persistedRuns = [];
    try {
      persistedRuns = await fetchPersistedRuns(limit);
    } catch (e) {
      // langgraph schema absent on fresh box — fall through to in-memory only
    }

    let merged = mergeRuns(liveRuns, persistedRuns);
    if (graphFilter) merged = merged.filter((r) => r.graph === graphFilter);

    res.json({
      runs: merged.slice(0, limit),
      total: merged.length,
      generated_at: new Date().toISOString(),
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/pipelines/runs/:thread_id
router.get('/runs/:thread_id', async (req, res) => {
  try {
    const threadId = req.params.thread_id;
    const live = traceBus.getRun(threadId);

    let checkpoints = [];
    try {
      // Use a scalar subquery to extract channel keys as a jsonb array so
      // checkpoints with empty channel_values still produce one row.
      const r = await dbQuery(`
        SELECT checkpoint_id, parent_checkpoint_id, type, metadata,
               checkpoint -> 'channel_versions' AS channel_versions,
               (SELECT jsonb_agg(k)
                  FROM jsonb_object_keys(COALESCE(checkpoint -> 'channel_values',
                                                  '{}'::jsonb)) AS k) AS channel_keys
          FROM langgraph.checkpoints
         WHERE thread_id = $1 AND checkpoint_ns = ''
         ORDER BY checkpoint_id ASC
      `, [threadId]);

      checkpoints = (r.rows || r).map((row) => ({
        checkpointId: row.checkpoint_id,
        parent: row.parent_checkpoint_id,
        type: row.type,
        metadata: row.metadata,
        channelVersions: row.channel_versions,
        channels: row.channel_keys || [],
      }));
    } catch (e) {
      // schema absent — fine
    }

    if (!live && checkpoints.length === 0) {
      return res.status(404).json({ error: 'run not found', threadId });
    }

    res.json({
      threadId,
      live: live ? {
        graph: graphFromRun(live),
        status: live.status,
        startedAt: live.startedAt,
        updatedAt: live.updatedAt,
        finishedAt: live.finishedAt,
        durationMs: (live.finishedAt || live.updatedAt) - live.startedAt,
        lastNode: live.lastNode,
        error: live.error,
        events: live.events || [],
      } : null,
      checkpoints,
      generated_at: new Date().toISOString(),
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// GET /api/pipelines/universe-inflation — 30-day union size vs total universe
router.get('/universe-inflation', async (_req, res) => {
  try {
    const result = await dbQuery(`
      SELECT resolved_for_date AS d,
             union_size AS u,
             alpaca_universe_size AS total,
             ROUND(union_size::numeric / NULLIF(alpaca_universe_size,0) * 100, 2) AS pct
      FROM universe_resolution_audit
      ORDER BY resolved_at DESC
      LIMIT 30
    `);
    res.json(result.rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GET /api/pipelines/backfill-history — monthly row counts in
// ticker_metadata_snapshots since 2021-05-01 (SP-2 Phase B 5y backfill).
// Returns: [{ month: 'YYYY-MM-01', row_count: N }, ...] sorted ascending.
router.get('/backfill-history', async (_req, res) => {
  try {
    const result = await dbQuery(`
      SELECT date_trunc('month', snapshot_date)::date AS month,
             count(*)::bigint AS row_count
      FROM ticker_metadata_snapshots
      WHERE snapshot_date >= '2021-05-01'
      GROUP BY 1
      ORDER BY 1
    `);
    const rows = (result.rows || result).map(r => ({
      month: r.month instanceof Date
        ? r.month.toISOString().slice(0, 10)
        : String(r.month).slice(0, 10),
      row_count: Number(r.row_count),
    }));
    res.json(rows);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GET /api/pipelines/stream — SSE live trace events
router.get('/stream', (req, res) => {
  res.setHeader('Content-Type', 'text/event-stream');
  res.setHeader('Cache-Control', 'no-cache');
  res.setHeader('Connection', 'keep-alive');
  res.setHeader('X-Accel-Buffering', 'no');  // disable nginx proxy buffering
  res.flushHeaders?.();
  res.write(`data: ${JSON.stringify({ type: 'connected', ts: Date.now() })}\n\n`);

  const onEvent = (ev) => res.write(`event: trace\ndata: ${JSON.stringify(ev)}\n\n`);
  const onRun   = (run) => res.write(`event: run\ndata: ${JSON.stringify({
    runId: run.runId, graph: graphFromRun(run), status: run.status,
    startedAt: run.startedAt, updatedAt: run.updatedAt, finishedAt: run.finishedAt,
    lastNode: run.lastNode, error: run.error,
  })}\n\n`);

  traceBus.bus.on('event', onEvent);
  traceBus.bus.on('run', onRun);

  // Keep-alive ping every 25s so intermediaries don't drop the connection.
  const ping = setInterval(() => res.write(':ping\n\n'), 25_000);

  req.on('close', () => {
    clearInterval(ping);
    traceBus.bus.off('event', onEvent);
    traceBus.bus.off('run', onRun);
  });
});

module.exports = router;
