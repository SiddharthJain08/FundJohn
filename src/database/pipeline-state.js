'use strict';

/**
 * Pipeline state persistence — durable stage tracking in PostgreSQL.
 * Every stage transition is a single atomic UPDATE — fast, consistent.
 *
 * Stages: research → data-prep → validation → equity-analyst → compute →
 *         report-builder → complete | failed
 */

const { v4: uuidv4 } = require('uuid');
const { query } = require('./postgres');

/**
 * Start a new pipeline run — returns runId.
 */
async function startRun({ skillName, ticker, budgetMode = 'GREEN' }) {
  const runId = uuidv4();
  await query(
    `INSERT INTO pipeline_state (run_id, skill_name, ticker, current_stage, budget_mode)
     VALUES ($1, $2, $3, 'init', $4)`,
    [runId, skillName, ticker || null, budgetMode]
  );
  return runId;
}

/**
 * Advance to the next stage, storing the previous stage's output.
 * Single atomic UPDATE — safe under concurrent reads.
 */
async function advanceStage(runId, nextStage, completedStage = null, stageOutput = null) {
  if (completedStage && stageOutput !== null) {
    await query(
      `UPDATE pipeline_state
       SET current_stage = $2,
           stage_outputs = stage_outputs || jsonb_build_object($3, $4::jsonb),
           updated_at = NOW()
       WHERE run_id = $1`,
      [runId, nextStage, completedStage, JSON.stringify(stageOutput)]
    );
  } else {
    await query(
      `UPDATE pipeline_state SET current_stage = $2, updated_at = NOW() WHERE run_id = $1`,
      [runId, nextStage]
    );
  }
}

/**
 * Mark a run complete. Sets verdict_cache_written = true if verdictWritten.
 */
async function completeRun(runId, { verdictWritten = false } = {}) {
  await query(
    `UPDATE pipeline_state
     SET current_stage = 'complete', verdict_cache_written = $2, updated_at = NOW()
     WHERE run_id = $1`,
    [runId, verdictWritten]
  );
}

/**
 * Mark a run failed with optional error detail in stage_outputs.
 */
async function failRun(runId, errorMsg) {
  await query(
    `UPDATE pipeline_state
     SET current_stage = 'failed',
         stage_outputs = stage_outputs || jsonb_build_object('error', $2::jsonb),
         updated_at = NOW()
     WHERE run_id = $1`,
    [runId, JSON.stringify({ message: errorMsg, ts: new Date().toISOString() })]
  );
}

/**
 * Find all interrupted runs (not complete/failed/expired) at boot.
 * Returns array of { run_id, skill_name, ticker, current_stage, started_at, updated_at }
 */
async function findInterruptedRuns() {
  const res = await query(
    `SELECT run_id, skill_name, ticker, current_stage, started_at, updated_at
     FROM pipeline_state
     WHERE current_stage NOT IN ('complete', 'failed')
       AND expired_at IS NULL
       AND started_at > NOW() - INTERVAL '7 days'
     ORDER BY started_at DESC`
  );
  return res.rows;
}

/**
 * Expire runs older than 7 days — keeps table small, data preserved for audit.
 */
async function expireOldRuns() {
  const res = await query(
    `UPDATE pipeline_state
     SET expired_at = NOW()
     WHERE started_at < NOW() - INTERVAL '7 days'
       AND expired_at IS NULL`
  );
  if (res.rowCount > 0) {
    console.log(`[pipeline-state] Expired ${res.rowCount} run(s) older than 7 days`);
  }
}

/**
 * Get recent run history for a ticker (for /status).
 */
async function getRecentRuns(limit = 10) {
  const res = await query(
    `SELECT run_id, skill_name, ticker, current_stage, started_at, updated_at, verdict_cache_written
     FROM pipeline_state
     WHERE expired_at IS NULL
     ORDER BY started_at DESC LIMIT $1`,
    [limit]
  );
  return res.rows;
}

module.exports = { startRun, advanceStage, completeRun, failRun, findInterruptedRuns, expireOldRuns, getRecentRuns };
