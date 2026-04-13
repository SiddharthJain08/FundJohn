'use strict';

/**
 * data-task-executor.js
 *
 * Reads a queued data_tasks plan from PostgreSQL and executes the
 * appropriate collector functions. Called after the data-agent writes
 * its plan. Reports progress and results to #data-alerts only.
 */

const { Pool }    = require('pg');
const path        = require('path');

// Provider → collector function mapping
const DATASET_HANDLERS = {
  prices:       'runHistoricalPrices',
  options_eod:  'runOptions',
  financials:   'runFundamentals',
  macro:        null, // Alpha Vantage — not yet in collector, skip gracefully
  insider:      null, // SEC EDGAR — not yet in collector, skip gracefully
};

/**
 * Execute a queued data_task by task ID.
 *
 * @param {string} taskId - UUID of the data_tasks row
 * @param {Function} alertPost - Discord #data-alerts poster function
 * @returns {Promise<{collected: Array, unavailable: Array, rowsAdded: number}>}
 */
async function executeTask(taskId, alertPost) {
  const pool = new Pool({ connectionString: process.env.POSTGRES_URI });

  // Load the task plan
  const taskRes = await pool.query(
    `SELECT * FROM data_tasks WHERE id = $1`,
    [taskId]
  );
  if (taskRes.rows.length === 0) {
    throw new Error(`data_tasks row not found: ${taskId}`);
  }
  const task = taskRes.rows[0];

  if (!task.plan || !task.plan.datasets || task.plan.datasets.length === 0) {
    await pool.query(
      `UPDATE data_tasks SET status='failed', notes='No datasets in plan', completed_at=NOW() WHERE id=$1`,
      [taskId]
    );
    await pool.end();
    return { collected: [], unavailable: [{ reason: 'No datasets in plan' }], rowsAdded: 0 };
  }

  // Mark running
  await pool.query(
    `UPDATE data_tasks SET status='running', started_at=NOW() WHERE id=$1`,
    [taskId]
  );

  if (alertPost) {
    alertPost(`📡 **Data collection starting** — task \`${taskId.slice(0, 8)}\`\n${task.description}`);
  }

  // Load collector lazily to avoid circular deps
  const collector = require('./collector');

  const collected   = [];
  const unavailable = [...(task.plan.unavailable || [])];
  let totalRowsAdded = 0;

  // Execute each dataset in priority order
  const datasets = (task.plan.datasets || []).sort((a, b) => (a.priority || 2) - (b.priority || 2));

  for (const ds of datasets) {
    const handlerName = DATASET_HANDLERS[ds.name];

    if (handlerName === undefined) {
      unavailable.push({ dataset: ds.name, reason: 'Unknown dataset type' });
      continue;
    }

    if (handlerName === null) {
      unavailable.push({
        dataset: ds.name,
        tickers: ds.tickers,
        reason: `${ds.name} collection not yet implemented in collector — will populate in next scheduled cycle`,
      });
      continue;
    }

    const tickers = ds.tickers && ds.tickers.length > 0 ? ds.tickers : null;
    const lookback = ds.lookback_days || 365;

    if (alertPost) {
      const tickerStr = tickers ? tickers.slice(0, 5).join(', ') + (tickers.length > 5 ? `...+${tickers.length - 5}` : '') : 'universe';
      alertPost(`📊 **${ds.name}** — collecting ${tickers?.length || 'all'} tickers (${lookback}d lookback) | ${tickerStr}`);
    }

    try {
      let result;
      if (ds.name === 'prices') {
        result = await collector.runHistoricalPrices(lookback, tickers);
      } else if (ds.name === 'options_eod') {
        result = await collector.runOptions(tickers);
      } else if (ds.name === 'financials') {
        result = await collector.runFundamentals(tickers);
      }

      const rowsAdded = result?.rowsWritten || result?.rows_written || 0;
      totalRowsAdded += rowsAdded;

      collected.push({
        dataset:   ds.name,
        tickers:   tickers || ['universe'],
        rows_added: rowsAdded,
        provider:  ds.provider,
      });

      if (alertPost) {
        alertPost(`✅ **${ds.name}** complete — ${rowsAdded.toLocaleString()} rows added`);
      }
    } catch (err) {
      unavailable.push({
        dataset: ds.name,
        tickers: tickers,
        reason:  err.message.slice(0, 200),
      });
      if (alertPost) {
        alertPost(`⚠️ **${ds.name}** failed — ${err.message.slice(0, 100)}`);
      }
    }
  }

  // Incrementally sync master Parquets — only new rows appended
  try {
    const { execSync } = require('child_process');
    const syncOut = execSync(
      `python3 ${path.join(__dirname, '../../scripts/sync_master_parquets.py')}`,
      { env: { ...process.env }, timeout: 300_000, stdio: 'pipe' }
    ).toString().trim();
    if (alertPost) alertPost(`✅ Master Parquets synced:\n\`\`\`\n${syncOut}\n\`\`\``);
  } catch (err) {
    if (alertPost) alertPost(`⚠️ Parquet sync failed — ${err.message.slice(0, 100)}`);
  }

  // Mark complete
  const finalStatus = unavailable.length > 0 && collected.length === 0 ? 'failed'
    : unavailable.length > 0 ? 'partial'
    : 'complete';

  await pool.query(
    `UPDATE data_tasks
     SET status=$1, completed_at=NOW(), collected=$2, unavailable=$3, rows_added=$4
     WHERE id=$5`,
    [finalStatus, JSON.stringify(collected), JSON.stringify(unavailable), totalRowsAdded, taskId]
  );
  await pool.end();

  if (alertPost) {
    const lines = [
      `📋 **Data task complete** — \`${taskId.slice(0, 8)}\` | Status: **${finalStatus.toUpperCase()}**`,
      `Rows added: **${totalRowsAdded.toLocaleString()}** across ${collected.length} dataset(s)`,
    ];
    if (unavailable.length > 0) {
      lines.push(`\nCould not collect:`);
      for (const u of unavailable.slice(0, 5)) {
        lines.push(`  • ${u.dataset || u.request || '?'} — ${u.reason}`);
      }
    }
    alertPost(lines.join('\n'));
  }

  return { collected, unavailable, rowsAdded: totalRowsAdded };
}

module.exports = { executeTask };
