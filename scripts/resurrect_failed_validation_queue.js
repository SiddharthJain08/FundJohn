#!/usr/bin/env node
'use strict';

/**
 * resurrect_failed_validation_queue.js — one-shot recovery for
 * implementation_queue rows quarantined as `validation_failed` due to the
 * 2026-05-29 strategycoder canonical_file case-drift bug.
 *
 * The fix in commit 7708d1b repaired the manifest entries so _resolveImplPath
 * now points at the actual .py files. This script re-invokes the orchestrator's
 * `_codeFromQueue` for each affected candidate. Since the file already exists
 * on disk, strategycoder is SKIPPED (`skipCoding=true`) and the run proceeds
 * directly to validate → unified_backtest → auto-promote, mirroring the
 * canonical Saturday-brain flow.
 *
 * Usage:
 *   node scripts/resurrect_failed_validation_queue.js [--dry-run]
 *
 * Sequential to avoid prices.parquet/unified_backtest resource contention.
 */

require('dotenv').config({ path: require('path').join(__dirname, '../.env') });
const { Pool } = require('pg');
const path = require('path');
const ResearchOrchestrator = require('../src/agent/research/research-orchestrator');

const DRY_RUN = process.argv.includes('--dry-run');

async function main() {
  const pool = new Pool({ connectionString: process.env.POSTGRES_URI });
  const { rows } = await pool.query(`
    SELECT candidate_id, strategy_spec
      FROM implementation_queue
     WHERE status = 'validation_failed'
       AND error_log ILIKE '%File not found%'
     ORDER BY queued_at ASC
  `);
  console.log(`[resurrect] Found ${rows.length} stuck candidates`);
  if (rows.length === 0) { await pool.end(); return; }

  const orch = new ResearchOrchestrator();
  const results = { promoted: 0, code_only: 0, backtest_error: 0, validate_failed: 0, exception: 0 };

  for (const row of rows) {
    const spec    = row.strategy_spec || {};
    const stratId = spec.strategy_id || row.candidate_id.slice(0, 8);
    const cidShort = row.candidate_id.slice(0, 8);
    console.log(`\n=== [${cidShort}] Resurrecting ${stratId} ===`);

    if (DRY_RUN) {
      console.log(`  (dry-run) would call _codeFromQueue for ${stratId}`);
      continue;
    }

    // Flip status back to 'pending' + clear stale error_log so the orchestrator
    // records new state cleanly.
    await pool.query(
      `UPDATE implementation_queue SET status='pending', error_log=NULL WHERE candidate_id=$1`,
      [row.candidate_id]
    );

    const item = { candidate_id: row.candidate_id, strategy_spec: spec };
    try {
      const r = await orch._codeFromQueue(
        item,
        (msg) => console.log(`  ${msg}`),
        () => {},  // channelNotify — silent (we have stdout)
        { onPhase: (p, n) => process.stdout.write(`[${p}:${n}%] `) }
      );
      console.log('');  // close the inline phase line
      if (r?.promoted) { results.promoted++; }
      else if (r?.reasonCode === 'backtest_error') { results.backtest_error++; }
      else if (r?.reasonCode === 'contract_violation') { results.validate_failed++; }
      else { results.code_only++; }
      console.log(`  → result:`, JSON.stringify(r, null, 2).slice(0, 600));
    } catch (e) {
      console.error(`  → exception:`, e.message);
      results.exception++;
    }
  }

  console.log(`\n=== Summary ===`);
  for (const [k, v] of Object.entries(results)) console.log(`  ${k}: ${v}`);
  await pool.end();
}

main().catch((e) => { console.error('[resurrect] fatal:', e); process.exit(1); });
