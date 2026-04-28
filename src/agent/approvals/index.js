// Approval-job orchestration.
//
// Entry points:
//   approveStaging(sid, rec, actor) → returns {status, body}
//
// The dispatch happens in server.js; this module owns job lifecycle (insert
// row → run worker → finish/fail) and exposes a poll tick that resumes
// long-running approve_staging jobs across process restarts.
//
// Under the fused-approval lifecycle (2026-04-27), there is only ONE
// asynchronous job kind: approve_staging. The worker runs the entire
// pipeline (backfill → strategycoder → validate → backtest → manifest
// transition staging→candidate) in-process. The remaining operator gate
// (candidate→live) uses the synchronous /api/strategies/:id/transition
// endpoint, where the sharpe ≥ 0.5 / max_dd ≤ 20% guard now lives.

const fs   = require('fs');
const path = require('path');

const { query: dbQuery } = require('../../database/postgres');
const stagingApprover    = require('./staging_approver');

const OPENCLAW_DIR = path.resolve(__dirname, '..', '..', '..');
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src', 'strategies', 'manifest.json');

// Injected by server.js at boot so we don't introduce a circular require.
let _broadcast = () => {};
function setBroadcast(fn) { _broadcast = fn; }

// In-memory map: job_id → currently-running child process. Populated by
// _codeFromQueue / runBackfill via onChild callbacks so cancelJob can SIGTERM.
const _jobChildren = new Map();
function registerChild(jobId, child) {
  _jobChildren.set(jobId, child);
  child.on('exit', () => { if (_jobChildren.get(jobId) === child) _jobChildren.delete(jobId); });
}
function killChild(jobId) {
  const c = _jobChildren.get(jobId);
  if (!c) return false;
  try { c.kill('SIGTERM'); } catch (_) {}
  setTimeout(() => { try { c.kill('SIGKILL'); } catch (_) {} }, 3_000);
  _jobChildren.delete(jobId);
  return true;
}

// ── Shared helpers exposed to sub-approvers ───────────────────────────────

async function insertJob({ strategyId, kind, phase, payload, actor }) {
  const { rows } = await dbQuery(
    `INSERT INTO strategy_approval_jobs
       (strategy_id, kind, status, phase, progress, payload, actor)
     VALUES ($1, $2, 'running', $3, 0, $4, $5)
     RETURNING job_id, strategy_id, kind, status, phase, progress, payload, actor, started_at`,
    [strategyId, kind, phase, JSON.stringify(payload || {}), actor],
  );
  return rows[0];
}

async function updateJob(jobId, { phase, progress, payload }) {
  const sets = [];
  const params = [jobId];
  if (phase !== undefined)    { params.push(phase);    sets.push(`phase      = $${params.length}`); }
  if (progress !== undefined) { params.push(progress); sets.push(`progress   = $${params.length}`); }
  if (payload !== undefined)  { params.push(JSON.stringify(payload)); sets.push(`payload = $${params.length}::jsonb`); }
  if (!sets.length) return;
  await dbQuery(`UPDATE strategy_approval_jobs SET ${sets.join(', ')} WHERE job_id = $1`, params);
}

async function finishJob(jobId, status, result) {
  // Only finalize if the job is still in-flight. Guards the race where
  // cancelJob() marks cancelled and the child-process exit handler then
  // tries to mark failed — the second UPDATE becomes a no-op.
  await dbQuery(
    `UPDATE strategy_approval_jobs
        SET status = $2, result = $3::jsonb, finished_at = NOW(), progress = 100
      WHERE job_id = $1 AND status IN ('pending','running')`,
    [jobId, status, JSON.stringify(result || {})],
  );
  _jobChildren.delete(jobId);
}

async function failJob(job, result) {
  await finishJob(job.job_id, 'failed', result);
  _broadcast({
    type:        'approval_job',
    job_id:      job.job_id,
    strategy_id: job.strategy_id,
    status:      'failed',
    result,
  });
}

function emit(event) { _broadcast(event); }

const { withManifestLock } = require('../../lib/manifest_lock');

function readManifest() { return JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8')); }
function writeManifest(m) {
  // Plain atomic write — kept for back-compat with callers that already
  // hold the lock externally. New code should prefer withManifestLock().
  const tmp = MANIFEST_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(m, null, 2));
  fs.renameSync(tmp, MANIFEST_PATH);
}

// Lifecycle state → strategy_registry.status. Locked values per the fused-
// approval rewrite plan:
//   live, monitoring         → 'approved' (execution gate)
//   candidate, staging       → 'pending_approval' (awaiting operator)
//   deprecated, archived     → 'deprecated'
// `paper` is intentionally absent — legacy state, no new transitions land here.
const REGISTRY_STATUS_FOR = {
  live:       'approved',
  monitoring: 'approved',
  candidate:  'pending_approval',
  staging:    'pending_approval',
  deprecated: 'deprecated',
  archived:   'deprecated',
};

// System-driven state transition that bypasses the dashboard guard. Writes
// manifest + lifecycle_events + strategy_registry.status under the cross-
// process manifest lock so concurrent writers cannot lose updates.
async function systemTransition(sid, toState, actor, reason) {
  let fromState, now;
  await withManifestLock(MANIFEST_PATH, (manifest) => {
    const rec = manifest.strategies[sid];
    if (!rec) throw new Error(`strategy ${sid} not in manifest`);
    fromState = rec.state;
    now = new Date().toISOString();
    rec.history = rec.history || [];
    rec.history.push({ from_state: fromState, to_state: toState, timestamp: now, actor, reason, metadata: {} });
    rec.state = toState;
    rec.state_since = now;
    manifest.updated_at = now;
    return manifest;
  }, { actor: 'approvals.systemTransition' });

  await dbQuery(
    `INSERT INTO lifecycle_events (strategy_id, from_state, to_state, actor, reason, metadata)
     VALUES ($1, $2, $3, $4, $5, $6::jsonb)`,
    [sid, fromState, toState, actor, reason, '{}'],
  ).catch(e => console.warn('[approvals] lifecycle_events insert failed:', e.message));

  const target = REGISTRY_STATUS_FOR[toState];
  if (target) {
    const sql = target === 'approved'
      ? `UPDATE strategy_registry
            SET status=$2,
                approved_by=COALESCE(approved_by,$3),
                approved_at=COALESCE(approved_at, NOW())
          WHERE id=$1`
      : `UPDATE strategy_registry SET status=$2 WHERE id=$1`;
    const params = target === 'approved' ? [sid, target, actor] : [sid, target];
    await dbQuery(sql, params).catch(e => console.warn('[approvals] status sync failed:', e.message));
  }

  _broadcast({ type: 'strategy_transition', strategy_id: sid, from_state: fromState, to_state: toState, at: now });
  return { fromState, toState, at: now };
}

// ── Dispatch ──────────────────────────────────────────────────────────────

async function hasActiveJob(sid) {
  const { rows } = await dbQuery(
    `SELECT job_id FROM strategy_approval_jobs
      WHERE strategy_id=$1 AND status IN ('pending','running')`, [sid]);
  return rows[0] || null;
}

async function approveStaging(sid, rec, actor) {
  const job = await insertJob({
    strategyId: sid, kind: 'approve_staging', phase: 'data_pipeline_setup',
    payload: {}, actor,
  });
  emit({ type: 'approval_job', job_id: job.job_id, strategy_id: sid, status: 'running', phase: job.phase, progress: 0 });
  // Fire-and-forget. Errors inside start() are caught there and set job=failed.
  stagingApprover.start(job, ctx()).catch(e => failJob(job, { error: e.message }));
  return { status: 202, body: { job_id: job.job_id, phase: job.phase } };
}

// Shared context/helpers passed to the worker.
function ctx() {
  return {
    dbQuery,
    emit,
    updateJob,
    finishJob,
    failJob,
    systemTransition,
    openclawDir: OPENCLAW_DIR,
  };
}

// ── Cancel ────────────────────────────────────────────────────────────────

async function cancelJob(sid) {
  const active = await hasActiveJob(sid);
  if (!active) return { status: 404, body: { error: 'no active job' } };

  const { rows } = await dbQuery(`SELECT * FROM strategy_approval_jobs WHERE job_id=$1`, [active.job_id]);
  const job = rows[0];

  // SIGTERM any running python subprocess for this job (backfill, validate,
  // backtest). Killing the child makes the awaited spawn resolve with a
  // non-zero exit; the worker's catch branch then calls failJob, which is a
  // no-op once we've already marked cancelled here.
  const killed = killChild(job.job_id);

  const insertedIds = (job.payload && job.payload.inserted_queue_ids) || [];
  if (insertedIds.length) {
    await dbQuery(
      `DELETE FROM data_ingestion_queue WHERE request_id = ANY($1::uuid[])`,
      [insertedIds],
    ).catch(() => {});
  }
  await finishJob(job.job_id, 'cancelled', {
    cancelled_at: new Date().toISOString(),
    child_killed: killed,
  });
  emit({ type: 'approval_job', job_id: job.job_id, strategy_id: sid, status: 'cancelled' });
  return { status: 200, body: { ok: true, job_id: job.job_id, child_killed: killed } };
}

// ── Recent failures (dashboard hydration, survives restart) ─────────────

async function listRecentFailures(limitDays = 30) {
  const { rows } = await dbQuery(
    `SELECT DISTINCT ON (strategy_id)
            job_id, strategy_id, kind, status, result, finished_at
       FROM strategy_approval_jobs
      WHERE status IN ('failed','cancelled')
        AND finished_at > NOW() - ($1 * INTERVAL '1 day')
        AND (result IS NULL OR result->>'dismissed_at' IS NULL)
      ORDER BY strategy_id, finished_at DESC`,
    [limitDays]);
  return rows;
}

async function dismissFailure(jobId) {
  const { rows } = await dbQuery(
    `WITH target AS (SELECT strategy_id FROM strategy_approval_jobs WHERE job_id = $1)
     UPDATE strategy_approval_jobs
        SET result = COALESCE(result, '{}'::jsonb)
                     || jsonb_build_object('dismissed_at', to_jsonb(NOW()::text))
      WHERE strategy_id IN (SELECT strategy_id FROM target)
        AND status IN ('failed','cancelled')
        AND (result IS NULL OR result->>'dismissed_at' IS NULL)
      RETURNING job_id, strategy_id`,
    [jobId]);
  return rows[0] || null;
}

// ── Active jobs list (dashboard hydration) ────────────────────────────────

async function listActive() {
  const { rows } = await dbQuery(
    `SELECT job_id, strategy_id, kind, status, phase, progress, payload, started_at
       FROM strategy_approval_jobs
      WHERE status IN ('pending','running')
      ORDER BY started_at DESC`);
  return rows;
}

// ── Crash-recovery poll tick ─────────────────────────────────────────────
// Runs every 60s. Looks for approve_staging jobs that are still 'running'
// (likely because the previous process crashed) and asks the worker to
// resume from the last-persisted phase. Active jobs in this process drive
// themselves via the in-memory state machine and are no-ops here.

let _pollTimer = null;
const _resumed = new Set();
async function _pollOnce() {
  try {
    const { rows: jobs } = await dbQuery(
      `SELECT * FROM strategy_approval_jobs
        WHERE status='running' AND kind='approve_staging'`);
    for (const job of jobs) {
      if (_resumed.has(job.job_id)) continue;   // already kicked
      // Skip just-started jobs; the in-process start() is still driving them.
      const ageMs = Date.now() - new Date(job.started_at).getTime();
      if (ageMs < 90_000) continue;
      _resumed.add(job.job_id);
      stagingApprover.resumeIfRunning(job, ctx())
        .catch(e => console.warn('[approvals] resume failed for', job.job_id, e.message));
    }
  } catch (e) {
    console.warn('[approvals] poll failed:', e.message);
  }
}

function startPolling(intervalMs = 60_000) {
  if (_pollTimer) return;
  _pollTimer = setInterval(_pollOnce, intervalMs);
  _pollTimer.unref?.();
  // Kick one immediate pass so a restart resumes quickly.
  setImmediate(_pollOnce);
}
function stopPolling() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = null;
}

// Startup sweep: clear legacy jobs that the fused-approval rewrite
// (2026-04-27) no longer drives. Two flavours:
//   1. kind='approve_candidate'                        — worker deleted
//   2. kind='approve_staging' AND phase='awaiting_snapshot' — old polled phase
// Fresh jobs use the new state machine and resume via _pollOnce above.
async function reconcileOnStartup() {
  const { rows } = await dbQuery(
    `SELECT job_id, strategy_id, kind, phase, started_at
       FROM strategy_approval_jobs
      WHERE status='running'
        AND ( kind='approve_candidate'
              OR (kind='approve_staging' AND phase='awaiting_snapshot') )`);
  for (const job of rows) {
    const err = job.kind === 'approve_candidate'
      ? 'approve_candidate flow removed (fused approval rewrite)'
      : 'awaiting_snapshot phase removed (fused approval rewrite — re-approve to use the new fused worker)';
    await finishJob(job.job_id, 'failed', { error: err });
    emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id, status: 'failed', result: { error: err } });
  }
}

function init({ broadcast }) {
  if (broadcast) setBroadcast(broadcast);
  reconcileOnStartup().catch(e => console.warn('[approvals] reconcileOnStartup:', e.message));
  startPolling();
}

module.exports = {
  init,
  setBroadcast,
  approveStaging,
  cancelJob,
  hasActiveJob,
  listActive,
  listRecentFailures,
  dismissFailure,
  // exposed for tests + inter-module use
  _internals: {
    insertJob, updateJob, finishJob, failJob, systemTransition,
    startPolling, stopPolling, _pollOnce,
    readManifest, writeManifest,
    registerChild, killChild, ctx,
    REGISTRY_STATUS_FOR,
  },
};
