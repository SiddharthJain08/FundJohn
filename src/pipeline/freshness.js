'use strict';

/**
 * Single source of truth for dataset staleness.
 * Reads the `data_freshness` SQL view (migration 039) and formats a
 * startup warning for the Discord log + SSE stream.
 *
 * Non-blocking: surfaces problems, never aborts the pipeline.
 */

const { query: dbQuery } = require('../database/postgres');

const SEVERITY_RANK = { empty: 3, very_stale: 3, stale: 2, fresh: 1, current: 0 };

async function getDataFreshness() {
  const r = await dbQuery('SELECT * FROM data_freshness');
  return r.rows.map(row => ({
    dataset:       row.dataset,
    maxDate:       row.max_date,
    expectedDate:  row.expected_date,
    deltaDays:     row.delta_days,
    rowCount:      Number(row.row_count),
    status:        row.status,
  }));
}

/** Called at boot. Logs and returns the full freshness report. */
async function runFreshnessCheck({ warnThreshold = 'stale' } = {}) {
  let report;
  try {
    report = await getDataFreshness();
  } catch (err) {
    console.warn('[freshness] View read failed:', err.message);
    return { rows: [], alerts: [], skipped: true };
  }

  const warnRank = SEVERITY_RANK[warnThreshold] ?? 2;
  const alerts = report.filter(r => (SEVERITY_RANK[r.status] ?? 0) >= warnRank);

  if (alerts.length === 0) {
    console.log(`[freshness] All ${report.length} datasets current`);
  } else {
    console.error(`[DATA_ALERT] freshness: ${alerts.length} dataset(s) stale`);
    for (const a of alerts) {
      console.error(`  ${a.dataset.padEnd(14)} max=${a.maxDate?.toISOString?.().slice(0,10) ?? 'NULL'} expected=${a.expectedDate?.toISOString?.().slice(0,10)} Δ=${a.deltaDays}d [${a.status}]`);
    }
  }
  return { rows: report, alerts, skipped: false };
}

module.exports = { getDataFreshness, runFreshnessCheck };
