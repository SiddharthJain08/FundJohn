// Approval-job orchestration.
//
// Entry points:
//   approveStaging(sid, rec, actor)   → returns {status, body}
//   approveCandidate(sid, rec, actor) → returns {status, body}
//   approvePaper(sid, rec, actor, {force})
//     → returns {status, body}        (synchronous; reuses existing paper→live
//                                      gate check + manifest/registry write)
//
// The dispatch happens in server.js; this module owns job lifecycle
// (insert row → run worker → finish/fail) and exposes a poll tick that
// advances awaiting_snapshot staging jobs after each collector pass.

const fs   = require('fs');
const path = require('path');

const { query: dbQuery } = require('../../database/postgres');
const stagingApprover    = require('./staging_approver');
const candidateApprover  = require('./candidate_approver');

const OPENCLAW_DIR = path.resolve(__dirname, '..', '..', '..');
const MANIFEST_PATH = path.join(OPENCLAW_DIR, 'src', 'strategies', 'manifest.json');

// Injected by server.js at boot so we don't introduce a circular require.
let _broadcast = () => {};
function setBroadcast(fn) { _broadcast = fn; }

// In-memory map: job_id → currently-running child process. Populated by
// _codeFromQueue via onChild callback so cancelJob can SIGTERM it.
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

function readManifest() { return JSON.parse(fs.readFileSync(MANIFEST_PATH, 'utf8')); }
function writeManifest(m) {
  const tmp = MANIFEST_PATH + '.tmp';
  fs.writeFileSync(tmp, JSON.stringify(m, null, 2));
  fs.renameSync(tmp, MANIFEST_PATH);
}

// System-driven state transition that bypasses the candidate/staging dashboard
// guard. Writes manifest + lifecycle_events + strategy_registry.status.
async function systemTransition(sid, toState, actor, reason) {
  const manifest = readManifest();
  const rec = manifest.strategies[sid];
  if (!rec) throw new Error(`strategy ${sid} not in manifest`);
  const fromState = rec.state;
  const now = new Date().toISOString();
  rec.history = rec.history || [];
  rec.history.push({ from_state: fromState, to_state: toState, timestamp: now, actor, reason, metadata: {} });
  rec.state = toState;
  rec.state_since = now;
  manifest.updated_at = now;
  writeManifest(manifest);

  await dbQuery(
    `INSERT INTO lifecycle_events (strategy_id, from_state, to_state, actor, reason, metadata)
     VALUES ($1, $2, $3, $4, $5, $6::jsonb)`,
    [sid, fromState, toState, actor, reason, '{}'],
  ).catch(e => console.warn('[approvals] lifecycle_events insert failed:', e.message));

  const REGISTRY_STATUS_FOR = {
    live: 'approved', monitoring: 'approved',
    paper: 'pending_approval', candidate: 'pending_approval', staging: 'pending_approval',
    deprecated: 'deprecated', archived: 'deprecated',
  };
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

async function approveCandidate(sid, rec, actor) {
  const job = await insertJob({
    strategyId: sid, kind: 'approve_candidate', phase: 'strategycoder',
    payload: {}, actor,
  });
  emit({ type: 'approval_job', job_id: job.job_id, strategy_id: sid, status: 'running', phase: job.phase, progress: 0 });
  candidateApprover.start(job, ctx()).catch(e => failJob(job, { error: e.message }));
  return { status: 202, body: { job_id: job.job_id, phase: job.phase } };
}

// Shared context/helpers passed to the workers.
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

  // SIGTERM any running python subprocess for this job (candidate_approver
  // backtest/validate runs). Killing the child makes _codeFromQueue's
  // await resolve with a non-zero exit code; its catch branch then calls
  // failJob → the job is already marked cancelled here, so failJob's UPDATE
  // is a no-op thanks to the finished_at filter we add in finishJob.
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
// Returns the latest failed/cancelled job per strategy that the user hasn't
// dismissed yet. The dashboard populates its red banner from this so the
// alert persists across page reloads and server restarts.
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
  // Dismissing one failure means "clear this strategy's red banner until a
  // new failure lands". Cascade the dismissal to every earlier undismissed
  // failed/cancelled job for the same strategy, so the next rehydrate
  // doesn't surface an older row that was already superseded visually.
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

// ── Poll tick ────────────────────────────────────────────────────────────
// Runs every 60s. Staging approvals read as much as they need from
// data_coverage; candidate approvals do their own orchestration and don't
// need the tick.

let _pollTimer = null;
async function _pollOnce() {
  try {
    const { rows: jobs } = await dbQuery(
      `SELECT * FROM strategy_approval_jobs
        WHERE status='running' AND kind='approve_staging'`);
    for (const job of jobs) {
      try { await stagingApprover.tick(job, ctx()); }
      catch (e) { console.warn('[approvals] tick failed for', job.job_id, e.message); }
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

// Startup sweep: any job still 'running' from a previous process that
// doesn't match a live underlying queue row gets failed as interrupted.
async function reconcileOnStartup() {
  const { rows } = await dbQuery(
    `SELECT job_id, strategy_id, kind, payload, started_at
       FROM strategy_approval_jobs WHERE status='running'`);
  for (const job of rows) {
    // Staging jobs are self-healing (polling will resume). Only candidate jobs
    // need rescuing — their _codeFromQueue promise died with the old process.
    if (job.kind !== 'approve_candidate') continue;
    const ageMin = (Date.now() - new Date(job.started_at).getTime()) / 60_000;
    if (ageMin < 2) continue; // just-started; leave it
    await finishJob(job.job_id, 'failed', { error: 'restart_interrupted' });
    emit({ type: 'approval_job', job_id: job.job_id, strategy_id: job.strategy_id, status: 'failed', result: { error: 'restart_interrupted' } });
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
  approveCandidate,
  cancelJob,
  hasActiveJob,
  listActive,
  listRecentFailures,
  dismissFailure,
  // exposed for tests + inter-module use
  _internals: { insertJob, updateJob, finishJob, failJob, systemTransition, startPolling, stopPolling, _pollOnce, readManifest, writeManifest, registerChild, killChild },
};
