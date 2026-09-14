'use strict';

const { test } = require('node:test');
const assert   = require('node:assert/strict');
const path     = require('node:path');
const fs       = require('node:fs');

const ROOT     = path.resolve(__dirname, '..');
const helpers  = require(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));
const capped   = require(path.join(ROOT, 'src/lib/capped_spawn.js'));

test('stepMemoryMax defaults to 4500M and reads OPENCLAW_STEP_MEMORY_MAX', () => {
  const old = process.env.OPENCLAW_STEP_MEMORY_MAX;
  delete process.env.OPENCLAW_STEP_MEMORY_MAX;
  try {
    assert.equal(helpers.stepMemoryMax(), '4500M');
    process.env.OPENCLAW_STEP_MEMORY_MAX = '3G';
    assert.equal(helpers.stepMemoryMax(), '3G');
  } finally {
    if (old === undefined) delete process.env.OPENCLAW_STEP_MEMORY_MAX;
    else process.env.OPENCLAW_STEP_MEMORY_MAX = old;
  }
});

test('runSubprocess reports memoryMax=null and still works when scopes are unavailable', async () => {
  capped._internals.reset({ probe: () => false, uid: () => 1001 });
  try {
    const out = await helpers.runSubprocess(['echo', 'hi'], { timeoutSec: 5, env: process.env });
    assert.equal(out.rc, 0);
    assert.match(out.stdout || '', /hi/);
    assert.equal(out.memoryMax, null);
  } finally {
    capped._internals.reset();
  }
});

test('runSubprocess wraps the argv in a MemoryMax scope when available', async () => {
  // Pretend scopes work but never actually run systemd-run: assert on the
  // wrapper contract instead, using the same call runSubprocess makes.
  // Depends on the ambient default ('4500M'), so pin OPENCLAW_STEP_MEMORY_MAX
  // unset for the duration — same discipline as the first test above.
  const old = process.env.OPENCLAW_STEP_MEMORY_MAX;
  delete process.env.OPENCLAW_STEP_MEMORY_MAX;
  capped._internals.reset({ probe: () => true, uid: () => 0 });
  try {
    const w = capped.wrapCapped('echo', ['hi'], { memoryMax: helpers.stepMemoryMax() });
    assert.equal(w.capped, true);
    assert.equal(w.cmd, 'systemd-run');
    assert.deepEqual(w.args.slice(0, 6),
      ['--scope', '--collect', '--quiet', '-p', 'MemoryMax=4500M', '--']);
  } finally {
    capped._internals.reset();
    if (old === undefined) delete process.env.OPENCLAW_STEP_MEMORY_MAX;
    else process.env.OPENCLAW_STEP_MEMORY_MAX = old;
  }
});

test('memoryMax "0" disables the wrap even when scopes are available', async () => {
  capped._internals.reset({ probe: () => true, uid: () => 0 });
  try {
    const out = await helpers.runSubprocess(['echo', 'hi'],
      { timeoutSec: 5, env: process.env, memoryMax: '0' });
    assert.equal(out.rc, 0);
    assert.equal(out.memoryMax, null);
  } finally {
    capped._internals.reset();
  }
});

// Beyond the brief: test 3 above only asserts on capped_spawn.wrapCapped's own
// contract (already covered by tests/lib/capped_spawn.test.js) — it never
// calls helpers.runSubprocess, so it does not prove runSubprocess actually
// wires the cap through. capped_spawn._internals can't reach that because
// daily_cycle_helpers.js destructures wrapCapped at module load (the
// reference is frozen), so inject the whole module via require.cache instead
// — the same pattern tests/test_daily_cycle_node.test.js already uses — and
// assert on the real argv wrapCapped hands to spawn.
test('runSubprocess passes the step cap to wrapCapped and spawns the wrapped argv', async () => {
  // Also depends on the ambient default (asserted below as '4500M') — pin it
  // unset for the duration, same as the first test above.
  const old = process.env.OPENCLAW_STEP_MEMORY_MAX;
  delete process.env.OPENCLAW_STEP_MEMORY_MAX;
  const CAPPED  = require.resolve(path.join(ROOT, 'src/lib/capped_spawn.js'));
  const HELPERS = require.resolve(path.join(ROOT, 'src/agent/graphs/daily_cycle_helpers.js'));
  const realCapped = require.cache[CAPPED];
  const calls = [];
  require.cache[CAPPED] = {
    id: CAPPED, filename: CAPPED, loaded: true,
    exports: {
      // daily_cycle_helpers.js now re-exports stepMemoryMax/DEFAULT_STEP_MEMORY_MAX
      // from this module (QD E2 fix round 1) — the stand-in must provide
      // them too, or the default-cap / fallback path throws "is not a
      // function" the moment it's re-required fresh via require.cache below.
      // Delegate to the REAL implementations (captured above, before the
      // swap) rather than hardcoding '4500M' — hardcoding would make this
      // assertion pass even if the real resolver regressed.
      stepMemoryMax: realCapped.exports.stepMemoryMax,
      DEFAULT_STEP_MEMORY_MAX: realCapped.exports.DEFAULT_STEP_MEMORY_MAX,
      wrapCapped: (cmd, args, opts) => {
        calls.push({ cmd, args, opts });
        return { cmd: 'echo', args: ['WRAPPED', cmd, ...args], capped: true, memoryMax: opts.memoryMax };
      },
    },
  };
  delete require.cache[HELPERS];
  try {
    const h2  = require(HELPERS);
    const out = await h2.runSubprocess(['python3', 'x.py'], { timeoutSec: 5, env: process.env });
    assert.deepEqual(calls[0],
      { cmd: 'python3', args: ['x.py'], opts: { memoryMax: '4500M', fallback: '4500M' } });
    assert.match(out.stdout, /WRAPPED python3 x\.py/); // the wrapped argv really reached spawn
    assert.equal(out.memoryMax, '4500M');
  } finally {
    require.cache[CAPPED] = realCapped;
    delete require.cache[HELPERS];
    if (old === undefined) delete process.env.OPENCLAW_STEP_MEMORY_MAX;
    else process.env.OPENCLAW_STEP_MEMORY_MAX = old;
  }
});

test('cron-schedule spawns the orchestrator through wrapCapped', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src/engine/cron-schedule.js'), 'utf8');
  const hits = src.match(/wrapCapped\(/g) || [];
  assert.ok(hits.length >= 2, `expected >= 2 wrapCapped call sites, found ${hits.length}`);
  assert.ok(src.includes("require('../lib/capped_spawn')"), 'must require capped_spawn');
  assert.ok(src.includes('OPENCLAW_STEP_MEMORY_MAX'), 'must use the step cap var');
  // The OPENCLAW_STEP_MEMORY_MAX check above is satisfied by the explanatory
  // comments at both call sites, which is source text, not code — pin the
  // actual resolver call too so the check can't be satisfied by a comment
  // alone if the real capped.stepMemoryMax() call is ever deleted.
  const resolverHits = src.match(/capped\.stepMemoryMax\(\)/g) || [];
  assert.equal(resolverHits.length, 2, `expected exactly 2 capped.stepMemoryMax() call sites, found ${resolverHits.length}`);
  // Both spawn() calls must actually pass the wrapped argv (w.cmd/w.args),
  // not the raw command — source-text only, no module load needed.
  const spawnHits = src.match(/spawn\(w\.cmd,\s*w\.args/g) || [];
  assert.equal(spawnHits.length, 2, `expected exactly 2 spawn(w.cmd, w.args) sites, found ${spawnHits.length}`);
});
