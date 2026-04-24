'use strict';

/**
 * /api/research/* routes for the FundJohn dashboard.
 *
 * Two concerns:
 *   1. Proxy chat endpoints (sessions, history, streaming messages) to
 *      the mastermind-chat service on 127.0.0.1:7871. The dashboard
 *      never talks to claude-bin directly.
 *   2. Serve Research-page data straight from Postgres: the active +
 *      queued research feed, the paper library with gate-decision
 *      drill-in, strategy_staging inbox, and a compact mastermind
 *      weekly-run history card.
 */

const express = require('express');
const http = require('http');
const { query } = require('../../database/postgres');

const CHAT_BASE = process.env.MASTERMIND_CHAT_URL || 'http://127.0.0.1:7871';

const router = express.Router();

// ─────────────────────── Chat proxy ────────────────────────────────────────

function parseChatBase() {
  const u = new URL(CHAT_BASE);
  return { host: u.hostname, port: parseInt(u.port || '80', 10) };
}

function proxyJson(req, res, method, path, body) {
  const { host, port } = parseChatBase();
  const data = body ? Buffer.from(JSON.stringify(body)) : null;
  const pr = http.request({
    host, port, method, path,
    headers: {
      'Content-Type': 'application/json',
      ...(data ? { 'Content-Length': data.length } : {}),
    },
  }, (upstream) => {
    res.status(upstream.statusCode || 502);
    res.set('Content-Type', upstream.headers['content-type'] || 'application/json');
    upstream.pipe(res);
  });
  pr.on('error', (e) => res.status(502).json({ error: `chat service unreachable: ${e.message}` }));
  if (data) pr.write(data);
  pr.end();
}

router.get('/sessions', (req, res) => proxyJson(req, res, 'GET', '/chat/sessions'));

router.post('/sessions', (req, res) => proxyJson(req, res, 'POST', '/chat/session', req.body || {}));

router.get('/sessions/:id/history', (req, res) =>
  proxyJson(req, res, 'GET', `/chat/${encodeURIComponent(req.params.id)}/history`)
);

router.post('/sessions/:id/archive', (req, res) =>
  proxyJson(req, res, 'POST', `/chat/${encodeURIComponent(req.params.id)}/archive`, {})
);

// Streamed message: SSE passthrough. Do not buffer upstream events.
router.post('/sessions/:id/message', (req, res) => {
  const { host, port } = parseChatBase();
  const body = Buffer.from(JSON.stringify(req.body || {}));
  const started = Date.now();
  const log = (...args) => console.log(`[research-proxy ${req.params.id.slice(0,8)}]`, ...args);
  log('begin body=', body.length, 'bytes');

  let clientGone = false;
  const pr = http.request({
    host, port, method: 'POST',
    path: `/chat/${encodeURIComponent(req.params.id)}/message`,
    headers: { 'Content-Type': 'application/json', 'Content-Length': body.length },
    agent: false, // fresh socket — avoids pool with stale keep-alive conns
    timeout: 0,
  }, (upstream) => {
    log('upstream response status=', upstream.statusCode, 'after', Date.now() - started, 'ms');
    res.status(upstream.statusCode || 502);
    res.setHeader('Content-Type', upstream.headers['content-type'] || 'text/event-stream');
    res.setHeader('Cache-Control', 'no-cache');
    res.setHeader('X-Accel-Buffering', 'no');
    if (typeof res.flushHeaders === 'function') res.flushHeaders();
    upstream.on('data', (chunk) => { res.write(chunk); });
    upstream.on('end',  () => { log('upstream end after', Date.now() - started, 'ms'); res.end(); });
    upstream.on('error', (e) => { log('upstream err', e.message); try { res.end(); } catch (_) {} });
    res.on('close', () => {
      if (!upstream.complete) { clientGone = true; log('client closed before upstream done — aborting'); try { pr.destroy(); } catch (_) {} }
    });
  });
  pr.setTimeout(0);
  pr.on('error', (e) => {
    log('request err', e.message, 'after', Date.now() - started, 'ms');
    if (clientGone) return;
    try {
      if (!res.headersSent) {
        res.status(502);
        res.setHeader('Content-Type', 'text/event-stream');
      }
      res.write(`event: error\ndata: ${JSON.stringify({ message: `chat service unreachable: ${e.message}` })}\n\n`);
      res.end();
    } catch (_) { /* already sent */ }
  });
  pr.end(body);
});

// ─────────────────────── Research queue + papers ──────────────────────────

router.get('/queue', async (_req, res) => {
  try {
    const [candidates, recentRuns] = await Promise.all([
      query(
        `SELECT rc.candidate_id, rc.source_url, rc.status, rc.kind,
                rc.submitted_at, rc.submitted_by, rc.priority,
                rcp.title, rcp.venue, rcp.published_date
           FROM research_candidates rc
           LEFT JOIN research_corpus rcp USING (source_url)
          WHERE rc.status IN ('pending','in_progress','blocked_buildable','blocked_unclassified')
          ORDER BY rc.priority ASC, rc.submitted_at DESC
          LIMIT 100`
      ),
      query(
        `SELECT id, run_type, status, records_written, duration_ms, created_at
           FROM pipeline_runs
          WHERE run_type LIKE '%research%' OR run_type LIKE '%paper%' OR run_type LIKE '%curator%'
          ORDER BY created_at DESC
          LIMIT 15`
      ),
    ]);
    res.json({
      queued: candidates.rows,
      recent_runs: recentRuns.rows,
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.get('/papers', async (req, res) => {
  const status = (req.query.status || '').toString();
  const q = (req.query.q || '').toString().trim();
  const params = [];
  const where = [];
  if (status) { params.push(status); where.push(`rc.status = $${params.length}`); }
  if (q) { params.push(`%${q}%`); where.push(`(rcp.title ILIKE $${params.length} OR rc.source_url ILIKE $${params.length})`); }
  const whereSql = where.length ? `WHERE ${where.join(' AND ')}` : '';
  try {
    const r = await query(
      `SELECT rc.candidate_id, rc.source_url, rc.status, rc.kind, rc.submitted_at,
              rcp.paper_id, rcp.title, rcp.authors, rcp.venue, rcp.published_date,
              (rc.hunter_result_json->>'confidence')::float AS confidence,
              rc.hunter_result_json->>'decision' AS decision
         FROM research_candidates rc
         LEFT JOIN research_corpus rcp USING (source_url)
         ${whereSql}
         ORDER BY rc.submitted_at DESC
         LIMIT 200`,
      params
    );
    res.json({ papers: r.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.get('/papers/:candidateId', async (req, res) => {
  try {
    const [cand, gates] = await Promise.all([
      query(
        `SELECT rc.*, rcp.paper_id, rcp.title, rcp.abstract, rcp.authors,
                rcp.venue, rcp.published_date
           FROM research_candidates rc
           LEFT JOIN research_corpus rcp USING (source_url)
          WHERE rc.candidate_id = $1`,
        [req.params.candidateId]
      ),
      query(
        `SELECT gate_name, outcome, reason_code, reason_detail, occurred_at
           FROM paper_gate_decisions
          WHERE candidate_id = $1
          ORDER BY occurred_at ASC`,
        [req.params.candidateId]
      ),
    ]);
    if (!cand.rows.length) return res.status(404).json({ error: 'not found' });
    res.json({ paper: cand.rows[0], gate_decisions: gates.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─────────────────────── Strategy staging ─────────────────────────────────

router.get('/staging', async (_req, res) => {
  try {
    const r = await query(
      `SELECT id, proposed_by, source_session_id, source_paper_id, name, thesis,
              parameters, universe, signal_frequency, regime_conditions,
              status, promoted_strategy_id, created_at, decided_at, decided_by,
              decision_note,
              quick_backtest_json, quick_backtest_started_at, quick_backtest_error
         FROM strategy_staging
        ORDER BY (status='pending') DESC, created_at DESC
        LIMIT 100`
    );
    res.json({ items: r.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/staging/:id/decision', async (req, res) => {
  const action = (req.body?.action || '').toLowerCase();
  const note = (req.body?.note || '').toString().slice(0, 2000) || null;
  const by = (req.body?.by || 'operator').toString();
  if (!['approved', 'rejected'].includes(action)) {
    return res.status(400).json({ error: 'action must be approved|rejected' });
  }
  try {
    const r = await query(
      `UPDATE strategy_staging
          SET status = $1, decided_at = NOW(), decided_by = $2, decision_note = $3
        WHERE id = $4 AND status = 'pending'
        RETURNING *`,
      [action, by, note, req.params.id]
    );
    if (!r.rows.length) return res.status(409).json({ error: 'not pending or not found' });
    res.json({ item: r.rows[0] });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─────────────────────── Campaigns (MasterMindJohn) ───────────────────────

router.get('/campaigns', async (req, res) => {
  const sessionId = (req.query.session_id || '').toString() || null;
  const params = [];
  const where = [];
  if (sessionId) { params.push(sessionId); where.push(`c.session_id = $${params.length}`); }
  const whereSql = where.length ? `WHERE ${where.join(' AND ')}` : '';
  try {
    const r = await query(
      `SELECT c.id, c.session_id, c.name, c.request_text, c.status,
              c.plan_json, c.progress_json, c.created_at, c.started_at,
              c.completed_at, c.cancel_requested,
              (SELECT COUNT(*) FROM research_candidates rc WHERE rc.campaign_id = c.id)::int AS candidates_inserted
         FROM research_campaigns c
         ${whereSql}
        ORDER BY c.created_at DESC
        LIMIT 40`,
      params
    );
    res.json({ campaigns: r.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.get('/campaigns/:id', async (req, res) => {
  try {
    const [camp, cands] = await Promise.all([
      query(`SELECT * FROM research_campaigns WHERE id = $1`, [req.params.id]),
      query(
        `SELECT rc.candidate_id, rc.source_url, rc.status, rc.kind,
                rc.submitted_at, rc.hunter_result_json->>'strategy_id' AS strategy_id,
                COALESCE(rc.hunter_result_json->>'strategy_id', s.name) AS slug,
                s.id AS staging_id, s.name AS staging_name, s.status AS staging_status,
                s.quick_backtest_json, s.promoted_strategy_id,
                reg.status AS registry_status,
                reg.backtest_sharpe AS registry_sharpe
           FROM research_candidates rc
           LEFT JOIN strategy_staging s ON s.source_paper_id = rc.candidate_id::text
           LEFT JOIN strategy_registry reg ON reg.id = s.promoted_strategy_id
          WHERE rc.campaign_id = $1
          ORDER BY rc.submitted_at ASC`,
        [req.params.id]
      ),
    ]);
    if (!camp.rows.length) return res.status(404).json({ error: 'not found' });
    res.json({ campaign: camp.rows[0], candidates: cands.rows });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

router.post('/campaigns/:id/cancel', async (req, res) => {
  try {
    const r = await query(
      `UPDATE research_campaigns
          SET cancel_requested = TRUE,
              status = CASE WHEN status IN ('awaiting_ack','planning') THEN 'cancelled' ELSE status END,
              completed_at = CASE WHEN status IN ('awaiting_ack','planning') THEN NOW() ELSE completed_at END
        WHERE id = $1
        RETURNING *`,
      [req.params.id]
    );
    if (!r.rows.length) return res.status(404).json({ error: 'not found' });
    res.json({ campaign: r.rows[0] });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// ─────────────────────── Corpus run history (compact) ──────────────────────

router.get('/runs', async (_req, res) => {
  try {
    // Unified recent-runs feed across the three new weekly Opus jobs plus
    // the existing Saturday corpus rater (curator_runs). Each source is
    // projected to the same shape so the UI doesn't need to know which
    // table a row came from.
    const [memos, recs, expansions, corpus] = await Promise.all([
      query(`SELECT id::text AS id, 'comprehensive-review' AS mode,
                    memo_date AS run_date, 'ok' AS status, cost_usd,
                    jsonb_build_object('strategy_id', strategy_id) AS input_stats,
                    created_at
               FROM strategy_memos
              ORDER BY memo_date DESC, created_at DESC LIMIT 12`),
      query(`SELECT id::text AS id, 'position-recs' AS mode,
                    rec_date AS run_date, action_taken AS status, NULL::numeric AS cost_usd,
                    jsonb_build_object('strategy_id', strategy_id,
                                       'size_delta_pct', size_delta_pct) AS input_stats,
                    created_at
               FROM strategy_sizing_recommendations
              ORDER BY rec_date DESC, created_at DESC LIMIT 12`),
      query(`SELECT id::text AS id, 'paper-expansion' AS mode,
                    run_date, status, cost_usd,
                    jsonb_build_object('papers_imported', papers_imported,
                                       'papers_skipped_dup', papers_skipped_dup) AS input_stats,
                    created_at
               FROM paper_source_expansions
              ORDER BY run_date DESC, created_at DESC LIMIT 6`),
      query(`SELECT run_id::text AS id, 'corpus' AS mode,
                    started_at::date AS run_date, status, total_cost_usd AS cost_usd,
                    jsonb_build_object('input_count', input_count,
                                       'output_count', output_count) AS input_stats,
                    started_at AS created_at
               FROM curator_runs
              ORDER BY started_at DESC LIMIT 6`),
    ]);
    const all = [...memos.rows, ...recs.rows, ...expansions.rows, ...corpus.rows]
      .sort((a, b) => (new Date(b.created_at) - new Date(a.created_at)))
      .slice(0, 20);
    res.json({ runs: all });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

module.exports = router;
