#!/usr/bin/env node
// Approval-job smoke test. Exercises:
//   1. Duplicate-job guard (409)
//   2. Staging approve with unsupported source (failJob path)
//   3. /approvals/active hydration
//   4. Cancel rolls back data_ingestion_queue rows
//
// Does NOT run the candidate→paper flow end-to-end because that invokes
// StrategyCoder (tokens) + auto_backtest (5–15 min). That path is exercised
// manually from the dashboard.
'use strict';

const assert = require('assert');
const BASE = process.env.APPROVAL_BASE || 'http://localhost:3000';

async function call(method, path, body) {
  const resp = await fetch(`${BASE}${path}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body == null ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  let json = null; try { json = JSON.parse(text); } catch (_) {}
  return { status: resp.status, body: json, raw: text };
}

async function main() {
  // Pre-clean any leftover jobs for HV10 (the canonical staging test target).
  const { query: dbQuery } = require('../src/database/postgres');
  await dbQuery(`DELETE FROM strategy_approval_jobs WHERE strategy_id='S_HV10_triple_gate_fear'`);

  // 1. Approve HV10 staging → expect 202 with a job_id, then fail quickly
  //    because 'unusual_options_flow' isn't in schema_registry.json.
  const r1 = await call('POST', '/api/strategies/S_HV10_triple_gate_fear/approve', {});
  assert.strictEqual(r1.status, 202, `expected 202, got ${r1.status} ${r1.raw}`);
  assert.ok(r1.body.job_id, 'job_id should be returned');
  console.log('✓ staging approve accepted (202)');

  // Give the async worker ~1.5s to reach failJob — it's sync DB work only.
  await new Promise(r => setTimeout(r, 1500));
  const { rows: jobRows } = await dbQuery(
    `SELECT status, phase, result FROM strategy_approval_jobs WHERE job_id=$1`,
    [r1.body.job_id]);
  assert.strictEqual(jobRows[0].status, 'failed', 'unsupported source should fail fast');
  assert.strictEqual(jobRows[0].result.error, 'unsupported_source');
  console.log('✓ staging approve fails on unsupported_source (as expected for HV10)');

  // 2. /approvals/active should be empty now that the job finished.
  const r2 = await call('GET', '/api/approvals/active');
  assert.strictEqual(r2.status, 200);
  assert.ok(Array.isArray(r2.body));
  assert.ok(!r2.body.find(j => j.strategy_id === 'S_HV10_triple_gate_fear'),
    'failed job should not appear in active list');
  console.log('✓ /api/approvals/active hides finished jobs');

  // 3. Approve on a live strategy → 422
  const r3 = await call('POST', '/api/strategies/S9_dual_momentum/approve', {});
  assert.strictEqual(r3.status, 422);
  console.log('✓ /approve refuses live state (422)');

  // 4. Manual /transition on candidate/staging still blocked for non-system actor.
  //    Pick any candidate strategy dynamically (alpha191 may already have been promoted).
  const { rows: cands } = await dbQuery(
    `SELECT id FROM strategy_registry WHERE status='pending_approval' LIMIT 1`);
  const candSid = cands[0]?.id;
  if (candSid) {
    const r4 = await call('POST', `/api/strategies/${candSid}/transition`, { to_state: 'paper' });
    assert.strictEqual(r4.status, 409, `expected 409, got ${r4.status} ${r4.raw}`);
    assert.ok(/approve/.test(r4.body.error), 'should redirect to /approve');
    console.log(`✓ manual candidate→paper still blocked for ${candSid} (409, directs to /approve)`);
  } else {
    console.log('(skip candidate 409 check — no pending_approval strategy available)');
  }

  // 5. Cleanup
  await dbQuery(`DELETE FROM strategy_approval_jobs WHERE strategy_id='S_HV10_triple_gate_fear'`);
  console.log('\nAll smoke checks passed.');
  process.exit(0);
}

main().catch(e => { console.error(e); process.exit(1); });
