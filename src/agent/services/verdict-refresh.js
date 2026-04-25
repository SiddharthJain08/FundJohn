'use strict';

/**
 * Async verdict refresh.
 *
 * Background worker that scans verdict_cache for stale rows, dedup-locks them,
 * and re-runs them via the Anthropic Batch API (50% cheaper than synchronous).
 *
 * Design:
 *   - Stale-after window: rows where stale_after < NOW() AND stale_after >
 *     NOW() - GRACE_HOURS. Outside the grace window, the verdict is too old
 *     to refresh as-is; it should be re-run by an operator with fresh
 *     parameters.
 *   - Dedup lock: Redis SETNX `verdict_refresh_lock:{ticker}:{type}` TTL 1h.
 *     Prevents two refresh workers from picking up the same row.
 *   - Race with operator-triggered runs: not actively prevented. If both
 *     run, the most recent finished verdict wins via ON CONFLICT in
 *     verdict_cache. Acceptable for v1 — refreshes are background and the
 *     batch path is hours-slow vs. seconds for operator runs.
 *   - Per-cycle namespace: each refresh wave gets a `verdict-refresh-{ts}`
 *     CYCLE_ID so all parallel refreshes share Python cycle-cache.
 *
 * Not done in v1:
 *   - Quality regression check (does the new verdict materially differ?). Just
 *     replaces blindly. Add when we have a baseline of refresh stability.
 *   - Per-ticker refresh frequency cap. Add when budget visibility matures.
 */

const { query } = require('../../database/postgres');
const { getClient } = require('../../database/redis');
const swarm = require('../subagents/swarm');

const GRACE_HOURS = parseInt(process.env.VERDICT_REFRESH_GRACE_HOURS || '72', 10);
const LOCK_TTL_S  = parseInt(process.env.VERDICT_REFRESH_LOCK_TTL_S  || '3600', 10);
const DEFAULT_BATCH_SIZE = parseInt(process.env.VERDICT_REFRESH_BATCH_SIZE || '5', 10);

/**
 * Fetch up to `limit` stale verdict rows that haven't been picked up by
 * another refresh worker. Lock acquired in same call to prevent re-pick.
 */
async function fetchAndLockStale(limit = DEFAULT_BATCH_SIZE) {
  const { rows } = await query(
    `SELECT ticker, analysis_type, analysis_date, stale_after, verdict
       FROM verdict_cache
      WHERE stale_after < NOW()
        AND stale_after > NOW() - ($1 || ' hours')::interval
      ORDER BY stale_after ASC
      LIMIT $2`,
    [String(GRACE_HOURS), limit * 3],  // overfetch since some will be locked
  );

  const r = getClient();
  const claimed = [];
  for (const row of rows) {
    const lockKey = `verdict_refresh_lock:${row.ticker}:${row.analysis_type}`;
    const ok = await r.set(lockKey, '1', 'EX', LOCK_TTL_S, 'NX');
    if (ok === 'OK') {
      claimed.push(row);
      if (claimed.length >= limit) break;
    }
  }
  return claimed;
}

/**
 * Refresh a single verdict via batch API. Returns refresh result.
 * Picks the right subagent type from the analysis_type:
 *   - 'diligence' → researchjohn (paper / batch eligible)
 *   - other types → researchjohn fallback (extend as new analysis types added)
 */
async function refreshOne(row, { batchId, notify }) {
  const taskMsg = `Refresh stale ${row.analysis_type} verdict for ${row.ticker}. Prior verdict was "${row.verdict}" as of ${row.analysis_date}; re-run with current data and emit a fresh memo.`;

  // researchjohn is the existing batch-eligible type per src/budget/batch.js
  // (BATCH_ELIGIBLE_TYPES.has('research') — name-mismatch is on us; researchjohn
  //  is the agent-type alias). Map carefully here.
  const type = 'researchjohn';

  return swarm.init({
    type,
    ticker:    row.ticker,
    workspace: process.env.OPENCLAW_DIR || '/root/openclaw',
    threadId:  batchId,   // shared CYCLE_ID across the wave
    notify:    notify || (() => {}),
    mode:      'PM_TASK',
    prompt:    taskMsg,
    useBatch:  true,
  });
}

async function releaseLock(ticker, analysisType) {
  const lockKey = `verdict_refresh_lock:${ticker}:${analysisType}`;
  try {
    await getClient().del(lockKey);
  } catch (err) {
    console.warn('[verdict-refresh] release lock failed:', err.message);
  }
}

/**
 * Run one refresh wave. Returns { picked, succeeded, failed }.
 */
async function refreshWave({ batchSize = DEFAULT_BATCH_SIZE, notify } = {}) {
  const claimed = await fetchAndLockStale(batchSize);
  if (!claimed.length) {
    return { picked: 0, succeeded: 0, failed: 0, batchId: null };
  }

  const batchId = `verdict-refresh-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
  console.log(`[verdict-refresh] wave ${batchId} — refreshing ${claimed.length} stale verdicts`);
  if (notify) {
    notify(`🔁 Verdict refresh wave: ${claimed.length} stale rows (batch=${batchId})`).catch(() => {});
  }

  const results = await Promise.allSettled(
    claimed.map((row) => refreshOne(row, { batchId, notify: () => {} })),
  );

  // Always release locks (even on failure) so the next wave can retry
  await Promise.all(claimed.map((row) => releaseLock(row.ticker, row.analysis_type)));

  const succeeded = results.filter((r) => r.status === 'fulfilled').length;
  const failed    = results.length - succeeded;

  if (notify) {
    notify(`✅ Verdict refresh wave done: ${succeeded}/${claimed.length} succeeded (batch=${batchId})`).catch(() => {});
  }
  return { picked: claimed.length, succeeded, failed, batchId };
}

module.exports = { fetchAndLockStale, refreshOne, refreshWave, releaseLock };
