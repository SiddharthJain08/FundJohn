'use strict';

/**
 * Single source of truth for dataset staleness.
 *
 * Parquet-primary: replaces the legacy `data_freshness` SQL VIEW (migration 039)
 * with a Python-side max(date) scan across each master parquet. Same output
 * shape so the dashboard / SSE stream stay byte-compatible.
 *
 * Non-blocking: surfaces problems, never aborts the pipeline.
 */

const { readParquet } = require('../data/parquet_store');
const { query: dbQuery } = require('../database/postgres');

const SEVERITY_RANK = { empty: 3, very_stale: 3, stale: 2, fresh: 1, current: 0 };

function _lastTradingDay() {
  const d   = new Date();
  const dow = d.getUTCDay();   // 0=Sun, 6=Sat
  const back = dow === 0 ? 2 : dow === 6 ? 1 : dow === 1 ? 3 : 1;
  d.setUTCDate(d.getUTCDate() - back);
  return d.toISOString().slice(0, 10);
}

function _status(maxDate, expected) {
  if (!maxDate) return { status: 'empty', deltaDays: null };
  const a = new Date(maxDate).getTime();
  const b = new Date(expected).getTime();
  const delta = Math.round((b - a) / 86400000);
  let status;
  if (delta <= 0)      status = 'current';
  else if (delta === 1) status = 'fresh';
  else if (delta <= 3)  status = 'stale';
  else                  status = 'very_stale';
  return { status, deltaDays: delta };
}

async function _dbFreshness(dataset, sql) {
  try {
    const r = await dbQuery(sql);
    const row = r.rows[0] || {};
    return {
      dataset,
      maxDate:  row.max_date ? new Date(row.max_date).toISOString().slice(0, 10) : null,
      rowCount: Number(row.row_count) || 0,
    };
  } catch (_e) {
    return { dataset, maxDate: null, rowCount: 0 };
  }
}

async function getDataFreshness() {
  const expected = _lastTradingDay();

  const [parquetRows, news, pnl] = await Promise.all([
    readParquet('freshness', {}),
    _dbFreshness('market_news', `SELECT MAX(published_at)::date AS max_date, COUNT(*) AS row_count FROM market_news`),
    _dbFreshness('signal_pnl',  `SELECT MAX(pnl_date)::date   AS max_date, COUNT(*) AS row_count FROM signal_pnl`),
  ]);

  // Parquet-backed rows already carry expected/delta/status in the CLI shape.
  const out = (parquetRows || []).map(r => ({
    dataset:      r.dataset,
    maxDate:      r.max_date,
    expectedDate: r.expected_date,
    deltaDays:    r.delta_days,
    rowCount:     Number(r.row_count) || 0,
    status:       r.status,
  }));

  // Operational DB tables retain their own freshness rows.
  for (const dbRow of [news, pnl]) {
    const { status, deltaDays } = _status(dbRow.maxDate, expected);
    out.push({
      dataset:      dbRow.dataset,
      maxDate:      dbRow.maxDate,
      expectedDate: expected,
      deltaDays,
      rowCount:     dbRow.rowCount,
      status,
    });
  }
  // Sort: most stale first (matches legacy VIEW), then by dataset name.
  out.sort((a, b) => ((b.deltaDays ?? -1) - (a.deltaDays ?? -1)) || a.dataset.localeCompare(b.dataset));
  return out;
}

/** Called at boot. Logs and returns the full freshness report. */
async function runFreshnessCheck({ warnThreshold = 'stale' } = {}) {
  let report;
  try {
    report = await getDataFreshness();
  } catch (err) {
    console.warn('[freshness] parquet read failed:', err.message);
    return { rows: [], alerts: [], skipped: true };
  }

  const warnRank = SEVERITY_RANK[warnThreshold] ?? 2;
  const alerts = report.filter(r => (SEVERITY_RANK[r.status] ?? 0) >= warnRank);

  if (alerts.length === 0) {
    console.log(`[freshness] All ${report.length} datasets current`);
  } else {
    console.error(`[DATA_ALERT] freshness: ${alerts.length} dataset(s) stale`);
    for (const a of alerts) {
      const md  = typeof a.maxDate === 'string' ? a.maxDate.slice(0, 10) : (a.maxDate ? String(a.maxDate).slice(0, 10) : 'NULL');
      const exp = typeof a.expectedDate === 'string' ? a.expectedDate.slice(0, 10) : String(a.expectedDate || '').slice(0, 10);
      console.error(`  ${a.dataset.padEnd(14)} max=${md} expected=${exp} Δ=${a.deltaDays}d [${a.status}]`);
    }
  }
  return { rows: report, alerts, skipped: false };
}

module.exports = { getDataFreshness, runFreshnessCheck };
