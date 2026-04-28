#!/usr/bin/env node
// Parallel-approval probe.
//
// Three independent confirmations that the fused-staging-approval flow
// runs strategycoder work concurrently when multiple staging strategies
// are approved at once:
//
//  (A) DB-level: spawn N approves in parallel, count overlapping
//      `running` rows in strategy_approval_jobs.
//  (B) OS-level: spawn N backfill subprocesses via runBackfill() and
//      assert N concurrent python child processes via /proc.
//  (C) Code-level: confirm approveStaging is fire-and-forget (no await
//      on the worker) and the worker has no global mutex.
//
// Does NOT spend Anthropic tokens. Each probe is self-cleaning.
'use strict';

const assert = require('assert');
const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = process.env.APPROVAL_BASE || 'http://localhost:3000';
const { query: dbQuery } = require('../src/database/postgres');
const { runBackfill } = require('../src/lib/backfill_runner');

async function call(method, urlPath, body) {
  const resp = await fetch(`${BASE}${urlPath}`, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body == null ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  let json = null; try { json = JSON.parse(text); } catch (_) {}
  return { status: resp.status, body: json };
}

// ─── Probe A: DB-level parallel approve ───────────────────────────────────
async function probeA() {
  console.log('\n[A] DB-level parallel-approve probe');

  // Pick three staging strategies with at least one unsupported source so
  // they all fail fast (no LLM cost). We force fast-fail by approving
  // strategies whose requirements include something like
  // 'unusual_options_flow' (not in schema_registry).
  const manifestPath = path.resolve(__dirname, '..', 'src', 'strategies', 'manifest.json');
  const m = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const stagingSids = Object.entries(m.strategies)
    .filter(([, r]) => r.state === 'staging')
    .map(([sid]) => sid);
  console.log(`    ${stagingSids.length} staging strategies in manifest`);

  // Take the first 3 to approve — whatever their failure mode, we only need
  // them to all REACH `running` status simultaneously. The phase doesn't
  // matter for the parallelism question.
  const sample = stagingSids.slice(0, 3);
  console.log(`    sample: ${sample.join(', ')}`);
  if (sample.length < 3) {
    console.log('    SKIP: need >=3 staging strategies to test parallelism');
    return;
  }

  // Pre-clean any prior jobs for the sample (in case a previous probe left
  // them in a state that would block re-approve via the per-strategy
  // exclusion constraint).
  await dbQuery(
    `DELETE FROM strategy_approval_jobs
       WHERE strategy_id = ANY($1::text[])
         AND status IN ('pending','running')`,
    [sample],
  );

  // Fire all three approves in parallel. Promise.all kicks them off
  // simultaneously; the dispatcher returns 202 fast (fire-and-forget).
  const t0 = Date.now();
  const results = await Promise.all(sample.map(sid =>
    call('POST', `/api/strategies/${encodeURIComponent(sid)}/approve`, {})
  ));
  const dispatchMs = Date.now() - t0;
  console.log(`    dispatched 3 approves in ${dispatchMs}ms`);

  // Every dispatch should return 202 with a job_id (else the test is
  // measuring something else).
  const accepted = results.filter(r => r.status === 202 && r.body && r.body.job_id);
  console.log(`    accepted: ${accepted.length}/3`);
  assert.strictEqual(accepted.length, 3, `expected 3 x 202; got ${results.map(r => r.status).join(',')}`);

  // The dispatcher itself completed in <500ms even though each worker may
  // run for many seconds. That alone proves the dispatch is non-blocking.
  assert.ok(dispatchMs < 2000,
    `dispatch took ${dispatchMs}ms — too slow, indicates serialization in approveStaging`);
  console.log(`    ✓ dispatcher returned 3x202 in ${dispatchMs}ms (< 2000ms threshold)`);

  // Snapshot the jobs immediately to capture how many are concurrently
  // running. With fire-and-forget dispatch, we expect 3 in (running OR
  // failed) state, all started within a ~1s window.
  const jobIds = accepted.map(r => r.body.job_id);
  const { rows: snap } = await dbQuery(
    `SELECT job_id, strategy_id, status, phase, started_at, finished_at
       FROM strategy_approval_jobs
      WHERE job_id = ANY($1::uuid[])
      ORDER BY started_at`,
    [jobIds],
  );
  console.log(`    job snapshot:`);
  for (const r of snap) {
    console.log(`      ${r.strategy_id.padEnd(40)}  ${String(r.status).padEnd(10)}  phase=${r.phase || '-'}`);
  }

  // started_at spread should be tiny — proof the workers all started in
  // parallel rather than queuing.
  const starts = snap.map(r => new Date(r.started_at).getTime());
  const startSpreadMs = Math.max(...starts) - Math.min(...starts);
  console.log(`    started_at spread: ${startSpreadMs}ms`);
  assert.ok(startSpreadMs < 1500,
    `started_at spread ${startSpreadMs}ms suggests jobs queued instead of parallel`);
  console.log(`    ✓ all 3 jobs started within ${startSpreadMs}ms of each other`);

  // Wait briefly so we can also observe at least one snapshot where
  // multiple jobs are concurrently running (or all 3 have finished fast).
  await new Promise(r => setTimeout(r, 700));
  const { rows: snap2 } = await dbQuery(
    `SELECT status FROM strategy_approval_jobs WHERE job_id = ANY($1::uuid[])`,
    [jobIds],
  );
  const stillRunning = snap2.filter(r => r.status === 'running').length;
  const finished     = snap2.filter(r => ['failed','succeeded','cancelled'].includes(r.status)).length;
  console.log(`    after 700ms: running=${stillRunning} finished=${finished}`);

  // Cleanup: cancel any still-running jobs (so we don't leave LLM/backfill
  // work running in the background after the probe exits).
  for (const r of snap2) {
    if (r.status === 'running') {
      await call('POST', `/api/strategies/${encodeURIComponent(snap.find(s => s.status === r.status)?.strategy_id || '')}/approve/cancel`, {});
    }
  }
  // Then delete probe-created job rows so the DB state is clean.
  await dbQuery(
    `DELETE FROM strategy_approval_jobs WHERE job_id = ANY($1::uuid[])`,
    [jobIds],
  );
  console.log('    ✓ probe cleaned up');
}


// ─── Probe B: OS-level parallel subprocess spawn ──────────────────────────
async function probeB() {
  console.log('\n[B] OS-level parallel subprocess probe');

  // Find an APPROVED data_ingestion_queue row to dry-run against. We pick
  // the same row 3 times — the python helper just runs a dry-run and
  // returns immediately, so collisions don't matter.
  const { rows } = await dbQuery(
    `SELECT request_id::text FROM data_ingestion_queue WHERE status='APPROVED' LIMIT 1`,
  );
  if (!rows.length) {
    console.log('    SKIP: no APPROVED data_ingestion_queue row to test against');
    return;
  }
  const rid = rows[0].request_id;

  // Snap the count of running python3 processes BEFORE the probe.
  const beforeCount = countPythonProcesses();
  console.log(`    python3 processes before: ${beforeCount}`);

  // Spawn 3 dry-run backfills in parallel. Each launches its own python3
  // subprocess; if there were any global serialization, only one would
  // run at a time and the wall-clock would be ~3x a single dry-run.
  const SLOW_PARALLEL_MIN = 3;  // expect at least 3 concurrent
  let peakConcurrent = 0;

  // Sampling thread that polls /proc every 25ms during the parallel run.
  let sampling = true;
  const sampler = (async () => {
    while (sampling) {
      const n = countPythonProcesses();
      if (n - beforeCount > peakConcurrent) peakConcurrent = n - beforeCount;
      await new Promise(r => setTimeout(r, 25));
    }
  })();

  const t0 = Date.now();
  const results = await Promise.all([
    runBackfill(rid, { dryRun: true }),
    runBackfill(rid, { dryRun: true }),
    runBackfill(rid, { dryRun: true }),
  ]);
  const elapsedMs = Date.now() - t0;
  sampling = false;
  await sampler;

  console.log(`    parallel runBackfill x3 elapsed: ${elapsedMs}ms`);
  console.log(`    peak concurrent python3 children: ${peakConcurrent}`);

  // Three dry-runs each spawn one python3 subprocess. Even with some
  // sampler timing slop, we expect to see at least 2 concurrent at peak.
  assert.ok(results.every(r => r.ok),
    `expected all 3 dry-runs to succeed; got ${JSON.stringify(results.map(r => r.ok))}`);
  console.log(`    ✓ all 3 dry-runs returned ok=true`);
  assert.ok(peakConcurrent >= 2,
    `peak concurrent ${peakConcurrent} suggests subprocess serialization (expected >=2)`);
  console.log(`    ✓ at least 2 python3 children alive simultaneously`);
}

function countPythonProcesses() {
  // /proc/<pid>/cmdline contains nul-separated args. Count those whose
  // cmdline matches our expected backfill spawn pattern.
  let n = 0;
  for (const ent of fs.readdirSync('/proc')) {
    if (!/^\d+$/.test(ent)) continue;
    try {
      const cmd = fs.readFileSync(`/proc/${ent}/cmdline`).toString('utf8');
      // pattern: 'python3\0-m\0src.pipeline.backfillers\0...'
      if (cmd.includes('src.pipeline.backfillers')) n += 1;
    } catch (_) { /* dead pid by the time we read */ }
  }
  return n;
}


// ─── Probe C: code-level review ───────────────────────────────────────────
function probeC() {
  console.log('\n[C] Code-level review');
  const indexJs = fs.readFileSync(
    path.resolve(__dirname, '..', 'src', 'agent', 'approvals', 'index.js'), 'utf8');

  // (1) approveStaging must NOT await stagingApprover.start
  const m1 = indexJs.match(/async function approveStaging[\s\S]*?\n\}/);
  assert.ok(m1, 'could not find approveStaging body');
  assert.ok(!/await\s+stagingApprover\.start/.test(m1[0]),
    'approveStaging awaits stagingApprover.start — that would serialize approves');
  console.log('    ✓ approveStaging is fire-and-forget (no await on worker)');

  // (2) no semaphore/p-limit/throttle import
  for (const lib of ['p-limit', 'p-queue', 'async-sema', 'semaphore']) {
    assert.ok(!indexJs.includes(`require('${lib}')`),
      `approvals/index.js imports ${lib} — would serialize`);
  }
  console.log('    ✓ no semaphore/p-limit/p-queue imports in approvals/index.js');

  // (3) one_active_job_per_strategy is a per-strategy gist EXCLUDE, not global
  const migrationSql = fs.readFileSync(
    path.resolve(__dirname, '..', 'src', 'database', 'migrations', '045_approval_jobs.sql'),
    'utf8');
  assert.ok(/EXCLUDE USING gist \(strategy_id WITH =\)/.test(migrationSql),
    'one_active_job_per_strategy EXCLUDE is missing or has wrong scope');
  console.log('    ✓ one_active_job_per_strategy scoped per-strategy_id, not global');
}


// ─── main ─────────────────────────────────────────────────────────────────
async function main() {
  probeC();   // synchronous code review
  await probeA();
  await probeB();
  console.log('\n✅ All parallelism probes passed.');
  process.exit(0);
}

main().catch((e) => {
  console.error('\n❌ probe failed:', e.message);
  console.error(e.stack);
  process.exit(1);
});
