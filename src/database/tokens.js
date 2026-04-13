'use strict';

/**
 * Token usage and cost tracking.
 *
 * Captures actual cost_usd from claude-bin JSON output per subagent run.
 * Uses historical actuals to estimate cost before a task starts.
 *
 * Claude pricing (per-million tokens, as of 2026):
 *   Sonnet 4.6: $3 input / $15 output
 *   Opus 4.6:   $15 input / $75 output
 *   Haiku 4.5:  $0.25 input / $1.25 output
 */

const { query } = require('./postgres');

// ── Task lifecycle ────────────────────────────────────────────────────────────

async function startTask(taskId, taskType, ticker, estCostUsd = null) {
  await query(
    `INSERT INTO task_costs (task_id, task_type, ticker, status, est_cost_usd)
     VALUES ($1, $2, $3, 'running', $4)
     ON CONFLICT (task_id) DO NOTHING`,
    [taskId, taskType, ticker, estCostUsd]
  ).catch(() => null);
}

async function recordSubagent(taskId, subagentId, subagentType, ticker, model, costUsd, durationMs, numTurns) {
  await query(
    `INSERT INTO subagent_costs (task_id, subagent_id, subagent_type, ticker, model, cost_usd, duration_ms, num_turns)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8)`,
    [taskId, subagentId, subagentType, ticker, model, costUsd ?? null, durationMs, numTurns ?? null]
  ).catch(() => null);

  // Roll up to task total
  await query(
    `UPDATE task_costs
     SET cost_usd = cost_usd + $2,
         num_subagents = num_subagents + 1
     WHERE task_id = $1`,
    [taskId, costUsd ?? 0]
  ).catch(() => null);
}

async function completeTask(taskId, status = 'complete') {
  await query(
    `UPDATE task_costs
     SET status = $2, completed_at = NOW(),
         duration_ms = EXTRACT(EPOCH FROM (NOW() - started_at)) * 1000
     WHERE task_id = $1`,
    [taskId, status]
  ).catch(() => null);
}

// ── Estimation ────────────────────────────────────────────────────────────────

async function estimateCost(taskType, ticker = null) {
  // Use last 30 completed tasks of same type as the baseline
  const res = await query(
    `SELECT
       COUNT(*)                             AS samples,
       AVG(cost_usd)                        AS mean_cost,
       STDDEV(cost_usd)                     AS std_cost,
       MIN(cost_usd)                        AS min_cost,
       MAX(cost_usd)                        AS max_cost,
       AVG(num_subagents)                   AS avg_subagents,
       AVG(duration_ms) / 1000.0            AS avg_duration_s
     FROM task_costs
     WHERE task_type = $1
       AND status = 'complete'
       AND cost_usd > 0
     ORDER BY completed_at DESC
     LIMIT 30`,
    [taskType]
  ).catch(() => null);

  const row = res?.rows?.[0];
  if (!row || Number(row.samples) < 2) {
    return { estimated: null, samples: Number(row?.samples ?? 0), reason: 'insufficient history' };
  }

  const mean = Number(row.mean_cost);
  const std  = Number(row.std_cost) || mean * 0.3; // fallback: 30% std if only 1 sample
  return {
    estimated:    mean,
    low:          Math.max(0, mean - std),
    high:         mean + std,
    samples:      Number(row.samples),
    avgSubagents: Number(row.avg_subagents).toFixed(1),
    avgDurationS: Math.round(Number(row.avg_duration_s)),
    reason:       'historical average',
  };
}

// ── Queries ───────────────────────────────────────────────────────────────────

async function getTaskCost(taskId) {
  const res = await query(
    `SELECT tc.*,
       json_agg(json_build_object(
         'type', sc.subagent_type, 'model', sc.model,
         'cost', sc.cost_usd, 'duration_ms', sc.duration_ms, 'turns', sc.num_turns
       ) ORDER BY sc.created_at) AS subagents
     FROM task_costs tc
     LEFT JOIN subagent_costs sc ON sc.task_id = tc.task_id
     WHERE tc.task_id = $1
     GROUP BY tc.id`,
    [taskId]
  ).catch(() => null);
  return res?.rows?.[0] || null;
}

async function getSpendSummary(days = 7) {
  const res = await query(
    `SELECT
       task_type,
       COUNT(*)                  AS runs,
       SUM(cost_usd)             AS total_cost,
       AVG(cost_usd)             AS avg_cost,
       MAX(cost_usd)             AS max_cost
     FROM task_costs
     WHERE completed_at >= NOW() - ($1 || ' days')::INTERVAL
       AND status = 'complete'
     GROUP BY task_type
     ORDER BY total_cost DESC`,
    [days]
  ).catch(() => null);
  return res?.rows || [];
}

async function getTotalSpend(days = 30) {
  const res = await query(
    `SELECT
       COALESCE(SUM(cost_usd), 0)        AS total_usd,
       COUNT(*)                           AS total_runs,
       MIN(started_at)                    AS first_run,
       SUM(CASE WHEN started_at >= NOW() - INTERVAL '1 day'  THEN cost_usd ELSE 0 END) AS today_usd,
       SUM(CASE WHEN started_at >= NOW() - INTERVAL '7 days' THEN cost_usd ELSE 0 END) AS week_usd
     FROM task_costs
     WHERE completed_at >= NOW() - ($1 || ' days')::INTERVAL`,
    [days]
  ).catch(() => null);
  return res?.rows?.[0] || { total_usd: 0, total_runs: 0 };
}

module.exports = { startTask, recordSubagent, completeTask, estimateCost, getTaskCost, getSpendSummary, getTotalSpend };
