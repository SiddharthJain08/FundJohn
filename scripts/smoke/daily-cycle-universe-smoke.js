/**
 * SP-2 Phase A smoke: daily-cycle.js perStrategyUniverse state + helper.
 * Verifies the loadPerStrategyUniverse helper shells out to the resolver
 * CLI and returns {sid: [...tickers]} for each live strategy. Does NOT
 * exercise the full graph — that smoke lives in graph-smoke.js (which
 * tests an older graph).
 *
 * Run: node test/daily-cycle-universe-smoke.js
 */
'use strict';

const assert = require('assert');
process.env.OPENCLAW_LANGGRAPH_USE_MEMORY_SAVER = '1';  // skip Postgres checkpointer

const { loadPerStrategyUniverse } = require('../src/agent/graphs/daily-cycle');

async function main() {
  // Pick two real live strategies from the manifest (any pair) to exercise
  // the CLI shell-out. The test passes if the helper returns objects with
  // ticker arrays. Empty arrays are acceptable (e.g., if the strategy
  // doesn't yet have a snapshot with > coverage floor) — we test shape,
  // not size.
  const fs = require('fs');
  const manifest = JSON.parse(fs.readFileSync('/root/openclaw/src/strategies/manifest.json', 'utf8'));
  const liveSids = Object.entries(manifest.strategies || {})
    .filter(([, r]) => r.state === 'live')
    .map(([sid]) => sid);
  if (liveSids.length === 0) {
    console.log('[smoke] no live strategies; skipping');
    return;
  }
  const sample = liveSids.slice(0, 2);
  const today = '2026-05-22';

  const result = loadPerStrategyUniverse(today, sample);

  assert.strictEqual(typeof result, 'object', 'result must be an object');
  for (const sid of sample) {
    assert.ok(sid in result, `result must contain ${sid}`);
    assert.ok(Array.isArray(result[sid]), `${sid} value must be an array`);
  }

  console.log(`[smoke] loadPerStrategyUniverse OK: ${sample.length} sids resolved`);
  for (const sid of sample) {
    console.log(`  ${sid}: ${result[sid].length} tickers`);
  }
}

main().catch((e) => { console.error('[smoke] FAIL:', e); process.exit(1); });
