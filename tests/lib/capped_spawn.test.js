/**
 * src/lib/capped_spawn.js — wrap a child command in a MemoryMax-capped
 * transient systemd scope (2026-08-30). Root cause it addresses: johnbot's
 * approval-job backtests (research-orchestrator._spawnPython), backfills and
 * universe-shrink runs were plain python3 children of the user-scope johnbot
 * service with NO memory cap; on 2026-08-30 01:41/01:48 two of them (3.1 GB
 * and 5.4 GB anon) triggered the kernel's GLOBAL OOM killer on the 8 GB box.
 * `systemd-run --scope` execs the command in place (same pid), so callers'
 * child.kill()/pid registration are unaffected.
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const cs = require('../../src/lib/capped_spawn');

test('disabled cap (0 / empty) passes the command through untouched', () => {
  cs._internals.reset({ probe: () => true, uid: 0 });
  for (const cap of ['0', '']) {
    const r = cs.wrapCapped('python3', ['-m', 'x'], { memoryMax: cap });
    assert.deepEqual(r, { cmd: 'python3', args: ['-m', 'x'], capped: false, memoryMax: null });
  }
  // undefined = "use the default cap" (default-on), never a silent disable
  assert.equal(cs.wrapCapped('python3', ['-m', 'x'], { memoryMax: undefined }).capped, true);
});

test('non-root or failed probe passes through (finisher runs as claudebot, no scope rights)', () => {
  cs._internals.reset({ probe: () => true, uid: 1001 });
  assert.equal(cs.wrapCapped('python3', ['a'], { memoryMax: '4500M' }).capped, false);
  cs._internals.reset({ probe: () => false, uid: 0 });
  assert.equal(cs.wrapCapped('python3', ['a'], { memoryMax: '4500M' }).capped, false);
});

test('root + working probe wraps in a transient scope with the cap; probe runs once', () => {
  let probes = 0;
  cs._internals.reset({ probe: () => { probes += 1; return true; }, uid: 0 });
  const r1 = cs.wrapCapped('python3', ['-m', 'backtest.unified_backtest', '--strategy-id', 'S_x'], { memoryMax: '4500M' });
  const r2 = cs.wrapCapped('python3', ['a'], { memoryMax: '3G' });
  assert.equal(probes, 1);
  assert.equal(r1.cmd, 'systemd-run');
  assert.deepEqual(r1.args, ['--scope', '--collect', '--quiet', '-p', 'MemoryMax=4500M', '--',
                             'python3', '-m', 'backtest.unified_backtest', '--strategy-id', 'S_x']);
  assert.equal(r1.capped, true);
  assert.equal(r1.memoryMax, '4500M');
  assert.equal(r2.args[4], 'MemoryMax=3G');
});

test('default cap comes from OPENCLAW_BACKTEST_MEMORY_MAX (default 4500M)', () => {
  cs._internals.reset({ probe: () => true, uid: 0 });
  const saved = process.env.OPENCLAW_BACKTEST_MEMORY_MAX;
  delete process.env.OPENCLAW_BACKTEST_MEMORY_MAX;
  assert.equal(cs.defaultMemoryMax(), '4500M');
  process.env.OPENCLAW_BACKTEST_MEMORY_MAX = '2G';
  assert.equal(cs.wrapCapped('python3', []).memoryMax, '2G');
  process.env.OPENCLAW_BACKTEST_MEMORY_MAX = '0';
  assert.equal(cs.wrapCapped('python3', []).capped, false);
  if (saved === undefined) delete process.env.OPENCLAW_BACKTEST_MEMORY_MAX; else process.env.OPENCLAW_BACKTEST_MEMORY_MAX = saved;
});

test('real scope (root only): the cap is enforced and the pid is the command itself', { skip: process.getuid() !== 0 }, () => {
  cs._internals.reset();                       // real probe
  const { spawnSync } = require('node:child_process');
  const w = cs.wrapCapped('python3', ['-c', 'import os,sys; print(os.getpid()); sys.stdout.flush(); b=bytearray(300*1024*1024); print("survived")'], { memoryMax: '100M' });
  assert.equal(w.capped, true, 'probe should succeed as root on this box');
  const r = spawnSync(w.cmd, w.args, { encoding: 'utf8' });
  assert.notEqual(r.status, 0);                // killed by the cgroup, never "survived"
  assert.ok(!/survived/.test(r.stdout));
});
