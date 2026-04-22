/**
 * Smoke test: runCycleGraph with stubbed subagents / runner.
 *   1. Invoke graph → confirm interrupt pause before botjohn.
 *   2. Resume with approval='approved' → confirm botjohn runs + status=ok.
 *   3. Run second cycle with vetoed approval → confirm short-circuit.
 *
 * Run: node test/graph-smoke.js
 */
'use strict';

const Module = require('module');

// Monkey-patch runner + swarm BEFORE graph.js requires them.
const origLoad = Module._load;
Module._load = function(request, parent, ...rest) {
  if (parent && parent.filename && parent.filename.includes('/agent/graph.js')) {
    if (request === '../execution/runner') {
      return { runDailyClose: async () => ({ stub: true, signals_generated: 3 }) };
    }
    if (request === './subagents/swarm') {
      return {
        init: async ({ type, ticker }) => ({
          subagentId: `stub-${type}-${Date.now()}`,
          output: `stub output for ${type}/${ticker}\nsignals_generated: 2`,
          duration: 42,
        }),
      };
    }
  }
  return origLoad.call(this, request, parent, ...rest);
};

const assert = (cond, msg) => { if (!cond) { console.error('[smoke] FAIL', msg); process.exit(1); } };

(async () => {
  const { runCycleGraph, resumeCycle } = require('../src/agent/graph');
  const traceBus = require('../src/agent/traceBus');

  // ── Phase 1: interrupt ─────────────────────────────────────────────────────
  console.log('[smoke] phase 1 — expect interrupt before botjohn');
  const threadId = `smoke-${Date.now()}`;
  const first = await runCycleGraph({
    cycleDate: '2026-04-22-smoke',
    portfolioState: { nav: 100_000 },
    memoDir: '/tmp/smoke-memos',
    reportPath: '/tmp/smoke-report.md',
    threadId,
    notify: (m) => console.log('  [notify]', m),
  });
  assert(first.status === 'awaiting_approval', `expected awaiting_approval, got ${first.status}`);
  assert(first.next?.includes('botjohn'), `expected next=[botjohn], got ${JSON.stringify(first.next)}`);
  console.log('  ok — paused, next=', first.next);

  // ── Phase 2: resume approved ───────────────────────────────────────────────
  console.log('[smoke] phase 2 — resume with approved');
  const resumed = await resumeCycle({ threadId, approval: 'approved' });
  assert(resumed.status === 'ok', `expected ok, got ${resumed.status}`);
  assert(resumed.botResult, 'expected botResult after resume');
  console.log('  ok — botResult.subagentId=', resumed.botResult.subagentId);

  // ── Phase 3: vetoed short-circuit ──────────────────────────────────────────
  console.log('[smoke] phase 3 — veto path');
  const threadId2 = `smoke-veto-${Date.now()}`;
  await runCycleGraph({
    cycleDate: '2026-04-22-smoke-2',
    portfolioState: {},
    memoDir: '/tmp/smoke-memos',
    reportPath: '/tmp/smoke-report.md',
    threadId: threadId2,
    notify: () => {},
  });
  const vetoed = await resumeCycle({ threadId: threadId2, approval: 'vetoed' });
  assert(vetoed.status === 'vetoed', `expected vetoed, got ${vetoed.status}`);
  assert(vetoed.botResult?.vetoed === true, 'expected botResult.vetoed=true');
  console.log('  ok — vetoed cleanly');

  const runs = traceBus.listRuns();
  console.log('[smoke] total runs:', runs.length);
  for (const r of runs) console.log('  -', r.runId, r.status, 'events:', r.events.length);
  console.log('[smoke] OK');
  process.exit(0);
})().catch((e) => { console.error('[smoke] ERROR', e); process.exit(1); });
