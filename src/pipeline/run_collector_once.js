'use strict';

/**
 * run_collector_once.js — standalone wrapper around collector.start().
 *
 * The daily orchestrator (src/execution/pipeline_orchestrator.py) invokes this
 * script as the `collect` step. The collector runs one cycle against the
 * master parquets + configured universe and exits 0 on clean completion,
 * non-zero on any phase error. No Discord hooks are wired up here — the
 * orchestrator itself handles the Discord phase-boundary posts for the new
 * 10am pipeline.
 */

require('dotenv').config({ path: require('path').join(__dirname, '../../.env') });

const collector = require('./collector');

async function main() {
  console.log('[collector-once] starting single-shot collection cycle');
  try {
    // Use runDailyCollection() directly: it's the one-cycle entry point
    // without the boot-time integrity / freshness checks / scheduler that
    // collector.start() wires up for the long-running johnbot process.
    await collector.runDailyCollection();
    console.log('[collector-once] cycle complete');
    process.exit(0);
  } catch (err) {
    console.error('[collector-once] cycle failed:', err && err.stack || err);
    process.exit(1);
  }
}

main();
