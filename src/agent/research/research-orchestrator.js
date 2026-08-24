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
const { spawn, execSync, spawnSync } = require('child_process');
const { spawnWithTimeout } = require('../../lib/spawn_timeout');
const { emitGateDecision, paperIdForCandidate } = require('./gate-decisions');
const { redteamStrategy } = require('../curators/strategy_redteam');

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
  const primary = path.join(IMPLEMENTATIONS_DIR, canonical);
  // Case-toggle fallback for strategycoder casing drift — the prompt template's
  // mixed example ("S_..." key + "s_..." canonical_file) historically produced
  // manifest entries that pointed at the wrong-case filename even though the
  // .py file existed (just with the strategy_id's actual case). Without this
  // fallback the validator emitted `Contract validation failed — File not
  // found` and quarantined the strategy as `validation_failed` despite working
  // code. 9 strategies were stuck this way before 2026-05-29.
  if (!fs.existsSync(primary) && canonical.length > 0) {
    const toggled = canonical[0].toUpperCase() === canonical[0]
      ? canonical[0].toLowerCase() + canonical.slice(1)
      : canonical[0].toUpperCase() + canonical.slice(1);
    const alt = path.join(IMPLEMENTATIONS_DIR, toggled);
    if (fs.existsSync(alt)) return alt;
  }
  return primary;
}

const PYTHON = process.env.PYTHON_BIN || 'python3';

/**
 * Validate an inferred_universe_filter name against the real CANDIDATE_PREDICATES
 * whitelist. Returns the name if valid (and gate is ON), null otherwise.
 * Gate: OPENCLAW_PHASE_D_PREDICATE_AT_MINT=1
 */
function _validateInferredFilter(name) {
  if (name == null) return null;
  if (process.env.OPENCLAW_PHASE_D_PREDICATE_AT_MINT !== '1') return null;  // gate
  const r = spawnSync(PYTHON, ['-c',
    'from src.strategies.universe_default import CANDIDATE_PREDICATES; '
    + 'import sys; sys.exit(0 if sys.argv[1] in CANDIDATE_PREDICATES else 1)',
    name], { encoding: 'utf8', cwd: OPENCLAW_DIR });
  if (r.status !== 0) {
    console.warn(`[research-orch] PaperHunter emitted invalid predicate '${name}', falling back to default`);
    return null;
  }
  return name;
}


/**
 * SP-4: Validate an inferred_instrument_class against VALID_INSTRUMENT_CLASSES.
 * Returns the class if valid AND the gate is ON; otherwise 'equity' (the
 * byte-identical default — gate OFF, null, or unknown all resolve to equity).
 * Gate: OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT=1
 */
function _validateInferredClass(name) {
  if (name == null) return 'equity';
  if (process.env.OPENCLAW_SP4_INSTRUMENT_CLASS_AT_MINT !== '1') return 'equity';  // gate
  const r = spawnSync(PYTHON, ['-c',
    'from src.strategies.lifecycle import VALID_INSTRUMENT_CLASSES; '
    + 'import sys; sys.exit(0 if sys.argv[1] in VALID_INSTRUMENT_CLASSES else 1)',
    name], { encoding: 'utf8', cwd: OPENCLAW_DIR });
  if (r.status !== 0) {
    console.warn(`[research-orch] PaperHunter emitted invalid instrument_class '${name}', falling back to equity`);
    return 'equity';
  }
  return name;
}

/**
 * SP-4: True iff `underlying` is in the Phase-0 synthetic-greeks envelope
 * (backtest.vol_index.VALID_OPTION_UNDERLYINGS — index/ETF ATM only). Used to
 * hard-reject out-of-envelope option strategies (single-name / OTM-wing) before
 * coding, since the synthetic engine can't price them with promotion-grade
 * fidelity. Not gated: the orchestrator only calls it when class=='option',
 * which itself requires the gate ON.
 */
function _optionUnderlyingSupported(underlying) {
  if (!underlying) return false;
  const r = spawnSync(PYTHON, ['-c',
    'from src.backtest.vol_index import is_supported_option_underlying; '
    + 'import sys; sys.exit(0 if is_supported_option_underlying(sys.argv[1]) else 1)',
    String(underlying)], { encoding: 'utf8', cwd: OPENCLAW_DIR });
  return r.status === 0;
}

/**
 * Task R2 review fix (Minor): shape-validate factor_prescreen.py's parsed
 * stdout before trusting it as a verdict. JSON.parse happily succeeds on
 * `5`, `"ok"`, `null`, `[]`, or `{}` — none of which carry the boolean
 * `pass` field the orchestrator branches on below. Without this check those
 * shapes fell through to the final `else` (clean-pass) branch instead of
 * being treated as the infra failure they actually are. Only an object with
 * a boolean `pass` field counts as a real prescreen verdict; everything
 * else (including `null`, arrays, and primitives) is not.
 */
function _isPrescreenShape(obj) {
  return obj !== null
    && typeof obj === 'object'
    && !Array.isArray(obj)
    && typeof obj.pass === 'boolean';
}

// Pure builder for the strategycoder subagent context. Extracted from
// _codeStrategy so the porting hook is unit-testable without spawning a
// subagent. Returns the EXACT ctx object _codeStrategy used to build inline,
// PLUS porting fields (REFERENCE_IMPLEMENTATION / PORTING_GUIDE / SOURCE_URL)
// ONLY when strategySpec.reference_impl is truthy (git_blueprint origin).
// Paper specs (no reference_impl) get the original 5-key ctx, byte-identical.
// The SP-4 option-underlying envelope guard lives here (it throws on an
// unsupported option underlying, exactly as the inline code did → the
// rejection propagates through _codeFromQueue's catch).
function buildCoderContext(strategySpec) {
  const validInferred = _validateInferredFilter(strategySpec?.inferred_universe_filter ?? null);
  const validClass    = _validateInferredClass(strategySpec?.inferred_instrument_class ?? null);
  // SP-4 envelope guard: an option strategy must be on a Phase-0-supported
  // index/ETF underlying, else the synthetic greeks engine can't price it
  // with promotion-grade fidelity. Hard-reject here (propagates to
  // _codeFromQueue's catch → status 'coding'→'failed') rather than emit a
  // bogus backtest. Only fires when the gate is ON (validClass=='option').
  if (validClass === 'option'
      && !_optionUnderlyingSupported(strategySpec?.inferred_option_underlying ?? null)) {
    throw new Error(
      `option_envelope_unsupported: underlying `
      + `'${strategySpec?.inferred_option_underlying ?? null}' not in `
      + `VALID_OPTION_UNDERLYINGS (Phase-0 index/ETF-ATM envelope)`);
  }
  const ctx = {
    role:          'implement_strategy',
    STRATEGY_SPEC: JSON.stringify(strategySpec),
    instructions:  'Implement this strategy. Apply fundjohn:strategy-coder and fundjohn:backtest-plumb skills.',
    INFERRED_UNIVERSE_FILTER:  validInferred,  // null or one of the 16 CANDIDATE_PREDICATES (12 legacy + 4 SP-7 tier ladder)
    INFERRED_INSTRUMENT_CLASS: validClass,     // equity (default/gate-off) | option | etp | crypto | futures
  };
  // Porting mode (Blueprint Fast Lane): git-imported strategies arrive with
  // the original LEAN/QuantConnect source in `reference_impl`. Hand it to the
  // coder + point at the porting guide so it TRANSLATES the rule into our
  // contract (clean-room) rather than reinventing. Conditionally added (never
  // assigned undefined) so paper specs keep an identical key set.
  if (strategySpec?.reference_impl) {
    ctx.REFERENCE_IMPLEMENTATION = strategySpec.reference_impl;
    ctx.PORTING_GUIDE            = 'docs/strategy-coding/quantconnect-to-basestrategy.md';
    ctx.SOURCE_URL               = strategySpec.reference_url;
  }
  return ctx;
}

// Async python runner — returns {stdout, stderr, code}. Unlike execSync, the
// Node event loop keeps serving HTTP traffic while this runs, so the Cancel
// button / other dashboard actions remain responsive during long backtests.
// If opts.onChild is provided, it's invoked synchronously with the spawned
// ChildProcess so the caller can SIGTERM it later (used by Cancel).
function _spawnPython(args, opts = {}) {
  const { cwd, timeoutMs = 600_000, onChild, env } = opts;
  return new Promise((resolve) => {
    const child = spawn('python3', args, { cwd, env: env || process.env });
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
           -- Blueprint Fast Lane: git-imported candidates (kind='git') carry a
           -- pre-curated spec + reference_impl and must be coded ONLY by the
           -- gated saturday_brain_finisher path (OPENCLAW_GIT_INGEST). Excluding
           -- them here keeps this ungated daily drainer from (a) bypassing the
           -- soak gate and (b) re-running PaperHunter on them, which would drop
           -- reference_impl and waste a hunt call on a GitHub blob URL.
           AND kind <> 'git'
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
      const hr = hunterResults.find(h => h.candidate_id === item.candidate_id);
      const specWithPred = {
        ...item.strategy_spec,
        inferred_universe_filter:  hr?.inferred_universe_filter ?? null,
        inferred_instrument_class: hr?.inferred_instrument_class ?? null,
      };
      await this._query(
        `INSERT INTO implementation_queue (candidate_id, strategy_spec, status)
         VALUES ($1, $2, 'pending')`,
        [item.candidate_id, JSON.stringify(specWithPred)]
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
      await this._codeFromQueue({ ...item, strategy_spec: specWithPred }, notify, channelNotify);
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
    // `let`, not `const` — re-resolved after strategycoder runs (below).
    let implPath = _resolveImplPath(stratId);
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

    // Re-resolve the path after strategycoder ran: the coder writes
    // `${stratId}.py` (exact case) while a finisher-staged manifest may carry
    // a LOWERCASED canonical_file — the pre-coding resolution predates the
    // file's existence, so _resolveImplPath's case-toggle fallback couldn't
    // fire and validation checked a stale, nonexistent lowercase path
    // (2026-07-14: 4 staging builds quarantined as contract_violation this
    // way despite freshly-written working code).
    if (!skipCoding) implPath = _resolveImplPath(stratId);

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
    notify?.(`  ✅ ${stratId} validation passed — running red-team review...`);
    onPhase('redteam', 50);

    // ── Phase 1.5: Mandatory LLM red-team gate (Task S1) ──────────────────────
    // Code-enforced: runs for EVERY candidate that reaches this point, never
    // skippable by an LLM's discretion. redteamStrategy() derives its verdict
    // in code from structured findings (never trusts the model's own verdict
    // field) and fails OPEN on infra trouble — see strategy_redteam.js. A
    // block here follows the exact same DB-update / emitGateDecision /
    // early-return pattern as the validate-failure path above. Reuses
    // vPaperId (same candidate_id -> same paper_id lookup) rather than
    // re-querying paperIdForCandidate a second time.
    const rtPaperId = vPaperId;
    let rtResult;
    try {
      rtResult = await redteamStrategy({
        implPath,
        paperContext: strategy_spec?.hypothesis_one_liner || strategy_spec?.signal_logic || null,
      });
    } catch (e) {
      // redteamStrategy() already contains its own retry/infra-fail handling
      // around the claude-bin call; this catch is a last-resort backstop so
      // an unexpected bug here can never silently block research either.
      console.error(`[redteam] unexpected exception auditing ${stratId}: ${e.message}`);
      rtResult = { verdict: 'pass', findings: [], infra_fail: true };
    }

    if (rtResult.infra_fail) {
      await emitGateDecision({
        paperId:     rtPaperId,
        candidateId: candidate_id,
        strategyId:  stratId,
        gateName:    'redteam',
        outcome:     'pass',
        reasonCode:  'redteam_infra_fail',
        metadata:    { findings: rtResult.findings },
      });
      notify?.(`  ⚠️ ${stratId} red-team reviewer infra failure — WARN-and-pass, continuing to backtest.`);
    } else if (rtResult.verdict === 'block') {
      const criticalFindings = rtResult.findings.filter((f) => f.severity === 'critical');
      const reason = criticalFindings[0]?.concern || 'red-team gate blocked (no concern text)';
      await this._query(
        `UPDATE implementation_queue SET status = 'redteam_blocked', error_log = $1 WHERE candidate_id = $2`,
        [reason, candidate_id]
      );
      await emitGateDecision({
        paperId:      rtPaperId,
        candidateId:  candidate_id,
        strategyId:   stratId,
        gateName:     'redteam',
        outcome:      'reject',
        reasonCode:   'redteam_blocked',
        reasonDetail: reason,
        metadata:     { findings: rtResult.findings },
      });
      notify?.(`  ❌ ${stratId} blocked by red-team gate: ${reason.slice(0, 200)}`);
      channelNotify?.(`❌ **${stratId}** blocked by red-team review — ${reason.slice(0, 200)}`);
      return { promoted: false, reasonCode: 'redteam_blocked', error: reason };
    } else {
      await emitGateDecision({
        paperId:     rtPaperId,
        candidateId: candidate_id,
        strategyId:  stratId,
        gateName:    'redteam',
        outcome:     'pass',
        metadata:    { findings: rtResult.findings },
      });
    }

    notify?.(`  ✅ ${stratId} red-team review passed — running factor prescreen...`);
    onPhase('prescreen', 55);

    // ── Phase 1.75: Cheap pre-backtest factor screen (Task R2) ────────────────
    // Code-enforced, same as validate/redteam above: runs for every candidate
    // that reaches this point. CONSERVATIVE BY DESIGN — hard-blocks ONLY
    // provably degenerate output (zero signals anywhere in the sample window,
    // or 100% constant output); everything else annotates with stats and
    // passes. Purely a cheap filter to skip the ~900s unified_backtest run
    // where it is provably pointless — never a quality gate. Any infra
    // trouble (non-zero exit incl. a SIGTERM'd timeout, which resolves with
    // code===null — hence `code !== 0` rather than `code === 1`, unparseable
    // stdout) is logged as prescreen_infra_fail and PASSES, mirroring
    // redteam's fail-open contract above.
    const psPaperId = vPaperId;
    let psResult = null;
    let psInfraFail = false;
    let psInfraReason = null;
    try {
      const { stdout, code } = await _spawnPython(
        ['-m', 'backtest.factor_prescreen', '--strategy-file', implPath],
        { cwd: OPENCLAW_DIR, timeoutMs: 120_000, onChild: opts.onChild,
          env: { ...process.env, PYTHONPATH: 'src' } });
      if (code !== 0) {
        psInfraFail = true;
        psInfraReason = `factor_prescreen.py exit=${code}; stdout: ${stdout.slice(-300)}`;
      } else {
        try {
          // Tolerate a stray print() from a generated strategy — the JSON
          // verdict is always the last line factor_prescreen.py emits.
          const lastLine = stdout.trim().split('\n').pop();
          const parsed = JSON.parse(lastLine);
          // Shape-validate: `5`, `{}`, `"ok"`, `null`, `[]` all parse as
          // valid JSON but are not a real {pass, reason, stats} verdict.
          // Treat anything that isn't exactly that shape as an infra
          // failure (same fail-open contract as an unparseable line),
          // rather than letting it fall through to the clean-pass branch.
          if (_isPrescreenShape(parsed)) {
            psResult = parsed;
          } else {
            psInfraFail = true;
            psInfraReason = `factor_prescreen.py stdout parsed but is not a valid prescreen result shape: ${lastLine.slice(0, 300)}`;
          }
        } catch (e) {
          psInfraFail = true;
          psInfraReason = `factor_prescreen.py unparseable stdout: ${stdout.slice(0, 300)}`;
        }
      }
    } catch (e) {
      psInfraFail = true;
      psInfraReason = `factor_prescreen.py threw: ${e.message}`;
    }

    if (psInfraFail) {
      await emitGateDecision({
        paperId:      psPaperId,
        candidateId:  candidate_id,
        strategyId:   stratId,
        gateName:     'prescreen',
        outcome:      'pass',
        reasonCode:   'prescreen_infra_fail',
        reasonDetail: psInfraReason,
      });
      notify?.(`  ⚠️ ${stratId} factor prescreen infra failure — WARN-and-pass, continuing to backtest.`);
    } else if (psResult && psResult.pass === false) {
      await this._query(
        `UPDATE implementation_queue SET status = 'prescreen_failed', error_log = $1 WHERE candidate_id = $2`,
        [psResult.reason || 'prescreen_failed', candidate_id]
      );
      await emitGateDecision({
        paperId:     psPaperId,
        candidateId: candidate_id,
        strategyId:  stratId,
        gateName:    'prescreen',
        outcome:     'reject',
        reasonCode:  psResult.reason || 'prescreen_failed',
        metadata:    { stats: psResult.stats },
      });
      notify?.(`  ❌ ${stratId} blocked by factor prescreen: ${psResult.reason}`);
      channelNotify?.(`❌ **${stratId}** blocked by factor prescreen — ${psResult.reason}`);
      return { promoted: false, reasonCode: 'prescreen_failed', error: psResult.reason };
    } else {
      // psResult.reason is non-null (but pass=true) for the two soft-pass
      // annotations — 'prescreen_skipped_aux_dependent' (Concern-1 fix) and
      // 'zero_signals_on_fallback_universe' (Concern-2 fix) — and null for
      // a genuine full pass. Recorded as reasonCode here (not just buried
      // inside metadata.stats, which is null for the aux-dependent case
      // anyway) so these annotations stay queryable/visible in
      // paper_gate_decisions rather than being indistinguishable from a
      // plain pass.
      await emitGateDecision({
        paperId:     psPaperId,
        candidateId: candidate_id,
        strategyId:  stratId,
        gateName:    'prescreen',
        outcome:     'pass',
        reasonCode:  psResult?.reason || null,
        metadata:    { stats: psResult?.stats || null },
      });
    }

    notify?.(`  ✅ ${stratId} factor prescreen passed — running backtest (may take 2–5 min)...`);
    onPhase('backtest', 60);

    // ── Phase 2: Unified backtest convergence gate ────────────────────────────
    // 2026-05-14 cutover: unified_backtest is the canonical single source
    // of truth (discovery methodology, per-regime breakdown, persisted to
    // strategy_backtest_runs + strategy_backtest_regimes + strategy_backtest_trades).
    // After the run we invoke eligibility_assigner so the new strategy's
    // metadata.eligible_regimes is set from data, not the author's class declaration.
    // Heartbeat emits progress every 5s so the dashboard chip ticks visibly.
    let btResult;
    {
      let hb = 60;
      const heartbeat = setInterval(() => {
        hb = Math.min(hb + 2, 85);
        try { onPhase('backtest', hb); } catch (_) {}
      }, 5_000);
      try {
        // --universe-cap tier_liquid (operator directive 2026-08-10): every NEW
        // candidate's first backtest is bounded to the largest backtestable
        // ladder tier. An uncapped 12,548-ticker run OOM-killed the Saturday
        // finisher on 2026-08-08 (SPY-only strategy, 5.8G unit peak); the cap
        // is also stamped into manifest metadata at registration below so
        // every later re-backtest inherits it.
        const { stdout, stderr, code } = await _spawnPython(
          ['-m', 'backtest.unified_backtest', '--strategy-file', implPath,
           '--universe-cap', 'tier_liquid'],
          { cwd: OPENCLAW_DIR, timeoutMs: 900_000, onChild: opts.onChild,
            env: { ...process.env, PYTHONPATH: 'src' } });
        // unified_backtest writes log lines to stdout and the run_id at the end.
        // It does NOT return JSON; we synthesize a minimal btResult by querying
        // strategy_backtest_runs for the just-written row.
        const runIdMatch = stdout.match(/run_id=([0-9a-f-]{36})/);
        if (code !== 0 || !runIdMatch) {
          btResult = { error: `unified_backtest exit=${code}; stdout: ${stdout.slice(-400)}; stderr: ${stderr.slice(-400)}` };
        } else {
          try {
            const runRes = await this._query(
              `SELECT total_sharpe, total_max_dd_pct, total_return_pct, total_trades,
                      total_hit_rate, avg_holding_days, run_id
               FROM strategy_backtest_runs WHERE run_id = $1`,
              [runIdMatch[1]]
            );
            const regRes = await this._query(
              `SELECT regime_state, sharpe, max_dd_pct, return_pct, trade_count, hit_rate
               FROM strategy_backtest_regimes WHERE run_id = $1`,
              [runIdMatch[1]]
            );
            const r = runRes.rows[0] || {};
            const regime_breakdown = {};
            for (const row of regRes.rows) {
              regime_breakdown[row.regime_state] = {
                sharpe:           row.sharpe,
                max_dd:           row.max_dd_pct != null ? row.max_dd_pct / 100 : null,
                total_return_pct: row.return_pct,
                trade_count:      row.trade_count,
                hit_rate:         row.hit_rate,
              };
            }
            btResult = {
              sharpe:            r.total_sharpe,
              max_dd:            r.total_max_dd_pct != null ? r.total_max_dd_pct / 100 : null,
              total_return_pct:  r.total_return_pct,
              trade_count:       r.total_trades,
              hit_rate:          r.total_hit_rate,
              avg_holding_days:  r.avg_holding_days,
              regime_breakdown,
              run_id:            r.run_id,
              method:            'unified_backtest_discovery',
            };
          } catch (e) {
            btResult = { error: `unified_backtest succeeded but DB read failed: ${e.message}` };
          }
        }
      } finally {
        clearInterval(heartbeat);
      }
    }

    // Auto-set eligible_regimes from the run's per-regime metrics. Best-effort;
    // a failure here doesn't block promotion (the operator can rerun later).
    if (btResult && !btResult.error && btResult.run_id) {
      try {
        await _spawnPython(
          ['-m', 'backtest.eligibility_assigner', '--strategy-id', stratId],
          { cwd: OPENCLAW_DIR, timeoutMs: 30_000,
            env: { ...process.env, PYTHONPATH: 'src' } });
      } catch (e) {
        notify?.(`  ⚠️ ${stratId} eligibility_assigner failed (non-fatal): ${e.message?.slice(0, 200)}`);
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
      `    # backtest_universe_cap=tier_liquid (operator directive 2026-08-10):`,
      `    # new candidates are ALWAYS ladder-bounded — the first backtest ran with`,
      `    # --universe-cap tier_liquid, and this stamp makes every later re-backtest`,
      `    # (fleet nightly, re-gates) inherit the same bound.`,
      `    lsm.register(sid, initial_state=StrategyState.CANDIDATE, metadata={'canonical_file': sid + '.py', 'backtest_universe_cap': 'tier_liquid'})`,
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
    // DEPRECATED: registry backtest_* mirror (read-consumers retired 2026-07-05)
    // Still written here for backward-compat (Option B keeps the write path
    // intact — append-only, no column drop). Canonical strategy_backtest_runs
    // is the sole read source now: src/lib/promotion_service.js,
    // src/research/strategy_forensics.py, src/services/mastermind_chat/snapshot.py,
    // src/channels/api/routes_research.js, src/channels/discord/relay.js.
    // See docs/archive/superpowers/specs/2026-07-05-option-b-mirror-retirement-design.md.
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
    // ctx construction (validation + SP-4 option-envelope guard + porting hook)
    // extracted to the pure, unit-tested buildCoderContext. The guard still
    // throws synchronously on an unsupported option underlying, which rejects
    // this async method's promise exactly as the inline throw did → propagates
    // to _codeFromQueue's catch.
    const ctx = buildCoderContext(strategySpec);
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

      // DEPRECATED: registry backtest_* mirror (read-consumers retired 2026-07-05)
      // backtest_sharpe seeded here is placeholder-only (initial registration,
      // pre-first-real-backtest); canonical strategy_backtest_runs is what
      // every reader now consults. See
      // docs/archive/superpowers/specs/2026-07-05-option-b-mirror-retirement-design.md.
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
    const tmpFile = `/tmp/research-ctx-${Date.now()}-${Math.random().toString(36).slice(2)}.json`;
    const ctxStr  = typeof contextObj === 'string' ? contextObj : JSON.stringify(contextObj, null, 2);

    try {
      fs.writeFileSync(tmpFile, ctxStr);
    } catch (e) {
      return Promise.reject(new Error(`Failed to write context file: ${e.message}`));
    }

    // Hard wall-clock cap so a single wedged claude-bin child can't block the
    // hunt fan-out (Promise.all over workers) until systemd's 6h ceiling. On
    // timeout the child is SIGKILLed and this rejects → the worker's per-
    // candidate catch degrades it to `fetch_failed` and the batch continues.
    // Backstop against an INFINITE hang, not a tight SLA — set well above the
    // longest legitimate paperhunter/strategycoder run (a complex strategycoder
    // can take many minutes). 20min default; override via env.
    const timeoutMs = (parseInt(process.env.OPENCLAW_SUBAGENT_TIMEOUT_S || '1200', 10) || 1200) * 1000;

    return spawnWithTimeout('node', [
      NODE_CLI,
      '--type',         type,
      '--ticker',       String(ticker),
      '--workspace',    DEFAULT_WORKSPACE,
      '--context-file', tmpFile,
    ], {
      cwd:   OPENCLAW_DIR,
      env:   { ...process.env, OPENCLAW_DIR },
      stdio: ['ignore', 'pipe', 'pipe'],
    }, { timeoutMs }).then(({ code, stdout, stderr, timedOut, error }) => {
      fs.unlink(tmpFile, () => {});
      if (timedOut) {
        throw new Error(`${type} timed out after ${timeoutMs / 1000}s (SIGKILLed)`);
      }
      if (error && code === -1) {
        throw new Error(`${type} spawn error: ${error}`);
      }
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
        throw new Error(`${type} exited ${code}: ${combined || '(no output captured)'}`);
      }
      try {
        const parsed = JSON.parse(stdout);
        this._sessionCost += parsed.total_cost_usd ?? 0;
        return parsed.result ?? stdout;
      } catch {
        return stdout;
      }
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
module.exports._validateInferredFilter = _validateInferredFilter;
module.exports._validateInferredClass = _validateInferredClass;
module.exports._optionUnderlyingSupported = _optionUnderlyingSupported;
module.exports._isPrescreenShape = _isPrescreenShape;
module.exports.buildCoderContext = buildCoderContext;
