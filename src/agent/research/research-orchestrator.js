'use strict';

/**
 * research-orchestrator.js — Queue-driven research loop for FundJohn.
 *
 * State lives in Postgres research_candidates table (not Redis session).
 * Redis is used only for the PAUSE_KEY signal.
 *
 * Public API (from bot.js):
 *   orch.submit({ url, submittedBy, priority })  — add paper to queue
 *   orch.start({ notify, channelNotify })         — process queue continuously
 *   orch.pause()                                  — set pause signal
 *   orch.getStatus()                              — queue stats string
 *   orch.getStatusText()                          — {status, text} for Discord presence
 *   orch.runReaperPass(notify)                    — weekly orphaned-column detector
 */

const fs   = require('fs');
const path = require('path');
const { spawn, execSync } = require('child_process');
const { emitGateDecision, paperIdForCandidate } = require('./gate-decisions');

const OPENCLAW_DIR      = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const NODE_CLI          = path.join(OPENCLAW_DIR, 'src/agent/run-subagent-cli.js');
const DEFAULT_WORKSPACE = path.join(OPENCLAW_DIR, 'workspaces/default');

const PAUSE_KEY = 'research:pause_requested';
const BATCH_SIZE = 5;  // candidates per processQueue call

const STOP_AFTER_KEY = 'research:stop_after_promoted';   // Redis key for one-shot mode

const IMPLEMENTATIONS_DIR = path.join(OPENCLAW_DIR, 'src/strategies/implementations');
const MANIFEST_PATH       = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');

/**
 * Resolve a strategy's implementation .py path. Honours `metadata.canonical_file`
 * from the manifest when present so the orchestrator picks up hand-coded files
 * (e.g. `str02_hurst_regime_flip.py`) that don't follow the `${stratId}.py`
 * default naming.
 */
function _resolveImplPath(stratId) {
  let canonical = `${stratId}.py`;
  try {
    const m = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
    const cf = m.strategies?.[stratId]?.metadata?.canonical_file;
    if (cf) canonical = cf;
  } catch (_) { /* manifest read error → fall back to default */ }
  return path.join(IMPLEMENTATIONS_DIR, canonical);
}

// Async python runner — returns {stdout, stderr, code}. Unlike execSync, the
// Node event loop keeps serving HTTP traffic while this runs, so the Cancel
// button / other dashboard actions remain responsive during long backtests.
// If opts.onChild is provided, it's invoked synchronously with the spawned
// ChildProcess so the caller can SIGTERM it later (used by Cancel).
function _spawnPython(args, opts = {}) {
  const { cwd, timeoutMs = 600_000, onChild } = opts;
  return new Promise((resolve) => {
    const child = spawn('python3', args, { cwd, env: process.env });
    if (typeof onChild === 'function') { try { onChild(child); } catch (_) {} }
    let stdout = '', stderr = '';
    const killTimer = setTimeout(() => {
      try { child.kill('SIGTERM'); } catch (_) {}
      setTimeout(() => { try { child.kill('SIGKILL'); } catch (_) {} }, 5_000);
    }, timeoutMs);
    child.stdout.on('data', b => { stdout += b.toString(); });
    child.stderr.on('data', b => { stderr += b.toString(); });
    child.on('exit', (code, signal) => {
      clearTimeout(killTimer);
      resolve({ stdout, stderr, code, signal });
    });
    child.on('error', (err) => {
      clearTimeout(killTimer);
      resolve({ stdout, stderr: stderr + '\n' + err.message, code: -1, signal: null });
    });
  });
}

class ResearchOrchestrator {
  constructor() {
    this._redis       = null;
    this._pool        = null;
    this._sessionCost = 0;   // cumulative LLM cost for the current start() session
  }

  // ── Public API ──────────────────────────────────────────────────────────────

  /**
   * Submit a paper URL to the research queue.
   * Returns { candidate_id, message }.
   */
  async submit({ url, submittedBy = 'operator', priority = 5 } = {}) {
    if (!url) throw new Error('url is required');
    const { rows } = await this._query(
      `INSERT INTO research_candidates (source_url, submitted_by, priority)
       VALUES ($1, $2, $3) RETURNING candidate_id`,
      [url, submittedBy, priority]
    );
    const candidateId = rows[0].candidate_id;
    console.log(`[research-orch] Submitted ${url} as ${candidateId}`);
    return { candidate_id: candidateId, message: `Queued: ${candidateId}` };
  }

  /**
   * Start continuous queue processing. Runs until queue empty or paused.
   * Fire-and-forget: returns immediately, loop runs in background.
   * stopAfterPromoted: if > 0, auto-pauses after that many strategies reach PAPER.
   */
  async start({ notify, channelNotify, stopAfterPromoted = 0 } = {}) {
    this._sessionCost = 0;

    const redis = await this._getRedis();
    await redis.del(PAUSE_KEY);

    if (stopAfterPromoted > 0) {
      await redis.set(STOP_AFTER_KEY, String(stopAfterPromoted), 'EX', 86_400);
    } else {
      await redis.del(STOP_AFTER_KEY);
    }

    const pending = await this._getPendingCount();
    if (pending === 0) {
      return 'Queue is empty — submit papers with `/research submit <url>` first.';
    }

    notify?.(`🔬 **Research started** — ${pending} paper(s) in queue.${stopAfterPromoted ? ` Will auto-pause after ${stopAfterPromoted} strategy promoted.` : ''}`);
    channelNotify?.(`🔬 **Research queue processing started** — ${pending} paper(s) queued.`);

    // Fire-and-forget loop
    this._runQueueLoop(notify, channelNotify).catch((e) => {
      console.error('[research-orch] Queue loop error:', e.message);
      notify?.(`❌ Research loop error: ${e.message}`);
      channelNotify?.(`❌ Research loop error: ${e.message}`);
    });

    return `Research loop started — processing ${pending} queued paper(s).`;
  }

  /**
   * Discover papers from arXiv and insert into research_candidates queue.
   * Expands the search window (14 → 30 → 60 → 90 days) until 10 new candidates
   * are inserted or all windows are exhausted.
   * Returns total count of new papers added.
   */
  async discover({ days = 14, notify, channelNotify } = {}) {
    const TARGET      = 10;
    const DAY_WINDOWS = [days, 30, 60, 90].filter((d, i, arr) => arr.indexOf(d) === i);
    let totalInserted = 0;

    notify?.('🔭 Running arXiv discovery (target: 10 new candidates)...');

    for (const window of DAY_WINDOWS) {
      if (totalInserted >= TARGET) break;
      try {
        const raw = execSync(
          `python3 src/ingestion/arxiv_discovery.py --days ${window}`,
          { cwd: OPENCLAW_DIR, timeout: 90_000 }
        ).toString();
        const match = raw.match(/Inserted (\d+) of (\d+)/);
        const [inserted, found] = match ? [parseInt(match[1]), parseInt(match[2])] : [0, 0];
        totalInserted += inserted;
        notify?.(`🔭 Window ${window}d: found ${found} scored paper(s), inserted ${inserted} (total: ${totalInserted})`);
        if (totalInserted >= TARGET) break;
        // Only continue to next window if we haven't hit target yet
      } catch (e) {
        notify?.(`⚠️ arXiv discovery failed for ${window}d window: ${e.message.slice(0, 150)}`);
      }
    }

    const msg = totalInserted >= TARGET
      ? `🔭 Discovery complete — ${totalInserted} new candidates added to queue.`
      : `🔭 Discovery done — ${totalInserted} new candidates added (arXiv had fewer than ${TARGET} matching papers in recent history).`;
    notify?.(msg);
    channelNotify?.(msg);
    return totalInserted;
  }

  /**
   * Discover papers (if queue empty) then start, auto-pausing after 1 promotion.
   * This is the "run until one strategy found" one-shot mode.
   */
  async runOne({ notify, channelNotify } = {}) {
    // Populate queue if empty
    let pending = await this._getPendingCount();
    if (pending === 0) {
      notify?.('📭 Queue is empty — discovering papers from arXiv...');
      const added = await this.discover({ days: 14, notify, channelNotify });
      if (added === 0) {
        return '⚠️ No new arXiv papers found and queue is empty. Submit a paper manually with `/research submit <url>`.';
      }
      pending = await this._getPendingCount();
    }

    return this.start({ notify, channelNotify, stopAfterPromoted: 1 });
  }

  /**
   * Set pause signal. Current batch completes, then loop stops.
   */
  async pause() {
    const redis = await this._getRedis();
    await redis.set(PAUSE_KEY, '1', 'EX', 86_400);
    return '⏸ Pause requested — will stop after current batch completes.';
  }

  /**
   * Queue stats as formatted string.
   */
  async getStatus() {
    const { rows } = await this._query(
      `SELECT status, COUNT(*)::int AS n
       FROM research_candidates
       GROUP BY status
       ORDER BY status`
    );
    const counts = Object.fromEntries(rows.map(r => [r.status, r.n]));
    const total  = rows.reduce((s, r) => s + r.n, 0);
    const pending = await this._getPendingCount();
    const redis  = await this._getRedis();
    const paused = await redis.get(PAUSE_KEY);

    const implRows = await this._query(
      `SELECT status, COUNT(*)::int AS n FROM implementation_queue GROUP BY status`
    );
    const implCounts = Object.fromEntries(implRows.rows.map(r => [r.status, r.n]));

    return [
      `**Research Queue Status**${paused ? ' ⏸ (paused)' : ''}`,
      `Queue: ${pending} pending | ${counts.processing || 0} processing | ${counts.done || 0} done | ${Object.entries(counts).filter(([k]) => k.startsWith('blocked')).reduce((s, [, v]) => s + v, 0)} blocked`,
      `Implementation: ${implCounts.pending || 0} pending coding | ${implCounts.done || 0} coded`,
      `Total candidates: ${total}`,
    ].join('\n');
  }

  /**
   * Returns {status, text} for Discord presence indicator.
   */
  async getStatusText() {
    try {
      const redis  = await this._getRedis();
      const paused = await redis.get(PAUSE_KEY);
      const pending = await this._getPendingCount();
      const { rows: implRows } = await this._query(
        `SELECT status, COUNT(*)::int AS n FROM implementation_queue GROUP BY status`
      );
      const coding  = (implRows.find(r => r.status === 'coding')?.n) || 0;
      const doneCnt = (implRows.find(r => r.status === 'done')?.n)   || 0;

      if (coding > 0) {
        return { status: 'busy', text: `Coding ${coding} strategy/ies...` };
      }
      if (pending > 0 && !paused) {
        return { status: 'busy', text: `Processing ${pending} queued paper(s)` };
      }
      if (pending > 0 && paused) {
        return { status: 'idle', text: `Paused — ${pending} paper(s) queued` };
      }
      if (doneCnt > 0) {
        return { status: 'idle', text: `${doneCnt} strategies coded — queue empty` };
      }
      return { status: 'idle', text: 'Ready — /research submit <url>' };
    } catch {
      return { status: 'idle', text: 'Ready — /research submit <url>' };
    }
  }

  /**
   * List top N pending candidates.
   */
  async listQueue(limit = 10) {
    const { rows } = await this._query(
      `SELECT candidate_id, source_url, submitted_by, submitted_at, priority, status
       FROM research_candidates
       WHERE status = 'pending'
       ORDER BY priority DESC, submitted_at ASC
       LIMIT $1`,
      [limit]
    );
    if (rows.length === 0) return 'Queue is empty.';
    const lines = rows.map((r, i) =>
      `${i + 1}. [P${r.priority}] ${r.source_url.slice(0, 60)}... (${r.candidate_id.slice(0, 8)})`
    );
    return `**Pending Research Queue** (${rows.length}):\n${lines.join('\n')}`;
  }

  // ── Queue loop ──────────────────────────────────────────────────────────────

  async _runQueueLoop(notify, channelNotify) {
    while (true) {
      const pending = await this._getPendingCount();
      if (pending === 0) {
        const costSummary = `Session cost: **$${this._sessionCost.toFixed(4)}**`;
        notify?.(`✅ Queue empty — research complete. ${costSummary}`);
        channelNotify?.(`✅ **Research queue exhausted** — all papers processed. ${costSummary}`);
        break;
      }

      const redis  = await this._getRedis();
      const paused = await redis.get(PAUSE_KEY);
      if (paused === '1') {
        const costSummary = `Session cost so far: **$${this._sessionCost.toFixed(4)}**`;
        notify?.(`⏸ Research paused. ${costSummary}`);
        break;
      }

      await this.processQueue({ notify, channelNotify });
    }
  }

  /**
   * Process one batch of up to BATCH_SIZE pending candidates.
   */
  async processQueue({ notify, channelNotify } = {}) {
    const costAtBatchStart = this._sessionCost;
    // Claim a batch atomically
    const { rows: batch } = await this._query(
      `UPDATE research_candidates
       SET status = 'processing'
       WHERE candidate_id IN (
         SELECT candidate_id FROM research_candidates
         WHERE status = 'pending'
         ORDER BY priority DESC, submitted_at ASC
         LIMIT $1
         FOR UPDATE SKIP LOCKED
       )
       RETURNING candidate_id, source_url, kind, hunter_result_json`,
      [BATCH_SIZE]
    );

    if (batch.length === 0) return;

    const paperRows    = batch.filter(r => r.kind !== 'internal');
    const internalRows = batch.filter(r => r.kind === 'internal');

    if (internalRows.length > 0) {
      notify?.(`🧩 **${internalRows.length} internal draft(s)** — skipping PaperHunter (MasterMindJohn pre-filled spec).`);
    }
    if (paperRows.length > 0) {
      notify?.(`🔍 **Batch started** — extracting ${paperRows.length} paper(s)...`);
    }

    // Phase 1: Run PaperHunter per paper candidate; pass through internal drafts.
    const paperResults = await Promise.all(
      paperRows.map(row => this._runPaperHunter(row).catch(e => {
        console.error(`[research-orch] PaperHunter failed for ${row.candidate_id}:`, e.message);
        return { rejection_reason_if_any: 'fetch_failed', candidate_id: row.candidate_id, source_url: row.source_url };
      }))
    );
    const internalResults = internalRows.map(row => {
      const spec = row.hunter_result_json && typeof row.hunter_result_json === 'object' ? row.hunter_result_json : {};
      return {
        ...spec,
        candidate_id: row.candidate_id,
        source_url:   row.source_url,
        _bypass:      'kind_internal',
      };
    });
    const hunterResults = [...paperResults, ...internalResults];

    // Store hunter results on each candidate row + emit gate decisions.
    for (const result of hunterResults) {
      if (!result?.candidate_id) continue;
      const isBypass = result._bypass === 'kind_internal';
      if (!isBypass) {
        await this._query(
          `UPDATE research_candidates SET hunter_result_json = $1 WHERE candidate_id = $2`,
          [JSON.stringify(result), result.candidate_id]
        );
      }
      const paperId   = await paperIdForCandidate(result.candidate_id);
      const rejection = result.rejection_reason_if_any;
      await emitGateDecision({
        paperId,
        candidateId:  result.candidate_id,
        strategyId:   result.strategy_id || null,
        gateName:     'paperhunter',
        outcome:      rejection ? 'reject' : 'pass',
        reasonCode:   rejection || (isBypass ? 'kind_internal_bypass' : null),
        reasonDetail: rejection ? (result.rejection_detail || null) : null,
        metadata:     { has_spec: Boolean(result.strategy_id), bypass: isBypass || undefined },
      });
    }

    // Phase 2: Build ResearchJohn context
    const manifestIds    = this._loadManifestIds();
    const signatures     = this._loadStrategySignatures();
    const ledgerSnapshot = await this._loadLedgerSnapshot();

    const rjCtx = {
      role:                 'classify_papers',
      hunters:              hunterResults,
      manifest_strategies:  manifestIds,
      strategy_signatures:  signatures,
      data_ledger_snapshot: ledgerSnapshot,
    };

    notify?.(`🧠 **ResearchJohn classifying** ${hunterResults.length} result(s)...`);

    let classification = { ready: [], buildable: [], blocked: [] };
    try {
      const raw = await this._runSubagent('researchjohn', 'classify', rjCtx);
      classification = this._parseJSON(raw) || classification;
    } catch (e) {
      console.error('[research-orch] ResearchJohn failed:', e.message);
    }

    // Phase 3: Process 3 queues

    // READY → implementation_queue + code immediately
    for (const item of (classification.ready || [])) {
      await this._query(
        `INSERT INTO implementation_queue (candidate_id, strategy_spec, status)
         VALUES ($1, $2, 'pending')`,
        [item.candidate_id, JSON.stringify(item.strategy_spec)]
      );
      await this._query(
        `UPDATE research_candidates SET status = 'done' WHERE candidate_id = $1`,
        [item.candidate_id]
      );
      const paperId = await paperIdForCandidate(item.candidate_id);
      await emitGateDecision({
        paperId,
        candidateId: item.candidate_id,
        strategyId:  item.strategy_spec?.strategy_id || null,
        gateName:    'researchjohn',
        outcome:     'pass',
        reasonCode:  'ready',
      });
      await this._codeFromQueue(item, notify, channelNotify);
    }

    // BUILDABLE → data_ingestion_queue (one row per missing column)
    for (const item of (classification.buildable || [])) {
      for (const col of (item.missing_columns || [])) {
        await this._query(
          `INSERT INTO data_ingestion_queue (requested_by_candidate_id, column_name)
           VALUES ($1, $2)
           ON CONFLICT DO NOTHING`,
          [item.candidate_id, col]
        );
      }
      await this._query(
        `UPDATE research_candidates SET status = 'blocked_buildable' WHERE candidate_id = $1`,
        [item.candidate_id]
      );
      const paperId = await paperIdForCandidate(item.candidate_id);
      await emitGateDecision({
        paperId,
        candidateId: item.candidate_id,
        strategyId:  item.strategy_spec?.strategy_id || null,
        gateName:    'researchjohn',
        outcome:     'buildable',
        reasonCode:  'missing_columns',
        metadata:    { missing_columns: item.missing_columns || [] },
      });
      const colList = (item.missing_columns || []).join(', ');
      channelNotify?.(`🔧 **BUILDABLE** strategy \`${item.strategy_spec?.strategy_id}\` — needs columns: \`${colList}\`. Awaiting BotJohn approval.`);
    }

    // BLOCKED → update status
    for (const item of (classification.blocked || [])) {
      await this._query(
        `UPDATE research_candidates SET status = 'blocked_rejected' WHERE candidate_id = $1`,
        [item.candidate_id]
      );
      const paperId = await paperIdForCandidate(item.candidate_id);
      await emitGateDecision({
        paperId,
        candidateId:  item.candidate_id,
        strategyId:   item.strategy_spec?.strategy_id || null,
        gateName:     'researchjohn',
        outcome:      'reject',
        reasonCode:   item.block_reason || 'blocked',
        reasonDetail: item.reasoning || null,
      });
    }

    // Mark any remaining 'processing' rows as done (hunters that weren't classified)
    const classifiedIds = new Set([
      ...(classification.ready    || []).map(i => i.candidate_id),
      ...(classification.buildable || []).map(i => i.candidate_id),
      ...(classification.blocked  || []).map(i => i.candidate_id),
    ]);
    for (const row of batch) {
      if (!classifiedIds.has(row.candidate_id)) {
        await this._query(
          `UPDATE research_candidates SET status = 'blocked_unclassified' WHERE candidate_id = $1`,
          [row.candidate_id]
        );
      }
    }

    const batchCost = this._sessionCost - costAtBatchStart;
    const summary = `Batch complete — READY: ${(classification.ready||[]).length}, BUILDABLE: ${(classification.buildable||[]).length}, BLOCKED: ${(classification.blocked||[]).length} | Batch cost: $${batchCost.toFixed(4)}`;
    notify?.(summary);
    channelNotify?.(`📊 **Research batch complete** — ${summary}`);
  }

  // ── Coding sub-phase ────────────────────────────────────────────────────────

  async _codeFromQueue(item, notify, channelNotify, opts = {}) {
    const { candidate_id, strategy_spec } = item;
    const stratId  = strategy_spec?.strategy_id || 'unknown';
    // Resolve the implementation path via manifest.canonical_file when set,
    // mirroring src/agent/approvals/staging_approver.js::readRequirements.
    // Falls back to "${stratId}.py" only when no canonical_file is recorded.
    const implPath = _resolveImplPath(stratId);
    const onPhase = typeof opts.onPhase === 'function' ? opts.onPhase : () => {};

    await this._query(
      `UPDATE implementation_queue SET status = 'coding' WHERE candidate_id = $1`,
      [candidate_id]
    );

    // Skip strategycoder when a hand-coded implementation already exists at
    // the canonical path. The fused-approval rewrite was designed for
    // candidates where strategycoder writes the .py from scratch; running
    // it against an existing file risks overwriting working code with a
    // weaker LLM-rewritten version.
    const skipCoding = fs.existsSync(implPath);
    notify?.(`  ⚙️ Coding: ${stratId}${skipCoding ? ' (existing file — skipping strategycoder)' : '...'}`);
    onPhase('strategycoder', 20);
    const costBeforeCoding = this._sessionCost;
    if (skipCoding) {
      await this._query(
        `UPDATE implementation_queue SET status = 'done', coded_at = NOW() WHERE candidate_id = $1`,
        [candidate_id]
      );
    } else {
      try {
        await this._codeStrategy(strategy_spec);
        await this._query(
          `UPDATE implementation_queue SET status = 'done', coded_at = NOW() WHERE candidate_id = $1`,
          [candidate_id]
        );
        notify?.(`  ✅ ${stratId} implemented — running validation...`);
      } catch (e) {
        await this._query(
          `UPDATE implementation_queue SET status = 'failed' WHERE candidate_id = $1`,
          [candidate_id]
        );
        notify?.(`  ⚠️ ${stratId} coding failed: ${e.message}`);
        return { promoted: false, reasonCode: 'coding_failed', error: e.message };
      }
    }

    // ── Phase 1: Contract validation ─────────────────────────────────────────
    // validate_strategy.py exits 0 on valid, 1 on invalid. Use async spawn so
    // we don't block the Node event loop (previously execSync held the loop
    // hostage for up to 10 min during backtests, making the whole API
    // unresponsive).
    onPhase('validate', 40);
    let validResult;
    {
      const { stdout, code } = await _spawnPython(
        ['src/strategies/validate_strategy.py', implPath],
        { cwd: OPENCLAW_DIR, timeoutMs: 60_000, onChild: opts.onChild });
      try {
        validResult = JSON.parse(stdout);
      } catch (_) {
        validResult = { ok: false, errors: [`validate_strategy.py exit=${code}; stdout: ${stdout.slice(0, 300)}`] };
      }
    }

    const vPaperId = await paperIdForCandidate(candidate_id);
    if (!validResult.ok) {
      const errLog = (validResult.errors || []).join('\n');
      await this._query(
        `UPDATE implementation_queue SET status = 'validation_failed', error_log = $1 WHERE candidate_id = $2`,
        [errLog, candidate_id]
      );
      await emitGateDecision({
        paperId:      vPaperId,
        candidateId:  candidate_id,
        strategyId:   stratId,
        gateName:     'validate',
        outcome:      'reject',
        reasonCode:   'contract_violation',
        reasonDetail: errLog,
        metadata:     { errors: validResult.errors || [] },
      });
      notify?.(`  ❌ ${stratId} validation failed: ${errLog.slice(0, 200)}`);
      channelNotify?.(`❌ **${stratId}** failed contract validation — see implementation_queue for errors.`);
      return { promoted: false, reasonCode: 'contract_violation', error: errLog };
    }
    await emitGateDecision({
      paperId:     vPaperId,
      candidateId: candidate_id,
      strategyId:  stratId,
      gateName:    'validate',
      outcome:     'pass',
      metadata:    { signal_count: validResult.signal_count ?? null },
    });
    notify?.(`  ✅ ${stratId} validation passed — running backtest (may take 2–5 min)...`);
    onPhase('backtest', 60);

    // ── Phase 2: Auto-backtest convergence gate ───────────────────────────────
    // auto_backtest.py exits 0 on pass, 1 on fail. JSON is on stdout in both
    // cases. Heartbeat emits progress every 5s during the long run so the
    // dashboard chip visibly ticks instead of appearing stuck.
    let btResult;
    {
      let hb = 60;
      const heartbeat = setInterval(() => {
        hb = Math.min(hb + 2, 85);
        try { onPhase('backtest', hb); } catch (_) {}
      }, 5_000);
      try {
        const { stdout, code } = await _spawnPython(
          ['src/strategies/auto_backtest.py', implPath],
          { cwd: OPENCLAW_DIR, timeoutMs: 600_000, onChild: opts.onChild });
        try {
          btResult = JSON.parse(stdout);
        } catch (_) {
          btResult = { error: `auto_backtest.py exit=${code}; stdout: ${stdout.slice(0, 300)}`, windows: [] };
        }
      } finally {
        clearInterval(heartbeat);
      }
    }

    // The only candidate→paper block is "code couldn't execute at all" —
    // signalled by auto_backtest.py setting `error` on contract violation,
    // import error, no strategy class, or missing prices. Metric-based
    // gating is gone: weak Sharpe / high DD / zero trades all still promote,
    // and the human judges from the persisted metrics in the dashboard.
    if (btResult.error) {
      await this._query(
        `UPDATE implementation_queue SET status = 'backtest_failed', backtest_result = $1 WHERE candidate_id = $2`,
        [JSON.stringify(btResult), candidate_id]
      );
      const summary = String(btResult.error).slice(0, 300);
      await emitGateDecision({
        paperId:      vPaperId,
        candidateId:  candidate_id,
        strategyId:   stratId,
        gateName:     'convergence',
        outcome:      'reject',
        reasonCode:   'backtest_error',
        reasonDetail: summary,
        metadata:     { error: summary },
      });
      notify?.(`  ❌ ${stratId} couldn't execute: ${summary}`);
      channelNotify?.(`❌ **${stratId}** couldn't execute — ${summary}.`);
      return { promoted: false, reasonCode: 'backtest_error', error: summary, backtest_result: btResult };
    }
    await emitGateDecision({
      paperId:     vPaperId,
      candidateId: candidate_id,
      strategyId:  stratId,
      gateName:    'convergence',
      outcome:     'pass',
      metadata: {
        sharpe:      btResult.sharpe      ?? null,
        max_dd:      btResult.max_dd      ?? null,
        trade_count: btResult.trade_count ?? null,
      },
    });

    // ── Phase 3: Auto-promote (state-aware) ─────────────────────────────────
    // Under the fused-approval lifecycle (2026-04-27), the canonical promotion
    // is STAGING → CANDIDATE: the fused worker invokes _codeFromQueue inline
    // once data is backfilled, and the backtest result lands the strategy in
    // CANDIDATE for the operator's live-click decision.
    //
    // BUT: _codeFromQueue is also called from saturday_brain Phase 6
    // (un-staged Tier-A candidates), saturday_brain_recovery, the daily
    // research-cycle's _runQueueLoop, and a stale-row sweep in cron-schedule.
    // For those paths the strategy may not be in the manifest at all, or may
    // already be in CANDIDATE. Reading the current state and only transitioning
    // when from→CANDIDATE is valid keeps every caller working.
    onPhase('promoting', 90);
    const reason = `Auto-backtest: Sharpe ${btResult.sharpe?.toFixed(2)}, DD ${(btResult.max_dd * 100)?.toFixed(1)}%, trades ${btResult.trade_count}`;
    const lifecyclePy = [
      `import sys; sys.path.insert(0, 'src')`,
      `from strategies.lifecycle import LifecycleStateMachine, StrategyState, VALID_TRANSITIONS`,
      `lsm = LifecycleStateMachine.from_manifest('src/strategies/manifest.json')`,
      `sid = ${JSON.stringify(stratId)}`,
      `if not lsm.is_registered(sid):`,
      `    # Strategy isn't in the manifest yet (saturday_brain Tier-A path). Register`,
      `    # in CANDIDATE so the dashboard surfaces it with the backtest metrics that`,
      `    # are about to land in strategy_registry.`,
      `    lsm.register(sid, initial_state=StrategyState.CANDIDATE, metadata={'canonical_file': sid + '.py'})`,
      `else:`,
      `    cur = lsm.get_state(sid)`,
      `    if (cur, StrategyState.CANDIDATE) in VALID_TRANSITIONS:`,
      `        lsm.transition(sid, StrategyState.CANDIDATE, actor='auto_backtest', reason=${JSON.stringify(reason)})`,
      `    elif cur == StrategyState.CANDIDATE:`,
      `        # Already in candidate — no transition needed. Touch state_since so the dashboard`,
      `        # surfaces freshness.`,
      `        from datetime import datetime, timezone`,
      `        lsm.get_record(sid).state_since = datetime.now(timezone.utc).isoformat()`,
      `    else:`,
      `        # No legal path to CANDIDATE from current state — leave state alone, just record metrics.`,
      `        print(f'lifecycle skip: cannot transition {sid} from {cur.value} to candidate', flush=True)`,
      `lsm.save_manifest('src/strategies/manifest.json')`,
    ].join('\n');
    const lc = await _spawnPython(['-c', lifecyclePy], { cwd: OPENCLAW_DIR, timeoutMs: 30_000 });
    if (lc.code !== 0) {
      notify?.(`  ⚠️ ${stratId} lifecycle promotion failed (exit=${lc.code}): ${(lc.stderr || lc.stdout).slice(0, 200)}`);
    }

    // Update strategy_registry: status + measured backtest metrics.
    // btResult.max_dd is a fraction (e.g. 0.058 = 5.8%); registry stores it as a percent number.
    // btResult.total_return_pct is already a percent (added by auto_backtest.py), nullable for older builds.
    //
    // 2026-04-27: We persist trade_count unconditionally so the dashboard can
    // distinguish "0 trades — strategy emitted no signals" from "never
    // backtested" (which leaves trade_count NULL). For sharpe/dd/return we
    // still cap garbage values: |sharpe| > 100 is auto_backtest.py's
    // near-zero-std artifact, NaN/inf can leak through too. Anything in the
    // valid envelope — including the legitimate sharpe of a 0-trade run
    // (which is 0 or near-0) — is now persisted instead of dropped.
    const btTrades = Number.isFinite(btResult.trade_count) ? btResult.trade_count : 0;
    const inEnvelope = (x) => x != null && isFinite(x) && Math.abs(x) <= 100;
    const sharpeOK = inEnvelope(btResult.sharpe);
    const ddOK     = inEnvelope(btResult.max_dd);
    const retOK    = inEnvelope(btResult.total_return_pct);
    const btSharpe = sharpeOK ? btResult.sharpe : null;
    const btDdPct  = ddOK     ? Math.round(btResult.max_dd * 100 * 100) / 100 : null;
    const btRetPct = retOK    ? btResult.total_return_pct : null;
    // Per-regime breakdown from auto_backtest. Nullable — strategies whose
    // backtest errored out keep the prior value.
    const btBreakdown = (btResult.regime_breakdown && typeof btResult.regime_breakdown === 'object')
      ? JSON.stringify(btResult.regime_breakdown)
      : null;
    await this._query(
      `UPDATE strategy_registry
          SET status                    = 'pending_approval',
              backtest_sharpe           = COALESCE($2, backtest_sharpe),
              backtest_max_dd_pct       = COALESCE($3, backtest_max_dd_pct),
              backtest_return_pct       = COALESCE($4, backtest_return_pct),
              backtest_trade_count      = $5,
              backtest_regime_breakdown = COALESCE($6::jsonb, backtest_regime_breakdown)
        WHERE id = $1`,
      [stratId, btSharpe, btDdPct, btRetPct, btTrades, btBreakdown]
    ).catch((e) => console.error(`[research-orch] registry update failed: ${e.message}`));

    await this._query(
      `UPDATE implementation_queue SET status = 'promoted', backtest_result = $1 WHERE candidate_id = $2`,
      [JSON.stringify(btResult), candidate_id]
    );
    await emitGateDecision({
      paperId:     vPaperId,
      candidateId: candidate_id,
      strategyId:  stratId,
      gateName:    'promotion',
      outcome:     'pass',
      reasonCode:  'auto_backtest_promoted',
      metadata: {
        sharpe: btResult.sharpe ?? null,
        max_dd: btResult.max_dd ?? null,
        trade_count: btResult.trade_count ?? null,
      },
    });

    const coderCost = this._sessionCost - costBeforeCoding;
    const summary = `Sharpe ${btResult.sharpe?.toFixed(2)}, DD ${(btResult.max_dd * 100)?.toFixed(1)}%, ${btResult.trade_count} trades`;
    const costLine = `Creation cost: $${coderCost.toFixed(4)} | Session total: $${this._sessionCost.toFixed(4)}`;
    notify?.(`  🚀 ${stratId} → CANDIDATE (awaiting live click) (${summary}) | ${costLine}`);
    channelNotify?.(`🚀 **${stratId}** auto-promoted to candidate (awaiting candidate→live approval) — ${summary}\n💰 ${costLine}`);

    // One-shot mode: auto-pause after N promotions
    try {
      const r = await this._getRedis();
      const remaining = parseInt(await r.get(STOP_AFTER_KEY) || '0');
      if (remaining > 0) {
        const newVal = remaining - 1;
        if (newVal <= 0) {
          await r.del(STOP_AFTER_KEY);
          await r.set(PAUSE_KEY, '1', 'EX', 86_400);
          notify?.('⏸ One-shot complete — research auto-paused. Run `/research start` to continue.');
          channelNotify?.('⏸ **One-shot complete** — 1 strategy promoted. Research auto-paused.');
        } else {
          await r.set(STOP_AFTER_KEY, String(newVal), 'EX', 86_400);
        }
      }
    } catch (_) { /* Redis unavailable — loop will continue normally */ }

    return { promoted: true, backtest_result: btResult, reasonCode: 'auto_backtest_promoted' };
  }

  // ── DataWiringAgent ─────────────────────────────────────────────────────────

  /**
   * Wire a new data column after BotJohn approval.
   * queueRow: row from data_ingestion_queue
   */
  async _wireColumn(queueRow) {
    const ctx = {
      role:           'add_column',
      REQUEST_ID:     queueRow.request_id,
      COLUMN_NAME:    queueRow.column_name,
      transform_spec: queueRow.transform_spec,
      provider:       queueRow.provider_preferred,
      refresh:        queueRow.refresh_cadence,
    };

    let result;
    try {
      result = await this._runSubagent('datawiring', queueRow.column_name, ctx);
      await this._query(
        `UPDATE data_ingestion_queue
         SET status = 'APPROVED_WIRED', wired_at = NOW()
         WHERE request_id = $1`,
        [queueRow.request_id]
      );
    } catch (e) {
      await this._query(
        `UPDATE data_ingestion_queue
         SET status = 'FAILED', failure_reason = $1
         WHERE request_id = $2`,
        [e.message, queueRow.request_id]
      );
      throw e;
    }
    return result;
  }

  // _unwireColumn + runReaperPass removed 2026-04-28 per CLAUDE.md
  // NEVER-DELETE-DATA core invariant. The data ledger is append-only —
  // orphaned columns from deprecated strategies stay collected so future
  // strategies can opt into them without re-backfilling history.

  // ── Internal helpers ────────────────────────────────────────────────────────

  async _runPaperHunter(candidateRow) {
    const ledger = await this._loadLedgerSnapshot();
    // Hydrate the paper's abstract + biblio from research_corpus so the
    // hunter has primary content even when WebFetch fails on a paywalled
    // DOI. With Sonnet 4.6 + a 1500-char abstract, ~80% of papers can be
    // blueprinted directly from the abstract; fetch becomes an optional
    // enhancement rather than a hard prerequisite.
    let paperBlock = { title: '', abstract: '', authors: [], venue: '', published_date: '' };
    try {
      const r = await this._query(
        `SELECT title, abstract, authors, venue, published_date::text AS published_date
           FROM research_corpus WHERE source_url = $1 LIMIT 1`,
        [candidateRow.source_url]
      );
      if (r.rows[0]) paperBlock = r.rows[0];
    } catch (_) { /* best-effort */ }

    const ctx = {
      role:            'extract',
      SOURCE_URL:      candidateRow.source_url,
      CANDIDATE_ID:    candidateRow.candidate_id,
      AVAILABLE_DATA:  JSON.stringify(ledger),
      PAPER_TITLE:     paperBlock.title || '',
      PAPER_ABSTRACT:  (paperBlock.abstract || '').slice(0, 8000),  // generous cap
      PAPER_AUTHORS:   Array.isArray(paperBlock.authors) ? paperBlock.authors.join(', ') : (paperBlock.authors || ''),
      PAPER_VENUE:     paperBlock.venue || '',
      PAPER_DATE:      paperBlock.published_date || '',
    };
    const raw = await this._runSubagent('paperhunter', candidateRow.candidate_id.slice(0, 8), ctx);
    return this._parseJSON(raw) || { rejection_reason_if_any: 'parse_failed', candidate_id: candidateRow.candidate_id };
  }

  /**
   * Saturday-brain fan-out: take an explicit list of candidate IDs (already
   * promoted to research_candidates by mastermind corpus rating), spawn
   * paperhunter in parallel for each, persist hunter_result_json + emit
   * paper_gate_decisions, and return the array of results in the same order.
   *
   * Differs from processQueue() in three ways:
   *   - Candidate IDs are passed in (no pending-status claim).
   *   - We do NOT invoke researchjohn classification or _codeFromQueue here;
   *     the saturday brain's Phase 5 (data_tier_filter) decides what runs
   *     synchronously in Phase 6 vs. drops to STAGING in Phase 7.
   *   - Concurrency is capped explicitly (paperhunter is parallel-safe but
   *     we don't want a 200-wide spawn storm overwhelming claude-bin).
   *
   * @param {string[]} candidateIds  array of research_candidates.candidate_id
   * @param {object}   opts
   *    - concurrency: max parallel paperhunters (default 8)
   *    - notify:      progress callback `(msg) => void`
   *    - onResult:    per-candidate callback `(idx, result) => void`
   * @returns {Promise<object[]>}    hunter results, indexed parallel to input
   */
  async runHunterFanout(candidateIds, opts = {}) {
    if (!Array.isArray(candidateIds) || candidateIds.length === 0) return [];
    const concurrency = Math.max(1, Math.min(opts.concurrency || 8, 32));
    const notify = opts.notify || (() => {});
    const onResult = opts.onResult || (() => {});

    // Hydrate candidate rows by ID. Order-preserving via a map.
    const { rows: rows } = await this._query(
      `SELECT candidate_id, source_url, kind, hunter_result_json
         FROM research_candidates
        WHERE candidate_id::text = ANY($1::text[])`,
      [candidateIds]
    );
    const byId = new Map(rows.map(r => [r.candidate_id, r]));
    const ordered = candidateIds
      .map(id => byId.get(id))
      .filter(Boolean);

    notify(`runHunterFanout: ${ordered.length}/${candidateIds.length} candidates resolved; concurrency=${concurrency}`);

    const results = new Array(ordered.length);
    let cursor = 0;
    let done = 0;

    const worker = async () => {
      while (true) {
        const i = cursor++;
        if (i >= ordered.length) return;
        const row = ordered[i];
        try {
          let result;
          if (row.kind === 'internal' && row.hunter_result_json && typeof row.hunter_result_json === 'object') {
            // MasterMindJohn pre-filled spec (e.g. ideator drafts) — skip
            // paperhunter spawn, just pass the spec through with bypass tag.
            result = {
              ...row.hunter_result_json,
              candidate_id: row.candidate_id,
              source_url:   row.source_url,
              _bypass:      'kind_internal',
            };
          } else {
            result = await this._runPaperHunter(row);
          }
          results[i] = result;

          // Persist + emit gate decision (mirrors processQueue:341–363).
          const isBypass = result?._bypass === 'kind_internal';
          if (!isBypass) {
            await this._query(
              `UPDATE research_candidates SET hunter_result_json = $1 WHERE candidate_id = $2`,
              [JSON.stringify(result), row.candidate_id]
            ).catch(() => {});
          }
          try {
            const paperId   = await paperIdForCandidate(row.candidate_id);
            const rejection = result?.rejection_reason_if_any;
            await emitGateDecision({
              paperId,
              candidateId:  row.candidate_id,
              strategyId:   result?.strategy_id || null,
              gateName:     'paperhunter',
              outcome:      rejection ? 'reject' : 'pass',
              reasonCode:   rejection || (isBypass ? 'kind_internal_bypass' : null),
              reasonDetail: rejection ? (result.rejection_detail || null) : null,
              metadata:     { has_spec: Boolean(result?.strategy_id), bypass: isBypass || undefined, source: 'saturday_brain' },
            });
          } catch (_) { /* gate emit best-effort */ }
        } catch (e) {
          results[i] = {
            rejection_reason_if_any: 'fetch_failed',
            candidate_id: row.candidate_id,
            source_url:   row.source_url,
            error:        e.message,
          };
        }
        done += 1;
        if (done % 10 === 0 || done === ordered.length) {
          notify(`runHunterFanout: ${done}/${ordered.length} done`);
        }
        try { onResult(i, results[i]); } catch (_) {}
      }
    };
    await Promise.all(Array.from({ length: concurrency }, () => worker()));
    return results;
  }

  async _codeStrategy(strategySpec) {
    const ctx = {
      role:          'implement_strategy',
      STRATEGY_SPEC: JSON.stringify(strategySpec),
      instructions:  'Implement this strategy. Apply fundjohn:strategy-coder and fundjohn:backtest-plumb skills.',
    };
    const result = await this._runSubagent('strategycoder', strategySpec.strategy_id || 'strategy', ctx);
    await this._registerStrategy(strategySpec).catch((e) =>
      console.error('[research-orch] strategy_registry insert failed:', e.message)
    );
    return result;
  }

  async _registerStrategy(spec) {
    const stratId = spec.strategy_id;
    if (!stratId) return;

    const pg = process.env.POSTGRES_URI;
    if (!pg) return;

    const { Pool } = require('pg');
    const pool = new Pool({ connectionString: pg });
    try {
      const params = {
        stop_pct:            spec.stop_pct,
        target_pct:          spec.target_pct,
        holding_period:      spec.holding_period,
        required_columns:    spec.data_requirements || [],
      };
      const universe = spec.universe
        ? [String(spec.universe).replace(/\s+/g, '')]
        : ['SP500'];
      const regimeConditions = Array.isArray(spec.regime_conditions)
        ? spec.regime_conditions.reduce((acc, r) => { acc[r] = true; return acc; }, {})
        : (spec.regime_conditions || {});

      await pool.query(
        `INSERT INTO strategy_registry
           (id, name, description, tier, implementation_path, parameters, regime_conditions, universe, status, backtest_sharpe)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, 'pending_approval', $9)
         ON CONFLICT (id) DO NOTHING`,
        [
          stratId,
          stratId,
          spec.signal_logic || spec.hypothesis_one_liner || stratId,
          2,
          `src/strategies/implementations/${stratId}.py`,
          JSON.stringify(params),
          JSON.stringify(regimeConditions),
          universe,
          spec.reported_sharpe ?? spec.reported_metrics?.sharpe ?? null,
        ]
      );
      console.log(`[research-orch] strategy_registry: registered ${stratId} as pending_approval`);
    } finally {
      await pool.end();
    }
  }

  _loadManifestIds() {
    try {
      const p = path.join(OPENCLAW_DIR, 'src/strategies/manifest.json');
      if (!fs.existsSync(p)) return [];
      const manifest = JSON.parse(fs.readFileSync(p, 'utf8'));
      const strats   = manifest.strategies || {};
      return Object.keys(strats);
    } catch { return []; }
  }

  _loadStrategySignatures() {
    try {
      const p = path.join(OPENCLAW_DIR, 'src/strategies/strategy_signatures.json');
      if (!fs.existsSync(p)) return {};
      return JSON.parse(fs.readFileSync(p, 'utf8'));
    } catch { return {}; }
  }

  async _loadLedgerSnapshot() {
    try {
      // Query data_columns directly to get coverage fields (min_date, max_date, row_count)
      // which the materialized view data_ledger does not expose.
      const { rows } = await this._query(
        `SELECT column_name, provider, min_date, max_date, row_count, ticker_count
         FROM data_columns LIMIT 500`
      );
      return rows;
    } catch { return []; }
  }

  _parseJSON(raw) {
    if (!raw) return null;
    if (typeof raw === 'object') return raw;
    const text = String(raw);

    // 1. Try a fenced ```json block first — Sonnet 4.6 reliably ignores
    //    "no markdown" instructions and wraps in ```json...``` even when
    //    told otherwise. (2026-05-02 saturday-brain hit this — 2/2
    //    paperhunter outputs failed parse via the old greedy regex.)
    const fence = text.match(/```(?:json)?\s*\n?([\s\S]*?)```/i);
    if (fence) {
      try { return JSON.parse(fence[1].trim()); } catch (_) { /* fallthrough */ }
    }

    // 2. Try balanced-brace extraction starting at the first `{` or `[`.
    //    The pre-2026-05-02 regex /[\[{][\s\S]*[\]}]/ is greedy across
    //    nested/multi-block content; e.g. an opening `[` inside the
    //    preamble plus a closing `}` after the JSON yields invalid spans.
    const start = text.search(/[\[{]/);
    if (start >= 0) {
      const open  = text[start];
      const close = open === '{' ? '}' : ']';
      let depth = 0, inStr = false, esc = false;
      for (let i = start; i < text.length; i++) {
        const c = text[i];
        if (inStr) {
          if (esc) { esc = false; continue; }
          if (c === '\\') { esc = true; continue; }
          if (c === '"') { inStr = false; }
          continue;
        }
        if (c === '"') { inStr = true; continue; }
        if (c === open)  depth++;
        else if (c === close) {
          depth--;
          if (depth === 0) {
            const slice = text.slice(start, i + 1);
            try { return JSON.parse(slice); } catch (_) { break; }
          }
        }
      }
    }

    // 3. Direct parse of the trimmed text (LLM occasionally complies).
    try { return JSON.parse(text.trim()); } catch (_) { /* nope */ }

    // 4. Last resort — log a truncated snippet so the operator can
    //    diagnose why parse failed (pre-fix this was silent → "parse_failed"
    //    sentinel hid root cause for weeks).
    console.error(`[research-orch] _parseJSON failed; raw head: ${text.slice(0, 400).replace(/\n/g, '\\n')}`);
    return null;
  }

  // ── Core subagent runner ────────────────────────────────────────────────────

  _runSubagent(type, ticker, contextObj) {
    return new Promise((resolve, reject) => {
      const tmpFile = `/tmp/research-ctx-${Date.now()}-${Math.random().toString(36).slice(2)}.json`;
      const ctxStr  = typeof contextObj === 'string' ? contextObj : JSON.stringify(contextObj, null, 2);

      try {
        fs.writeFileSync(tmpFile, ctxStr);
      } catch (e) {
        return reject(new Error(`Failed to write context file: ${e.message}`));
      }

      const child = spawn('node', [
        NODE_CLI,
        '--type',         type,
        '--ticker',       String(ticker),
        '--workspace',    DEFAULT_WORKSPACE,
        '--context-file', tmpFile,
      ], {
        cwd:   OPENCLAW_DIR,
        env:   { ...process.env, OPENCLAW_DIR },
        stdio: ['ignore', 'pipe', 'pipe'],
      });

      let stdout = '';
      let stderr = '';
      child.stdout.on('data', (d) => { stdout += d; });
      child.stderr.on('data', (d) => { stderr += d; process.stderr.write(d); });

      child.on('exit', (code) => {
        fs.unlink(tmpFile, () => {});
        if (code !== 0) {
          // claude-bin sometimes exits non-zero with all useful detail on
          // stdout (the Anthropic CLI dumps its error JSON there in --print
          // mode). Capture both streams so the operator sees the actual cause
          // — auth failure, rate limit, prompt parse error — instead of just
          // the spawn-line preamble.
          const combined = [
            stderr ? `stderr: ${stderr.trim()}` : '',
            stdout ? `stdout: ${stdout.trim()}` : '',
          ].filter(Boolean).join(' | ').slice(0, 1500);
          return reject(new Error(`${type} exited ${code}: ${combined || '(no output captured)'}`));
        }
        try {
          const parsed = JSON.parse(stdout);
          this._sessionCost += parsed.total_cost_usd ?? 0;
          resolve(parsed.result ?? stdout);
        } catch {
          resolve(stdout);
        }
      });

      child.on('error', (err) => {
        fs.unlink(tmpFile, () => {});
        reject(err);
      });
    });
  }

  // ── Postgres helpers ────────────────────────────────────────────────────────

  async _query(sql, params = []) {
    if (!this._pool) {
      const { Pool } = require('pg');
      this._pool = new Pool({
        connectionString: process.env.POSTGRES_URI,
        max: 5,
      });
    }
    return this._pool.query(sql, params);
  }

  async _getPendingCount() {
    try {
      const { rows } = await this._query(
        `SELECT COUNT(*)::int AS n FROM research_candidates WHERE status = 'pending'`
      );
      return rows[0]?.n || 0;
    } catch { return 0; }
  }

  // ── Redis helpers ───────────────────────────────────────────────────────────

  async _getRedis() {
    if (!this._redis) {
      const Redis = require('ioredis');
      this._redis = new Redis(process.env.REDIS_URL || 'redis://localhost:6379', {
        maxRetriesPerRequest: 3,
        lazyConnect: true,
      });
      this._redis.on('error', (e) => console.error('[research-orch] Redis error:', e.message));
    }
    return this._redis;
  }
}

module.exports = ResearchOrchestrator;
