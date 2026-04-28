#!/usr/bin/env node
'use strict';

/**
 * saturday_brain_retry_failed.js — re-fan paperhunter on candidates whose
 * previous extraction emitted rejection_reason_if_any='fetch_failed'.
 *
 * Used after fixing the paperhunter prompt + abstract injection so the
 * 52 candidates that failed yesterday on paywalled DOIs can extract from
 * their abstract instead. Then chains into the finisher to tier + code +
 * stage the newly-extracted specs.
 *
 * Usage: node src/agent/curators/saturday_brain_retry_failed.js
 *          [--max-age-hours 36]
 *          [--limit 100]
 *          [--concurrency 8]
 *          [--tier-a-cap 30]
 *          [--dry-run]
 */

const fs   = require('fs');
const path = require('path');

const OPENCLAW_DIR = process.env.OPENCLAW_DIR || path.join(__dirname, '../../..');

try {
  for (const line of fs.readFileSync(path.join(OPENCLAW_DIR, '.env'), 'utf8').split('\n')) {
    const m = /^([A-Z_][A-Z0-9_]*)=(.*)$/.exec(line.trim());
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2];
  }
} catch (_) {}

const ResearchOrchestrator = require('../research/research-orchestrator');

function _query(sql, params = []) {
  const { Pool } = require('pg');
  if (!_query._pool) _query._pool = new Pool({ connectionString: process.env.POSTGRES_URI, max: 4 });
  return _query._pool.query(sql, params);
}

function getArg(name, fallback = null) {
  const i = process.argv.indexOf(name);
  if (i < 0) return fallback;
  const next = process.argv[i + 1];
  if (!next || next.startsWith('--')) return true;
  return next;
}

async function main() {
  const dryRun      = !!getArg('--dry-run', false);
  const maxAgeHrs   = parseInt(getArg('--max-age-hours', '36'), 10);
  const limit       = parseInt(getArg('--limit', '100'), 10);
  const concurrency = parseInt(getArg('--concurrency', '8'), 10);
  const tierACap    = parseInt(getArg('--tier-a-cap', '30'), 10);
  const log = (m) => console.error(`[retry] ${m}`);

  log(`Starting retry for fetch_failed candidates (max-age=${maxAgeHrs}h, limit=${limit}, concurrency=${concurrency}, dryRun=${dryRun})`);

  const { rows: failed } = await _query(
    `SELECT candidate_id::text AS candidate_id, source_url
       FROM research_candidates
      WHERE hunter_result_json->>'rejection_reason_if_any' = 'fetch_failed'
        AND submitted_at > NOW() - INTERVAL '${maxAgeHrs} hours'
      ORDER BY priority DESC, submitted_at DESC
      LIMIT $1`,
    [limit]
  );
  log(`Found ${failed.length} fetch_failed candidates eligible for retry`);
  if (failed.length === 0) { process.exit(0); }
  if (dryRun) {
    for (const c of failed.slice(0, 10)) log(`  ${c.candidate_id.slice(0,8)} ${c.source_url.slice(0, 80)}`);
    log('DRY RUN — exiting before fan-out.');
    process.exit(0);
  }

  // Clear the old hunter_result_json so re-extraction writes fresh.
  await _query(
    `UPDATE research_candidates
        SET hunter_result_json = NULL
      WHERE candidate_id::text = ANY($1::text[])`,
    [failed.map(f => f.candidate_id)]
  );
  log(`Cleared old hunter_result_json on ${failed.length} rows.`);

  // Re-fan paperhunter.
  const orch = new ResearchOrchestrator();
  log(`Spawning paperhunter on ${failed.length} candidates (concurrency=${concurrency})...`);
  const t0 = Date.now();
  const results = await orch.runHunterFanout(
    failed.map(f => f.candidate_id),
    { concurrency, notify: (m) => log(`  hunt: ${m}`) }
  );
  const elapsed = ((Date.now() - t0) / 1000).toFixed(0);
  log(`Hunt complete in ${elapsed}s.`);

  // Summarize.
  const passed   = results.filter(r => r && r.strategy_id && !r.rejection_reason_if_any).length;
  const stillFF  = results.filter(r => r && r.rejection_reason_if_any === 'fetch_failed').length;
  const otherRej = results.filter(r => r && r.rejection_reason_if_any && r.rejection_reason_if_any !== 'fetch_failed').length;
  log(`Outcomes: ${passed} extracted with strategy_id, ${stillFF} still fetch_failed, ${otherRej} rejected by other gate`);

  // Now run the finisher on these candidates so tier + code + stage happens.
  log(`Chaining into saturday_brain_finisher to tier/code/stage the newly-extracted specs...`);
  const { spawn } = require('child_process');
  const finisher = spawn(
    'node',
    [
      path.join(__dirname, 'saturday_brain_finisher.js'),
      '--tier-a-cap', String(tierACap),
      '--since-iso', new Date(Date.now() - maxAgeHrs * 3600 * 1000).toISOString().slice(0, 10),
    ],
    { cwd: OPENCLAW_DIR, env: process.env, stdio: ['ignore', 'inherit', 'inherit'] }
  );
  finisher.on('close', (code) => {
    log(`finisher exit ${code}`);
    process.exit(code ?? 0);
  });
}

main().catch((e) => {
  console.error('[retry] FATAL:', e.message);
  console.error(e.stack);
  process.exit(1);
});
