// Fused staging-approval worker.
//
// Operator clicks Approve on a STAGING strategy → this worker drives the
// entire pipeline as a single async job:
//
//   data_pipeline_setup → backfilling → coding → validating → backtesting →
//   promoting → succeeded
//
// On promotion the strategy lands in CANDIDATE state with backtest metrics
// on strategy_registry. The remaining gate (CANDIDATE → LIVE) is the
// operator's separate click (sharpe ≥ 0.5, max_dd ≤ 20%) handled by the
// existing /api/strategies/:id/transition endpoint.
//
// Phase advancement is persisted to strategy_approval_jobs.phase so the
// polling loop in approvals/index.js::_pollOnce can resume after a restart.
// resumeIfRunning(job, ctx) re-enters the appropriate phase based on what's
// already been done.

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR = path.resolve(__dirname, '..', '..', '..');
const REQ_DIR      = path.join(OPENCLAW_DIR, 'src', 'strategies', 'implementations');
const SCHEMA_PATH  = path.join(OPENCLAW_DIR, 'data', 'master', 'schema_registry.json');

// data_coverage / data_columns "fresh enough" cutoff (matches collector tolerance).
const COVERAGE_LAG_DAYS = 7;

// ── Requirements + coverage helpers ─────────────────────────────────────────

function readRequirements(strategyId) {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(OPENCLAW_DIR, 'src', 'strategies', 'manifest.json'), 'utf8'));
  const rec = manifest.strategies[strategyId] || {};
  const canonical = (rec.metadata && rec.metadata.canonical_file) || `${strategyId.toLowerCase()}.py`;
  const base = canonical.replace(/\.py$/, '');
  const p = path.join(REQ_DIR, `${base}.requirements.json`);
  if (!fs.existsSync(p)) return { required: [], optional: [] };
  const j = JSON.parse(fs.readFileSync(p, 'utf8'));
  return {
    required: Array.isArray(j.required) ? j.required : [],
    optional: Array.isArray(j.optional) ? j.optional : [],
  };
}

async function readPlannedRequirements(strategyId, dbQuery) {
  const { rows } = await dbQuery(
    `SELECT data_requirements_planned FROM strategy_registry WHERE id=$1`,
    [strategyId]
  ).catch(() => ({ rows: [] }));
  const planned = rows[0]?.data_requirements_planned;
  if (!planned) return null;
  let arr = planned;
  if (typeof arr === 'string') {
    try { arr = JSON.parse(arr); } catch (_) { return null; }
  }
  if (!Array.isArray(arr) || arr.length === 0) return null;
  const required = [...new Set(arr.map(e => e?.column || e?.data_type).filter(Boolean))];
  return { required, optional: [], _planned_route: arr };
}

async function loadEffectiveRequirements(strategyId, dbQuery) {
  const onDisk = readRequirements(strategyId);
  if (onDisk.required.length || onDisk.optional.length) return onDisk;
  const planned = await readPlannedRequirements(strategyId, dbQuery);
  if (planned) return planned;
  return onDisk;  // empty
}

function readSchemaRegistry() {
  if (!fs.existsSync(SCHEMA_PATH)) return {};
  return JSON.parse(fs.readFileSync(SCHEMA_PATH, 'utf8'));
}

/**
 * Resolve whether *src* is covered by an existing collector. Mirrors the
 * provider-resolution walk in src/pipeline/queue_drain.py + backfillers/__init__.py:
 *   1. data_columns ledger (Postgres) — any column-level row counts as supported
 *   2. schema_registry top-level dataset key (e.g. "prices", "financials")
 *   3. schema_registry sub-column inside any dataset's `columns` list
 *
 * Returns true if any of those match. The previous check was top-level only,
 * which incorrectly flagged sub-columns like `revenue` (under `financials`)
 * as unsupported.
 */
async function sourceIsKnownToProvider(src, schema, dbQuery) {
  if (!src) return false;
  // (1) data_columns ledger
  try {
    const { rows } = await dbQuery(
      `SELECT 1 FROM data_columns WHERE column_name=$1 LIMIT 1`,
      [src],
    );
    if (rows.length) return true;
  } catch (_) { /* fall through */ }
  // (2) top-level dataset key
  if (schema && schema[src]) return true;
  // (3) sub-column inside any dataset
  if (schema && typeof schema === 'object') {
    for (const meta of Object.values(schema)) {
      if (!meta || typeof meta !== 'object') continue;
      const cols = Array.isArray(meta.columns) ? meta.columns : [];
      if (cols.includes(src)) return true;
    }
  }
  return false;
}

async function sourcesMissingCoverage(sources, dbQuery) {
  const cutoff = new Date(Date.now() - COVERAGE_LAG_DAYS * 86_400_000)
    .toISOString().slice(0, 10);
  const missing = [];
  for (const src of sources) {
    const { rows: byCol } = await dbQuery(
      `SELECT 1 FROM data_columns WHERE column_name=$1 AND max_date >= $2 LIMIT 1`,
      [src, cutoff]).catch(() => ({ rows: [] }));
    if (byCol.length) continue;
    const { rows: byType } = await dbQuery(
      `SELECT 1 FROM data_coverage WHERE data_type=$1 AND date_to >= $2 LIMIT 1`,
      [src, cutoff]).catch(() => ({ rows: [] }));
    if (byType.length) continue;
    missing.push(src);
  }
  return missing;
}

// ── Strategy spec loader (folded in from candidate_approver) ────────────────

const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src', 'strategies', 'manifest.json');

async function loadStrategySpec(strategyId, dbQuery) {
  const r = await dbQuery(
    `SELECT candidate_id, hunter_result_json FROM research_candidates
      WHERE hunter_result_json->>'strategy_id' = $1
      ORDER BY submitted_at DESC LIMIT 1`, [strategyId]).catch(() => ({ rows: [] }));
  if (r.rows[0] && r.rows[0].hunter_result_json) {
    return { candidate_id: r.rows[0].candidate_id, ...r.rows[0].hunter_result_json };
  }

  try {
    const h = await dbQuery(
      `SELECT * FROM strategy_hypotheses WHERE strategy_id = $1 ORDER BY created_at DESC LIMIT 1`,
      [strategyId]);
    if (h.rows[0]) {
      const row = h.rows[0];
      return {
        strategy_id: strategyId,
        hypothesis_one_liner: row.hypothesis || row.description || 'See strategy_hypotheses row',
        signal_formula_pseudocode: row.formula || row.pseudocode || '',
        regime_applicability: row.regimes || ['LOW_VOL','TRANSITIONING','HIGH_VOL'],
        data_requirements: row.data_requirements || { required: [], optional: [] },
      };
    }
  } catch (_) { /* table may not exist */ }

  return _specFromManifest(strategyId);
}

function _specFromManifest(strategyId) {
  try {
    const m = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
    const rec = m.strategies && m.strategies[strategyId];
    if (!rec) return null;
    const md = rec.metadata || {};
    const description = md.description || '';
    let docstring = '';
    let activeRegimes = ['LOW_VOL','TRANSITIONING','HIGH_VOL'];
    let signalFreq = 'daily';
    try {
      if (md.canonical_file) {
        const implPath = path.join(REQ_DIR, md.canonical_file);
        const src = fs.readFileSync(implPath, 'utf8');
        const dm = src.match(/^"""([\s\S]*?)"""/m) || src.match(/^'''([\s\S]*?)'''/m);
        if (dm) docstring = dm[1].trim();
        const rm = src.match(/active_in_regimes\s*=\s*\[([^\]]+)\]/);
        if (rm) {
          activeRegimes = rm[1].split(',').map(s => s.trim().replace(/['"]/g,'')).filter(Boolean);
        }
        const fm = src.match(/signal_frequency\s*=\s*['"]([^'"]+)['"]/);
        if (fm) signalFreq = fm[1];
      }
    } catch (_) { /* impl file optional */ }

    const oneLiner = docstring.split('\n')[0] || description || `Manifest-only strategy ${strategyId}`;
    return {
      strategy_id: strategyId,
      _spec_origin: 'manifest_fallback',
      hypothesis_one_liner: oneLiner.slice(0, 280),
      signal_formula_pseudocode: docstring || description,
      regime_applicability: activeRegimes,
      signal_frequency: signalFreq,
      data_requirements: { required: ['prices'], optional: [] },
      manifest_metadata: md,
    };
  } catch (_) {
    return null;
  }
}

// ── Phase implementations ────────────────────────────────────────────────────

async function _phaseDataPipelineSetup(job, ctx) {
  const { dbQuery, updateJob, failJob, emit } = ctx;

  // Stamp the operator's approval click. Acts as the audit gate that
  // distinguishes "operator authorized" from "Saturday brain dropped a row
  // into staging".
  await dbQuery(
    `UPDATE strategy_registry
        SET staging_approved_at = COALESCE(staging_approved_at, NOW())
      WHERE id = $1`,
    [job.strategy_id]
  ).catch(() => {});

  const reqs    = await loadEffectiveRequirements(job.strategy_id, dbQuery);
  const all     = [...new Set([...reqs.required, ...reqs.optional])];
  const missing = await sourcesMissingCoverage(all, dbQuery);

  if (!missing.length) {
    // Tier-A path: nothing to backfill.
    await updateJob(job.job_id, {
      phase: 'coding', progress: 60,
      payload: { ...(job.payload || {}), missing_sources: [], inserted_queue_ids: [] },
    });
    emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id,
           status: 'running', phase: 'coding', progress: 60 });
    return { phase: 'coding', missing: [], queueIds: [] };
  }

  const schema = readSchemaRegistry();
  const unsupported = [];
  for (const s of missing) {
    if (!await sourceIsKnownToProvider(s, schema, dbQuery)) unsupported.push(s);
  }
  if (unsupported.length) {
    await failJob(job, {
      error: 'unsupported_source',
      unsupported,
      hint: `Add a collector module for ${unsupported.join(', ')} in data/master/schema_registry.json (or register it in data_columns) before approving.`,
    });
    return { phase: 'failed' };
  }

  const insertedIds = [];
  for (const src of missing) {
    const { rows: existing } = await dbQuery(
      `SELECT request_id FROM data_ingestion_queue
        WHERE column_name=$1 AND status IN ('PENDING','APPROVED') LIMIT 1`, [src]);
    if (existing.length) {
      await dbQuery(
        `UPDATE data_ingestion_queue
            SET status='APPROVED',
                approved_by=COALESCE(approved_by,$2),
                approved_at=COALESCE(approved_at,NOW()),
                backfill_status=CASE WHEN backfill_status='complete' THEN backfill_status ELSE 'pending' END
          WHERE request_id=$1`,
        [existing[0].request_id, job.actor]).catch(() => {});
      insertedIds.push(existing[0].request_id);
    } else {
      const { rows } = await dbQuery(
        `INSERT INTO data_ingestion_queue (column_name, status, approved_by, approved_at, backfill_status)
         VALUES ($1, 'APPROVED', $2, NOW(), 'pending') RETURNING request_id`,
        [src, job.actor]);
      insertedIds.push(rows[0].request_id);
    }
  }

  await updateJob(job.job_id, {
    phase: 'backfilling', progress: 5,
    payload: { ...(job.payload || {}), missing_sources: missing, inserted_queue_ids: insertedIds },
  });
  emit({
    type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id,
    status: 'running', phase: 'backfilling', progress: 5,
    payload: { missing_sources: missing },
  });
  return { phase: 'backfilling', missing, queueIds: insertedIds };
}

// Lazy-loaded poster for #data-alerts. Returns a function `(msg) => Promise`.
// Falls back to a no-op when the discord module isn't available (tests, scripts).
function _dataAlertsPoster() {
  try {
    const personas = require('../../channels/discord/agent-personas');
    return (msg) => personas.post('databot', 'data-alerts', msg).catch(() => {});
  } catch (_) {
    return () => {};
  }
}

async function _phaseBackfilling(job, ctx) {
  const { dbQuery, updateJob, failJob, emit } = ctx;
  const queueIds = (job.payload && job.payload.inserted_queue_ids) || [];
  if (queueIds.length === 0) {
    // Defensive: Tier-A jumped straight to coding; nothing to do.
    await updateJob(job.job_id, { phase: 'coding', progress: 60 });
    emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id,
           status: 'running', phase: 'coding', progress: 60 });
    return { phase: 'coding' };
  }

  const { runBackfill } = require('../../lib/backfill_runner');
  const dataAlert = _dataAlertsPoster();

  // Announce the inline backfill kicked off by this approval. Mirrors the
  // queue_drain.py format so the operator sees the same shape regardless
  // of which path drove the work.
  const missing = (job.payload && job.payload.missing_sources) || [];
  if (missing.length) {
    await dataAlert(
      `📥 **Staging-approval backfill started** — \`${job.strategy_id}\`\n` +
      `• Columns: ${missing.map(c => `\`${c}\``).join(', ')}\n` +
      `• Rows will register in data_columns and join the daily collection set.`
    );
  }

  let done = 0;
  const failures = [];
  for (const rid of queueIds) {
    // Skip rows already complete (resumption after restart).
    const { rows: existing } = await dbQuery(
      `SELECT backfill_status FROM data_ingestion_queue WHERE request_id=$1`,
      [rid]).catch(() => ({ rows: [] }));
    if (existing[0] && existing[0].backfill_status === 'complete') {
      done++;
      continue;
    }

    const result = await runBackfill(rid, {
      onChild: (child) => {
        try { require('./index')._internals.registerChild(job.job_id, child); }
        catch (_) {}
      },
    });
    if (!result.ok) {
      failures.push({ request_id: rid, column: result.column_name, error: result.error });
      await dataAlert(
        `❌ \`${result.column_name || 'unknown'}\` backfill FAILED for ` +
        `\`${job.strategy_id}\` — \`${(result.error || '').slice(0, 200)}\``
      );
      continue;
    }
    done++;
    const pct = 5 + Math.round((done / queueIds.length) * 55);   // 5→60
    await dbQuery(
      `UPDATE strategy_approval_jobs SET progress=$2 WHERE job_id=$1 AND progress<$2`,
      [job.job_id, pct]).catch(() => {});
    emit({
      type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id,
      status: 'running', phase: 'backfilling', progress: pct,
      payload: { backfilled: done, total: queueIds.length, last_column: result.column_name },
    });
    await dataAlert(
      `✅ **Backfill complete** — \`${result.column_name}\` for \`${job.strategy_id}\`\n` +
      `• Rows written: **${(result.rows_written || 0).toLocaleString()}** via \`${result.provider || '?'}\`\n` +
      `• Elapsed: **${(result.elapsed_s || 0).toFixed(1)}s**\n` +
      `• Column is now joining the daily collection set.`
    );
  }

  if (failures.length) {
    await failJob(job, {
      error: 'backfill_failed',
      failures,
      hint: 'One or more required columns failed to backfill — see queue rows for detail. Cancel + retry once the upstream issue is resolved.',
    });
    return { phase: 'failed' };
  }

  // Verify coverage now lands. If a backfiller reported success but
  // data_columns/data_coverage isn't yet visible, the strategycoder will fail
  // for the wrong reason; stall here briefly with a clearer error if so.
  const reqs = await loadEffectiveRequirements(job.strategy_id, dbQuery);
  const all  = [...new Set([...reqs.required, ...reqs.optional])];
  const stillMissing = await sourcesMissingCoverage(all, dbQuery);
  if (stillMissing.length) {
    await failJob(job, {
      error: 'coverage_lag',
      still_missing: stillMissing,
      hint: 'Backfill finished but data_coverage still does not show the columns. Check the backfiller output and retry.',
    });
    return { phase: 'failed' };
  }

  await updateJob(job.job_id, { phase: 'coding', progress: 60 });
  emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id,
         status: 'running', phase: 'coding', progress: 60 });
  return { phase: 'coding' };
}

async function _phaseCodeAndBacktest(job, ctx) {
  const { dbQuery, updateJob, finishJob, failJob, systemTransition, emit } = ctx;
  const ResearchOrchestrator = require('../research/research-orchestrator');

  const spec = await loadStrategySpec(job.strategy_id, dbQuery);
  if (!spec) {
    await failJob(job, {
      error: 'missing_strategy_spec',
      hint: `No research_candidates / strategy_hypotheses / manifest spec found for ${job.strategy_id}.`,
    });
    return;
  }
  spec.strategy_id = spec.strategy_id || job.strategy_id;

  // Insert/reuse implementation_queue row (kept for back-compat with the
  // research orchestrator which still keys off it).
  const { rows: existing } = await dbQuery(
    `SELECT item_id, candidate_id, strategy_spec FROM implementation_queue
      WHERE candidate_id = $1 AND status IN ('pending','coding')
      ORDER BY queued_at DESC LIMIT 1`,
    [spec.candidate_id || null]);
  let item;
  if (existing[0]) {
    item = existing[0];
  } else {
    const { rows } = await dbQuery(
      `INSERT INTO implementation_queue (candidate_id, strategy_spec, status)
       VALUES ($1, $2::jsonb, 'pending')
       RETURNING item_id, candidate_id, strategy_spec`,
      [spec.candidate_id || null, JSON.stringify(spec)]);
    item = rows[0];
  }

  // Forward the research-orchestrator phase callbacks into the approval-job
  // progress chip. We map orch phases to staging-approver phases:
  //   strategycoder → coding (60–70%)
  //   validate      → validating (70–80%)
  //   backtest      → backtesting (80–95%)
  //   promoting     → promoting (95–99%)
  const onPhase = (orchPhase, orchPct) => {
    const map = {
      strategycoder: { phase: 'coding',      base: 60, span: 10 },
      validate:      { phase: 'validating',  base: 70, span: 10 },
      backtest:      { phase: 'backtesting', base: 80, span: 15 },
      promoting:     { phase: 'promoting',   base: 95, span:  4 },
    }[orchPhase];
    if (!map) return;
    const orchSpan = orchPhase === 'backtest' ? 25 : 20;  // orch reports 60→85 over backtest
    const orchBase = { strategycoder: 20, validate: 40, backtest: 60, promoting: 90 }[orchPhase] || 0;
    const local    = Math.max(0, Math.min(1, (orchPct - orchBase) / orchSpan));
    const pct      = Math.round(map.base + local * map.span);
    updateJob(job.job_id, { phase: map.phase, progress: pct }).catch(() => {});
    emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id,
           status: 'running', phase: map.phase, progress: pct });
  };

  try {
    const orch = new ResearchOrchestrator();
    const onChild = (child) => {
      try { require('./index')._internals.registerChild(job.job_id, child); }
      catch (_) {}
    };
    const outcome = await orch._codeFromQueue(item, undefined, undefined, { onPhase, onChild });

    if (!outcome || !outcome.promoted) {
      await failJob(job, {
        reasonCode: outcome && outcome.reasonCode || 'unknown',
        backtest:   outcome && outcome.backtest_result || null,
        error:      outcome && outcome.error || null,
      });
      return;
    }

    // _codeFromQueue calls lifecycle.transition(target=CANDIDATE) inline, but
    // there's a known failure mode where the inline Python exits non-zero
    // (e.g. transition rejected because manifest still says 'paper' under
    // legacy state) and the JS catches that without aborting. Force the
    // transition here if the state isn't yet 'candidate'.
    let currentState = null;
    try {
      const m = JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8'));
      currentState = m.strategies[job.strategy_id] && m.strategies[job.strategy_id].state;
    } catch (_) {}
    if (currentState !== 'candidate') {
      try {
        await systemTransition(job.strategy_id, 'candidate', 'system:fused_staging_approval',
          `Auto-promote after fused approval (manifest was at ${currentState})`);
      } catch (e) {
        console.warn('[staging_approver] forced systemTransition failed:', e.message);
      }
    }

    await finishJob(job.job_id, 'succeeded', { backtest: outcome.backtest_result });
    emit({
      type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id,
      status: 'succeeded', phase: 'done', progress: 100,
      result: { backtest: outcome.backtest_result },
    });
  } catch (e) {
    await failJob(job, { error: e.message });
  }
}

// ── Entry points ────────────────────────────────────────────────────────────

async function start(job, ctx) {
  const { failJob } = ctx;
  try {
    const setup = await _phaseDataPipelineSetup(job, ctx);
    if (setup.phase === 'failed') return;

    // Refresh the in-memory job snapshot with the payload we just persisted,
    // so subsequent phases see the queue ids.
    job.payload = job.payload || {};
    if (setup.queueIds) job.payload.inserted_queue_ids = setup.queueIds;
    if (setup.missing)  job.payload.missing_sources    = setup.missing;

    if (setup.phase === 'backfilling') {
      const bf = await _phaseBackfilling(job, ctx);
      if (bf.phase === 'failed') return;
    }

    await _phaseCodeAndBacktest(job, ctx);
  } catch (e) {
    await failJob(job, { error: e.message });
  }
}

/**
 * Crash-recovery resumption. The approvals/index.js polling loop calls
 * resumeIfRunning(job) for every running approve_staging job after a process
 * restart. The job's `phase` column tells us where to pick up.
 */
async function resumeIfRunning(job, ctx) {
  const { failJob } = ctx;
  try {
    switch (job.phase) {
      case 'data_pipeline_setup':
      case null:
      case undefined:
        // start() never persisted past phase 0 — restart from the top.
        return start(job, ctx);
      case 'backfilling': {
        const bf = await _phaseBackfilling(job, ctx);
        if (bf.phase === 'failed') return;
        return _phaseCodeAndBacktest(job, ctx);
      }
      case 'coding':
      case 'validating':
      case 'backtesting':
      case 'promoting':
        // Re-enter the orchestrator. _codeFromQueue is internally idempotent
        // on implementation_queue rows (it reuses pending/coding rows) and
        // will re-run validate + backtest from scratch — acceptable on the
        // rare crash-recovery path.
        return _phaseCodeAndBacktest(job, ctx);
      default:
        return;
    }
  } catch (e) {
    await failJob(job, { error: `resume failed: ${e.message}` });
  }
}

module.exports = {
  start,
  resumeIfRunning,
  _internals: {
    readRequirements,
    readPlannedRequirements,
    loadEffectiveRequirements,
    readSchemaRegistry,
    sourcesMissingCoverage,
    sourceIsKnownToProvider,
    loadStrategySpec,
    _phaseDataPipelineSetup,
    _phaseBackfilling,
    _phaseCodeAndBacktest,
  },
};
