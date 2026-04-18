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

const OPENCLAW_DIR      = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');
const NODE_CLI          = path.join(OPENCLAW_DIR, 'src/agent/run-subagent-cli.js');
const DEFAULT_WORKSPACE = path.join(OPENCLAW_DIR, 'workspaces/default');

const PAUSE_KEY = 'research:pause_requested';
const BATCH_SIZE = 5;  // candidates per processQueue call

const STOP_AFTER_KEY = 'research:stop_after_promoted';   // Redis key for one-shot mode

class ResearchOrchestrator {
  constructor() {
    this._redis = null;
    this._pool  = null;
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
        notify?.('✅ Queue empty — research complete.');
        channelNotify?.('✅ **Research queue exhausted** — all papers processed.');
        break;
      }

      const redis  = await this._getRedis();
      const paused = await redis.get(PAUSE_KEY);
      if (paused === '1') {
        notify?.('⏸ Research paused.');
        break;
      }

      await this.processQueue({ notify, channelNotify });
    }
  }

  /**
   * Process one batch of up to BATCH_SIZE pending candidates.
   */
  async processQueue({ notify, channelNotify } = {}) {
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
       RETURNING candidate_id, source_url`,
      [BATCH_SIZE]
    );

    if (batch.length === 0) return;

    notify?.(`🔍 **Batch started** — extracting ${batch.length} paper(s)...`);

    // Phase 1: Run PaperHunter per candidate (parallel)
    const hunterResults = await Promise.all(
      batch.map(row => this._runPaperHunter(row).catch(e => {
        console.error(`[research-orch] PaperHunter failed for ${row.candidate_id}:`, e.message);
        return { rejection_reason_if_any: 'fetch_failed', candidate_id: row.candidate_id, source_url: row.source_url };
      }))
    );

    // Store hunter results on each candidate row
    for (const result of hunterResults) {
      if (!result?.candidate_id) continue;
      await this._query(
        `UPDATE research_candidates SET hunter_result_json = $1 WHERE candidate_id = $2`,
        [JSON.stringify(result), result.candidate_id]
      );
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
      const colList = (item.missing_columns || []).join(', ');
      channelNotify?.(`🔧 **BUILDABLE** strategy \`${item.strategy_spec?.strategy_id}\` — needs columns: \`${colList}\`. Awaiting BotJohn approval.`);
    }

    // BLOCKED → update status
    for (const item of (classification.blocked || [])) {
      await this._query(
        `UPDATE research_candidates SET status = 'blocked_rejected' WHERE candidate_id = $1`,
        [item.candidate_id]
      );
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

    const summary = `Batch complete — READY: ${(classification.ready||[]).length}, BUILDABLE: ${(classification.buildable||[]).length}, BLOCKED: ${(classification.blocked||[]).length}`;
    notify?.(summary);
    channelNotify?.(`📊 **Research batch complete** — ${summary}`);
  }

  // ── Coding sub-phase ────────────────────────────────────────────────────────

  async _codeFromQueue(item, notify, channelNotify) {
    const { candidate_id, strategy_spec } = item;
    const stratId  = strategy_spec?.strategy_id || 'unknown';
    const implPath = path.join(OPENCLAW_DIR, 'src', 'strategies', 'implementations', `${stratId}.py`);

    await this._query(
      `UPDATE implementation_queue SET status = 'coding' WHERE candidate_id = $1`,
      [candidate_id]
    );

    notify?.(`  ⚙️ Coding: ${stratId}...`);
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
      return;
    }

    // ── Phase 1: Contract validation ─────────────────────────────────────────
    // validate_strategy.py exits 0 on valid, 1 on invalid — capture stdout on throw.
    let validResult;
    try {
      const raw = execSync(
        `python3 src/strategies/validate_strategy.py "${implPath}"`,
        { cwd: OPENCLAW_DIR, timeout: 60_000 }
      ).toString();
      validResult = JSON.parse(raw);
    } catch (e) {
      const stdout = e.stdout?.toString?.() || '';
      try {
        validResult = JSON.parse(stdout);
      } catch (_) {
        validResult = { ok: false, errors: [e.message.slice(0, 300)] };
      }
    }

    if (!validResult.ok) {
      const errLog = (validResult.errors || []).join('\n');
      await this._query(
        `UPDATE implementation_queue SET status = 'validation_failed', error_log = $1 WHERE candidate_id = $2`,
        [errLog, candidate_id]
      );
      notify?.(`  ❌ ${stratId} validation failed: ${errLog.slice(0, 200)}`);
      channelNotify?.(`❌ **${stratId}** failed contract validation — see implementation_queue for errors.`);
      return;
    }
    notify?.(`  ✅ ${stratId} validation passed — running backtest (may take 2–5 min)...`);

    // ── Phase 2: Auto-backtest convergence gate ───────────────────────────────
    // Note: auto_backtest.py exits 0 on pass, 1 on fail — execSync throws on
    // non-zero exit. Parse stdout from the error object to retain JSON metrics.
    let btResult;
    try {
      const raw = execSync(
        `python3 src/strategies/auto_backtest.py "${implPath}"`,
        { cwd: OPENCLAW_DIR, timeout: 600_000 }
      ).toString();
      btResult = JSON.parse(raw);
    } catch (e) {
      const stdout = e.stdout?.toString?.() || '';
      try {
        btResult = JSON.parse(stdout);
      } catch (_) {
        btResult = { passed: false, error: e.message.slice(0, 300), windows: [] };
      }
    }

    if (!btResult.passed) {
      await this._query(
        `UPDATE implementation_queue SET status = 'backtest_failed', backtest_result = $1 WHERE candidate_id = $2`,
        [JSON.stringify(btResult), candidate_id]
      );
      const summary = btResult.error
        ? btResult.error.slice(0, 200)
        : `Sharpe ${btResult.sharpe?.toFixed(2)}, DD ${(btResult.max_dd * 100)?.toFixed(1)}%, trades ${btResult.trade_count}`;
      notify?.(`  ❌ ${stratId} backtest failed: ${summary}`);
      channelNotify?.(`❌ **${stratId}** failed backtest gate — ${summary}.`);
      return;
    }

    // ── Phase 3: Auto-promote CANDIDATE → PAPER ───────────────────────────────
    const reason = `Auto-backtest: Sharpe ${btResult.sharpe?.toFixed(2)}, DD ${(btResult.max_dd * 100)?.toFixed(1)}%, trades ${btResult.trade_count}`;
    try {
      execSync(
        `python3 -c "
import sys; sys.path.insert(0, 'src')
from strategies.lifecycle import LifecycleStateMachine, StrategyState
lsm = LifecycleStateMachine.from_manifest('src/strategies/manifest.json')
lsm.transition('${stratId}', StrategyState.PAPER, actor='auto_backtest', reason='${reason.replace(/'/g, '')}')
lsm.save_manifest('src/strategies/manifest.json')
"`,
        { cwd: OPENCLAW_DIR, timeout: 30_000 }
      );
    } catch (e) {
      // Lifecycle transition failure is non-fatal — strategy is still coded
      notify?.(`  ⚠️ ${stratId} lifecycle promotion failed: ${e.message.slice(0, 200)}`);
    }

    // Update strategy_registry status to paper
    await this._query(
      `UPDATE strategy_registry SET status = 'paper' WHERE id = $1`,
      [stratId]
    ).catch((e) => console.error(`[research-orch] registry status update failed: ${e.message}`));

    await this._query(
      `UPDATE implementation_queue SET status = 'promoted', backtest_result = $1 WHERE candidate_id = $2`,
      [JSON.stringify(btResult), candidate_id]
    );

    const summary = `Sharpe ${btResult.sharpe?.toFixed(2)}, DD ${(btResult.max_dd * 100)?.toFixed(1)}%, ${btResult.trade_count} trades`;
    notify?.(`  🚀 ${stratId} → PAPER trading (${summary})`);
    channelNotify?.(`🚀 **${stratId}** auto-promoted to paper trading — ${summary}`);

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

  /**
   * Wire removal of a deprecated column after BotJohn approval.
   */
  async _unwireColumn(queueRow) {
    const ctx = {
      role:        'remove_column',
      REQUEST_ID:  queueRow.request_id,
      COLUMN_NAME: queueRow.column_name,
      action:      queueRow.recommended_action,
    };
    return this._runSubagent('datawiring', queueRow.column_name, ctx);
  }

  // ── Weekly Reaper ───────────────────────────────────────────────────────────

  async runReaperPass(notify) {
    notify?.('🔍 Reaper pass starting — scanning for orphaned data columns...');

    let rows;
    try {
      const result = await this._query(
        `SELECT column_name, last_consumed_at
         FROM data_ledger
         WHERE current_users = '{}'
           AND (last_consumed_at IS NULL OR last_consumed_at < NOW() - INTERVAL '30 days')`
      );
      rows = result.rows;
    } catch (e) {
      notify?.(`⚠️ Reaper: data_ledger query failed — ${e.message}`);
      return;
    }

    if (rows.length === 0) {
      notify?.('✅ Reaper pass complete — no orphaned columns found.');
      return;
    }

    for (const row of rows) {
      await this._query(
        `INSERT INTO data_deprecation_queue (column_name, recommended_action)
         VALUES ($1, 'stop_collecting')
         ON CONFLICT DO NOTHING`,
        [row.column_name]
      ).catch(() => {});
    }

    notify?.(`🗑️ Reaper: ${rows.length} orphaned column(s) queued for deprecation: ${rows.map(r => r.column_name).join(', ')}`);
  }

  // ── Internal helpers ────────────────────────────────────────────────────────

  async _runPaperHunter(candidateRow) {
    const ctx = {
      role:          'extract',
      SOURCE_URL:    candidateRow.source_url,
      CANDIDATE_ID:  candidateRow.candidate_id,
    };
    const raw = await this._runSubagent('paperhunter', candidateRow.candidate_id.slice(0, 8), ctx);
    return this._parseJSON(raw) || { rejection_reason_if_any: 'parse_failed', candidate_id: candidateRow.candidate_id };
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
      const { rows } = await this._query(
        `SELECT column_name, current_users, provider FROM data_ledger LIMIT 500`
      );
      return rows;
    } catch { return []; }
  }

  _parseJSON(raw) {
    if (!raw) return null;
    try {
      if (typeof raw === 'object') return raw;
      const str = (raw.match(/[\[{][\s\S]*[\]}]/) || [raw])[0];
      return JSON.parse(str);
    } catch { return null; }
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
          return reject(new Error(`${type} exited ${code}: ${stderr.slice(0, 200)}`));
        }
        try {
          const parsed = JSON.parse(stdout);
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
