// tests/test_pipelines_summary.js — tile counts must include durable graphs so the
// panel isn't empty after a restart wipes the in-memory traceBus (live bug:
// graphs:[] despite durable_total:58).
const assert = require('assert');
const { summarizePipelines } = require('../src/channels/api/pipelines_summary');
const NOW = Date.parse('2026-06-29T12:00:00Z');

// 1. Empty traceBus + durable graphs present → graphs is NON-empty (the bug fix).
{
  const s = summarizePipelines([], [{ threadId: 't1', graph: 'daily-cycle' }, { threadId: 't2', graph: 'paperhunter' }], NOW);
  assert.deepStrictEqual([...s.graphs].sort(), ['daily-cycle', 'paperhunter']);
  assert.strictEqual(s.active, 0);
  assert.strictEqual(s.live_window, 'since_restart');
}
// 2. Live runs drive active/today/failures; graphs union live+durable, deduped, no 'unknown'.
{
  const live = [
    { status: 'running', startedAt: NOW - 1000, updatedAt: NOW, meta: { graph: 'daily-cycle' } },
    { status: 'error',   startedAt: NOW - 2000, updatedAt: NOW - 1000, meta: { graph: 'x' } },
  ];
  const s = summarizePipelines(live, [{ threadId: 't', graph: 'daily-cycle' }], NOW);
  assert.strictEqual(s.active, 1);
  assert.strictEqual(s.failures_24h, 1);
  assert.strictEqual(s.today, 2);
  assert.deepStrictEqual([...s.graphs].sort(), ['daily-cycle', 'x']);
}
// 3. Old failure outside 24h not counted.
{
  const live = [{ status: 'failed', startedAt: NOW - 5 * 86400000, updatedAt: NOW - 5 * 86400000, meta: { graph: 'g' } }];
  assert.strictEqual(summarizePipelines(live, [], NOW).failures_24h, 0);
}
console.log('ok test_pipelines_summary');
